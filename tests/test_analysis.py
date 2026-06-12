from __future__ import annotations

import sqlite3
from pathlib import Path
from typing import cast

from kol_archive.analysis import (
    AnalysisSettings,
    list_crowding_events,
    load_analysis_settings,
    post_ticker_history,
    selective_deletion_analysis,
    stage_crowding_events,
)
from kol_archive.database import connect_database, initialize_database

NOW = "2026-06-12T00:00:00+00:00"


def _lastrowid(cursor: sqlite3.Cursor) -> int:
    assert cursor.lastrowid is not None
    return cursor.lastrowid


def _connection(tmp_path: Path) -> sqlite3.Connection:
    connection = connect_database(tmp_path / "analysis.sqlite3")
    initialize_database(connection)
    return connection


def _claim(
    connection: sqlite3.Connection,
    uid: str,
    ticker: str,
    direction: str,
    made_at: str,
    *,
    excess_return: float | None = None,
    removed: bool = False,
) -> tuple[int, int, int]:
    existing = connection.execute(
        "SELECT id FROM authors WHERE platform = 'xueqiu' AND platform_uid = ?", (uid,)
    ).fetchone()
    author_id = (
        int(existing["id"])
        if existing is not None
        else _lastrowid(
            connection.execute(
                """
                INSERT INTO authors(platform, platform_uid, live_monitoring_started_at, notes)
                VALUES ('xueqiu', ?, '2026-01-01T00:00:00+00:00', ?)
                """,
                (uid, f"作者 {uid}"),
            )
        )
    )
    post_id = _lastrowid(
        connection.execute(
            """
            INSERT INTO posts(
                author_id, platform, platform_post_id, first_seen_at, feed_state,
                source_state, watch_mode, ingest_mode
            ) VALUES (?, 'xueqiu', ?, ?, 'present', ?, 'recent_window', 'live')
            """,
            (
                author_id,
                f"post-{uid}-{made_at}",
                made_at,
                "gone_confirmed" if removed else "reachable",
            ),
        )
    )
    version_id = _lastrowid(
        connection.execute(
            """
            INSERT INTO post_versions(
                post_id, content_text, content_hash, first_observed_at, ingest_mode, raw_payload
            ) VALUES (?, ?, ?, ?, 'live', '{}')
            """,
            (post_id, f"观点 {ticker}", f"hash-{uid}", made_at),
        )
    )
    connection.execute(
        "INSERT INTO version_tickers(version_id, ticker) VALUES (?, ?)", (version_id, ticker)
    )
    if removed:
        connection.execute(
            """
            INSERT INTO post_events(post_id, dimension, from_value, to_value, detected_at)
            VALUES (?, 'source_state', 'reachable', 'gone_confirmed', ?)
            """,
            (post_id, made_at),
        )
    claim_id = _lastrowid(
        connection.execute(
            """
            INSERT INTO claims(
                post_id, version_id, author_id, ticker, direction, horizon_days,
                claim_made_at, ingest_mode, status, created_at
            ) VALUES (?, ?, ?, ?, ?, 30, ?, 'live', ?, ?)
            """,
            (
                post_id,
                version_id,
                author_id,
                ticker,
                direction,
                made_at,
                "resolved" if excess_return is not None else "open",
                made_at,
            ),
        )
    )
    if excess_return is not None:
        connection.execute(
            """
            INSERT INTO claim_outcomes(
                claim_id, resolved_at, raw_return, benchmark_return, excess_return,
                benchmark_ticker, outcome_method_version
            ) VALUES (?, ?, ?, 0.0, ?, 'SH000300', 'descriptive-common-close-v1')
            """,
            (claim_id, made_at, excess_return, excess_return),
        )
    return post_id, version_id, claim_id


def test_selective_deletion_analysis_is_neutral_and_sample_gated(tmp_path: Path) -> None:
    connection = _connection(tmp_path)
    try:
        _claim(connection, "one", "SH688303", "long", NOW, excess_return=-0.1, removed=True)
        _claim(
            connection,
            "one",
            "SH688303",
            "long",
            "2026-06-13T00:00:00+00:00",
            excess_return=0.2,
        )

        insufficient = selective_deletion_analysis(connection, 2)
        comparable = selective_deletion_analysis(connection, 1)

        assert {item["comparison_label"] for item in insufficient} == {"样本不足"}
        assert {item["comparison_label"] for item in comparable} == {"分布差异可供比较"}
        assert all("改口" not in str(item) for item in insufficient)
    finally:
        connection.close()


