"""Structured framework extraction (enrich-v3): LLM transport and parsing.

Phase 10. For versions whose enrichment hit ``label_transferable_framework``,
extract the analysis framework the author actually stated — input variables,
logic chain, conclusion shape, and the author's own applicability/invalidation
conditions — into an independent derived table. Mirrors :mod:`kol_archive.enrich`
for transport; like every enrichment it never writes back into evidence text and
grounds itself in a verbatim snippet of the original.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from typing import Any, cast

import httpx

from kol_archive.models import FrameworkExtractionResult

SYSTEM_PROMPT = """你在协助用户从一条 KOL 发言原文中提取作者明确表达的分析框架（可迁移的判断方法），
用于建立可复用的框架库。只依据输入原文明确表达的内容，不得补造变量、条件、逻辑或结论。

输出 JSON 对象，且仅含以下字段：
- framework_found：布尔。原文是否真的给出了一个可迁移的分析框架（输入变量 + 推理逻辑），
  而非单点结论或情绪表态。拿不准时取 false。
- topic：字符串，框架所属主题的简短中文标签（如 估值/行业周期/仓位管理/财报分析/宏观流动性），
  便于聚合浏览；framework_found 为 false 时返回空字符串。
- summary：字符串，用一两句中文概括该框架做什么判断；false 时返回空字符串。
- input_variables：字符串数组，框架依赖的输入变量或观察指标，逐个列出（如 "存货周转天数"、
  "毛利率变化"）；只列原文出现过的，false 时返回空数组。
- logic_chain：字符串，原文表达的推理链条（从输入变量到结论的逻辑步骤），用中文转述但
  不得添加原文没有的环节；false 时返回空字符串。
- conclusion_shape：字符串，框架输出的结论形态（如 买卖区间判断/风险预警信号/排除筛选条件），
  false 时返回空字符串。
- applicability_conditions：字符串，作者**自己声明**的适用范围或前提；原文未声明则返回空字符串，
  不得替作者推断。
- invalidation_conditions：字符串，作者**自己声明**的失效或不适用条件；原文未声明则返回空字符串。
- evidence_snippet：字符串，必须是原文中逐字摘录的一小段，作为框架确实被表达过的依据；
  framework_found 为 true 时必填，false 时返回空字符串。

不要美化原文，不要把单次结论包装成方法论。"""


@dataclass(frozen=True)
class FrameworkSettings:
    base_url: str
    model: str
    api_key: str = field(repr=False)
    prompt_version: str = "framework-v1"


def load_framework_settings(config: dict[str, Any]) -> FrameworkSettings:
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
    model = str(llm.get("model") or "").strip()
    if not model:
        raise ValueError("llm.model must be configured")
    return FrameworkSettings(
        base_url=str(llm.get("base_url") or "https://api.openai.com/v1").rstrip("/"),
        model=model,
        api_key=api_key,
        prompt_version=str(llm.get("framework_prompt_version") or "framework-v1").strip(),
    )


def _strip_whitespace(value: str) -> str:
    return "".join(value.split())


def _required_field(parsed: dict[str, Any], key: str) -> str:
    value = str(parsed.get(key) or "").strip()
    if not value:
        raise ValueError(f"framework extraction must include a non-empty {key}")
    return value


def parse_framework_result(
    parsed: dict[str, Any], original_text: str
) -> FrameworkExtractionResult | None:
    """Validate one parsed LLM payload; ``None`` means no framework was stated."""
    found = parsed.get("framework_found")
    if not isinstance(found, bool):
        raise ValueError("framework_found must be a JSON boolean")
    if not found:
        return None
    variables_raw = parsed.get("input_variables")
    if not isinstance(variables_raw, list) or not variables_raw:
        raise ValueError("an extracted framework requires a non-empty input_variables array")
    variables = tuple(
        dict.fromkeys(str(item).strip() for item in variables_raw if str(item).strip())
    )
    if not variables:
        raise ValueError("input_variables must contain non-empty variable names")
    # The snippet is surfaced as the framework's 依据片段 — a hallucinated quote
    # would become durable false evidence, so enforce verbatim membership with
    # the project's whitespace-stripped comparison convention.
    evidence_snippet = _required_field(parsed, "evidence_snippet")
    if _strip_whitespace(evidence_snippet) not in _strip_whitespace(original_text):
        raise ValueError("LLM evidence_snippet is not a verbatim excerpt of the original text")
    return FrameworkExtractionResult(
        topic=_required_field(parsed, "topic"),
        summary=_required_field(parsed, "summary"),
        input_variables=variables,
        logic_chain=_required_field(parsed, "logic_chain"),
        conclusion_shape=_required_field(parsed, "conclusion_shape"),
        applicability_conditions=str(parsed.get("applicability_conditions") or "").strip(),
        invalidation_conditions=str(parsed.get("invalidation_conditions") or "").strip(),
        evidence_snippet=evidence_snippet,
    )


def request_framework_extraction(
    settings: FrameworkSettings,
    original_text: str,
    *,
    client: httpx.Client | None = None,
) -> FrameworkExtractionResult | None:
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
        try:
            content = cast(dict[str, Any], payload)["choices"][0]["message"]["content"]
        except (KeyError, IndexError, TypeError) as error:
            raise ValueError("LLM response is missing choices[0].message.content") from error
        try:
            parsed = json.loads(str(content))
        except json.JSONDecodeError as error:
            raise ValueError("LLM response content is not valid JSON") from error
        if not isinstance(parsed, dict):
            raise ValueError("LLM response content must be a JSON object")
        return parse_framework_result(cast(dict[str, Any], parsed), original_text)
    finally:
        if owned_client:
            active_client.close()
