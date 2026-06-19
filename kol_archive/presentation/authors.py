"""Per-author projections: scorecards, viewpoints, clusters, and profiles."""

from __future__ import annotations

import sqlite3
from typing import cast

from kol_archive.models import WatchMode

from .common import _MARKET_RELATED_VIEWPOINT_SQL, _post_projection, _row_dict
from .market import (
    _descriptive_market_snapshot,
    _local_ticker_name,
    _viewpoint_ticker,
    _viewpoint_ticker_name,
    _within_viewpoint_cluster_window,
)


def author_scorecards(connection: sqlite3.Connection, prompt_version: str) -> dict[str, object]:
    """Per-author label *composition*: a diagnostic summary, never a ranking.

    Charter §0.11 forbids cross-person leaderboards and prominent hit-rate
    metrics, so this returns only the raw counts (enriched total, per-label
    counts, genre mix) in a stable author-id order, with **no hit-rate percentage
    and no density sort**. A reader sees what each author tends to produce; the
    tool does not compute or rank a "who is best" verdict. ``label_scale`` (the
    global max label count) lets bar lengths stay comparable as composition, not
    as a score.
    """
    if not prompt_version.strip():
        raise ValueError("prompt_version must not be empty")
    rows = connection.execute(
        """
        SELECT
            a.id AS author_id,
            a.platform_uid AS author_platform_uid,
            a.notes AS author_name,
            COUNT(*) AS enriched,
            SUM(e.label_first_hand_info) AS first_hand,
            SUM(e.label_transferable_framework) AS framework,
            SUM(e.label_reasoned_non_consensus) AS non_consensus,
            SUM(
                CASE WHEN e.label_first_hand_info = 1
                      OR e.label_transferable_framework = 1
                      OR e.label_reasoned_non_consensus = 1 THEN 1 ELSE 0 END
            ) AS hit
        FROM enrichments e
        JOIN posts p ON p.id = e.post_id
        JOIN authors a ON a.id = p.author_id
        WHERE e.prompt_version = ?
        GROUP BY a.id
        ORDER BY a.id
        """,
        (prompt_version.strip(),),
    ).fetchall()
    cards: list[dict[str, object]] = []
    label_scale = 1
    for row in rows:
        enriched = int(row["enriched"])
        hit = int(row["hit"] or 0)
        first_hand = int(row["first_hand"] or 0)
        framework = int(row["framework"] or 0)
        non_consensus = int(row["non_consensus"] or 0)
        label_scale = max(label_scale, first_hand, framework, non_consensus)
        genres = [
            {"post_type": genre["post_type"], "count": int(genre["count"])}
            for genre in connection.execute(
                """
                SELECT e.post_type, COUNT(*) AS count
                FROM enrichments e
                JOIN posts p ON p.id = e.post_id
                WHERE p.author_id = ? AND e.prompt_version = ?
                GROUP BY e.post_type ORDER BY count DESC, e.post_type
                """,
                (int(row["author_id"]), prompt_version.strip()),
            ).fetchall()
        ]
        cards.append(
            {
                "author_platform_uid": row["author_platform_uid"],
                "author_name": row["author_name"],
                "enriched": enriched,
                "hit": hit,
                "first_hand": first_hand,
                "framework": framework,
                "non_consensus": non_consensus,
                "genres": genres,
            }
        )
    return {"scorecards": cards, "label_scale": label_scale}


def author_recent_viewpoints(
    connection: sqlite3.Connection,
    platform_uid: str,
    prompt_version: str,
    *,
    limit: int = 10,
) -> list[dict[str, object]]:
    """Return one author's latest enriched viewpoints and any market outcomes."""
    if limit < 1:
        raise ValueError("viewpoint limit must be positive")
    if not platform_uid.strip():
        raise ValueError("author uid must not be empty")
    if not prompt_version.strip():
        raise ValueError("prompt_version must not be empty")
    rows = connection.execute(
        f"""
        SELECT
            p.id AS post_id,
            p.platform_post_id,
            p.url,
            p.source_state,
            p.posted_at_claimed,
            p.first_seen_at,
            COALESCE(p.posted_at_claimed, v.first_observed_at, p.first_seen_at) AS viewpoint_at,
            p.current_version_id AS version_id,
            v.content_text AS current_text,
            v.raw_payload,
            v.first_observed_at AS viewpoint_first_observed_at,
            e.rationale AS enrichment_rationale,
            e.evidence_snippet AS enrichment_evidence_snippet,
            e.stance_summary AS enrichment_stance_summary
        FROM posts p
        JOIN authors a ON a.id = p.author_id
        JOIN post_versions v ON v.id = p.current_version_id
        JOIN enrichments e
            ON e.version_id = p.current_version_id AND e.prompt_version = ?
        WHERE a.platform = 'xueqiu'
          AND a.platform_uid = ?
          AND e.post_type = '观点'
          AND {_MARKET_RELATED_VIEWPOINT_SQL}
        ORDER BY
            COALESCE(p.posted_at_claimed, v.first_observed_at, p.first_seen_at) DESC,
            p.id DESC
        LIMIT ?
        """,
        (prompt_version.strip(), platform_uid.strip(), limit),
    ).fetchall()
    viewpoints = [_row_dict(row) for row in rows]
    outcomes_by_version: dict[int, list[dict[str, object]]] = {}
    if rows:
        version_ids = [int(row["version_id"]) for row in rows]
        placeholders = ",".join("?" for _ in version_ids)
        outcomes = connection.execute(
            f"""
            SELECT
                c.version_id,
                c.id AS claim_id,
                c.ticker,
                c.direction,
                c.horizon_days,
                c.target_price,
                c.confidence_phrasing,
                c.claim_made_at,
                c.status,
                o.resolved_at,
                o.raw_return,
                o.benchmark_return,
                o.excess_return,
                o.benchmark_ticker,
                o.outcome_method_version,
                o.notes AS outcome_notes
            FROM claims c
            LEFT JOIN claim_outcomes o ON o.claim_id = c.id
            WHERE c.version_id IN ({placeholders})
            ORDER BY c.id
            """,
            version_ids,
        ).fetchall()
        for outcome in outcomes:
            outcomes_by_version.setdefault(int(outcome["version_id"]), []).append(
                _row_dict(outcome)
            )
    for viewpoint in viewpoints:
        viewpoint["market_outcomes"] = outcomes_by_version.get(
            int(str(viewpoint["version_id"])), []
        )
    return viewpoints


