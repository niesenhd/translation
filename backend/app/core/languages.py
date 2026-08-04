"""系统唯一的语言代码、语对归一化和展示名映射。"""
from __future__ import annotations

SUPPORTED_LANGUAGES = frozenset({"zh", "en", "fr", "es", "ru", "ar", "ja", "ko", "zh-Hant"})
SUPPORTED_SOURCE_LANGUAGES = SUPPORTED_LANGUAGES | {"auto"}

_PAIR_CODE_ALIASES = {
    "zh": "zh",
    "zh-hans": "zh",
    "zh-cn": "zh",
    "zh-hant": "zh",
    "zh-tw": "zh",
    "zh-hk": "zh",
    "en": "en",
    "fr": "fr",
    "es": "es",
    "ru": "ru",
    "ar": "ar",
    "ja": "ja",
    "ko": "ko",
    "auto": "auto",
}

LANGUAGE_NAMES = {
    "zh": "Simplified Chinese",
    "zh-hans": "Simplified Chinese",
    "zh-cn": "Simplified Chinese",
    "zh-hant": "Traditional Chinese",
    "zh-tw": "Traditional Chinese",
    "zh-hk": "Traditional Chinese (Hong Kong)",
    "en": "English",
    "fr": "French",
    "es": "Spanish",
    "ru": "Russian",
    "ar": "Arabic",
    "ja": "Japanese",
    "ko": "Korean",
}


def language_pair_code(code: str) -> str:
    """把 BCP-47/前端代码归一为术语库和 TM 使用的短代码。"""
    cleaned = (code or "").strip()
    lowered = cleaned.lower()
    return _PAIR_CODE_ALIASES.get(lowered, lowered.split("-", 1)[0])


def resolve_language_name(code: str) -> str:
    """把语言代码解析为模型提示词使用的明确英文名称。"""
    cleaned = (code or "").strip()
    if not cleaned:
        return "English"
    lowered = cleaned.lower()
    return LANGUAGE_NAMES.get(lowered, LANGUAGE_NAMES.get(lowered.split("-", 1)[0], cleaned))
