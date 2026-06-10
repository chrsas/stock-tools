"""Daily OHLC bars from Xueqiu's kline endpoint, fetched through the WAF-fronting client.

Xueqiu's ``stock.xueqiu.com/v5/stock/chart/kline.json`` returns daily open/high/low/
close behind the same Aliyun WAF that blocks plain ``httpx`` (see ``browser.py``). We
reuse the collector's client — a ``BrowserClient`` that runs the request as a same-origin
fetch inside the WAF-cleared Edge tab — instead of adding an httpx-based wrapper that the
WAF would just bounce. Symbols match the archive's ``SH######`` ticker form unchanged.
"""

from __future__ import annotations

import json
import re
import sqlite3
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta, timezone
from typing import Any

import httpx

KLINE_URL = "https://stock.xueqiu.com/v5/stock/chart/kline.json"
_A_SHARE_TIMEZONE = timezone(timedelta(hours=8))
_TICKER = re.compile(r"(?:SH|SZ|BJ)\d{6}")
# Xueqiu rejects unbounded pulls; ~2 trading years comfortably covers any viewpoint window.
DEFAULT_BAR_COUNT = 500


class KlineError(RuntimeError):
    """A kline fetch produced no usable bars (transport, WAF, or malformed payload)."""


@dataclass(frozen=True)
class DailyBar:
    date: str
    open: float
    high: float
    low: float
    close: float
    volume: float | None


def validated_symbol(raw: object) -> str:
    symbol = str(raw or "").strip().upper()
    if not _TICKER.fullmatch(symbol):
        raise ValueError(f"invalid ticker symbol: {symbol!r}")
    return symbol


def _finite_positive(value: object) -> float | None:
    try:
        number = float(value)  # type: ignore[arg-type]
    except TypeError, ValueError:
        return None
    return number if number > 0 else None


def bars_from_payload(payload: dict[str, Any]) -> list[DailyBar]:
    """Parse Xueqiu's ``{data: {column, item}}`` table into ascending daily bars.

    Rows missing any OHLC field are skipped rather than raising: one bad bar must not
    discard a whole otherwise-usable history.
    """
    data = payload.get("data") or {}
    columns = data.get("column") or []
    items = data.get("item") or []
    index = {str(name): position for position, name in enumerate(columns)}
    required = ("timestamp", "open", "high", "low", "close")
    if not all(name in index for name in required):
        raise ValueError("kline payload missing OHLC columns")
    bars: list[DailyBar] = []
    for row in items:
        if not isinstance(row, list):
            continue
        try:
            timestamp = float(row[index["timestamp"]])
        except TypeError, ValueError, IndexError:
            continue
        opens = _finite_positive(row[index["open"]])
        high = _finite_positive(row[index["high"]])
        low = _finite_positive(row[index["low"]])
        close = _finite_positive(row[index["close"]])
        if None in (opens, high, low, close):
            continue
        volume = _finite_positive(row[index["volume"]]) if "volume" in index else None
        bar_date = datetime.fromtimestamp(timestamp / 1000, tz=_A_SHARE_TIMEZONE).date().isoformat()
        bars.append(
            DailyBar(
                date=bar_date,
                open=float(opens),  # type: ignore[arg-type]
                high=float(high),  # type: ignore[arg-type]
                low=float(low),  # type: ignore[arg-type]
                close=float(close),  # type: ignore[arg-type]
                volume=volume,
            )
        )
    bars.sort(key=lambda bar: bar.date)
    return bars


def fetch_daily_bars(
    client: Any,
    symbol: str,
    *,
    count: int = DEFAULT_BAR_COUNT,
    timeout: float | None = None,
) -> list[DailyBar]:
    """Pull the most recent ``count`` daily bars for ``symbol`` ending today."""
    symbol = validated_symbol(symbol)
    now_ms = int(datetime.now(tz=UTC).timestamp() * 1000)
    params = {
        "symbol": symbol,
        "begin": now_ms,
        "period": "day",
        "type": "before",
        "count": -abs(int(count)),
        "indicator": "kline",
    }
    try:
        response = client.get(KLINE_URL, params=params, timeout=timeout)
    except httpx.HTTPError as error:
        raise KlineError(f"kline request failed for {symbol}: {type(error).__name__}") from error
    if response.status_code != 200:
        raise KlineError(f"kline HTTP {response.status_code} for {symbol}")
    try:
        payload = json.loads(response.text)
    except json.JSONDecodeError as error:
        raise KlineError(f"kline payload was not JSON for {symbol} (WAF challenge?)") from error
    if str(payload.get("error_code") or "0") != "0":
        raise KlineError(f"kline error_code={payload.get('error_code')} for {symbol}")
    bars = bars_from_payload(payload)
    if not bars:
        raise KlineError(f"kline payload contained no usable bars for {symbol}")
    return bars


def store_daily_bars(connection: sqlite3.Connection, ticker: str, bars: list[DailyBar]) -> int:
    """Upsert OHLC bars into ``prices`` idempotently; returns rows written."""
    ticker = validated_symbol(ticker)
    if not bars:
        return 0
    with connection:
        connection.executemany(
            """
            INSERT INTO prices(ticker, date, close, open, high, low, volume)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(ticker, date) DO UPDATE SET
                close = excluded.close,
                open = excluded.open,
                high = excluded.high,
                low = excluded.low,
                volume = excluded.volume
            """,
            (
                (ticker, bar.date, bar.close, bar.open, bar.high, bar.low, bar.volume)
                for bar in bars
            ),
        )
    return len(bars)


def discover_tickers(connection: sqlite3.Connection) -> list[str]:
    """Every ticker the archive already tracks: market claims plus imported prices."""
    rows = connection.execute(
        """
        SELECT ticker FROM claims
        UNION
        SELECT ticker FROM prices
        """
    ).fetchall()
    seen: list[str] = []
    for row in rows:
        try:
            symbol = validated_symbol(row[0])
        except ValueError:
            continue
        if symbol not in seen:
            seen.append(symbol)
    return sorted(seen)


@dataclass(frozen=True)
class KlineFetchSummary:
    tickers: int
    bars: int
    failures: tuple[str, ...]


def fetch_and_store(
    connection: sqlite3.Connection,
    client: Any,
    tickers: list[str],
    *,
    count: int = DEFAULT_BAR_COUNT,
    timeout: float | None = None,
) -> KlineFetchSummary:
    """Fetch daily bars for each ticker and upsert them; collect per-ticker failures.

    A single ticker's failure (delisting, WAF hiccup) is recorded and skipped so the
    rest of the batch still lands, mirroring how image downloads tolerate dead links.
    """
    written = 0
    succeeded = 0
    failures: list[str] = []
    for ticker in tickers:
        try:
            bars = fetch_daily_bars(client, ticker, count=count, timeout=timeout)
        except (KlineError, ValueError) as error:
            failures.append(f"{ticker}: {error}")
            continue
        written += store_daily_bars(connection, ticker, bars)
        succeeded += 1
    return KlineFetchSummary(tickers=succeeded, bars=written, failures=tuple(failures))
