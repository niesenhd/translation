"""PaddleOCR 离线 OCR 引擎 + PP-Structure 版面分析。

组合方案：
- PP-Structure：版面分析（识别表格、图片、文字区域等）+ 表格结构识别
- PaddleOCR：文字识别（精确识别每个文字区域的内容）

各取所长：
- PP-Structure 擅长版面分析，能准确识别表格区域并提取表格结构
- PaddleOCR 擅长文字识别，对普通文字区域的识别精度更高

纯 CPU / GPU 均可运行，无需联网，适合内网私有化部署。
"""
from __future__ import annotations

import io
import logging
import os
import threading
from dataclasses import dataclass, field
from typing import Optional

# 关闭 oneDNN/MKL-DNN 推理加速：某些 CPU（如部分 Intel/AMD 服务器）
# 在 Paddle 3.x + oneDNN 组合下会报
# "ConvertPirAttribute2RuntimeAttribute not support" 等错误，导致 OCR 推理失败
# 进而 fallback 到 VL 模型走外网。这里在模块加载前强制关闭，确保本地推理可用。
# 必须设在 import paddle 之前，所以放到模块顶部。
os.environ.setdefault("FLAGS_use_mkldnn", "false")
os.environ.setdefault("FLAGS_use_onednn", "false")
# 限制 CPU 线程数：OCR 推理默认会用满所有核，导致 SSH 无响应
os.environ.setdefault("OMP_NUM_THREADS", "4")
os.environ.setdefault("MKL_NUM_THREADS", "4")

# 禁用 Paddle IR（PIR）：PaddlePaddle 3.x 默认启用 PIR，在 CPU 推理路径中
# PIR + oneDNN 组合会导致 "ConvertPirAttribute2RuntimeAttribute not support" 错误。
# 即使 FLAGS_use_mkldnn=false，PIR 仍会生成 oneDNN 指令。通过 monkey-patch
# paddle.inference.Config.enable_new_ir 强制关闭 PIR，使推理走旧路径。
# 必须在每次创建引擎前确保补丁已应用（Celery fork 后模块级补丁可能失效）。
def _apply_pir_patch():
    """确保 paddle.inference.Config.enable_new_ir 被强制禁用。"""
    import paddle as _pd
    if not getattr(_pd.inference.Config, '_pir_patched', False):
        _orig = _pd.inference.Config.enable_new_ir
        def _patched(self, enable=True):
            return _orig(self, False)
        _pd.inference.Config.enable_new_ir = _patched
        _pd.inference.Config._pir_patched = True
        log.info("已应用 PIR 禁用补丁：paddle.inference.Config.enable_new_ir -> False")

from PIL import Image

from app.services.ocr import TextRegion

log = logging.getLogger(__name__)

# 引擎单例
# OCR 引擎按 Paddle 语种缓存：不同语种加载不同识别模型。
# 此前是单个全局单例，第一次初始化的语种"固化"，后续俄语/阿语等
# 文档的图片仍用中英模型识别，产出乱码。
_ocr_engines: dict[str, object] = {}
_structure_engine = None
_engine_lock = threading.Lock()


@dataclass
class PaddleOCRResult:
    """PaddleOCR 单条识别结果。"""
    bbox: list[float]  # [x1, y1, x2, y2] 归一化到 0~1
    text: str
    confidence: float


@dataclass
class StructureRegion:
    """PP-Structure 版面分析结果。"""
    bbox: list[float]       # [x1, y1, x2, y2] 归一化到 0~1
    region_type: str        # "text", "table", "figure", "title", "list", "header", "footer"
    text: str = ""          # 文字内容（text/title/list 类型）
    html: str = ""          # 表格 HTML（table 类型）
    confidence: float = 0.0


def _map_lang(lang: str) -> str:
    """将系统语种代码映射为 PaddleOCR 语种代码。"""
    mapping = {
        "zh": "ch",       # 中英混合
        "en": "en",
        "fr": "fr",
        "de": "german",
        "ko": "korean",
        "ja": "japan",
        "ru": "ru",
        "es": "es",
        "pt": "pt",
        "it": "it",
        "ar": "ar",
        "hi": "hi",
        "th": "th",
        "vi": "vi",
        "id": "id",
        "ms": "ms",
    }
    src = lang.split("→")[0].strip() if "→" in lang else lang
    return mapping.get(src, "ch")


