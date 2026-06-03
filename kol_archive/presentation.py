"""Read-only timeline and evidence-card projections for the local CLI."""

from __future__ import annotations

import sqlite3
from difflib import unified_diff
from typing import Any

from kol_archive.maintenance import redact_text
from kol_archive.models import FeedState, SourceState, WatchMode


def _row_dict(
    row: sqlite3.Row,
    *,
    redacted_columns: frozenset[str] = frozenset(),
) -> dict[str, object]:
    return {
        str(key): redact_text(str(row[key]))
        if key in redacted_columns and row[key] is not None
        else row[key]
        for key in row.keys()
    }


def _status_summary(
    feed_state: FeedState,
    source_state: SourceState,
    watch_mode: WatchMode,
) -> dict[str, str]:
    summary = {
        "human_label": (
            f"feed：{_feed_label(feed_state)}；来源：{_source_label(source_state)}；"
            f"监控：{_watch_label(watch_mode)}"
        ),
    }
    if source_state is SourceState.GONE_CONFIRMED:
        return summary | {
            "deletion_signal_level": "strong",
            "deletion_signal_label": "强信号：来源页明确显示已移除，不归因移除主体",
        }
    if source_state is SourceState.UNAVAILABLE:
        return summary | {
            "deletion_signal_level": "weak",
            "deletion_signal_label": "弱信号：直链当前不可访问，无法确认移除",
        }
    if feed_state is FeedState.ABSENT_CONFIRMED:
        return summary | {
            "deletion_signal_level": "weak",
            "deletion_signal_label": "弱信号：feed 内连续健康轮次缺席，未经直链确认",
        }
    if feed_state is FeedState.OUT_OF_SCOPE and watch_mode is WatchMode.INACTIVE:
        return summary | {
            "deletion_signal_level": "none",
            "deletion_signal_label": "无删帖信号：帖子已滑出监控窗口",
        }
    if feed_state is FeedState.PRESENT:
        return summary | {
            "deletion_signal_level": "none",
            "deletion_signal_label": "无删帖信号：最近 feed 观察为在场",
        }
    return summary | {
        "deletion_signal_level": "none",
        "deletion_signal_label": "无删帖信号：当前证据不足",
    }


def _feed_label(state: FeedState) -> str:
    return {
        FeedState.PRESENT: "在场",
        FeedState.ABSENT_CONFIRMED: "连续健康轮次缺席",
        FeedState.OUT_OF_SCOPE: "已滑出监控窗口",
        FeedState.UNKNOWN: "待确认",
    }[state]


def _source_label(state: SourceState) -> str:
    return {
        SourceState.REACHABLE: "直链可访问",
        SourceState.GONE_CONFIRMED: "来源页明确显示已移除",
        SourceState.UNAVAILABLE: "直链当前不可访问",
        SourceState.UNKNOWN: "未复查",
    }[state]


def _watch_label(mode: WatchMode) -> str:
    return {
        WatchMode.RECENT_WINDOW: "近期窗口",
        WatchMode.PINNED: "已钉住",
        WatchMode.INACTIVE: "停止持续监控",
    }[mode]


def _post_projection(row: sqlite3.Row) -> dict[str, object]:
    projection = _row_dict(row)
    projection["status"] = _status_summary(
        FeedState(str(row["feed_state"])),
        SourceState(str(row["source_state"])),
        WatchMode(str(row["watch_mode"])),
    )
    return projection


def list_timeline(connection: sqlite3.Connection, *, limit: int = 50) -> list[dict[str, object]]:
    if limit < 1:
        raise ValueError("timeline limit must be positive")
    rows = connection.execute(
        """
        SELECT
            p.id AS post_id,
            p.platform,
            p.platform_post_id,
            a.platform_uid AS author_platform_uid,
            p.url,
            p.posted_at_claimed,
            p.first_seen_at,
            p.last_present_at,
            p.source_checked_at,
            p.feed_state,
            p.source_state,
            p.watch_mode,
            p.absent_healthy_streak,
            p.current_version_id,
            v.content_text AS current_text,
            v.first_observed_at AS current_version_first_observed_at,
            (
                SELECT MAX(s.observed_at)
                FROM version_sightings s
                WHERE s.version_id = p.current_version_id
            ) AS current_version_last_observed_at,
            (
                SELECT MAX(o.observed_at)
                FROM post_observations o
                WHERE o.post_id = p.id AND o.present = 0
            ) AS last_feed_absence_detected_at
        FROM posts p
        JOIN authors a ON a.id = p.author_id
        LEFT JOIN post_versions v ON v.id = p.current_version_id
        ORDER BY COALESCE(p.last_present_at, p.first_seen_at) DESC, p.id DESC
        LIMIT ?
        """,
        (limit,),
    ).fetchall()
    return [_post_projection(row) for row in rows]


