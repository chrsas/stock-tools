"""Manual single-version LLM rewrite requests for training exercises."""

from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from typing import Any, cast

import httpx

SYSTEM_PROMPT = """你在协助用户做可证伪命题改写训练。
只处理输入原文明确表达的内容。不得补造标的、方向、时间范围、目标价或事实。
输出 JSON 对象，且仅含 rewritten_claim 和 rationale 两个字符串字段。
rewritten_claim 应保留原文不确定性。原文缺少可证伪命题时，明确写出无法形成有效命题。
rationale 简述保留了哪些原文边界，以及哪些信息因原文缺失而留空。"""


@dataclass(frozen=True)
class RewriteSettings:
    base_url: str
    model: str
    api_key: str = field(repr=False)
    prompt_version: str = "v1"


@dataclass(frozen=True)
class RewriteSuggestion:
    rewritten_claim: str
    rationale: str


def _required_text(mapping: dict[str, Any], key: str) -> str:
    value = str(mapping.get(key) or "").strip()
    if not value:
        raise ValueError(f"llm.{key} must be configured")
    return value


def load_rewrite_settings(config: dict[str, Any]) -> RewriteSettings:
    llm = config.get("llm") or {}
    if not isinstance(llm, dict):
        raise ValueError("llm must be a mapping")
    provider = str(llm.get("provider") or "openai_compatible").strip()
    if provider != "openai_compatible":
        raise ValueError("llm.provider must be openai_compatible")
    api_key_env = str(llm.get("api_key_env") or "LLM_API_KEY").strip()
    api_key = os.environ.get(api_key_env)
    if not api_key:
        raise ValueError(f"LLM API key environment variable is missing: {api_key_env}")
    return RewriteSettings(
        base_url=str(llm.get("base_url") or "https://api.openai.com/v1").rstrip("/"),
        model=_required_text(llm, "model"),
        api_key=api_key,
        prompt_version=str(llm.get("prompt_version") or "v1").strip(),
    )


def _parse_suggestion(payload: dict[str, Any]) -> RewriteSuggestion:
    try:
        content = payload["choices"][0]["message"]["content"]
    except (KeyError, IndexError, TypeError) as error:
        raise ValueError("LLM response is missing choices[0].message.content") from error
    try:
        parsed = json.loads(str(content))
    except json.JSONDecodeError as error:
        raise ValueError("LLM response content is not valid JSON") from error
    if not isinstance(parsed, dict):
        raise ValueError("LLM response content must be a JSON object")
    rewritten_claim = str(parsed.get("rewritten_claim") or "").strip()
    rationale = str(parsed.get("rationale") or "").strip()
    if not rewritten_claim or not rationale:
        raise ValueError("LLM response must include rewritten_claim and rationale")
    return RewriteSuggestion(rewritten_claim=rewritten_claim, rationale=rationale)


def request_rewrite(
    settings: RewriteSettings,
    original_text: str,
    *,
    client: httpx.Client | None = None,
) -> RewriteSuggestion:
    owned_client = client is None
    active_client = client or httpx.Client(timeout=30.0)
    try:
        response = active_client.post(
            f"{settings.base_url}/chat/completions",
            headers={"Authorization": f"Bearer {settings.api_key}"},
            json={
                "model": settings.model,
                "messages": [
                    {"role": "system", "content": SYSTEM_PROMPT},
                    {"role": "user", "content": original_text},
                ],
                "response_format": {"type": "json_object"},
            },
        )
        response.raise_for_status()
        payload = response.json()
        if not isinstance(payload, dict):
            raise ValueError("LLM response body must be a JSON object")
        return _parse_suggestion(cast(dict[str, Any], payload))
    finally:
        if owned_client:
            active_client.close()
