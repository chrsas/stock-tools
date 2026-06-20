"""The full evidence card for a single post and its version history."""

from __future__ import annotations

import sqlite3
from difflib import unified_diff
from typing import Any

from .common import _post_projection, _row_dict


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
            COALESCE(
                json_extract(v.raw_payload, '$.user.screen_name'), a.notes
            ) AS author_display_name,
            json_extract(v.raw_payload, '$.user.profile_image_url') AS author_avatar_url,
            json_extract(v.raw_payload, '$.user.description') AS author_description,
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
                stance_summary,
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
