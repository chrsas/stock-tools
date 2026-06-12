"""Neutral distribution analysis, crowding events, and ticker-history panels."""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from datetime import timedelta
from statistics import fmean, median
from typing import Any

from kol_archive.presentation import version_descriptive_market_snapshots
from kol_archive.time import parse_utc_timestamp

CROWDING_METHOD_VERSION = "confirmed-claims-rolling-v1"


@dataclass(frozen=True)
class AnalysisSettings:
    min_group_samples: int = 10
    crowding_min_authors: int = 3
    crowding_window_days: int = 7


def load_analysis_settings(config: dict[str, Any]) -> AnalysisSettings:
    section = config.get("analysis") or {}
    if not isinstance(section, dict):
        raise ValueError("analysis must be a mapping")
    min_group_samples = section.get("min_group_samples")
    crowding_min_authors = section.get("crowding_min_authors")
    crowding_window_days = section.get("crowding_window_days")
    settings = AnalysisSettings(
        min_group_samples=int(10 if min_group_samples is None else min_group_samples),
        crowding_min_authors=int(3 if crowding_min_authors is None else crowding_min_authors),
        crowding_window_days=int(7 if crowding_window_days is None else crowding_window_days),
    )
    if settings.min_group_samples < 1:
        raise ValueError("analysis.min_group_samples must be positive")
    if settings.crowding_min_authors < 2:
        raise ValueError("analysis.crowding_min_authors must be at least 2")
    if settings.crowding_window_days < 1:
        raise ValueError("analysis.crowding_window_days must be positive")
    return settings


def _distribution(values: list[float]) -> dict[str, object]:
    return {
        "sample_count": len(values),
        "mean_excess_return": fmean(values) if values else None,
        "median_excess_return": median(values) if values else None,
    }


def selective_deletion_analysis(
    connection: sqlite3.Connection, min_group_samples: int
) -> list[dict[str, object]]:
    """Compare resolved-claim distributions by observed source-removal state."""
    if min_group_samples < 1:
        raise ValueError("min_group_samples must be positive")
    rows = connection.execute(
        """
        SELECT
            c.author_id,
            COALESCE(a.notes, a.platform_uid) AS author_name,
            c.horizon_days,
            o.benchmark_ticker,
            o.outcome_method_version,
            o.excess_return,
            EXISTS(
                SELECT 1 FROM post_events e
                WHERE e.post_id = c.post_id
                  AND e.dimension = 'source_state'
                  AND e.to_value = 'gone_confirmed'
                  AND e.detected_at >= c.claim_made_at
                  AND e.detected_at <= o.resolved_at
            ) AS removed
        FROM claims c
        JOIN claim_outcomes o ON o.claim_id = c.id
        JOIN authors a ON a.id = c.author_id
        WHERE c.ingest_mode = 'live'
          AND c.horizon_days IS NOT NULL
          AND o.excess_return IS NOT NULL
        ORDER BY c.author_id, c.horizon_days, o.outcome_method_version, c.id
        """
    ).fetchall()
    groups: dict[tuple[object, ...], dict[str, object]] = {}
    for row in rows:
        key = (
            row["author_id"],
            row["author_name"],
            row["horizon_days"],
            row["benchmark_ticker"],
            row["outcome_method_version"],
        )
        group = groups.setdefault(key, {"removed": [], "retained": []})
        bucket = "removed" if bool(row["removed"]) else "retained"
        values = group[bucket]
        assert isinstance(values, list)
        values.append(float(row["excess_return"]))
    results: list[dict[str, object]] = []
    for group_key, group in groups.items():
        removed = group["removed"]
        retained = group["retained"]
        assert isinstance(removed, list) and isinstance(retained, list)
        sufficient = len(removed) >= min_group_samples and len(retained) >= min_group_samples
        results.append(
            {
                "author_id": group_key[0],
                "author_name": group_key[1],
                "horizon_days": group_key[2],
                "benchmark_ticker": group_key[3],
                "outcome_method_version": group_key[4],
                "removed": _distribution(removed),
                "retained": _distribution(retained),
                "sufficient_samples": sufficient,
                "comparison_label": "分布差异可供比较" if sufficient else "样本不足",
                "min_group_samples": min_group_samples,
            }
        )
    return results


