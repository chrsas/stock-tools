from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from kol_archive.database import connect_database, initialize_database
from kol_archive.watchlist import (
    add_watchlist_ticker,
    extract_market_tickers,
    list_watchlist,
    mark_watchlist_alert_sent,
    pending_watchlist_alerts,
    remove_watchlist_ticker,
    stage_watchlist_alerts,
    validated_watchlist_ticker,
    watchlist_match_link,
    watchlist_match_title,
)

START = "2026-06-12T00:00:00+00:00"
AFTER_START = "2026-06-12T01:00:00+00:00"


def _lastrowid(cursor: sqlite3.Cursor) -> int:
    assert cursor.lastrowid is not None
    return cursor.lastrowid


def _connection(tmp_path: Path) -> sqlite3.Connection:
    connection = connect_database(tmp_path / "watchlist.sqlite3")
    initialize_database(connection)
    return connection


def _version(
    connection: sqlite3.Connection,
    *,
    observed_at: str = AFTER_START,
    text: str = "关注 SH688303",
    raw_payload: str = '{"user":{"screen_name":"测试作者"}}',
    ingest_mode: str = "live",
) -> tuple[int, int, int]:
    author_id = _lastrowid(
        connection.execute(
            """
            INSERT INTO authors(platform, platform_uid, live_monitoring_started_at, notes)
            VALUES ('xueqiu', 'author-1', ?, '作者备注')
            """,
            (START,),
        )
    )
    post_id = _lastrowid(
        connection.execute(
            """
            INSERT INTO posts(
                author_id, platform, platform_post_id, first_seen_at, feed_state,
                source_state, watch_mode, ingest_mode
            ) VALUES (?, 'xueqiu', 'post-1', ?, 'present', 'reachable', 'recent_window', 'live')
            """,
            (author_id, observed_at),
        )
    )
    version_id = _lastrowid(
        connection.execute(
            """
            INSERT INTO post_versions(
                post_id, content_text, content_hash, first_observed_at, ingest_mode, raw_payload
            ) VALUES (?, ?, 'hash', ?, ?, ?)
            """,
            (post_id, text, observed_at, ingest_mode, raw_payload),
        )
    )
    connection.execute(
        "UPDATE posts SET current_version_id = ?, current_content_hash = 'hash' WHERE id = ?",
        (version_id, post_id),
    )
    return author_id, post_id, version_id


def test_watchlist_add_update_list_and_remove(tmp_path: Path) -> None:
    connection = _connection(tmp_path)
    try:
        add_watchlist_ticker(connection, "sh688303", START, name="大全能源", note="第一条备注")
        add_watchlist_ticker(connection, "SH688303", AFTER_START, note="更新备注")

        assert list_watchlist(connection) == [
            {
                "ticker": "SH688303",
                "name": "大全能源",
                "added_at": START,
                "note": "更新备注",
                "alert_count": 0,
            }
        ]
        add_watchlist_ticker(connection, "SH688303", AFTER_START, name="更新名称")
        assert list_watchlist(connection)[0]["note"] == "更新备注"
        assert list_watchlist(connection)[0]["name"] == "更新名称"
        assert remove_watchlist_ticker(connection, "sh688303") is True
        assert remove_watchlist_ticker(connection, "SH688303") is False
        with pytest.raises(ValueError, match="A-share"):
            validated_watchlist_ticker("AAPL")
    finally:
        connection.close()


def test_extract_market_tickers_from_text_and_stock_correlation() -> None:
    assert extract_market_tickers(
        "正文提到 SH688303",
        '{"stockCorrelation":[{"symbol":"SZ000001"}],"nested":{"stockCorrelation":["BJ430047"]}}',
    ) == {"SH688303", "SZ000001", "BJ430047"}
    assert extract_market_tickers("SH688303", "{invalid") == {"SH688303"}
    assert extract_market_tickers("SH6883030 XSH688303 SH688303A", None) == set()


def test_watchlist_alerts_are_staged_once_and_keep_body_private(tmp_path: Path) -> None:
    connection = _connection(tmp_path)
    try:
        _, post_id, version_id = _version(connection, text="正文包含秘密信息 SH688303")
        add_watchlist_ticker(connection, "SH688303", START)

        assert stage_watchlist_alerts(connection, START, AFTER_START) == 1
        assert stage_watchlist_alerts(connection, START, AFTER_START) == 0
        [match] = pending_watchlist_alerts(connection)
        title = watchlist_match_title(match)
        link = watchlist_match_link("http://127.0.0.1:8765/", match)

        assert match.version_id == version_id
        assert title == "关注标的命中：测试作者 · SH688303"
        assert "秘密信息" not in title
        assert link == f"http://127.0.0.1:8765/posts/{post_id}"
        mark_watchlist_alert_sent(connection, match.alert_id, AFTER_START)
        assert pending_watchlist_alerts(connection) == []
        assert list_watchlist(connection)[0]["alert_count"] == 1
    finally:
        connection.close()


def test_pending_alert_marks_author_with_resolved_claim(tmp_path: Path) -> None:
    connection = _connection(tmp_path)
    try:
        author_id, post_id, version_id = _version(connection)
        add_watchlist_ticker(connection, "SH688303", START)
        claim_id = _lastrowid(
            connection.execute(
                """
                INSERT INTO claims(
                    post_id, version_id, author_id, ticker, direction, claim_made_at,
                    ingest_mode, status, created_at
                ) VALUES (?, ?, ?, 'SH688303', 'long', ?, 'live', 'resolved', ?)
                """,
                (post_id, version_id, author_id, START, START),
            )
        )
        connection.execute(
            """
            INSERT INTO claim_outcomes(
                claim_id, resolved_at, raw_return, benchmark_ticker, benchmark_return,
                excess_return, outcome_method_version
            ) VALUES (?, ?, 0.1, 'SH000300', 0.02, 0.08, 'test-v1')
            """,
            (claim_id, AFTER_START),
        )

        assert stage_watchlist_alerts(connection, START, AFTER_START) == 1
        [match] = pending_watchlist_alerts(connection)
        assert match.has_resolved_claim is True
        assert watchlist_match_title(match).endswith("该作者此标的有已结算记录")
    finally:
        connection.close()


def test_old_or_backfill_versions_do_not_stage(tmp_path: Path) -> None:
    connection = _connection(tmp_path)
    try:
        _version(connection, observed_at=START, ingest_mode="backfill")
        add_watchlist_ticker(connection, "SH688303", START)

        assert stage_watchlist_alerts(connection, AFTER_START, AFTER_START) == 0
    finally:
        connection.close()
