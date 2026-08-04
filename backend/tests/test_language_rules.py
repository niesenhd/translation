from app.core.languages import (
    SUPPORTED_LANGUAGES,
    SUPPORTED_SOURCE_LANGUAGES,
    language_pair_code,
    resolve_language_name,
)
from app.services.text_rules import should_skip_translation


def test_public_language_allowlists_are_exact() -> None:
    assert SUPPORTED_LANGUAGES == {"zh", "en", "fr", "es", "ru", "ar", "ja", "ko", "zh-Hant"}
    assert SUPPORTED_SOURCE_LANGUAGES == SUPPORTED_LANGUAGES | {"auto"}


def test_language_mapping_is_shared_for_tm_glossary_and_prompts() -> None:
    assert language_pair_code("zh-Hant") == "zh"
    assert language_pair_code("zh-CN") == "zh"
    assert resolve_language_name("zh-Hant") == "Traditional Chinese"


def test_skip_translation_rules_cover_structured_values_but_not_words() -> None:
    assert should_skip_translation("2026-08-04") is True
    assert should_skip_translation("https://translation.example") is True
    assert should_skip_translation("123,456.00%") is True
    assert should_skip_translation("Agreement") is False
    assert should_skip_translation("§", short_symbols=True) is True
