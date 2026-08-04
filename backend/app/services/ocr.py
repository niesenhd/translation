"""图片 OCR + 翻译（一体化）：使用 Qwen-VL 多模态能力。

设计：
- 一次调用同时完成"识别图中文字区域 + 翻译为目标语言"
- 模型自行判断是否为装饰性图（logo / 二维码 / 印章 / 签名 / 装饰图），返回 [SKIP]
- 返回结构化数据：每个文字区域的坐标、原文、译文
- 支持就地替换图片中的文字（擦除原文、写入译文）

返回：
- ocr_and_translate: 纯译文文本（向后兼容）
- ocr_image_regions: 结构化数据 [(bbox, original, translated), ...]
- process_image_ocr: 就地替换图片文字，返回修改后的图片字节
"""
from __future__ import annotations

import base64
import io
import json
import logging
import math
import re
import threading
import time
from dataclasses import dataclass
from typing import Optional

from openai import OpenAI
from openai import APIError, APITimeoutError, RateLimitError

from app.core.config import get_settings
from app.core.languages import resolve_language_name
from app.services.text_rules import should_skip_translation

logger = logging.getLogger(__name__)

# ---------- 简单 OCR（只返回译文文本，向后兼容） ----------

OCR_PROMPT = (
    "你是一个图像文字识别和翻译助手。请按以下规则处理这张图片：\n"
    "1. 如果图片是 logo、二维码、印章、签名、装饰性图形（无明确文字），输出 [SKIP]\n"
    "2. 如果图片含有可读文字，识别所有文字并翻译为 {target_lang}\n"
    "3. 仅输出译文本身，不要任何解释、引号、前缀\n"
    "4. 多行文字用换行符分隔，按视觉自上而下、自左而右的顺序\n"
    "5. 数字、邮箱、URL、商标名等保留原文不翻译"
)

# ---------- 结构化 OCR（返回文字区域坐标） ----------

OCR_REGION_PROMPT = (
    "你是一个高精度图像文字识别助手。请仔细分析这张图片，识别图中**所有**文字区域，不要遗漏任何文字。\n"
    "规则：\n"
    "1. 如果图片是 logo、二维码、印章、签名、装饰性图形（无明确文字），输出 [SKIP]\n"
    "2. 对每个文字区域，给出：\n"
    "   - bbox：文字在图片中的精确位置，归一化坐标 [x1, y1, x2, y2]（0.0~1.0），x1,y1 是左上角，x2,y2 是右下角\n"
    "   - text：该区域的原始文字（必须完整，不要截断）\n"
    "3. 不要包含二维码、logo、印章、图标等无文字区域\n"
    "4. 严格按以下 JSON 格式输出，不要任何其他内容：\n"
    '[{{"bbox": [x1, y1, x2, y2], "text": "原始文字"}}]\n'
    "5. 如果没有可识别的文字区域，输出 [SKIP]\n"
    "6. text 字段必须完整填写，不能省略或截断\n"
    "7. 每个独立的文字区域单独一个对象（同一行连续的文字归为一个区域）\n"
    "8. bbox 坐标必须精确覆盖文字区域，不要过大也不要过小\n"
    "9. 必须识别图中所有可见文字，不要遗漏任何一行或一个区域"
)


@dataclass
class TextRegion:
    """图片中的一个文字区域。"""
    bbox: list[float]  # [x1, y1, x2, y2] 归一化坐标 0.0~1.0
    original: str
    translated: str
    is_table: bool = False       # 是否为表格区域
    table_html: str = ""         # 表格 HTML 结构（PP-Structure 识别结果）


_client: Optional[OpenAI] = None
_client_lock = threading.Lock()


def _get_client() -> OpenAI:
    global _client
    if _client is None:
        with _client_lock:
            if _client is None:  # double-check
                settings = get_settings()
                from app.models.model_config import ModelType
                from app.services.model_config_service import get_runtime_model_config

                active = get_runtime_model_config(ModelType.VL, settings)
                api_key = active.api_key
                base_url = active.api_base_url
                _client = OpenAI(
                    api_key=api_key,
                    base_url=base_url,
                    timeout=120.0,
                )
    return _client


def reset_ocr_client() -> None:
    """重置 VL 模型客户端单例，使新配置生效。"""
    global _client
    with _client_lock:
        _client = None


def _is_too_small(image_bytes: bytes, min_side: int) -> bool:
    """图片过小（如装饰小图标）直接跳过 OCR，节省 token。"""
    try:
        from PIL import Image
        img = Image.open(io.BytesIO(image_bytes))
        w, h = img.size
        return w < min_side or h < min_side
    except Exception:  # noqa: BLE001
        return False


