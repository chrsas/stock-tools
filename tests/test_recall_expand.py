from __future__ import annotations

import json
from typing import Any

import httpx
import pytest

from kol_archive.recall_expand import (
    ExpandedGroup,
    ExpandSettings,
    expand_query,
    load_expand_settings,
)

TODAY = "2025-07-01"


def _llm_response(payload: dict[str, object]) -> httpx.MockTransport:
    def handle(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={"choices": [{"message": {"content": json.dumps(payload, ensure_ascii=False)}}]},
        )

    return httpx.MockTransport(handle)


def _settings() -> ExpandSettings:
    return ExpandSettings(
        base_url="https://llm.example/v1",
        model="test-model",
        api_key="secret-key",
        prompt_version="expand-v1",
    )


def test_load_expand_settings_reads_prompt_version(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("TEST_LLM_KEY", "secret-key")
    settings = load_expand_settings(
        {
            "llm": {
                "provider": "openai_compatible",
                "base_url": "https://llm.example/v1/",
                "model": "test-model",
                "api_key_env": "TEST_LLM_KEY",
                "expand_prompt_version": "expand-v2",
            }
        }
    )
    assert settings == ExpandSettings(
        base_url="https://llm.example/v1",
        model="test-model",
        api_key="secret-key",
        prompt_version="expand-v2",
    )


def test_load_expand_settings_defaults_prompt_version(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("LLM_API_KEY", "secret-key")
    settings = load_expand_settings({"llm": {"model": "m"}})
    assert settings.prompt_version == "expand-v1"


def test_expand_query_parses_groups_window_and_tickers() -> None:
    payload: dict[str, object] = {
        "groups": [
            {"label": "event", "terms": ["美伊", "伊朗", "霍尔木兹"]},
            {"label": "market", "terms": ["油价", "原油"]},
        ],
        "date_from": "2025-06-10",
        "date_to": "2025-06-30",
        "tickers": ["sh601857"],
        "notes": "拆成事件与标的两组",
    }
    with httpx.Client(transport=_llm_response(payload)) as client:
        result = expand_query(_settings(), "美伊冲突那阵油价怎么看", today=TODAY, client=client)
    assert result.groups == (
        ExpandedGroup("event", ("美伊", "伊朗", "霍尔木兹")),
        ExpandedGroup("market", ("油价", "原油")),
    )
    assert result.date_from == "2025-06-10"
    assert result.date_to == "2025-06-30"
    assert result.tickers == ("SH601857",)  # uppercased
    assert result.notes == "拆成事件与标的两组"
    assert result.to_payload()["groups"] == [
        {"label": "event", "terms": ["美伊", "伊朗", "霍尔木兹"]},
        {"label": "market", "terms": ["油价", "原油"]},
    ]


def test_expand_query_drops_invalid_and_future_dates() -> None:
    payload: dict[str, object] = {
        "groups": [{"label": "event", "terms": ["伊朗"]}],
        "date_from": "not-a-date",
        "date_to": "2099-01-01",  # future relative to TODAY anchor
        "tickers": [],
        "notes": "",
    }
    with httpx.Client(transport=_llm_response(payload)) as client:
        result = expand_query(_settings(), "伊朗局势", today=TODAY, client=client)
    assert result.date_from == ""
    assert result.date_to == ""


def test_expand_query_skips_empty_groups_and_dedupes_terms() -> None:
    payload: dict[str, object] = {
        "groups": [
            {"label": "", "terms": []},  # dropped: no terms
            {"label": "", "terms": ["油价", "油价", " 原油 "]},  # default label, dedupe, trim
        ],
        "date_from": "",
        "date_to": "",
    }
    with httpx.Client(transport=_llm_response(payload)) as client:
        result = expand_query(_settings(), "油价", today=TODAY, client=client)
    assert len(result.groups) == 1
    assert result.groups[0].label == "group2"
    assert result.groups[0].terms == ("油价", "原油")
    assert result.tickers == ()


def test_expand_query_retries_once_when_no_usable_group() -> None:
    responses: list[dict[str, object]] = [
        {"groups": [], "date_from": "", "date_to": ""},
        {"groups": [{"label": "event", "terms": ["伊朗"]}], "date_from": "", "date_to": ""},
    ]

    def handle(request: httpx.Request) -> httpx.Response:
        payload = responses.pop(0)
        return httpx.Response(
            200,
            json={"choices": [{"message": {"content": json.dumps(payload, ensure_ascii=False)}}]},
        )

    with httpx.Client(transport=httpx.MockTransport(handle)) as client:
        result = expand_query(_settings(), "伊朗", today=TODAY, client=client)
    assert result.groups == (ExpandedGroup("event", ("伊朗",)),)
    assert responses == []  # both responses consumed (one retry)


def test_expand_query_raises_after_retry_still_unusable() -> None:
    payload: dict[str, Any] = {"groups": [], "date_from": "", "date_to": ""}
    with httpx.Client(transport=_llm_response(payload)) as client:
        with pytest.raises(ValueError, match="no usable keyword group"):
            expand_query(_settings(), "伊朗", today=TODAY, client=client)


def test_expand_query_rejects_blank_question() -> None:
    with pytest.raises(ValueError, match="must not be empty"):
        expand_query(_settings(), "   ", today=TODAY)
