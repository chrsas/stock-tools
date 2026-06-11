from __future__ import annotations

import sqlite3
from collections.abc import Iterator
from pathlib import Path
from typing import cast

import pytest

from kol_archive.database import connect_database, initialize_database
from kol_archive.decisions import common_close_outcome, list_decisions
from kol_archive.market import OUTCOME_METHOD_VERSION
from kol_archive.service import Archive

DECIDED_AT = "2026-06-01T00:00:00+00:00"


@pytest.fixture
def archive(tmp_path: Path) -> Iterator[Archive]:
    connection = connect_database(tmp_path / "archive.sqlite3")
    initialize_database(connection)
    try:
        yield Archive(connection)
    finally:
        connection.close()


def test_decision_requires_invalidation_and_locks_thesis(archive: Archive) -> None:
    with pytest.raises(ValueError, match="invalidation condition"):
        archive.add_decision("SH688303", "long", "观察论点", " ", DECIDED_AT)

    decision_id = archive.add_decision(
        "SH688303",
        "neutral",
        "观察论点",
        "收入增速低于预期",
        DECIDED_AT,
        horizon_days=7,
    )
    archive.close_decision(decision_id, "closed", "2026-06-08T00:00:00+00:00")
    row = archive.connection.execute(
        "SELECT status, closed_at FROM my_decisions WHERE id = ?", (decision_id,)
    ).fetchone()
    assert row["status"] == "closed"
    assert row["closed_at"] == "2026-06-08T00:00:00+00:00"

    with pytest.raises(sqlite3.IntegrityError, match="thesis fields"):
        archive.connection.execute(
            "UPDATE my_decisions SET thesis_text = '事后改写' WHERE id = ?", (decision_id,)
        )
    with pytest.raises(sqlite3.IntegrityError, match="thesis fields"):
        archive.connection.execute(
            "UPDATE my_decisions SET horizon_days = 90 WHERE id = ?", (decision_id,)
        )
    with pytest.raises(sqlite3.IntegrityError, match="cannot be deleted"):
        archive.connection.execute("DELETE FROM my_decisions WHERE id = ?", (decision_id,))


def test_reviews_are_append_only_and_overdue_list_is_visible(archive: Archive) -> None:
    decision_id = archive.add_decision(
        "SH688303", "neutral", "观察论点", "证伪条件", DECIDED_AT, horizon_days=3
    )
    before_close = list_decisions(archive.connection, "2026-06-10T00:00:00+00:00")
    assert before_close["counts"] == {"due_unresolved": 1, "review_overdue": 0, "open": 1}

    archive.close_decision(decision_id, "expired", "2026-06-10T00:00:00+00:00")
    after_close = list_decisions(archive.connection, "2026-06-10T00:00:00+00:00")
    assert after_close["counts"] == {"due_unresolved": 1, "review_overdue": 1, "open": 0}

    review_id = archive.review_decision(
        decision_id, "2026-06-10T01:00:00+00:00", "复盘原文", "以后检查证伪条件"
    )
    with pytest.raises(sqlite3.IntegrityError, match="append-only"):
        archive.connection.execute(
            "UPDATE my_decision_reviews SET retro_text = '改写' WHERE id = ?", (review_id,)
        )
    reviewed = list_decisions(archive.connection, "2026-06-10T02:00:00+00:00")
    assert reviewed["counts"] == {"due_unresolved": 1, "review_overdue": 0, "open": 0}
    items = cast(list[dict[str, object]], reviewed["items"])
    reviews = cast(list[dict[str, object]], items[0]["reviews"])
    assert reviews[0]["retro_text"] == "复盘原文"

    filtered = list_decisions(
        archive.connection,
        "2026-06-10T02:00:00+00:00",
        decided_from="2026-06-02",
    )
    assert filtered["items"] == []