def _call_vl(prompt: str, image_bytes: bytes, mime: str = "image/png") -> str:
    """调用 Qwen-VL 多模态模型，返回原始文本响应。"""
    settings = get_settings()
    if not settings.enable_image_ocr:
        return ""
    if not image_bytes:
        return ""

    # 模型配置统一从 service 层读取，避免服务层反向依赖 API 路由。
    from app.models.model_config import ModelType
    from app.services.model_config_service import get_runtime_model_config

    vl_model = get_runtime_model_config(ModelType.VL, settings).model_id

    b64 = base64.b64encode(image_bytes).decode("ascii")
    image_url = f"data:{mime};base64,{b64}"

    last_err: Exception | None = None
    for attempt in range(4):
        try:
            completion = _get_client().chat.completions.create(
                model=vl_model,
                messages=[
                    {
                        "role": "user",
                        "content": [
                            {"type": "text", "text": prompt},
                            {"type": "image_url", "image_url": {"url": image_url}},
                        ],
                    }
                ],
                temperature=0.1,
                max_tokens=8192,
            )
            return (completion.choices[0].message.content or "").strip()
        except RateLimitError as exc:
            last_err = exc
            time.sleep(min(2 ** (attempt + 1), 30))
        except (APITimeoutError, APIError) as exc:
            last_err = exc
            time.sleep(min(1 + 2 * attempt, 15))
        except Exception as exc:  # noqa: BLE001
            last_err = exc
            break
    logger.warning("VL 模型调用失败：%s", last_err)
    return ""


def ocr_and_translate(
    image_bytes: bytes,
    target_lang: str,
    mime: str = "image/png",
    source_lang: str = "auto",
    glossary: list[dict] | None = None,
    tm_lookup: Callable | None = None,
) -> str:
    """对单张图片进行 OCR + 翻译。返回译文（空字符串表示跳过 / 无文字）。

    OCR 后端优先级与 ocr_image_regions 一致：
    1. PaddleOCR + PP-Structure（离线，优先使用）
    2. VL 模型（备选方案，需网络）
    """
    if _is_too_small(image_bytes, get_settings().ocr_min_image_size):
        return ""

    # 优先使用 PaddleOCR（本地离线）
    from app.services.paddle_ocr import is_paddleocr_available
    if is_paddleocr_available():
        try:
            from app.services.paddle_ocr import paddleocr_to_text_regions
            regions = paddleocr_to_text_regions(
                image_bytes, target_lang,
                # lang 决定识别模型语种——不传则恒为中英模型，
                # 俄语/阿语等文档的图片会识别成乱码
                lang=source_lang,
                source_lang=source_lang, glossary=glossary, tm_lookup=tm_lookup,
            )
            if regions:
                translated_lines = [r.translated for r in regions if r.translated]
                if translated_lines:
                    logger.info("PaddleOCR+PP-Structure 识别到 %d 个文字区域（ocr_and_translate）", len(regions))
                    return "\n".join(translated_lines)
            # regions 为空说明图片无文字（logo/二维码等），直接返回空
            return ""
        except Exception as exc:
            logger.warning("PaddleOCR 执行失败，降级使用 VL 模型: %s", exc)

    # 备选：VL 模型
    logger.info("PaddleOCR 不可用，使用 VL 模型（ocr_and_translate）")
    target_name = resolve_language_name(target_lang)
    text = _call_vl(OCR_PROMPT.format(target_lang=target_name), image_bytes, mime)
    if not text or text == "[SKIP]" or "[SKIP]" in text.upper():
        return ""
    return text


def ocr_image_regions(
    image_bytes: bytes,
    target_lang: str,
    mime: str = "image/png",
    source_lang: str = "auto",
    glossary: list[dict] | None = None,
    tm_lookup: Callable | None = None,
) -> list[TextRegion]:
    """对单张图片进行结构化 OCR + 翻译，返回文字区域列表。

    OCR 后端优先级：
    1. PaddleOCR + PP-Structure（离线，优先使用）
       - PP-Structure：版面分析 + 表格结构识别
       - PaddleOCR：精确文字识别
    2. VL 模型（如 Qwen-VL，备选方案）
       - 需要网络或私有化部署
       - 管理员可在模型配置中激活 VL 模型作为备选

    当管理员激活了 VL 模型且 PaddleOCR 不可用时，使用 VL 模型。
    """
    if _is_too_small(image_bytes, get_settings().ocr_min_image_size):
        return []

    # 优先使用 PaddleOCR + PP-Structure
    from app.services.paddle_ocr import is_paddleocr_available
    if is_paddleocr_available():
        try:
            from app.services.paddle_ocr import paddleocr_to_text_regions
            regions = paddleocr_to_text_regions(
                image_bytes, target_lang,
                lang=source_lang,
                source_lang=source_lang, glossary=glossary, tm_lookup=tm_lookup,
            )
            logger.info("PaddleOCR+PP-Structure 识别到 %d 个文字区域", len(regions))
            return regions
        except Exception as exc:
            logger.warning("PaddleOCR 执行失败: %s", exc)

    # 备选：使用 VL 模型
    logger.info("PaddleOCR 不可用，使用 VL 模型")
    return _ocr_image_regions_vl(
        image_bytes, target_lang, mime,
        source_lang=source_lang, glossary=glossary, tm_lookup=tm_lookup,
    )


