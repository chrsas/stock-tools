"""SQLite schema, indexes, views, and evidence-protection triggers."""

from __future__ import annotations

import sqlite3
from pathlib import Path

EVIDENCE_TABLES = (
    "fetch_runs",
    "probe_runs",
    "post_observations",
    "post_versions",
    "post_events",
)

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
    rationale TEXT NOT NULL,
    evidence_snippet TEXT NOT NULL,
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

CREATE TABLE IF NOT EXISTS claim_outcomes (
    claim_id INTEGER PRIMARY KEY REFERENCES claims(id),
    resolved_at TEXT NOT NULL,
    raw_return REAL,
    benchmark_return REAL,
    excess_return REAL,
    notes TEXT
);

CREATE TABLE IF NOT EXISTS prices (
    ticker TEXT NOT NULL,
    date TEXT NOT NULL,
    close REAL NOT NULL,
    PRIMARY KEY(ticker, date)
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


def initialize_database(connection: sqlite3.Connection) -> None:
    connection.executescript(SCHEMA)
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
    for table in EVIDENCE_TABLES:
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
        """
    )
