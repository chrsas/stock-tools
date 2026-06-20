"""SQLite schema, indexes, views, and evidence-protection triggers."""

from __future__ import annotations

import re
import sqlite3
from pathlib import Path

from kol_archive.market import extract_market_tickers, has_explicit_market_relation
from kol_archive.obs import install_db_tracing

EVIDENCE_TABLES = (
    "fetch_runs",
    "probe_runs",
    "post_observations",
    "post_versions",
    "post_events",
    "post_images",
)
DERIVED_APPEND_ONLY_TABLES = ("crowding_events", "crowding_event_members", "topic_briefs")

SCHEMA = """
CREATE TABLE IF NOT EXISTS authors (
    id INTEGER PRIMARY KEY,
    platform TEXT NOT NULL,
    platform_uid TEXT NOT NULL,
    live_monitoring_started_at TEXT NOT NULL,
    notes TEXT,
    UNIQUE(platform, platform_uid)
);

CREATE TABLE IF NOT EXISTS fetch_runs (
    id INTEGER PRIMARY KEY,
    author_id INTEGER NOT NULL REFERENCES authors(id),
    platform TEXT NOT NULL,
    started_at TEXT NOT NULL,
    finished_at TEXT NOT NULL,
    status TEXT NOT NULL CHECK(status IN ('ok', 'partial', 'failed')),
    login_state TEXT NOT NULL CHECK(login_state IN ('valid', 'expired', 'unknown')),
    pages_fetched INTEGER NOT NULL CHECK(pages_fetched >= 0),
    pagination_complete INTEGER NOT NULL CHECK(pagination_complete IN (0, 1)),
    covered_from TEXT,
    covered_to TEXT,
    rate_limited INTEGER NOT NULL CHECK(rate_limited IN (0, 1)),
    http_error_count INTEGER NOT NULL CHECK(http_error_count >= 0),
    ingest_mode TEXT NOT NULL CHECK(ingest_mode IN ('live', 'backfill')),
    adapter_version TEXT NOT NULL,
    parse_failure_count INTEGER NOT NULL DEFAULT 0 CHECK(parse_failure_count >= 0),
    reached_timeline_end INTEGER NOT NULL DEFAULT 0 CHECK(reached_timeline_end IN (0, 1)),
    notes TEXT
);

CREATE TABLE IF NOT EXISTS posts (
    id INTEGER PRIMARY KEY,
    author_id INTEGER NOT NULL REFERENCES authors(id),
    platform TEXT NOT NULL,
    platform_post_id TEXT NOT NULL,
    first_seen_at TEXT NOT NULL,
    last_present_at TEXT,
    current_version_id INTEGER REFERENCES post_versions(id),
    current_content_hash TEXT,
    current_image_manifest_hash TEXT,
    absent_healthy_streak INTEGER NOT NULL DEFAULT 0 CHECK(absent_healthy_streak >= 0),
    feed_state TEXT NOT NULL CHECK(
        feed_state IN ('present', 'absent_confirmed', 'out_of_scope', 'unknown')
    ),
    source_state TEXT NOT NULL CHECK(
        source_state IN ('reachable', 'gone_confirmed', 'unavailable', 'unknown')
    ),
    source_checked_at TEXT,
    watch_mode TEXT NOT NULL CHECK(watch_mode IN ('recent_window', 'pinned', 'inactive')),
    posted_at_claimed TEXT,
    url TEXT,
    ingest_mode TEXT NOT NULL CHECK(ingest_mode IN ('live', 'backfill')),
    raw_meta TEXT CHECK(raw_meta IS NULL OR json_valid(raw_meta)),
    UNIQUE(platform, platform_post_id)
);

CREATE TABLE IF NOT EXISTS post_versions (
    id INTEGER PRIMARY KEY,
    post_id INTEGER NOT NULL REFERENCES posts(id),
    content_text TEXT NOT NULL,
    content_hash TEXT NOT NULL,
    image_manifest_hash TEXT,
    first_observed_at TEXT NOT NULL,
    ingest_mode TEXT NOT NULL CHECK(ingest_mode IN ('live', 'backfill')),
    raw_payload TEXT CHECK(raw_payload IS NULL OR json_valid(raw_payload))
);

CREATE TABLE IF NOT EXISTS probe_runs (
    id INTEGER PRIMARY KEY,
    post_id INTEGER NOT NULL REFERENCES posts(id),
    started_at TEXT NOT NULL,
    finished_at TEXT NOT NULL,
    observed_at TEXT NOT NULL,
    status TEXT NOT NULL CHECK(status IN ('ok', 'partial', 'failed')),
    http_status INTEGER,
    login_state TEXT NOT NULL CHECK(login_state IN ('valid', 'expired', 'unknown')),
    rate_limited INTEGER NOT NULL CHECK(rate_limited IN (0, 1)),
    result TEXT NOT NULL CHECK(
        result IN ('reachable', 'explicitly_removed', 'restricted', 'not_found', 'unknown')
    ),
    content_fidelity TEXT NOT NULL CHECK(content_fidelity IN ('full', 'preview', 'na')),
    observed_version_id INTEGER REFERENCES post_versions(id),
    ingest_mode TEXT NOT NULL CHECK(ingest_mode IN ('live', 'backfill')),
    adapter_version TEXT NOT NULL,
    notes TEXT
);

CREATE TABLE IF NOT EXISTS post_observations (
    id INTEGER PRIMARY KEY,
    fetch_run_id INTEGER NOT NULL REFERENCES fetch_runs(id),
    post_id INTEGER NOT NULL REFERENCES posts(id),
    observed_at TEXT NOT NULL,
    present INTEGER NOT NULL CHECK(present IN (0, 1)),
    content_hash TEXT,
    content_fidelity TEXT NOT NULL CHECK(content_fidelity IN ('full', 'preview', 'na')),
    version_id INTEGER REFERENCES post_versions(id),
    UNIQUE(fetch_run_id, post_id),
    CHECK(
        (content_fidelity = 'full' AND present = 1 AND content_hash IS NOT NULL
            AND version_id IS NOT NULL)
        OR
        (content_fidelity IN ('preview', 'na') AND content_hash IS NULL
            AND version_id IS NULL)
    )
);

CREATE TABLE IF NOT EXISTS post_events (
    id INTEGER PRIMARY KEY,
    post_id INTEGER NOT NULL REFERENCES posts(id),
    dimension TEXT NOT NULL CHECK(
        dimension IN ('feed_state', 'source_state', 'watch_mode', 'content')
    ),
    from_value TEXT,
    to_value TEXT NOT NULL,
    detected_at TEXT NOT NULL,
    evidence_fetch_run_id INTEGER REFERENCES fetch_runs(id),
    evidence_probe_run_id INTEGER REFERENCES probe_runs(id),
    from_version_id INTEGER REFERENCES post_versions(id),
    to_version_id INTEGER REFERENCES post_versions(id),
    notes TEXT,
    CHECK(NOT (evidence_fetch_run_id IS NOT NULL AND evidence_probe_run_id IS NOT NULL))
);

CREATE TABLE IF NOT EXISTS recheck_queue (
    id INTEGER PRIMARY KEY,
    post_id INTEGER NOT NULL REFERENCES posts(id),
    reason TEXT NOT NULL CHECK(reason IN ('llm_candidate', 'recent_feed_absent')),
    enqueued_at TEXT NOT NULL,
    expires_at TEXT NOT NULL,
    state TEXT NOT NULL CHECK(state IN ('pending', 'confirmed', 'expired'))
);

CREATE UNIQUE INDEX IF NOT EXISTS uq_recheck_queue_pending_post
ON recheck_queue(post_id) WHERE state = 'pending';

CREATE TABLE IF NOT EXISTS attention_log (
    id INTEGER PRIMARY KEY,
    author_id INTEGER NOT NULL REFERENCES authors(id),
    post_id INTEGER REFERENCES posts(id),
    version_id INTEGER REFERENCES post_versions(id),
    triggered_at TEXT NOT NULL,
    my_reason TEXT NOT NULL,
    my_expectation TEXT,
    reviewed_at TEXT,
    my_retro TEXT
);

CREATE TABLE IF NOT EXISTS rewrite_exercises (
    id INTEGER PRIMARY KEY,
    post_id INTEGER NOT NULL REFERENCES posts(id),
    version_id INTEGER NOT NULL REFERENCES post_versions(id),
    original_text TEXT NOT NULL,
    llm_rewritten_claim TEXT NOT NULL,
    llm_rationale TEXT NOT NULL,
    model TEXT NOT NULL,
    prompt_version TEXT NOT NULL,
    my_verdict TEXT CHECK(my_verdict IN ('valid', 'too_vague', 'wrong')),
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS enrichments (
    id INTEGER PRIMARY KEY,
    post_id INTEGER NOT NULL REFERENCES posts(id),
    version_id INTEGER NOT NULL REFERENCES post_versions(id),
    post_type TEXT NOT NULL,
    label_first_hand_info INTEGER NOT NULL CHECK(label_first_hand_info IN (0, 1)),
    label_transferable_framework INTEGER NOT NULL CHECK(label_transferable_framework IN (0, 1)),
    label_reasoned_non_consensus INTEGER NOT NULL CHECK(label_reasoned_non_consensus IN (0, 1)),
    is_market_related INTEGER NOT NULL CHECK(is_market_related IN (0, 1)),
    rationale TEXT NOT NULL,
    evidence_snippet TEXT NOT NULL,
    stance_summary TEXT NOT NULL DEFAULT '',
    model TEXT NOT NULL,
    prompt_version TEXT NOT NULL,
    created_at TEXT NOT NULL,
    UNIQUE(version_id, prompt_version)
);

CREATE TABLE IF NOT EXISTS claims (
    id INTEGER PRIMARY KEY,
    post_id INTEGER NOT NULL REFERENCES posts(id),
    version_id INTEGER NOT NULL REFERENCES post_versions(id),
    author_id INTEGER NOT NULL REFERENCES authors(id),
    ticker TEXT NOT NULL,
    direction TEXT NOT NULL CHECK(direction IN ('long', 'short', 'neutral')),
    horizon_days INTEGER,
    target_price REAL,
    confidence_phrasing TEXT,
    claim_made_at TEXT NOT NULL,
    ingest_mode TEXT NOT NULL CHECK(ingest_mode IN ('live', 'backfill')),
    status TEXT NOT NULL CHECK(status IN ('open', 'expired', 'resolved')),
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS claim_proposals (
    id INTEGER PRIMARY KEY,
    version_id INTEGER NOT NULL REFERENCES post_versions(id),
    ticker TEXT NOT NULL,
    direction TEXT NOT NULL CHECK(direction IN ('long', 'short', 'neutral')),
    horizon_days INTEGER CHECK(horizon_days IS NULL OR horizon_days > 0),
    target_price REAL CHECK(target_price IS NULL OR target_price > 0),
    confidence_phrasing TEXT,
    evidence_snippet TEXT NOT NULL CHECK(length(trim(evidence_snippet)) > 0),
    model TEXT NOT NULL,
    prompt_version TEXT NOT NULL,
    created_at TEXT NOT NULL,
    review_state TEXT NOT NULL DEFAULT 'pending'
        CHECK(review_state IN ('pending', 'accepted', 'rejected')),
    reviewed_at TEXT,
    claim_id INTEGER REFERENCES claims(id),
    UNIQUE(version_id, ticker, prompt_version),
    CHECK(
        (review_state = 'pending' AND reviewed_at IS NULL AND claim_id IS NULL)
        OR (review_state = 'accepted' AND reviewed_at IS NOT NULL AND claim_id IS NOT NULL)
        OR (review_state = 'rejected' AND reviewed_at IS NOT NULL AND claim_id IS NULL)
    )
);

CREATE TABLE IF NOT EXISTS claim_proposal_scans (
    id INTEGER PRIMARY KEY,
    version_id INTEGER NOT NULL REFERENCES post_versions(id),
    model TEXT NOT NULL,
    prompt_version TEXT NOT NULL,
    proposal_count INTEGER NOT NULL CHECK(proposal_count >= 0),
    created_at TEXT NOT NULL,
    UNIQUE(version_id, prompt_version)
);

CREATE TABLE IF NOT EXISTS claim_outcomes (
    claim_id INTEGER PRIMARY KEY REFERENCES claims(id),
    resolved_at TEXT NOT NULL,
    raw_return REAL,
    benchmark_return REAL,
    excess_return REAL,
    benchmark_ticker TEXT NOT NULL DEFAULT 'UNKNOWN',
    outcome_method_version TEXT NOT NULL DEFAULT 'legacy-unknown',
    notes TEXT
);

CREATE TABLE IF NOT EXISTS my_decisions (
    id INTEGER PRIMARY KEY,
    ticker TEXT NOT NULL,
    direction TEXT NOT NULL CHECK(direction IN ('long', 'short', 'neutral')),
    thesis_text TEXT NOT NULL CHECK(length(trim(thesis_text)) > 0),
    invalidation_condition TEXT NOT NULL CHECK(length(trim(invalidation_condition)) > 0),
    horizon_days INTEGER CHECK(horizon_days IS NULL OR horizon_days > 0),
    position_note TEXT,
    decided_at TEXT NOT NULL,
    source_post_id INTEGER REFERENCES posts(id),
    source_version_id INTEGER REFERENCES post_versions(id),
    status TEXT NOT NULL DEFAULT 'open'
        CHECK(status IN ('open', 'invalidated', 'expired', 'closed')),
    closed_at TEXT,
    notes TEXT,
    CHECK(
        (source_version_id IS NULL)
        OR (source_post_id IS NOT NULL)
    )
);

CREATE TABLE IF NOT EXISTS my_decision_outcomes (
    id INTEGER PRIMARY KEY,
    decision_id INTEGER NOT NULL REFERENCES my_decisions(id),
    resolved_at TEXT NOT NULL,
    raw_return REAL NOT NULL,
    benchmark_return REAL NOT NULL,
    excess_return REAL NOT NULL,
    benchmark_ticker TEXT NOT NULL DEFAULT 'UNKNOWN',
    outcome_method_version TEXT NOT NULL,
    notes TEXT,
    UNIQUE(decision_id, benchmark_ticker, outcome_method_version)
);

CREATE TABLE IF NOT EXISTS my_decision_reviews (
    id INTEGER PRIMARY KEY,
    decision_id INTEGER NOT NULL REFERENCES my_decisions(id),
    reviewed_at TEXT NOT NULL,
    retro_text TEXT NOT NULL CHECK(length(trim(retro_text)) > 0),
    lesson TEXT
);

CREATE TABLE IF NOT EXISTS prices (
    ticker TEXT NOT NULL,
    date TEXT NOT NULL,
    close REAL NOT NULL,
    open REAL,
    high REAL,
    low REAL,
    volume REAL,
    PRIMARY KEY(ticker, date)
);

CREATE TABLE IF NOT EXISTS ticker_names (
    ticker TEXT PRIMARY KEY,
    name TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS watchlist (
    ticker TEXT PRIMARY KEY,
    name TEXT,
    added_at TEXT NOT NULL,
    note TEXT
);

CREATE TABLE IF NOT EXISTS watchlist_alerts (
    id INTEGER PRIMARY KEY,
    version_id INTEGER NOT NULL REFERENCES post_versions(id),
    ticker TEXT NOT NULL,
    detected_at TEXT NOT NULL,
    sent_at TEXT,
    UNIQUE(version_id, ticker)
);

CREATE TABLE IF NOT EXISTS version_tickers (
    version_id INTEGER NOT NULL REFERENCES post_versions(id),
    ticker TEXT NOT NULL,
    PRIMARY KEY(version_id, ticker)
);

CREATE TABLE IF NOT EXISTS version_ticker_scans (
    version_id INTEGER PRIMARY KEY REFERENCES post_versions(id)
);

CREATE TABLE IF NOT EXISTS crowding_events (
    id INTEGER PRIMARY KEY,
    ticker TEXT NOT NULL,
    direction TEXT NOT NULL CHECK(direction IN ('long', 'short', 'neutral')),
    window_start TEXT NOT NULL,
    window_end TEXT NOT NULL,
    detected_at TEXT NOT NULL,
    author_count INTEGER NOT NULL CHECK(author_count >= 1),
    method_version TEXT NOT NULL,
    UNIQUE(ticker, direction, window_start, window_end, method_version)
);

CREATE TABLE IF NOT EXISTS crowding_event_members (
    event_id INTEGER NOT NULL REFERENCES crowding_events(id),
    claim_id INTEGER NOT NULL REFERENCES claims(id),
    author_id INTEGER NOT NULL REFERENCES authors(id),
    post_id INTEGER NOT NULL REFERENCES posts(id),
    version_id INTEGER NOT NULL REFERENCES post_versions(id),
    PRIMARY KEY(event_id, claim_id)
);

-- Derived framework library (enrich-v3, phase 10): the structured shape of an
-- analysis framework the author stated in one observed version. Inference, not
-- evidence — it never writes back into version text, and every row links to its
-- source version_id so the framework stays usable (with provenance) even after
-- the original post is gone. Idempotency mirrors enrichments via
-- UNIQUE(version_id, prompt_version).
CREATE TABLE IF NOT EXISTS framework_extractions (
    id INTEGER PRIMARY KEY,
    post_id INTEGER NOT NULL REFERENCES posts(id),
    version_id INTEGER NOT NULL REFERENCES post_versions(id),
    topic TEXT NOT NULL CHECK(length(trim(topic)) > 0),
    summary TEXT NOT NULL CHECK(length(trim(summary)) > 0),
    input_variables TEXT NOT NULL CHECK(json_valid(input_variables)),
    logic_chain TEXT NOT NULL CHECK(length(trim(logic_chain)) > 0),
    conclusion_shape TEXT NOT NULL CHECK(length(trim(conclusion_shape)) > 0),
    applicability_conditions TEXT NOT NULL DEFAULT '',
    invalidation_conditions TEXT NOT NULL DEFAULT '',
    evidence_snippet TEXT NOT NULL CHECK(length(trim(evidence_snippet)) > 0),
    model TEXT NOT NULL,
    prompt_version TEXT NOT NULL,
    created_at TEXT NOT NULL,
    UNIQUE(version_id, prompt_version)
);

-- One row per (version, prompt_version) scanned for a framework, including
-- scans that found none — without it a "no framework stated" verdict would be
-- retried forever. Mirrors claim_proposal_scans.
CREATE TABLE IF NOT EXISTS framework_extraction_scans (
    id INTEGER PRIMARY KEY,
    version_id INTEGER NOT NULL REFERENCES post_versions(id),
    model TEXT NOT NULL,
    prompt_version TEXT NOT NULL,
    extraction_count INTEGER NOT NULL CHECK(extraction_count IN (0, 1)),
    created_at TEXT NOT NULL,
    UNIQUE(version_id, prompt_version)
);

-- Append-only record of a synthesized topic-recall brief (主题回溯 简报合成).
-- A brief is the only token-spending, prose-generating step of recall, so every
-- run is archived immutably with full provenance: the confirmed keyword groups and
-- window that produced it, the coverage/selection denominators *as they stood at
-- synthesis time*, and the exact version_ids cited. That lets a later reader audit
-- a brief against the evidence it actually rested on — even after the corpus grows
-- or source posts disappear. The retrieval base (recall.py) writes nothing here;
-- only an explicit synthesis action appends a row.
CREATE TABLE IF NOT EXISTS topic_briefs (
    id INTEGER PRIMARY KEY,
    question TEXT NOT NULL CHECK(length(trim(question)) > 0),
    groups TEXT NOT NULL CHECK(json_valid(groups)),
    tickers TEXT NOT NULL DEFAULT '[]' CHECK(json_valid(tickers)),
    authors TEXT NOT NULL DEFAULT '[]' CHECK(json_valid(authors)),
    date_from TEXT NOT NULL,
    date_to TEXT NOT NULL,
    require_all_groups INTEGER NOT NULL CHECK(require_all_groups IN (0, 1)),
    coverage TEXT NOT NULL CHECK(json_valid(coverage)),
    selection TEXT NOT NULL CHECK(json_valid(selection)),
    cited_version_ids TEXT NOT NULL DEFAULT '[]' CHECK(json_valid(cited_version_ids)),
    brief_text TEXT NOT NULL,
    model TEXT NOT NULL,
    prompt_version TEXT NOT NULL,
    created_at TEXT NOT NULL
);

-- Append-only evidence: one row per image-fetch attempt for a version. A
-- re-download of the same normalized_url appends a new row (never updates), so a
-- byte swap behind an unchanged URL stays visible as a second row with a
-- different sha256. download_status records 'ok' or 'failed' so a failure is
-- archived rather than silently retried-into-nothing.
CREATE TABLE IF NOT EXISTS post_images (
    id INTEGER PRIMARY KEY,
    version_id INTEGER NOT NULL REFERENCES post_versions(id),
    source_url TEXT NOT NULL,
    normalized_url TEXT NOT NULL,
    ordinal INTEGER NOT NULL CHECK(ordinal >= 0),
    sha256 TEXT,
    mime_type TEXT,
    byte_size INTEGER CHECK(byte_size IS NULL OR byte_size >= 0),
    image_bytes BLOB,
    downloaded_at TEXT NOT NULL,
    download_status TEXT NOT NULL CHECK(download_status IN ('ok', 'failed')),
    notes TEXT,
    CHECK(
        (download_status = 'ok' AND sha256 IS NOT NULL AND image_bytes IS NOT NULL)
        OR (download_status = 'failed' AND image_bytes IS NULL)
    )
);

-- Derived, searchable text extracted from a stored image (not evidence: it is a
-- machine transcription that may contain recognition errors). Keyed idempotently
-- per (image, engine, engine_version) so re-runs and engine upgrades coexist.
CREATE TABLE IF NOT EXISTS image_ocr (
    id INTEGER PRIMARY KEY,
    image_id INTEGER NOT NULL REFERENCES post_images(id),
    image_sha256 TEXT NOT NULL,
    engine TEXT NOT NULL,
    engine_version TEXT NOT NULL,
    ocr_text TEXT NOT NULL,
    created_at TEXT NOT NULL,
    UNIQUE(image_id, engine, engine_version)
);

-- Inference, not evidence: a vision model's description of an image, used only to
-- enrich/filter attention. Mirrors enrichments' idempotency; the bytes sent are
-- the stored BLOB (not the remote URL), so a failed/replaced source cannot change
-- what was judged. Keyed per (image, model, prompt_version).
CREATE TABLE IF NOT EXISTS image_enrichments (
    id INTEGER PRIMARY KEY,
    image_id INTEGER NOT NULL REFERENCES post_images(id),
    image_sha256 TEXT NOT NULL,
    model TEXT NOT NULL,
    prompt_version TEXT NOT NULL,
    prompt TEXT NOT NULL,
    description TEXT NOT NULL,
    created_at TEXT NOT NULL,
    UNIQUE(image_id, model, prompt_version)
);

CREATE VIEW IF NOT EXISTS version_sightings AS
SELECT version_id, observed_at, 'feed' AS channel, fetch_run_id AS run_id
FROM post_observations
WHERE version_id IS NOT NULL
UNION ALL
SELECT observed_version_id AS version_id, observed_at, 'direct' AS channel, id AS run_id
FROM probe_runs
WHERE observed_version_id IS NOT NULL;
"""


