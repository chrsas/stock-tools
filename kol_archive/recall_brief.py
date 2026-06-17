"""LLM synthesis of a retrospective topic brief (主题回溯 简报合成).

Phase 11, step 3. Steps 1–2 gave a deterministic, citation-anchored retrieval
(:mod:`kol_archive.recall`) and an editable query expansion
(:mod:`kol_archive.recall_expand`). This module is the *only* place a recall brief
is written, and it is the only prose-generating, token-spending step of recall —
so it is held to the charter's evidence-first discipline:

* It synthesizes **only** from the versions the deterministic retrieval already
  returned. Every cited ``version_id`` is validated against that retrieved set, so
  a hallucinated id can never enter a brief.
* The brief is fixed to four blocks — 覆盖度 / 当时判断 / 后来描述性结果 / 缺口与反证 —
  and each point carries the ``version_id`` it rests on. The reader can click
  straight from a sentence to the archived evidence behind it.
* It must say so plainly when the sample is thin, and must not launder a handful of
  *surviving* posts into a false "大家当时都看多" consensus (recall runs over a
  corpus that over-represents what was never deleted — charter 4/7). The 缺口与反证
  block exists to surface removed posts and missing coverage neutrally.

The synthesized brief is handed back to the caller, which appends it immutably to
``topic_briefs`` (see :func:`kol_archive.recall.append_topic_brief`) together with
the coverage/selection denominators *as they stood at synthesis time*.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from typing import Any, cast

import httpx

# The four blocks are fixed in order and title: a brief always answers the same
# four questions, so it stays comparable across topics and auditable against the
# charter. Keys mirror the JSON contract the model is asked to return.
BLOCKS: tuple[tuple[str, str], ...] = (
    ("coverage", "覆盖度"),
    ("contemporaneous_judgement", "当时判断"),
    ("later_descriptive_outcome", "后来描述性结果"),
    ("gaps_and_counterevidence", "缺口与反证"),
)

# A brief is a summary, not a transcript. Cap the evidence handed to the model and
# the points it may emit per block so a popular window stays within a sane prompt
# and the output stays scannable.
MAX_BRIEF_HITS = 80
MAX_POINTS_PER_BLOCK = 12
CONTENT_SNIPPET_LIMIT = 600

SYSTEM_PROMPT = """你在基于一组**已确定性检索、可逐条核对**的历史发言，合成一份主题回溯简报。\
你只能综述给定证据里已经表达的内容，绝不补造事实、观点、标的或市场结果，也绝不评判谁当时对、谁错。

关键纪律：检索只覆盖现存归档，已删除的发言会让画面显得偏“干净”，幸存样本会高估某种观点的普遍性。\
因此样本少时必须直说“样本少，不足以代表当时共识”，绝不能把少数幸存发言洗成“大家当时都怎么看”的假共识。

输出 JSON 对象，且仅含以下四个键，每个键的值是一个数组，数组每个元素是一个“要点”对象 \
{"text": 一句简短中文, "version_ids": 该要点所依据的 version_id 整数数组}：
- coverage（覆盖度）：这批证据有多薄或多厚——命中版本/博主/帖子数、各分组各自命中数、\
  时间与来源是否集中。样本不足以代表共识时必须明说。此块的 version_ids 可以为空数组。
- contemporaneous_judgement（当时判断）：检索到的博主当时就该主题实际说了什么。\
  每个要点都要在 version_ids 里给出其综述所依据的版本；只综述检索到的发言，不外推、不补造、\
  不把少数样本说成普遍共识。
- later_descriptive_outcome（后来描述性结果）：仅复述证据里附带的描述性市场变化（标的/超额收益），\
  保持中性、只描述、不做因果归因、不判断对错；若证据未附带市场结果数据，就如实说明没有。
- gaps_and_counterevidence（缺口与反证）：缺什么、有什么反证——\
  曾被来源页移除的帖子（中性列出，不归因）、命中里的少数/相反意见、时间或覆盖盲区。\
  涉及具体发言时在 version_ids 里给出依据。

