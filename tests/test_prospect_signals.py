"""Tests for scanner.prospect_signals and scanner.prospect_preopen."""
import sqlite3
import tempfile
from datetime import date, timedelta

import pandas as pd
import pytest

from scanner.prospect_signals import _analyze, run as ps_run, build_discord_messages
from scanner.prospect_preopen import _prev_business_day, query_candidates, build_message


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_closes(prices: list[float]) -> pd.Series:
    """Build a daily Close series ending today from a price list (oldest first)."""
    idx = pd.date_range(end=date.today().isoformat(), periods=len(prices), freq="B")
    return pd.Series(prices, index=idx, name="Close")


def _make_db(prospects=None, eod_rows=None):
    """Return a temp sqlite3 DB with prospects + eod_signals populated."""
    conn = sqlite3.connect(":memory:")
    conn.execute("""CREATE TABLE prospects
        (id INTEGER PRIMARY KEY, ticker TEXT, added_date TEXT,
         source_scan TEXT, notes TEXT, status TEXT, bucket TEXT)""")
    conn.execute("""CREATE TABLE eod_signals
        (id INTEGER PRIMARY KEY, signal_date TEXT, ticker TEXT,
         source TEXT, price REAL, tape TEXT, rsi_d REAL, rsi_w REAL,
         macd_d_state TEXT, macd_w_state TEXT, buzz_d REAL, buzz_w REAL,
         ema_4e_pct REAL, toby_status TEXT)""")
    conn.execute("""CREATE TABLE locker_history
        (id INTEGER PRIMARY KEY, ticker TEXT, event TEXT, date TEXT,
         reason TEXT, detail TEXT)""")
    conn.execute("""CREATE TABLE locker_room
        (id INTEGER PRIMARY KEY, ticker TEXT, status TEXT,
         removed_date TEXT, scan_signals TEXT)""")

    for t in (prospects or []):
        conn.execute(
            "INSERT INTO prospects (ticker, added_date, status, bucket) VALUES (?,?,?,?)",
            (t, date.today().isoformat(), "active", "prospect"),
        )
    for row in (eod_rows or []):
        conn.execute(
            "INSERT INTO eod_signals (signal_date, ticker, ema_4e_pct, toby_status) VALUES (?,?,?,?)",
            row,
        )
    conn.commit()
    return conn


# ---------------------------------------------------------------------------
# _analyze
# ---------------------------------------------------------------------------

class TestAnalyze:
    def test_too_short_returns_none(self):
        closes = _make_closes([100, 101, 102])
        status, _, _ = _analyze(closes)
        assert status is None

    def test_reclaim(self):
        # Price holds at 100 for 15 days (EMA ~100), then drops hard to 70 for
        # 4 days so EMA decays but stays above 70, then jumps to 105 today.
        # prev_close (70) < ema4_yest (~74), live_price (105) > ema4_yest -> reclaim.
        prices = [100] * 15 + [70, 70, 70, 70] + [105]
        closes = _make_closes(prices)
        status, live, ema_yest = _analyze(closes)
        assert status == "reclaim"
        assert live > ema_yest

    def test_clarity(self):
        # Price in uptrend, one-day dip below EMA, then recovers
        prices = [100] * 15 + [102, 104, 106, 80] + [109]
        closes = _make_closes(prices)
        status, live, ema_yest = _analyze(closes)
        assert status == "clarity"

    def test_already_above_no_signal(self):
        # Rising trend, never dipped below EMA
        prices = [100 + i for i in range(20)]
        closes = _make_closes(prices)
        status, _, _ = _analyze(closes)
        assert status is None

    def test_still_below_no_signal(self):
        # Downtrend, price still below EMA
        prices = [100 - i * 0.5 for i in range(20)]
        closes = _make_closes(prices)
        status, _, _ = _analyze(closes)
        assert status is None

    def test_ema_computed_without_today_bar(self):
        # The EMA reference (ema4_yest) must not include today's bar.
        # Verify by checking that ema4_yest equals EMA computed on closes[:-1].
        prices = [100] * 15 + [80, 80, 85, 70] + [110]
        closes = _make_closes(prices)
        status, live, ema_yest = _analyze(closes)
        if status is not None:
            expected_ema = float(closes.iloc[:-1].ewm(span=4, adjust=False).mean().iloc[-1])
            assert abs(ema_yest - expected_ema) < 0.001


# ---------------------------------------------------------------------------
# build_discord_messages
# ---------------------------------------------------------------------------

