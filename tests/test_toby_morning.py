"""Tests for scanner/toby_morning.py scoring and message-building."""
import sqlite3
import tempfile
from pathlib import Path

import pytest

from scanner.toby_morning import (
    _score,
    build_discord_message,
    build_ob1_content,
    score_locker_tickers,
)


# --- _score unit tests ---

def test_score_perfect():
    h = _score("NVDA", "locker", "reclaim", 75.0, {"NVDA"}, {"NVDA"}, set())
    assert h["score"] == 5  # new_high(2) + buzz(1) + reclaim(1) + rsi_d(1)
    assert h["new_high"] and h["vol_buzz"] and h["reclaim"] and h["rsi_hit"]


def test_score_three_of_four_no_reclaim():
    h = _score("META", "locker", "above", 72.0, {"META"}, {"META"}, set())
    assert h["score"] == 4  # new_high(2) + buzz(1) + rsi_d(1), no reclaim
    assert not h["reclaim"]


def test_score_two_of_four_excluded():
    h = _score("AAPL", "locker", "above", 65.0, {"AAPL"}, set(), set())
    assert h["score"] == 2


def test_score_rsi_from_scans_set():
    """rsi_d=None but ticker in rsi_set still counts."""
    h = _score("TSLA", "prospect", "reclaim", None, {"TSLA"}, set(), {"TSLA"})
    assert h["rsi_hit"]
    assert h["score"] == 4  # new_high(2) + reclaim(1) + rsi_set(1)


def test_score_new_highs_worth_two_points():
    h = _score("AMD", "locker", "below", 60.0, {"AMD"}, set(), set())
    assert h["score"] == 2
    assert h["new_high"]
    assert not h["reclaim"] and not h["vol_buzz"] and not h["rsi_hit"]


# --- build_discord_message ---

def _make_hits():
    return [
        {"ticker": "NVDA", "source": "locker", "score": 4,
         "new_high": True, "vol_buzz": True, "reclaim": True, "rsi_hit": True},
        {"ticker": "META", "source": "locker", "score": 3,
         "new_high": True, "vol_buzz": True, "reclaim": False, "rsi_hit": True},
        {"ticker": "AAPL", "source": "locker", "score": 2,
         "new_high": True, "vol_buzz": False, "reclaim": False, "rsi_hit": False},
    ]


def test_message_includes_qualified_only():
    msg = build_discord_message(_make_hits(), "2026-05-18", 3, 0)
    assert "NVDA" in msg
    assert "META" in msg
    assert "AAPL" not in msg


def test_message_sorted_by_score():
    msg = build_discord_message(_make_hits(), "2026-05-18", 3, 0)
    assert msg.index("NVDA") < msg.index("META")


def test_message_no_qualified():
    hits = [{"ticker": "AAPL", "source": "locker", "score": 2,
             "new_high": True, "vol_buzz": False, "reclaim": False, "rsi_hit": False}]
    msg = build_discord_message(hits, "2026-05-18", 1, 0)
    assert "no candidates" in msg


def test_message_checkboxes():
    msg = build_discord_message(_make_hits(), "2026-05-18", 3, 0)
    assert "✅ New high" in msg
    assert "·  4 EMA reclaim" in msg  # META has no reclaim


# --- build_ob1_content ---

def test_ob1_empty_when_no_qualified():
    hits = [{"ticker": "X", "source": "locker", "score": 2,
             "new_high": True, "vol_buzz": False, "reclaim": False, "rsi_hit": False}]
    assert build_ob1_content(hits, "2026-05-18") == ""


def test_ob1_lists_tickers():
    content = build_ob1_content(_make_hits(), "2026-05-18")
    assert "NVDA" in content
    assert "META" in content
    assert "toby_morning" in content


# --- score_locker_tickers integration with temp DB ---

@pytest.fixture
def temp_db():
    schema = """
    CREATE TABLE locker_room (
        id INTEGER PRIMARY KEY,
        ticker TEXT,
        status TEXT,
        ema4_status TEXT,
        rsi_d REAL
    );
    CREATE TABLE scans (
        id INTEGER PRIMARY KEY,
        ticker TEXT,
        scan_type TEXT,
        scan_date TEXT,
        value REAL,
        is_new_entry INTEGER,
        week_ending TEXT
    );
    CREATE TABLE prospects (
        id INTEGER PRIMARY KEY,
        ticker TEXT,
        status TEXT
    );
    """
    with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as f:
        db_path = Path(f.name)
    conn = sqlite3.connect(db_path)
    conn.executescript(schema)
    conn.execute("INSERT INTO locker_room VALUES (1, 'NVDA', 'active', 'reclaim', 75.0)")
    conn.execute("INSERT INTO locker_room VALUES (2, 'META', 'active', 'above', 72.0)")
    conn.execute("INSERT INTO locker_room VALUES (3, 'AAPL', 'active', 'below', 65.0)")
    conn.execute("INSERT INTO locker_room VALUES (4, 'TEST', 'active', 'reclaim', 80.0)")
    conn.commit()
    yield conn
    conn.close()
    db_path.unlink(missing_ok=True)


def test_score_locker_excludes_test_ticker(temp_db):
    results = score_locker_tickers(temp_db, set(), set(), set())
    tickers = [r["ticker"] for r in results]
    assert "TEST" not in tickers


def test_score_locker_correct_scores(temp_db):
    highs = {"NVDA", "META"}
    buzz  = {"NVDA"}
    results = score_locker_tickers(temp_db, highs, buzz, set())
    by_ticker = {r["ticker"]: r for r in results}

    assert by_ticker["NVDA"]["score"] == 5  # new_high(2) + buzz(1) + reclaim(1) + rsi_d(1)
    assert by_ticker["META"]["score"] == 3  # new_high(2) + above(0) + rsi_d>=70(1)
    assert by_ticker["AAPL"]["score"] == 0  # nothing


def test_score_locker_rsi_from_stored_rsi_d(temp_db):
    results = score_locker_tickers(temp_db, set(), set(), set())
    by_ticker = {r["ticker"]: r for r in results}
    assert by_ticker["NVDA"]["rsi_hit"]   # rsi_d=75 >= 70
    assert by_ticker["META"]["rsi_hit"]   # rsi_d=72 >= 70
    assert not by_ticker["AAPL"]["rsi_hit"]  # rsi_d=65 < 70
