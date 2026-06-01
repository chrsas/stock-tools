from __future__ import annotations

import json
from pathlib import Path
from typing import Any, cast

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