def _get_ocr_engine(lang: str = "ch"):
    """获取 PaddleOCR 引擎单例（用于纯文字识别）。

    适配 PaddleOCR 3.x 新 API：
    - 移除了 `use_angle_cls` / `show_log`
    - 改为 `use_textline_orientation` / `use_doc_orientation_classify`
    - 调用接口为 `predict()`，返回单 dict 含 `rec_texts` / `rec_scores` / `rec_boxes`
    """
    paddle_lang = _map_lang(lang)
    engine = _ocr_engines.get(paddle_lang)
    if engine is None:
        with _engine_lock:
            engine = _ocr_engines.get(paddle_lang)  # double-check
            if engine is None:
                _apply_pir_patch()
                try:
                    from paddleocr import PaddleOCR
                    engine = PaddleOCR(
                        lang=paddle_lang,
                        # 关闭文档级方向分类（图像里通常都是正向文字）
                        use_doc_orientation_classify=False,
                        # 关闭文档矫正（普通图片不需要）
                        use_doc_unwarping=False,
                        # 开启文字行方向检测（替代旧版 use_angle_cls）
                        use_textline_orientation=True,
                    )
                    _ocr_engines[paddle_lang] = engine
                    log.info("PaddleOCR 引擎初始化成功 (lang=%s→%s)", lang, paddle_lang)
                except ImportError:
                    log.error("PaddleOCR 未安装，请运行: pip install paddleocr paddlepaddle")
                    raise
                except Exception as e:
                    # 非中英语种模型可能未随镜像预下载/不受支持——回退中英模型，
                    # 至少保证拉丁字母/数字可识别，不让整个任务失败
                    if paddle_lang != "ch":
                        log.warning("PaddleOCR 语种 %s 初始化失败（%s），回退 ch 模型", paddle_lang, e)
                        return _get_ocr_engine("zh")
                    log.error("PaddleOCR 初始化失败: %s", e)
                    raise
    return engine


def _get_structure_engine(lang: str = "ch"):
    """获取 PP-Structure V3 引擎单例（用于版面分析 + 表格识别）。

    适配 PaddleOCR 3.x：原 `PPStructure` 已升级为 `PPStructureV3`，
    参数体系完全重构，初始化时仅设置基础开关，运行时通过 `predict()` 控制流程。
    需安装：pip install "paddlex[ocr]"。
    """
    global _structure_engine
    if _structure_engine is None:
        with _engine_lock:
            if _structure_engine is None:  # double-check
                _apply_pir_patch()
                try:
                    from paddleocr import PPStructureV3
                    _structure_engine = PPStructureV3(
                        # 关闭文档级方向/矫正（节省时间）
                        use_doc_orientation_classify=False,
                        use_doc_unwarping=False,
                        # 开启表格识别（核心能力）
                        use_table_recognition=True,
                        # 关闭印章/公式/图表识别（与文字翻译无关，开启会拖慢速度）
                        use_seal_recognition=False,
                        use_formula_recognition=False,
                        use_chart_recognition=False,
                    )
                    log.info("PP-StructureV3 引擎初始化成功 (lang=%s)", lang)
                except ImportError:
                    log.error("PaddleOCR / paddlex[ocr] 未安装，请运行: pip install paddleocr paddlepaddle 'paddlex[ocr]'")
                    raise
                except Exception as e:
                    log.error("PP-StructureV3 初始化失败: %s", e)
                    raise
    return _structure_engine



def _normalize_bbox(bbox_points, img_w: int, img_h: int) -> list[float]:
    """将四点坐标转为 [x1, y1, x2, y2] 并归一化到 0~1。"""
    if isinstance(bbox_points, (list, tuple)) and len(bbox_points) == 4:
        # 可能是 [[x1,y1],[x2,y2],[x3,y3],[x4,y4]] 或 [x1, y1, x2, y2]
        if isinstance(bbox_points[0], (list, tuple)):
            xs = [p[0] for p in bbox_points]
            ys = [p[1] for p in bbox_points]
            x1 = min(xs) / img_w
            y1 = min(ys) / img_h
            x2 = max(xs) / img_w
            y2 = max(ys) / img_h
        else:
            x1 = float(bbox_points[0]) / img_w
            y1 = float(bbox_points[1]) / img_h
            x2 = float(bbox_points[2]) / img_w
            y2 = float(bbox_points[3]) / img_h
    else:
        return [0.0, 0.0, 0.0, 0.0]

    # 确保在 0~1 范围内
    x1 = max(0.0, min(1.0, x1))
    y1 = max(0.0, min(1.0, y1))
    x2 = max(0.0, min(1.0, x2))
    y2 = max(0.0, min(1.0, y2))

    # 确保 x1 < x2, y1 < y2
    if x1 > x2:
        x1, x2 = x2, x1
    if y1 > y2:
        y1, y2 = y2, y1

    return [x1, y1, x2, y2]


