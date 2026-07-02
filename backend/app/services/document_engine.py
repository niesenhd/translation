"""文件解析与翻译引擎：DOCX / PDF / TXT / MD / XLSX / XLS / CSV / PPTX。

核心策略：
- 在原文档对象上替换文本，最大限度保留格式
- PDF 先用 pdf2docx 转成 docx，再走 docx 流程
- 对照模式（bilingual）：在每段译文前/后插入对照行
- P1.3：图片 OCR + 翻译（Qwen 多模态），叠加文本框写回
- P1.4：PDF 扫描件整页 OCR
- P1.5：阿拉伯语 RTL 排版 + PDF 长译文回流
"""
from __future__ import annotations

import io
import logging
import os
import re
import tempfile
import threading
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from typing import Callable

from docx import Document
from pdf2docx import Converter

from app.core.config import get_settings
from app.models.task import OutputMode
from app.services.chunker import chunk_paragraphs, total_chars
from app.services.translator import Translator

logger = logging.getLogger(__name__)


@dataclass
class TranslationContext:
    target_lang: str
    source_lang: str = "auto"
    output_mode: OutputMode = OutputMode.PLAIN
    # 是否翻译图片中的文字（yes=翻译，no=仅翻译文档文字，图片保持原样）
    translate_images: str = "yes"
    # 进度回调：(已完成段数, 总段数) -> None
    on_progress: Callable[[int, int], None] | None = field(default=None, repr=False)
    # 术语库（由调用方注入）
    glossary: list[dict] | None = field(default=None, repr=False)
    # 翻译记忆库查询函数（由调用方注入）
    tm_lookup: Callable | None = field(default=None, repr=False)


# ---------------------- 公共工具 ----------------------


def _bilingual_join(original: str, translated: str) -> str:
    """对照模式：原文 + 换行 + 译文。"""
    if not original.strip():
        return original
    return f"{original}\n{translated}"


def _translate_text(translator: Translator, text: str, ctx: TranslationContext) -> str:
    if not text.strip():
        return text

    # 优先查翻译记忆库
    if ctx.tm_lookup is not None:
        tm_match = ctx.tm_lookup(text, ctx.source_lang, ctx.target_lang)
        if tm_match and tm_match.get("exact"):
            # 完全一致（归一化后逐字相等）才直接复用译文。
            # 高相似但非全等（如仅日期/金额不同）绝不可直接套用——会把
            # 旧记忆中的数字/当事人名写进当前文书。
            translated = tm_match["target_text"]
        elif tm_match and tm_match.get("similarity", 0) >= 0.8:
            # 高相似非全等（80%+），将 TM 结果作为参考注入 prompt
            translated = translator.translate(
                text, ctx.target_lang, ctx.source_lang,
                glossary=ctx.glossary,
                tm_reference=tm_match["target_text"],
            )
        else:
            translated = translator.translate(text, ctx.target_lang, ctx.source_lang, glossary=ctx.glossary)
    else:
        translated = translator.translate(text, ctx.target_lang, ctx.source_lang, glossary=ctx.glossary)
    if not translated or not translated.strip():
        # 空译文（API 安全过滤/截断返回空 content）一律保留原文兜底，
        # 否则 docx 写回路径会把整段原文抹掉
        logger.warning("译文为空，保留原文兜底：%.60s", text)
        translated = text
    if ctx.output_mode == OutputMode.BILINGUAL:
        return _bilingual_join(text, translated)
    return translated


def _translate_many(
    texts: list[str],
    translator: Translator,
    ctx: TranslationContext,
) -> list[str]:
    """段落级并发翻译：保持输入顺序，空段直接返回原值；不可翻译段不计入进度。

    特点：
    - 顺序保持：用 list[idx] 写回，不依赖 future 完成顺序
    - 进度逐段触发：每段完成都调一次 on_progress
    - 异常透传：单段失败直接抛出，由上层 Celery 任务转 FAILED
    - 大文件分片调度：当总字符数超过阈值时，按分片顺序处理（片内仍并发），
      避免一次性把成百上千段全部塞进线程池占满 DashScope 配额
    """
    settings = get_settings()
    workers = max(1, int(settings.translation_paragraph_concurrency))

    # 是否触发分片调度
    use_chunking = total_chars(texts) > settings.translation_large_file_threshold

    results: list[str] = [""] * len(texts)
    progress_total = max(sum(1 for t in texts if t and t.strip()), 1)
    progress_state = {"done": 0, "fail": 0, "fail_reasons": []}
    lock = threading.Lock()

    # 单段失败容忍策略：
    # - 内容性错误（ContentRejectedError，400）：不计数，仅保留原文兜底
    # - 系统性错误（限流/超时/连接）：失败率 > 40%（且失败 >= 5 段）→ 整体抛错让任务失败
    # - 单段失败 → 用原文兜底，记录到 progress_state，但继续翻译
    FAIL_RATE_THRESHOLD = 0.4
    FAIL_MIN_COUNT = 5

    def _record_failure(text: str, exc: Exception) -> None:
        # 内容性错误（400 Bad Request）：不计数，仅保留原文兜底
        # 常见原因：个别段落因内容敏感被 API 拒绝，其他段落仍可正常翻译
        from app.services.translator import ContentRejectedError
        if isinstance(exc, ContentRejectedError):
            logger.warning("段落因内容被 API 拒绝，保留原文：%s", str(exc)[:200])
            return

        with lock:
            progress_state["fail"] += 1
            if len(progress_state["fail_reasons"]) < 3:
                progress_state["fail_reasons"].append(str(exc)[:200])
            done = progress_state["done"]
            fail = progress_state["fail"]
        # 触发整体失败：失败数 >= 阈值且失败率超限
        if fail >= FAIL_MIN_COUNT and fail / max(done + fail, 1) > FAIL_RATE_THRESHOLD:
            reasons = "；".join(progress_state["fail_reasons"])
            raise RuntimeError(
                f"翻译服务异常：{fail}/{done + fail} 段失败（触发熔断）。最近错误：{reasons}"
            )

    def _do(idx: int, text: str) -> None:
        try:
            results[idx] = _translate_text(translator, text, ctx)
        except Exception as exc:  # noqa: BLE001
            # 单段失败：保留原文，避免整段空白
            results[idx] = text
            _record_failure(text, exc)
        if text and text.strip():
            with lock:
                progress_state["done"] += 1
                done = progress_state["done"]
            if ctx.on_progress:
                ctx.on_progress(done, progress_total)

    def _run_indices(indices: list[int]) -> None:
        if workers == 1 or len(indices) <= 1:
            for i in indices:
                _do(i, texts[i])
            return
        with ThreadPoolExecutor(max_workers=workers, thread_name_prefix="translate") as ex:
            futures = [ex.submit(_do, i, texts[i]) for i in indices]
            for f in futures:
                f.result()

    if not use_chunking:
        _run_indices(list(range(len(texts))))
        return results

    # 大文件分片：把段落聚合成分片，按片顺序执行（片内仍并发）
    chunks = chunk_paragraphs(
        texts,
        max_chars=settings.translation_chunk_max_chars,
        min_tail_chars=settings.translation_chunk_min_chars,
    )
    for indices in chunks:
        _run_indices(indices)
    return results


# ---------------------- TXT ----------------------


def translate_txt(data: bytes, translator: Translator, ctx: TranslationContext) -> bytes:
    text = data.decode("utf-8", errors="replace")
    # 按段落（双换行）切分，保留原段落结构
    paragraphs = text.split("\n\n")
    translated = _translate_many(paragraphs, translator, ctx)
    return "\n\n".join(translated).encode("utf-8")


# ---------------------- Markdown ----------------------


