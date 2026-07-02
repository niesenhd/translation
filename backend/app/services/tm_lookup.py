"""翻译记忆库查询服务：根据语种方向查找匹配的翻译记忆。"""
from __future__ import annotations

from sqlalchemy import select

from app.core.database import SessionLocal
from app.models.translation_memory import TranslationMemory


def lookup_tm(source_text: str, source_lang: str, target_lang: str, threshold: float = 0.8) -> dict | None:
    """查找翻译记忆库中的匹配项。

    使用简单的字符级相似度匹配。threshold 为匹配阈值（0~1），默认 0.8。
    返回格式：{"source_text": ..., "target_text": ..., "similarity": ...} 或 None
    """
    lang_code_map = {
        "zh": "zh", "zh-Hans": "zh", "zh-CN": "zh",
        "zh-Hant": "zh", "zh-TW": "zh", "zh-HK": "zh",
        "en": "en", "fr": "fr", "es": "es", "ru": "ru",
        "ar": "ar", "ja": "ja", "ko": "ko", "de": "de",
        "pt": "pt", "it": "it",
    }
    src = lang_code_map.get(source_lang, source_lang.split("-")[0])
    tgt = lang_code_map.get(target_lang, target_lang.split("-")[0])

    db = SessionLocal()
    try:
        if src == "auto":
            # 源语言未知（上传默认 auto）：按目标语匹配全部语对。
            # 相似度阈值本身足以排除跨源语言的误匹配（不同语言的文本
            # bigram 重合度极低），否则精确等值查询永远查不到 auto→xx。
            condition = TranslationMemory.lang_pair.like(f"%→{tgt}")
        else:
            condition = TranslationMemory.lang_pair == f"{src}→{tgt}"
        entries = list(db.scalars(
            select(TranslationMemory).where(condition)
        ))
    finally:
        db.close()

    if not entries:
        return None

    # 计算字符级相似度
    best_match = None
    best_sim = 0.0
    source_normalized = source_text.strip().lower()

    for entry in entries:
        entry_normalized = entry.source_text.strip().lower()
        sim = _char_similarity(source_normalized, entry_normalized)
        if sim > best_sim:
            best_sim = sim
            best_match = entry

    if best_match and best_sim >= threshold:
        return {
            "source_text": best_match.source_text,
            "target_text": best_match.target_text,
            "similarity": round(best_sim, 3),
            # exact：归一化后逐字相等。只有 exact 才允许直接复用译文——
            # bigram Dice 对长段落不敏感，仅改动一个日期/金额相似度仍可
            # >0.95，法律文书直接套用旧译文会引入事实错误。
            "exact": source_normalized == best_match.source_text.strip().lower(),
        }
    return None


def _char_similarity(a: str, b: str) -> float:
    """计算两个字符串的字符级相似度（Dice 系数）。"""
    if not a or not b:
        return 0.0
    if a == b:
        return 1.0

    # 使用 bigram
    set_a = set(a[i:i+2] for i in range(len(a) - 1))
    set_b = set(b[i:i+2] for i in range(len(b) - 1))

    if not set_a and not set_b:
        return 1.0
    if not set_a or not set_b:
        return 0.0

    intersection = len(set_a & set_b)
    return 2.0 * intersection / (len(set_a) + len(set_b))
