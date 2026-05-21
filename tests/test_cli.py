"""
CLI command tests using an in-memory SQLite DB.

Each test gets a fresh DB via the `db` fixture -- no file on disk, no shared state.
"""
import sqlite3
import types
from datetime import date

import pytest

from db.init import SCHEMA


# ---------------------------------------------------------------------------
# Fixture: in-memory DB seeded with the real schema
# ---------------------------------------------------------------------------

@pytest.fixture
def db(monkeypatch, tmp_path):
    """Return a sqlite3.Connection to an in-memory DB with the real schema.

    Also patches scanner.cli.DB_PATH so the CLI commands hit this DB.
    """
    conn = sqlite3.connect(":memory:")
    conn.executescript(SCHEMA)
    conn.commit()

    db_file = tmp_path / "test.db"
    file_conn = sqlite3.connect(db_file)
    file_conn.executescript(SCHEMA)
    file_conn.commit()
    file_conn.close()

    import scanner.cli as cli_mod
    monkeypatch.setattr(cli_mod, "DB_PATH", db_file)

    return db_file


def read(db_file, sql, params=()):
    conn = sqlite3.connect(db_file)
    rows = conn.execute(sql, params).fetchall()
    conn.close()
    return rows


# ---------------------------------------------------------------------------
# add-prospect
# ---------------------------------------------------------------------------

def test_add_prospect_inserts_row(db):
    from scanner.cli import cmd_add_prospect

    args = types.SimpleNamespace(tickers=["AAPL"], source="sc_workbench", notes="test note", bucket="prospect")
    cmd_add_prospect(args)

    rows = read(db, "SELECT ticker, source_scan, notes, bucket, status FROM prospects")
    assert len(rows) == 1
    ticker, source, notes, bucket, status = rows[0]
    assert ticker == "AAPL"
    assert source == "sc_workbench"
    assert notes == "test note"
    assert bucket == "prospect"
    assert status == "active"


def test_add_prospect_lowercases_ticker(db):
    from scanner.cli import cmd_add_prospect

    args = types.SimpleNamespace(tickers=["nvda"], source="sc_workbench", notes=None, bucket="prospect")
    cmd_add_prospect(args)

    rows = read(db, "SELECT ticker FROM prospects")
    assert rows[0][0] == "NVDA"


def test_add_prospect_buy_and_hold_bucket(db):
    from scanner.cli import cmd_add_prospect

    args = types.SimpleNamespace(tickers=["OXY"], source="position_scan", notes="200w SMA", bucket="buy_and_hold")
    cmd_add_prospect(args)

    rows = read(db, "SELECT bucket FROM prospects WHERE ticker='OXY'")
    assert rows[0][0] == "buy_and_hold"


# ---------------------------------------------------------------------------
# drop-prospect
# ---------------------------------------------------------------------------

def test_drop_prospect_sets_status(db):
    from scanner.cli import cmd_add_prospect, cmd_drop_prospect

    cmd_add_prospect(types.SimpleNamespace(tickers=["COHR"], source="sc_workbench", notes=None, bucket="prospect"))
    cmd_drop_prospect(types.SimpleNamespace(ticker="COHR"))

    rows = read(db, "SELECT status FROM prospects WHERE ticker='COHR'")
    assert rows[0][0] == "dropped"


def test_drop_prospect_missing_ticker_is_noop(db, capsys):
    from scanner.cli import cmd_drop_prospect

    cmd_drop_prospect(types.SimpleNamespace(ticker="FAKE"))
    out = capsys.readouterr().out
    assert "not found" in out


# ---------------------------------------------------------------------------
# promote
# ---------------------------------------------------------------------------

def test_promote_moves_prospect_to_locker(db):
    from scanner.cli import cmd_add_prospect, cmd_promote

    cmd_add_prospect(types.SimpleNamespace(tickers=["NVDA"], source="sc_workbench", notes=None, bucket="prospect"))
    cmd_promote(types.SimpleNamespace(ticker="NVDA"))

    prospect_status = read(db, "SELECT status FROM prospects WHERE ticker='NVDA'")[0][0]
    assert prospect_status == "promoted"

    locker_rows = read(db, "SELECT ticker, status FROM locker_room WHERE ticker='NVDA'")
    assert len(locker_rows) == 1
    assert locker_rows[0][1] == "active"


def test_promote_missing_prospect_is_noop(db, capsys):
    from scanner.cli import cmd_promote

    cmd_promote(types.SimpleNamespace(ticker="FAKE"))
    out = capsys.readouterr().out
    assert "not in active prospects" in out


# ---------------------------------------------------------------------------
# exit (locker room)
# ---------------------------------------------------------------------------

def test_exit_removes_from_locker(db):
    from scanner.cli import cmd_add_prospect, cmd_promote, cmd_exit

    cmd_add_prospect(types.SimpleNamespace(tickers=["TSLA"], source="sc_workbench", notes=None, bucket="prospect"))
    cmd_promote(types.SimpleNamespace(ticker="TSLA"))
    cmd_exit(types.SimpleNamespace(ticker="TSLA"))

    rows = read(db, "SELECT status, removed_date FROM locker_room WHERE ticker='TSLA'")
    assert rows[0][0] == "removed"
    assert rows[0][1] == date.today().isoformat()


# ---------------------------------------------------------------------------
# list-prospects
# ---------------------------------------------------------------------------

def test_list_prospects_shows_all(db, capsys):
    from scanner.cli import cmd_add_prospect, cmd_list_prospects

    cmd_add_prospect(types.SimpleNamespace(tickers=["AAPL", "MSFT"], source="sc_workbench", notes=None, bucket="prospect"))
    cmd_list_prospects(types.SimpleNamespace(bucket=None))

    out = capsys.readouterr().out
    assert "AAPL" in out
    assert "MSFT" in out


def test_list_prospects_bucket_filter(db, capsys):
    from scanner.cli import cmd_add_prospect, cmd_list_prospects

    cmd_add_prospect(types.SimpleNamespace(tickers=["AAPL"], source="sc_workbench", notes=None, bucket="prospect"))
    cmd_add_prospect(types.SimpleNamespace(tickers=["OXY"], source="position_scan", notes=None, bucket="buy_and_hold"))
    capsys.readouterr()  # flush insert prints before asserting list output
    cmd_list_prospects(types.SimpleNamespace(bucket="buy_and_hold"))

    out = capsys.readouterr().out
    assert "OXY" in out
    assert "AAPL" not in out


# ---------------------------------------------------------------------------
# prospect-sc-sync dry-run
# ---------------------------------------------------------------------------

def test_prospect_sc_sync_dry_run(db, capsys):
    from scanner.cli import cmd_add_prospect, cmd_prospect_sc_sync

    cmd_add_prospect(types.SimpleNamespace(tickers=["NVDA", "AMD"], source="sc_workbench", notes=None, bucket="prospect"))
    cmd_prospect_sc_sync(types.SimpleNamespace(bucket="prospect", clear=False, dry_run=True))

    out = capsys.readouterr().out
    assert "NVDA" in out
    assert "AMD" in out
    assert "dry-run" in out.lower()