def test_common_close_settlement_is_stable_and_idempotent(archive: Archive) -> None:
    decision_id = archive.add_decision(
        "SH688303", "neutral", "观察论点", "证伪条件", DECIDED_AT, horizon_days=3
    )
    archive.connection.executemany(
        "INSERT INTO prices(ticker, date, close) VALUES (?, ?, ?)",
        [
            ("SH688303", "2026-05-29", 10),
            ("SH000300", "2026-05-29", 100),
            ("SH688303", "2026-06-05", 11),
            ("SH000300", "2026-06-05", 102),
        ],
    )
    outcome = common_close_outcome(
        archive.connection, "SH688303", "SH000300", DECIDED_AT, horizon_days=3
    )
    assert outcome is not None
    assert outcome["resolved_at"] == "2026-06-05"
    assert cast(float, outcome["raw_return"]) == pytest.approx(0.1)
    assert cast(float, outcome["benchmark_return"]) == pytest.approx(0.02)
    assert cast(float, outcome["excess_return"]) == pytest.approx(0.08)

    first = archive.add_decision_outcome(
        decision_id,
        str(outcome["resolved_at"]),
        cast(float, outcome["raw_return"]),
        cast(float, outcome["benchmark_return"]),
        cast(float, outcome["excess_return"]),
        "SH000300",
        OUTCOME_METHOD_VERSION,
        str(outcome["notes"]),
    )
    second = archive.add_decision_outcome(
        decision_id,
        str(outcome["resolved_at"]),
        cast(float, outcome["raw_return"]),
        cast(float, outcome["benchmark_return"]),
        cast(float, outcome["excess_return"]),
        "SH000300",
        OUTCOME_METHOD_VERSION,
        str(outcome["notes"]),
    )
    assert first is not None
    assert second is None
    count = archive.connection.execute("SELECT COUNT(*) FROM my_decision_outcomes").fetchone()[0]
    assert count == 1
    with pytest.raises(sqlite3.IntegrityError, match="append-only"):
        archive.connection.execute(
            "UPDATE my_decision_outcomes SET raw_return = 9 WHERE id = ?", (first,)
        )
    with pytest.raises(sqlite3.IntegrityError, match="append-only"):
        archive.connection.execute("DELETE FROM my_decision_outcomes WHERE id = ?", (first,))


def test_settlement_waits_for_pre_decision_close_and_handles_weekend(archive: Archive) -> None:
    decided_at = "2026-06-06T01:00:00+00:00"
    archive.connection.executemany(
        "INSERT INTO prices(ticker, date, close) VALUES (?, ?, ?)",
        [
            ("SH688303", "2026-06-08", 11),
            ("SH000300", "2026-06-08", 101),
        ],
    )
    assert (
        common_close_outcome(archive.connection, "SH688303", "SH000300", decided_at, horizon_days=1)
        is None
    )

    archive.connection.executemany(
        "INSERT INTO prices(ticker, date, close) VALUES (?, ?, ?)",
        [
            ("SH688303", "2026-06-05", 10),
            ("SH000300", "2026-06-05", 100),
        ],
    )
    outcome = common_close_outcome(
        archive.connection, "SH688303", "SH000300", decided_at, horizon_days=1
    )
    assert outcome is not None
    assert outcome["start_date"] == "2026-06-05"
    assert outcome["resolved_at"] == "2026-06-08"


def test_conflicting_outcome_is_rejected_and_benchmark_is_recorded(archive: Archive) -> None:
    decision_id = archive.add_decision(
        "SH688303", "neutral", "观察论点", "证伪条件", DECIDED_AT, horizon_days=3
    )
    archive.add_decision_outcome(
        decision_id,
        "2026-06-05",
        0.1,
        0.02,
        0.08,
        "SH000300",
        OUTCOME_METHOD_VERSION,
        "stable",
    )
    with pytest.raises(ValueError, match="conflicts with immutable"):
        archive.add_decision_outcome(
            decision_id,
            "2026-06-05",
            0.2,
            0.02,
            0.18,
            "SH000300",
            OUTCOME_METHOD_VERSION,
            "changed",
        )
    archive.add_decision_outcome(
        decision_id,
        "2026-06-05",
        0.1,
        0.03,
        0.07,
        "SH000905",
        OUTCOME_METHOD_VERSION,
        "different benchmark",
    )
    rows = archive.connection.execute(
        "SELECT benchmark_ticker FROM my_decision_outcomes ORDER BY id"
    ).fetchall()
    assert [row["benchmark_ticker"] for row in rows] == ["SH000300", "SH000905"]


