from __future__ import annotations

from typing import Any

import httpx
import pytest

from kol_archive.database import connect_database, initialize_database
from kol_archive.kline import (
    DailyBar,
    KlineError,
    bars_from_payload,
    discover_tickers,
    fetch_and_store,
    fetch_daily_bars,
    store_daily_bars,
    validated_symbol,
)

# Real Xueqiu kline column order, trimmed to the fields we read.
_COLUMNS = ["timestamp", "volume", "open", "high", "low", "close", "chg", "percent"]


def _payload(items: list[list[Any]], *, error_code: int = 0) -> dict[str, Any]:
    return {
        "error_code": error_code,
        "data": {"symbol": "SH688303", "column": _COLUMNS, "item": items},
    }


def _bar_row(timestamp_ms: int, opens: float, high: float, low: float, close: float) -> list[Any]:
    return [timestamp_ms, 1000, opens, high, low, close, 0.1, 1.0]


class _FakeClient:
    """Stands in for BrowserClient/httpx: returns a queued response per get()."""

    def __init__(self, responses: dict[str, httpx.Response]) -> None:
        self._responses = responses
        self.calls: list[dict[str, Any]] = []

    def get(
        self, url: str, *, params: dict[str, Any] | None = None, timeout: float | None = None
    ) -> httpx.Response:
        symbol = str((params or {}).get("symbol"))
        self.calls.append({"url": url, "params": params})
        return self._responses[symbol]


def test_bars_from_payload_parses_ohlc_and_beijing_date() -> None:
    # Xueqiu daily timestamps are Beijing midnight of the trading day, in ms.
    bars = bars_from_payload(_payload([_bar_row(1780502400000, 50.1, 52.3, 49.8, 51.2)]))

    assert bars == [
        DailyBar(date="2026-06-04", open=50.1, high=52.3, low=49.8, close=51.2, volume=1000.0)
    ]


def test_bars_from_payload_skips_incomplete_rows_and_sorts() -> None:
    payload = _payload(
        [
            _bar_row(1780588800000, 12.0, 13.0, 11.0, 12.5),  # 2026-06-05
            [1780416000000, 1000, None, 13.0, 11.0, 12.0, 0, 0],  # 06-03, missing open -> skip
            _bar_row(1780502400000, 10.0, 11.0, 9.0, 10.5),  # 2026-06-04
        ]
    )

    dates = [bar.date for bar in bars_from_payload(payload)]

    assert dates == ["2026-06-04", "2026-06-05"]


def test_bars_from_payload_rejects_missing_columns() -> None:
    with pytest.raises(ValueError):
        bars_from_payload({"data": {"column": ["timestamp", "close"], "item": []}})


def test_fetch_daily_bars_raises_on_non_json_waf_body() -> None:
    client = _FakeClient({"SH688303": httpx.Response(200, text="<html>滑块验证</html>")})

    with pytest.raises(KlineError):
        fetch_daily_bars(client, "SH688303")


def test_fetch_daily_bars_raises_on_error_code() -> None:
    client = _FakeClient({"SH688303": httpx.Response(200, json=_payload([], error_code=1))})

    with pytest.raises(KlineError):
        fetch_daily_bars(client, "SH688303")


def test_fetch_daily_bars_raises_when_payload_has_no_usable_bars() -> None:
    client = _FakeClient({"SH688303": httpx.Response(200, json=_payload([]))})

    with pytest.raises(KlineError, match="no usable bars"):
        fetch_daily_bars(client, "SH688303")


def test_store_daily_bars_upserts_idempotently() -> None:
    connection = connect_database(":memory:")
    initialize_database(connection)
    bars = [DailyBar("2026-06-04", 10.0, 11.0, 9.0, 10.5, 1000.0)]

    store_daily_bars(connection, "SH688303", bars)
    revised = [DailyBar("2026-06-04", 10.0, 12.0, 9.0, 11.0, 2000.0)]
    store_daily_bars(connection, "SH688303", revised)

    row = connection.execute(
        "SELECT close, open, high, low, volume FROM prices WHERE ticker = 'SH688303'"
    ).fetchall()
    assert len(row) == 1
    assert tuple(row[0]) == (11.0, 10.0, 12.0, 9.0, 2000.0)


def test_discover_tickers_unions_claims_and_prices() -> None:
    connection = connect_database(":memory:")
    initialize_database(connection)
    store_daily_bars(connection, "SH688303", [DailyBar("2026-06-04", 1, 1, 1, 1, None)])
    connection.execute(
        "INSERT INTO prices(ticker, date, close) VALUES ('SH000300', '2026-06-04', 100)"
    )

    assert discover_tickers(connection) == ["SH000300", "SH688303"]


def test_fetch_and_store_records_per_ticker_failures() -> None:
    connection = connect_database(":memory:")
    initialize_database(connection)
    ok = _payload([_bar_row(1780761600000, 10, 11, 9, 10.5)])
    client = _FakeClient(
        {
            "SH688303": httpx.Response(200, json=ok),
            "SH000300": httpx.Response(500, text="boom"),
        }
    )

    summary = fetch_and_store(connection, client, ["SH688303", "SH000300"])

    assert summary.tickers == 1
    assert summary.bars == 1
    assert len(summary.failures) == 1
    assert "SH000300" in summary.failures[0]


def test_validated_symbol_rejects_garbage() -> None:
    assert validated_symbol("sh688303") == "SH688303"
    with pytest.raises(ValueError):
        validated_symbol("AAPL")