def translate_md(data: bytes, translator: Translator, ctx: TranslationContext) -> bytes:
    """简化策略：按段（空行分隔）翻译；代码块整体跳过。

    实现：先把内容拆为顺序片段列表，每个片段标记是否需翻译，
    再把需翻译的部分送入并发翻译，最后按原顺序拼回。
    """
    text = data.decode("utf-8", errors="replace")
    lines = text.split("\n")

    # 片段：(needs_translate, content)
    segments: list[tuple[bool, str]] = []
    buffer: list[str] = []
    in_code_block = False

    def flush_buffer() -> None:
        if buffer:
            segments.append((True, "\n".join(buffer)))
            buffer.clear()

    for line in lines:
        stripped = line.lstrip()
        if stripped.startswith("```"):
            flush_buffer()
            segments.append((False, line))
            in_code_block = not in_code_block
            continue
        if in_code_block:
            segments.append((False, line))
            continue
        if line.strip() == "":
            flush_buffer()
            segments.append((False, line))
        else:
            buffer.append(line)
    flush_buffer()

    # 抽取需翻译的部分并发处理
    translatable_indices = [i for i, (need, _) in enumerate(segments) if need]
    translatable_texts = [segments[i][1] for i in translatable_indices]
    translated = _translate_many(translatable_texts, translator, ctx)

    out_lines: list[str] = []
    j = 0
    for need, content in segments:
        if need:
            out_lines.append(translated[j])
            j += 1
        else:
            out_lines.append(content)
    return "\n".join(out_lines).encode("utf-8")


# ---------------------- DOCX ----------------------


def _collect_docx_paragraphs(doc: Document) -> list:
    """收集所有需要翻译的段落对象（含表格内）。"""
    items: list = []
    for paragraph in doc.paragraphs:
        if paragraph.text.strip():
            items.append(paragraph)
    for table in doc.tables:
        for row in table.rows:
            for cell in row.cells:
                for paragraph in cell.paragraphs:
                    if paragraph.text.strip():
                        items.append(paragraph)
    return items


def _collect_docx_header_footer_paragraphs(doc: Document) -> list:
    """收集页眉/页脚（含首页、奇偶页变体，及其中表格）的可翻译段落。

    doc.paragraphs 不含页眉页脚，需逐 section 单独取。
    链接到上一节的页眉/页脚（is_linked_to_previous）没有自己的内容（继承自上一节），
    跳过以避免对同一底层段落重复翻译。
    """
    items: list = []
    for section in doc.sections:
        for hf in (
            section.header, section.footer,
            section.first_page_header, section.first_page_footer,
            section.even_page_header, section.even_page_footer,
        ):
            if hf is None:
                continue
            try:
                if hf.is_linked_to_previous:
                    continue
            except Exception:  # noqa: BLE001
                pass
            for paragraph in hf.paragraphs:
                if paragraph.text.strip():
                    items.append(paragraph)
            for table in hf.tables:
                for row in table.rows:
                    for cell in row.cells:
                        for paragraph in cell.paragraphs:
                            if paragraph.text.strip():
                                items.append(paragraph)
    return items


def _get_docx_footnote_paragraphs(doc: Document) -> tuple[list, dict]:
    """提取脚注（footnotes）和尾注（endnotes）中的段落。

    python-docx 不支持脚注/尾注，需要直接解析 XML。
    返回 (paragraphs, roots_map) 元组：
    - paragraphs: p_elem 列表（lxml w:p 节点）
    - roots_map: {fn_part: root_element} 记录哪些 part 被解析过，
      最后序列化写回时需要用到。
    """
    from lxml import etree

    NS_W = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"

    items: list = []
    roots_map: dict = {}
    part = doc.part
    for rel in part.rels.values():
        if not (rel.reltype.endswith("/footnotes") or rel.reltype.endswith("/endnotes")):
            continue
        fn_part = rel.target_part
        root = etree.fromstring(fn_part.blob)
        roots_map[fn_part] = root
        for p_elem in root.findall(f".//{{{NS_W}}}p"):
            texts = p_elem.findall(f".//{{{NS_W}}}t")
            full_text = "".join(t.text or "" for t in texts)
            if full_text.strip():
                items.append(p_elem)
    return items, roots_map


def _extract_paragraph_text_from_xml(p_elem) -> str:
    """从 lxml w:p 元素中提取纯文本。"""
    NS_W = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"
    texts = p_elem.findall(f".//{{{NS_W}}}t")
    return "".join(t.text or "" for t in texts)


def _write_text_to_xml_paragraph(p_elem, new_text: str) -> None:
    """把翻译后的文本写回 lxml w:p 元素。

    策略：找到所有 w:t 子节点，把文本写入第一个，其余清空。
    """
    NS_W = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"
    t_nodes = p_elem.findall(f".//{{{NS_W}}}t")
    if not t_nodes:
        return
    t_nodes[0].text = new_text
    for t in t_nodes[1:]:
        t.text = ""


def _run_has_drawing(run) -> bool:
    """判断 run 内是否含图片 / 嵌入对象 / 公式等非文本元素。

    python-docx 的 `run.text = value` 会清空 run 的所有子元素（包括 w:drawing），
    因此对含图片的 run 必须跳过，避免图片丢失。
    """
    el = run._element
    # 命名空间前缀已在 docx 内置注册，xpath 直接用即可
    return bool(
        el.xpath(".//w:drawing")
        or el.xpath(".//w:pict")
        or el.xpath(".//w:object")
    )


def _run_is_preservable(run) -> bool:
    """判断 run 是否必须原样保留（绝不可被 run.text= 覆盖或清空）。

    `run.text` 的 setter 会先调用 clear_content() 删除 run 内**所有**子元素，
    因此凡含以下特殊元素的 run 都必须跳过，否则内容会丢失：
    - 图片 / 嵌入对象 / 公式：w:drawing / w:pict / w:object
    - 脚注 / 尾注引用标记：w:footnoteReference / w:endnoteReference（正文里指向脚注的小标记）
      及 w:footnoteRef / w:endnoteRef（脚注文本内的回指标记）——
      一旦丢失，脚注虽仍在 footnotes.xml 中但正文无引用，Word 不再显示，
      表现为"译文只有一部分"（脚注占比大的法律文书尤其明显）。
    - 字段：w:fldChar / w:instrText（页码、目录、交叉引用等），清空会破坏字段。
    """
    el = run._element
    return bool(el.xpath(
        ".//w:drawing | .//w:pict | .//w:object | "
        ".//w:footnoteReference | .//w:endnoteReference | "
        ".//w:footnoteRef | .//w:endnoteRef | "
        ".//w:fldChar | .//w:instrText"
    ))


def _write_translated_to_paragraph(paragraph, new_text: str) -> None:
    """把翻译后的文本写回段落，保留其中的图片/嵌入对象。

    策略：
    - 只把整段译文写入第一个"普通文本 run"，保留其字体格式
    - 其他"普通文本 run"清空文本（不会破坏其格式）
    - 不可覆盖的 run（图片/脚注引用/字段等）完全不动，避免内容丢失
    - 段落里完全没有普通文本 run（只有图片/脚注引用）时不做任何修改
    """
    runs = list(paragraph.runs)
    text_runs = [r for r in runs if not _run_is_preservable(r)]
    if not text_runs:
        return  # 无可写入的普通文本 run（只有图片/脚注引用等），保持原样
    text_runs[0].text = new_text
    for r in text_runs[1:]:
        r.text = ""


