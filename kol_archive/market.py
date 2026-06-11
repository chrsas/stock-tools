"""Deterministic market-relation checks derived from archived post evidence."""

from __future__ import annotations

import json
import re
import sqlite3
from datetime import date, timedelta, timezone

from kol_archive.time import parse_utc_timestamp

_A_SHARE_TICKER = re.compile(r"(?:SH|SZ|BJ)\d{6}")
A_SHARE_TIMEZONE = timezone(timedelta(hours=8))
OUTCOME_METHOD_VERSION = "descriptive-common-close-v1"


def has_explicit_market_relation(content_text: str, raw_payload: str | None) -> bool:
    """Return whether archived text or stockCorrelation names an A-share ticker."""
    if _A_SHARE_TICKER.search(content_text):
        return True
    if not raw_payload:
        return False
    try:
        payload = json.loads(raw_payload)
    except json.JSONDecodeError:
        return False

    def visit(value: object) -> bool:
        if isinstance(value, dict):
            correlation = value.get("stockCorrelation")
            if isinstance(correlation, list):
                for item in correlation:
                    if _A_SHARE_TICKER.fullmatch(str(item)):
                        return True
                    if isinstance(item, dict) and any(
                        _A_SHARE_TICKER.fullmatch(str(item.get(key) or ""))
                        for key in ("symbol", "ticker", "code")
                    ):
                        return True
            return any(visit(child) for child in value.values())
        if isinstance(value, list):
            return any(visit(child) for child in value)
        return False

    return visit(payload)


def local_market_date(timestamp: str) -> date:
    return parse_utc_timestamp(timestamp).astimezone(A_SHARE_TIMEZONE).date()


def common_close_returns(
    connection: sqlite3.Connection,
    ticker: str,
    benchmark_ticker: str,
    observed_at: str,
    end_date: str | None = None,
) -> dict[str, object] | None:
    """Calculate common-close changes from before an observed local market date."""
    observed_date = local_market_date(observed_at).isoformat()
    start = connection.execute(
        """
        SELECT asset.date, asset.close AS asset_close, benchmark.close AS benchmark_close
        FROM prices asset
        JOIN prices benchmark ON benchmark.date = asset.date AND benchmark.ticker = ?
        WHERE asset.ticker = ? AND asset.date < ?
        ORDER BY asset.date DESC
        LIMIT 1
        """,
        (benchmark_ticker, ticker, observed_date),
    ).fetchone()
    if start is None:
        return None
    if end_date is None:
        end = connection.execute(
            """
            SELECT asset.date, asset.close AS asset_close, benchmark.close AS benchmark_close
            FROM prices asset
            JOIN prices benchmark ON benchmark.date = asset.date AND benchmark.ticker = ?
            WHERE asset.ticker = ? AND asset.date >= ?
            ORDER BY asset.date DESC
            LIMIT 1
            """,
            (benchmark_ticker, ticker, observed_date),
        ).fetchone()
    else:
        date.fromisoformat(end_date)
        end = connection.execute(
            """
            SELECT asset.date, asset.close AS asset_close, benchmark.close AS benchmark_close
            FROM prices asset
            JOIN prices benchmark ON benchmark.date = asset.date AND benchmark.ticker = ?
            WHERE asset.ticker = ? AND asset.date >= ?
            ORDER BY asset.date
            LIMIT 1
            """,
            (benchmark_ticker, ticker, end_date),
        ).fetchone()
    if end is None:
        return None
    asset_start = float(start["asset_close"])
    benchmark_start = float(start["benchmark_close"])
    if asset_start == 0 or benchmark_start == 0:
        return None
    raw_return = float(end["asset_close"]) / asset_start - 1
    benchmark_return = float(end["benchmark_close"]) / benchmark_start - 1
    return {
        "ticker": ticker,
        "benchmark_ticker": benchmark_ticker,
        "start_date": str(start["date"]),
        "end_date": str(end["date"]),
        "raw_return": raw_return,
        "benchmark_return": benchmark_return,
        "excess_return": raw_return - benchmark_return,
        "method_version": OUTCOME_METHOD_VERSION,
    }