def _ocr_image_regions_vl(
    image_bytes: bytes,
    target_lang: str,
    mime: str = "image/png",
    source_lang: str = "auto",
    glossary: list[dict] | None = None,
    tm_lookup: Callable | None = None,
) -> list[TextRegion]:
    """使用 VL 模型进行 OCR + 翻译（原有逻辑）。"""
    if _is_too_small(image_bytes, get_settings().ocr_min_image_size):
        return []

    # 第一步：VL 模型识别文字区域
    raw = _call_vl(OCR_REGION_PROMPT, image_bytes, mime)

    if not raw or "[SKIP]" in raw.upper():
        logger.info("图片 OCR VL 返回空或 SKIP: %s", (raw or "")[:100])
        return []

    # 解析 OCR 结果
    ocr_items = _parse_ocr_json(raw)
    if not ocr_items:
        logger.info("图片 OCR 解析到 0 个文字区域")
        return []

    logger.info("图片 OCR 识别到 %d 个文字区域", len(ocr_items))

    # 第二步：批量翻译原文
    texts_to_translate = []
    for item in ocr_items:
        text = item.get("text", "")
        # 判断是否需要翻译（纯数字、公式等不需要）
        if _should_skip_translation(text):
            texts_to_translate.append(None)  # 标记为不需要翻译
        else:
            texts_to_translate.append(text)

    # 批量翻译需要翻译的文本
    target_name = resolve_language_name(target_lang)
    translated_texts = _batch_translate(
        [t for t in texts_to_translate if t is not None],
        target_lang,
        target_name,
        source_lang=source_lang,
        glossary=glossary,
        tm_lookup=tm_lookup,
    )

    # 组装结果
    regions: list[TextRegion] = []
    translate_idx = 0
    for i, item in enumerate(ocr_items):
        bbox = item.get("bbox")
        original = item.get("text", "")
        if not bbox or not original:
            continue

        if texts_to_translate[i] is None:
            # 不需要翻译，译文=原文
            translated = original
        else:
            if translate_idx < len(translated_texts):
                translated = translated_texts[translate_idx]
            else:
                translated = original
            translate_idx += 1

        # 规范化 bbox
        if isinstance(bbox, (list, tuple)) and len(bbox) == 4:
            bbox = [float(v) for v in bbox]
        else:
            continue

        # 确保 bbox 在 0~1 范围内
        bbox = [max(0.0, min(1.0, v)) for v in bbox]

        # 确保 x1 < x2, y1 < y2
        if bbox[0] > bbox[2]:
            bbox[0], bbox[2] = bbox[2], bbox[0]
        if bbox[1] > bbox[3]:
            bbox[1], bbox[3] = bbox[3], bbox[1]

        # 跳过太小的区域（可能是噪声）
        if (bbox[2] - bbox[0]) < 0.01 or (bbox[3] - bbox[1]) < 0.01:
            continue

        regions.append(TextRegion(bbox=bbox, original=original, translated=translated))

    return regions


def _should_skip_translation(text: str) -> bool:
    """向后兼容旧导入路径；实际规则由 text_rules 统一维护。"""
    return should_skip_translation(text)


def _batch_translate(
    texts: list[str],
    target_lang: str,
    target_name: str,
    source_lang: str = "auto",
    glossary: list[dict] | None = None,
    tm_lookup: Callable | None = None,
) -> list[str]:
    """批量翻译文本列表，使用文本翻译模型。"""
    if not texts:
        return []

    from app.services.translator import get_translator

    translator = get_translator()
    results = []
    for text in texts:
        try:
            # 优先查翻译记忆库
            tm_reference = None
            if tm_lookup is not None:
                tm_match = tm_lookup(text, source_lang, target_lang)
                if tm_match and tm_match.get("similarity", 0) >= 0.8:
                    tm_reference = tm_match.get("target_text")
                    # 仅归一化后逐字相等才直接复用；高相似非全等只作参考，
                    # 防止旧记忆的数字/日期差异被原样写入
                    if tm_match.get("exact") and tm_reference:
                        results.append(tm_reference.strip())
                        continue

            translated = translator.translate(
                text, target_lang, source_lang,
                glossary=glossary,
                tm_reference=tm_reference,
            )
            if translated and translated.strip():
                results.append(translated.strip())
            else:
                results.append(text)
        except Exception as exc:
            logger.warning("图片文字翻译失败，保留原文：%s → %s", text[:50], exc)
            results.append(text)

    return results


