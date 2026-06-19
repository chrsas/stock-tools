from __future__ import annotations

import io
import json
import sys
from collections.abc import Iterator
from pathlib import Path
from typing import cast

import httpx
import pytest

from kol_archive.cli.common import configure_stdout_utf8, print_json, resolve_db_path
from kol_archive.database import connect_database, initialize_database
from kol_archive.models import (
    ContentFidelity,
    FeedRun,
    IngestMode,
    LoginState,
    NormalizedPost,
    ProbeResult,
    ProbeRun,
    RunStatus,
    WatchMode,
)
from kol_archive.presentation import build_evidence_card, list_timeline
from kol_archive.rewrite import RewriteSettings, load_rewrite_settings, request_rewrite
from kol_archive.service import Archive

BASE_TIME = "2026-06-01T00:00:00+00:00"


@pytest.fixture
def archive() -> Iterator[Archive]:
    connection = connect_database(":memory:")
    initialize_database(connection)
    service = Archive(connection)
    service.add_author("xueqiu", "100", BASE_TIME)
    try:
        yield service
    finally:
        connection.close()


def make_feed_run(finished_at: str = BASE_TIME, *, notes: str | None = None) -> FeedRun:
    return FeedRun(
        author_id=1,
        platform="xueqiu",
        started_at=finished_at,
        finished_at=finished_at,
        status=RunStatus.OK,
        login_state=LoginState.VALID,
        pages_fetched=1,
        pagination_complete=True,
        covered_from="2026-05-01T00:00:00+00:00",
        covered_to="2026-06-02T00:00:00+00:00",
        rate_limited=False,
        http_error_count=0,
        ingest_mode=IngestMode.LIVE,
        adapter_version="xueqiu-2",
        notes=notes,
    )


def make_post(
    platform_post_id: str = "post-1",
    *,
    observed_at: str = BASE_TIME,
    text: str = "A",
    posted_at_claimed: str = "2026-05-20T00:00:00+00:00",
) -> NormalizedPost:
    return NormalizedPost(
        platform_post_id=platform_post_id,
        author_id=1,
        observed_at=observed_at,
        content_fidelity=ContentFidelity.FULL,
        content_text=text,
        content_hash=f"hash-{text}",
        posted_at_claimed=posted_at_claimed,
        url=f"https://xueqiu.com/100/{platform_post_id}",
    )


def post_id(archive: Archive, platform_post_id: str = "post-1") -> int:
    row = archive.connection.execute(
        "SELECT id FROM posts WHERE platform_post_id = ?",
        (platform_post_id,),
    ).fetchone()
    assert row is not None
    return int(row["id"])


def test_timeline_exposes_observation_times_and_deletion_signal_levels(archive: Archive) -> None:
    archive.record_feed_run(make_feed_run(), [make_post()])
    for hour in (1, 2, 3):
        archive.record_feed_run(make_feed_run(f"2026-06-01T0{hour}:00:00+00:00"), [])

    timeline = list_timeline(archive.connection)

    assert timeline[0]["current_version_first_observed_at"] == BASE_TIME
    assert timeline[0]["current_version_last_observed_at"] == BASE_TIME
    assert timeline[0]["last_feed_absence_detected_at"] == "2026-06-01T03:00:00+00:00"
    assert cast(dict[str, str], timeline[0]["status"])["deletion_signal_level"] == "weak"

    archive.record_probe_run(
        ProbeRun(
            post_id=post_id(archive),
            started_at="2026-06-01T04:00:00+00:00",
            finished_at="2026-06-01T04:00:00+00:00",
            observed_at="2026-06-01T04:00:00+00:00",
            status=RunStatus.OK,
            http_status=200,
            login_state=LoginState.VALID,
            rate_limited=False,
            result=ProbeResult.EXPLICITLY_REMOVED,
            content_fidelity=ContentFidelity.NA,
            ingest_mode=IngestMode.LIVE,
            adapter_version="xueqiu-2",
            notes="explicit_removed_placeholder",
        )
    )

    assert (
        cast(dict[str, str], list_timeline(archive.connection)[0]["status"])[
            "deletion_signal_level"
        ]
        == "strong"
    )


