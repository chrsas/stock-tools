from __future__ import annotations

import logging
import sqlite3
from collections.abc import Iterator
from dataclasses import replace
from pathlib import Path

import pytest

from kol_archive.cli.common import init_db
from kol_archive.database import EVIDENCE_TABLES, connect_database, initialize_database
from kol_archive.models import (
    ArchiveSettings,
    ContentFidelity,
    FeedRun,
    FeedState,
    IngestMode,
    LoginState,
    NormalizedPost,
    ProbeResult,
    ProbeRun,
    QueueReason,
    RunStatus,
    SourceState,
    WatchMode,
)
from kol_archive.service import Archive, is_healthy_feed_run, is_healthy_probe_run

BASE_TIME = "2026-06-01T00:00:00+00:00"


@pytest.fixture
def archive() -> Iterator[Archive]:
    connection = connect_database(":memory:")
    initialize_database(connection)
    service = Archive(connection, ArchiveSettings(absent_threshold_n=3))
    service.add_author("xueqiu", "100", BASE_TIME)
    try:
        yield service
    finally:
        connection.close()


def make_feed_run(
    *,
    finished_at: str = BASE_TIME,
    status: RunStatus = RunStatus.OK,
    login_state: LoginState = LoginState.VALID,
    pagination_complete: bool = True,
    rate_limited: bool = False,
    parse_failure_count: int = 0,
) -> FeedRun:
    return FeedRun(
        author_id=1,
        platform="xueqiu",
        started_at=finished_at,
        finished_at=finished_at,
        status=status,
        login_state=login_state,
        pages_fetched=1,
        pagination_complete=pagination_complete,
        covered_from="2026-05-01T00:00:00+00:00",
        covered_to="2026-06-02T00:00:00+00:00",
        rate_limited=rate_limited,
        http_error_count=0,
        ingest_mode=IngestMode.LIVE,
        adapter_version="xueqiu-1",
        parse_failure_count=parse_failure_count,
    )


def make_post(
    platform_post_id: str = "post-1",
    *,
    observed_at: str = BASE_TIME,
    text: str = "A",
    fidelity: ContentFidelity = ContentFidelity.FULL,
    posted_at_claimed: str = "2026-05-20T00:00:00+00:00",
) -> NormalizedPost:
    return NormalizedPost(
        platform_post_id=platform_post_id,
        author_id=1,
        observed_at=observed_at,
        content_fidelity=fidelity,
        content_text=text if fidelity is ContentFidelity.FULL else None,
        content_hash=f"hash-{text}" if fidelity is ContentFidelity.FULL else None,
        posted_at_claimed=posted_at_claimed,
        url=f"https://xueqiu.com/100/{platform_post_id}",
        raw_payload={"text": text},
    )


def make_probe_run(
    post_id: int,
    result: ProbeResult,
    *,
    observed_at: str = BASE_TIME,
    status: RunStatus = RunStatus.OK,
    login_state: LoginState = LoginState.VALID,
    rate_limited: bool = False,
    fidelity: ContentFidelity = ContentFidelity.NA,
) -> ProbeRun:
    return ProbeRun(
        post_id=post_id,
        started_at=observed_at,
        finished_at=observed_at,
        observed_at=observed_at,
        status=status,
        http_status=200,
        login_state=login_state,
        rate_limited=rate_limited,
        result=result,
        content_fidelity=fidelity,
        ingest_mode=IngestMode.LIVE,
        adapter_version="xueqiu-1",
    )


def scalar(archive: Archive, query: str, params: tuple[object, ...] = ()) -> object:
    row = archive.connection.execute(query, params).fetchone()
    assert row is not None
    return row[0]


