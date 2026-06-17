"""Retrospective topic recall: deterministic, citation-anchored retrieval.

Phase 11 (主题回溯). Given a topic expressed as keyword *groups* plus a time
window, return the observed versions that match — verbatim snippets, version ids,
source state and whether the post was later removed — with coverage and selection
counts alongside. This module performs **no LLM call and generates no prose**: it
is pure SQL over the archive, so its output carries zero hallucination risk and
is fully auditable. The optional brief synthesis (the only token-spending,
hallucination-capable step) lives elsewhere and is gated behind an explicit
action; this layer is the free, honest base it rests on.

Matching is *grouped* to fight noise: within a group the terms are OR-ed (油价 /
原油 / 布油 …), across groups they are AND-ed by default (a hit must mention both
the event *and* the market), with an opt-in relaxation to OR so recall can be
loosened when a window is sparse. Every text source the archive holds is
searched — version body, the enrichment stance summary, the extracted framework
fields, and image OCR — so a judgement stated only in a chart's OCR or a
framework summary is still found.
"""

from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta, timezone

from kol_archive.maintenance import redact_text
from kol_archive.presentation import version_descriptive_market_snapshots

# Beijing wall-clock is the natural frame for "那几周": a user thinking "美伊冲突
# 那段时间" means local calendar days, not UTC. Naive --from/--to dates are read
# in +08:00 and converted to UTC for storage-aligned comparison.
LOCAL_TZ_OFFSET_HOURS = 8


@dataclass(frozen=True)
class TermGroup:
    """One OR-set of keywords (e.g. the market group: 油价/原油/布油/WTI/能源)."""

    label: str
    terms: tuple[str, ...]


@dataclass(frozen=True)
class RetrievalQuery:
    """A normalized recall request: grouped terms, optional tickers, a window."""

    groups: tuple[TermGroup, ...]
    date_from: str
    date_to: str
    tickers: tuple[str, ...] = ()
    require_all_groups: bool = True
    limit: int = 200
    question: str = ""

    def validate(self) -> None:
        if not self.groups or not any(group.terms for group in self.groups):
            raise ValueError("at least one keyword group with terms is required")
        if self.limit < 1:
            raise ValueError("limit must be positive")
        # Surface a malformed window early rather than as an empty result set.
        parse_window_bound(self.date_from)
        parse_window_bound(self.date_to)


def parse_window_bound(value: str, *, end_of_day: bool = False) -> str:
    """Normalize a --from/--to bound to a UTC ISO timestamp.

    Accepts a bare ``YYYY-MM-DD`` (read as Beijing local time) or a full ISO
    timestamp (its own offset is honoured). A bare end date expands to the end of
    that local day so ``--to 2025-06-30`` includes everything posted on the 30th.
    """
    text = value.strip()
    if not text:
        raise ValueError("window bound must not be empty")
    if "T" in text or " " in text:
        parsed = datetime.fromisoformat(text)
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=_local_tz())
        return parsed.astimezone(UTC).isoformat()
    day = datetime.fromisoformat(text).date()
    clock = (23, 59, 59) if end_of_day else (0, 0, 0)
    local = datetime(day.year, day.month, day.day, *clock, tzinfo=_local_tz())
    return local.astimezone(UTC).isoformat()


def _local_tz() -> timezone:
    return timezone(timedelta(hours=LOCAL_TZ_OFFSET_HOURS))


def _escape_like(term: str) -> str:
    """Escape LIKE metacharacters so a literal keyword stays literal."""
    return term.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


# Each clause carries its own bound parameters; ESCAPE keeps %/_ in a keyword
# literal. A version satisfies a *term* if it appears in any text source.
def _term_clause(term: str) -> tuple[str, list[str]]:
    like = f"%{_escape_like(term)}%"
    clause = (
        "(v.content_text LIKE ? ESCAPE '\\'"
        " OR EXISTS (SELECT 1 FROM enrichments e"
        "   WHERE e.version_id = v.id AND e.stance_summary LIKE ? ESCAPE '\\')"
        " OR EXISTS (SELECT 1 FROM framework_extractions f"
        "   WHERE f.version_id = v.id AND ("
        "     f.topic LIKE ? ESCAPE '\\' OR f.summary LIKE ? ESCAPE '\\'"
        "     OR f.logic_chain LIKE ? ESCAPE '\\' OR f.conclusion_shape LIKE ? ESCAPE '\\'"
        "     OR f.applicability_conditions LIKE ? ESCAPE '\\'"
        "     OR f.invalidation_conditions LIKE ? ESCAPE '\\'))"
        " OR EXISTS (SELECT 1 FROM image_ocr io JOIN post_images pi ON pi.id = io.image_id"
        "   WHERE pi.version_id = v.id AND io.ocr_text LIKE ? ESCAPE '\\'))"
    )
    return clause, [like] * 9


