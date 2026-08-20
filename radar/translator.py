from __future__ import annotations

import json
import os
import urllib.error
import urllib.request


DASHSCOPE_CHAT_URL = "https://dashscope.aliyuncs.com/compatible-mode/v1/chat/completions"


class TranslationError(RuntimeError):
    pass


def translate_to_chinese(text: str) -> tuple[str, str]:
    api_key = os.environ.get("DASHSCOPE_API_KEY", "").strip()
    if not api_key:
        raise TranslationError("DASHSCOPE_API_KEY is not configured")

    source = text.strip()
    if not source:
        raise TranslationError("translation text is empty")
    source = source[:4000]
    model = os.environ.get("TRANSLATION_MODEL", "deepseek-v4-flash-0731").strip() or "deepseek-v4-flash-0731"
    endpoint = os.environ.get("DASHSCOPE_CHAT_URL", DASHSCOPE_CHAT_URL).strip() or DASHSCOPE_CHAT_URL
    payload = {
        "model": model,
        "messages": [
            {
                "role": "system",
                "content": (
                    "你是专业的 AI 技术资讯翻译。将用户提供的英文准确翻译成简体中文，"
                    "保留模型名、产品名、代码、URL 和专业缩写。只输出中文译文，不要解释。"
                ),
            },
            {"role": "user", "content": source},
        ],
        "temperature": 0.1,
        "stream": False,
        "enable_thinking": False,
        "max_tokens": 1200,
    }
    request = urllib.request.Request(
        endpoint,
        data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
        headers={
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
            "Accept": "application/json",
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=45) as response:
            result = json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        raise TranslationError(f"DashScope API returned HTTP {exc.code}") from exc
    except (urllib.error.URLError, TimeoutError, json.JSONDecodeError) as exc:
        raise TranslationError(f"DashScope API request failed: {exc.__class__.__name__}") from exc

    choices = result.get("choices") or []
    translated = ""
    if choices and isinstance(choices[0], dict):
        message = choices[0].get("message") or {}
        translated = str(message.get("content") or "").strip()
    if not translated:
        translated = str(result.get("result") or "").strip()
    if not translated:
        raise TranslationError("DashScope API returned an empty translation")
    return translated, model
