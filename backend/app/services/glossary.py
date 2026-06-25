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

    当 source_lang="auto" 时：
    - 正向匹配：加载所有以 `→{tgt}` 结尾的条目（如 zh→en、fr→en 都会命中）
    - 反向匹配：加载所有以 `{tgt}→` 开头的条目，source/target 互换
    """
    src = _normalize_lang(source_lang)
    tgt = _normalize_lang(target_lang)

    db = SessionLocal()
    try:
        if src == "auto" or not src:
            # 自动检测源语言：无法精确匹配方向，加载所有目标语种相关的条目
            forward_pattern = f"%→{tgt}"
            reverse_pattern = f"{tgt}→%"

            # 正向条目（lang_pair 以 →{tgt} 结尾）
            forward_entries = list(db.scalars(
                select(TermEntry).where(TermEntry.lang_pair.like(forward_pattern))
            ))
            # 反向条目（lang_pair 以 {tgt}→ 开头），source/target 互换
            reverse_entries = list(db.scalars(
                select(TermEntry).where(TermEntry.lang_pair.like(reverse_pattern))
            ))

            result = [
                {
                    "source_term": e.source_term,
                    "target_term": e.target_term,
                    "priority": e.priority.value,
                }
                for e in forward_entries
            ]
            result.extend([
                {
                    "source_term": e.target_term,
                    "target_term": e.source_term,
                    "priority": e.priority.value,
                }
                for e in reverse_entries
            ])
            return result

        # 已知源语言：精确方向匹配 + 反向匹配 + 通配
        lang_pair = f"{src}→{tgt}"
        reverse_pair = f"{tgt}→{src}"

        # 1. 正向匹配
        entries = list(db.scalars(
            select(TermEntry).where(TermEntry.lang_pair == lang_pair)
        ))
        # 2. 反向匹配：把反向 lang_pair 的 source/target 互换后加入
        reverse_entries = list(db.scalars(
            select(TermEntry).where(TermEntry.lang_pair == reverse_pair)
        ))
        # 3. 也加载无语种方向限制的术语（lang_pair 为空或包含通配符）
        wildcard_entries = list(db.scalars(
            select(TermEntry).where(
                TermEntry.lang_pair.like(f"%→{tgt}%"),
                TermEntry.lang_pair != lang_pair,
                TermEntry.lang_pair != reverse_pair,
            )
        ))

        result = [
            {
                "source_term": e.source_term,
                "target_term": e.target_term,
                "priority": e.priority.value,
            }
            for e in entries + wildcard_entries
        ]
        # 反向条目互换 source/target
        result.extend([
            {
                "source_term": e.target_term,
                "target_term": e.source_term,
                "priority": e.priority.value,
            }
            for e in reverse_entries
        ])
        return result
    finally:
        db.close()
