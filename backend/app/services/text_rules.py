"""所有文档/OCR 路径共用的“无需翻译”判断。"""
from __future__ import annotations

import re

_NUMBER_RE = re.compile(r"^[\d\s,.%$€¥£₹+\-]+$")
_DATE_RE = re.compile(r"^\d{4}[-/]\d{1,2}[-/]\d{1,2}$")
_TIME_RE = re.compile(r"^\d{1,2}:\d{2}(:\d{2})?$")
_EMAIL_RE = re.compile(r"^[\w.+-]+@[\w-]+\.[\w.]+$")
_URL_RE = re.compile(r"^https?://", re.IGNORECASE)
_VERSION_RE = re.compile(r"^v?\d+\.\d+", re.IGNORECASE)
_CJK_RE = re.compile(r"[\u4e00-\u9fff\u3040-\u309f\u30a0-\u30ff\uac00-\ud7af]")


def should_skip_translation(text: object, *, short_symbols: bool = False) -> bool:
    if not isinstance(text, str):
        return True
    cleaned = text.strip()
    if not cleaned:
        return True
    if any(pattern.match(cleaned) for pattern in (_NUMBER_RE, _DATE_RE, _TIME_RE, _EMAIL_RE, _URL_RE, _VERSION_RE)):
        return True
    return short_symbols and len(cleaned) <= 2 and _CJK_RE.search(cleaned) is None
