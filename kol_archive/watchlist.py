"""Watchlist management and post-collection intersection alerts."""

from __future__ import annotations

import json
import re
import sqlite3
from dataclasses import dataclass
from urllib.parse import quote

from kol_archive.time import parse_utc_timestamp

_A_SHARE_TICKER = re.compile(r"(?<![A-Z0-9])(?:SH|SZ|BJ)\d{6}(?![A-Z0-9])")


@dataclass(frozen=True)
class WatchlistMatch:
    alert_id: int
    version_id: int
    post_id: int
    ticker: str
    author_name: str
    has_resolved_claim: bool


def validated_watchlist_ticker(value: str) -> str:
    ticker = value.strip().upper()
    if not _A_SHARE_TICKER.fullmatch(ticker):
        raise ValueError("watchlist ticker must be an A-share ticker")
    return ticker


def extract_market_tickers(content_text: str, raw_payload: str | None) -> set[str]:
    tickers = set(_A_SHARE_TICKER.findall(content_text))
    if not raw_payload:
        return tickers
    try:
        payload = json.loads(raw_payload)
    except json.JSONDecodeError:
        return tickers

    def visit(value: object) -> None:
        if isinstance(value, dict):
            correlation = value.get("stockCorrelation")
            if isinstance(correlation, list):
                for item in correlation:
                    if _A_SHARE_TICKER.fullmatch(str(item)):
                        tickers.add(str(item))
                    elif isinstance(item, dict):
                        for key in ("symbol", "ticker", "code"):
                            candidate = str(item.get(key) or "")
                            if _A_SHARE_TICKER.fullmatch(candidate):
                                tickers.add(candidate)
            for child in value.values():
                visit(child)
        elif isinstance(value, list):
            for child in value:
                visit(child)

    visit(payload)
    return tickers


def add_watchlist_ticker(
    connection: sqlite3.Connection,
    ticker: str,
    added_at: str,
    *,
    name: str | None = None,
    note: str | None = None,
) -> None:
    ticker = validated_watchlist_ticker(ticker)
    added_at = parse_utc_timestamp(added_at).isoformat()
    connection.execute(
        """
        INSERT INTO watchlist(ticker, name, added_at, note)
        VALUES (?, ?, ?, ?)
        ON CONFLICT(ticker) DO UPDATE SET
            name = COALESCE(excluded.name, watchlist.name),
            note = COALESCE(excluded.note, watchlist.note)
        """,
        (
            ticker,
            None if name is None else name.strip() or None,
            added_at,
            None if note is None else note.strip() or None,
        ),
    )


def remove_watchlist_ticker(connection: sqlite3.Connection, ticker: str) -> bool:
    cursor = connection.execute(
        "DELETE FROM watchlist WHERE ticker = ?", (validated_watchlist_ticker(ticker),)
    )
    return cursor.rowcount == 1


def list_watchlist(connection: sqlite3.Connection) -> list[dict[str, object]]:
    return [
        dict(row)
        for row in connection.execute(
            """
            SELECT
                w.ticker,
                COALESCE(w.name, tn.name) AS name,
                w.added_at,
                w.note,
                (
                    SELECT COUNT(*) FROM watchlist_alerts a
                    WHERE a.ticker = w.ticker AND a.sent_at IS NOT NULL
                ) AS alert_count
            FROM watchlist w
            LEFT JOIN ticker_names tn ON tn.ticker = w.ticker
            ORDER BY w.added_at DESC, w.ticker
            """
        ).fetchall()
    ]


def stage_watchlist_alerts(
    connection: sqlite3.Connection, observed_since: str, detected_at: str
) -> int:
    observed_since = parse_utc_timestamp(observed_since).isoformat()
    detected_at = parse_utc_timestamp(detected_at).isoformat()
    watched = {
        str(row["ticker"]) for row in connection.execute("SELECT ticker FROM watchlist").fetchall()
    }
    if not watched:
        return 0
    rows = connection.execute(
        """
        SELECT id AS version_id, content_text, raw_payload
        FROM post_versions
        WHERE ingest_mode = 'live' AND first_observed_at >= ?
        ORDER BY id
        """,
        (observed_since,),
    ).fetchall()
    staged = 0
    connection.execute("BEGIN IMMEDIATE")
    try:
        for row in rows:
            for ticker in sorted(
                extract_market_tickers(
                    str(row["content_text"]),
                    None if row["raw_payload"] is None else str(row["raw_payload"]),
                )
                & watched
            ):
                cursor = connection.execute(
                    """
                    INSERT OR IGNORE INTO watchlist_alerts(version_id, ticker, detected_at)
                    VALUES (?, ?, ?)
                    """,
                    (row["version_id"], ticker, detected_at),
                )
                staged += cursor.rowcount
    except BaseException:
        connection.execute("ROLLBACK")
        raise
    else:
        connection.execute("COMMIT")
    return staged


def pending_watchlist_alerts(connection: sqlite3.Connection) -> list[WatchlistMatch]:
    return [
        WatchlistMatch(
            alert_id=int(row["alert_id"]),
            version_id=int(row["version_id"]),
            post_id=int(row["post_id"]),
            ticker=str(row["ticker"]),
            author_name=str(row["author_name"]),
            has_resolved_claim=bool(row["has_resolved_claim"]),
        )
        for row in connection.execute(
            """
            SELECT
                wa.id AS alert_id,
                wa.version_id,
                wa.ticker,
                p.id AS post_id,
                COALESCE(json_extract(v.raw_payload, '$.user.screen_name'), a.platform_uid)
                    AS author_name,
                EXISTS(
                    SELECT 1 FROM claims c
                    JOIN claim_outcomes o ON o.claim_id = c.id
                    WHERE c.author_id = p.author_id AND c.ticker = wa.ticker
                ) AS has_resolved_claim
            FROM watchlist_alerts wa
            JOIN post_versions v ON v.id = wa.version_id
            JOIN posts p ON p.id = v.post_id
            JOIN authors a ON a.id = p.author_id
            WHERE wa.sent_at IS NULL
            ORDER BY wa.detected_at, wa.id
            """
        ).fetchall()
    ]


def watchlist_match_title(match: WatchlistMatch) -> str:
    settled = " · 该作者此标的有已结算记录" if match.has_resolved_claim else ""
    return f"关注标的命中：{match.author_name} · {match.ticker}{settled}"


def watchlist_match_link(private_base_url: str, match: WatchlistMatch) -> str:
    return f"{private_base_url.rstrip('/')}/posts/{quote(str(match.post_id))}"


def mark_watchlist_alert_sent(connection: sqlite3.Connection, alert_id: int, sent_at: str) -> None:
    sent_at = parse_utc_timestamp(sent_at).isoformat()
    cursor = connection.execute(
        "UPDATE watchlist_alerts SET sent_at = ? WHERE id = ? AND sent_at IS NULL",
        (sent_at, alert_id),
    )
    if cursor.rowcount != 1:
        raise ValueError(f"unknown or already sent watchlist alert id: {alert_id}")