def test_init_db_creates_file_database_with_foreign_keys_and_wal(tmp_path: Path) -> None:
    db_path = tmp_path / "nested" / "archive.sqlite3"

    init_db(db_path)
    connection = connect_database(db_path)
    try:
        assert connection.execute("PRAGMA foreign_keys").fetchone()[0] == 1
        assert connection.execute("PRAGMA journal_mode").fetchone()[0] == "wal"
        assert (
            connection.execute(
                """
                SELECT COUNT(*) FROM sqlite_master
                WHERE type = 'view' AND name = 'version_sightings'
                """
            ).fetchone()[0]
            == 1
        )
    finally:
        connection.close()


@pytest.mark.parametrize(
    ("status", "login_state", "pagination_complete", "rate_limited", "expected"),
    [
        (RunStatus.OK, LoginState.VALID, True, False, True),
        (RunStatus.PARTIAL, LoginState.VALID, True, False, False),
        (RunStatus.FAILED, LoginState.VALID, True, False, False),
        (RunStatus.OK, LoginState.EXPIRED, True, False, False),
        (RunStatus.OK, LoginState.UNKNOWN, True, False, False),
        (RunStatus.OK, LoginState.VALID, False, False, False),
        (RunStatus.OK, LoginState.VALID, True, True, False),
    ],
)
def test_feed_health_gate(
    status: RunStatus,
    login_state: LoginState,
    pagination_complete: bool,
    rate_limited: bool,
    expected: bool,
) -> None:
    run = make_feed_run(
        status=status,
        login_state=login_state,
        pagination_complete=pagination_complete,
        rate_limited=rate_limited,
    )

    assert is_healthy_feed_run(run) is expected


@pytest.mark.parametrize(
    ("status", "login_state", "rate_limited", "expected"),
    [
        (RunStatus.OK, LoginState.VALID, False, True),
        (RunStatus.PARTIAL, LoginState.VALID, False, False),
        (RunStatus.FAILED, LoginState.VALID, False, False),
        (RunStatus.OK, LoginState.EXPIRED, False, False),
        (RunStatus.OK, LoginState.UNKNOWN, False, False),
        (RunStatus.OK, LoginState.VALID, True, False),
    ],
)
def test_probe_health_gate(
    status: RunStatus,
    login_state: LoginState,
    rate_limited: bool,
    expected: bool,
) -> None:
    run = make_probe_run(
        1,
        ProbeResult.NOT_FOUND,
        status=status,
        login_state=login_state,
        rate_limited=rate_limited,
    )

    assert is_healthy_probe_run(run) is expected


def test_evidence_tables_are_append_only_and_post_identity_is_locked(archive: Archive) -> None:
    archive.record_feed_run(make_feed_run(), [make_post()])
    post_id = int(str(scalar(archive, "SELECT id FROM posts")))
    archive.record_probe_run(make_probe_run(post_id, ProbeResult.NOT_FOUND))
    # Seed a post_images row so the append-only triggers have a row to fire on
    # (a BEFORE UPDATE/DELETE trigger does not run when zero rows match).
    version_id = int(str(scalar(archive, "SELECT id FROM post_versions")))
    archive.connection.execute(
        """
        INSERT INTO post_images(
            id, version_id, source_url, normalized_url, ordinal, sha256, mime_type,
            byte_size, image_bytes, downloaded_at, download_status
        ) VALUES (1, ?, ?, ?, 0, ?, 'image/png', 3, ?, ?, 'ok')
        """,
        (
            version_id,
            "https://x/i.png?s=1",
            "https://x/i.png",
            "abc",
            b"png",
            "2026-06-02T00:00:00+00:00",
        ),
    )

    for table in EVIDENCE_TABLES:
        with pytest.raises(sqlite3.IntegrityError, match="append-only"):
            archive.connection.execute(f"UPDATE {table} SET id = id WHERE id = 1")
        with pytest.raises(sqlite3.IntegrityError, match="append-only"):
            archive.connection.execute(f"DELETE FROM {table} WHERE id = 1")

    for column, replacement in [
        ("id", 99),
        ("author_id", 99),
        ("platform", "other"),
        ("platform_post_id", "other"),
        ("first_seen_at", "2026-06-02T00:00:00+00:00"),
    ]:
        with pytest.raises(sqlite3.IntegrityError, match="identity fields"):
            archive.connection.execute(
                f"UPDATE posts SET {column} = ? WHERE id = ?", (replacement, post_id)
            )
    with pytest.raises(sqlite3.IntegrityError, match="cannot be deleted"):
        archive.connection.execute("DELETE FROM posts WHERE id = ?", (post_id,))

    archive.connection.execute(
        "UPDATE posts SET source_checked_at = ? WHERE id = ?",
        ("2026-06-03T00:00:00+00:00", post_id),
    )