def connect_database(path: str | Path) -> sqlite3.Connection:
    connection = sqlite3.connect(path, isolation_level=None)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA foreign_keys = ON")
    connection.execute("PRAGMA journal_mode = WAL")
    install_db_tracing(connection)
    return connection


def _ensure_column(
    connection: sqlite3.Connection, table: str, column: str, definition: str
) -> None:
    """Add a column to an existing table if a prior schema lacked it (idempotent).

    ``CREATE TABLE IF NOT EXISTS`` never alters an existing table, so archives
    created before a column was added need this back-fill. ADD COLUMN is DDL, so
    the append-only UPDATE triggers do not block it.
    """
    existing = {row["name"] for row in connection.execute(f"PRAGMA table_info({table})")}
    if column not in existing:
        connection.execute(f"ALTER TABLE {table} ADD COLUMN {definition}")


def _create_index_if_columns(
    connection: sqlite3.Connection,
    index_name: str,
    table: str,
    columns: tuple[str, ...],
) -> None:
    existing = {
        str(row["name"]) for row in connection.execute(f"PRAGMA table_info({table})").fetchall()
    }
    if all(column in existing for column in columns):
        connection.execute(
            f"CREATE INDEX IF NOT EXISTS {index_name} ON {table}({', '.join(columns)})"
        )


