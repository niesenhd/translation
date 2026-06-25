"""模型适配器抽象 + DashScope（Qwen）OpenAI 兼容实现。

未来接入新模型时，只需新增一个 Adapter 类并在 `get_translator()` 中分发。
"""
from __future__ import annotations

from abc import ABC, abstractmethod
import threading

from openai import OpenAI
from openai import APIError, APITimeoutError, BadRequestError, RateLimitError
import time

from app.core.config import get_settings

SYSTEM_PROMPT = (
    "You are a professional legal document translator. "
    "Translate the user's text into {target_lang}. "
    "Strictly preserve original formatting markers, line breaks, indentation, and inline placeholders. "
    "Do NOT add explanations. Do NOT translate code, formulas, URLs, email addresses, or proper nouns that should remain in the original form. "
    "Only output the translated text."
    "{glossary_section}"
)

GLOSSARY_SECTION = (
    "\n\nIMPORTANT GLOSSARY (must follow):\n"
    "The following terms must be translated exactly as specified:\n"
    "{glossary}\n"
    "For terms marked [STRICT], you MUST use the specified translation. "
    "For terms marked [PREFERRED], you SHOULD use the specified translation when appropriate."
)


class ContentRejectedError(RuntimeError):
    """内容被 API 拒绝（400 Bad Request），非系统性错误，不应计入熔断。

    常见原因：prompt 过长、内容触发安全过滤策略。
    上层应保留原文兜底，不阻塞整体翻译任务。
    """


# 语言代码（前端发送的 BCP-47 / 自定义代码） → Prompt 中使用的明确语言名
# 关键约束：
# 1. 必须用英文语言名，模型识别度最高、最稳定
# 2. 中文必须区分简体（Simplified）和繁体（Traditional），否则模型可能默认输出简体
LANGUAGE_NAMES: dict[str, str] = {
    "zh": "Simplified Chinese",
    "zh-Hans": "Simplified Chinese",
    "zh-CN": "Simplified Chinese",
    "zh-Hant": "Traditional Chinese",
    "zh-TW": "Traditional Chinese",
    "zh-HK": "Traditional Chinese (Hong Kong)",
    "en": "English",
    "fr": "French",
    "es": "Spanish",
    "ru": "Russian",
    "ar": "Arabic",
    "ja": "Japanese",
    "ko": "Korean",
    "de": "German",
    "pt": "Portuguese",
    "it": "Italian",
}


def resolve_language_name(code: str) -> str:
    """把语言代码解析为模型友好的语言名。未知代码原样返回。"""
    if not code:
        return "English"
    return LANGUAGE_NAMES.get(code, LANGUAGE_NAMES.get(code.split("-")[0], code))


class Translator(ABC):
    @abstractmethod
    def translate(self, text: str, target_lang: str, source_lang: str = "auto") -> str: ...


class DashScopeTranslator(Translator):
    """阿里云 DashScope（Qwen）OpenAI 兼容端点。

    优先从 model_configs 表读取激活的翻译模型配置，
    其次从 system_config 读取，
    最后使用环境变量默认值。
    """

    def __init__(self) -> None:
        settings = get_settings()

        # 1. 优先从 model_configs 表读取激活配置
        from app.api.model_configs import get_active_translation_config
        active = get_active_translation_config()

        if active:
            api_key = active.api_key
            base_url = active.api_base_url
            model = active.model_id
        else:
            # 2. 其次从 system_config 读取
            from app.api.admin import _get_config_value, KEY_TRANSLATION_MODEL, KEY_API_BASE_URL, KEY_API_KEY
            api_key = _get_config_value(KEY_API_KEY, settings.dashscope_api_key)
            base_url = _get_config_value(KEY_API_BASE_URL, settings.dashscope_base_url)
            model = _get_config_value(KEY_TRANSLATION_MODEL, settings.dashscope_model)

        if not api_key:
            raise RuntimeError("未配置翻译模型 API Key")
        self._client = OpenAI(
            api_key=api_key,
            base_url=base_url,
            timeout=120.0,
            # 禁用 SDK 内置重试：SDK 默认退避太短（0.5-1s），
            # 与应用层 6 次重试叠加后会在限流期间轰炸 API（429 雪崩）。
            # 由应用层的指数退避全权控制重试节奏。
            max_retries=0,
        )
        self._model = model

    def translate(self, text: str, target_lang: str, source_lang: str = "auto", glossary: list[dict] | None = None, tm_reference: str | None = None) -> str:
        if not text.strip():
            return text

        # 预检：纯数字/符号/空白的内容不需要翻译，直接返回避免 API 400
        stripped = text.strip()
        if not any(c.isalpha() for c in stripped):
            return text

        target_name = resolve_language_name(target_lang)

        # 构建术语库提示
        glossary_section = ""
        if glossary:
            # 按当前段落内容过滤：只传入实际出现的术语
            # 避免术语库过大（数万条）撑爆 API token 上限
            text_lower = text.lower()
            relevant = [
                t for t in glossary
                if t.get("source_term") and t["source_term"].lower() in text_lower
            ]
            if relevant:
                lines = []
                for term in relevant:
                    priority_tag = "[STRICT]" if term.get("priority") == "strict" else "[PREFERRED]"
                    lines.append(f"- {priority_tag} \"{term['source_term']}\" → \"{term['target_term']}\"")
                glossary_section = GLOSSARY_SECTION.format(glossary="\n".join(lines))

        # 构建 TM 参考提示
        tm_section = ""
        if tm_reference:
            tm_section = f"\n\nREFERENCE TRANSLATION (similar text, for reference only, adjust as needed):\n\"{tm_reference}\""

        last_err: Exception | None = None
        # 重试 6 次，覆盖瞬时网络错误 + 限流。429 时退避更激进。
        for attempt in range(6):
            try:
                completion = self._client.chat.completions.create(
                    model=self._model,
                    messages=[
                        {"role": "system", "content": SYSTEM_PROMPT.format(target_lang=target_name, glossary_section=glossary_section) + tm_section},
                        {"role": "user", "content": text},
                    ],
                    temperature=0.2,
                    # 关闭思维链：翻译是直接生成任务，无需 reasoning，
                    # 关闭后单次调用的 latency 与输出 token 显著减少。
                    extra_body={"enable_thinking": False},
                )
                return completion.choices[0].message.content or ""
            except BadRequestError as exc:
                # 400 Bad Request：重试也无用（多半是 prompt 过长 / 内容触发安全过滤）。
                # 立即抛出 ContentRejectedError，由上层把该段保留原文，不阻塞整体任务，也不计入熔断。
                raise ContentRejectedError(f"DashScope 拒绝请求 (400): {exc}") from exc
            except RateLimitError as exc:
                # 限流：指数退避 5/10/20/40/60/60s
                # DashScope 限流恢复需要时间，退避太短会导致 429 雪崩
                last_err = exc
                wait = min(5 * (2 ** attempt), 60)
                time.sleep(wait)
            except (APITimeoutError, APIError) as exc:
                last_err = exc
                # 普通退避：1s, 3s, 7s, 15s, 31s, 60s
                time.sleep(min(1 + 2 * attempt, 60))
        # 全部重试失败
        raise RuntimeError(f"调用 DashScope 失败：{last_err}") from last_err


_translator: Translator | None = None
_translator_lock = threading.Lock()


def get_translator() -> Translator:
    global _translator
    if _translator is None:
        with _translator_lock:
            if _translator is None:  # double-check
                _translator = DashScopeTranslator()
    return _translator


def reset_translator() -> None:
    """重置翻译器单例，使新配置生效。"""
    global _translator
    _translator = None