def _parse_ocr_json(raw: str) -> list[dict]:
    """从 VL 模型响应中解析 OCR 结果（只含 bbox 和 text）。

    健壮性设计：VL 模型返回的 JSON 经常不规范，此函数尝试多种修复策略：
    1. 标准 JSON 解析
    2. 修复缺少 [ 的 bbox
    3. 修复缺少 "text" 字段的条目
    4. 截断修复：找到最后一个完整的 } 并闭合数组
    5. 逐行解析：当整体 JSON 无法修复时，逐行提取 bbox 和 text
    """
    # 去掉 markdown 代码块标记
    cleaned = raw.strip()
    if cleaned.startswith("```"):
        cleaned = re.sub(r'^```(?:json)?\s*', '', cleaned)
        cleaned = re.sub(r'\s*```\s*$', '', cleaned)
        cleaned = cleaned.strip()

    # 提取 JSON 数组
    json_match = re.search(r'\[.*\]', cleaned, re.DOTALL)
    if not json_match:
        # 没有完整数组，尝试逐行解析
        items = _parse_ocr_line_by_line(cleaned)
        if items:
            logger.info("逐行解析 OCR 结果，得到 %d 个区域", len(items))
            return items
        logger.warning("无法从 VL 响应中提取 JSON：%s", raw[:200])
        return []

    json_str = json_match.group()

    # 尝试标准解析
    try:
        items = json.loads(json_str)
        if isinstance(items, list):
            items = _validate_ocr_items(items)
            return items
    except json.JSONDecodeError:
        pass

    # 修复策略 1：修复常见的 bbox 格式问题
    fixed = _fix_ocr_json(json_str)
    try:
        items = json.loads(fixed)
        if isinstance(items, list):
            items = _validate_ocr_items(items)
            if items:
                logger.info("JSON 修复成功，解析到 %d 个区域", len(items))
                return items
    except json.JSONDecodeError:
        pass

    # 修复策略 2：截断修复 — 找到最后一个完整的 } 并闭合数组
    last_brace = fixed.rfind('}')
    if last_brace > 0:
        truncated = fixed[:last_brace + 1] + ']'
        truncated = re.sub(r',\s*\]', ']', truncated)
        try:
            items = json.loads(truncated)
            if isinstance(items, list):
                items = _validate_ocr_items(items)
                if items:
                    logger.info("JSON 截断修复成功，解析到 %d 个区域", len(items))
                    return items
        except json.JSONDecodeError:
            pass

    # 修复策略 3：逐行解析
    items = _parse_ocr_line_by_line(cleaned)
    if items:
        logger.info("逐行解析 OCR 结果（JSON 修复失败后），得到 %d 个区域", len(items))
        return items

    logger.warning("JSON 解析全部失败：%s", raw[:300])
    return []


def _fix_ocr_json(json_str: str) -> str:
    """修复 VL 模型返回的常见 JSON 格式问题。"""
    fixed = json_str

    # 修复1：bbox 缺少左括号 [，如 "bbox": 0.29, 0.23 → "bbox": [0.29, 0.23
    fixed = re.sub(r'"bbox"\s*:\s*(?!\[)([\d.])', r'"bbox": [\1', fixed)

    # 修复2：bbox 缺少右括号 ]，如 "bbox": [0.29, 0.23, → "bbox": [0.29, 0.23],
    # 找到 "bbox": [ 后面跟的数字和逗号，在遇到 "text" 前补 ]
    fixed = re.sub(
        r'"bbox"\s*:\s*\[([\d.,\s]+?)(?=\s*[,}]\s*"?text)',
        r'"bbox": [\1]',
        fixed,
    )

    # 修复3：尾部多余的逗号
    fixed = re.sub(r',\s*([}\]])', r'\1', fixed)

    # 修复4：双重 [[
    fixed = re.sub(r'"bbox"\s*:\s*\[\[', r'"bbox": [', fixed)

    return fixed


def _validate_ocr_items(items: list) -> list[dict]:
    """验证和过滤 OCR 解析结果，确保每个条目都有有效的 bbox 和 text。"""
    valid = []
    for item in items:
        if not isinstance(item, dict):
            continue

        bbox = item.get("bbox")
        text = item.get("text", "")

        # 如果没有 text 字段，跳过
        if not text or not str(text).strip():
            continue

        # 验证 bbox 格式
        if isinstance(bbox, (list, tuple)) and len(bbox) == 4:
            try:
                bbox = [float(v) for v in bbox]
                item["bbox"] = bbox
                valid.append(item)
            except (ValueError, TypeError):
                continue

    return valid


