"""Timeline streams, the attention queue, and the pinned list."""

from __future__ import annotations

import sqlite3

from kol_archive.models import WatchMode

from .common import _post_projection


def list_timeline(
    connection: sqlite3.Connection, *, limit: int = 50, offset: int = 0
) -> list[dict[str, object]]:
    if limit < 1:
        raise ValueError("timeline limit must be positive")
    if offset < 0:
        raise ValueError("timeline offset must not be negative")
    rows = connection.execute(
        """
        SELECT
            p.id AS post_id,
            p.platform,
            p.platform_post_id,
            a.platform_uid AS author_platform_uid,
            COALESCE(
                json_extract(v.raw_payload, '$.user.screen_name'), a.notes
            ) AS author_display_name,
            json_extract(v.raw_payload, '$.user.profile_image_url') AS author_avatar_url,
            json_extract(v.raw_payload, '$.user.description') AS author_description,
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
        ORDER BY
            COALESCE(p.posted_at_claimed, p.last_present_at, p.first_seen_at) DESC,
            COALESCE(p.last_present_at, p.first_seen_at) DESC,
            p.id DESC
        LIMIT ? OFFSET ?
        """,
        (limit, offset),
    ).fetchall()
    return [_post_projection(row) for row in rows]


def list_filtered_timeline(
    connection: sqlite3.Connection, prompt_version: str, *, limit: int = 50, offset: int = 0
) -> list[dict[str, object]]:
    """The label-gate stream: posts whose current version was enriched (for
    ``prompt_version``) and hit at least one label, newest first.

    This is a filtered *view* of the raw timeline, not a replacement — the raw
    stream stays one call away via :func:`list_timeline`. Posts without an
    enrichment for this prompt version are simply absent here, never hidden.
    """
    if limit < 1:
        raise ValueError("timeline limit must be positive")
    if offset < 0:
        raise ValueError("timeline offset must not be negative")
    if not prompt_version.strip():
        raise ValueError("prompt_version must not be empty")
    rows = connection.execute(
        """
        SELECT
            p.id AS post_id,
            p.platform,
            p.platform_post_id,
            a.platform_uid AS author_platform_uid,
            COALESCE(
                json_extract(v.raw_payload, '$.user.screen_name'), a.notes
            ) AS author_display_name,
            json_extract(v.raw_payload, '$.user.profile_image_url') AS author_avatar_url,
            json_extract(v.raw_payload, '$.user.description') AS author_description,
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
        ORDER BY
            COALESCE(p.posted_at_claimed, p.last_present_at, p.first_seen_at) DESC,
            COALESCE(p.last_present_at, p.first_seen_at) DESC,
            p.id DESC
        LIMIT ? OFFSET ?
        """,
        (prompt_version.strip(), limit, offset),
    ).fetchall()
    return [_post_projection(row) for row in rows]


# Shared card columns for the attention queue and the pinned list. Both render
# the same `_queue_card`, so they project identical fields; only the FROM/WHERE
# differ (queue inner-joins enrichments and excludes dispositioned posts, the
# pinned list left-joins so a post pinned without an enrichment still shows).
_QUEUE_CARD_COLUMNS = """
            p.id AS post_id,
            p.platform_post_id,
            a.platform_uid AS author_platform_uid,
            a.notes AS author_name,
            COALESCE(
                json_extract(v.raw_payload, '$.user.screen_name'), a.notes
            ) AS author_display_name,
            json_extract(v.raw_payload, '$.user.profile_image_url') AS author_avatar_url,
            json_extract(v.raw_payload, '$.user.description') AS author_description,
            p.url,
            p.posted_at_claimed,
            p.feed_state,
            p.source_state,
            p.watch_mode,
            p.source_checked_at,
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
                e.label_first_hand_info + e.label_transferable_framework
                + e.label_reasoned_non_consensus
            ) AS tier,
            (SELECT COUNT(*) FROM post_versions pv WHERE pv.post_id = p.id) AS version_count,
            -- Latest evidence may come from either channel: a healthy direct-link
            -- probe can create the current version (probe_runs.observed_version_id)
            -- with no feed observation of its own, so source channel/run_id from the
            -- version_sightings view (which unions both) rather than post_observations.
            (
                SELECT s.channel FROM version_sightings s
                WHERE s.version_id = p.current_version_id
                ORDER BY s.observed_at DESC LIMIT 1
            ) AS latest_evidence_channel,
            (
                SELECT s.run_id FROM version_sightings s
                WHERE s.version_id = p.current_version_id
                ORDER BY s.observed_at DESC LIMIT 1
            ) AS latest_evidence_run_id,
            (
                SELECT fid FROM (
                    SELECT observed_at, content_fidelity AS fid FROM post_observations
                    WHERE version_id = p.current_version_id
                    UNION ALL
                    SELECT observed_at, content_fidelity AS fid FROM probe_runs
                    WHERE observed_version_id = p.current_version_id
                    ORDER BY observed_at DESC LIMIT 1
                )
            ) AS current_content_fidelity,
            (
                SELECT MAX(s.observed_at)
                FROM version_sightings s
                WHERE s.version_id = p.current_version_id
            ) AS current_version_last_observed_at,
            (
                SELECT COUNT(*)
                FROM version_sightings s
                WHERE s.version_id = p.current_version_id
            ) AS current_version_observation_count
"""


