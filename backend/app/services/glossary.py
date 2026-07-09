"""术语库查询服务：根据语种方向加载术语，供翻译流程使用。"""
from __future__ import annotations

from sqlalchemy import or_, select

from app.core.database import SessionLocal
from app.models.term import TermEntry

# domain="法学" 的是 6.8 万条《英汉法律词典》原始导出（单字头词、词典式
# 单一义项、跨领域噪声）。模型裸翻质量优于这类词典释义，注入只会带偏
# （如 court→议会、execute→履行、qualified→附条件的）。故词典退出翻译注入，
# 仅作管理后台检索参考。参与注入的是带具体领域标签的精选术语（合同/诉讼/等）。
_DICTIONARY_DOMAIN = "法学"


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


def _row(e: TermEntry, swap: bool = False) -> dict:
    """构造注入用条目；swap=True 时把反向语对的 source/target 互换成本方向。"""
    return {
        "source_term": e.target_term if swap else e.source_term,
        "target_term": e.source_term if swap else e.target_term,
        "priority": e.priority.value,
    }


def get_glossary_for_lang_pair(source_lang: str, target_lang: str) -> list[dict]:
    """根据源/目标语言加载精选术语（domain≠法学）。

    返回格式：[{"source_term": ..., "target_term": ..., "priority": ...}, ...]

    方案要点（2026-07-09，需求 line 257 要求双向匹配 + 质量治理）：
    1. **词典退出注入**：domain="法学" 的 6.8 万条词典导出不参与注入（模型裸翻更优）。
       保留带具体领域标签的精选术语（合同/诉讼/知识产权 等，约数百条）。
    2. **精选术语双向匹配**：满足需求"仅录一个方向也能两个方向命中"。对精选术语
       同时加载正向（src→tgt）与反向（tgt→src，source/target 互换）。
       因只作用于精选小集，不会重蹈"整本词典反向=垃圾"的覆辙。
    """
    src = _normalize_lang(source_lang)
    tgt = _normalize_lang(target_lang)
    not_dict = or_(TermEntry.domain.is_(None), TermEntry.domain != _DICTIONARY_DOMAIN)

    db = SessionLocal()
    try:
        if src and src != "auto":
            fwd = list(db.scalars(
                select(TermEntry).where(
                    TermEntry.lang_pair == f"{src}→{tgt}", not_dict,
                )
            ))
            rev = list(db.scalars(
                select(TermEntry).where(
                    TermEntry.lang_pair == f"{tgt}→{src}", not_dict,
                )
            ))
            return [_row(e) for e in fwd] + [_row(e, swap=True) for e in rev]

        # auto 源语言：源文语种未知，译文语种确定。
        # 正向：所有以 →{tgt} 结尾的精选条目（源文语种不限、方向正确）。
        # 反向：所有以 {tgt}→ 开头的精选条目互换 source/target（覆盖源文恰为 tgt 的情形）。
        fwd = list(db.scalars(
            select(TermEntry).where(
                TermEntry.lang_pair.like(f"%→{tgt}"), not_dict,
            )
        ))
        rev = list(db.scalars(
            select(TermEntry).where(
                TermEntry.lang_pair.like(f"{tgt}→%"), not_dict,
            )
        ))
        return [_row(e) for e in fwd] + [_row(e, swap=True) for e in rev]
    finally:
        db.close()