def paddleocr_recognize(image_bytes: bytes, lang: str = "ch") -> list[PaddleOCRResult]:
    """使用 PaddleOCR 识别图片中的文字。

    适配 PaddleOCR 3.x 新 API：调用 `predict()`，返回单个 dict（每张图一条），
    内部含 `rec_texts` / `rec_scores` / `rec_boxes` / `dt_polys`。

    Args:
        image_bytes: 图片二进制数据
        lang: 语种代码（如 "zh", "en", "zh→en"）

    Returns:
        识别结果列表，bbox 归一化到 0~1
    """
    engine = _get_ocr_engine(lang)
    img = Image.open(io.BytesIO(image_bytes))
    if img.mode != "RGB":
        img = img.convert("RGB")
    img_w, img_h = img.size

    import numpy as np
    img_array = np.array(img)

    # 新版 API：使用 predict()
    results = engine.predict(img_array)

    if not results:
        return []

    ocr_results: list[PaddleOCRResult] = []
    for r in results:
        # r 类似 dict 的对象，含 rec_texts / rec_scores / rec_boxes / dt_polys
        rec_texts = r.get("rec_texts") if hasattr(r, "get") else getattr(r, "rec_texts", None)
        rec_scores = r.get("rec_scores") if hasattr(r, "get") else getattr(r, "rec_scores", None)
        # 优先用 rec_boxes（[x1,y1,x2,y2] 格式）；否则用 dt_polys（4 点格式）
        rec_boxes = r.get("rec_boxes") if hasattr(r, "get") else getattr(r, "rec_boxes", None)
        dt_polys = r.get("dt_polys") if hasattr(r, "get") else getattr(r, "dt_polys", None)

        if not rec_texts:
            continue

        # 优先使用 rec_boxes（更紧凑）
        if rec_boxes is not None and len(rec_boxes) > 0:
            for i, text in enumerate(rec_texts):
                if i >= len(rec_boxes):
                    break
                box = rec_boxes[i]  # [x1, y1, x2, y2]
                if hasattr(box, "tolist"):
                    box = box.tolist()
                if len(box) < 4:
                    continue
                bbox = _normalize_bbox(list(box[:4]), img_w, img_h)
                if (bbox[2] - bbox[0]) < 0.005 or (bbox[3] - bbox[1]) < 0.005:
                    continue
                conf = float(rec_scores[i]) if rec_scores is not None and i < len(rec_scores) else 0.0
                ocr_results.append(PaddleOCRResult(bbox=bbox, text=text, confidence=conf))
        elif dt_polys is not None and len(dt_polys) > 0:
            for i, text in enumerate(rec_texts):
                if i >= len(dt_polys):
                    break
                poly = dt_polys[i]
                if hasattr(poly, "tolist"):
                    poly = poly.tolist()
                bbox = _normalize_bbox(poly, img_w, img_h)
                if (bbox[2] - bbox[0]) < 0.005 or (bbox[3] - bbox[1]) < 0.005:
                    continue
                conf = float(rec_scores[i]) if rec_scores is not None and i < len(rec_scores) else 0.0
                ocr_results.append(PaddleOCRResult(bbox=bbox, text=text, confidence=conf))

    return ocr_results