def test_decision_times_are_normalized_and_filters_use_local_dates(archive: Archive) -> None:
    older = archive.add_decision(
        "SH688303",
        "neutral",
        "较早",
        "证伪条件",
        "2026-06-11T07:00:00+08:00",
    )
    newer = archive.add_decision(
        "SH688303",
        "neutral",
        "较晚",
        "证伪条件",
        "2026-06-11T01:00:00+00:00",
    )
    rows = archive.connection.execute(
        "SELECT id, decided_at FROM my_decisions ORDER BY id"
    ).fetchall()
    assert rows[0]["decided_at"] == "2026-06-10T23:00:00+00:00"
    assert rows[1]["decided_at"] == "2026-06-11T01:00:00+00:00"

    listed = list_decisions(
        archive.connection,
        "2026-06-11T02:00:00+00:00",
        decided_from="2026-06-11",
        decided_to="2026-06-11",
    )
    assert [item["id"] for item in cast(list[dict[str, object]], listed["items"])] == [
        newer,
        older,
    ]
    with pytest.raises(ValueError):
        list_decisions(archive.connection, "2026-06-11T02:00:00+00:00", decided_from="2026/06/11")


def test_initialize_upgrades_existing_decision_constraints(tmp_path: Path) -> None:
    connection = connect_database(tmp_path / "legacy.sqlite3")
    connection.executescript(
        """
        CREATE TABLE my_decisions (
            id INTEGER PRIMARY KEY, ticker TEXT NOT NULL, direction TEXT NOT NULL,
            thesis_text TEXT NOT NULL, invalidation_condition TEXT NOT NULL,
            horizon_days INTEGER, position_note TEXT, decided_at TEXT NOT NULL,
            source_post_id INTEGER, source_version_id INTEGER, status TEXT NOT NULL,
            closed_at TEXT, notes TEXT
        );
        CREATE TABLE my_decision_outcomes (
            id INTEGER PRIMARY KEY, decision_id INTEGER NOT NULL, resolved_at TEXT NOT NULL,
            raw_return REAL NOT NULL, benchmark_return REAL NOT NULL, excess_return REAL NOT NULL,
            outcome_method_version TEXT NOT NULL, notes TEXT,
            UNIQUE(decision_id, resolved_at, outcome_method_version)
        );
        CREATE TABLE my_decision_reviews (
            id INTEGER PRIMARY KEY, decision_id INTEGER NOT NULL, reviewed_at TEXT NOT NULL,
            retro_text TEXT NOT NULL, lesson TEXT
        );
        CREATE TRIGGER protect_my_decisions_thesis
        BEFORE UPDATE ON my_decisions
        WHEN OLD.thesis_text IS NOT NEW.thesis_text
        BEGIN SELECT RAISE(ABORT, 'legacy thesis lock'); END;
        """
    )
    connection.execute(
        """
        INSERT INTO my_decisions(
            ticker, direction, thesis_text, invalidation_condition, horizon_days,
            decided_at, status
        ) VALUES ('SH688303', 'neutral', '论点', '证伪', 7, ?, 'open')
        """,
        (DECIDED_AT,),
    )
    connection.execute(
        """
        INSERT INTO my_decision_outcomes(
            decision_id, resolved_at, raw_return, benchmark_return, excess_return,
            outcome_method_version
        ) VALUES (1, '2026-06-08', 0.1, 0.02, 0.08, ?)
        """,
        (OUTCOME_METHOD_VERSION,),
    )
    connection.execute(
        """
        INSERT INTO my_decision_outcomes(
            decision_id, resolved_at, raw_return, benchmark_return, excess_return,
            outcome_method_version
        ) VALUES (1, '2026-06-09', 0.11, 0.02, 0.09, ?)
        """,
        (OUTCOME_METHOD_VERSION,),
    )
    initialize_database(connection)
    row = connection.execute("SELECT benchmark_ticker FROM my_decision_outcomes").fetchone()
    assert row["benchmark_ticker"] == "UNKNOWN"
    archive = Archive(connection)
    outcome_id = archive.add_decision_outcome(
        1,
        "2026-06-08",
        0.1,
        0.02,
        0.08,
        "SH000300",
        OUTCOME_METHOD_VERSION,
    )
    assert outcome_id is not None
    assert connection.execute("SELECT COUNT(*) FROM my_decision_outcomes").fetchone()[0] == 3
    with pytest.raises(sqlite3.IntegrityError, match="thesis fields"):
        connection.execute("UPDATE my_decisions SET horizon_days = 30 WHERE id = 1")
    with pytest.raises(sqlite3.IntegrityError, match="append-only"):
        connection.execute("DELETE FROM my_decision_outcomes WHERE id = 1")
    connection.close()
