"""术语库查询服务：根据语种方向加载术语，供翻译流程使用。"""
from __future__ import annotations

from sqlalchemy import select

from app.core.database import SessionLocal
from app.models.term import TermEntry


def _normalize_lang(code: str) -> str:
    """把语言代码归一化到短代码（zh / en / fr ...）。"""
    lang_code_map = {
        "zh": "zh", "zh-Hans": "zh", "zh-CN": "zh",
        "zh-Hant": "zh", "zh-TW": "zh", "zh-HK": "zh",
        "en": "en", "fr": "fr", "es": "es", "ru": "ru",
        "ar": "ar", "ja": "ja", "ko": "ko", "de": "de",
        "pt": "pt", "it": "it",
    }
    if not code:
        return ""
    return lang_code_map.get(code, code.split("-")[0])


def get_glossary_for_lang_pair(source_lang: str, target_lang: str) -> list[dict]:
    """根据源语言和目标语言加载匹配的术语。

    返回格式：[{"source_term": ..., "target_term": ..., "priority": ...}, ...]

    只按真实方向正向加载，**不做反向匹配**。早期实现会把反方向（如 zh→en）的整本
    词典 source/target 互换后当作本方向（en→zh）术语注入——这会塞进数万条词典式
    反向释义（如某 zh→en 条目反向成 "transaction→和息"、"terms→开庭期"），系统性
    把模型带偏，是译文质量劣化的主因（律师反馈"翻译水平低"的根因）。术语库本质是
    方向性的，跨方向复用应靠人工按方向录入，而非自动反向。
    """
    src = _normalize_lang(source_lang)
    tgt = _normalize_lang(target_lang)

    db = SessionLocal()
    try:
        if src and src != "auto":
            # 已知源语言：仅加载精确方向（如 en→zh）
            entries = list(db.scalars(
                select(TermEntry).where(TermEntry.lang_pair == f"{src}→{tgt}")
            ))
        else:
            # auto 源语言：源文语种未知，但译文语种确定，加载所有以 →{tgt} 结尾的
            # 正向条目（源语种不限，但方向正确：源文 → 目标译文语种）
            entries = list(db.scalars(
                select(TermEntry).where(TermEntry.lang_pair.like(f"%→{tgt}"))
            ))
        return [
            {
                "source_term": e.source_term,
                "target_term": e.target_term,
                "priority": e.priority.value,
            }
            for e in entries
        ]
    finally:
        db.close()