def test_timeline_orders_by_claimed_post_time_before_observation_time(
    archive: Archive,
) -> None:
    archive.record_feed_run(
        make_feed_run("2026-06-03T00:00:00+00:00"),
        [
            make_post(
                "new-live",
                observed_at="2026-06-03T00:00:00+00:00",
                posted_at_claimed="2026-06-03T00:00:00+00:00",
            )
        ],
    )
    archive.record_feed_run(
        make_feed_run("2026-06-03T01:00:00+00:00"),
        [
            make_post(
                "old-backfill",
                observed_at="2026-06-03T01:00:00+00:00",
                posted_at_claimed="2026-03-01T00:00:00+00:00",
            )
        ],
    )

    timeline = list_timeline(archive.connection)

    assert [item["platform_post_id"] for item in timeline] == ["new-live", "old-backfill"]


def test_timeline_keeps_feed_presence_visible_when_direct_link_is_unavailable(
    archive: Archive,
) -> None:
    archive.record_feed_run(make_feed_run(), [make_post()])
    archive.record_probe_run(
        ProbeRun(
            post_id=post_id(archive),
            started_at="2026-06-01T01:00:00+00:00",
            finished_at="2026-06-01T01:00:00+00:00",
            observed_at="2026-06-01T01:00:00+00:00",
            status=RunStatus.OK,
            http_status=404,
            login_state=LoginState.VALID,
            rate_limited=False,
            result=ProbeResult.NOT_FOUND,
            content_fidelity=ContentFidelity.NA,
            ingest_mode=IngestMode.LIVE,
            adapter_version="xueqiu-2",
        )
    )

    status = cast(dict[str, str], list_timeline(archive.connection)[0]["status"])

    assert status["deletion_signal_level"] == "weak"
    assert "列表观察：在场" in status["human_label"]
    assert "来源：直链当前不可访问" in status["human_label"]


def test_evidence_card_shows_version_diff_events_runs_and_notes(archive: Archive) -> None:
    archive.record_feed_run(make_feed_run(), [make_post()])
    archive.record_feed_run(
        make_feed_run("2026-06-01T01:00:00+00:00", notes="cookie=secret-value"),
        [make_post(observed_at="2026-06-01T01:00:00+00:00", text="B")],
    )

    card = build_evidence_card(archive.connection, post_id(archive))

    assert card["versions"][0]["diff_from_prior_observed_version"] is None
    assert "-A" in card["versions"][1]["diff_from_prior_observed_version"]
    assert "+B" in card["versions"][1]["diff_from_prior_observed_version"]
    assert card["events"][-1]["evidence_fetch_run_id"] == 2
    assert card["feed_observations"][1]["fetch_run_id"] == 2
    assert card["feed_observations"][1]["fetch_notes"] == "cookie=[REDACTED]"
    assert "secret-value" not in json.dumps(card, ensure_ascii=False)


def test_attention_reason_pins_and_keeps_selected_version(archive: Archive) -> None:
    archive.record_feed_run(make_feed_run(), [make_post()])
    selected_version_id = archive.current_version_id(post_id(archive))

    attention_id = archive.add_attention(
        post_id(archive),
        selected_version_id,
        "2026-06-01T01:00:00+00:00",
        "需要继续跟踪",
        "关注后续兑现情况",
    )
    archive.record_feed_run(
        make_feed_run("2026-06-01T02:00:00+00:00"),
        [make_post(observed_at="2026-06-01T02:00:00+00:00", text="B")],
    )

    row = archive.connection.execute(
        """
        SELECT l.id, l.version_id, p.watch_mode, p.current_version_id
        FROM attention_log l JOIN posts p ON p.id = l.post_id
        WHERE l.id = ?
        """,
        (attention_id,),
    ).fetchone()
    assert row is not None
    assert row["version_id"] == selected_version_id
    assert row["current_version_id"] != selected_version_id
    assert row["watch_mode"] == WatchMode.PINNED