def test_a_b_a_creates_three_versions_with_distinct_first_observations(archive: Archive) -> None:
    archive.record_feed_run(make_feed_run(finished_at="2026-06-01T00:00:00+00:00"), [make_post()])
    archive.record_feed_run(
        make_feed_run(finished_at="2026-06-01T01:00:00+00:00"),
        [make_post(observed_at="2026-06-01T01:00:00+00:00", text="B")],
    )
    archive.record_feed_run(
        make_feed_run(finished_at="2026-06-01T02:00:00+00:00"),
        [make_post(observed_at="2026-06-01T02:00:00+00:00", text="A")],
    )

    versions = archive.connection.execute(
        "SELECT content_text, first_observed_at FROM post_versions ORDER BY id"
    ).fetchall()
    assert [(row["content_text"], row["first_observed_at"]) for row in versions] == [
        ("A", "2026-06-01T00:00:00+00:00"),
        ("B", "2026-06-01T01:00:00+00:00"),
        ("A", "2026-06-01T02:00:00+00:00"),
    ]


def test_unchanged_positive_observation_is_throttled_weekly(archive: Archive) -> None:
    archive.record_feed_run(make_feed_run(), [make_post()])
    archive.record_feed_run(
        make_feed_run(finished_at="2026-06-06T00:00:00+00:00"),
        [make_post(observed_at="2026-06-06T00:00:00+00:00")],
    )

    assert scalar(archive, "SELECT COUNT(*) FROM fetch_runs") == 2
    assert scalar(archive, "SELECT COUNT(*) FROM post_observations WHERE present = 1") == 1

    archive.record_feed_run(
        make_feed_run(finished_at="2026-06-08T00:00:00+00:00"),
        [make_post(observed_at="2026-06-08T00:00:00+00:00")],
    )

    assert scalar(archive, "SELECT COUNT(*) FROM post_observations WHERE present = 1") == 2
    assert (
        scalar(archive, "SELECT MAX(observed_at) FROM version_sightings")
        == "2026-06-08T00:00:00+00:00"
    )


def test_unchanged_positive_observation_stops_after_max_count(archive: Archive) -> None:
    for day in ("01", "08", "15", "22", "29"):
        archive.record_feed_run(
            make_feed_run(finished_at=f"2026-06-{day}T00:00:00+00:00"),
            [make_post(observed_at=f"2026-06-{day}T00:00:00+00:00")],
        )
    archive.record_feed_run(
        make_feed_run(finished_at="2026-07-06T00:00:00+00:00"),
        [make_post(observed_at="2026-07-06T00:00:00+00:00")],
    )

    assert scalar(archive, "SELECT COUNT(*) FROM post_observations WHERE present = 1") == 5
    assert scalar(archive, "SELECT COUNT(*) FROM post_versions") == 1