def ppstructure_analyze(image_bytes: bytes, lang: str = "ch") -> list[StructureRegion]:
    """使用 PP-Structure V3 进行版面分析 + 表格识别。

    PP-Structure V3 输出结构（每张图返回一条 result）：
    - layout_det_res: 版面检测结果（含 bbox / label）
    - overall_ocr_res: OCR 整体结果
    - table_res_list: 表格识别结果列表（每个含 bbox + html）
    - parsing_res_list: 段落级解析结果（含 block_label / block_bbox / block_content）

    我们以 `parsing_res_list` 为主提取语义块，遇到 table 类型则关联 `table_res_list` 拿 HTML。

    Args:
        image_bytes: 图片二进制数据
        lang: 语种代码

    Returns:
        版面分析结果列表（StructureRegion）
    """
    engine = _get_structure_engine(lang)
    img = Image.open(io.BytesIO(image_bytes))
    if img.mode != "RGB":
        img = img.convert("RGB")
    img_w, img_h = img.size

    import numpy as np
    img_array = np.array(img)

    # 新版 API：predict()
    results = engine.predict(img_array)

    if not results:
        return []

    structure_results: list[StructureRegion] = []

    for r in results:
        # 兼容 dict 与对象
        def _g(key, default=None):
            if hasattr(r, "get"):
                return r.get(key, default)
            return getattr(r, key, default)

        # 表格 HTML 索引：bbox 元组 -> html
        table_html_map: dict[tuple, str] = {}
        table_res_list = _g("table_res_list") or []
        for t in table_res_list:
            t_bbox = t.get("table_region_id") if hasattr(t, "get") else getattr(t, "table_region_id", None)
            t_html = t.get("pred_html") if hasattr(t, "get") else getattr(t, "pred_html", "")
            if not t_html:
                # 备用字段名
                t_html = t.get("html") if hasattr(t, "get") else getattr(t, "html", "")
            cell_box = t.get("cell_box_list") if hasattr(t, "get") else getattr(t, "cell_box_list", None)
            # 用整体 bbox 作为索引；尝试常见字段
            t_box = t.get("bbox") if hasattr(t, "get") else getattr(t, "bbox", None)
            if t_box is not None and t_html:
                key = tuple(float(x) for x in (list(t_box.tolist()) if hasattr(t_box, "tolist") else list(t_box))[:4])
                table_html_map[key] = t_html

        # 主入口：parsing_res_list（语义块）
        parsing_list = _g("parsing_res_list") or []
        for block in parsing_list:
            label = block.get("block_label") if hasattr(block, "get") else getattr(block, "block_label", "text")
            bbox_raw = block.get("block_bbox") if hasattr(block, "get") else getattr(block, "block_bbox", None)
            content = block.get("block_content") if hasattr(block, "get") else getattr(block, "block_content", "")

            if bbox_raw is None:
                continue
            if hasattr(bbox_raw, "tolist"):
                bbox_raw = bbox_raw.tolist()
            bbox = _normalize_bbox(list(bbox_raw[:4]) if len(bbox_raw) >= 4 else bbox_raw, img_w, img_h)
            if (bbox[2] - bbox[0]) < 0.005 or (bbox[3] - bbox[1]) < 0.005:
                continue

            # 跳过图片/印章等非文字区域
            if label in ("image", "figure", "seal", "chart"):
                continue

            text = ""
            html = ""
            if label == "table":
                # 优先在 table_html_map 中匹配
                key = tuple(float(x) for x in bbox_raw[:4])
                html = table_html_map.get(key, "")
                if not html and isinstance(content, str) and "<" in content:
                    html = content
                text = _html_to_text(html) if html else (content if isinstance(content, str) else "")
            else:
                text = content if isinstance(content, str) else ""

            if not text.strip() and not html.strip():
                continue

            structure_results.append(StructureRegion(
                bbox=bbox,
                region_type="table" if label == "table" else "text",
                text=text.strip(),
                html=html,
                confidence=1.0,
            ))

        # 兜底：parsing_res_list 为空但 table_res_list 不为空时，单独输出表格
        if not parsing_list and table_res_list:
            for t in table_res_list:
                t_box = t.get("bbox") if hasattr(t, "get") else getattr(t, "bbox", None)
                t_html = t.get("pred_html") if hasattr(t, "get") else getattr(t, "pred_html", "")
                if t_box is None or not t_html:
                    continue
                if hasattr(t_box, "tolist"):
                    t_box = t_box.tolist()
                bbox = _normalize_bbox(list(t_box[:4]), img_w, img_h)
                structure_results.append(StructureRegion(
                    bbox=bbox,
                    region_type="table",
                    text=_html_to_text(t_html),
                    html=t_html,
                    confidence=1.0,
                ))

    return structure_results


def _html_to_text(html: str) -> str:
    """将表格 HTML 转为纯文本（简单提取 td 内容）。"""
    import re
    if not html:
        return ""
    # 提取所有 <td> 标签中的文本
    cells = re.findall(r'<td[^>]*>(.*?)</td>', html, re.DOTALL)
    # 去除 HTML 标签
    clean_cells = []
    for cell in cells:
        clean = re.sub(r'<[^>]+>', '', cell).strip()
        if clean:
            clean_cells.append(clean)
    return " | ".join(clean_cells)


