"""LLM query expansion for retrospective topic recall (主题回溯 扩词).

Phase 11, step 2. The deterministic retrieval in :mod:`kol_archive.recall` needs
*grouped* keywords and a time window. Typing those by hand is the friction point:
a user thinks "美伊冲突那阵子大家怎么看油价", not "event=美伊,伊朗,霍尔木兹 /
market=油价,原油,布油 / 2025-06-10..2025-06-30". This module asks the model to
turn the natural-language question into editable term groups plus a suggested
Beijing-local window.

It is deliberately the *only* token-spending step here, and it is the lightest
possible one: the model proposes search aids, never an answer. It must not judge
the topic, must not fabricate facts, and its output is always handed back to the
user to confirm or edit before the deterministic (zero-token, zero-hallucination)
retrieval runs. So a wrong synonym or a bad date costs nothing but an edit — it
can never become false evidence.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
from typing import Any, cast

import httpx

from kol_archive.obs import http_client

# Match recall.py: a bare question about "那几周" means Beijing calendar days, so
# the "today" anchor we give the model — and any date it returns — are read in +08:00.
LOCAL_TZ_OFFSET_HOURS = 8

SYSTEM_PROMPT = """你在协助用户把一个关于市场主题的中文问题，拆解成用于**检索历史发言**的\
分组关键词，并建议一个回溯时间窗。你只产出检索辅助，绝不回答问题本身、不下判断、不补造事实或观点。

输出 JSON 对象，且仅含以下字段：
- groups：数组，每个元素是 {"label": 字符串, "terms": 字符串数组}。每组代表一个检索维度，\
  组内的词是同义/近义扩展（检索时组内 OR），不同组代表必须同时命中的不同维度（检索时组间 AND）。\
  典型是把「事件主体」与「市场标的」分成两组，如 \
  {"label":"event","terms":["美伊","伊朗","霍尔木兹"]} 与 \
  {"label":"market","terms":["油价","原油","布油","WTI"]}。\
  label 用简短英文小写；terms 用中文为主的检索词，包含常见别称/缩写，但不得脱离问题臆造无关词。
- date_from / date_to：建议的回溯起止日期，格式严格为 YYYY-MM-DD，按北京时间理解。\
  若问题指向某个可定位的事件时段就给出合理窗口；若无法判断时段则返回空字符串 ""。\
  不要给未来日期，不要把窗口开得过宽到失去意义。
- tickers：数组，仅当问题明确指向具体 A 股标的时给出代码（如 SH601857），否则返回空数组 []。
- notes：一句简短中文说明你如何拆分维度与为何选这个时间窗，便于用户判断是否需要修改。