def list_filtered_timeline(
    connection: sqlite3.Connection, prompt_version: str, *, limit: int = 50
) -> list[dict[str, object]]:
    """The label-gate stream: posts whose current version was enriched (for
    ``prompt_version``) and hit at least one label, newest first.

    This is a filtered *view* of the raw timeline, not a replacement — the raw
    stream stays one call away via :func:`list_timeline`. Posts without an
    enrichment for this prompt version are simply absent here, never hidden.
    """
    if limit < 1:
        raise ValueError("timeline limit must be positive")
    if not prompt_version.strip():
        raise ValueError("prompt_version must not be empty")
    rows = connection.execute(
        """
        SELECT
            p.id AS post_id,
            p.platform,
            p.platform_post_id,
            a.platform_uid AS author_platform_uid,
            p.url,
            p.posted_at_claimed,
            p.first_seen_at,
            p.last_present_at,
            p.source_checked_at,
            p.feed_state,
            p.source_state,
            p.watch_mode,
            p.absent_healthy_streak,
            p.current_version_id,
            v.content_text AS current_text,
            v.first_observed_at AS current_version_first_observed_at,
            e.post_type,
            e.label_first_hand_info,
            e.label_transferable_framework,
            e.label_reasoned_non_consensus,
            e.rationale AS enrichment_rationale,
            e.evidence_snippet AS enrichment_evidence_snippet,
            e.prompt_version AS enrichment_prompt_version,
            (
                SELECT MAX(s.observed_at)
                FROM version_sightings s
                WHERE s.version_id = p.current_version_id
            ) AS current_version_last_observed_at,
            (
                SELECT MAX(o.observed_at)
                FROM post_observations o
                WHERE o.post_id = p.id AND o.present = 0
            ) AS last_feed_absence_detected_at
        FROM posts p
        JOIN authors a ON a.id = p.author_id
        JOIN post_versions v ON v.id = p.current_version_id
        JOIN enrichments e
            ON e.version_id = p.current_version_id AND e.prompt_version = ?
        WHERE e.label_first_hand_info = 1
           OR e.label_transferable_framework = 1
           OR e.label_reasoned_non_consensus = 1
        ORDER BY COALESCE(p.last_present_at, p.first_seen_at) DESC, p.id DESC
        LIMIT ?
        """,
        (prompt_version.strip(), limit),
    ).fetchall()
    return [_post_projection(row) for row in rows]


def _version_history(connection: sqlite3.Connection, post_id: int) -> list[dict[str, object]]:
    rows = connection.execute(
        """
        SELECT
            v.id AS version_id,
            v.first_observed_at,
            MAX(s.observed_at) AS last_observed_at,
            v.ingest_mode,
            v.content_hash,
            v.content_text
        FROM post_versions v
        LEFT JOIN version_sightings s ON s.version_id = v.id
        WHERE v.post_id = ?
        GROUP BY v.id
        ORDER BY v.id
        """,
        (post_id,),
    ).fetchall()
    history: list[dict[str, object]] = []
    prior_text: str | None = None
    prior_version_id: int | None = None
    for row in rows:
        item = _row_dict(row)
        text = str(row["content_text"])
        if prior_text is None:
            item["diff_from_prior_observed_version"] = None
        else:
            item["diff_from_prior_observed_version"] = "".join(
                unified_diff(
                    prior_text.splitlines(keepends=True),
                    text.splitlines(keepends=True),
                    fromfile=f"observed-version-{prior_version_id}",
                    tofile=f"observed-version-{row['version_id']}",
                )
            )
        history.append(item)
        prior_text = text
        prior_version_id = int(row["version_id"])
    return history