def test_selective_deletion_ignores_removal_after_claim_resolution(tmp_path: Path) -> None:
    connection = _connection(tmp_path)
    try:
        post_id, _, _ = _claim(connection, "one", "SH688303", "long", NOW, excess_return=0.1)
        connection.execute(
            """
            INSERT INTO post_events(post_id, dimension, from_value, to_value, detected_at)
            VALUES (?, 'source_state', 'reachable', 'gone_confirmed', '2026-07-12T00:00:00+00:00')
            """,
            (post_id,),
        )

        [result] = selective_deletion_analysis(connection, 1)

        assert cast(dict[str, object], result["removed"])["sample_count"] == 0
        assert cast(dict[str, object], result["retained"])["sample_count"] == 1
    finally:
        connection.close()


def test_crowding_events_are_append_only_idempotent_and_drillable(tmp_path: Path) -> None:
    connection = _connection(tmp_path)
    try:
        for index, day in enumerate((10, 11, 12, 13, 14), start=1):
            _claim(
                connection,
                str(index),
                "SH688303",
                "long",
                f"2026-06-{day:02d}T00:00:00+00:00",
            )
        settings = AnalysisSettings(crowding_min_authors=3, crowding_window_days=7)

        assert stage_crowding_events(connection, settings, NOW) == 1
        assert stage_crowding_events(connection, settings, NOW) == 0
        _claim(connection, "six", "SH688303", "long", "2026-06-20T00:00:00+00:00")
        assert stage_crowding_events(connection, settings, NOW) == 0
        selects: list[str] = []
        connection.set_trace_callback(
            lambda statement: (
                selects.append(statement)
                if statement.lstrip().upper().startswith("SELECT")
                else None
            )
        )
        [event] = list_crowding_events(connection)
        connection.set_trace_callback(None)

        assert event["ticker"] == "SH688303"
        assert event["author_count"] == 3
        assert len(cast(list[object], event["members"])) == 3
        assert len(selects) == 2
        for table in ("crowding_events", "crowding_event_members"):
            try:
                connection.execute(f"DELETE FROM {table}")
            except sqlite3.IntegrityError as error:
                assert "append-only" in str(error)
            else:
                raise AssertionError(f"{table} accepted DELETE")
    finally:
        connection.close()


def test_crowding_event_and_members_roll_back_together(tmp_path: Path) -> None:
    connection = _connection(tmp_path)
    try:
        for index, day in enumerate((10, 11), start=1):
            _claim(
                connection,
                str(index),
                "SH688303",
                "long",
                f"2026-06-{day:02d}T00:00:00+00:00",
            )
        connection.execute(
            """
            CREATE TRIGGER fail_crowding_member BEFORE INSERT ON crowding_event_members
            BEGIN SELECT RAISE(ABORT, 'injected member failure'); END
            """
        )

        try:
            stage_crowding_events(
                connection,
                AnalysisSettings(crowding_min_authors=2),
                NOW,
            )
        except sqlite3.IntegrityError as error:
            assert "injected member failure" in str(error)
        else:
            raise AssertionError("injected crowding member failure did not fire")
        assert connection.execute("SELECT COUNT(*) FROM crowding_events").fetchone()[0] == 0
    finally:
        connection.close()


def test_analysis_settings_reject_explicit_zero_values() -> None:
    for key in ("min_group_samples", "crowding_min_authors", "crowding_window_days"):
        try:
            load_analysis_settings({"analysis": {key: 0}})
        except ValueError:
            pass
        else:
            raise AssertionError(f"analysis.{key}=0 was accepted")


def test_analysis_template_hides_distribution_metrics_when_samples_are_insufficient() -> None:
    template = Path("frontend/src/App.vue").read_text(encoding="utf-8")

    assert 'v-if="item.sufficient_samples"' in template
    assert "${item.benchmark_ticker}-${item.outcome_method_version}" in template
    assert "样本不足" not in template
    assert "改口" not in template


