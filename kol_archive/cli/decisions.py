"""Personal decision-log commands: add, close, review, list, and resolve."""

from __future__ import annotations

import argparse
from datetime import UTC, datetime
from pathlib import Path
from typing import cast

from kol_archive.config import load_config
from kol_archive.decisions import common_close_outcome, list_decisions
from kol_archive.market import OUTCOME_METHOD_VERSION

from .common import configured_db_path, connect_existing_archive, print_json, resolve_db_path


def _add_decision_command(args: argparse.Namespace) -> None:
    connection, archive = connect_existing_archive(configured_db_path(args.path, args.config_dir))
    try:
        decision_id = archive.add_decision(
            args.ticker,
            args.direction,
            args.thesis,
            args.invalidation,
            args.decided_at or datetime.now(tz=UTC).isoformat(),
            horizon_days=args.horizon_days,
            position_note=args.position_note,
            source_post_id=args.source_post_id,
            source_version_id=args.source_version_id,
            notes=args.notes,
        )
        print_json({"decision_id": decision_id})
    finally:
        connection.close()


def _close_decision_command(args: argparse.Namespace) -> None:
    connection, archive = connect_existing_archive(configured_db_path(args.path, args.config_dir))
    try:
        archive.close_decision(
            args.decision_id,
            args.status,
            args.closed_at or datetime.now(tz=UTC).isoformat(),
            args.notes,
        )
        print_json({"decision_id": args.decision_id, "status": args.status})
    finally:
        connection.close()


def _review_decision_command(args: argparse.Namespace) -> None:
    connection, archive = connect_existing_archive(configured_db_path(args.path, args.config_dir))
    try:
        review_id = archive.review_decision(
            args.decision_id,
            args.reviewed_at or datetime.now(tz=UTC).isoformat(),
            args.retro,
            args.lesson,
        )
        print_json({"decision_id": args.decision_id, "review_id": review_id})
    finally:
        connection.close()


def _decisions_command(args: argparse.Namespace) -> None:
    connection, _ = connect_existing_archive(configured_db_path(args.path, args.config_dir))
    try:
        print_json(
            list_decisions(
                connection,
                datetime.now(tz=UTC).isoformat(),
                status=args.status,
                ticker=args.ticker,
                decided_from=args.since,
                decided_to=args.until,
                limit=args.limit,
            )
        )
    finally:
        connection.close()


def _resolve_decisions_command(args: argparse.Namespace) -> None:
    config = load_config(args.config_dir)
    benchmark = str((config.get("prices") or {}).get("benchmark_ticker") or "SH000300").upper()
    connection, archive = connect_existing_archive(resolve_db_path(args.path, config))
    resolved = 0
    pending = 0
    try:
        rows = connection.execute(
            """
            SELECT id, ticker, decided_at, horizon_days
            FROM my_decisions
            WHERE horizon_days IS NOT NULL
              AND date(decided_at, '+8 hours', '+' || horizon_days || ' days')
                  <= date('now', '+8 hours')
              AND NOT EXISTS (
                  SELECT 1
                  FROM my_decision_outcomes o
                  WHERE o.decision_id = my_decisions.id
                    AND o.benchmark_ticker = ?
                    AND o.outcome_method_version = ?
              )
            ORDER BY decided_at, id
            """,
            (benchmark, OUTCOME_METHOD_VERSION),
        ).fetchall()
        for row in rows:
            outcome = common_close_outcome(
                connection,
                str(row["ticker"]),
                benchmark,
                str(row["decided_at"]),
                int(row["horizon_days"]),
            )
            if outcome is None:
                pending += 1
                continue
            outcome_id = archive.add_decision_outcome(
                int(row["id"]),
                str(outcome["resolved_at"]),
                cast(float, outcome["raw_return"]),
                cast(float, outcome["benchmark_return"]),
                cast(float, outcome["excess_return"]),
                benchmark,
                OUTCOME_METHOD_VERSION,
                str(outcome["notes"]),
            )
            resolved += int(outcome_id is not None)
        print_json(
            {
                "resolved": resolved,
                "pending_prices": pending,
                "method_version": OUTCOME_METHOD_VERSION,
                "benchmark_ticker": benchmark,
            }
        )
    finally:
        connection.close()


def register(subparsers: argparse._SubParsersAction[argparse.ArgumentParser]) -> None:
    add_decision_parser = subparsers.add_parser("add-decision", help="record one personal decision")
    add_decision_parser.add_argument("--ticker", required=True)
    add_decision_parser.add_argument(
        "--direction", choices=["long", "short", "neutral"], required=True
    )
    add_decision_parser.add_argument("--thesis", required=True)
    add_decision_parser.add_argument("--invalidation", required=True)
    add_decision_parser.add_argument("--horizon-days", type=int)
    add_decision_parser.add_argument("--position-note")
    add_decision_parser.add_argument("--decided-at")
    add_decision_parser.add_argument("--source-post-id", type=int)
    add_decision_parser.add_argument("--source-version-id", type=int)
    add_decision_parser.add_argument("--notes")
    add_decision_parser.add_argument("--path", type=Path)
    add_decision_parser.add_argument("--config-dir", type=Path, default=Path("config"))
    add_decision_parser.set_defaults(handler=_add_decision_command)
    close_decision_parser = subparsers.add_parser(
        "close-decision", help="manually close one personal decision"
    )
    close_decision_parser.add_argument("decision_id", type=int)
    close_decision_parser.add_argument(
        "--status", choices=["invalidated", "expired", "closed"], required=True
    )
    close_decision_parser.add_argument("--closed-at")
    close_decision_parser.add_argument("--notes")
    close_decision_parser.add_argument("--path", type=Path)
    close_decision_parser.add_argument("--config-dir", type=Path, default=Path("config"))
    close_decision_parser.set_defaults(handler=_close_decision_command)
    review_decision_parser = subparsers.add_parser(
        "review-decision", help="append a personal decision review"
    )
    review_decision_parser.add_argument("decision_id", type=int)
    review_decision_parser.add_argument("--retro", required=True)
    review_decision_parser.add_argument("--lesson")
    review_decision_parser.add_argument("--reviewed-at")
    review_decision_parser.add_argument("--path", type=Path)
    review_decision_parser.add_argument("--config-dir", type=Path, default=Path("config"))
    review_decision_parser.set_defaults(handler=_review_decision_command)
    decisions_parser = subparsers.add_parser("decisions", help="list personal decisions")
    decisions_parser.add_argument("--status", choices=["open", "invalidated", "expired", "closed"])
    decisions_parser.add_argument("--ticker")
    decisions_parser.add_argument("--since", help="include decisions on or after this date")
    decisions_parser.add_argument("--until", help="include decisions on or before this date")
    decisions_parser.add_argument("--limit", type=int, default=100)
    decisions_parser.add_argument("--path", type=Path)
    decisions_parser.add_argument("--config-dir", type=Path, default=Path("config"))
    decisions_parser.set_defaults(handler=_decisions_command)
    resolve_decisions_parser = subparsers.add_parser(
        "resolve-decisions", help="settle due personal decisions from imported prices"
    )
    resolve_decisions_parser.add_argument("--path", type=Path)
    resolve_decisions_parser.add_argument("--config-dir", type=Path, default=Path("config"))
    resolve_decisions_parser.set_defaults(handler=_resolve_decisions_command)
