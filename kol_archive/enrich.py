"""Batch LLM enrichment: post_type + three labels per observed version.

Phase 3. Mirrors :mod:`kol_archive.rewrite` for the LLM transport and parsing,
but produces structured labels instead of a rewrite suggestion. Enrichment is a
batch, idempotent operation keyed by ``UNIQUE(version_id, prompt_version)`` — it
never fabricates and grounds every verdict in a verbatim snippet of the original
text, per the charter's evidence-first ("照妖镜") principle.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from typing import Any, cast

import httpx

from kol_archive.models import EnrichmentResult

SYSTEM_PROMPT = """你在协助用户给一条 KOL 发言原文打结构化标签，用于过滤注意力，不替用户下判断。
只依据输入原文明确表达的内容，不得补造标的、事实、信息来源或观点。

输出 JSON 对象，且仅含以下字段：
- post_type：字符串，原文体裁的简短中文分类（如 观点/数据/研究/转述/情绪/答疑/公告/其他）。
- label_first_hand_info：布尔。原文是否给出第一手信息（亲历调研、原始数据、一手凭证），
  而非转述或泛泛而谈。
- label_transferable_framework：布尔。原文是否给出可迁移的分析框架或方法论，而非单点结论。
- label_reasoned_non_consensus：布尔。原文是否提出有论证支撑的非共识观点，而非随大流或无依据断言。
- rationale：字符串，简述每个标签为真/为假的依据，只引用原文已表达的内容。
- stance_summary：字符串，用一句简短中文概括作者明确表达的主张或立场；若原文没有明确立场，
  返回空字符串。不得把标签裁定理由当成立场，不得补造方向、标的或结论。
- evidence_snippet：字符串，必须是原文中逐字摘录的一小段，作为上述判断的依据；
  原文为空或无可摘录内容时返回空字符串。

