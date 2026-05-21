"""
Tests for scanner.auto_exit -- EXIT-D2 drops all active locker tickers (position and watchlist).
"""
import sqlite3
from datetime import date, timedelta

import pytest

from db.init import SCHEMA

_EOD_SIGNALS_DDL = """
CREATE TABLE IF NOT EXISTS eod_signals (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    signal_date  TEXT NOT NULL,
    ticker       TEXT NOT NULL,
    source       TEXT NOT NULL,
    price        REAL,
    tape         TEXT,
    rsi_d        REAL,
    rsi_w        REAL,
    macd_d_state TEXT,
    macd_w_state TEXT,
    buzz_d       REAL,
    buzz_w       REAL,
    ema_4e_pct   REAL,
    toby_status  TEXT,
    UNIQUE(ticker, signal_date)
);
"""


@pytest.fixture
def db(monkeypatch, tmp_path):
    db_file = tmp_path / "ae_test.db"
    conn = sqlite3.connect(db_file)
    conn.executescript(SCHEMA)
    conn.executescript(_EOD_SIGNALS_DDL)
    conn.commit()
    conn.close()

    import scanner.auto_exit as ae_mod
    import locker._helpers as locker_helpers
    monkeypatch.setattr(ae_mod, "DB_PATH", db_file)
    monkeypatch.setattr(locker_helpers, "DB_PATH", db_file)
    monkeypatch.setattr(locker_helpers, "_push_to_droplet", lambda: None)
    # suppress Discord + KB side-effects
    monkeypatch.setattr(ae_mod, "discord_router", type("DR", (), {"send_to": staticmethod(lambda *a, **k: None)})())
    monkeypatch.setattr(ae_mod, "ob1_capture", lambda *a, **k: None)
    return db_file


def _seed(db_file, ticker, toby_status, scan_date, has_position=False):
    conn = sqlite3.connect(db_file)
    conn.execute(
        "INSERT INTO locker_room (ticker, status, promoted_date) VALUES (?, 'active', ?)",
        (ticker, scan_date),
    )
    conn.execute(
        """INSERT INTO eod_signals (signal_date, ticker, source, price, tape, toby_status)
           VALUES (?, ?, 'locker', 10.0, 'green', ?)""",
        (scan_date, ticker, toby_status),
    )
    if has_position:
        conn.execute(
            """INSERT INTO positions (ticker, broker, entry_date, entry_price, shares,
               stop_price, size_type, setup, r_risk, status)
               VALUES (?, 'alpaca', ?, 9.0, 100, 8.0, 'flat', 'test', 1.0, 'open')""",
            (ticker, scan_date),
        )
    conn.commit()
    conn.close()


def _read(db_file, sql, params=()):
    conn = sqlite3.connect(db_file)
    rows = conn.execute(sql, params).fetchall()
    conn.close()
    return rows


# ---------------------------------------------------------------------------
# EXIT-D2: watchlist-only ticker gets dropped
# ---------------------------------------------------------------------------

def test_exit_d2_watchlist_only_is_dropped(db):
    today = date.today().isoformat()
    _seed(db, "CVS", "exit-d2", today, has_position=False)

    from scanner.auto_exit import run
    result = run(today)

    assert any(p["ticker"] == "CVS" for p in result["dropped"])
    rows = _read(db, "SELECT status FROM locker_room WHERE ticker='CVS'")
    assert rows[0][0] == "removed"


def test_exit_d2_watchlist_only_logs_history(db):
    today = date.today().isoformat()
    _seed(db, "CVS", "exit-d2", today, has_position=False)

    from scanner.auto_exit import run
    run(today)

    rows = _read(db, "SELECT event FROM locker_history WHERE ticker='CVS'")
    events = [r[0] for r in rows]
    assert "auto_drop" in events


def test_exit_d2_watchlist_only_recycled_to_prospects(db):
    today = date.today().isoformat()
    _seed(db, "CVS", "exit-d2", today, has_position=False)

    from scanner.auto_exit import run
    run(today)

    rows = _read(db, "SELECT status FROM prospects WHERE ticker='CVS'")
    assert len(rows) == 1
    assert rows[0][0] == "active"


# ---------------------------------------------------------------------------
# EXIT-D2: position holder still gets dropped (regression)
# ---------------------------------------------------------------------------

def test_exit_d2_position_holder_is_dropped(db):
    today = date.today().isoformat()
    _seed(db, "HIMX", "exit-d2", today, has_position=True)

    from scanner.auto_exit import run
    result = run(today)

    assert any(p["ticker"] == "HIMX" for p in result["dropped"])
    rows = _read(db, "SELECT status FROM locker_room WHERE ticker='HIMX'")
    assert rows[0][0] == "removed"


# ---------------------------------------------------------------------------
# EXIT-D2: dry_run does not write to DB
# ---------------------------------------------------------------------------

def test_exit_d2_dry_run_no_write(db):
    today = date.today().isoformat()
    _seed(db, "BRUN", "exit-d2", today, has_position=False)

    from scanner.auto_exit import run
    result = run(today, dry_run=True)

    assert result["dry_run"] is True
    assert any(p["ticker"] == "BRUN" for p in result["dropped"])
    rows = _read(db, "SELECT status FROM locker_room WHERE ticker='BRUN'")
    assert rows[0][0] == "active"  # unchanged


# ---------------------------------------------------------------------------
# Result dict does not contain watching_d2
# ---------------------------------------------------------------------------

def test_result_has_no_watching_d2_key(db):
    today = date.today().isoformat()
    from scanner.auto_exit import run
    result = run(today)
    assert "watching_d2" not in result