def _translate_docx_inplace(doc: Document, translator: Translator, ctx: TranslationContext) -> None:
    """在 docx 文档对象上原地替换文本（段落级并发）。

    注意：_translate_many 返回的字符串已经按 ctx.output_mode 处理过对照逻辑，
    这里直接写回即可，不要再次调用 _bilingual_join。
    """
    # 正文段落（含表格内）+ 页眉/页脚段落（两者都是普通 python-docx 段落，
    # 走 .text + _write_translated_to_paragraph 同一套写回逻辑）
    paragraphs = _collect_docx_paragraphs(doc) + _collect_docx_header_footer_paragraphs(doc)
    # 脚注 + 尾注段落
    fn_paragraphs, fn_roots_map = _get_docx_footnote_paragraphs(doc)
    all_paragraphs = paragraphs + fn_paragraphs

    if not all_paragraphs:
        return
    # 正文段落用 .text，脚注段落用 XML 提取
    texts = []
    for i, p in enumerate(all_paragraphs):
        if i < len(paragraphs):
            texts.append(p.text)
        else:
            texts.append(_extract_paragraph_text_from_xml(p))
    translated = _translate_many(texts, translator, ctx)
    for i, (para, original, new_text) in enumerate(zip(all_paragraphs, texts, translated)):
        if not original.strip():
            continue
        if not new_text or not new_text.strip():
            # 空译文不写回（保留原文），防止段落内容被抹掉
            continue
        if i < len(paragraphs):
            _write_translated_to_paragraph(para, new_text)
        else:
            _write_text_to_xml_paragraph(para, new_text)

    # 保存脚注/尾注 roots_map 到 doc 对象上供 translate_docx 序列化
    if not hasattr(doc, '_fn_roots_map'):
        doc._fn_roots_map = {}
    doc._fn_roots_map.update(fn_roots_map)


def _translate_paragraph(paragraph, translator: Translator, ctx: TranslationContext) -> None:
    """保留以单段函数（用于将来调用方便），目前主流程已走并发版本。"""
    text = paragraph.text
    if not text.strip():
        return
    translated = translator.translate(text, ctx.target_lang, ctx.source_lang)
    if ctx.output_mode == OutputMode.BILINGUAL:
        new_text = _bilingual_join(text, translated)
    else:
        new_text = translated
    _write_translated_to_paragraph(paragraph, new_text)


def _replace_docx_images(doc: Document, ctx: TranslationContext) -> None:
    """P1.3：对 docx 中的内嵌图片执行 OCR + 就地替换文字。

    遍历文档中所有图片，调用 process_image_ocr 擦除原文并写入译文，
    然后将修改后的图片替换回文档。

    如果 ctx.translate_images == "no"，跳过图片 OCR，保持原样。
    """
    if ctx.translate_images == "no":
        logger.info("用户选择不翻译图片文字，跳过 DOCX 图片 OCR")
        return

    from app.services.ocr import process_image_ocr

    # 收集所有需要处理的图片关系 ID 和对应的图片数据
    ns = {
        "a": "http://schemas.openxmlformats.org/drawingml/2006/main",
        "r": "http://schemas.openxmlformats.org/officeDocument/2006/relationships",
        "wp": "http://schemas.openxmlformats.org/drawingml/2006/wordprocessingDrawing",
    }

    replaced_count = 0
    processed_rIds: set[str] = set()
    for paragraph in doc.paragraphs:
        for run in paragraph.runs:
            if not _run_has_drawing(run):
                continue
            el = run._element
            for blip in el.findall(".//a:blip", ns):
                rId = blip.get("{http://schemas.openxmlformats.org/officeDocument/2006/relationships}embed")
                if not rId or rId in processed_rIds:
                    continue
                processed_rIds.add(rId)
                try:
                    image_part = doc.part.related_parts[rId]
                    image_bytes = image_part.blob
                    mime = getattr(image_part, "content_type", "image/png") or "image/png"

                    logger.info("DOCX 图片 OCR 处理: rId=%s, size=%d bytes, mime=%s",
                                rId, len(image_bytes), mime)

                    # 就地替换图片中的文字（透传术语库 / TM / 源语种）
                    new_image_bytes = process_image_ocr(
                        image_bytes, ctx.target_lang, mime,
                        source_lang=ctx.source_lang,
                        glossary=ctx.glossary,
                        tm_lookup=ctx.tm_lookup,
                    )
                    if new_image_bytes == image_bytes:
                        logger.info("DOCX 图片 OCR 无变化，跳过替换")
                        continue

                    # 替换图片数据
                    image_part._blob = new_image_bytes
                    replaced_count += 1
                    logger.info("DOCX 图片 OCR 替换成功: rId=%s", rId)
                except (KeyError, AttributeError) as exc:
                    logger.warning("替换 docx 图片失败：%s", exc)

    if replaced_count > 0:
        logger.info("DOCX 共替换 %d 张图片", replaced_count)


def translate_docx(data: bytes, translator: Translator, ctx: TranslationContext) -> bytes:
    doc = Document(io.BytesIO(data))
    _translate_docx_inplace(doc, translator, ctx)

    # P1.5：阿拉伯语 RTL 排版
    is_rtl = ctx.target_lang.lower() in ("ar", "ara", "arabic")
    if is_rtl:
        _apply_docx_rtl(doc)

    # P1.3：图片 OCR + 就地替换文字（擦除原文，写入译文）
    _replace_docx_images(doc, ctx)

    # 脚注/尾注修改写回 part blob（python-docx 不会自动序列化脚注 XML）
    _flush_footnote_parts(doc)

    out = io.BytesIO()
    doc.save(out)
    return out.getvalue()


def _flush_footnote_parts(doc: Document) -> None:
    """把修改后的脚注/尾注 XML 树序列化回对应的 part blob。"""
    from lxml import etree

    roots_map = getattr(doc, '_fn_roots_map', None)
    if not roots_map:
        return
    for fn_part, root in roots_map.items():
        fn_part._blob = etree.tostring(
            root, xml_declaration=True, encoding="UTF-8", standalone=True
        )


def _apply_docx_rtl(doc: Document) -> None:
    """P1.5：为 DOCX 文档设置 RTL（从右到左）排版。

    遍历所有段落和表格单元格，设置双向文本属性。
    """
    from docx.oxml.ns import qn
    from docx.enum.text import WD_ALIGN_PARAGRAPH

    for paragraph in doc.paragraphs:
        _set_paragraph_rtl(paragraph)
    for table in doc.tables:
        for row in table.rows:
            for cell in row.cells:
                for paragraph in cell.paragraphs:
                    _set_paragraph_rtl(paragraph)


def _set_paragraph_rtl(paragraph) -> None:
    """设置单个段落的 RTL 属性。"""
    from docx.oxml.ns import qn

    pPr = paragraph._element.get_or_add_pPr()
    # 设置双向文本：bidi=1 表示 RTL
    bidi = pPr.find(qn("w:bidi"))
    if bidi is None:
        bidi = pPr.makeelement(qn("w:bidi"), {})
        pPr.append(bidi)
    bidi.set(qn("w:val"), "1")
    # 设置右对齐
    paragraph.alignment = 2  # WD_ALIGN_PARAGRAPH.RIGHT

    # 为每个 run 设置 RTL
    for run in paragraph.runs:
        rPr = run._element.get_or_add_rPr()
        rtl = rPr.find(qn("w:rtl"))
        if rtl is None:
            rtl = rPr.makeelement(qn("w:rtl"), {})
            rPr.append(rtl)


# ---------------------- PDF ----------------------


def translate_pdf_to_docx(data: bytes, translator: Translator, ctx: TranslationContext) -> bytes:
    """PDF（电子版）→ docx → 翻译 → docx 输出。

    适用于希望在 Word 中二次编辑的场景。版面会有损耗（pdf2docx 的固有问题）。
    """
    with tempfile.TemporaryDirectory() as tmp:
        pdf_path = os.path.join(tmp, "input.pdf")
        docx_path = os.path.join(tmp, "input.docx")
        with open(pdf_path, "wb") as f:
            f.write(data)

        cv = Converter(pdf_path)
        cv.convert(docx_path, start=0, end=None)
        cv.close()

        with open(docx_path, "rb") as f:
            docx_bytes = f.read()

    return translate_docx(docx_bytes, translator, ctx)