def _rebuild_legacy_decision_outcomes(connection: sqlite3.Connection) -> None:
    """Remove the temporary resolved-at unique constraint from early phase-5 DBs."""
    row = connection.execute(
        "SELECT sql FROM sqlite_master WHERE type = 'table' AND name = 'my_decision_outcomes'"
    ).fetchone()
    if row is None or row["sql"] is None:
        return
    normalized = re.sub(r"\s+", "", str(row["sql"]).lower())
    if "unique(decision_id,resolved_at,outcome_method_version)" not in normalized:
        return
    columns = {
        item["name"] for item in connection.execute("PRAGMA table_info(my_decision_outcomes)")
    }
    benchmark_expression = (
        "COALESCE(benchmark_ticker, 'UNKNOWN')" if "benchmark_ticker" in columns else "'UNKNOWN'"
    )
    connection.execute("BEGIN IMMEDIATE")
    try:
        connection.execute(
            """
            CREATE TABLE my_decision_outcomes_rebuilt (
                id INTEGER PRIMARY KEY,
                decision_id INTEGER NOT NULL REFERENCES my_decisions(id),
                resolved_at TEXT NOT NULL,
                raw_return REAL NOT NULL,
                benchmark_return REAL NOT NULL,
                excess_return REAL NOT NULL,
                benchmark_ticker TEXT NOT NULL DEFAULT 'UNKNOWN',
                outcome_method_version TEXT NOT NULL,
                notes TEXT
            )
            """
        )
        connection.execute(
            f"""
            INSERT INTO my_decision_outcomes_rebuilt(
                id, decision_id, resolved_at, raw_return, benchmark_return, excess_return,
                benchmark_ticker, outcome_method_version, notes
            )
            SELECT
                id, decision_id, resolved_at, raw_return, benchmark_return, excess_return,
                {benchmark_expression}, outcome_method_version, notes
            FROM my_decision_outcomes
            """
        )
        connection.execute("DROP TABLE my_decision_outcomes")
        connection.execute(
            "ALTER TABLE my_decision_outcomes_rebuilt RENAME TO my_decision_outcomes"
        )
    except BaseException:
        connection.execute("ROLLBACK")
        raise
    else:
        connection.execute("COMMIT")


