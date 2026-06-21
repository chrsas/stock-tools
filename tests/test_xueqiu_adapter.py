from __future__ import annotations

import json
from pathlib import Path
from typing import Any, cast

import pytest

from kol_archive.adapters.xueqiu import (
    epoch_ms_to_utc,
    parse_feed_page,
    parse_probe_response,
)
from kol_archive.models import ContentFidelity, LoginState, ProbeResult, RunStatus

FIXTURES = Path(__file__).parent.parent / "probe" / "fixtures"


def load_fixture(name: str) -> dict[str, Any]:
    return cast(dict[str, Any], json.loads((FIXTURES / name).read_text(encoding="utf-8")))


def test_feed_page_excludes_pinned_post_from_coverage_and_marks_preview() -> None:
    page = parse_feed_page(
        load_fixture("xueqiu_timeline_page.json"),
        author_id=1,
        observed_at="2026-06-01T00:00:00+00:00",
    )

    assert [post.platform_post_id for post in page.posts] == ["111", "222", "333"]
    assert page.posts[0].content_fidelity is ContentFidelity.FULL
    assert page.posts[2].content_fidelity is ContentFidelity.PREVIEW
    assert page.covered_from == epoch_ms_to_utc(1779000000000)
    assert page.covered_to == epoch_ms_to_utc(1780214445000)
    assert page.covers_window("2026-05-01T00:00:00+00:00") is False
    # 头部置顶帖（id 111, mark==1）不能当 head，否则时间线“头变没变”判断会被永久钉死；
    # head 必须是最新的非置顶帖（id 222），其时间即覆盖区间上界。
    assert page.head_platform_post_id == "222"
    assert page.head_posted_at == epoch_ms_to_utc(1780214445000)


def test_feed_and_direct_fixture_normalize_to_same_hash() -> None:
    page = parse_feed_page(
        load_fixture("xueqiu_timeline_page.json"),
        author_id=1,
        observed_at="2026-06-01T00:00:00+00:00",
    )
    probe = parse_probe_response(
        200,
        load_fixture("xueqiu_show_reachable.json"),
        author_id=1,
        observed_at="2026-06-01T01:00:00+00:00",
    )

    assert probe.result is ProbeResult.REACHABLE
    assert probe.observed_post is not None
    assert probe.observed_post.content_hash == page.posts[1].content_hash


def test_recognized_post_with_missing_text_degrades_to_na() -> None:
    payload = load_fixture("xueqiu_show_reachable.json")
    payload.pop("text")
    page_payload = {"statuses": [payload], "total": 1, "page": 1, "maxPage": 1}

    page = parse_feed_page(
        page_payload,
        author_id=1,
        observed_at="2026-06-01T00:00:00+00:00",
    )

    assert page.parse_failure_count == 1
    assert page.posts[0].content_fidelity is ContentFidelity.NA


def test_probe_classifies_login_expiry_not_found_and_explicit_removal() -> None:
    expired = parse_probe_response(
        400,
        load_fixture("xueqiu_error_login.json"),
        author_id=1,
        observed_at="2026-06-01T00:00:00+00:00",
    )
    missing = parse_probe_response(
        400,
        load_fixture("xueqiu_error_not_found.json"),
        author_id=1,
        observed_at="2026-06-01T00:00:00+00:00",
    )
    removed = parse_probe_response(
        200,
        {"explicitly_removed": True},
        author_id=1,
        observed_at="2026-06-01T00:00:00+00:00",
    )

    assert (expired.status, expired.login_state) == (RunStatus.PARTIAL, LoginState.EXPIRED)
    assert missing.result is ProbeResult.NOT_FOUND
    assert removed.result is ProbeResult.EXPLICITLY_REMOVED


def test_probe_classifies_non_json_200_response() -> None:
    probe = parse_probe_response(
        200,
        None,
        author_id=1,
        observed_at="2026-06-01T00:00:00+00:00",
    )

    assert probe.status is RunStatus.FAILED
    assert probe.result is ProbeResult.UNKNOWN
    assert probe.notes == "response_not_json"


def test_probe_classifies_json_non_object_200_response() -> None:
    probe = parse_probe_response(
        200,
        None,
        author_id=1,
        observed_at="2026-06-01T00:00:00+00:00",
        payload_issue="response_json_not_object",
    )

    assert probe.status is RunStatus.FAILED
    assert probe.result is ProbeResult.UNKNOWN
    assert probe.notes == "response_json_not_object"


@pytest.mark.parametrize(
    "restriction",
    [
        {"is_private": True},
        {"is_refused": True},
        {"legal_user_visible": False},
    ],
)
def test_probe_classifies_restricted_visibility_flags(restriction: dict[str, bool]) -> None:
    payload = load_fixture("xueqiu_show_reachable.json")
    payload.update(restriction)

    restricted = parse_probe_response(
        200,
        payload,
        author_id=1,
        observed_at="2026-06-01T00:00:00+00:00",
    )

    assert restricted.result is ProbeResult.RESTRICTED
    assert restricted.observed_post is None