要求：version_ids 只能取自给定证据中出现过的 version_id，绝不可编造或臆测 id；拿不准就少写要点。\
输出必须是严格合法的 JSON，字符串内的 ASCII 双引号需转义。只输出 JSON。"""

RETRY_PROMPT = """上次输出未通过校验。请重新输出完整 JSON 对象：\
恰好含 coverage / contemporaneous_judgement / later_descriptive_outcome / \
gaps_and_counterevidence 四个键，每个键为要点数组，每个要点为 \
{"text": 字符串, "version_ids": 整数数组}；version_ids 只取自给定证据里的 version_id。\
只输出可被标准解析器直接解析的 JSON。"""


@dataclass(frozen=True)
class BriefSettings:
    base_url: str
    model: str
    api_key: str = field(repr=False)
    prompt_version: str = "brief-v1"


@dataclass(frozen=True)
class BriefPoint:
    """One synthesized point plus the retrieved versions it rests on.

    ``date_label`` anchors the point to *when* it was said — the post date of the
    cited versions (a single day, or ``earliest~latest`` when they span several) — so
    a retrospective claim like "5月中旬减仓" is auditable against the actual timeline,
    not just a list of ids. Empty when the point cites no version.
    """

    text: str
    version_ids: tuple[int, ...]
    date_label: str = ""


@dataclass(frozen=True)
class BriefSection:
    key: str
    title: str
    points: tuple[BriefPoint, ...]


@dataclass(frozen=True)
class TopicBrief:
    """A synthesized four-block brief: rendered text + the exact ids it cites."""

    sections: tuple[BriefSection, ...]
    brief_text: str
    cited_version_ids: tuple[int, ...]

    def to_payload(self) -> dict[str, object]:
        return {
            "sections": [
                {
                    "key": section.key,
                    "title": section.title,
                    "points": [
                        {
                            "text": point.text,
                            "version_ids": list(point.version_ids),
                            "date_label": point.date_label,
                        }
                        for point in section.points
                    ],
                }
                for section in self.sections
            ],
            "brief_text": self.brief_text,
            "cited_version_ids": list(self.cited_version_ids),
        }


def _required_text(mapping: dict[str, Any], key: str) -> str:
    value = str(mapping.get(key) or "").strip()
    if not value:
        raise ValueError(f"llm.{key} must be configured")
    return value


def load_brief_settings(config: dict[str, Any]) -> BriefSettings:
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
    return BriefSettings(
        base_url=str(llm.get("base_url") or "https://api.openai.com/v1").rstrip("/"),
        model=_required_text(llm, "model"),
        api_key=api_key,
        prompt_version=str(llm.get("brief_prompt_version") or "brief-v1").strip() or "brief-v1",
    )


def _clip(text: str) -> str:
    collapsed = " ".join(text.split())
    if len(collapsed) <= CONTENT_SNIPPET_LIMIT:
        return collapsed
    return collapsed[:CONTENT_SNIPPET_LIMIT] + "…"


def _summarize_hit(hit: dict[str, object]) -> dict[str, object]:
    snapshot = cast(dict[str, object], hit.get("market_snapshot") or {})
    return {
        "version_id": int(cast(int, hit["version_id"])),
        "author": hit.get("author_display_name") or hit.get("author_platform_uid"),
        "at": str(hit.get("viewpoint_at") or "")[:10],
        "removed": bool(hit.get("removed")),
        "stance": hit.get("stance_summary") or "",
        "framework_topics": hit.get("framework_topics") or [],
        "content": _clip(str(hit.get("content_text") or "")),
        "market": (
            None
            if not snapshot
            else {
                "raw_return": snapshot.get("raw_return"),
                "excess_return": snapshot.get("excess_return"),
            }
        ),
    }


def _build_digest(retrieval: dict[str, object]) -> dict[str, object]:
    """Compact, id-anchored view of the retrieval for the model to summarize."""
    hits = cast(list[dict[str, object]], retrieval.get("hits") or [])
    return {
        "query": retrieval.get("query"),
        "coverage": retrieval.get("coverage"),
        "selection": retrieval.get("selection"),
        "hits": [_summarize_hit(hit) for hit in hits[:MAX_BRIEF_HITS]],
        "hit_truncated": len(hits) > MAX_BRIEF_HITS,
    }


def _clean_version_ids(value: object, *, allowed_ids: set[int]) -> tuple[int, ...]:
    """Keep only ids that actually appear in the retrieved set — drop any others.

    This is the guard that makes a brief auditable: the model can name no version
    the deterministic retrieval did not return, so a fabricated citation is silently
    discarded rather than persisted as false provenance.
    """
    if not isinstance(value, list):
        return ()
    seen: list[int] = []
    for item in value:
        try:
            version_id = int(item)
        except TypeError, ValueError:
            continue
        if version_id in allowed_ids and version_id not in seen:
            seen.append(version_id)
    return tuple(seen)


def _date_label(version_ids: tuple[int, ...], version_dates: dict[int, str]) -> str:
    """The post-date span of the cited versions: one day, or ``earliest~latest``.

    Dates are ``YYYY-MM-DD`` strings, so lexical sort is chronological. Versions with
    no recorded date are skipped; an empty result means no date could be attached.
    """
    dates = sorted({version_dates[v] for v in version_ids if version_dates.get(v)})
    if not dates:
        return ""
    return dates[0] if len(dates) == 1 else f"{dates[0]}~{dates[-1]}"


def _clean_points(
    value: object, *, allowed_ids: set[int], version_dates: dict[int, str]
) -> list[BriefPoint]:
    if isinstance(value, list):
        items: list[object] = value
    elif value:
        items = [value]
    else:
        items = []
    points: list[BriefPoint] = []
    for item in items:
        if isinstance(item, dict):
            text = str(item.get("text") or "").strip()
            raw_ids: object = item.get("version_ids")
        else:
            text = str(item or "").strip()
            raw_ids = None
        if not text:
            continue
        version_ids = _clean_version_ids(raw_ids, allowed_ids=allowed_ids)
        points.append(
            BriefPoint(
                text=text,
                version_ids=version_ids,
                date_label=_date_label(version_ids, version_dates),
            )
        )
        if len(points) >= MAX_POINTS_PER_BLOCK:
            break
    return points


def _render_brief_text(sections: tuple[BriefSection, ...]) -> str:
    lines: list[str] = []
    for section in sections:
        lines.append(f"## {section.title}")
        if section.points:
            for point in section.points:
                if point.version_ids:
                    ids = "、".join(f"v{version_id}" for version_id in point.version_ids)
                    citation = (
                        f"〔{point.date_label} · {ids}〕" if point.date_label else f"〔{ids}〕"
                    )
                else:
                    citation = ""
                lines.append(f"- {point.text}{citation}")
        else:
            lines.append("- （本次未生成该部分内容）")
        lines.append("")
    return "\n".join(lines).strip()


def _json_content(value: object) -> str:
    content = str(value).strip()
    if content.startswith("```") and content.endswith("```"):
        lines = content.splitlines()
        if len(lines) >= 3:
            content = "\n".join(lines[1:-1]).strip()
    return content


def _parse_brief(
    payload: dict[str, Any], *, allowed_ids: set[int], version_dates: dict[int, str]
) -> TopicBrief:
    try:
        content = payload["choices"][0]["message"]["content"]
    except (KeyError, IndexError, TypeError) as error:
        raise ValueError("LLM response is missing choices[0].message.content") from error
    try:
        parsed = json.loads(_json_content(content))
    except json.JSONDecodeError as error:
        raise ValueError("LLM brief content is not valid JSON") from error
    if not isinstance(parsed, dict):
        raise ValueError("LLM brief content must be a JSON object")
    sections: list[BriefSection] = []
    cited: set[int] = set()
    for key, title in BLOCKS:
        points = _clean_points(
            parsed.get(key), allowed_ids=allowed_ids, version_dates=version_dates
        )
        for point in points:
            cited.update(point.version_ids)
        sections.append(BriefSection(key=key, title=title, points=tuple(points)))
    if not any(section.points for section in sections):
        raise ValueError("LLM brief produced no usable section")
    return TopicBrief(
        sections=tuple(sections),
        brief_text=_render_brief_text(tuple(sections)),
        cited_version_ids=tuple(sorted(cited)),
    )


def synthesize_brief(
    settings: BriefSettings,
    retrieval: dict[str, object],
    *,
    client: httpx.Client | None = None,
) -> TopicBrief:
    """Synthesize a four-block brief from a deterministic retrieval result.

    ``retrieval`` is the dict returned by :func:`kol_archive.recall.retrieve`
    (query echo + coverage + selection + hits). Only the retrieved hits are summarized
    and only their version ids may be cited. Retries once on a parse failure,
    mirroring enrichment and expansion.
    """
    hits = cast(list[dict[str, object]], retrieval.get("hits") or [])
    allowed_ids = {int(cast(int, hit["version_id"])) for hit in hits}
    if not allowed_ids:
        raise ValueError("retrieval produced no hits to synthesize a brief from")
    # Post date per cited version, so every point can be anchored to when it was said.
    version_dates = {
        int(cast(int, hit["version_id"])): str(hit.get("viewpoint_at") or "")[:10] for hit in hits
    }
    digest = json.dumps(_build_digest(retrieval), ensure_ascii=False, default=str)
    owned_client = client is None
    active_client = client or httpx.Client(timeout=60.0)
    try:
        messages = [
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": digest},
        ]
        try:
            return _request_and_parse(
                settings,
                messages,
                active_client,
                allowed_ids=allowed_ids,
                version_dates=version_dates,
            )
        except ValueError:
            messages.append({"role": "user", "content": RETRY_PROMPT})
            return _request_and_parse(
                settings,
                messages,
                active_client,
                allowed_ids=allowed_ids,
                version_dates=version_dates,
            )
    finally:
        if owned_client:
            active_client.close()


def _request_and_parse(
    settings: BriefSettings,
    messages: list[dict[str, str]],
    client: httpx.Client,
    *,
    allowed_ids: set[int],
    version_dates: dict[int, str],
) -> TopicBrief:
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
    return _parse_brief(
        cast(dict[str, Any], payload), allowed_ids=allowed_ids, version_dates=version_dates
    )