def _should_skip_pdf_text(text: str) -> bool:
    """判断 PDF 中的文本是否不需要翻译（纯数字、公式、日期等）。"""
    import re as _re
    text = text.strip()
    if not text:
        return True
    # 纯数字（含小数、百分号、货币符号）
    if _re.match(r'^[\d\s,.%$€¥£₹+\-]+$', text):
        return True
    # 日期格式
    if _re.match(r'^\d{4}[-/]\d{1,2}[-/]\d{1,2}$', text):
        return True
    # 时间格式
    if _re.match(r'^\d{1,2}:\d{2}(:\d{2})?$', text):
        return True
    # 邮箱
    if _re.match(r'^[\w.+-]+@[\w-]+\.[\w.]+$', text):
        return True
    # URL
    if _re.match(r'^https?://', text):
        return True
    # 版本号
    if _re.match(r'^v?\d+\.\d+', text, _re.IGNORECASE):
        return True
    # 单个标点/符号
    if len(text) <= 2 and not _re.search(r'[\u4e00-\u9fff\u3040-\u309f\u30a0-\u30ff\uac00-\ud7af]', text):
        return True
    return False


def _is_scanned_pdf(doc) -> bool:
    """判断 PDF 是否为扫描件（无文字层或极少文字）。

    标准：所有页面的文字块总字符数 < 每页 20 字符 → 视为扫描件。
    """
    total_chars = 0
    total_pages = len(doc)
    for page in doc:
        for b in page.get_text("blocks"):
            if b[6] == 0:  # 文字块
                total_chars += len((b[4] or "").strip())
    return total_pages > 0 and total_chars < total_pages * 20


def _ocr_pdf_scanned_pages(doc, ctx: TranslationContext) -> None:
    """P1.4：PDF 扫描件整页 OCR + 翻译。

    对每页渲染为图片，送 OCR 引擎识别 + 翻译（PaddleOCR 优先，VL 降级），
    在页面顶部叠加译文文本框。

    如果 ctx.translate_images == "no"，跳过扫描件 OCR。
    """
    if ctx.translate_images == "no":
        logger.info("用户选择不翻译图片文字，跳过 PDF 扫描件 OCR")
        return

    import fitz  # PyMuPDF
    from app.services.ocr import ocr_and_translate

    for page_idx, page in enumerate(doc):
        # 渲染页面为图片（DPI 150 平衡质量与速度）
        pix = page.get_pixmap(dpi=150)
        image_bytes = pix.tobytes("png")

        translated = ocr_and_translate(
            image_bytes, ctx.target_lang, "image/png",
            source_lang=ctx.source_lang,
            glossary=ctx.glossary,
            tm_lookup=ctx.tm_lookup,
        )
        if not translated:
            continue

        # 在页面顶部叠加译文文本框
        page_rect = page.rect
        text_rect = fitz.Rect(
            page_rect.x0 + 10,
            page_rect.y0 + 10,
            page_rect.x1 - 10,
            page_rect.y1 - 10,
        )
        # 使用 CJK 字体
        try:
            page.insert_textbox(
                text_rect,
                translated,
                fontname="china-s",
                fontsize=11,
                align=0,
            )
        except Exception:
            page.insert_textbox(text_rect, translated, fontsize=11, align=0)

        if ctx.on_progress:
            ctx.on_progress(page_idx + 1, len(doc))


def _replace_pdf_images(doc, ctx: TranslationContext) -> None:
    """P1.3：PDF 内嵌图片 OCR + 就地替换文字。

    遍历每页中的图片，调用 process_image_ocr 擦除原文并写入译文，
    然后将修改后的图片替换回 PDF。

    如果 ctx.translate_images == "no"，跳过图片 OCR，保持原样。
    """
    if ctx.translate_images == "no":
        logger.info("用户选择不翻译图片文字，跳过 PDF 图片 OCR")
        return

    import fitz  # PyMuPDF
    from app.services.ocr import process_image_ocr

    replaced_count = 0
    for page in doc:
        images = page.get_images(full=True)
        for img_info in images:
            xref = img_info[0]
            try:
                base_image = doc.extract_image(xref)
                image_bytes = base_image["image"]
                ext = base_image.get("ext", "png")
                mime_map = {"png": "image/png", "jpeg": "image/jpeg", "jpg": "image/jpeg"}
                mime = mime_map.get(ext, f"image/{ext}")

                logger.info("PDF 图片 OCR 处理: xref=%s, size=%d bytes, ext=%s",
                            xref, len(image_bytes), ext)

                # 跳过过小的图片（图标、装饰元素等，不是真正的扫描件内容）
                if len(image_bytes) < 5000:
                    logger.info("PDF 图片过小 (xref=%s, %d bytes)，跳过 OCR", xref, len(image_bytes))
                    continue

                # 就地替换图片中的文字（透传术语库 / TM / 源语种）
                new_image_bytes = process_image_ocr(
                    image_bytes, ctx.target_lang, mime,
                    source_lang=ctx.source_lang,
                    glossary=ctx.glossary,
                    tm_lookup=ctx.tm_lookup,
                )
                if new_image_bytes == image_bytes:
                    logger.info("PDF 图片 OCR 无变化，跳过替换")
                    continue

                # 替换 PDF 中的图片
                try:
                    # 获取原始图片的 Pixmap 信息
                    orig_pix = fitz.Pixmap(doc, xref)
                    orig_n = orig_pix.n  # 颜色通道数
                    orig_alpha = orig_pix.alpha
                    orig_pix = None  # 释放

                    # 将新图片字节转为 Pixmap
                    img_doc = fitz.open(f"image/{ext}", new_image_bytes)
                    new_pix = fitz.Pixmap(img_doc[0])
                    img_doc.close()

                    # 去掉 alpha 通道（PDF 内嵌图片通常不带 alpha）
                    if new_pix.alpha:
                        new_pix = fitz.Pixmap(new_pix, 0)

                    # 颜色空间匹配：确保新 Pixmap 与原始图片通道数一致
                    if orig_n == 1 and new_pix.n >= 3:
                        # 原图灰度，新图 RGB -> 转灰度
                        new_pix = fitz.Pixmap(fitz.csGRAY, new_pix)
                    elif orig_n == 4 and new_pix.n == 3:
                        # 原图 CMYK，新图 RGB -> 转 CMYK
                        new_pix = fitz.Pixmap(fitz.csCMYK, new_pix)

                    doc.update_image(xref, pixmap=new_pix)
                    replaced_count += 1
                    logger.info("PDF 图片 OCR 替换成功: xref=%s", xref)
                except Exception as exc:
                    logger.warning("PDF 图片替换失败 (xref=%s): %s", xref, exc)
            except Exception as exc:
                logger.warning("PDF 图片处理失败 (xref=%s): %s", xref, exc)

    if replaced_count > 0:
        logger.info("PDF 共替换 %d 张图片", replaced_count)


def _sample_pdf_bg_color(page, rect) -> tuple[float, float, float]:
    """采样 PDF 页面中指定区域周围的背景色，返回归一化 RGB (0~1)。

    用于 redact 擦除时填充背景色，避免有色底/表格底纹页面留下白块。
    采样策略：取 bbox 四周一圈像素的均值作为背景色。
    失败时回退到白色 (1, 1, 1)。
    """
    import fitz  # noqa: F811

    try:
        # 向外扩展 2px 采样背景，但不超过页面边界
        page_rect = page.rect
        clip = fitz.Rect(
            max(page_rect.x0, rect.x0 - 2),
            max(page_rect.y0, rect.y0 - 2),
            min(page_rect.x1, rect.x1 + 2),
            min(page_rect.y1, rect.y1 + 2),
        )
        if clip.width <= 0 or clip.height <= 0:
            return (1, 1, 1)

        pix = page.get_pixmap(clip=clip)
        samples = pix.samples
        n = pix.n  # 通道数（RGB=3, RGBA=4, 灰度=1）

        # 取四角各 1 个像素的均值作为背景色
        w = pix.width
        h = pix.height
        corner_indices = [0, (w - 1) * n, (h - 1) * w * n, ((h - 1) * w + (w - 1)) * n]
        r_sum = g_sum = b_sum = 0
        count = 0
        for idx in corner_indices:
            if idx + n - 1 >= len(samples):
                continue
            if n >= 3:
                r_sum += samples[idx]
                g_sum += samples[idx + 1]
                b_sum += samples[idx + 2]
            else:
                # 灰度图：RGB 三通道相同
                r_sum += samples[idx]
                g_sum += samples[idx]
                b_sum += samples[idx]
            count += 1

        if count == 0:
            return (1, 1, 1)

        return (r_sum / (count * 255), g_sum / (count * 255), b_sum / (count * 255))
    except Exception:  # noqa: BLE001
        return (1, 1, 1)