def author_recent_viewpoint_clusters(
    connection: sqlite3.Connection,
    platform_uid: str,
    prompt_version: str,
    *,
    limit: int = 10,
    benchmark_ticker: str = "SH000300",
    cluster_window_days: int = 7,
) -> list[dict[str, object]]:
    """Group one author's recent viewpoints by explicit A-share ticker evidence."""
    if cluster_window_days < 1:
        raise ValueError("cluster_window_days must be positive")
    viewpoints = author_recent_viewpoints(
        connection, platform_uid, prompt_version, limit=max(limit * 10, 50)
    )
    clusters: list[dict[str, object]] = []
    for viewpoint in viewpoints:
        ticker = _viewpoint_ticker(viewpoint)
        cluster = next(
            (
                item
                for item in clusters
                if ticker
                and ticker == item["ticker"]
                and _within_viewpoint_cluster_window(
                    item["first_at"], viewpoint.get("viewpoint_at"), days=cluster_window_days
                )
            ),
            None,
        )
        if cluster is None:
            cluster = {
                "cluster_key": ticker or f"post-{viewpoint['post_id']}",
                "ticker": ticker,
                "viewpoints": [],
                "latest_at": viewpoint.get("viewpoint_at"),
                "first_at": viewpoint.get("viewpoint_at"),
            }
            clusters.append(cluster)
        cast(list[dict[str, object]], cluster["viewpoints"]).append(viewpoint)
        cluster["first_at"] = viewpoint.get("viewpoint_at")
    for cluster in clusters:
        items = cast(list[dict[str, object]], cluster["viewpoints"])
        primary = cast(str | None, cluster["ticker"])
        name = None
        if primary:
            name = _local_ticker_name(connection, primary)
            for item in items:
                payload_name = _viewpoint_ticker_name(item, primary)
                if payload_name:
                    name = payload_name
                    break
        cluster["title"] = f"{name}（{primary}）" if name and primary else primary or "独立观点"
        cluster["statement_count"] = len(items)
        cluster["market_snapshot"] = (
            _descriptive_market_snapshot(connection, primary, cluster["first_at"], benchmark_ticker)
            if primary
            else None
        )
    return clusters[:limit]


