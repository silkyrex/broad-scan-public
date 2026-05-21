"""
Locker CLI command tests using a real schema in a temp DB.
No yfinance calls -- commands that require live data are excluded.
"""
import json
import sqlite3
import types
from datetime import date
from pathlib import Path

import pytest

from db.init import SCHEMA


@pytest.fixture
def db(monkeypatch, tmp_path):
    db_file = tmp_path / "locker_test.db"
    conn = sqlite3.connect(db_file)
    conn.executescript(SCHEMA)
    conn.commit()
    conn.close()

    import locker._helpers as locker_helpers
    monkeypatch.setattr(locker_helpers, "DB_PATH", db_file)
    # suppress scp push in all tests
    monkeypatch.setattr(locker_helpers, "_push_to_droplet", lambda: None)
    return db_file


def read(db_file, sql, params=()):
    conn = sqlite3.connect(db_file)
    rows = conn.execute(sql, params).fetchall()
    conn.close()
    return rows


# ---------------------------------------------------------------------------
# cmd_add
# ---------------------------------------------------------------------------

def test_add_inserts_active_row(db):
    from locker.cli import cmd_add
    cmd_add(types.SimpleNamespace(ticker="NVDA", source="manual", sector="Tech",
                                   notes="AI chips", bucket="prospect", scan_signals="[]"))
    rows = read(db, "SELECT ticker, status, sector FROM locker_room WHERE ticker='NVDA'")
    assert len(rows) == 1
    assert rows[0][1] == "active"
    assert rows[0][2] == "Tech"


def test_add_uppercases_ticker(db):
    from locker.cli import cmd_add
    cmd_add(types.SimpleNamespace(ticker="nvda", source="manual", sector="",
                                   notes="", bucket="prospect", scan_signals="[]"))
    rows = read(db, "SELECT ticker FROM locker_room")
    assert rows[0][0] == "NVDA"


def test_add_logs_history(db):
    from locker.cli import cmd_add
    cmd_add(types.SimpleNamespace(ticker="AAPL", source="manual", sector="",
                                   notes="", bucket="prospect", scan_signals="[]"))
    rows = read(db, "SELECT event FROM locker_history WHERE ticker='AAPL'")
    assert len(rows) >= 1
    assert rows[0][0] == "added"


def test_add_updates_existing_active_row(db):
    from locker.cli import cmd_add
    cmd_add(types.SimpleNamespace(ticker="AAPL", source="manual", sector="Tech",
                                   notes="", bucket="prospect", scan_signals="[]"))
    cmd_add(types.SimpleNamespace(ticker="AAPL", source="chart", sector="Semis",
                                   notes="", bucket="prospect", scan_signals="[]"))
    rows = read(db, "SELECT COUNT(*) FROM locker_room WHERE ticker='AAPL'")
    assert rows[0][0] == 1  # still one row, not a duplicate
    rows = read(db, "SELECT sector FROM locker_room WHERE ticker='AAPL'")
    assert rows[0][0] == "Semis"


# ---------------------------------------------------------------------------
# cmd_del
# ---------------------------------------------------------------------------

def test_del_sets_removed_status(db):
    from locker.cli import cmd_add, cmd_del
    cmd_add(types.SimpleNamespace(ticker="TSLA", source="manual", sector="",
                                   notes="", bucket="prospect", scan_signals="[]"))
    cmd_del(types.SimpleNamespace(ticker="TSLA"))
    rows = read(db, "SELECT status FROM locker_room WHERE ticker='TSLA'")
    assert rows[0][0] == "removed"


def test_del_missing_ticker_is_noop(db, capsys):
    from locker.cli import cmd_del
    cmd_del(types.SimpleNamespace(ticker="FAKE"))
    out = capsys.readouterr().out
    assert "not in active locker room" in out


# ---------------------------------------------------------------------------
# cmd_note
# ---------------------------------------------------------------------------

def test_note_updates_text(db):
    from locker.cli import cmd_add, cmd_note
    cmd_add(types.SimpleNamespace(ticker="COHR", source="manual", sector="",
                                   notes="original", bucket="prospect", scan_signals="[]"))
    cmd_note(types.SimpleNamespace(ticker="COHR", text="updated thesis"))
    rows = read(db, "SELECT notes FROM locker_room WHERE ticker='COHR'")
    assert rows[0][0] == "updated thesis"


