"""Ticker extraction and descriptive market snapshots for enriched viewpoints."""

from __future__ import annotations

import json
import sqlite3
from datetime import datetime
from typing import cast

from kol_archive.market import OUTCOME_METHOD_VERSION, common_close_returns, local_market_date

from .common import _CN_TICKER, _MARKET_RELATED_VIEWPOINT_SQL, _TICKER_NAME


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
