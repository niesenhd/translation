"""模型适配器抽象 + DashScope（Qwen）OpenAI 兼容实现。

未来接入新模型时，只需新增一个 Adapter 类并在 `get_translator()` 中分发。
"""
from __future__ import annotations

from abc import ABC, abstractmethod
import logging
import re
import threading

from openai import OpenAI
from openai import APIError, APITimeoutError, BadRequestError, RateLimitError
import time

from app.core.config import get_settings

logger = logging.getLogger(__name__)

SYSTEM_PROMPT = (
    "You are a senior legal translator for a law firm, specializing in Chinese-English legal document translation.\n"
    "Translate the user's text into {target_lang}.\n\n"
    "STYLE REQUIREMENTS:\n"
    "- Use formal, precise legal register (e.g., 'shall' for obligations, 'may' for permissions).\n"
    "- Preserve the original meaning faithfully; do not add interpretations or omissions.\n"
    "- Keep sentence structure close to the source where target-language grammar allows.\n"
    "- Maintain consistent terminology throughout the document.\n\n"
    "PRESERVE EXACTLY (do not translate or alter):\n"
    "- Formatting markers, line breaks, indentation, and inline placeholders like {{...}}.\n"
    "- Article/clause numbers, cross-references, defined terms in quotation marks.\n"
    "- Code, formulas, URLs, email addresses.\n"
    "- Proper nouns (names of people, entities, statutes) that should remain in original form.\n\n"
    "OUTPUT: Only the translated text. No explanations, no notes.{glossary_section}"
)

GLOSSARY_SECTION = (
    "\n\nMANDATORY GLOSSARY — apply consistently:\n"
    "The following terms appear in the source text and MUST be translated as specified:\n"
    "{glossary}\n"
    "- [STRICT] terms: you MUST use the exact specified translation, no variants.\n"
    "- [PREFERRED] terms: use the specified translation unless context clearly demands otherwise.\n"
    "Apply the same glossary translation every time the term recurs in the document."
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


# ── 术语匹配 ────────────────────────────────────────────────────────
# 替换原来的朴素子串匹配，解决两类问题：
# 1. 误配：英文短术语 "act" 会命中 "actual"；中文 "法" 命中几乎所有句子
# 2. 漏配：无法识别词边界、无法去重（同一术语多条命中）

# 中文术语的最小长度：单字术语歧义太大，强制要求 >= 2 字
_ZH_MIN_LEN = 2
# 英文术语的最小长度：1-2 字母的术语误配率高，强制要求 >= 3 字符
_EN_MIN_LEN = 3
# 单段最多注入的术语条数，防止 prompt 膨胀
_MAX_GLOSSARY_HITS = 40


def _match_glossary(text: str, glossary: list[dict]) -> list[dict]:
    """从术语库中筛选出在 text 中实际出现的术语。

    匹配策略：
    - 中文术语：直接子串匹配（中文无空格分词），但要求长度 >= 2
    - 英文术语：用正则词边界 \\b 匹配，避免 act 命中 actual
    - 最长优先：若短术语是某长术语的子串且两者都命中，保留长术语
    - 上限截断：超过 _MAX_GLOSSARY_HITS 条时，strict 优先、再按长度降序
    """
    hits: list[dict] = []
    hit_terms: set[str] = set()  # 已命中的 source_term（去重）

    for term in glossary:
        src = term.get("source_term")
        if not src:
            continue
        src_stripped = src.strip()
        if not src_stripped:
            continue

        is_ascii = src_stripped.isascii()

        # 长度过滤：短术语误配率高
        if is_ascii:
            if len(src_stripped) < _EN_MIN_LEN:
                continue
        else:
            if len(src_stripped) < _ZH_MIN_LEN:
                continue

        matched = False
        if is_ascii:
            # 英文：词边界匹配，大小写不敏感
            # 转义正则特殊字符，防止 source_term 含 ( ) . 等导致误解析
            pattern = r"\b" + re.escape(src_stripped) + r"\b"
            if re.search(pattern, text, re.IGNORECASE):
                matched = True
        else:
            # 中文：直接子串匹配
            if src_stripped in text:
                matched = True

        if matched and src_stripped not in hit_terms:
            hits.append(term)
            hit_terms.add(src_stripped)

    if not hits:
        return []

    # 最长优先去重：若 A 是 B 的子串且两者都命中，移除 A（保留更具体的 B）
    # 例如 "违约" 和 "根本违约" 都命中时，只保留 "根本违约"
    # 但 strict 级别的短术语保留（用户明确要求强制）
    filtered: list[dict] = []
    for term in hits:
        src = term["source_term"].strip()
        is_strict = term.get("priority") == "strict"
        # 检查是否存在另一个更长的命中术语包含此术语
        is_shadowed = False
        if not is_strict:
            for other in hits:
                if other is term:
                    continue
                other_src = other["source_term"].strip()
                if len(other_src) > len(src) and src in other_src:
                    is_shadowed = True
                    break
        if not is_shadowed:
            filtered.append(term)

    # 上限截断：strict 优先，其次按术语长度降序（更具体的先保留）
    if len(filtered) > _MAX_GLOSSARY_HITS:
        filtered.sort(
            key=lambda t: (
                0 if t.get("priority") == "strict" else 1,
                -len(t["source_term"]),
            )
        )
        filtered = filtered[:_MAX_GLOSSARY_HITS]

    return filtered


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
            relevant = _match_glossary(text, glossary)
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
        budget_override: int | None = None  # 译文被截断时临时放大 max_tokens 重试一次
        # 重试 6 次，覆盖瞬时网络错误 + 限流。429 时退避更激进。
        for attempt in range(6):
            try:
                # 显式 max_tokens：不设则走 DashScope 偏小的默认值，长段落译文被
                # 中途截断（NVCA 实测多段砍到半句）。按输入长度给足预算，封顶 8192。
                # token 估算：CJK 约 1 字符=1 token，拉丁约 4 字符=1 token，
                # 取 len/2 是对混合文本的稳妥上估；×3 留出译文(≤2x 原文)余量。
                est_tokens = max(256, len(text) // 2)
                max_tokens = budget_override or min(8192, max(2048, est_tokens * 3))
                completion = self._client.chat.completions.create(
                    model=self._model,
                    messages=[
                        {"role": "system", "content": SYSTEM_PROMPT.format(target_lang=target_name, glossary_section=glossary_section) + tm_section},
                        {"role": "user", "content": text},
                    ],
                    temperature=0.2,
                    max_tokens=max_tokens,
                    # 关闭思维链：翻译是直接生成任务，无需 reasoning，
                    # 关闭后单次调用的 latency 与输出 token 显著减少。
                    extra_body={"enable_thinking": False},
                )
                choice = completion.choices[0]
                content = (choice.message.content or "").strip()
                finish = getattr(choice, "finish_reason", None)

                # 截断检测：finish_reason=length 表示输出被 max_tokens 砍断。
                # 首次截断 → 放大到 8192 重试一次；仍截断(极长单段) → 返回部分译文并告警
                # （部分 > 无，由上层 _translate_text 兜底；非空故不会误判为漏翻）
                if finish == "length" and budget_override is None:
                    budget_override = 8192
                    time.sleep(0.3)
                    continue
                if finish == "length":
                    logger.warning("译文在 max_tokens=8192 下仍被截断(段过长)，返回部分译文：%.60s", text)

                # 空译文：偶发(模型返回空 content)。重试，仍空则交给上层兜底保留原文
                if not content:
                    last_err = RuntimeError("模型返回空译文")
                    time.sleep(min(1 + 2 * attempt, 30))
                    continue

                return content
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