def test_empty_feed_run_does_not_count_as_absence_and_reobservation_stays_throttled(
    archive: Archive,
) -> None:
    # Seeing a post, then a poll that does not include it, then seeing it again within the
    # weekly interval: the empty poll is no longer an absence signal (feed-side inference
    # was retired), so the post stays present and the re-sighting is throttled to one
    # observation rather than being treated as a recovery.
    archive.record_feed_run(make_feed_run(), [make_post()])
    archive.record_feed_run(make_feed_run(finished_at="2026-06-02T00:00:00+00:00"), [])

    archive.record_feed_run(
        make_feed_run(finished_at="2026-06-03T00:00:00+00:00"),
        [make_post(observed_at="2026-06-03T00:00:00+00:00")],
    )

    assert scalar(archive, "SELECT feed_state FROM posts") == FeedState.PRESENT
    assert scalar(archive, "SELECT absent_healthy_streak FROM posts") == 0
    assert scalar(archive, "SELECT COUNT(*) FROM post_observations WHERE present = 0") == 0
    assert scalar(archive, "SELECT COUNT(*) FROM post_observations WHERE present = 1") == 1


def test_preview_observation_does_not_create_version_or_content_event(archive: Archive) -> None:
    archive.record_feed_run(
        make_feed_run(),
        [make_post(fidelity=ContentFidelity.PREVIEW)],
    )

    assert scalar(archive, "SELECT COUNT(*) FROM post_versions") == 0
    assert scalar(archive, "SELECT COUNT(*) FROM post_events WHERE dimension = 'content'") == 0
    observation = archive.connection.execute(
        "SELECT content_hash, version_id FROM post_observations"
    ).fetchone()
    assert observation is not None
    assert observation["content_hash"] is None
    assert observation["version_id"] is None


def test_feed_no_longer_infers_absence_however_many_polls_miss_the_post(
    archive: Archive,
) -> None:
    # An incremental feed poll covers only new content, so a known post being missing is
    # not evidence of deletion. However many healthy polls do not include it, the post
    # stays present, no present=false observation is written, and nothing is enqueued —
    # deletion confirmation is the direct-recheck lifecycle's job (Track B) now.
    archive.record_feed_run(make_feed_run(), [make_post()])

    for hour in (1, 2, 3, 4):
        archive.record_feed_run(make_feed_run(finished_at=f"2026-06-01T0{hour}:00:00+00:00"), [])
        assert scalar(archive, "SELECT feed_state FROM posts") == FeedState.PRESENT

    assert scalar(archive, "SELECT absent_healthy_streak FROM posts") == 0
    assert scalar(archive, "SELECT COUNT(*) FROM post_observations WHERE present = 0") == 0
    assert scalar(archive, "SELECT COUNT(*) FROM recheck_queue WHERE state = 'pending'") == 0


def test_partial_run_keeps_positive_archive_and_blocks_all_negative_inference(
    archive: Archive,
) -> None:
    archive.record_feed_run(make_feed_run(), [make_post("seen"), make_post("missing")])

    archive.record_feed_run(
        make_feed_run(
            finished_at="2026-06-01T01:00:00+00:00",
            status=RunStatus.PARTIAL,
        ),
        [make_post("seen", observed_at="2026-06-01T01:00:00+00:00", text="B")],
    )

    missing = archive.connection.execute(
        "SELECT absent_healthy_streak FROM posts WHERE platform_post_id = 'missing'"
    ).fetchone()
    assert missing is not None
    assert missing["absent_healthy_streak"] == 0
    assert scalar(archive, "SELECT COUNT(*) FROM post_versions WHERE content_text = 'B'") == 1


