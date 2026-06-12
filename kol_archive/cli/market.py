"""Market-data commands: prices/ticker-name imports, K-line fetch, and the watchlist."""

from __future__ import annotations

import argparse
from datetime import UTC, datetime
from pathlib import Path

from kol_archive.config import load_config
from kol_archive.kline import (
    DEFAULT_BAR_COUNT,
    discover_tickers,
    fetch_and_store,
    validated_symbol,
)
from kol_archive.prices import import_prices_csv, import_ticker_names_csv
from kol_archive.watchlist import (
    add_watchlist_ticker,
    list_watchlist,
    remove_watchlist_ticker,
)

from .collect import _build_collector_client
from .common import configured_db_path, connect_existing_archive, print_json, resolve_db_path


def _import_prices_command(args: argparse.Namespace) -> None:
    config = load_config(args.config_dir)
    connection, _ = connect_existing_archive(resolve_db_path(args.path, config))
    try:
        summary = import_prices_csv(connection, args.csv_path)
        print_json({"rows": summary.rows, "tickers": summary.tickers, "names": summary.names})
    finally:
        connection.close()


def _import_ticker_names_command(args: argparse.Namespace) -> None:
    config = load_config(args.config_dir)
    connection, _ = connect_existing_archive(resolve_db_path(args.path, config))
    try:
        summary = import_ticker_names_csv(connection, args.csv_path)
        print_json({"rows": summary.rows})
    finally:
        connection.close()


def _fetch_kline_command(args: argparse.Namespace) -> None:
    config = load_config(args.config_dir)
    prices_config = config.get("prices") or {}
    benchmark = (args.benchmark or str(prices_config.get("benchmark_ticker") or "SH000300")).upper()
    connection, _ = connect_existing_archive(resolve_db_path(args.path, config))
    client = None
    try:
        tickers = (
            [validated_symbol(ticker) for ticker in args.ticker]
            if args.ticker
            else discover_tickers(connection)
        )
        # The benchmark must share dates with each asset for the snapshot join, so always
        # pull it alongside whatever assets we fetch.
        if benchmark not in tickers:
            tickers.append(benchmark)
        if not tickers:
            note = "no tracked tickers; pass --ticker"
            print_json({"tickers": 0, "bars": 0, "failures": [], "note": note})
            return
        client = _build_collector_client(config)
        summary = fetch_and_store(connection, client, tickers, count=args.count)
        print_json(
            {"tickers": summary.tickers, "bars": summary.bars, "failures": list(summary.failures)}
        )
    finally:
        if client is not None:
            client.close()
        connection.close()


def _watch_ticker_command(args: argparse.Namespace) -> None:
    connection, _ = connect_existing_archive(configured_db_path(args.path, args.config_dir))
    try:
        add_watchlist_ticker(
            connection,
            args.ticker,
            datetime.now(tz=UTC).isoformat(),
            name=args.name,
            note=args.note,
        )
        print_json({"ticker": args.ticker.strip().upper(), "watched": True})
    finally:
        connection.close()


def _unwatch_ticker_command(args: argparse.Namespace) -> None:
    connection, _ = connect_existing_archive(configured_db_path(args.path, args.config_dir))
    try:
        removed = remove_watchlist_ticker(connection, args.ticker)
        print_json({"ticker": args.ticker.strip().upper(), "removed": removed})
    finally:
        connection.close()


def _watchlist_command(args: argparse.Namespace) -> None:
    connection, _ = connect_existing_archive(configured_db_path(args.path, args.config_dir))
    try:
        print_json(list_watchlist(connection))
    finally:
        connection.close()


def register(subparsers: argparse._SubParsersAction[argparse.ArgumentParser]) -> None:
    import_prices_parser = subparsers.add_parser(
        "import-prices", help="import ticker,date,close[,name] CSV rows"
    )
    import_prices_parser.add_argument("csv_path", type=Path)
    import_prices_parser.add_argument("--path", type=Path)
    import_prices_parser.add_argument("--config-dir", type=Path, default=Path("config"))
    import_prices_parser.set_defaults(handler=_import_prices_command)
    import_names_parser = subparsers.add_parser(
        "import-ticker-names", help="import a locally maintained ticker,name CSV"
    )
    import_names_parser.add_argument("csv_path", type=Path)
    import_names_parser.add_argument("--path", type=Path)
    import_names_parser.add_argument("--config-dir", type=Path, default=Path("config"))
    import_names_parser.set_defaults(handler=_import_ticker_names_command)
    fetch_kline_parser = subparsers.add_parser(
        "fetch-kline", help="fetch daily OHLC bars from Xueqiu via the dedicated browser"
    )
    fetch_kline_parser.add_argument(
        "--ticker",
        action="append",
        help="ticker to fetch (repeatable); default: every tracked ticker",
    )
    fetch_kline_parser.add_argument(
        "--benchmark",
        help="benchmark ticker to include (default: prices.benchmark_ticker or SH000300)",
    )
    fetch_kline_parser.add_argument(
        "--count", type=int, default=DEFAULT_BAR_COUNT, help="daily bars to pull per ticker"
    )
    fetch_kline_parser.add_argument("--path", type=Path)
    fetch_kline_parser.add_argument("--config-dir", type=Path, default=Path("config"))
    fetch_kline_parser.set_defaults(handler=_fetch_kline_command)
    watch_ticker_parser = subparsers.add_parser(
        "watch-ticker", help="add or update one ticker in the watchlist"
    )
    watch_ticker_parser.add_argument("ticker")
    watch_ticker_parser.add_argument("--name")
    watch_ticker_parser.add_argument("--note")
    watch_ticker_parser.add_argument("--path", type=Path)
    watch_ticker_parser.add_argument("--config-dir", type=Path, default=Path("config"))
    watch_ticker_parser.set_defaults(handler=_watch_ticker_command)
    unwatch_ticker_parser = subparsers.add_parser(
        "unwatch-ticker", help="remove one ticker from the watchlist"
    )
    unwatch_ticker_parser.add_argument("ticker")
    unwatch_ticker_parser.add_argument("--path", type=Path)
    unwatch_ticker_parser.add_argument("--config-dir", type=Path, default=Path("config"))
    unwatch_ticker_parser.set_defaults(handler=_unwatch_ticker_command)
    watchlist_parser = subparsers.add_parser("watchlist", help="list watched tickers")
    watchlist_parser.add_argument("--path", type=Path)
    watchlist_parser.add_argument("--config-dir", type=Path, default=Path("config"))
    watchlist_parser.set_defaults(handler=_watchlist_command)