def _backfill_market_relation(connection: sqlite3.Connection) -> None:
    rows = connection.execute(
        """
        SELECT e.id, v.content_text, v.raw_payload
        FROM enrichments e
        JOIN post_versions v ON v.id = e.version_id
        WHERE e.is_market_related IS NULL
        """
    ).fetchall()
    connection.executemany(
        "UPDATE enrichments SET is_market_related = ? WHERE id = ?",
        (
            (
                int(
                    has_explicit_market_relation(
                        str(row["content_text"]),
                        str(row["raw_payload"]) if row["raw_payload"] is not None else None,
                    )
                ),
                int(row["id"]),
            )
            for row in rows
        ),
    )


def _backfill_version_tickers(connection: sqlite3.Connection) -> None:
    rows = connection.execute(
        """
        SELECT v.id, v.content_text, v.raw_payload
        FROM post_versions v
        WHERE NOT EXISTS (
            SELECT 1 FROM version_ticker_scans scan WHERE scan.version_id = v.id
        )
        """
    ).fetchall()
    connection.executemany(
        "INSERT OR IGNORE INTO version_tickers(version_id, ticker) VALUES (?, ?)",
        (
            (int(row["id"]), ticker)
            for row in rows
            for ticker in sorted(
                extract_market_tickers(
                    str(row["content_text"]),
                    str(row["raw_payload"]) if row["raw_payload"] is not None else None,
                )
            )
        ),
    )
    connection.executemany(
        "INSERT INTO version_ticker_scans(version_id) VALUES (?)",
        ((int(row["id"]),) for row in rows),
    )


