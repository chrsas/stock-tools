"""Assemble the ``/api/home`` view payloads from the read-only projections."""

from __future__ import annotations

import sqlite3
from datetime import UTC, datetime
from typing import cast
from urllib.parse import parse_qs

from kol_archive.analysis import list_crowding_events, selective_deletion_analysis
from kol_archive.claims import list_claim_proposals
from kol_archive.decisions import list_decisions
from kol_archive.models import FeedState, WatchMode
from kol_archive.presentation import (
    author_recent_viewpoint_clusters,
    author_viewpoint_overview,
    framework_library,
    list_attention_queue,
    list_filtered_timeline,
    list_pinned_versions,
    list_timeline,
)
from kol_archive.recall import build_recall_page
from kol_archive.watchlist import list_watchlist


def _queue_counts(connection: sqlite3.Connection, prompt_version: str) -> dict[str, int]:
    def scalar(query: str, *params: object) -> int:
        return int(connection.execute(query, params).fetchone()[0])

    pending = scalar(
        """
        SELECT COUNT(*) FROM posts p
        JOIN enrichments e ON e.version_id = p.current_version_id AND e.prompt_version = ?
        WHERE p.watch_mode != ?
          AND (e.label_first_hand_info = 1 OR e.label_transferable_framework = 1
               OR e.label_reasoned_non_consensus = 1)
          AND NOT EXISTS (SELECT 1 FROM attention_log al WHERE al.version_id = p.current_version_id)
        """,
        prompt_version,
        WatchMode.PINNED.value,
    )
    three = scalar(
        """
        SELECT COUNT(*) FROM posts p
        JOIN enrichments e ON e.version_id = p.current_version_id AND e.prompt_version = ?
        WHERE p.watch_mode != ?
          AND e.label_first_hand_info = 1 AND e.label_transferable_framework = 1
          AND e.label_reasoned_non_consensus = 1
          AND NOT EXISTS (SELECT 1 FROM attention_log al WHERE al.version_id = p.current_version_id)
        """,
        prompt_version,
        WatchMode.PINNED.value,
    )
    pinned = scalar("SELECT COUNT(*) FROM posts WHERE watch_mode = ?", WatchMode.PINNED.value)
    absent = scalar(
        "SELECT COUNT(*) FROM posts WHERE feed_state = ?", FeedState.ABSENT_CONFIRMED.value
    )
    return {"pending": pending, "three": three, "pinned": pinned, "absent": absent}


def _home_payload(
    connection: sqlite3.Connection,
    prompt_version: str,
    limit: int,
    query: str,
    benchmark_ticker: str = "SH000300",
    cluster_window_days: int = 7,
    analysis_min_group_samples: int = 10,
    framework_prompt_version: str = "framework-v1",
) -> dict[str, object]:
    values = parse_qs(query)
    view = (values.get("view") or ["authors"])[0]
    tier3_only = (values.get("tier") or [""])[0] == "3"
    if view == "raw":
        return {"view": "raw", "items": list_timeline(connection, limit=limit)}
    if view == "filtered":
        return {
            "view": "filtered",
            "items": list_filtered_timeline(connection, prompt_version, limit=limit),
            "prompt_version": prompt_version,
        }
    if view == "pinned":
        return {
            "view": "pinned",
            "items": list_pinned_versions(connection, prompt_version, limit=limit),
            "counts": _queue_counts(connection, prompt_version),
        }
    if view == "queue" or tier3_only:
        items = list_attention_queue(connection, prompt_version, limit=limit)
        if tier3_only:
            items = [item for item in items if int(cast(int, item.get("tier") or 0)) >= 3]
        return {
            "view": "queue",
            "items": items,
            "counts": _queue_counts(connection, prompt_version),
            "tier3_only": tier3_only,
        }
    if view == "decisions":
        status_values = values.get("status")
        ticker_values = values.get("ticker")
        from_values = values.get("from")
        to_values = values.get("to")
        return {
            "view": "decisions",
            **list_decisions(
                connection,
                datetime.now(tz=UTC).isoformat(),
                status=status_values[0] if status_values else None,
                ticker=ticker_values[0] if ticker_values else None,
                decided_from=from_values[0] if from_values else None,
                decided_to=to_values[0] if to_values else None,
                limit=limit,
            ),
        }
    if view == "claims":
        state_values = values.get("state")
        return {
            "view": "claims",
            **list_claim_proposals(
                connection,
                review_state=state_values[0] if state_values else None,
                limit=limit,
            ),
        }
    if view == "watchlist":
        return {"view": "watchlist", "items": list_watchlist(connection)}
    if view == "frameworks":
        topic_values = values.get("topic")
        variable_values = values.get("variable")
        return {
            "view": "frameworks",
            **framework_library(
                connection,
                framework_prompt_version,
                topic=topic_values[0] if topic_values else None,
                variable=variable_values[0] if variable_values else None,
                limit=limit,
            ),
        }
    if view == "recall":
        return build_recall_page(
            connection,
            values,
            prompt_version=prompt_version,
            benchmark_ticker=benchmark_ticker,
        )
    if view == "analysis":
        return {
            "view": "analysis",
            "selective_deletion": selective_deletion_analysis(
                connection, analysis_min_group_samples
            ),
            "crowding_events": list_crowding_events(connection, limit=limit),
        }
    if view == "operations":
        return {
            "view": "operations",
            "authors": author_viewpoint_overview(connection, prompt_version),
        }
    authors = author_viewpoint_overview(connection, prompt_version)
    selected_uid = (values.get("author") or [""])[0] or None
    selected = next(
        (
            author
            for author in authors
            if str(author.get("author_platform_uid") or "") == str(selected_uid or "")
        ),
        authors[0] if authors else None,
    )
    clusters = (
        author_recent_viewpoint_clusters(
            connection,
            str(selected["author_platform_uid"]),
            prompt_version,
            limit=10,
            benchmark_ticker=benchmark_ticker,
            cluster_window_days=cluster_window_days,
        )
        if selected
        else []
    )
    return {"view": "authors", "authors": authors, "selected": selected, "clusters": clusters}