class TestBuildDiscordMessages:
    def test_empty(self):
        assert build_discord_messages({"reclaims": [], "clarities": [], "dry_run": False}) == []

    def test_reclaim_batched(self):
        result = {
            "reclaims": [{"ticker": "AAPL", "price": 200.0, "ema4": 195.0, "pct": 2.56}],
            "clarities": [],
            "dry_run": False,
        }
        msgs = build_discord_messages(result)
        assert len(msgs) == 1
        assert "RECLAIM" in msgs[0]
        assert "AAPL" in msgs[0]

    def test_clarity_batched(self):
        result = {
            "reclaims": [],
            "clarities": [
                {"ticker": "NVDA", "price": 900.0, "ema4": 880.0, "pct": 2.27},
                {"ticker": "TSLA", "price": 250.0, "ema4": 240.0, "pct": 4.17},
            ],
            "dry_run": False,
        }
        msgs = build_discord_messages(result)
        # Both clarity tickers must be in ONE message (batched)
        assert len(msgs) == 1
        assert "NVDA" in msgs[0]
        assert "TSLA" in msgs[0]


# ---------------------------------------------------------------------------
# _prev_business_day
# ---------------------------------------------------------------------------

class TestPrevBusinessDay:
    def test_tuesday_returns_monday(self):
        assert _prev_business_day(date(2026, 5, 19)) == date(2026, 5, 18)

    def test_monday_returns_friday(self):
        assert _prev_business_day(date(2026, 5, 18)) == date(2026, 5, 15)

    def test_saturday_returns_friday(self):
        assert _prev_business_day(date(2026, 5, 16)) == date(2026, 5, 15)


# ---------------------------------------------------------------------------
# query_candidates
# ---------------------------------------------------------------------------

class TestQueryCandidates:
    def test_reclaim_candidate(self):
        today = date.today().isoformat()
        prev = _prev_business_day(date.today()).isoformat()
        conn = _make_db(
            prospects=["AAPL"],
            eod_rows=[
                (today, "AAPL", -3.5, "hold"),   # below EMA today
                (prev,  "AAPL", -2.0, "hold"),   # also below prev day
            ],
        )
        result = query_candidates(conn, today)
        assert len(result["reclaims"]) == 1
        assert result["reclaims"][0]["ticker"] == "AAPL"
        assert len(result["clarities"]) == 0

    def test_clarity_candidate(self):
        today = date.today().isoformat()
        prev = _prev_business_day(date.today()).isoformat()
        conn = _make_db(
            prospects=["NVDA"],
            eod_rows=[
                (today, "NVDA", -1.2, "hold"),   # below EMA today
                (prev,  "NVDA",  2.5, "hold"),   # above EMA prev day
            ],
        )
        result = query_candidates(conn, today)
        assert len(result["clarities"]) == 1
        assert result["clarities"][0]["ticker"] == "NVDA"
        assert len(result["reclaims"]) == 0

    def test_clarity_not_duplicated_in_reclaims(self):
        today = date.today().isoformat()
        prev = _prev_business_day(date.today()).isoformat()
        conn = _make_db(
            prospects=["MSFT"],
            eod_rows=[
                (today, "MSFT", -0.5, "hold"),
                (prev,  "MSFT",  1.8, "hold"),
            ],
        )
        result = query_candidates(conn, today)
        tickers_in_reclaims = [r["ticker"] for r in result["reclaims"]]
        assert "MSFT" not in tickers_in_reclaims

    def test_above_ema_not_included(self):
        today = date.today().isoformat()
        conn = _make_db(
            prospects=["GOOG"],
            eod_rows=[(today, "GOOG", 3.2, "hold")],
        )
        result = query_candidates(conn, today)
        assert result["reclaims"] == []
        assert result["clarities"] == []

    def test_empty_no_eod_data(self):
        today = date.today().isoformat()
        conn = _make_db(prospects=["XYZ"])
        result = query_candidates(conn, today)
        assert result["reclaims"] == []
        assert result["clarities"] == []


# ---------------------------------------------------------------------------
# build_message
# ---------------------------------------------------------------------------

class TestBuildMessage:
    def test_none_when_empty(self):
        assert build_message({"clarities": [], "reclaims": [], "scan_date": "2026-05-20"}) is None

    def test_reclaim_section_present(self):
        result = {
            "clarities": [],
            "reclaims": [{"ticker": "AAPL", "pct": -2.1}],
            "scan_date": "2026-05-20",
        }
        msg = build_message(result)
        assert msg is not None
        assert "RECLAIM WATCH" in msg
        assert "AAPL" in msg

    def test_clarity_shown_before_reclaim(self):
        result = {
            "clarities": [{"ticker": "NVDA", "pct": -0.5}],
            "reclaims":  [{"ticker": "AAPL", "pct": -3.0}],
            "scan_date": "2026-05-20",
        }
        msg = build_message(result)
        assert msg.index("CLARITY") < msg.index("RECLAIM")