def test_post_ticker_history_uses_index_and_includes_removed_versions(tmp_path: Path) -> None:
    connection = _connection(tmp_path)
    try:
        post_id, _, _ = _claim(connection, "one", "SH688303", "long", NOW)
        author_id = connection.execute(
            "SELECT author_id FROM posts WHERE id = ?", (post_id,)
        ).fetchone()[0]
        prior_post_id = _lastrowid(
            connection.execute(
                """
                INSERT INTO posts(
                    author_id, platform, platform_post_id, first_seen_at, feed_state,
                    source_state, watch_mode, ingest_mode
                ) VALUES (?, 'xueqiu', 'prior', '2026-05-01T00:00:00+00:00',
                    'absent_confirmed', 'gone_confirmed', 'pinned', 'live')
                """,
                (author_id,),
            )
        )
        prior_version_id = _lastrowid(
            connection.execute(
                """
                INSERT INTO post_versions(
                    post_id, content_text, content_hash, first_observed_at, ingest_mode, raw_payload
                ) VALUES (
                    ?, '旧观点 SH688303', 'old-hash',
                    '2026-05-01T00:00:00+00:00', 'live', '{}'
                )
                """,
                (prior_post_id,),
            )
        )
        connection.execute(
            "INSERT INTO version_tickers(version_id, ticker) VALUES (?, 'SH688303')",
            (prior_version_id,),
        )
        edited_version_id = _lastrowid(
            connection.execute(
                """
                INSERT INTO post_versions(
                    post_id, content_text, content_hash, first_observed_at, ingest_mode, raw_payload
                ) VALUES (
                    ?, '旧观点编辑 SH688303', 'edited-hash',
                    '2026-05-02T00:00:00+00:00', 'live', '{}'
                )
                """,
                (prior_post_id,),
            )
        )
        connection.execute(
            "INSERT INTO version_tickers(version_id, ticker) VALUES (?, 'SH688303')",
            (edited_version_id,),
        )
        connection.execute(
            """
            INSERT INTO post_events(post_id, dimension, from_value, to_value, detected_at)
            VALUES (?, 'source_state', 'reachable', 'gone_confirmed', '2026-05-02T00:00:00+00:00')
            """,
            (prior_post_id,),
        )
        selects: list[str] = []
        connection.set_trace_callback(
            lambda statement: (
                selects.append(statement)
                if statement.lstrip().upper().startswith("SELECT")
                else None
            )
        )

        history = post_ticker_history(connection, post_id, "enrich-v1", "SH000300")

        connection.set_trace_callback(None)
        items = cast(list[dict[str, object]], history["items"])
        assert len(items) == 3
        assert any(bool(item["has_removal_event"]) for item in items)
        assert len([item for item in items if item["events"]]) == 1
        assert all("json_tree" not in query and " GLOB " not in query for query in selects)
        plan = connection.execute(
            """
            EXPLAIN QUERY PLAN
            SELECT version_id FROM version_tickers WHERE ticker = 'SH688303'
            """
        ).fetchall()
        assert "idx_version_tickers_ticker" in " ".join(str(row["detail"]) for row in plan)
    finally:
        connection.close()


def test_initialize_backfills_version_tickers_without_prefix_false_positive(tmp_path: Path) -> None:
    connection = _connection(tmp_path)
    try:
        author_id = _lastrowid(
            connection.execute(
                """
                INSERT INTO authors(platform, platform_uid, live_monitoring_started_at)
                VALUES ('xueqiu', 'one', ?)
                """,
                (NOW,),
            )
        )
        post_id = _lastrowid(
            connection.execute(
                """
                INSERT INTO posts(
                    author_id, platform, platform_post_id, first_seen_at, feed_state,
                    source_state, watch_mode, ingest_mode
                ) VALUES (?, 'xueqiu', 'post', ?, 'present', 'reachable', 'recent_window', 'live')
                """,
                (author_id, NOW),
            )
        )
        connection.execute(
            """
            INSERT INTO post_versions(
                post_id, content_text, content_hash, first_observed_at, ingest_mode, raw_payload
            ) VALUES (?, 'SH688303 与 SH6883030', 'hash', ?, 'live', '{}')
            """,
            (post_id, NOW),
        )

        initialize_database(connection)

        assert [
            row["ticker"]
            for row in connection.execute("SELECT ticker FROM version_tickers").fetchall()
        ] == ["SH688303"]
        assert connection.execute("SELECT COUNT(*) FROM version_ticker_scans").fetchone()[0] == 1
    finally:
        connection.close()