def translate_pdf_inplace(data: bytes, translator: Translator, ctx: TranslationContext) -> bytes:
    """PDF 就地替换文字（PyMuPDF），最大限度保留版面/页眉页脚/图片。

    思路：
    使用 page.get_text("dict") 获取每个文字 span 的精确位置和字号，
    逐 span 擦除原文并写入译文，最大限度保留表格和版面结构。

    P1.3 增强：内嵌图片 OCR + 翻译
    P1.4 增强：扫描件整页 OCR + 翻译
    P1.5 增强：阿拉伯语 RTL 排版 + 长译文回流
    """
    import fitz  # PyMuPDF

    doc = fitz.open(stream=data, filetype="pdf")

    # P1.4：检测扫描件 PDF，走整页 OCR 路径
    if _is_scanned_pdf(doc):
        _ocr_pdf_scanned_pages(doc, ctx)
        out = io.BytesIO()
        doc.save(out, deflate=True, clean=True)
        doc.close()
        return out.getvalue()

    # 1. 收集所有可翻译的 span（精确到每个文字片段）
    #    每个 span 包含：页面索引、bbox、原文、字号、颜色
    spans_info: list[dict] = []  # [{page_idx, rect, text, size, color}, ...]
    texts: list[str] = []

    for page_idx, page in enumerate(doc):
        text_dict = page.get_text("dict", flags=fitz.TEXT_PRESERVE_WHITESPACE)
        for block in text_dict.get("blocks", []):
            if block.get("type") != 0:  # 非文字块跳过
                continue
            for line in block.get("lines", []):
                for span in line.get("spans", []):
                    text = (span.get("text") or "").strip()
                    if not text:
                        continue
                    # 跳过纯数字/符号等无需翻译的内容
                    if _should_skip_pdf_text(text):
                        continue
                    bbox = span.get("bbox")
                    if not bbox or len(bbox) != 4:
                        continue
                    rect = fitz.Rect(bbox)
                    if rect.width < 2 or rect.height < 2:
                        continue
                    size = span.get("size", 10)
                    color = span.get("color", 0)
                    # color 是整数，转为 RGB
                    r = (color >> 16) & 0xFF
                    g = (color >> 8) & 0xFF
                    b = color & 0xFF

                    spans_info.append({
                        "page_idx": page_idx,
                        "rect": rect,
                        "text": text,
                        "size": size,
                        "color": (r / 255.0, g / 255.0, b / 255.0),
                    })
                    texts.append(text)

    if not texts:
        return data

    # 2. 批量翻译
    translated = _translate_many(texts, translator, ctx)

    # 2.5 为每个 span 采样背景色（避免有色底/表格底纹页面留白块）
    # 采样策略：取 span bbox 稍微向外扩展一圈，取该区域的四角像素均值作为背景色
    for span_info in spans_info:
        span_info["bg_color"] = _sample_pdf_bg_color(doc[span_info["page_idx"]], span_info["rect"])

    # 3. 逐 span 擦除原文并写入译文
    for span_info, new_text in zip(spans_info, translated):
        if not new_text or new_text == span_info["text"]:
            continue

        page = doc[span_info["page_idx"]]
        rect = span_info["rect"]

        # 擦除原文（用采样到的背景色填充，避免有色底页面留白块）
        page.add_redact_annot(rect, fill=span_info["bg_color"])

    # 一次性提交所有擦除
    for page in doc:
        page.apply_redactions()

    # 4. 写回译文
    for span_info, new_text in zip(spans_info, translated):
        if not new_text or new_text == span_info["text"]:
            continue

        page = doc[span_info["page_idx"]]
        rect = span_info["rect"]
        original_size = span_info["size"]
        color = span_info["color"]

        # P1.5：阿拉伯语 RTL 排版
        is_rtl = ctx.target_lang.lower() in ("ar", "ara", "arabic")
        align = 2 if is_rtl else 0

        # 使用原文的字号作为基准，如果译文放不下则缩小
        fontname = "china-s"
        inserted = False
        for size_reduction in [0, 0.15, 0.3, 0.45, 0.6]:
            current_size = original_size * (1 - size_reduction)
            if current_size < 4:
                break
            try:
                rc = page.insert_textbox(
                    rect,
                    new_text,
                    fontname=fontname,
                    fontsize=current_size,
                    align=align,
                    color=color,
                )
                if rc >= 0:
                    inserted = True
                    break
            except Exception:
                break

        if not inserted:
            try:
                page.insert_textbox(
                    rect, new_text, fontsize=original_size * 0.5,
                    align=align, color=color,
                )
            except Exception:
                pass

    # P1.3：内嵌图片 OCR + 就地替换文字
    _replace_pdf_images(doc, ctx)

    out = io.BytesIO()
    doc.save(out, deflate=True, clean=True)
    doc.close()
    return out.getvalue()


def _libreoffice_convert(data: bytes, source_ext: str, target_ext: str) -> bytes:
    """使用 LibreOffice headless 将老格式文件转换为新格式。

    支持：.doc → .docx, .ppt → .pptx
    要求：容器内已安装 libreoffice（通过 Dockerfile 安装）。
    """
    with tempfile.TemporaryDirectory() as tmp:
        input_path = os.path.join(tmp, f"input.{source_ext}")
        with open(input_path, "wb") as f:
            f.write(data)

        import subprocess
        result = subprocess.run(
            [
                "libreoffice",
                "--headless",
                "--convert-to", target_ext,
                "--outdir", tmp,
                input_path,
            ],
            capture_output=True,
            text=True,
            timeout=120,
        )
        if result.returncode != 0:
            raise RuntimeError(
                f"LibreOffice 转换 .{source_ext} → .{target_ext} 失败：{result.stderr[:500]}"
            )

        output_path = os.path.join(tmp, f"input.{target_ext}")
        if not os.path.exists(output_path):
            # LibreOffice 有时输出文件名不同，尝试查找
            for fname in os.listdir(tmp):
                if fname.endswith(f".{target_ext}"):
                    output_path = os.path.join(tmp, fname)
                    break
            else:
                raise FileNotFoundError(f"LibreOffice 转换后未找到 .{target_ext} 文件")

        with open(output_path, "rb") as f:
            return f.read()


def translate_doc(data: bytes, translator: Translator, ctx: TranslationContext) -> bytes:
    """老版 Word (.doc) 翻译。

    实现路径：LibreOffice headless 转换 .doc → .docx → 走 docx 翻译路径。
    输出格式：.docx（与 .xls → .xlsx 一致，老格式特性可能丢失）。
    """
    docx_data = _libreoffice_convert(data, "doc", "docx")
    return translate_docx(docx_data, translator, ctx)


def translate_ppt(data: bytes, translator: Translator, ctx: TranslationContext) -> bytes:
    """老版 PowerPoint (.ppt) 翻译。

    实现路径：LibreOffice headless 转换 .ppt → .pptx → 走 pptx 翻译路径。
    输出格式：.pptx（老格式特性如宏/特殊动画可能丢失）。
    """
    pptx_data = _libreoffice_convert(data, "ppt", "pptx")
    return translate_pptx(pptx_data, translator, ctx)


# ---------------------- 派发器 ----------------------