def test_note_missing_ticker_is_noop(db, capsys):
    from locker.cli import cmd_note
    cmd_note(types.SimpleNamespace(ticker="FAKE", text="x"))
    out = capsys.readouterr().out
    assert "not found" in out


# ---------------------------------------------------------------------------
# cmd_stale
# ---------------------------------------------------------------------------

def test_stale_flags_old_name(db, capsys):
    from locker.cli import cmd_add, cmd_stale
    import locker.cli as locker_mod
    import sqlite3 as sq

    cmd_add(types.SimpleNamespace(ticker="XYZ", source="manual", sector="",
                                   notes="", bucket="prospect", scan_signals="[]"))
    # backdate promoted_date to 30 days ago
    conn = sq.connect(db)
    conn.execute("UPDATE locker_room SET promoted_date=date('now','-30 days') WHERE ticker='XYZ'")
    conn.commit()
    conn.close()

    cmd_stale(types.SimpleNamespace(days=14))
    out = capsys.readouterr().out
    assert "XYZ" in out


def test_stale_recent_name_not_flagged(db, capsys):
    from locker.cli import cmd_add, cmd_stale
    cmd_add(types.SimpleNamespace(ticker="NEW", source="manual", sector="",
                                   notes="", bucket="prospect", scan_signals="[]"))
    cmd_stale(types.SimpleNamespace(days=14))
    out = capsys.readouterr().out
    assert "No stale" in out or "NEW" not in out


# ---------------------------------------------------------------------------
# cmd_open_pos / cmd_close_pos
# ---------------------------------------------------------------------------

def test_open_position_inserts_row(db):
    from locker.cli import cmd_add, cmd_open_pos
    cmd_add(types.SimpleNamespace(ticker="META", source="manual", sector="",
                                   notes="", bucket="prospect", scan_signals="[]"))
    cmd_open_pos(types.SimpleNamespace(
        ticker="META", shares=1, entry=500.0, stop=470.0,
        broker="alpaca", size="full", setup="4ema_reclaim"
    ))
    rows = read(db, "SELECT ticker, status, entry_price, shares FROM positions WHERE ticker='META'")
    assert len(rows) == 1
    assert rows[0][1] == "open"
    assert rows[0][2] == 500.0
    assert rows[0][3] == 1.0


def test_open_position_calculates_r_risk(db):
    from locker.cli import cmd_add, cmd_open_pos
    cmd_add(types.SimpleNamespace(ticker="MSFT", source="manual", sector="",
                                   notes="", bucket="prospect", scan_signals="[]"))
    cmd_open_pos(types.SimpleNamespace(
        ticker="MSFT", shares=1, entry=400.0, stop=380.0,
        broker="alpaca", size="full", setup="4ema_reclaim"
    ))
    rows = read(db, "SELECT r_risk FROM positions WHERE ticker='MSFT'")
    # r_risk = shares * (entry - stop) = 1 * 20 = 20
    assert rows[0][0] == pytest.approx(20.0)


def test_open_position_blocks_duplicate(db, capsys):
    from locker.cli import cmd_add, cmd_open_pos
    cmd_add(types.SimpleNamespace(ticker="AMZN", source="manual", sector="",
                                   notes="", bucket="prospect", scan_signals="[]"))
    args = types.SimpleNamespace(ticker="AMZN", shares=10, entry=200.0, stop=190.0,
                                  broker="alpaca", size="full", setup="4ema_reclaim")
    cmd_open_pos(args)
    cmd_open_pos(args)
    out = capsys.readouterr().out
    assert "already has an open position" in out
    rows = read(db, "SELECT COUNT(*) FROM positions WHERE ticker='AMZN' AND status='open'")
    assert rows[0][0] == 1


def test_open_position_requires_locker_membership(db, capsys):
    from locker.cli import cmd_open_pos
    cmd_open_pos(types.SimpleNamespace(
        ticker="GHOST", shares=10, entry=100.0, stop=90.0,
        broker="alpaca", size="full", setup="4ema_reclaim"
    ))
    out = capsys.readouterr().out
    assert "not in active locker room" in out
