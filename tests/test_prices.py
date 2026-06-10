from __future__ import annotations

from pathlib import Path

import pytest

from kol_archive.database import connect_database, initialize_database
from kol_archive.prices import import_prices_csv, import_ticker_names_csv


def test_import_prices_csv_is_idempotent_and_imports_optional_names(tmp_path: Path) -> None:
    connection = connect_database(":memory:")
    initialize_database(connection)
    source = tmp_path / "prices.csv"
    source.write_text(
        "ticker,date,close,name\nSH000300,2026-06-01,100,沪深300\nSZ300973,2026-06-01,25.5,立高食品\n",
        encoding="utf-8",
    )

    first = import_prices_csv(connection, source)
    source.write_text(
        "ticker,date,close,name\nSH000300,2026-06-01,101,沪深300\nSZ300973,2026-06-01,25.5,立高食品\n",
        encoding="utf-8",
    )
    second = import_prices_csv(connection, source)

    assert first.rows == second.rows == 2
    assert (
        connection.execute("SELECT close FROM prices WHERE ticker = 'SH000300'").fetchone()[0]
        == 101
    )
    assert (
        connection.execute("SELECT name FROM ticker_names WHERE ticker = 'SZ300973'").fetchone()[0]
        == "立高食品"
    )
    connection.close()


def test_import_prices_csv_validates_all_rows_before_writing(tmp_path: Path) -> None:
    connection = connect_database(":memory:")
    initialize_database(connection)
    source = tmp_path / "prices.csv"
    source.write_text(
        "ticker,date,close\nSH000300,2026-06-01,100\nbad,2026-06-01,2\n",
        encoding="utf-8",
    )

    with pytest.raises(ValueError, match="invalid ticker"):
        import_prices_csv(connection, source)

    assert connection.execute("SELECT COUNT(*) FROM prices").fetchone()[0] == 0
    connection.close()


def test_import_ticker_names_csv_updates_local_mapping(tmp_path: Path) -> None:
    connection = connect_database(":memory:")
    initialize_database(connection)
    source = tmp_path / "names.csv"
    source.write_text("ticker,name\nBJ920982,锦波生物\n", encoding="utf-8")

    summary = import_ticker_names_csv(connection, source)

    assert summary.rows == 1
    assert (
        connection.execute("SELECT name FROM ticker_names WHERE ticker = 'BJ920982'").fetchone()[0]
        == "锦波生物"
    )
    connection.close()
