"""Read-only timeline and evidence-card projections for the local CLI."""

from __future__ import annotations

import json
import re
import sqlite3
from datetime import datetime
from difflib import unified_diff
from typing import Any, cast

from kol_archive.maintenance import redact_text
from kol_archive.market import OUTCOME_METHOD_VERSION, common_close_returns, local_market_date
from kol_archive.models import FeedState, SourceState, WatchMode

_CN_TICKER = re.compile(r"^(?:SH|SZ|BJ)\d{6}$")
_TICKER_NAME = re.compile(r"\$([^$()]+)\(((?:SH|SZ|BJ)\d{6})\)\$")
_MARKET_RELATED_VIEWPOINT_SQL = """
(e.is_market_related = 1
 OR EXISTS (SELECT 1 FROM claims market_claim WHERE market_claim.version_id = e.version_id))
"""


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
        LIMIT ?
        """,
        (prompt_version.strip(), limit),
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


def _viewpoint_ticker(viewpoint: dict[str, object]) -> str | None:
    text_matches = {
        ticker for _, ticker in _TICKER_NAME.findall(str(viewpoint.get("current_text") or ""))
    }
    if len(text_matches) == 1:
        return str(next(iter(text_matches)))
    outcome_matches = {
        str(outcome["ticker"])
        for outcome in cast(list[dict[str, object]], viewpoint.get("market_outcomes") or [])
        if _CN_TICKER.fullmatch(str(outcome.get("ticker") or ""))
    }
    if len(outcome_matches) == 1:
        return str(next(iter(outcome_matches)))
    raw = viewpoint.get("raw_payload")
    if not isinstance(raw, str) or not raw:
        return None
    try:
        payload = json.loads(raw)
    except json.JSONDecodeError:
        return None
    tickers: set[str] = set()

    def visit(value: object) -> None:
        if isinstance(value, dict):
            correlation = value.get("stockCorrelation")
            if isinstance(correlation, list):
                for item in correlation:
                    if _CN_TICKER.fullmatch(str(item)):
                        tickers.add(str(item))
                    elif isinstance(item, dict):
                        for key in ("symbol", "ticker", "code"):
                            symbol = str(item.get(key) or "")
                            if _CN_TICKER.fullmatch(symbol):
                                tickers.add(symbol)
            for child in value.values():
                visit(child)
        elif isinstance(value, list):
            for child in value:
                visit(child)

    visit(payload)
    return next(iter(tickers)) if len(tickers) == 1 else None


def _within_viewpoint_cluster_window(newer: object, older: object, *, days: int = 7) -> bool:
    try:
        delta = datetime.fromisoformat(str(newer)) - datetime.fromisoformat(str(older))
    except ValueError:
        return False
    return 0 <= delta.total_seconds() < days * 24 * 60 * 60


def _viewpoint_ticker_name(viewpoint: dict[str, object], ticker: str) -> str | None:
    for name, found_ticker in _TICKER_NAME.findall(str(viewpoint.get("current_text") or "")):
        if found_ticker == ticker:
            return str(name)
    raw = viewpoint.get("raw_payload")
    if not isinstance(raw, str) or not raw:
        return None
    try:
        payload = json.loads(raw)
    except json.JSONDecodeError:
        return None

    def visit(value: object) -> str | None:
        if isinstance(value, dict):
            symbols = {str(value.get(key) or "") for key in ("symbol", "ticker", "code")}
            if ticker in symbols:
                for key in ("name", "stockName", "title"):
                    name = str(value.get(key) or "").strip()
                    if name:
                        return name
            for child in value.values():
                found = visit(child)
                if found:
                    return found
        elif isinstance(value, list):
            for child in value:
                found = visit(child)
                if found:
                    return found
        return None

    return visit(payload)


def _local_ticker_name(connection: sqlite3.Connection, ticker: str) -> str | None:
    row = connection.execute("SELECT name FROM ticker_names WHERE ticker = ?", (ticker,)).fetchone()
    return str(row["name"]) if row is not None else None


def _descriptive_market_snapshot(
    connection: sqlite3.Connection,
    ticker: str,
    viewpoint_at: object,
    benchmark_ticker: str,
) -> dict[str, object] | None:
    try:
        snapshot = common_close_returns(connection, ticker, benchmark_ticker, str(viewpoint_at))
    except ValueError:
        return None
    if snapshot is None:
        return None
    return {
        **snapshot,
        "series": _daily_series(
            connection,
            ticker,
            benchmark_ticker,
            str(snapshot["start_date"]),
            str(snapshot["end_date"]),
        ),
    }


def version_descriptive_market_snapshots(
    connection: sqlite3.Connection,
    version_ids: set[int],
    prompt_version: str,
    benchmark_ticker: str,
) -> dict[int, dict[str, object]]:
    """Return existing descriptive market snapshots for enriched viewpoint versions."""
    if not version_ids:
        return {}
    if not prompt_version.strip():
        raise ValueError("prompt_version must not be empty")
    placeholders = ",".join("?" for _ in version_ids)
    rows = connection.execute(
        f"""
        SELECT
            v.id AS version_id,
            v.content_text AS current_text,
            v.raw_payload,
            v.first_observed_at AS viewpoint_at,
            (
                SELECT json_group_array(c.ticker)
                FROM claims c
                WHERE c.version_id = v.id
            ) AS claim_tickers
        FROM post_versions v
        JOIN enrichments e ON e.version_id = v.id AND e.prompt_version = ?
        WHERE v.id IN ({placeholders})
          AND e.post_type = '观点'
          AND {_MARKET_RELATED_VIEWPOINT_SQL}
        ORDER BY v.id
        """,
        (prompt_version.strip(), *sorted(version_ids)),
    ).fetchall()
    targets: list[tuple[int, str, str, str | None]] = []
    for row in rows:
        claim_tickers = json.loads(str(row["claim_tickers"] or "[]"))
        viewpoint = {
            "current_text": row["current_text"],
            "raw_payload": row["raw_payload"],
            "viewpoint_at": row["viewpoint_at"],
            "market_outcomes": [{"ticker": ticker} for ticker in claim_tickers],
        }
        ticker = _viewpoint_ticker(viewpoint)
        if ticker is None:
            continue
        targets.append(
            (
                int(row["version_id"]),
                ticker,
                local_market_date(str(row["viewpoint_at"])).isoformat(),
                _viewpoint_ticker_name(viewpoint, ticker),
            )
        )
    if not targets:
        return {}
    values = ",".join("(?, ?, ?, ?)" for _ in targets)
    parameters: list[object] = []
    for version_id, ticker, observed_date, payload_name in targets:
        parameters.extend((version_id, ticker, observed_date, payload_name))
    price_rows = connection.execute(
        f"""
        WITH targets(version_id, ticker, observed_date, payload_name) AS (
            VALUES {values}
        )
        SELECT
            targets.version_id,
            targets.ticker,
            COALESCE(targets.payload_name, names.name) AS ticker_name,
            start.date AS start_date,
            start.asset_close AS asset_start,
            start.benchmark_close AS benchmark_start,
            finish.date AS end_date,
            finish.asset_close AS asset_end,
            finish.benchmark_close AS benchmark_end
        FROM targets
        LEFT JOIN ticker_names names ON names.ticker = targets.ticker
        LEFT JOIN (
            SELECT t.version_id, asset.date, asset.close AS asset_close,
                   benchmark.close AS benchmark_close
            FROM targets t
            JOIN prices asset ON asset.ticker = t.ticker
            JOIN prices benchmark
                ON benchmark.ticker = ? AND benchmark.date = asset.date
            WHERE asset.date = (
                SELECT MAX(prior.date) FROM prices prior
                JOIN prices prior_benchmark
                    ON prior_benchmark.ticker = ? AND prior_benchmark.date = prior.date
                WHERE prior.ticker = t.ticker AND prior.date < t.observed_date
            )
        ) start ON start.version_id = targets.version_id
        LEFT JOIN (
            SELECT t.version_id, asset.date, asset.close AS asset_close,
                   benchmark.close AS benchmark_close
            FROM targets t
            JOIN prices asset ON asset.ticker = t.ticker
            JOIN prices benchmark
                ON benchmark.ticker = ? AND benchmark.date = asset.date
            WHERE asset.date = (
                SELECT MAX(latest.date) FROM prices latest
                JOIN prices latest_benchmark
                    ON latest_benchmark.ticker = ? AND latest_benchmark.date = latest.date
                WHERE latest.ticker = t.ticker AND latest.date >= t.observed_date
            )
        ) finish ON finish.version_id = targets.version_id
        ORDER BY targets.version_id
        """,
        (*parameters, benchmark_ticker, benchmark_ticker, benchmark_ticker, benchmark_ticker),
    ).fetchall()
    snapshots: dict[int, dict[str, object]] = {}
    for row in price_rows:
        if row["start_date"] is None or row["end_date"] is None:
            continue
        asset_start = float(row["asset_start"])
        benchmark_start = float(row["benchmark_start"])
        if asset_start == 0 or benchmark_start == 0:
            continue
        raw_return = float(row["asset_end"]) / asset_start - 1
        benchmark_return = float(row["benchmark_end"]) / benchmark_start - 1
        snapshots[int(row["version_id"])] = {
            "ticker": row["ticker"],
            "ticker_name": row["ticker_name"],
            "benchmark_ticker": benchmark_ticker,
            "start_date": row["start_date"],
            "end_date": row["end_date"],
            "raw_return": raw_return,
            "benchmark_return": benchmark_return,
            "excess_return": raw_return - benchmark_return,
            "method_version": OUTCOME_METHOD_VERSION,
        }
    return snapshots


def _daily_series(
    connection: sqlite3.Connection,
    ticker: str,
    benchmark_ticker: str,
    start_date: str,
    end_date: str,
) -> list[dict[str, object]]:
    """Per-trading-day asset OHLC and benchmark close across the snapshot window.

    OHLC is NULL for close-only CSV rows; the frontend draws a close line when open is
    absent and a candlestick when it is present (Xueqiu kline bars).
    """
    rows = connection.execute(
        """
        SELECT asset.date AS date,
               asset.open AS asset_open,
               asset.high AS asset_high,
               asset.low AS asset_low,
               asset.close AS asset_close,
               benchmark.close AS benchmark_close
        FROM prices asset
        JOIN prices benchmark ON benchmark.date = asset.date AND benchmark.ticker = ?
        WHERE asset.ticker = ? AND asset.date >= ? AND asset.date <= ?
        ORDER BY asset.date ASC
        """,
        (benchmark_ticker, ticker, start_date, end_date),
    ).fetchall()
    return [
        {
            "date": row["date"],
            "open": row["asset_open"],
            "high": row["asset_high"],
            "low": row["asset_low"],
            "close": row["asset_close"],
            "benchmark_close": row["benchmark_close"],
        }
        for row in rows
    ]


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
        (prompt_version.strip(),),
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