def test_parse_failure_degrades_seen_post_and_blocks_other_negative_inference(
    archive: Archive,
) -> None:
    archive.record_feed_run(make_feed_run(), [make_post("degraded"), make_post("other")])
    for hour in (1, 2, 3):
        archive.record_feed_run(
            make_feed_run(finished_at=f"2026-06-01T0{hour}:00:00+00:00"),
            [make_post("other", observed_at=f"2026-06-01T0{hour}:00:00+00:00")],
        )
    prior_version_id = scalar(
        archive, "SELECT current_version_id FROM posts WHERE platform_post_id = 'degraded'"
    )

    fetch_run_id = archive.record_feed_run(
        make_feed_run(
            finished_at="2026-06-01T04:00:00+00:00",
            parse_failure_count=1,
        ),
        [
            make_post(
                "degraded",
                observed_at="2026-06-01T04:00:00+00:00",
                fidelity=ContentFidelity.NA,
            )
        ],
    )

    degraded = archive.connection.execute(
        """
        SELECT feed_state, absent_healthy_streak, last_present_at, current_version_id
        FROM posts WHERE platform_post_id = 'degraded'
        """
    ).fetchone()
    assert degraded is not None
    assert degraded["feed_state"] == FeedState.PRESENT
    assert degraded["absent_healthy_streak"] == 0
    assert degraded["last_present_at"] == "2026-06-01T04:00:00+00:00"
    assert degraded["current_version_id"] == prior_version_id
    assert (
        scalar(archive, "SELECT status FROM fetch_runs WHERE id = ?", (fetch_run_id,))
        == RunStatus.PARTIAL
    )
    assert (
        scalar(
            archive,
            """
            SELECT COUNT(*) FROM post_observations o
            JOIN posts p ON p.id = o.post_id
            WHERE o.fetch_run_id = ? AND p.platform_post_id = 'other'
            """,
            (fetch_run_id,),
        )
        == 0
    )


def test_run_log_contains_health_metadata_without_notes(
    archive: Archive, caplog: pytest.LogCaptureFixture
) -> None:
    run = replace(make_feed_run(), notes="cookie=secret-value")

    with caplog.at_level(logging.INFO, logger="kol_archive.archive"):
        archive.record_feed_run(run, [make_post()])

    assert "feed_run archived run_id=1 author_id=1 status=ok healthy=True" in caplog.text
    assert "cookie" not in caplog.text
    assert "secret-value" not in caplog.text


def test_gone_confirmed_is_sticky_until_reachable(archive: Archive) -> None:
    archive.record_feed_run(make_feed_run(), [make_post()])
    post_id = int(str(scalar(archive, "SELECT id FROM posts")))

    archive.record_probe_run(make_probe_run(post_id, ProbeResult.EXPLICITLY_REMOVED))
    archive.record_probe_run(make_probe_run(post_id, ProbeResult.NOT_FOUND))
    archive.record_probe_run(make_probe_run(post_id, ProbeResult.RESTRICTED))
    assert scalar(archive, "SELECT source_state FROM posts") == SourceState.GONE_CONFIRMED

    observed = make_post(observed_at="2026-06-02T00:00:00+00:00", text="B")
    archive.record_probe_run(
        make_probe_run(
            post_id,
            ProbeResult.REACHABLE,
            observed_at=observed.observed_at,
            fidelity=ContentFidelity.FULL,
        ),
        observed,
    )
    assert scalar(archive, "SELECT source_state FROM posts") == SourceState.REACHABLE
    assert scalar(archive, "SELECT COUNT(*) FROM post_versions") == 2


def test_degraded_probe_keeps_source_projection_and_checked_time_unchanged(
    archive: Archive,
) -> None:
    archive.record_feed_run(make_feed_run(), [make_post()])
    post_id = int(str(scalar(archive, "SELECT id FROM posts")))
    archive.record_probe_run(
        make_probe_run(
            post_id,
            ProbeResult.REACHABLE,
            observed_at="2026-06-01T01:00:00+00:00",
            fidelity=ContentFidelity.FULL,
        ),
        make_post(observed_at="2026-06-01T01:00:00+00:00"),
    )

    archive.record_probe_run(
        make_probe_run(
            post_id,
            ProbeResult.NOT_FOUND,
            observed_at="2026-06-01T02:00:00+00:00",
            login_state=LoginState.EXPIRED,
        )
    )

    row = archive.connection.execute(
        "SELECT source_state, source_checked_at FROM posts WHERE id = ?", (post_id,)
    ).fetchone()
    assert row is not None
    assert row["source_state"] == SourceState.REACHABLE
    assert row["source_checked_at"] == "2026-06-01T01:00:00+00:00"
    assert scalar(archive, "SELECT COUNT(*) FROM probe_runs") == 2


