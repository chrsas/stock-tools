"""Personal decision log: open, close, review, and settle decisions."""

from __future__ import annotations

from datetime import date

from kol_archive.time import parse_utc_timestamp

from .base import ArchiveBase, _required_lastrowid


class DecisionsMixin(ArchiveBase):
    def add_decision(
        self,
        ticker: str,
        direction: str,
        thesis_text: str,
        invalidation_condition: str,
        decided_at: str,
        *,
        horizon_days: int | None = None,
        position_note: str | None = None,
        source_post_id: int | None = None,
        source_version_id: int | None = None,
        notes: str | None = None,
    ) -> int:
        ticker = ticker.strip().upper()
        thesis_text = thesis_text.strip()
        invalidation_condition = invalidation_condition.strip()
        if not ticker:
            raise ValueError("ticker must not be empty")
        if direction not in {"long", "short", "neutral"}:
            raise ValueError("decision direction must be long, short, or neutral")
        if not thesis_text:
            raise ValueError("decision thesis must not be empty")
        if not invalidation_condition:
            raise ValueError("invalidation condition must not be empty")
        if horizon_days is not None and horizon_days <= 0:
            raise ValueError("horizon_days must be positive")
        decided_at = parse_utc_timestamp(decided_at).isoformat()
        with self._transaction():
            if source_version_id is not None:
                if source_post_id is None:
                    raise ValueError("source_post_id is required with source_version_id")
                self._get_post_version(source_post_id, source_version_id)
            elif source_post_id is not None:
                self._get_post(source_post_id)
            cursor = self.connection.execute(
                """
                INSERT INTO my_decisions(
                    ticker, direction, thesis_text, invalidation_condition, horizon_days,
                    position_note, decided_at, source_post_id, source_version_id, status, notes
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 'open', ?)
                """,
                (
                    ticker,
                    direction,
                    thesis_text,
                    invalidation_condition,
                    horizon_days,
                    None if position_note is None else position_note.strip() or None,
                    decided_at,
                    source_post_id,
                    source_version_id,
                    None if notes is None else notes.strip() or None,
                ),
            )
        return _required_lastrowid(cursor)

    def close_decision(
        self,
        decision_id: int,
        status: str,
        closed_at: str,
        notes: str | None = None,
    ) -> None:
        if status not in {"invalidated", "expired", "closed"}:
            raise ValueError("closed decision status must be invalidated, expired, or closed")
        closed_at = parse_utc_timestamp(closed_at).isoformat()
        with self._transaction():
            cursor = self.connection.execute(
                """
                UPDATE my_decisions
                SET status = ?, closed_at = ?, notes = COALESCE(?, notes)
                WHERE id = ? AND status = 'open'
                """,
                (status, closed_at, None if notes is None else notes.strip() or None, decision_id),
            )
            if cursor.rowcount != 1:
                raise ValueError(f"unknown or already closed decision id: {decision_id}")

    def review_decision(
        self,
        decision_id: int,
        reviewed_at: str,
        retro_text: str,
        lesson: str | None = None,
    ) -> int:
        retro_text = retro_text.strip()
        if not retro_text:
            raise ValueError("decision review must not be empty")
        reviewed_at = parse_utc_timestamp(reviewed_at).isoformat()
        with self._transaction():
            if (
                self.connection.execute(
                    "SELECT 1 FROM my_decisions WHERE id = ?", (decision_id,)
                ).fetchone()
                is None
            ):
                raise ValueError(f"unknown decision id: {decision_id}")
            cursor = self.connection.execute(
                """
                INSERT INTO my_decision_reviews(decision_id, reviewed_at, retro_text, lesson)
                VALUES (?, ?, ?, ?)
                """,
                (
                    decision_id,
                    reviewed_at,
                    retro_text,
                    None if lesson is None else lesson.strip() or None,
                ),
            )
        return _required_lastrowid(cursor)

    def add_decision_outcome(
        self,
        decision_id: int,
        resolved_at: str,
        raw_return: float,
        benchmark_return: float,
        excess_return: float,
        benchmark_ticker: str,
        outcome_method_version: str,
        notes: str | None = None,
    ) -> int | None:
        benchmark_ticker = benchmark_ticker.strip().upper()
        if not benchmark_ticker:
            raise ValueError("benchmark_ticker must not be empty")
        if not outcome_method_version.strip():
            raise ValueError("outcome_method_version must not be empty")
        try:
            date.fromisoformat(resolved_at)
        except ValueError as error:
            raise ValueError("resolved_at must be an ISO date") from error
        with self._transaction():
            existing = self.connection.execute(
                """
                SELECT resolved_at, raw_return, benchmark_return, excess_return, notes
                FROM my_decision_outcomes
                WHERE decision_id = ? AND benchmark_ticker = ? AND outcome_method_version = ?
                """,
                (decision_id, benchmark_ticker, outcome_method_version.strip()),
            ).fetchone()
            if existing is not None:
                expected = (
                    resolved_at,
                    raw_return,
                    benchmark_return,
                    excess_return,
                    notes,
                )
                actual = (
                    existing["resolved_at"],
                    existing["raw_return"],
                    existing["benchmark_return"],
                    existing["excess_return"],
                    existing["notes"],
                )
                if actual != expected:
                    raise ValueError(
                        f"decision outcome conflicts with immutable result: {decision_id}"
                    )
                return None
            cursor = self.connection.execute(
                """
                INSERT INTO my_decision_outcomes(
                    decision_id, resolved_at, raw_return, benchmark_return, excess_return,
                    benchmark_ticker, outcome_method_version, notes
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    decision_id,
                    resolved_at,
                    raw_return,
                    benchmark_return,
                    excess_return,
                    benchmark_ticker,
                    outcome_method_version.strip(),
                    notes,
                ),
            )
        return _required_lastrowid(cursor)
