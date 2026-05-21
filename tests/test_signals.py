import pandas as pd
import numpy as np
import pytest
from datetime import date
from scanner.signals import check_signals, week_ending


def rising_close(n=100, start=100.0, step=1.0) -> pd.Series:
    """Monotonically rising close series with a DatetimeIndex."""
    idx = pd.date_range(end="2026-05-13", periods=n, freq="B")
    return pd.Series([start + i * step for i in range(n)], index=idx, name="Close")


def flat_close(n=100, price=50.0) -> pd.Series:
    idx = pd.date_range(end="2026-05-13", periods=n, freq="B")
    return pd.Series([price] * n, index=idx, name="Close")


# --- week_ending ---

def test_week_ending_wednesday():
    d = date(2026, 5, 13)  # Wednesday
    assert week_ending(d) == date(2026, 5, 16)  # Saturday


def test_week_ending_saturday():
    d = date(2026, 5, 16)  # Saturday
    assert week_ending(d) == date(2026, 5, 16)


def test_week_ending_sunday():
    d = date(2026, 5, 17)  # Sunday
    assert week_ending(d) == date(2026, 5, 23)  # Next Saturday


# --- 52w high ---

def test_52w_high_new_entry():
    """Stock dips below its 52w high yesterday, then pops above today -> is_new_signal."""
    n = 260
    idx = pd.date_range(end="2026-05-13", periods=n, freq="B")
    # 258 days flat at 50, yesterday dips to 49, today pops to 52
    prices = [50.0] * 258 + [49.0, 52.0]
    close = pd.Series(prices, index=idx, name="Close")
    results = check_signals(close, date(2026, 5, 13))
    scan_types = [r["scan_type"] for r in results]
    assert "52w_high" in scan_types
    hit = next(r for r in results if r["scan_type"] == "52w_high")
    assert hit["is_new_signal"] == 1


def test_52w_high_not_triggered_on_flat():
    """Flat price series never makes a new 52w high."""
    close = flat_close(n=260)
    results = check_signals(close, date(2026, 5, 13))
    scan_types = [r["scan_type"] for r in results]
    assert "52w_high" not in scan_types


# --- ATH ---

def test_ath_new_entry():
    """Rising series: today is ATH."""
    close = rising_close(n=300)
    results = check_signals(close, date(2026, 5, 13))
    scan_types = [r["scan_type"] for r in results]
    assert "ath" in scan_types


# --- 12w high ---

def test_12w_high_new_entry():
    """Rising series: today is a 12w high."""
    close = rising_close(n=100)
    results = check_signals(close, date(2026, 5, 13))
    scan_types = [r["scan_type"] for r in results]
    assert "12w_high" in scan_types


# --- RSI daily ---

def test_rsi_daily_above_70_on_rising_series():
    """Monotonically rising series produces RSI > 70."""
    close = rising_close(n=100)
    results = check_signals(close, date(2026, 5, 13))
    scan_types = [r["scan_type"] for r in results]
    assert "rsi_daily" in scan_types


def test_rsi_daily_not_triggered_on_flat():
    """Flat price series: RSI stays near 50, not above 70."""
    close = flat_close(n=100)
    results = check_signals(close, date(2026, 5, 13))
    scan_types = [r["scan_type"] for r in results]
    assert "rsi_daily" not in scan_types


# --- result structure ---

def test_result_keys():
    """Each result dict has required keys."""
    close = rising_close(n=300)
    results = check_signals(close, date(2026, 5, 13))
    assert len(results) > 0
    for r in results:
        assert set(r.keys()) == {"scan_type", "scan_date", "value", "is_new_signal", "week_ending"}


def test_only_hits_returned():
    """check_signals returns only in_scan=True results (no misses)."""
    close = flat_close(n=300)
    results = check_signals(close, date(2026, 5, 13))
    for r in results:
        assert r["scan_type"] in ["52w_high", "ath", "12w_high", "rsi_daily", "rsi_weekly", "rsi_monthly"]


# --- RSI new_entry ---

def test_rsi_daily_new_entry_false_on_continuous_rise():
    """Long rising series: RSI stays above 70 for many bars, so is_new_signal=0."""
    close = rising_close(n=200)
    results = check_signals(close, date(2026, 5, 13))
    hit = next((r for r in results if r["scan_type"] == "rsi_daily"), None)
    assert hit is not None, "expected rsi_daily in results"
    assert hit["is_new_signal"] == 0


def test_rsi_daily_new_entry_true_on_cross():
    """
    Alternating +2/-1 series keeps RSI ~67 (below 70).
    A final large up-day (+20) pushes RSI to ~84 -- a clean cross.
    Yesterday RSI <= 70, today RSI > 70 -> is_new_signal=1.
    """
    n = 100
    idx = pd.date_range(end="2026-05-13", periods=n, freq="B")
    prices = [50.0]
    for i in range(n - 2):
        prices.append(prices[-1] + (2.0 if i % 2 == 0 else -1.0))
    prices.append(prices[-1] + 20.0)
    close = pd.Series(prices, index=idx, name="Close")
    results = check_signals(close, date(2026, 5, 13))
    hit = next((r for r in results if r["scan_type"] == "rsi_daily"), None)
    assert hit is not None, "expected rsi_daily to fire after +20 spike on alternating base"
    assert hit["is_new_signal"] == 1