def _has_table_regions(regions: list[StructureRegion]) -> bool:
    """判断版面分析结果中是否包含表格区域。"""
    return any(r.region_type == "table" for r in regions)


def paddleocr_combined_recognize(
    image_bytes: bytes,
    target_lang: str,
    lang: str = "ch",
    source_lang: str = "auto",
    glossary: list[dict] | None = None,
    tm_lookup=None,
) -> list[TextRegion]:
    """PaddleOCR + PP-Structure 组合识别方案。

    策略：
    1. 先用 PP-Structure 进行版面分析，识别表格、文字区域等
    2. 如果检测到表格区域，使用 PP-Structure 的表格识别结果（保留表格结构）
    3. 对于非表格的文字区域，使用 PaddleOCR 进行更精确的文字识别
    4. 如果 PP-Structure 未检测到表格，则直接使用 PaddleOCR 识别所有文字

    这样可以：
    - 利用 PP-Structure 的表格识别能力，保留表格结构
    - 利用 PaddleOCR 的文字识别精度，提高普通文字的识别质量

    Args:
        image_bytes: 图片二进制数据
        target_lang: 目标语种
        lang: 源语种代码
        source_lang: 翻译用源语种（默认 auto）
        glossary: 术语库
        tm_lookup: 翻译记忆库查询函数

    Returns:
        TextRegion 列表
    """
    from app.services.ocr import _should_skip_translation, _batch_translate

    img = Image.open(io.BytesIO(image_bytes))
    if img.mode != "RGB":
        img = img.convert("RGB")
    img_w, img_h = img.size

    # 第一步：PP-Structure 版面分析
    try:
        structure_results = ppstructure_analyze(image_bytes, lang)
        log.info("PP-Structure 版面分析识别到 %d 个区域", len(structure_results))
    except Exception as exc:
        log.warning("PP-Structure 分析失败，降级使用 PaddleOCR: %s", exc)
        structure_results = []

    # 如果没有检测到表格，直接使用 PaddleOCR 识别（更精确）
    if not structure_results or not _has_table_regions(structure_results):
        log.info("未检测到表格区域，使用 PaddleOCR 纯文字识别")
        return _paddleocr_text_regions(
            image_bytes, target_lang, lang,
            source_lang=source_lang, glossary=glossary, tm_lookup=tm_lookup,
        )

    # 第二步：有表格区域，组合使用 PP-Structure + PaddleOCR
    log.info("检测到表格区域，使用 PP-Structure + PaddleOCR 组合方案")
    combined_regions: list[TextRegion] = []

    # 收集非表格区域的 bbox，用于 PaddleOCR 结果去重
    table_bboxes: list[list[float]] = []

    for sr in structure_results:
        if sr.region_type == "table":
            # 表格区域：使用 PP-Structure 的结果（保留表格结构信息）
            table_bboxes.append(sr.bbox)
            combined_regions.append(TextRegion(
                bbox=sr.bbox,
                original=sr.text,
                translated="",  # 待翻译
                is_table=True,
                table_html=sr.html,
            ))
        else:
            # 非表格文字区域：先收集，后续用 PaddleOCR 精确识别
            pass

    # 第三步：使用 PaddleOCR 识别所有文字
    ocr_results = paddleocr_recognize(image_bytes, lang)

    # 过滤掉与表格区域重叠的文字（这些已在表格中识别）
    for ocr in ocr_results:
        if _is_overlap_with_tables(ocr.bbox, table_bboxes):
            continue
        combined_regions.append(TextRegion(
            bbox=ocr.bbox,
            original=ocr.text,
            translated="",  # 待翻译
        ))

    if not combined_regions:
        return []

    # 第四步：批量翻译
    texts_to_translate = []
    translate_indices = []
    for i, r in enumerate(combined_regions):
        if _should_skip_translation(r.original):
            continue
        texts_to_translate.append(r.original)
        translate_indices.append(i)

    translated_map: dict[int, str] = {}
    if texts_to_translate:
        from app.services.ocr import _batch_translate
        target_name = target_lang  # _batch_translate 内部只用 target_lang
        translated_texts = _batch_translate(
            texts_to_translate, target_lang, target_name,
            source_lang=source_lang, glossary=glossary, tm_lookup=tm_lookup,
        )
        for idx, translated in zip(translate_indices, translated_texts):
            translated_map[idx] = translated

    # 填充翻译结果
    for i, r in enumerate(combined_regions):
        r.translated = translated_map.get(i, r.original)

    return combined_regions