def list_attention_queue(
    connection: sqlite3.Connection, prompt_version: str, *, limit: int = 50
) -> list[dict[str, object]]:
    """The pending-attention queue: enriched current versions that hit at least
    one label and have not yet been dispositioned.

    A version is *dispositioned* (and so leaves the queue) once its post is
    pinned or it has an ``attention_log`` entry, i.e. after the user pins it or
    writes a关注理由. This is pure derivation over existing tables, so there is
    no separate "reviewed" store: nothing here is hidden, only ordered.

    Ordered by tier (number of labels hit) then most recent observation
    (``current_version_last_observed_at``, not first-seen), so the densest
    signals float to the top and a version seen again recently resurfaces.
    """
    if limit < 1:
        raise ValueError("timeline limit must be positive")
    if not prompt_version.strip():
        raise ValueError("prompt_version must not be empty")
    rows = connection.execute(
        f"""
        SELECT{_QUEUE_CARD_COLUMNS}
        FROM posts p
        JOIN authors a ON a.id = p.author_id
        JOIN post_versions v ON v.id = p.current_version_id
        JOIN enrichments e
            ON e.version_id = p.current_version_id AND e.prompt_version = ?
        WHERE p.watch_mode != ?
          AND (
              e.label_first_hand_info = 1
              OR e.label_transferable_framework = 1
              OR e.label_reasoned_non_consensus = 1
          )
          AND NOT EXISTS (
              SELECT 1 FROM attention_log al WHERE al.version_id = p.current_version_id
          )
        ORDER BY tier DESC,
            COALESCE(current_version_last_observed_at, v.first_observed_at) DESC, p.id DESC
        LIMIT ?
        """,
        (prompt_version.strip(), WatchMode.PINNED.value, limit),
    ).fetchall()
    return [_post_projection(row) for row in rows]


def list_pinned_versions(
    connection: sqlite3.Connection, prompt_version: str, *, limit: int = 50
) -> list[dict[str, object]]:
    """The pinned list: the counterpart to the attention queue, showing the
    posts the user has pinned for long-term watching.

    These are exactly the posts that left the queue via a pin (``watch_mode =
    pinned``). Both the current version and its enrichment are left-joined, not
    required: a post can be pinned straight from its evidence card or the CLI
    before it has a full-fidelity version (e.g. a preview-only sighting) or any
    enrichment, and it must still appear here so the list matches the toolbar
    ``已钉住`` count one-for-one. Ordered newest-observation first so a pinned
    post that just changed resurfaces to the top.
    """
    if limit < 1:
        raise ValueError("timeline limit must be positive")
    if not prompt_version.strip():
        raise ValueError("prompt_version must not be empty")
    rows = connection.execute(
        f"""
        SELECT{_QUEUE_CARD_COLUMNS}
        FROM posts p
        JOIN authors a ON a.id = p.author_id
        LEFT JOIN post_versions v ON v.id = p.current_version_id
        LEFT JOIN enrichments e
            ON e.version_id = p.current_version_id AND e.prompt_version = ?
        WHERE p.watch_mode = ?
        ORDER BY COALESCE(current_version_last_observed_at, v.first_observed_at) DESC, p.id DESC
        LIMIT ?
        """,
        (prompt_version.strip(), WatchMode.PINNED.value, limit),
    ).fetchall()
    return [_post_projection(row) for row in rows]