def stage_crowding_events(
    connection: sqlite3.Connection,
    settings: AnalysisSettings,
    detected_at: str,
) -> int:
    """Append qualifying confirmed-claim rolling windows and their members."""
    detected_at = parse_utc_timestamp(detected_at).isoformat()
    rows = connection.execute(
        """
        SELECT id AS claim_id, author_id, post_id, version_id, ticker, direction, claim_made_at
        FROM claims
        WHERE ingest_mode = 'live'
          AND direction IN ('long', 'short')
        ORDER BY ticker, direction, claim_made_at, id
        """
    ).fetchall()
    staged = 0
    episode_key: tuple[object, object] | None = None
    episode_last_at = None
    episode_has_event = False
    connection.execute("BEGIN IMMEDIATE")
    try:
        for end in rows:
            end_at = parse_utc_timestamp(str(end["claim_made_at"]))
            row_key = (end["ticker"], end["direction"])
            if (
                row_key != episode_key
                or episode_last_at is None
                or end_at - episode_last_at > timedelta(days=settings.crowding_window_days)
            ):
                episode_key = row_key
                episode_has_event = False
            episode_last_at = end_at
            start_at = end_at - timedelta(days=settings.crowding_window_days)
            members = [
                row
                for row in rows
                if row["ticker"] == end["ticker"]
                and row["direction"] == end["direction"]
                and start_at <= parse_utc_timestamp(str(row["claim_made_at"])) <= end_at
            ]
            if len({int(row["author_id"]) for row in members}) < settings.crowding_min_authors:
                continue
            if episode_has_event:
                continue
            cursor = connection.execute(
                """
                INSERT OR IGNORE INTO crowding_events(
                    ticker, direction, window_start, window_end, detected_at,
                    author_count, method_version
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    end["ticker"],
                    end["direction"],
                    start_at.isoformat(),
                    end_at.isoformat(),
                    detected_at,
                    len({int(row["author_id"]) for row in members}),
                    CROWDING_METHOD_VERSION,
                ),
            )
            episode_has_event = True
            if cursor.rowcount == 0:
                continue
            assert cursor.lastrowid is not None
            event_id = int(cursor.lastrowid)
            connection.executemany(
                """
                INSERT INTO crowding_event_members(
                    event_id, claim_id, author_id, post_id, version_id
                ) VALUES (?, ?, ?, ?, ?)
                """,
                (
                    (
                        event_id,
                        row["claim_id"],
                        row["author_id"],
                        row["post_id"],
                        row["version_id"],
                    )
                    for row in members
                ),
            )
            staged += 1
    except BaseException:
        connection.execute("ROLLBACK")
        raise
    else:
        connection.execute("COMMIT")
    return staged


def list_crowding_events(
    connection: sqlite3.Connection, limit: int = 50
) -> list[dict[str, object]]:
    if limit < 1:
        raise ValueError("limit must be positive")
    events = [
        dict(row)
        for row in connection.execute(
            """
            SELECT e.*, tn.name AS ticker_name
            FROM crowding_events e
            LEFT JOIN ticker_names tn ON tn.ticker = e.ticker
            ORDER BY e.window_end DESC, e.id DESC
            LIMIT ?
            """,
            (limit,),
        ).fetchall()
    ]
    by_id = {int(event["id"]): event for event in events}
    for event in events:
        event["members"] = []
    if not events:
        return events
    placeholders = ",".join("?" for _ in events)
    for row in connection.execute(
        f"""
        SELECT
            m.event_id,
            m.claim_id, m.author_id, m.post_id, m.version_id,
            COALESCE(a.notes, a.platform_uid) AS author_name,
            c.claim_made_at,
            o.resolved_at,
            o.raw_return,
            o.excess_return,
            o.benchmark_ticker,
            o.outcome_method_version
        FROM crowding_event_members m
        JOIN authors a ON a.id = m.author_id
        JOIN claims c ON c.id = m.claim_id
        LEFT JOIN claim_outcomes o ON o.claim_id = m.claim_id
        WHERE m.event_id IN ({placeholders})
        ORDER BY m.event_id, c.claim_made_at, m.claim_id
        """,
        tuple(by_id),
    ).fetchall():
        member = dict(row)
        event_id = int(member.pop("event_id"))
        members = by_id[event_id]["members"]
        assert isinstance(members, list)
        members.append(member)
    return events


def post_ticker_history(
    connection: sqlite3.Connection,
    post_id: int,
    prompt_version: str,
    benchmark_ticker: str,
) -> dict[str, object]:
    post = connection.execute("SELECT author_id FROM posts WHERE id = ?", (post_id,)).fetchone()
    if post is None:
        raise ValueError(f"unknown post id: {post_id}")
    tickers = [
        str(row["ticker"])
        for row in connection.execute(
            """
            SELECT DISTINCT vt.ticker
            FROM version_tickers vt
            JOIN post_versions v ON v.id = vt.version_id
            WHERE v.post_id = ?
            ORDER BY vt.ticker
            """,
            (post_id,),
        ).fetchall()
    ]
    if not tickers:
        return {"tickers": [], "items": [], "empty_label": "无既往记录"}
    placeholders = ",".join("?" for _ in tickers)
    rows = connection.execute(
        f"""
        SELECT DISTINCT
            v.id AS version_id,
            v.post_id,
            vt.ticker,
            v.first_observed_at,
            v.content_text,
            p.source_state,
            EXISTS(
                SELECT 1 FROM post_events e
                WHERE e.post_id = p.id
                  AND e.dimension = 'source_state'
                  AND e.to_value = 'gone_confirmed'
            ) AS has_removal_event
        FROM version_tickers vt
        JOIN post_versions v ON v.id = vt.version_id
        JOIN posts p ON p.id = v.post_id
        WHERE p.author_id = ? AND vt.ticker IN ({placeholders})
        ORDER BY v.first_observed_at DESC, v.id DESC
        """,
        (post["author_id"], *tickers),
    ).fetchall()
    version_ids = {int(row["version_id"]) for row in rows}
    snapshots = version_descriptive_market_snapshots(
        connection, version_ids, prompt_version, benchmark_ticker
    )
    post_ids = {int(row["post_id"]) for row in rows}
    event_placeholders = ",".join("?" for _ in post_ids)
    events_by_post: dict[int, list[dict[str, object]]] = {}
    for event in connection.execute(
        f"""
        SELECT post_id, dimension, from_value, to_value, detected_at
        FROM post_events
        WHERE post_id IN ({event_placeholders})
          AND dimension IN ('source_state', 'content')
        ORDER BY detected_at, id
        """,
        sorted(post_ids),
    ).fetchall():
        events_by_post.setdefault(int(event["post_id"]), []).append(dict(event))
    items = []
    event_posts_seen: set[int] = set()
    for row in rows:
        item = dict(row)
        row_post_id = int(row["post_id"])
        item["market_snapshot"] = snapshots.get(int(row["version_id"]))
        item["events"] = (
            events_by_post.get(row_post_id, []) if row_post_id not in event_posts_seen else []
        )
        event_posts_seen.add(row_post_id)
        items.append(item)
    return {"tickers": tickers, "items": items, "empty_label": None}