def test_probe_rejects_mismatched_observation_identity(archive: Archive) -> None:
    archive.record_feed_run(make_feed_run(), [make_post()])
    post_id = int(str(scalar(archive, "SELECT id FROM posts")))

    with pytest.raises(ValueError, match="identity"):
        archive.record_probe_run(
            make_probe_run(post_id, ProbeResult.REACHABLE, fidelity=ContentFidelity.FULL),
            make_post("different"),
        )

    assert scalar(archive, "SELECT COUNT(*) FROM probe_runs") == 0


def test_only_one_pending_recheck_is_allowed_per_post(archive: Archive) -> None:
    archive.record_feed_run(make_feed_run(), [make_post()])
    post_id = int(str(scalar(archive, "SELECT id FROM posts")))

    archive.enqueue_recheck(
        post_id,
        QueueReason.LLM_CANDIDATE,
        BASE_TIME,
        "2026-06-08T00:00:00+00:00",
    )
    archive.enqueue_recheck(
        post_id,
        QueueReason.RECENT_FEED_ABSENT,
        BASE_TIME,
        "2026-06-08T00:00:00+00:00",
    )

    assert scalar(archive, "SELECT COUNT(*) FROM recheck_queue WHERE state = 'pending'") == 1


def test_pin_confirms_selected_queue_and_expiry_closes_stale_queue(archive: Archive) -> None:
    archive.record_feed_run(make_feed_run(), [make_post("confirm"), make_post("expire")])
    confirm_id = int(
        str(scalar(archive, "SELECT id FROM posts WHERE platform_post_id = 'confirm'"))
    )
    expire_id = int(str(scalar(archive, "SELECT id FROM posts WHERE platform_post_id = 'expire'")))
    archive.enqueue_recheck(
        confirm_id, QueueReason.LLM_CANDIDATE, BASE_TIME, "2026-06-08T00:00:00+00:00"
    )
    archive.enqueue_recheck(
        expire_id, QueueReason.LLM_CANDIDATE, BASE_TIME, "2026-06-02T00:00:00+00:00"
    )

    archive.pin_post(
        confirm_id, "2026-06-01T01:00:00+00:00", confirm_reason=QueueReason.LLM_CANDIDATE
    )
    expired = archive.expire_rechecks("2026-06-03T00:00:00+00:00")

    assert expired == 1
    states = archive.connection.execute(
        "SELECT post_id, state FROM recheck_queue ORDER BY post_id"
    ).fetchall()
    assert [(row["post_id"], row["state"]) for row in states] == [
        (confirm_id, "confirmed"),
        (expire_id, "expired"),
    ]


def test_feed_crash_rolls_back_evidence_and_projection(archive: Archive) -> None:
    with pytest.raises(RuntimeError, match="injected crash"):
        archive.record_feed_run(
            make_feed_run(),
            [make_post()],
            crash_after_evidence=True,
        )

    assert scalar(archive, "SELECT COUNT(*) FROM fetch_runs") == 0
    assert scalar(archive, "SELECT COUNT(*) FROM posts") == 0
    assert scalar(archive, "SELECT COUNT(*) FROM post_versions") == 0
    assert scalar(archive, "SELECT COUNT(*) FROM post_observations") == 0
    assert scalar(archive, "SELECT COUNT(*) FROM post_events") == 0