def _parse_ocr_line_by_line(text: str) -> list[dict]:
    """逐行解析 OCR 结果，当整体 JSON 解析失败时的兜底策略。

    支持的格式：
    - {"bbox": [x1, y1, x2, y2], "text": "..."}
    - "bbox": [x1, y1, x2, y2], "text": "..."
    """
    items = []

    # 尝试提取所有包含 bbox 的行
    # 匹配模式：bbox 坐标 + text 内容
    pattern = re.compile(
        r'"bbox"\s*:\s*\[?\s*([\d.]+)\s*,\s*([\d.]+)\s*,\s*([\d.]+)\s*,\s*([\d.]+)\s*\]?'
        r'\s*,?\s*"?text"?\s*:\s*"([^"]*)"',
        re.DOTALL,
    )

    for match in pattern.finditer(text):
        try:
            bbox = [float(match.group(i)) for i in range(1, 5)]
            text_content = match.group(5).strip()
            if text_content:
                items.append({"bbox": bbox, "text": text_content})
        except (ValueError, IndexError):
            continue

    # 如果上面的模式没匹配到，尝试更宽松的模式：先找 bbox，再找最近的 text
    if not items:
        # 找所有 bbox 坐标
        bbox_pattern = re.compile(
            r'"bbox"\s*:\s*\[?\s*([\d.]+)\s*,\s*([\d.]+)\s*,\s*([\d.]+)\s*,\s*([\d.]+)\s*\]?'
        )
        text_pattern = re.compile(r'"text"\s*:\s*"([^"]*)"')

        bbox_matches = list(bbox_pattern.finditer(text))
        text_matches = list(text_pattern.finditer(text))

        for i, bm in enumerate(bbox_matches):
            try:
                bbox = [float(bm.group(j)) for j in range(1, 5)]
                # 找最近的 text（在 bbox 之后）
                text_content = ""
                for tm in text_matches:
                    if tm.start() > bm.start():
                        text_content = tm.group(1).strip()
                        break
                if text_content:
                    items.append({"bbox": bbox, "text": text_content})
            except (ValueError, IndexError):
                continue

    return items


# ---------- 图片文字就地替换 ----------

def process_image_ocr(
    image_bytes: bytes,
    target_lang: str,
    mime: str = "image/png",
    source_lang: str = "auto",
    glossary: list[dict] | None = None,
    tm_lookup: Callable | None = None,
) -> bytes:
    """对图片中的文字进行就地替换：擦除原文区域，写入译文。

    效果类似微信截图翻译：原文被完全覆盖，译文以匹配的字体/颜色/大小写入。
    返回修改后的图片字节。如果无文字或处理失败，返回原始图片字节。

    处理流程：
    1. OCR 识别文字区域
    2. 在原始图片上采样每个区域的文字颜色和背景色
    3. 按从上到下排序，先擦除所有原文区域
    4. 再在擦除后的图片上绘制所有译文
    """
    from PIL import Image, ImageDraw, ImageFont, ImageFilter

    regions = ocr_image_regions(
        image_bytes, target_lang, mime,
        source_lang=source_lang, glossary=glossary, tm_lookup=tm_lookup,
    )
    if not regions:
        logger.info("图片 OCR 无文字区域，返回原图")
        return image_bytes

    logger.info("图片 OCR 识别到 %d 个文字区域", len(regions))

    try:
        img = Image.open(io.BytesIO(image_bytes))
        if img.mode not in ("RGB", "RGBA"):
            img = img.convert("RGB")
        img_w, img_h = img.size

        # 保存原始图片副本用于颜色采样
        original_img = img.copy()

        # 过滤掉无需翻译的区域，准备绘制信息
        draw_regions = []
        for region in regions:
            if region.translated == region.original:
                logger.debug("图片 OCR 跳过无需翻译的区域: %s", region.original[:50])
                continue

            x1 = int(region.bbox[0] * img_w)
            y1 = int(region.bbox[1] * img_h)
            x2 = int(region.bbox[2] * img_w)
            y2 = int(region.bbox[3] * img_h)

            region_w = x2 - x1
            region_h = y2 - y1

            if region_w < 5 or region_h < 5:
                continue

            # 在原始图片上采样颜色（先采背景，供文字颜色判定深底浅字场景）
            bg_color = _sample_bg_inside(original_img, x1, y1, x2, y2)
            text_color = _sample_text_color(original_img, x1, y1, x2, y2, bg_color)

            # 擦除区域：仅小幅扩大（2px），避免误擦周围内容
            pad = 2
            erase_x1 = max(0, x1 - pad)
            erase_y1 = max(0, y1 - pad)
            erase_x2 = min(img_w, x2 + pad)
            erase_y2 = min(img_h, y2 + pad)

            # 基于原文区域高度估算字号（原文高度约为字号的 1.3 倍）
            estimated_font_size = max(8, int(region_h * 0.75))

            draw_regions.append({
                "text": region.translated,
                "text_color": text_color,
                "bg_color": bg_color,
                "erase": (erase_x1, erase_y1, erase_x2, erase_y2),
                "original_bbox": (x1, y1, x2, y2),
                "estimated_font_size": estimated_font_size,
                "y1": y1,
            })

        if not draw_regions:
            logger.info("图片 OCR 所有区域无需翻译，返回原图")
            return image_bytes

        # 按从上到下排序
        draw_regions.sort(key=lambda r: r["y1"])

        # 第一步：擦除所有原文区域
        draw = ImageDraw.Draw(img)
        for dr in draw_regions:
            ex1, ey1, ex2, ey2 = dr["erase"]
            draw.rectangle([ex1, ey1, ex2, ey2], fill=dr["bg_color"])

        # 第二步：绘制所有译文
        for dr in draw_regions:
            ex1, ey1, ex2, ey2 = dr["erase"]
            draw_w = ex2 - ex1
            draw_h = ey2 - ey1

            # 自适应字号：从估算字号开始，逐步缩小直到文字能放入区域
            font_size = _fit_font_size(draw, dr["text"], None, draw_w, draw_h)
            # 如果自适应字号远大于估算字号，使用估算字号（避免文字过大）
            if font_size > dr["estimated_font_size"] * 1.2:
                font_size = dr["estimated_font_size"]
            if font_size < 8:
                font_size = 8

            try:
                use_font = _get_font(img_w, font_size)
            except Exception:
                use_font = None

            _draw_text_in_region(draw, dr["text"], use_font, ex1, ey1, ex2, ey2, dr["text_color"])

        # 保存为原格式
        out = io.BytesIO()
        save_format = "PNG"
        if mime and "jpeg" in mime:
            save_format = "JPEG"
        elif mime and "webp" in mime:
            save_format = "WEBP"
        # JPEG 不支持透明通道：RGBA/P/LA 图片直接 save('JPEG') 会抛
        # "cannot write mode RGBA as JPEG" 并被外层 except 吞掉 → 返回未翻译原图。
        # 保存前把带 alpha 的图合成到白底再转 RGB。
        if save_format == "JPEG" and img.mode not in ("RGB", "L"):
            if img.mode in ("RGBA", "LA") or (img.mode == "P" and "transparency" in img.info):
                rgba = img.convert("RGBA")
                background = Image.new("RGB", rgba.size, (255, 255, 255))
                background.paste(rgba, mask=rgba.split()[-1])
                img = background
            else:
                img = img.convert("RGB")
        img.save(out, format=save_format, quality=95)
        return out.getvalue()

    except Exception as exc:
        logger.warning("图片文字就地替换失败，返回原图：%s", exc)
        return image_bytes