def _query_rows(
    connection: sqlite3.Connection,
    query: str,
    post_id: int,
    *,
    redacted_columns: frozenset[str] = frozenset(),
) -> list[dict[str, object]]:
    return [
        _row_dict(row, redacted_columns=redacted_columns)
        for row in connection.execute(query, (post_id,)).fetchall()
    ]


def build_evidence_card(connection: sqlite3.Connection, post_id: int) -> dict[str, Any]:
    row = connection.execute(
        """
        SELECT
            p.id,
            p.author_id,
            p.platform,
            p.platform_post_id,
            p.first_seen_at,
            p.last_present_at,
            p.current_version_id,
            p.current_content_hash,
            p.absent_healthy_streak,
            p.feed_state,
            p.source_state,
            p.source_checked_at,
            p.watch_mode,
            p.posted_at_claimed,
            p.url,
            p.ingest_mode,
            a.platform_uid AS author_platform_uid,
            v.content_text AS current_text,
            v.first_observed_at AS current_version_first_observed_at,
            (
                SELECT MAX(s.observed_at)
                FROM version_sightings s
                WHERE s.version_id = p.current_version_id
            ) AS current_version_last_observed_at,
            (
                SELECT MAX(o.observed_at)
                FROM post_observations o
                WHERE o.post_id = p.id AND o.present = 0
            ) AS last_feed_absence_detected_at
        FROM posts p
        JOIN authors a ON a.id = p.author_id
        LEFT JOIN post_versions v ON v.id = p.current_version_id
        WHERE p.id = ?
        """,
        (post_id,),
    ).fetchone()
    if row is None:
        raise ValueError(f"unknown post id: {post_id}")
    return {
        "post": _post_projection(row),
        "versions": _version_history(connection, post_id),
        "feed_observations": _query_rows(
            connection,
            """
            SELECT
                o.id AS observation_id,
                o.fetch_run_id,
                o.observed_at,
                o.present,
                o.content_fidelity,
                o.version_id,
                r.status AS fetch_status,
                r.login_state AS fetch_login_state,
                r.pagination_complete,
                r.rate_limited,
                r.notes AS fetch_notes
            FROM post_observations o
            JOIN fetch_runs r ON r.id = o.fetch_run_id
            WHERE o.post_id = ?
            ORDER BY o.id
            """,
            post_id,
            redacted_columns=frozenset({"fetch_notes"}),
        ),
        "direct_probes": _query_rows(
            connection,
            """
            SELECT
                id AS probe_run_id,
                observed_at,
                status,
                login_state,
                rate_limited,
                result,
                content_fidelity,
                observed_version_id,
                notes
            FROM probe_runs
            WHERE post_id = ?
            ORDER BY id
            """,
            post_id,
            redacted_columns=frozenset({"notes"}),
        ),
        "events": _query_rows(
            connection,
            """
            SELECT
                id AS event_id,
                dimension,
                from_value,
                to_value,
                detected_at,
                evidence_fetch_run_id,
                evidence_probe_run_id,
                from_version_id,
                to_version_id,
                notes
            FROM post_events
            WHERE post_id = ?
            ORDER BY id
            """,
            post_id,
            redacted_columns=frozenset({"notes"}),
        ),
        "attention_log": _query_rows(
            connection,
            """
            SELECT
                id AS attention_id,
                version_id,
                triggered_at,
                my_reason,
                my_expectation,
                reviewed_at,
                my_retro
            FROM attention_log
            WHERE post_id = ?
            ORDER BY id
            """,
            post_id,
        ),
        "rewrite_exercises": _query_rows(
            connection,
            """
            SELECT
                id AS rewrite_exercise_id,
                version_id,
                original_text,
                llm_rewritten_claim,
                llm_rationale,
                model,
                prompt_version,
                my_verdict,
                created_at
            FROM rewrite_exercises
            WHERE post_id = ?
            ORDER BY id
            """,
            post_id,
        ),
        "enrichments": _query_rows(
            connection,
            """
            SELECT
                id AS enrichment_id,
                version_id,
                post_type,
                label_first_hand_info,
                label_transferable_framework,
                label_reasoned_non_consensus,
                rationale,
                evidence_snippet,
                model,
                prompt_version,
                created_at
            FROM enrichments
            WHERE post_id = ?
            ORDER BY id
            """,
            post_id,
        ),
    }