def translate_csv(data: bytes, translator: Translator, ctx: TranslationContext) -> bytes:
    """CSV 翻译。

    - 自动探测编码（UTF-8 / GBK），失败回退 latin-1（不丢字节）
    - 自动探测分隔符（, ; \\t |）
    - 仅翻译非空、非数字的字段
    - 输出始终用 UTF-8（带 BOM，方便 Excel 直接打开不乱码）
    """
    import csv
    import io as _io

    # 编码探测
    text: str | None = None
    used_encoding = "utf-8-sig"
    for enc in ("utf-8-sig", "utf-8", "gb18030", "gbk"):
        try:
            text = data.decode(enc)
            used_encoding = enc
            break
        except UnicodeDecodeError:
            continue
    if text is None:
        text = data.decode("latin-1")
        used_encoding = "latin-1"

    # 分隔符探测
    sample = text[:4096]
    try:
        dialect = csv.Sniffer().sniff(sample, delimiters=",;\t|")
    except csv.Error:
        # 兜底：按逗号
        class _Default(csv.excel):
            pass
        dialect = _Default

    rows = list(csv.reader(_io.StringIO(text), dialect=dialect))
    if not rows:
        return data

    # 收集需翻译的单元格（保持位置）
    locations: list[tuple[int, int]] = []
    texts: list[str] = []

    def _is_number(s: str) -> bool:
        s = s.strip()
        if not s:
            return False
        try:
            float(s.replace(",", ""))
            return True
        except ValueError:
            return False

    for r_idx, row in enumerate(rows):
        for c_idx, val in enumerate(row):
            v = (val or "").strip()
            if not v or _is_number(v):
                continue
            locations.append((r_idx, c_idx))
            texts.append(val)

    if texts:
        translated = _translate_many(texts, translator, ctx)
        for (r_idx, c_idx), new_text in zip(locations, translated):
            if new_text:
                rows[r_idx][c_idx] = new_text

    # 写回（始终用 UTF-8 BOM，避免 Excel 打开中文乱码）
    out = _io.StringIO()
    writer = csv.writer(out, dialect=dialect)
    writer.writerows(rows)
    result = out.getvalue()
    return ("\ufeff" + result if not result.startswith("\ufeff") else result).encode("utf-8")


def translate_xls(data: bytes, translator: Translator, ctx: TranslationContext) -> bytes:
    """老版 Excel (.xls) 翻译。

    实现路径：xlrd 读取 .xls → 数据/公式转入 openpyxl Workbook → 走 xlsx 翻译路径。
    输出格式：.xlsx（业界通行；老 .xls 缺乏现代格式特性，无意义保留）

    限制：
    - .xls 不保留：图表、数据透视、宏（与 .xls 转 .xlsx 通用限制一致）
    - 单元格格式（字体/边框/对齐）做尽力保留；样式可能与原版略有差异
    """
    import xlrd
    from openpyxl import Workbook

    book = xlrd.open_workbook(file_contents=data, formatting_info=False)
    wb = Workbook()
    # 删除 openpyxl 默认创建的空白 sheet
    default_ws = wb.active
    wb.remove(default_ws)

    for sheet in book.sheets():
        new_ws = wb.create_sheet(title=(sheet.name or "Sheet")[:31])
        for r in range(sheet.nrows):
            for c in range(sheet.ncols):
                cell = sheet.cell(r, c)
                value = cell.value
                # xlrd ctype: 0=empty 1=text 2=number 3=date 4=bool 5=error 6=blank
                if cell.ctype == xlrd.XL_CELL_EMPTY or cell.ctype == xlrd.XL_CELL_BLANK:
                    continue
                if cell.ctype == xlrd.XL_CELL_DATE:
                    try:
                        from datetime import datetime
                        value = xlrd.xldate.xldate_as_datetime(value, book.datemode)
                    except Exception:  # noqa: BLE001
                        pass
                elif cell.ctype == xlrd.XL_CELL_BOOLEAN:
                    value = bool(value)
                elif cell.ctype == xlrd.XL_CELL_NUMBER:
                    # 整数显示为 int，避免 1.0 -> 1
                    if value == int(value):
                        value = int(value)
                new_ws.cell(row=r + 1, column=c + 1, value=value)

        # 合并单元格还原
        for crange in sheet.merged_cells:
            r1, r2, c1, c2 = crange  # xlrd: [r1, r2) [c1, c2) 半开区间
            new_ws.merge_cells(start_row=r1 + 1, start_column=c1 + 1,
                               end_row=r2, end_column=c2)

    # 转成 xlsx 字节，复用 xlsx 翻译路径
    intermediate = io.BytesIO()
    wb.save(intermediate)
    return translate_xlsx(intermediate.getvalue(), translator, ctx)


def _replace_pptx_images(prs, ctx: TranslationContext) -> None:
    """P1.3：PPT 内嵌图片 OCR + 就地替换文字。

    遍历幻灯片中的图片 shape，调用 process_image_ocr 擦除原文并写入译文，
    然后将修改后的图片替换回幻灯片。

    如果 ctx.translate_images == "no"，跳过图片 OCR，保持原样。
    """
    if ctx.translate_images == "no":
        logger.info("用户选择不翻译图片文字，跳过 PPT 图片 OCR")
        return

    from app.services.ocr import process_image_ocr

    replaced_count = 0
    for slide in prs.slides:
        for shape in slide.shapes:
            if shape.shape_type != 13:  # MSO_SHAPE_TYPE.PICTURE = 13
                continue
            try:
                image = shape.image
                image_bytes = image.blob
                mime = image.content_type or "image/png"

                logger.info("PPT 图片 OCR 处理: shape=%s, size=%d bytes, mime=%s",
                            shape.shape_id, len(image_bytes), mime)

                # 就地替换图片中的文字（透传术语库 / TM / 源语种）
                new_image_bytes = process_image_ocr(
                    image_bytes, ctx.target_lang, mime,
                    source_lang=ctx.source_lang,
                    glossary=ctx.glossary,
                    tm_lookup=ctx.tm_lookup,
                )

                if new_image_bytes == image_bytes:
                    logger.info("PPT 图片 OCR 无变化，跳过替换")
                    continue

                # 替换图片数据：通过 slide part 的关系找到 ImagePart
                slide_part = shape.part
                # 从 shape 的 XML 中获取 blip rId
                sp = shape._element
                ns = {
                    "a": "http://schemas.openxmlformats.org/drawingml/2006/main",
                    "r": "http://schemas.openxmlformats.org/officeDocument/2006/relationships",
                }
                blip = sp.find(".//a:blip", ns)
                if blip is None:
                    logger.warning("PPT 图片未找到 blip 引用: shape=%s", shape.shape_id)
                    continue
                rId = blip.get("{http://schemas.openxmlformats.org/officeDocument/2006/relationships}embed")
                if not rId:
                    logger.warning("PPT 图片 blip 无 rId: shape=%s", shape.shape_id)
                    continue

                # 获取 ImagePart 并替换 blob
                image_part = slide_part.related_part(rId)
                image_part._blob = new_image_bytes
                replaced_count += 1
                logger.info("PPT 图片 OCR 替换成功: shape=%s, rId=%s", shape.shape_id, rId)
            except Exception as exc:
                logger.warning("PPT 图片替换失败: %s", exc)

    if replaced_count > 0:
        logger.info("PPT 共替换 %d 张图片", replaced_count)