def test_rewrite_exercise_pins_and_copies_original_observed_version(archive: Archive) -> None:
    archive.record_feed_run(make_feed_run(), [make_post()])
    selected_version_id = archive.current_version_id(post_id(archive))

    source = archive.rewrite_source(post_id(archive), selected_version_id)
    exercise_id = archive.add_rewrite_exercise(
        source,
        "命题改写",
        "保留原文边界",
        "test-model",
        "v1",
        "2026-06-01T01:00:00+00:00",
    )
    archive.review_rewrite_exercise(exercise_id, "valid")

    row = archive.connection.execute(
        """
        SELECT r.version_id, r.original_text, r.my_verdict, p.watch_mode
        FROM rewrite_exercises r JOIN posts p ON p.id = r.post_id
        WHERE r.id = ?
        """,
        (exercise_id,),
    ).fetchone()
    assert row is not None
    assert tuple(row) == (selected_version_id, "A", "valid", WatchMode.PINNED)


def test_unpin_uses_recent_window_to_choose_next_watch_mode(archive: Archive) -> None:
    archive.record_feed_run(
        make_feed_run(),
        [
            make_post("recent", posted_at_claimed="2026-05-20T00:00:00+00:00"),
            make_post("old", posted_at_claimed="2026-04-01T00:00:00+00:00"),
        ],
    )
    archive.pin_post(post_id(archive, "recent"), "2026-06-01T01:00:00+00:00")
    archive.pin_post(post_id(archive, "old"), "2026-06-01T01:00:00+00:00")

    for platform_post_id in ("recent", "old"):
        archive.unpin_post_for_window(
            post_id(archive, platform_post_id),
            "2026-06-01T02:00:00+00:00",
            "2026-05-01T00:00:00+00:00",
        )

    rows = archive.connection.execute(
        "SELECT platform_post_id, watch_mode FROM posts ORDER BY platform_post_id"
    ).fetchall()
    assert [tuple(row) for row in rows] == [
        ("old", WatchMode.INACTIVE),
        ("recent", WatchMode.RECENT_WINDOW),
    ]


def test_llm_rewrite_reads_key_from_environment_and_parses_structured_response(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("TEST_LLM_KEY", "secret-key")
    settings = load_rewrite_settings(
        {
            "llm": {
                "provider": "openai_compatible",
                "base_url": "https://llm.example/v1/",
                "model": "test-model",
                "api_key_env": "TEST_LLM_KEY",
                "prompt_version": "rewrite-v1",
            }
        }
    )

    def handle(request: httpx.Request) -> httpx.Response:
        assert str(request.url) == "https://llm.example/v1/chat/completions"
        assert request.headers["Authorization"] == "Bearer secret-key"
        body = json.loads(request.content)
        assert body["messages"][1]["content"] == "原文"
        return httpx.Response(
            200,
            json={
                "choices": [
                    {
                        "message": {
                            "content": json.dumps(
                                {"rewritten_claim": "改写", "rationale": "理由"},
                                ensure_ascii=False,
                            )
                        }
                    }
                ]
            },
        )

    with httpx.Client(transport=httpx.MockTransport(handle)) as client:
        suggestion = request_rewrite(settings, "原文", client=client)

    assert settings == RewriteSettings(
        base_url="https://llm.example/v1",
        model="test-model",
        api_key="secret-key",
        prompt_version="rewrite-v1",
    )
    assert suggestion.rewritten_claim == "改写"
    assert suggestion.rationale == "理由"


def test_explicit_database_path_overrides_configured_storage_path() -> None:
    config = {"storage": {"db_path": "data/custom.sqlite3"}}

    assert resolve_db_path(None, config) == Path("data/custom.sqlite3")
    assert resolve_db_path(Path("data/override.sqlite3"), config) == Path("data/override.sqlite3")


def testconfigure_stdout_utf8_supports_emoji_json_output(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    raw = io.BytesIO()
    stdout = io.TextIOWrapper(raw, encoding="gbk")
    monkeypatch.setattr(sys, "stdout", stdout)

    configure_stdout_utf8()
    print_json({"text": "🥚"})
    stdout.flush()

    assert json.loads(raw.getvalue().decode("utf-8")) == {"text": "🥚"}
    stdout.detach()
