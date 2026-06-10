"""Validated CSV imports for local descriptive market data."""

from __future__ import annotations

import csv
import math
import re
import sqlite3
from dataclasses import dataclass
from datetime import date
from pathlib import Path

_TICKER = re.compile(r"(?:SH|SZ|BJ)\d{6}")


@dataclass(frozen=True)
class PriceImportSummary:
    rows: int
    tickers: int
    names: int


@dataclass(frozen=True)
class TickerNameImportSummary:
    rows: int


def _validated_ticker(raw: object, line_number: int) -> str:
    ticker = str(raw or "").strip().upper()
    if not _TICKER.fullmatch(ticker):
        raise ValueError(f"invalid ticker at CSV line {line_number}: {ticker}")
    return ticker


def import_prices_csv(connection: sqlite3.Connection, path: Path) -> PriceImportSummary:
    """Import ``ticker,date,close[,name]`` rows atomically and idempotently."""
    if not path.is_file():
        raise FileNotFoundError(f"price CSV does not exist: {path}")
    rows: list[tuple[str, str, float, str | None]] = []
    with path.open("r", encoding="utf-8-sig", newline="") as source:
        reader = csv.DictReader(source)
        required = {"ticker", "date", "close"}
        if not reader.fieldnames or not required.issubset(reader.fieldnames):
            raise ValueError("price CSV must contain ticker,date,close columns")
        for line_number, raw in enumerate(reader, start=2):
            ticker = _validated_ticker(raw.get("ticker"), line_number)
            raw_date = str(raw.get("date") or "").strip()
            try:
                parsed_date = date.fromisoformat(raw_date).isoformat()
            except ValueError as error:
                raise ValueError(f"invalid date at CSV line {line_number}: {raw_date}") from error
            try:
                close = float(str(raw.get("close") or "").strip())
            except ValueError as error:
                raise ValueError(f"invalid close at CSV line {line_number}") from error
            if not math.isfinite(close) or close <= 0:
                raise ValueError(f"close must be positive at CSV line {line_number}")
            name = str(raw.get("name") or "").strip() or None
            rows.append((ticker, parsed_date, close, name))
    if not rows:
        raise ValueError("price CSV contains no data rows")

    with connection:
        connection.executemany(
            """
            INSERT INTO prices(ticker, date, close) VALUES (?, ?, ?)
            ON CONFLICT(ticker, date) DO UPDATE SET close = excluded.close
            """,
            ((ticker, row_date, close) for ticker, row_date, close, _ in rows),
        )
        connection.executemany(
            """
            INSERT INTO ticker_names(ticker, name) VALUES (?, ?)
            ON CONFLICT(ticker) DO UPDATE SET name = excluded.name
            """,
            ((ticker, name) for ticker, _, _, name in rows if name),
        )
    return PriceImportSummary(
        rows=len(rows),
        tickers=len({ticker for ticker, _, _, _ in rows}),
        names=len({ticker for ticker, _, _, name in rows if name}),
    )


def import_ticker_names_csv(connection: sqlite3.Connection, path: Path) -> TickerNameImportSummary:
    """Import a small locally maintained ``ticker,name`` mapping."""
    if not path.is_file():
        raise FileNotFoundError(f"ticker-name CSV does not exist: {path}")
    rows: list[tuple[str, str]] = []
    with path.open("r", encoding="utf-8-sig", newline="") as source:
        reader = csv.DictReader(source)
        if not reader.fieldnames or not {"ticker", "name"}.issubset(reader.fieldnames):
            raise ValueError("ticker-name CSV must contain ticker,name columns")
        for line_number, raw in enumerate(reader, start=2):
            ticker = _validated_ticker(raw.get("ticker"), line_number)
            name = str(raw.get("name") or "").strip()
            if not name:
                raise ValueError(f"name must not be empty at CSV line {line_number}")
            rows.append((ticker, name))
    if not rows:
        raise ValueError("ticker-name CSV contains no data rows")
    with connection:
        connection.executemany(
            """
            INSERT INTO ticker_names(ticker, name) VALUES (?, ?)
            ON CONFLICT(ticker) DO UPDATE SET name = excluded.name
            """,
            rows,
        )
    return TickerNameImportSummary(rows=len(rows))