def _group_clause(group: TermGroup) -> tuple[str, list[str]]:
    parts: list[str] = []
    params: list[str] = []
    for term in group.terms:
        clause, clause_params = _term_clause(term)
        parts.append(clause)
        params.extend(clause_params)
    return "(" + " OR ".join(parts) + ")", params


def _ticker_clause(tickers: tuple[str, ...]) -> tuple[str, list[str]]:
    placeholders = ",".join("?" for _ in tickers)
    clause = (
        "EXISTS (SELECT 1 FROM version_tickers vt"
        f" WHERE vt.version_id = v.id AND vt.ticker IN ({placeholders}))"
    )
    return clause, [ticker.upper() for ticker in tickers]


def _where(query: RetrievalQuery) -> tuple[str, list[object]]:
    """Build the shared WHERE: live versions, in window, matching the groups."""
    clauses: list[str] = [
        "v.ingest_mode = 'live'",
        "datetime(COALESCE(p.posted_at_claimed, v.first_observed_at))"
        " BETWEEN datetime(?) AND datetime(?)",
    ]
    params: list[object] = [
        parse_window_bound(query.date_from),
        parse_window_bound(query.date_to, end_of_day=True),
    ]
    group_sqls: list[str] = []
    for group in query.groups:
        if not group.terms:
            continue
        clause, group_params = _group_clause(group)
        group_sqls.append(clause)
        params.extend(group_params)
    joiner = " AND " if query.require_all_groups else " OR "
    clauses.append("(" + joiner.join(group_sqls) + ")")
    if query.tickers:
        clause, ticker_params = _ticker_clause(query.tickers)
        clauses.append(clause)
        params.extend(ticker_params)
    return " AND ".join(clauses), params


_HIT_COLUMNS = """
    v.id AS version_id,
    v.post_id,
    a.platform_uid AS author_platform_uid,
    COALESCE(json_extract(v.raw_payload, '$.user.screen_name'), a.notes) AS author_display_name,
    COALESCE(p.posted_at_claimed, v.first_observed_at) AS viewpoint_at,
    v.first_observed_at,
    v.content_text,
    p.url,
    p.source_state,
    (
        SELECT e.stance_summary FROM enrichments e
        WHERE e.version_id = v.id AND e.stance_summary != ''
        ORDER BY e.id DESC LIMIT 1
    ) AS stance_summary,
    (
        SELECT json_group_array(DISTINCT f.topic) FROM framework_extractions f
        WHERE f.version_id = v.id
    ) AS framework_topics,
    EXISTS (
        SELECT 1 FROM post_events pe
        WHERE pe.post_id = p.id
          AND pe.dimension = 'source_state'
          AND pe.to_value = 'gone_confirmed'
    ) AS removed
"""


def retrieve(
    connection: sqlite3.Connection,
    query: RetrievalQuery,
    *,
    prompt_version: str = "enrich-v2",
    benchmark_ticker: str = "SH000300",
) -> dict[str, object]:
    """Run the deterministic recall and return hits + coverage + selection.

    ``prompt_version``/``benchmark_ticker`` are only used to attach existing
    descriptive market snapshots to hits; they never gate which versions match.
    """
    query.validate()
    where, params = _where(query)
    rows = connection.execute(
        f"""
        SELECT{_HIT_COLUMNS}
        FROM post_versions v
        JOIN posts p ON p.id = v.post_id
        JOIN authors a ON a.id = p.author_id
        WHERE {where}
        ORDER BY datetime(COALESCE(p.posted_at_claimed, v.first_observed_at)) ASC, v.id ASC
        LIMIT ?
        """,
        (*params, query.limit),
    ).fetchall()

    version_ids = {int(row["version_id"]) for row in rows}
    snapshots = version_descriptive_market_snapshots(
        connection, version_ids, prompt_version, benchmark_ticker
    )

    hits: list[dict[str, object]] = []
    for row in rows:
        version_id = int(row["version_id"])
        topics = _json_list(row["framework_topics"])
        hits.append(
            {
                "version_id": version_id,
                "post_id": int(row["post_id"]),
                "author_platform_uid": row["author_platform_uid"],
                "author_display_name": redact_text(str(row["author_display_name"]))
                if row["author_display_name"] is not None
                else None,
                "viewpoint_at": row["viewpoint_at"],
                "first_observed_at": row["first_observed_at"],
                "content_text": row["content_text"],
                "url": row["url"],
                "source_state": row["source_state"],
                "removed": bool(row["removed"]),
                "stance_summary": row["stance_summary"] or "",
                "framework_topics": topics,
                "market_snapshot": snapshots.get(version_id),
            }
        )

    return {
        "query": _query_echo(query),
        "coverage": _coverage(connection, query, where, params),
        "selection": _selection(connection, where, params),
        "hits": hits,
    }