def translate_pptx(data: bytes, translator: Translator, ctx: TranslationContext) -> bytes:
    """PPT (.pptx) 翻译。

    覆盖范围（需求 2.2）：
    - 幻灯片所有 shape 文本（标题/正文/独立文本框/形状文字）
    - 分组 shape 递归
    - 表格内每个 cell 的文本
    - 演讲者备注（notes）
    - 母版 / 版式中的文本（如公司名、版权信息）
    - SmartArt 中的文本（操作底层 XML 找 a:t）
    - P1.3：图片 OCR + 翻译，叠加文本框

    保留：
    - 字体 / 字号 / 颜色 / 对齐 / 形状大小 / 图片 / 动画
    - 富文本一段多 run 时合并为第一 run（保留首字体），与 docx 一致
    """
    from pptx import Presentation
    from pptx.enum.shapes import MSO_SHAPE_TYPE

    prs = Presentation(io.BytesIO(data))

    # 收集所有 paragraph 引用（保持顺序）
    paragraphs: list = []  # 每项是 python-pptx 的 _Paragraph 对象
    smartart_text_elements: list = []  # SmartArt 内的 <a:t> XML 元素

    def _walk_shape(shape) -> None:
        # 分组：递归
        if shape.shape_type == MSO_SHAPE_TYPE.GROUP:
            for sub in shape.shapes:
                _walk_shape(sub)
            return
        # 表格
        if shape.has_table:
            for row in shape.table.rows:
                for cell in row.cells:
                    if cell.text_frame:
                        for p in cell.text_frame.paragraphs:
                            paragraphs.append(p)
            return
        # 文本框 / 标题 / 形状文字
        if shape.has_text_frame:
            for p in shape.text_frame.paragraphs:
                paragraphs.append(p)
            return
        # SmartArt：python-pptx 不直接支持，扫描底层 XML 的 a:t 元素
        try:
            element = shape._element
            # SmartArt 数据图引用：通过命名空间找 a:t
            ns = {"a": "http://schemas.openxmlformats.org/drawingml/2006/main"}
            for t_elem in element.findall(".//a:t", ns):
                smartart_text_elements.append(t_elem)
        except Exception:  # noqa: BLE001
            pass

    # 1. 母版
    for master in prs.slide_masters:
        for shape in master.shapes:
            _walk_shape(shape)
        # 版式
        for layout in master.slide_layouts:
            for shape in layout.shapes:
                _walk_shape(shape)

    # 2. 幻灯片
    for slide in prs.slides:
        for shape in slide.shapes:
            _walk_shape(shape)
        # 备注
        if slide.has_notes_slide and slide.notes_slide.notes_text_frame:
            for p in slide.notes_slide.notes_text_frame.paragraphs:
                paragraphs.append(p)

    # 准备翻译列表
    para_texts: list[str] = []
    for p in paragraphs:
        para_texts.append("".join(run.text or "" for run in p.runs))

    smart_texts: list[str] = [t.text or "" for t in smartart_text_elements]

    # 合并请求一次发出（位置信息内嵌在两个列表的索引里）
    all_texts = para_texts + smart_texts
    if not all_texts:
        out = io.BytesIO()
        prs.save(out)
        return out.getvalue()

    translated = _translate_many(all_texts, translator, ctx)
    para_translated = translated[: len(para_texts)]
    smart_translated = translated[len(para_texts):]

    # 3. 写回 paragraph：把译文放在第一个 run，其余 run 清空（保留首 run 字体）
    for p, new_text in zip(paragraphs, para_translated):
        if not new_text:
            continue
        runs = list(p.runs)
        if not runs:
            continue
        runs[0].text = new_text
        for r in runs[1:]:
            r.text = ""

    # 4. 写回 SmartArt 文本元素
    for t_elem, new_text in zip(smartart_text_elements, smart_translated):
        if new_text:
            t_elem.text = new_text

    # P1.3：图片 OCR + 就地替换文字
    _replace_pptx_images(prs, ctx)

    # P1.5：阿拉伯语 RTL 排版
    is_rtl = ctx.target_lang.lower() in ("ar", "ara", "arabic")
    if is_rtl:
        _apply_pptx_rtl(prs)

    out = io.BytesIO()
    prs.save(out)
    return out.getvalue()


def _apply_pptx_rtl(prs) -> None:
    """P1.5：为 PPT 文档设置 RTL（从右到左）排版。"""
    from pptx.enum.text import PP_ALIGN

    for slide in prs.slides:
        for shape in slide.shapes:
            if shape.has_text_frame:
                for paragraph in shape.text_frame.paragraphs:
                    paragraph.alignment = PP_ALIGN.RIGHT
                    for run in paragraph.runs:
                        run.font.language_id = 1025  # Arabic LCID


