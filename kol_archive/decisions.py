"""Personal decision-log projections and deterministic price settlement."""

from __future__ import annotations

import sqlite3
from datetime import date, timedelta
from typing import Any

from kol_archive.market import (
    A_SHARE_TIMEZONE,
    OUTCOME_METHOD_VERSION,
    common_close_returns,
    local_market_date,
)
from kol_archive.time import parse_utc_timestamp


def _rows(
    connection: sqlite3.Connection, query: str, params: tuple[object, ...]
) -> list[dict[str, Any]]:
    return [dict(row) for row in connection.execute(query, params).fetchall()]


def list_decisions(
    connection: sqlite3.Connection,
    as_of: str,
    *,
    status: str | None = None,
    ticker: str | None = None,
    decided_from: str | None = None,
    decided_to: str | None = None,
    limit: int = 100,
) -> dict[str, object]:
    if limit <= 0:
        raise ValueError("limit must be positive")
    as_of_date = parse_utc_timestamp(as_of).astimezone(A_SHARE_TIMEZONE).date().isoformat()
    if decided_from:
        date.fromisoformat(decided_from)
    if decided_to:
        date.fromisoformat(decided_to)
    if decided_from and decided_to and decided_from > decided_to:
        raise ValueError("decided_from must be on or before decided_to")
    filters = ["1 = 1"]
    params: list[object] = []
    if status:
        if status not in {"open", "invalidated", "expired", "closed"}:
            raise ValueError("decision status must be open, invalidated, expired, or closed")
        filters.append("d.status = ?")
        params.append(status)
    if ticker:
        filters.append("d.ticker = ?")
        params.append(ticker.strip().upper())
    if decided_from:
        filters.append("date(d.decided_at, '+8 hours') >= ?")
        params.append(decided_from)
    if decided_to:
        filters.append("date(d.decided_at, '+8 hours') <= ?")
        params.append(decided_to)
    params.extend((as_of_date, limit))
    decisions = _rows(
        connection,
        f"""
        SELECT
            d.*,
            tn.name AS ticker_name,
            date(d.decided_at, '+8 hours', '+' || d.horizon_days || ' days') AS due_date,
            CASE
                WHEN d.horizon_days IS NOT NULL
                 AND date(d.decided_at, '+8 hours', '+' || d.horizon_days || ' days') <= ?
                 AND NOT EXISTS (
                    SELECT 1 FROM my_decision_outcomes o WHERE o.decision_id = d.id
                 )
                THEN 1 ELSE 0
            END AS due_unresolved,
            CASE
                WHEN d.status != 'open'
                 AND NOT EXISTS (
                    SELECT 1 FROM my_decision_reviews r WHERE r.decision_id = d.id
                 )
                THEN 1 ELSE 0
            END AS review_overdue
        FROM my_decisions d
        LEFT JOIN ticker_names tn ON tn.ticker = d.ticker
        WHERE {" AND ".join(filters)}
        ORDER BY julianday(d.decided_at) DESC, d.id DESC
        LIMIT ?
        """,
        tuple(params),
    )
    decision_ids = [int(decision["id"]) for decision in decisions]
    outcomes_by_decision: dict[int, list[dict[str, Any]]] = {item: [] for item in decision_ids}
    reviews_by_decision: dict[int, list[dict[str, Any]]] = {item: [] for item in decision_ids}
    if decision_ids:
        placeholders = ",".join("?" for _ in decision_ids)
        outcomes = _rows(
            connection,
            f"""
            SELECT id, decision_id, resolved_at, raw_return, benchmark_return, excess_return,
                   benchmark_ticker, outcome_method_version, notes
            FROM my_decision_outcomes
            WHERE decision_id IN ({placeholders})
            ORDER BY decision_id, resolved_at, id
            """,
            tuple(decision_ids),
        )
        reviews = _rows(
            connection,
            f"""
            SELECT id, decision_id, reviewed_at, retro_text, lesson
            FROM my_decision_reviews
            WHERE decision_id IN ({placeholders})
            ORDER BY decision_id, reviewed_at, id
            """,
            tuple(decision_ids),
        )
        for outcome in outcomes:
            outcomes_by_decision[int(outcome["decision_id"])].append(outcome)
        for review in reviews:
            reviews_by_decision[int(review["decision_id"])].append(review)
    for decision in decisions:
        decision_id = int(decision["id"])
        decision["outcomes"] = outcomes_by_decision[decision_id]
        decision["reviews"] = reviews_by_decision[decision_id]
    counts = connection.execute(
        """
        SELECT
            SUM(CASE
                WHEN d.horizon_days IS NOT NULL
                 AND date(d.decided_at, '+8 hours', '+' || d.horizon_days || ' days') <= ?
                 AND NOT EXISTS (
                    SELECT 1 FROM my_decision_outcomes o WHERE o.decision_id = d.id
                 )
                THEN 1 ELSE 0 END) AS due_unresolved,
            SUM(CASE
                WHEN d.status != 'open'
                 AND NOT EXISTS (
                    SELECT 1 FROM my_decision_reviews r WHERE r.decision_id = d.id
                 )
                THEN 1 ELSE 0 END) AS review_overdue,
            SUM(CASE WHEN d.status = 'open' THEN 1 ELSE 0 END) AS open_count
        FROM my_decisions d
        """,
        (as_of_date,),
    ).fetchone()
    return {
        "items": decisions,
        "counts": {
            "due_unresolved": int(counts["due_unresolved"] or 0),
            "review_overdue": int(counts["review_overdue"] or 0),
            "open": int(counts["open_count"] or 0),
        },
        "filters": {
            "status": status,
            "ticker": ticker,
            "decided_from": decided_from,
            "decided_to": decided_to,
        },
        "outcome_method_version": OUTCOME_METHOD_VERSION,
    }


def common_close_outcome(
    connection: sqlite3.Connection,
    ticker: str,
    benchmark_ticker: str,
    decided_at: str,
    horizon_days: int,
) -> dict[str, object] | None:
    if horizon_days <= 0:
        raise ValueError("horizon_days must be positive")
    target_date = (local_market_date(decided_at) + timedelta(days=horizon_days)).isoformat()
    outcome = common_close_returns(
        connection, ticker, benchmark_ticker, decided_at, end_date=target_date
    )
    if outcome is None:
        return None
    return {
        **outcome,
        "resolved_at": outcome["end_date"],
        "notes": f"共同收盘起点 {outcome['start_date']}，目标自然日 {target_date}",
    }
