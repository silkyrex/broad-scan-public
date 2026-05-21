"""
Tests for theme_buzz.score() and scan().
score() accepts theme_map directly -- no yfinance or ETF cache needed.
"""
import pandas as pd
import pytest

from scanner.theme_buzz import score, scan


def _map(ticker: str, etfs: list[str], avg_momentum: float) -> dict:
    """Minimal synthetic theme_map entry."""
    return {
        ticker.upper(): {
            "themes":        ["ai_robotics"],
            "etfs":          etfs,
            "etf_count":     len(etfs),
            "holdings":      {e: 5.0 for e in etfs},
            "ret_1mo_by_etf": {e: avg_momentum for e in etfs},
            "avg_momentum":  avg_momentum,
        }
    }


# --- score: in-theme tickers ---

def test_score_buzz_true_when_momentum_positive():
    result = score("NVDA", theme_map=_map("NVDA", ["BOTZ"], avg_momentum=3.0))
    assert result["buzz"] is True
    assert result["in_theme"] is True
    assert result["etf_count"] == 1
    assert result["avg_momentum"] == pytest.approx(3.0)


def test_score_buzz_false_when_momentum_zero():
    result = score("NVDA", theme_map=_map("NVDA", ["BOTZ"], avg_momentum=0.0))
    assert result["buzz"] is False
    assert result["in_theme"] is True


def test_score_buzz_false_when_momentum_negative():
    result = score("NVDA", theme_map=_map("NVDA", ["BOTZ"], avg_momentum=-2.0))
    assert result["buzz"] is False


def test_score_theme_score_formula():
    """theme_score = etf_count + avg_momentum / 10."""
    result = score("NVDA", theme_map=_map("NVDA", ["BOTZ", "ROBO"], avg_momentum=5.0))
    expected = 2 + 5.0 / 10
    assert result["theme_score"] == pytest.approx(expected)


def test_score_multiple_etfs_counted():
    result = score("PLTR", theme_map=_map("PLTR", ["HACK", "ITA", "XAR"], avg_momentum=1.0))
    assert result["etf_count"] == 3


# --- score: ticker not in theme map ---

def test_score_not_in_theme_returns_defaults():
    result = score("FAKE", theme_map={})
    assert result["buzz"] is False
    assert result["in_theme"] is False
    assert result["theme_score"] == 0.0
    assert result["etf_count"] == 0
    assert result["themes"] == []


def test_score_case_insensitive():
    tm = _map("NVDA", ["BOTZ"], avg_momentum=2.0)
    assert score("nvda", theme_map=tm)["in_theme"] is True
    assert score("NVDA", theme_map=tm)["in_theme"] is True


# --- scan: batch ---

def test_scan_returns_dataframe():
    tm = {**_map("NVDA", ["BOTZ"], 3.0), **_map("PLTR", ["HACK"], -1.0)}
    df = scan(["NVDA", "PLTR"], theme_map=tm)
    assert isinstance(df, pd.DataFrame)
    assert set(df["ticker"]) == {"NVDA", "PLTR"}


def test_scan_sorted_by_theme_score_descending():
    tm = {**_map("NVDA", ["BOTZ", "ROBO"], 5.0), **_map("AMD", ["BOTZ"], 1.0)}
    df = scan(["AMD", "NVDA"], theme_map=tm)
    assert df.iloc[0]["ticker"] == "NVDA"


def test_scan_unknown_ticker_included_with_zero_score():
    tm = _map("NVDA", ["BOTZ"], 3.0)
    df = scan(["NVDA", "UNKNOWN"], theme_map=tm)
    unknown_row = df[df["ticker"] == "UNKNOWN"]
    assert len(unknown_row) == 1
    assert not unknown_row.iloc[0]["buzz"]