def translate_xlsx(data: bytes, translator: Translator, ctx: TranslationContext) -> bytes:
    """Excel (.xlsx) 翻译。

    规则（需求 2.2）：
    - 单元格文字：翻译；公式 / 数字 / 日期 / 布尔：不动
    - 合并单元格：openpyxl 自动保留 `merged_cells`，仅左上角格有值
    - 单元格格式：openpyxl 默认保留所有 style
    - 工作表名称：翻译（重名时自动加后缀）
    - 图表标题：翻译（轴标签 / 数据标签等深层结构 P1.1.b 增强）
    - 富文本（一格多种字体）：本期作为单字体译文写回，保字体颜色但合并多字体
    - 内嵌图片：原样保留
    - 批注 / 数据验证下拉项：本期不翻译（归 P2）
    """
    import re

    from openpyxl import load_workbook
    from openpyxl.cell.cell import TYPE_FORMULA
    from openpyxl.utils.cell import column_index_from_string

    wb = load_workbook(io.BytesIO(data))

    # ====== 第 0 阶段：扫描所有公式，收集字面量黑名单 + 命中字面量的格子坐标 ======
    # 设计：被公式引用的范围里，只有"内容等于公式字符串字面量"的格子需要保持原文，
    # 其它纯数据格子（员工姓名、部门下属内容等）正常翻译。
    # 这样 VLOOKUP/COUNTIF/MATCH 等查找类公式的字面量仍能在表里命中，结果不变。
    formula_literals_to_skip: set[str] = set()
    # (sheet_idx, row, col) -> 该格内容若与某字面量相同则跳过翻译
    formula_ranges: list[tuple[int, int, int, int, int]] = []  # (sheet_idx, r1, c1, r2, c2)

    # 表名 -> sheet_idx 映射，用于解析跨表引用
    sheet_name_to_idx: dict[str, int] = {}
    for idx, ws in enumerate(wb.worksheets):
        sheet_name_to_idx[ws.title] = idx

    # 匹配单元格/范围引用，支持跨表：[SheetName!]A1[:B2]
    # 组：sheet_name(可选) / col_a / row_a / col_b(可选) / row_b(可选)
    _REF_RE = re.compile(
        r"(?<![A-Za-z0-9_$:!])"
        r"(?:'([^']+)'!|([A-Za-z0-9_]+)!)?"
        r"([A-Z]{1,3})(\$?\d+)"
        r"(?::([A-Z]{1,3})(\$?\d+))?"
    )

    def _resolve_sheet_idx(current_idx: int, sheet_name: str | None) -> int | None:
        """把公式里的 sheet 引用名解析为 sheet_idx。"""
        if not sheet_name:
            return current_idx
        return sheet_name_to_idx.get(sheet_name)

    for sheet_idx, ws in enumerate(wb.worksheets):
        for row in ws.iter_rows():
            for cell in row:
                if cell.data_type != TYPE_FORMULA:
                    continue
                formula_str = cell.value if isinstance(cell.value, str) else None
                if not formula_str:
                    continue
                # 字符串字面量
                for lit in re.findall(r'"([^"]*)"', formula_str):
                    if lit:
                        formula_literals_to_skip.add(lit)
                # 单元格 / 范围引用（含跨表 Sheet!A1:B2）
                for ref_match in re.finditer(_REF_RE, formula_str):
                    sheet_quoted, sheet_plain, col_a, row_a, col_b, row_b = ref_match.groups()
                    sheet_name = sheet_quoted or sheet_plain
                    target_sheet_idx = _resolve_sheet_idx(sheet_idx, sheet_name)
                    if target_sheet_idx is None:
                        continue
                    try:
                        c1 = column_index_from_string(col_a)
                        r1 = int(row_a.lstrip("$"))
                        if col_b and row_b:
                            c2 = column_index_from_string(col_b)
                            r2 = int(row_b.lstrip("$"))
                        else:
                            c2, r2 = c1, r1
                        formula_ranges.append((target_sheet_idx, min(r1, r2), min(c1, c2), max(r1, r2), max(c1, c2)))
                    except Exception:  # noqa: BLE001
                        continue

    def _cell_in_any_formula_range(sheet_idx: int, row: int, col: int) -> bool:
        for s, r1, c1, r2, c2 in formula_ranges:
            if s == sheet_idx and r1 <= row <= r2 and c1 <= col <= c2:
                return True
        return False

    # ====== 第一阶段：扫描所有可翻译文本，记录定位信息 ======
    # 定位类型：
    #   ("cell", sheet_idx, row, col)
    #   ("sheet_name", sheet_idx)
    #   ("chart_title", sheet_idx, chart_idx)
    locations: list[tuple] = []
    texts: list[str] = []

    def _is_translatable_str(value) -> bool:
        if not isinstance(value, str):
            return False
        s = value.strip()
        if not s:
            return False
        # 公式（保险起见再判断）
        if s.startswith("="):
            return False
        # 纯数字 / 含千分位的数字
        if re.fullmatch(r"[+-]?[\d,]+(\.\d+)?%?", s):
            return False
        return True

    for sheet_idx, ws in enumerate(wb.worksheets):
        # 工作表名
        if ws.title and ws.title.strip():
            locations.append(("sheet_name", sheet_idx))
            texts.append(ws.title)

        # 单元格
        for row in ws.iter_rows():
            for cell in row:
                if cell.data_type == TYPE_FORMULA:
                    # 公式整体保留不翻译
                    continue
                if not _is_translatable_str(cell.value):
                    continue
                # 跳过：内容与公式字面量完全一致（且自己确实落在某个公式引用范围内）。
                # 这样 VLOOKUP/COUNTIF/MATCH 的查找键保持不变，公式重算结果保持一致；
                # 同范围内"内容不等于字面量"的纯数据格子（员工姓名、部门、备注等）仍可翻译。
                if cell.value in formula_literals_to_skip and _cell_in_any_formula_range(
                    sheet_idx, cell.row, cell.column
                ):
                    continue
                locations.append(("cell", sheet_idx, cell.row, cell.column))
                texts.append(cell.value)

        # 图表标题（_charts 是 openpyxl 内部属性，但稳定可用）
        try:
            charts = list(ws._charts)
        except Exception:  # noqa: BLE001
            charts = []
        for chart_idx, chart in enumerate(charts):
            title_text = _get_chart_title_text(chart)
            if title_text and _is_translatable_str(title_text):
                locations.append(("chart_title", sheet_idx, chart_idx))
                texts.append(title_text)

    if not texts:
        out = io.BytesIO()
        wb.save(out)
        return out.getvalue()

    # ====== 第二阶段：并发翻译（复用段落级并发 + 分片调度） ======
    translated = _translate_many(texts, translator, ctx)

    # ====== 第三阶段：写回 ======
    used_titles: set[str] = set()
    for loc, new_text in zip(locations, translated):
        if not new_text:
            continue
        kind = loc[0]
        if kind == "sheet_name":
            sheet_idx = loc[1]
            ws = wb.worksheets[sheet_idx]
            new_title = new_text.strip()[:31]  # Excel sheet 名长度上限 31
            # 去除 Excel 不允许的字符
            new_title = re.sub(r'[\\/*?:\[\]]', "_", new_title) or ws.title
            # 重名兜底
            base = new_title
            n = 2
            while new_title in used_titles:
                suffix = f"_{n}"
                new_title = base[: 31 - len(suffix)] + suffix
                n += 1
            ws.title = new_title
            used_titles.add(new_title)
        elif kind == "cell":
            _, sheet_idx, row, col = loc
            ws = wb.worksheets[sheet_idx]
            ws.cell(row=row, column=col).value = new_text
        elif kind == "chart_title":
            _, sheet_idx, chart_idx = loc
            ws = wb.worksheets[sheet_idx]
            try:
                chart = list(ws._charts)[chart_idx]
                _set_chart_title_text(chart, new_text)
            except Exception:  # noqa: BLE001
                pass

    out = io.BytesIO()
    wb.save(out)
    return out.getvalue()


def _get_chart_title_text(chart) -> str | None:
    """提取图表标题纯文本（图表对象层级较深，此处尽量兜底）。"""
    try:
        title = chart.title
        if title is None:
            return None
        # 字符串型标题
        if isinstance(title, str):
            return title
        # RichText 类型
        rich = getattr(title, "tx", None)
        rich = getattr(rich, "rich", None) if rich is not None else None
        if rich is None:
            return None
        parts: list[str] = []
        for p in getattr(rich, "p", []) or []:
            for r in getattr(p, "r", []) or []:
                t = getattr(r, "t", "") or ""
                if t:
                    parts.append(t)
        return "".join(parts) or None
    except Exception:  # noqa: BLE001
        return None


def _set_chart_title_text(chart, new_text: str) -> None:
    """把图表标题替换为译文（仅替换文本，不动字体）。"""
    try:
        title = chart.title
        if title is None:
            return
        if isinstance(title, str):
            chart.title = new_text
            return
        rich = getattr(title, "tx", None)
        rich = getattr(rich, "rich", None) if rich is not None else None
        if rich is None:
            return
        # 找到第一个 r，写入译文，其它 r 清空
        first = True
        for p in getattr(rich, "p", []) or []:
            for r in getattr(p, "r", []) or []:
                if first:
                    r.t = new_text
                    first = False
                else:
                    r.t = ""
    except Exception:  # noqa: BLE001
        pass


def translate_file(
    ext: str,
    data: bytes,
    translator: Translator,
    ctx: TranslationContext,
    pdf_output_format: str = "pdf",
) -> tuple[bytes, str]:
    """返回 (翻译后字节, 输出文件扩展名)。

    pdf_output_format: 仅当 ext == "pdf" 时生效
        - "pdf"  : PyMuPDF 就地替换，输出仍是 PDF（默认，最大限度保留原版面）
        - "docx" : 转 Word 输出，便于二次编辑
    """
    ext = ext.lower().lstrip(".")
    if ext == "txt":
        return translate_txt(data, translator, ctx), "txt"
    if ext == "md":
        return translate_md(data, translator, ctx), "md"
    if ext == "docx":
        return translate_docx(data, translator, ctx), "docx"
    if ext == "doc":
        # .doc 输出转为 .docx（LibreOffice 转换后走 docx 翻译路径）
        return translate_doc(data, translator, ctx), "docx"
    if ext == "xlsx":
        return translate_xlsx(data, translator, ctx), "xlsx"
    if ext == "xls":
        # .xls 输出转为 .xlsx（业界标准做法）
        return translate_xls(data, translator, ctx), "xlsx"
    if ext == "csv":
        return translate_csv(data, translator, ctx), "csv"
    if ext == "pptx":
        return translate_pptx(data, translator, ctx), "pptx"
    if ext == "ppt":
        # .ppt 输出转为 .pptx（LibreOffice 转换后走 pptx 翻译路径）
        return translate_ppt(data, translator, ctx), "pptx"
    if ext == "pdf":
        # 对照模式下译文翻倍，写回原 PDF span 的 bbox 会严重溢出 / 字号骤缩到不可读，
        # 因此强制走"转 Word"路径——段落可自由换行，对照内容才能正常显示。
        if ctx.output_mode == OutputMode.BILINGUAL:
            logger.info("PDF + 对照模式：强制输出 Word，避免就地替换文字溢出")
            return translate_pdf_to_docx(data, translator, ctx), "docx"
        if pdf_output_format == "docx":
            return translate_pdf_to_docx(data, translator, ctx), "docx"
        return translate_pdf_inplace(data, translator, ctx), "pdf"
    raise ValueError(f"暂不支持的格式：{ext}")