要求：至少给出一个含 terms 的 group。分组拿不准时，宁可只给一个宽一点的 event 组，\
也不要塞入与问题无关的词。输出必须是严格合法的 JSON，字符串内的 ASCII 双引号需转义。只输出 JSON。"""

RETRY_PROMPT = """上次输出未通过校验。请重新输出完整 JSON 对象：\
groups 至少一组且每组 terms 非空；date_from/date_to 为 YYYY-MM-DD 或空字符串；\
tickers 为字符串数组；只输出可被标准解析器直接解析的 JSON。"""

# A defensive cap: an expansion is a search aid, not a corpus. A model that returns
# hundreds of "synonyms" would only produce an unusable form and a noisy LIKE scan.
MAX_GROUPS = 8
MAX_TERMS_PER_GROUP = 24
MAX_TICKERS = 16


@dataclass(frozen=True)
class ExpandSettings:
    base_url: str
    model: str
    api_key: str = field(repr=False)
    prompt_version: str = "expand-v1"


@dataclass(frozen=True)
class ExpandedGroup:
    """One suggested OR-set of keywords, mirroring recall.TermGroup."""

    label: str
    terms: tuple[str, ...]


@dataclass(frozen=True)
class ExpandedQuery:
    """Editable retrieval suggestion: grouped terms, a window, tickers, a note.

    Every field is a *suggestion* the user reviews before the deterministic recall
    runs; nothing here is persisted or treated as evidence.
    """

    groups: tuple[ExpandedGroup, ...]
    date_from: str
    date_to: str
    tickers: tuple[str, ...]
    notes: str

    def to_payload(self) -> dict[str, object]:
        return {
            "groups": [{"label": g.label, "terms": list(g.terms)} for g in self.groups],
            "date_from": self.date_from,
            "date_to": self.date_to,
            "tickers": list(self.tickers),
            "notes": self.notes,
        }


def _required_text(mapping: dict[str, Any], key: str) -> str:
    value = str(mapping.get(key) or "").strip()
    if not value:
        raise ValueError(f"llm.{key} must be configured")
    return value


def load_expand_settings(config: dict[str, Any]) -> ExpandSettings:
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
    return ExpandSettings(
        base_url=str(llm.get("base_url") or "https://api.openai.com/v1").rstrip("/"),
        model=_required_text(llm, "model"),
        api_key=api_key,
        prompt_version=str(llm.get("expand_prompt_version") or "expand-v1").strip() or "expand-v1",
    )


def _local_tz() -> timezone:
    return timezone(timedelta(hours=LOCAL_TZ_OFFSET_HOURS))


def beijing_today() -> str:
    return datetime.now(tz=_local_tz()).date().isoformat()


def _coerce_date(value: object, *, not_after: date | None) -> str:
    """Accept a bare YYYY-MM-DD or drop it to "" — never fail expansion on a date.

    The window is a hint the user edits anyway, so a malformed or future date is
    silently discarded rather than rejected: better an empty window box than a
    failed expansion. ``not_after`` clamps out a hallucinated future bound.
    """
    text = str(value or "").strip()
    if not text:
        return ""
    try:
        parsed = date.fromisoformat(text)
    except ValueError:
        return ""
    if not_after is not None and parsed > not_after:
        return ""
    return parsed.isoformat()


def _clean_terms(value: object) -> tuple[str, ...]:
    if not isinstance(value, list):
        return ()
    seen: list[str] = []
    for item in value:
        term = str(item or "").strip()
        if term and term not in seen:
            seen.append(term)
        if len(seen) >= MAX_TERMS_PER_GROUP:
            break
    return tuple(seen)


def _clean_groups(value: object) -> tuple[ExpandedGroup, ...]:
    if not isinstance(value, list):
        raise ValueError("LLM expansion 'groups' must be a JSON array")
    groups: list[ExpandedGroup] = []
    for index, item in enumerate(value):
        if not isinstance(item, dict):
            continue
        terms = _clean_terms(item.get("terms"))
        if not terms:
            continue
        label = str(item.get("label") or "").strip() or f"group{index + 1}"
        groups.append(ExpandedGroup(label=label, terms=terms))
        if len(groups) >= MAX_GROUPS:
            break
    if not groups:
        raise ValueError("LLM expansion produced no usable keyword group")
    return tuple(groups)


def _clean_tickers(value: object) -> tuple[str, ...]:
    if not isinstance(value, list):
        return ()
    seen: list[str] = []
    for item in value:
        ticker = str(item or "").strip().upper()
        if ticker and ticker not in seen:
            seen.append(ticker)
        if len(seen) >= MAX_TICKERS:
            break
    return tuple(seen)


def _json_content(value: object) -> str:
    content = str(value).strip()
    if content.startswith("```") and content.endswith("```"):
        lines = content.splitlines()
        if len(lines) >= 3:
            content = "\n".join(lines[1:-1]).strip()
    return content


def _parse_expansion(payload: dict[str, Any], *, today: str) -> ExpandedQuery:
    try:
        content = payload["choices"][0]["message"]["content"]
    except (KeyError, IndexError, TypeError) as error:
        raise ValueError("LLM response is missing choices[0].message.content") from error
    try:
        parsed = json.loads(_json_content(content))
    except json.JSONDecodeError as error:
        raise ValueError("LLM expansion content is not valid JSON") from error
    if not isinstance(parsed, dict):
        raise ValueError("LLM expansion content must be a JSON object")
    not_after = date.fromisoformat(today)
    return ExpandedQuery(
        groups=_clean_groups(parsed.get("groups")),
        date_from=_coerce_date(parsed.get("date_from"), not_after=not_after),
        date_to=_coerce_date(parsed.get("date_to"), not_after=not_after),
        tickers=_clean_tickers(parsed.get("tickers")),
        notes=str(parsed.get("notes") or "").strip(),
    )


def expand_query(
    settings: ExpandSettings,
    question: str,
    *,
    today: str | None = None,
    client: httpx.Client | None = None,
) -> ExpandedQuery:
    """Suggest editable keyword groups + a window for a natural-language question.

    ``today`` anchors the model's date reasoning (Beijing local); it defaults to
    the current Beijing day. Retries once on a parse failure, mirroring enrichment.
    """
    text = question.strip()
    if not text:
        raise ValueError("question must not be empty")
    anchor = today or beijing_today()
    owned_client = client is None
    active_client = client or http_client(timeout=30.0)
    try:
        messages = [
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": f"今天是 {anchor}（北京时间）。问题：{text}"},
        ]
        try:
            return _request_and_parse(settings, messages, active_client, today=anchor)
        except ValueError:
            messages.append({"role": "user", "content": RETRY_PROMPT})
            return _request_and_parse(settings, messages, active_client, today=anchor)
    finally:
        if owned_client:
            active_client.close()


def _request_and_parse(
    settings: ExpandSettings,
    messages: list[dict[str, str]],
    client: httpx.Client,
    *,
    today: str,
) -> ExpandedQuery:
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
    return _parse_expansion(cast(dict[str, Any], payload), today=today)
