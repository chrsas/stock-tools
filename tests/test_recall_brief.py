from __future__ import annotations

import json
from typing import Any, cast

import httpx
import pytest

from kol_archive.recall_brief import (
    BriefSettings,
    load_brief_settings,
    synthesize_brief,
)


def _llm_response(payload: dict[str, object]) -> httpx.MockTransport:
    def handle(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={"choices": [{"message": {"content": json.dumps(payload, ensure_ascii=False)}}]},
        )

    return httpx.MockTransport(handle)


def _settings() -> BriefSettings:
    return BriefSettings(
        base_url="https://llm.example/v1",
        model="test-model",
        api_key="secret-key",
        prompt_version="brief-v1",
    )


def _retrieval() -> dict[str, object]:
    return {
        "query": {"question": "美伊冲突油价", "date_from": "2025-06-10T00:00:00+00:00"},
        "coverage": {"version_count": 2, "author_count": 2, "post_count": 2, "groups": []},
        "selection": {"removed_post_count": 1, "removed_post_ids": [9]},
        "hits": [
            {
                "version_id": 11,
                "post_id": 5,
                "author_platform_uid": "a",
                "author_display_name": "作者甲",
                "viewpoint_at": "2025-06-15T01:00:00+00:00",
                "content_text": "伊朗局势紧张，油价可能冲高",
                "removed": False,
                "stance_summary": "看多原油",
                "framework_topics": [],
                "market_snapshot": {"raw_return": 0.05, "excess_return": 0.02},
            },
            {
                "version_id": 22,
                "post_id": 9,
                "author_platform_uid": "b",
                "author_display_name": "作者乙",
                "viewpoint_at": "2025-06-18T01:00:00+00:00",
                "content_text": "霍尔木兹风险被高估",
                "removed": True,
                "stance_summary": "看空油价",
                "framework_topics": [],
                "market_snapshot": None,
            },
        ],
    }


def test_load_brief_settings_reads_prompt_version(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("TEST_LLM_KEY", "secret-key")
    settings = load_brief_settings(
        {
            "llm": {
                "base_url": "https://llm.example/v1/",
                "model": "test-model",
                "api_key_env": "TEST_LLM_KEY",
                "brief_prompt_version": "brief-v2",
            }
        }
    )
    assert settings == BriefSettings(
        base_url="https://llm.example/v1",
        model="test-model",
        api_key="secret-key",
        prompt_version="brief-v2",
    )


def test_load_brief_settings_defaults_prompt_version(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("LLM_API_KEY", "secret-key")
    settings = load_brief_settings({"llm": {"model": "m"}})
    assert settings.prompt_version == "brief-v1"


def test_synthesize_brief_builds_four_blocks_and_citations() -> None:
    payload: dict[str, object] = {
        "coverage": [
            {"text": "命中 2 个版本、2 位博主，样本少，不足以代表共识", "version_ids": []}
        ],
        "contemporaneous_judgement": [
            {"text": "甲看多原油", "version_ids": [11]},
            {"text": "乙认为风险被高估", "version_ids": [22]},
        ],
        "later_descriptive_outcome": [{"text": "甲发言后标的上涨", "version_ids": [11]}],
        "gaps_and_counterevidence": [{"text": "乙的帖子后被来源页移除", "version_ids": [22]}],
    }
    with httpx.Client(transport=_llm_response(payload)) as client:
        brief = synthesize_brief(_settings(), _retrieval(), client=client)

    assert [section.key for section in brief.sections] == [
        "coverage",
        "contemporaneous_judgement",
        "later_descriptive_outcome",
        "gaps_and_counterevidence",
    ]
    assert [section.title for section in brief.sections] == [
        "覆盖度",
        "当时判断",
        "后来描述性结果",
        "缺口与反证",
    ]
    assert brief.cited_version_ids == (11, 22)
    assert "## 覆盖度" in brief.brief_text
    # Each cited point carries the post date of its versions (anchored to "当时").
    assert "〔2025-06-15 · v11〕" in brief.brief_text
    assert "〔2025-06-18 · v22〕" in brief.brief_text
    assert "样本少" in brief.brief_text
    judgement = next(s for s in brief.sections if s.key == "contemporaneous_judgement")
    assert judgement.points[0].date_label == "2025-06-15"
    payload_out = brief.to_payload()
    assert payload_out["cited_version_ids"] == [11, 22]
    sections_out = cast(list[dict[str, Any]], payload_out["sections"])
    assert sections_out[1]["points"][0]["date_label"] == "2025-06-15"


def test_synthesize_brief_drops_hallucinated_version_ids() -> None:
    payload: dict[str, object] = {
        "coverage": [],
        "contemporaneous_judgement": [
            # 999 is not in the retrieved set and must be discarded; 11 is kept.
            {"text": "综述", "version_ids": [11, 999]},
        ],
        "later_descriptive_outcome": [],
        "gaps_and_counterevidence": [],
    }
    with httpx.Client(transport=_llm_response(payload)) as client:
        brief = synthesize_brief(_settings(), _retrieval(), client=client)
    assert brief.cited_version_ids == (11,)
    judgement = next(s for s in brief.sections if s.key == "contemporaneous_judgement")
    assert judgement.points[0].version_ids == (11,)


def test_synthesize_brief_retries_once_when_no_usable_section() -> None:
    responses: list[dict[str, object]] = [
        {
            "coverage": [],
            "contemporaneous_judgement": [],
            "later_descriptive_outcome": [],
            "gaps_and_counterevidence": [],
        },
        {
            "coverage": [{"text": "样本少", "version_ids": []}],
            "contemporaneous_judgement": [],
            "later_descriptive_outcome": [],
            "gaps_and_counterevidence": [],
        },
    ]

    def handle(request: httpx.Request) -> httpx.Response:
        payload = responses.pop(0)
        return httpx.Response(
            200,
            json={"choices": [{"message": {"content": json.dumps(payload, ensure_ascii=False)}}]},
        )

    with httpx.Client(transport=httpx.MockTransport(handle)) as client:
        brief = synthesize_brief(_settings(), _retrieval(), client=client)
    assert responses == []  # both consumed (one retry)
    assert brief.sections[0].points[0].text == "样本少"


def test_synthesize_brief_raises_without_hits() -> None:
    empty = cast(dict[str, object], {"hits": [], "coverage": {}, "selection": {}})
    with pytest.raises(ValueError, match="no hits"):
        synthesize_brief(_settings(), empty)


def test_synthesize_brief_raises_after_retry_still_unusable() -> None:
    payload: dict[str, Any] = {
        "coverage": [],
        "contemporaneous_judgement": [],
        "later_descriptive_outcome": [],
        "gaps_and_counterevidence": [],
    }
    with httpx.Client(transport=_llm_response(payload)) as client:
        with pytest.raises(ValueError, match="no usable section"):
            synthesize_brief(_settings(), _retrieval(), client=client)