def test_probe_crash_rolls_back_evidence_and_projection(archive: Archive) -> None:
    archive.record_feed_run(make_feed_run(), [make_post()])
    post_id = int(str(scalar(archive, "SELECT id FROM posts")))

    with pytest.raises(RuntimeError, match="injected crash"):
        archive.record_probe_run(
            make_probe_run(
                post_id,
                ProbeResult.REACHABLE,
                observed_at="2026-06-01T01:00:00+00:00",
                fidelity=ContentFidelity.FULL,
            ),
            make_post(observed_at="2026-06-01T01:00:00+00:00", text="B"),
            crash_after_evidence=True,
        )

    assert scalar(archive, "SELECT COUNT(*) FROM probe_runs") == 0
    assert scalar(archive, "SELECT COUNT(*) FROM post_versions") == 1
    assert scalar(archive, "SELECT current_content_hash FROM posts") == "hash-A"


def test_version_sightings_combines_feed_and_direct_observations(archive: Archive) -> None:
    archive.record_feed_run(make_feed_run(), [make_post()])
    post_id = int(str(scalar(archive, "SELECT id FROM posts")))
    archive.record_probe_run(
        make_probe_run(
            post_id,
            ProbeResult.REACHABLE,
            observed_at="2026-06-01T01:00:00+00:00",
            fidelity=ContentFidelity.FULL,
        ),
        make_post(observed_at="2026-06-01T01:00:00+00:00"),
    )

    sightings = archive.connection.execute(
        "SELECT channel, observed_at FROM version_sightings ORDER BY observed_at"
    ).fetchall()
    assert [(row["channel"], row["observed_at"]) for row in sightings] == [
        ("feed", "2026-06-01T00:00:00+00:00"),
        ("direct", "2026-06-01T01:00:00+00:00"),
    ]


def test_recheck_lifecycle_retires_recent_window_post_on_gone_but_keeps_pinned(
    archive: Archive,
) -> None:
    archive.record_feed_run(make_feed_run(), [make_post("gone"), make_post("kept")])
    gone_id = int(str(scalar(archive, "SELECT id FROM posts WHERE platform_post_id = 'gone'")))
    kept_id = int(str(scalar(archive, "SELECT id FROM posts WHERE platform_post_id = 'kept'")))
    archive.pin_post(kept_id, "2026-06-01T01:00:00+00:00")

    archive.record_probe_run(make_probe_run(gone_id, ProbeResult.EXPLICITLY_REMOVED))
    archive.record_probe_run(make_probe_run(kept_id, ProbeResult.EXPLICITLY_REMOVED))

    rows = archive.connection.execute(
        "SELECT platform_post_id, feed_state, watch_mode, source_state "
        "FROM posts ORDER BY platform_post_id"
    ).fetchall()
    assert [
        (row["platform_post_id"], row["feed_state"], row["watch_mode"], row["source_state"])
        for row in rows
    ] == [
        # A confirmed-gone recent_window post is retired: archived, monitoring stopped.
        ("gone", FeedState.OUT_OF_SCOPE, WatchMode.INACTIVE, SourceState.GONE_CONFIRMED),
        # A pinned post is a manual override and is never auto-retired.
        ("kept", FeedState.PRESENT, WatchMode.PINNED, SourceState.GONE_CONFIRMED),
    ]


def test_recheck_lifecycle_retires_recent_window_post_after_max_rechecks(
    archive: Archive,
) -> None:
    archive.record_feed_run(make_feed_run(), [make_post()])
    post_id = int(str(scalar(archive, "SELECT id FROM posts")))

    # Five healthy rechecks is the configured budget (positive_observation_max_count).
    for day in ("02", "09", "16", "23"):
        archive.record_probe_run(
            make_probe_run(
                post_id, ProbeResult.NOT_FOUND, observed_at=f"2026-06-{day}T00:00:00+00:00"
            )
        )
        assert scalar(archive, "SELECT watch_mode FROM posts") == WatchMode.RECENT_WINDOW

    archive.record_probe_run(
        make_probe_run(post_id, ProbeResult.NOT_FOUND, observed_at="2026-06-30T00:00:00+00:00")
    )

    row = archive.connection.execute(
        "SELECT feed_state, watch_mode FROM posts WHERE id = ?", (post_id,)
    ).fetchone()
    assert tuple(row) == (FeedState.OUT_OF_SCOPE, WatchMode.INACTIVE)