def _coverage(
    connection: sqlite3.Connection,
    query: RetrievalQuery,
    where: str,
    params: list[object],
) -> dict[str, object]:
    """Honest denominators: how thin is this evidence, and split how across groups.

    Computed by independent aggregate queries over the *full* match set, not the
    ``--limit``-capped detail rows — a popular window must report its true author
    and post counts, never an undercount of the first N. Each group also gets its
    own in-window hit count (event-only vs market-only) to expose where the
    overlap is — or isn't.
    """
    totals = connection.execute(
        f"""
        SELECT
            COUNT(*) AS version_count,
            COUNT(DISTINCT p.author_id) AS author_count,
            COUNT(DISTINCT v.post_id) AS post_count
        FROM post_versions v
        JOIN posts p ON p.id = v.post_id
        WHERE {where}
        """,
        params,
    ).fetchone()
    group_counts: list[dict[str, object]] = []
    for group in query.groups:
        if not group.terms:
            continue
        group_counts.append(
            {
                "label": group.label,
                "terms": list(group.terms),
                "version_count": _count_group_versions(connection, query, group),
            }
        )
    return {
        "version_count": int(totals["version_count"]),
        "author_count": int(totals["author_count"]),
        "post_count": int(totals["post_count"]),
        "groups": group_counts,
        "require_all_groups": query.require_all_groups,
    }


def _count_group_versions(
    connection: sqlite3.Connection,
    query: RetrievalQuery,
    group: TermGroup,
) -> int:
    """In-window live versions matching this single group (+ ticker filter)."""
    clauses = [
        "v.ingest_mode = 'live'",
        "datetime(COALESCE(p.posted_at_claimed, v.first_observed_at))"
        " BETWEEN datetime(?) AND datetime(?)",
    ]
    params: list[object] = [
        parse_window_bound(query.date_from),
        parse_window_bound(query.date_to, end_of_day=True),
    ]
    group_sql, group_params = _group_clause(group)
    clauses.append(group_sql)
    params.extend(group_params)
    if query.tickers:
        ticker_sql, ticker_params = _ticker_clause(query.tickers)
        clauses.append(ticker_sql)
        params.extend(ticker_params)
    row = connection.execute(
        f"""
        SELECT COUNT(*) AS n
        FROM post_versions v
        JOIN posts p ON p.id = v.post_id
        WHERE {" AND ".join(clauses)}
        """,
        params,
    ).fetchone()
    return int(row["n"])


def _selection(
    connection: sqlite3.Connection,
    where: str,
    params: list[object],
) -> dict[str, object]:
    """Selection signals: which matched posts were later removed at the source.

    Aggregated over the *full* match set (not the ``--limit``-capped detail), so
    the removed share is honest even when a window overflows the page. Recall over
    a surviving corpus over-represents what was not deleted; surfacing the removed
    posts neutrally (no attribution — charter 4/7) lets the reader discount a
    clean-looking picture rather than trust it.
    """
    rows = connection.execute(
        f"""
        SELECT DISTINCT v.post_id
        FROM post_versions v
        JOIN posts p ON p.id = v.post_id
        WHERE {where}
          AND EXISTS (
              SELECT 1 FROM post_events pe
              WHERE pe.post_id = p.id
                AND pe.dimension = 'source_state'
                AND pe.to_value = 'gone_confirmed'
          )
        ORDER BY v.post_id
        """,
        params,
    ).fetchall()
    removed_post_ids = [int(row["post_id"]) for row in rows]
    return {
        "removed_post_count": len(removed_post_ids),
        "removed_post_ids": removed_post_ids,
    }


def _query_echo(query: RetrievalQuery) -> dict[str, object]:
    return {
        "question": query.question,
        "groups": [{"label": g.label, "terms": list(g.terms)} for g in query.groups if g.terms],
        "tickers": list(query.tickers),
        "date_from": parse_window_bound(query.date_from),
        "date_to": parse_window_bound(query.date_to, end_of_day=True),
        "require_all_groups": query.require_all_groups,
        "limit": query.limit,
    }


def _json_list(value: object) -> list[str]:
    if value is None:
        return []
    parsed = json.loads(str(value))
    return [str(item) for item in parsed if item is not None]