def initialize_database(connection: sqlite3.Connection) -> None:
    connection.executescript(SCHEMA)
    _rebuild_legacy_decision_outcomes(connection)
    _ensure_column(
        connection,
        "fetch_runs",
        "parse_failure_count",
        "parse_failure_count INTEGER NOT NULL DEFAULT 0 CHECK(parse_failure_count >= 0)",
    )
    _ensure_column(
        connection,
        "fetch_runs",
        "reached_timeline_end",
        "reached_timeline_end INTEGER NOT NULL DEFAULT 0 CHECK(reached_timeline_end IN (0, 1))",
    )
    # Image-manifest tracking, added after the schema shipped. Existing rows keep
    # NULL: version-change detection treats a NULL prior manifest as "not
    # comparable" (no spurious fork on the first post-upgrade poll); the next
    # positive projection populates posts.current_image_manifest_hash, after which
    # real manifest comparison begins. We never back-fill post_versions —
    # ALTER ADD COLUMN is DDL (allowed), but UPDATE on an append-only evidence
    # table is not.
    _ensure_column(
        connection,
        "post_versions",
        "image_manifest_hash",
        "image_manifest_hash TEXT",
    )
    _ensure_column(
        connection,
        "posts",
        "current_image_manifest_hash",
        "current_image_manifest_hash TEXT",
    )
    _ensure_column(
        connection,
        "enrichments",
        "is_market_related",
        "is_market_related INTEGER CHECK(is_market_related IN (0, 1))",
    )
    _ensure_column(
        connection,
        "enrichments",
        "stance_summary",
        "stance_summary TEXT NOT NULL DEFAULT ''",
    )
    # Daily OHLC, added so Xueqiu kline bars can back a candlestick view. CSV price
    # imports only carry close, so these stay NULL for those rows; the chart falls
    # back to a close line when open/high/low are absent.
    for column in ("open", "high", "low", "volume"):
        _ensure_column(connection, "prices", column, f"{column} REAL")
    _ensure_column(
        connection,
        "my_decision_outcomes",
        "benchmark_ticker",
        "benchmark_ticker TEXT NOT NULL DEFAULT 'UNKNOWN'",
    )
    _ensure_column(
        connection,
        "claim_outcomes",
        "benchmark_ticker",
        "benchmark_ticker TEXT NOT NULL DEFAULT 'UNKNOWN'",
    )
    _ensure_column(
        connection,
        "claim_outcomes",
        "outcome_method_version",
        "outcome_method_version TEXT NOT NULL DEFAULT 'legacy-unknown'",
    )
    # Author filter on recall, added after topic_briefs shipped. Pre-upgrade briefs
    # were synthesized with no author narrowing, so '[]' (all authors) is the honest
    # back-fill for their pinned query.
    _ensure_column(
        connection,
        "topic_briefs",
        "authors",
        "authors TEXT NOT NULL DEFAULT '[]' CHECK(json_valid(authors))",
    )
    _backfill_market_relation(connection)
    _backfill_version_tickers(connection)
    connection.executescript(
        """
        CREATE INDEX IF NOT EXISTS idx_enrichments_market_viewpoints
        ON enrichments(prompt_version, post_type, is_market_related, version_id);
        CREATE INDEX IF NOT EXISTS idx_claims_version_id ON claims(version_id);
        CREATE INDEX IF NOT EXISTS idx_claim_proposals_review
        ON claim_proposals(review_state, created_at, id);
        CREATE INDEX IF NOT EXISTS idx_claim_proposal_scans_version
        ON claim_proposal_scans(version_id, prompt_version);
        CREATE INDEX IF NOT EXISTS idx_claims_status_due
        ON claims(status, claim_made_at, horizon_days);
        CREATE INDEX IF NOT EXISTS idx_my_decisions_status_due
        ON my_decisions(status, decided_at, horizon_days);
        CREATE INDEX IF NOT EXISTS idx_my_decisions_ticker_decided
        ON my_decisions(ticker, decided_at DESC);
        CREATE INDEX IF NOT EXISTS idx_my_decision_outcomes_decision
        ON my_decision_outcomes(decision_id, resolved_at);
        CREATE UNIQUE INDEX IF NOT EXISTS idx_my_decision_outcomes_method
        ON my_decision_outcomes(decision_id, benchmark_ticker, outcome_method_version)
        WHERE benchmark_ticker != 'UNKNOWN';
        CREATE INDEX IF NOT EXISTS idx_my_decision_reviews_decision
        ON my_decision_reviews(decision_id, reviewed_at);
        CREATE INDEX IF NOT EXISTS idx_watchlist_alerts_pending
        ON watchlist_alerts(sent_at, detected_at, id);
        CREATE INDEX IF NOT EXISTS idx_version_tickers_ticker
        ON version_tickers(ticker, version_id);
        CREATE INDEX IF NOT EXISTS idx_posts_author
        ON posts(author_id, id);
        CREATE INDEX IF NOT EXISTS idx_post_events_post
        ON post_events(post_id, dimension, id);
        CREATE INDEX IF NOT EXISTS idx_crowding_events_recent
        ON crowding_events(window_end DESC, id DESC);
        CREATE INDEX IF NOT EXISTS idx_crowding_members_event
        ON crowding_event_members(event_id, author_id);
        CREATE INDEX IF NOT EXISTS idx_framework_extractions_topic
        ON framework_extractions(prompt_version, topic, id);
        CREATE INDEX IF NOT EXISTS idx_framework_scans_version
        ON framework_extraction_scans(version_id, prompt_version);
        CREATE INDEX IF NOT EXISTS idx_topic_briefs_recent
        ON topic_briefs(created_at DESC, id DESC);
        """
    )
    _create_index_if_columns(
        connection,
        "idx_post_versions_post",
        "post_versions",
        ("post_id", "id"),
    )
    for table in (*EVIDENCE_TABLES, *DERIVED_APPEND_ONLY_TABLES):
        connection.executescript(
            f"""
            CREATE TRIGGER IF NOT EXISTS protect_{table}_update
            BEFORE UPDATE ON {table}
            BEGIN
                SELECT RAISE(ABORT, '{table} is append-only');
            END;
            CREATE TRIGGER IF NOT EXISTS protect_{table}_delete
            BEFORE DELETE ON {table}
            BEGIN
                SELECT RAISE(ABORT, '{table} is append-only');
            END;
            """
        )
    connection.executescript(
        """
        CREATE TRIGGER IF NOT EXISTS protect_posts_delete
        BEFORE DELETE ON posts
        BEGIN
            SELECT RAISE(ABORT, 'posts cannot be deleted');
        END;

        CREATE TRIGGER IF NOT EXISTS protect_posts_identity
        BEFORE UPDATE ON posts
        WHEN OLD.id IS NOT NEW.id
          OR OLD.author_id IS NOT NEW.author_id
          OR OLD.platform IS NOT NEW.platform
          OR OLD.platform_post_id IS NOT NEW.platform_post_id
          OR OLD.first_seen_at IS NOT NEW.first_seen_at
        BEGIN
            SELECT RAISE(ABORT, 'posts identity fields cannot be updated');
        END;

        CREATE TRIGGER IF NOT EXISTS protect_my_decisions_delete
        BEFORE DELETE ON my_decisions
        BEGIN
            SELECT RAISE(ABORT, 'my_decisions cannot be deleted');
        END;

        DROP TRIGGER IF EXISTS protect_my_decisions_thesis;
        CREATE TRIGGER protect_my_decisions_thesis
        BEFORE UPDATE ON my_decisions
        WHEN OLD.id IS NOT NEW.id
          OR OLD.ticker IS NOT NEW.ticker
          OR OLD.direction IS NOT NEW.direction
          OR OLD.thesis_text IS NOT NEW.thesis_text
          OR OLD.invalidation_condition IS NOT NEW.invalidation_condition
          OR OLD.horizon_days IS NOT NEW.horizon_days
          OR OLD.decided_at IS NOT NEW.decided_at
          OR OLD.source_post_id IS NOT NEW.source_post_id
          OR OLD.source_version_id IS NOT NEW.source_version_id
        BEGIN
            SELECT RAISE(ABORT, 'my_decisions thesis fields cannot be updated');
        END;

        CREATE TRIGGER IF NOT EXISTS protect_my_decision_reviews_update
        BEFORE UPDATE ON my_decision_reviews
        BEGIN
            SELECT RAISE(ABORT, 'my_decision_reviews is append-only');
        END;

        CREATE TRIGGER IF NOT EXISTS protect_my_decision_reviews_delete
        BEFORE DELETE ON my_decision_reviews
        BEGIN
            SELECT RAISE(ABORT, 'my_decision_reviews is append-only');
        END;

        CREATE TRIGGER IF NOT EXISTS protect_my_decision_outcomes_update
        BEFORE UPDATE ON my_decision_outcomes
        BEGIN
            SELECT RAISE(ABORT, 'my_decision_outcomes is append-only');
        END;

        CREATE TRIGGER IF NOT EXISTS protect_my_decision_outcomes_delete
        BEFORE DELETE ON my_decision_outcomes
        BEGIN
            SELECT RAISE(ABORT, 'my_decision_outcomes is append-only');
        END;

        CREATE TRIGGER IF NOT EXISTS protect_claim_proposals_delete
        BEFORE DELETE ON claim_proposals
        BEGIN
            SELECT RAISE(ABORT, 'claim_proposals cannot be deleted');
        END;

        CREATE TRIGGER IF NOT EXISTS protect_claim_proposals_content
        BEFORE UPDATE ON claim_proposals
        WHEN OLD.id IS NOT NEW.id
          OR OLD.version_id IS NOT NEW.version_id
          OR OLD.ticker IS NOT NEW.ticker
          OR OLD.direction IS NOT NEW.direction
          OR OLD.horizon_days IS NOT NEW.horizon_days
          OR OLD.target_price IS NOT NEW.target_price
          OR OLD.confidence_phrasing IS NOT NEW.confidence_phrasing
          OR OLD.evidence_snippet IS NOT NEW.evidence_snippet
          OR OLD.model IS NOT NEW.model
          OR OLD.prompt_version IS NOT NEW.prompt_version
          OR OLD.created_at IS NOT NEW.created_at
        BEGIN
            SELECT RAISE(ABORT, 'claim_proposals content cannot be updated');
        END;

        CREATE TRIGGER IF NOT EXISTS protect_claim_proposal_scans_update
        BEFORE UPDATE ON claim_proposal_scans
        BEGIN
            SELECT RAISE(ABORT, 'claim_proposal_scans is append-only');
        END;

        CREATE TRIGGER IF NOT EXISTS protect_claim_proposal_scans_delete
        BEFORE DELETE ON claim_proposal_scans
        BEGIN
            SELECT RAISE(ABORT, 'claim_proposal_scans is append-only');
        END;

        CREATE TRIGGER IF NOT EXISTS protect_claim_outcomes_update
        BEFORE UPDATE ON claim_outcomes
        BEGIN
            SELECT RAISE(ABORT, 'claim_outcomes is append-only');
        END;

        CREATE TRIGGER IF NOT EXISTS protect_claim_outcomes_delete
        BEFORE DELETE ON claim_outcomes
        BEGIN
            SELECT RAISE(ABORT, 'claim_outcomes is append-only');
        END;
        """
    )