def _sample_text_color(img, x1: int, y1: int, x2: int, y2: int, bg_color: tuple | None = None) -> tuple:
    """采样文字区域中的文字颜色。

    旧实现只找"深色像素"（brightness<180），深底白字场景采不到样，兜底返回黑色
    → 与深背景同色、译文不可见。改为：以背景亮度为基准，深底取最亮像素（浅色文字），
    浅底取最暗像素（深色文字）；无有效前景像素时用背景反色兜底，保证可读对比。
    """
    w, h = img.size
    # 在 bbox 中心区域采样
    cx1 = x1 + (x2 - x1) // 4
    cy1 = y1 + (y2 - y1) // 4
    cx2 = x1 + 3 * (x2 - x1) // 4
    cy2 = y1 + 3 * (y2 - y1) // 4

    def _lum(p):
        return p[0] * 0.299 + p[1] * 0.587 + p[2] * 0.114

    # 背景亮度：优先用传入的 bg_color，否则默认按浅底处理
    bg_lum = _lum(bg_color) if bg_color else 255.0
    dark_bg = bg_lum < 128  # 深色背景 → 文字应为浅色

    pixels = []
    step = max(1, (cx2 - cx1) // 10)
    for x in range(cx1, min(cx2, w), step):
        for y in range(cy1, min(cy2, h), step):
            try:
                pixel = img.getpixel((x, y))
                if isinstance(pixel, int):
                    pixel = (pixel, pixel, pixel)
                if len(pixel) >= 3:
                    pixels.append(pixel[:3])
            except IndexError:
                pass

    if not pixels:
        # 无采样：用背景反色兜底
        return (255, 255, 255) if dark_bg else (0, 0, 0)

    # 前景像素 = 与背景亮度差异最大的一侧
    pixels.sort(key=_lum)
    if dark_bg:
        # 深底：取最亮像素作为文字色
        candidate = pixels[-1]
        # 若最亮像素仍偏暗（采样全是背景），用白色兜底保证可见
        if _lum(candidate) - bg_lum < 40:
            return (255, 255, 255)
        return candidate
    else:
        # 浅底：取最暗像素作为文字色
        candidate = pixels[0]
        if bg_lum - _lum(candidate) < 40:
            return (0, 0, 0)
        return candidate


def _sample_bg_inside(img, x1: int, y1: int, x2: int, y2: int) -> tuple:
    """采样文字区域内部的背景色（在边缘采样，避免采样到文字本身）。"""
    w, h = img.size
    samples = []
    margin = max(2, int((y2 - y1) * 0.1))

    # 在 bbox 内部四条边的内侧采样
    for x in range(x1, min(x2, w), max(1, (x2 - x1) // 8)):
        # 上边内侧
        for y in range(y1, min(y1 + margin, h)):
            try:
                samples.append(img.getpixel((x, y)))
            except IndexError:
                pass
        # 下边内侧
        for y in range(max(0, y2 - margin), min(y2, h)):
            try:
                samples.append(img.getpixel((x, y)))
            except IndexError:
                pass

    for y in range(y1, min(y2, h), max(1, (y2 - y1) // 8)):
        # 左边内侧
        for x in range(x1, min(x1 + margin, w)):
            try:
                samples.append(img.getpixel((x, y)))
            except IndexError:
                pass
        # 右边内侧
        for x in range(max(0, x2 - margin), min(x2, w)):
            try:
                samples.append(img.getpixel((x, y)))
            except IndexError:
                pass

    if not samples:
        return (255, 255, 255)

    # 过滤掉太暗的像素（可能是文字），取亮色中位数作为背景
    bright_samples = []
    for s in samples:
        if isinstance(s, int):
            s = (s, s, s)
        if len(s) >= 3:
            brightness = s[0] * 0.299 + s[1] * 0.587 + s[2] * 0.114
            if brightness > 100:  # 排除文字像素
                bright_samples.append(s[:3])

    if not bright_samples:
        # 如果所有像素都偏暗，取最亮的
        if isinstance(samples[0], int):
            return (255, 255, 255)
        bright_samples = [s[:3] for s in samples if len(s) >= 3]
        if not bright_samples:
            return (255, 255, 255)
        bright_samples.sort(key=lambda p: p[0] * 0.299 + p[1] * 0.587 + p[2] * 0.114, reverse=True)
        return bright_samples[0]

    r = sorted(s[0] for s in bright_samples)[len(bright_samples) // 2]
    g = sorted(s[1] for s in bright_samples)[len(bright_samples) // 2]
    b = sorted(s[2] for s in bright_samples)[len(bright_samples) // 2]
    return (r, g, b)


def _sample_background(img, x1: int, y1: int, x2: int, y2: int) -> tuple:
    """采样文字区域周围的背景色。"""
    from PIL import Image

    w, h = img.size
    # 在文字区域外围采样像素
    samples = []
    sample_margin = max(3, int((x2 - x1) * 0.1))

    # 上方采样
    for x in range(x1, x2, max(1, (x2 - x1) // 5)):
        sy = max(0, y1 - sample_margin)
        if sy < h and x < w:
            try:
                samples.append(img.getpixel((x, sy)))
            except IndexError:
                pass

    # 下方采样
    for x in range(x1, x2, max(1, (x2 - x1) // 5)):
        sy = min(h - 1, y2 + sample_margin)
        if sy >= 0 and x < w:
            try:
                samples.append(img.getpixel((x, sy)))
            except IndexError:
                pass

    # 左侧采样
    for y in range(y1, y2, max(1, (y2 - y1) // 5)):
        sx = max(0, x1 - sample_margin)
        if sx < w and y < h:
            try:
                samples.append(img.getpixel((sx, y)))
            except IndexError:
                pass

    # 右侧采样
    for y in range(y1, y2, max(1, (y2 - y1) // 5)):
        sx = min(w - 1, x2 + sample_margin)
        if sx >= 0 and y < h:
            try:
                samples.append(img.getpixel((sx, y)))
            except IndexError:
                pass

    if not samples:
        return (255, 255, 255)

    # 取中位数作为背景色
    if isinstance(samples[0], int):
        return (255, 255, 255)

    r = sorted(s[0] for s in samples if len(s) >= 3)[len(samples) // 2]
    g = sorted(s[1] for s in samples if len(s) >= 3)[len(samples) // 2]
    b = sorted(s[2] for s in samples if len(s) >= 3)[len(samples) // 2]
    return (r, g, b)


def _get_font(img_width: int, size: int = 0, bold: bool = False) -> ImageFont.FreeTypeFont:
    """获取支持 CJK 的字体。"""
    from PIL import ImageFont

    if size < 1:
        size = max(12, img_width // 40)

    # 尝试常见 CJK 字体路径（Linux Docker 容器中）
    font_paths = [
        "/usr/share/fonts/truetype/noto/NotoSansCJK-Regular.ttc",
        "/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc",
        "/usr/share/fonts/noto-cjk/NotoSansCJK-Regular.ttc",
        "/usr/share/fonts/truetype/wqy/wqy-zenhei.ttc",
        "/usr/share/fonts/truetype/wqy/wqy-microhei.ttc",
        "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
    ]
    # 粗体字体路径
    bold_paths = [
        "/usr/share/fonts/truetype/noto/NotoSansCJK-Bold.ttc",
        "/usr/share/fonts/opentype/noto/NotoSansCJK-Bold.ttc",
        "/usr/share/fonts/noto-cjk/NotoSansCJK-Bold.ttc",
        "/usr/share/fonts/truetype/wqy/wqy-zenhei.ttc",  # 文泉驿没有单独粗体
        "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
    ]

    paths = bold_paths if bold else font_paths

    for path in paths:
        try:
            return ImageFont.truetype(path, size)
        except (OSError, IOError):
            continue

    # fallback：尝试普通字体
    if bold:
        for path in font_paths:
            try:
                return ImageFont.truetype(path, size)
            except (OSError, IOError):
                continue

    # fallback：Pillow 默认字体
    try:
        return ImageFont.truetype("DejaVuSans.ttf", size)
    except (OSError, IOError):
        return ImageFont.load_default()


def _fit_font_size(draw, text: str, base_font, max_w: int, max_h: int) -> int:
    """自适应字号：找到能放入区域的最大字号。

    支持多行文字：如果单行放不下，尝试换行后放入。
    返回字号大小（像素）。
    """
    from PIL import ImageFont

    # 从 base_font 的当前大小开始尝试，如果为 None 则根据区域高度估算
    if base_font is not None:
        try:
            current_size = base_font.size
        except AttributeError:
            current_size = 16
    else:
        # 根据区域高度估算初始字号（文字高度约为字号的 1.2 倍）
        current_size = max(10, int(max_h * 0.8))

    # 先试当前大小
    try:
        use_font = base_font if base_font else _get_font(0, current_size)
        bbox = draw.textbbox((0, 0), text, font=use_font)
        tw = bbox[2] - bbox[0]
        th = bbox[3] - bbox[1]
        if tw <= max_w and th <= max_h:
            return current_size
    except Exception:
        pass

    # 逐步缩小（更细粒度：每步减 2px）
    size = current_size
    while size >= 8:
        try:
            font = _get_font(0, size)
            bbox = draw.textbbox((0, 0), text, font=font)
            tw = bbox[2] - bbox[0]
            th = bbox[3] - bbox[1]
            if tw <= max_w and th <= max_h:
                return size
        except Exception:
            pass
        size -= 2

    # 如果单行放不下，尝试多行
    # 找到能通过换行放入的最小字号（考虑行间距 1.2 倍）
    for size in range(max(8, current_size // 2), 7, -2):
        try:
            font = _get_font(0, size)
            lines = _wrap_text(text, font, max_w - 4)
            total_h = 0
            for line in lines:
                bbox = draw.textbbox((0, 0), line, font=font)
                total_h += int((bbox[3] - bbox[1]) * 1.2)  # 行间距 1.2 倍
            if total_h <= max_h:
                return size
        except Exception:
            continue

    return 8  # 最小字号


def _wrap_text(text: str, font, max_w: int) -> list[str]:
    """将文本按最大宽度换行。"""
    lines = []
    current_line = ""
    for char in text:
        test_line = current_line + char
        try:
            bbox = font.getbbox(test_line)
            tw = bbox[2] - bbox[0]
        except Exception:
            tw = len(test_line) * 10
        if tw > max_w and current_line:
            lines.append(current_line)
            current_line = char
        else:
            current_line = test_line
    if current_line:
        lines.append(current_line)
    return lines if lines else [text]


def _draw_text_in_region(draw, text: str, font, x1: int, y1: int, x2: int, y2: int, color: tuple) -> None:
    """在指定区域内绘制文本，左对齐+垂直居中，支持多行。

    改进点：
    - 使用 anchor="lt"（左上角）定位，避免 CJK 字体基线偏移问题
    - 左对齐更符合文档排版习惯
    - 多行文本行间距合理控制
    - 垂直居中确保文字在区域内不偏移
    """
    region_w = x2 - x1
    region_h = y2 - y1

    if not font:
        draw.text((x1, y1), text, fill=color)
        return

    # 使用 anchor="lt"（left-top）来精确定位，避免基线偏移
    # 计算单行文本尺寸
    try:
        # 使用 getbbox 获取字体度量（更准确）
        font_bbox = font.getbbox("Ayjg")  # 混合字符获取平均度量
        ascent = -font_bbox[1]  # 基线以上的高度
        descent = font_bbox[3] - 0  # 基线以下的高度（近似）
        line_height = font_bbox[3] - font_bbox[1]
    except Exception:
        ascent = font.size if hasattr(font, 'size') else 16
        descent = 4
        line_height = ascent + descent

    # 计算文本宽度
    try:
        text_bbox = draw.textbbox((0, 0), text, font=font)
        tw = text_bbox[2] - text_bbox[0]
    except Exception:
        tw = len(text) * (font.size if hasattr(font, 'size') else 16) // 2

    # 左侧留 2px 间距
    margin = 2

    if tw <= region_w - margin * 2:
        # 单行：左对齐+垂直居中
        tx = x1 + margin
        # 垂直居中：基于行高计算
        ty = y1 + max(0, (region_h - line_height) // 2)
        draw.text((tx, ty), text, fill=color, font=font)
    else:
        # 多行绘制
        lines = _wrap_text(text, font, region_w - margin * 2)
        line_spacing = int(line_height * 0.3)  # 行间距为行高的 30%
        total_h = len(lines) * line_height + (len(lines) - 1) * line_spacing

        # 垂直居中起始位置
        cy = y1 + max(0, (region_h - total_h) // 2)

        for line in lines:
            lx = x1 + margin
            draw.text((lx, cy), line, fill=color, font=font)
            cy += line_height + line_spacing