输出必须是严格合法的 JSON。字符串中的 ASCII 双引号必须使用反斜杠转义。
若逐字证据片段含有容易破坏 JSON 的引号，可摘录更短且仍能在原文逐字找到的片段。
标签拿不准时取 false。不要美化、不要替原文补全缺失的信息。"""

RETRY_PROMPT = """上次输出未通过严格校验。请重新输出完整 JSON 对象。
确保 JSON 可由标准解析器直接解析，字符串内的 ASCII 双引号正确转义，
且 evidence_snippet 去除空白后仍能在原文中逐字找到。只输出 JSON。"""


@dataclass(frozen=True)
class EnrichSettings:
    base_url: str
    model: str
    api_key: str = field(repr=False)
    prompt_version: str = "enrich-v2"


def _required_text(mapping: dict[str, Any], key: str) -> str:
    value = str(mapping.get(key) or "").strip()
    if not value:
        raise ValueError(f"llm.{key} must be configured")
    return value


def load_enrich_settings(config: dict[str, Any]) -> EnrichSettings:
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
    return EnrichSettings(
        base_url=str(llm.get("base_url") or "https://api.openai.com/v1").rstrip("/"),
        model=_required_text(llm, "model"),
        api_key=api_key,
        prompt_version=str(llm.get("enrich_prompt_version") or "enrich-v2").strip(),
    )


def _required_bool(mapping: dict[str, Any], key: str) -> bool:
    if key not in mapping:
        raise ValueError(f"LLM response is missing boolean field: {key}")
    value = mapping[key]
    if isinstance(value, bool):
        return value
    raise ValueError(f"LLM response field must be a JSON boolean: {key}")


_QUOTE_EQUIVALENTS = str.maketrans(
    {
        "“": '"',
        "”": '"',
        "＂": '"',
        "‘": "'",
        "’": "'",
        "＇": "'",
    }
)


def _normalized_excerpt(value: str) -> tuple[str, list[int]]:
    characters: list[str] = []
    positions: list[int] = []
    for position, character in enumerate(value):
        if character.isspace():
            continue
        characters.append(character.translate(_QUOTE_EQUIVALENTS))
        positions.append(position)
    return "".join(characters), positions


def _original_excerpt(candidate: str, original_text: str) -> str | None:
    needle, _ = _normalized_excerpt(candidate)
    haystack, positions = _normalized_excerpt(original_text)
    if not needle:
        return ""
    start = haystack.find(needle)
    if start < 0:
        return None
    end = start + len(needle) - 1
    return original_text[positions[start] : positions[end] + 1].strip()


def _diagnostic_excerpt(value: object, limit: int = 320) -> str:
    return " ".join(str(value).split())[:limit]


def _json_content(value: object) -> str:
    content = str(value).strip()
    if content.startswith("```") and content.endswith("```"):
        lines = content.splitlines()
        if len(lines) >= 3:
            content = "\n".join(lines[1:-1]).strip()
    return content


def _parse_result(payload: dict[str, Any], original_text: str) -> EnrichmentResult:
    try:
        content = payload["choices"][0]["message"]["content"]
    except (KeyError, IndexError, TypeError) as error:
        raise ValueError("LLM response is missing choices[0].message.content") from error
    json_content = _json_content(content)
    try:
        parsed = json.loads(json_content)
    except json.JSONDecodeError as error:
        excerpt = _diagnostic_excerpt(content)
        raise ValueError(
            f"LLM response content is not valid JSON; response excerpt: {excerpt}"
        ) from error
    if not isinstance(parsed, dict):
        raise ValueError("LLM response content must be a JSON object")
    post_type = str(parsed.get("post_type") or "").strip()
    rationale = str(parsed.get("rationale") or "").strip()
    if not post_type:
        raise ValueError("LLM response must include a non-empty post_type")
    if not rationale:
        raise ValueError("LLM response must include a non-empty rationale")
    # evidence_snippet may be empty when the original text carries nothing
    # quotable; the model is told to return "" rather than invent one. When
    # non-empty it must be a verbatim quote — we persist and surface it as the
    # "依据片段", so a hallucinated snippet would become durable false evidence.
    # Compare with whitespace removed (the project's "去空白后" convention) to
    # tolerate reflowing without admitting fabricated content.
    returned_snippet = str(parsed.get("evidence_snippet") or "").strip()
    evidence_snippet = _original_excerpt(returned_snippet, original_text)
    stance_summary = str(parsed.get("stance_summary") or "").strip()
    if evidence_snippet is None:
        excerpt = _diagnostic_excerpt(returned_snippet)
        raise ValueError(
            "LLM evidence_snippet is not a verbatim excerpt of the original text; "
            f"returned snippet: {excerpt}"
        )
    first_hand = _required_bool(parsed, "label_first_hand_info")
    transferable = _required_bool(parsed, "label_transferable_framework")
    non_consensus = _required_bool(parsed, "label_reasoned_non_consensus")
    # A label that gates a post into the filter stream must carry its evidence.
    # An empty snippet is only acceptable when no label fired (nothing to back up).
    if (first_hand or transferable or non_consensus) and not evidence_snippet:
        raise ValueError("a true label requires a non-empty evidence_snippet")
    return EnrichmentResult(
        post_type=post_type,
        label_first_hand_info=first_hand,
        label_transferable_framework=transferable,
        label_reasoned_non_consensus=non_consensus,
        rationale=rationale,
        evidence_snippet=evidence_snippet,
        stance_summary=stance_summary,
    )


def request_enrichment(
    settings: EnrichSettings,
    original_text: str,
    *,
    client: httpx.Client | None = None,
) -> EnrichmentResult:
    owned_client = client is None
    active_client = client or httpx.Client(timeout=30.0)
    try:
        messages = [
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": original_text},
        ]
        try:
            return _request_and_parse(settings, original_text, messages, active_client)
        except ValueError:
            messages.append({"role": "user", "content": RETRY_PROMPT})
            return _request_and_parse(settings, original_text, messages, active_client)
    finally:
        if owned_client:
            active_client.close()


def _request_and_parse(
    settings: EnrichSettings,
    original_text: str,
    messages: list[dict[str, str]],
    client: httpx.Client,
) -> EnrichmentResult:
    response = client.post(
        f"{settings.base_url}/chat/completions",
        headers={"Authorization": f"Bearer {settings.api_key}"},
        json={
            "model": settings.model,
            "messages": messages,
            "response_format": {"type": "json_object"},
        },
    )
    response.raise_for_status()
    payload = response.json()
    if not isinstance(payload, dict):
        raise ValueError("LLM response body must be a JSON object")
    return _parse_result(cast(dict[str, Any], payload), original_text)