def _paddleocr_text_regions(
    image_bytes: bytes,
    target_lang: str,
    lang: str = "ch",
    source_lang: str = "auto",
    glossary: list[dict] | None = None,
    tm_lookup=None,
) -> list[TextRegion]:
    """使用 PaddleOCR 纯文字识别（不含表格分析）。"""
    from app.services.ocr import _should_skip_translation, _batch_translate

    ocr_results = paddleocr_recognize(image_bytes, lang)

    if not ocr_results:
        return []

    # 分离需要翻译和不需要翻译的文本
    texts_to_translate = []
    translate_indices = []
    for i, r in enumerate(ocr_results):
        if _should_skip_translation(r.text):
            continue
        texts_to_translate.append(r.text)
        translate_indices.append(i)

    # 批量翻译
    translated_map: dict[int, str] = {}
    if texts_to_translate:
        from app.services.ocr import _batch_translate
        target_name = target_lang  # _batch_translate 内部只用 target_lang
        translated_texts = _batch_translate(
            texts_to_translate, target_lang, target_name,
            source_lang=source_lang, glossary=glossary, tm_lookup=tm_lookup,
        )
        for idx, translated in zip(translate_indices, translated_texts):
            translated_map[idx] = translated

    # 组装 TextRegion
    regions: list[TextRegion] = []
    for i, r in enumerate(ocr_results):
        translated = translated_map.get(i, r.text)
        regions.append(TextRegion(
            bbox=r.bbox,
            original=r.text,
            translated=translated,
        ))

    return regions


def _is_overlap_with_tables(
    bbox: list[float],
    table_bboxes: list[list[float]],
    threshold: float = 0.5,
) -> bool:
    """判断一个 bbox 是否与表格区域重叠。

    Args:
        bbox: 待判断的区域 [x1, y1, x2, y2]
        table_bboxes: 表格区域列表
        threshold: 重叠比例阈值，超过此值认为重叠

    Returns:
        True 表示与表格重叠
    """
    if not table_bboxes:
        return False

    x1, y1, x2, y2 = bbox
    area = (x2 - x1) * (y2 - y1)
    if area <= 0:
        return False

    for tb in table_bboxes:
        # 计算交集
        ix1 = max(x1, tb[0])
        iy1 = max(y1, tb[1])
        ix2 = min(x2, tb[2])
        iy2 = min(y2, tb[3])

        if ix1 < ix2 and iy1 < iy2:
            inter_area = (ix2 - ix1) * (iy2 - iy1)
            overlap_ratio = inter_area / area
            if overlap_ratio > threshold:
                return True

    return False


def paddleocr_to_text_regions(
    image_bytes: bytes,
    target_lang: str,
    lang: str = "ch",
    source_lang: str = "auto",
    glossary: list[dict] | None = None,
    tm_lookup=None,
) -> list[TextRegion]:
    """使用 PaddleOCR + PP-Structure 组合方案识别图片文字。

    优先使用组合方案（PP-Structure版面分析 + PaddleOCR文字识别），
    如果 PP-Structure 不可用则降级为纯 PaddleOCR。

    Args:
        image_bytes: 图片二进制数据
        target_lang: 目标语种（用于翻译）
        lang: 源语种代码
        source_lang: 翻译用源语种（默认 auto）
        glossary: 术语库
        tm_lookup: 翻译记忆库查询函数

    Returns:
        TextRegion 列表
    """
    try:
        return paddleocr_combined_recognize(
            image_bytes, target_lang, lang,
            source_lang=source_lang, glossary=glossary, tm_lookup=tm_lookup,
        )
    except Exception as exc:
        log.warning("PaddleOCR 组合方案失败，降级使用纯 PaddleOCR: %s", exc)
        return _paddleocr_text_regions(
            image_bytes, target_lang, lang,
            source_lang=source_lang, glossary=glossary, tm_lookup=tm_lookup,
        )


def is_paddleocr_available() -> bool:
    """检查 PaddleOCR 是否可用。"""
    try:
        import paddleocr  # noqa: F401
        return True
    except ImportError:
        return False