def test_lifecycle_first_recheck_is_due_one_day_after_the_post(archive: Archive) -> None:
    archive.record_feed_run(
        make_feed_run(), [make_post(posted_at_claimed="2026-05-20T00:00:00+00:00")]
    )
    post_id = int(str(scalar(archive, "SELECT id FROM posts")))

    # Default first_recheck_after_days=1: nothing is due before 2026-05-21.
    assert archive.probe_targets("2026-05-20T12:00:00+00:00") == []
    targets = archive.probe_targets("2026-05-21T00:00:00+00:00")
    assert [target.post_id for target in targets] == [post_id]


def test_lifecycle_subsequent_recheck_waits_one_interval_after_last_probe(
    archive: Archive,
) -> None:
    archive.record_feed_run(
        make_feed_run(), [make_post(posted_at_claimed="2026-05-20T00:00:00+00:00")]
    )
    post_id = int(str(scalar(archive, "SELECT id FROM posts")))
    archive.record_probe_run(
        make_probe_run(post_id, ProbeResult.NOT_FOUND, observed_at="2026-05-21T00:00:00+00:00")
    )

    # After the first probe the clock restarts at the 7-day interval from that probe.
    assert archive.probe_targets("2026-05-24T00:00:00+00:00") == []
    targets = archive.probe_targets("2026-05-28T00:00:00+00:00")
    assert [target.post_id for target in targets] == [post_id]


def test_lifecycle_due_posts_need_as_of_unlike_pinned_targets(archive: Archive) -> None:
    archive.record_feed_run(
        make_feed_run(), [make_post(posted_at_claimed="2026-05-20T00:00:00+00:00")]
    )
    post_id = int(str(scalar(archive, "SELECT id FROM posts")))

    # No pin and no queue row: the post is only reachable through the lifecycle, which
    # requires an as_of clock. The always-on pinned/queued targets do not.
    assert archive.probe_targets() == []
    assert [target.post_id for target in archive.probe_targets("2026-06-01T00:00:00+00:00")] == [
        post_id
    ]


def test_probe_targets_limit_drains_most_overdue_lifecycle_posts_first(archive: Archive) -> None:
    archive.record_feed_run(
        make_feed_run(),
        [
            make_post("old", text="old", posted_at_claimed="2026-05-10T00:00:00+00:00"),
            make_post("new", text="new", posted_at_claimed="2026-05-13T00:00:00+00:00"),
            make_post("mid", text="mid", posted_at_claimed="2026-05-11T00:00:00+00:00"),
        ],
    )

    # All three are lifecycle-due, but a budget of 2 takes the two oldest-posted first;
    # the newest waits for a later run.
    targets = archive.probe_targets("2026-06-01T00:00:00+00:00", limit=2)
    assert [target.platform_post_id for target in targets] == ["old", "mid"]


def test_probe_targets_limit_keeps_pinned_ahead_of_lifecycle_backlog(archive: Archive) -> None:
    archive.record_feed_run(
        make_feed_run(),
        [
            make_post("life", text="life", posted_at_claimed="2026-05-10T00:00:00+00:00"),
            make_post("pin", text="pin", posted_at_claimed="2026-05-09T00:00:00+00:00"),
        ],
    )
    pin_id = int(str(scalar(archive, "SELECT id FROM posts WHERE platform_post_id = 'pin'")))
    archive.pin_post(pin_id, "2026-06-01T01:00:00+00:00")

    # The single slot goes to the pinned post even though the lifecycle post is also due.
    targets = archive.probe_targets("2026-06-01T02:00:00+00:00", limit=1)
    assert [target.platform_post_id for target in targets] == ["pin"]