def author_viewpoint_overview(
    connection: sqlite3.Connection,
    prompt_version: str,
) -> list[dict[str, object]]:
    """Return lightweight per-author viewpoint counts, in stable author order."""
    if not prompt_version.strip():
        raise ValueError("prompt_version must not be empty")
    rows = connection.execute(
        f"""
        SELECT
            a.id AS author_id,
            a.platform_uid AS author_platform_uid,
            a.notes AS author_name,
            (
                SELECT COALESCE(json_extract(v.raw_payload, '$.user.screen_name'), a.notes)
                FROM posts p JOIN post_versions v ON v.id = p.current_version_id
                WHERE p.author_id = a.id
                ORDER BY COALESCE(p.posted_at_claimed, p.last_present_at, p.first_seen_at) DESC,
                         p.id DESC
                LIMIT 1
            ) AS author_display_name,
            (
                SELECT json_extract(v.raw_payload, '$.user.profile_image_url')
                FROM posts p JOIN post_versions v ON v.id = p.current_version_id
                WHERE p.author_id = a.id
                ORDER BY COALESCE(p.posted_at_claimed, p.last_present_at, p.first_seen_at) DESC,
                         p.id DESC
                LIMIT 1
            ) AS author_avatar_url,
            COUNT(e.id) AS viewpoint_count,
            MAX(COALESCE(p.posted_at_claimed, p.last_present_at, p.first_seen_at))
                AS latest_post_at,
            MAX(
                CASE WHEN e.id IS NOT NULL
                    THEN COALESCE(p.posted_at_claimed, p.last_present_at, p.first_seen_at)
                END
            ) AS latest_viewpoint_at,
            (
                SELECT COUNT(*)
                FROM posts pending_post
                WHERE pending_post.author_id = a.id
                  AND pending_post.current_version_id IS NOT NULL
                  AND NOT EXISTS (
                      SELECT 1 FROM enrichments pending_enrichment
                      WHERE pending_enrichment.version_id = pending_post.current_version_id
                        AND pending_enrichment.prompt_version = ?
                  )
            ) AS pending_enrichment_count,
            (
                SELECT MAX(latest_enrichment.created_at)
                FROM enrichments latest_enrichment
                JOIN posts latest_enriched_post
                    ON latest_enriched_post.current_version_id = latest_enrichment.version_id
                WHERE latest_enriched_post.author_id = a.id
                  AND latest_enrichment.prompt_version = ?
            ) AS latest_enrichment_at,
            SUM(
                CASE WHEN EXISTS (
                    SELECT 1
                    FROM claims c JOIN claim_outcomes o ON o.claim_id = c.id
                    WHERE c.version_id = e.version_id
                ) THEN 1 ELSE 0 END
            ) AS evaluated_viewpoint_count
        FROM authors a
        LEFT JOIN posts p ON p.author_id = a.id
        LEFT JOIN enrichments e
            ON e.version_id = p.current_version_id
            AND e.prompt_version = ?
            AND e.post_type = '观点'
            AND {_MARKET_RELATED_VIEWPOINT_SQL}
        WHERE a.platform = 'xueqiu'
        GROUP BY a.id
        ORDER BY a.id
        """,
        (prompt_version.strip(), prompt_version.strip(), prompt_version.strip()),
    ).fetchall()
    return [_row_dict(row) for row in rows]


def author_profile(
    connection: sqlite3.Connection,
    platform_uid: str,
    *,
    limit: int = 30,
    prompt_version: str = "enrich-v1",
    benchmark_ticker: str = "SH000300",
    cluster_window_days: int = 7,
) -> dict[str, object]:
    if limit < 1:
        raise ValueError("author post limit must be positive")
    if not platform_uid.strip():
        raise ValueError("author uid must not be empty")
    row = connection.execute(
        """
        SELECT
            a.id AS author_id,
            a.platform,
            a.platform_uid AS author_platform_uid,
            a.notes AS author_name,
            a.live_monitoring_started_at,
            COUNT(p.id) AS post_count,
            SUM(CASE WHEN p.ingest_mode = 'live' THEN 1 ELSE 0 END) AS live_post_count,
            SUM(CASE WHEN p.watch_mode = ? THEN 1 ELSE 0 END) AS pinned_count,
            (
                SELECT COALESCE(json_extract(v.raw_payload, '$.user.screen_name'), a.notes)
                FROM posts p2 JOIN post_versions v ON v.id = p2.current_version_id
                WHERE p2.author_id = a.id
                ORDER BY COALESCE(p2.posted_at_claimed, p2.last_present_at, p2.first_seen_at) DESC,
                         p2.id DESC
                LIMIT 1
            ) AS author_display_name,
            (
                SELECT json_extract(v.raw_payload, '$.user.profile_image_url')
                FROM posts p2 JOIN post_versions v ON v.id = p2.current_version_id
                WHERE p2.author_id = a.id
                ORDER BY COALESCE(p2.posted_at_claimed, p2.last_present_at, p2.first_seen_at) DESC,
                         p2.id DESC
                LIMIT 1
            ) AS author_avatar_url,
            (
                SELECT json_extract(v.raw_payload, '$.user.description')
                FROM posts p2 JOIN post_versions v ON v.id = p2.current_version_id
                WHERE p2.author_id = a.id
                ORDER BY COALESCE(p2.posted_at_claimed, p2.last_present_at, p2.first_seen_at) DESC,
                         p2.id DESC
                LIMIT 1
            ) AS author_description
        FROM authors a
        LEFT JOIN posts p ON p.author_id = a.id
        WHERE a.platform = 'xueqiu' AND a.platform_uid = ?
        GROUP BY a.id
        """,
        (WatchMode.PINNED.value, platform_uid.strip()),
    ).fetchone()
    if row is None:
        raise ValueError(f"author not found: {platform_uid}")
    posts = connection.execute(
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
        WHERE a.platform = 'xueqiu' AND a.platform_uid = ?
        ORDER BY
            COALESCE(p.posted_at_claimed, p.last_present_at, p.first_seen_at) DESC,
            COALESCE(p.last_present_at, p.first_seen_at) DESC,
            p.id DESC
        LIMIT ?
        """,
        (platform_uid.strip(), limit),
    ).fetchall()
    return {
        "author": _row_dict(row),
        "posts": [_post_projection(post) for post in posts],
        "viewpoint_clusters": author_recent_viewpoint_clusters(
            connection,
            platform_uid,
            prompt_version,
            limit=10,
            benchmark_ticker=benchmark_ticker,
            cluster_window_days=cluster_window_days,
        ),
    }
