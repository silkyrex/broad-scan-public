"""
Tests for volume_buzz.score() using mocked yfinance.
No network calls; synthetic DataFrames drive all assertions.
"""
import pandas as pd
import pytest
from unittest.mock import patch, MagicMock

from scanner.volume_buzz import score, THRESHOLD, MA_DAILY


def _make_df(n: int = 120, base_vol: int = 1_000_000,
             today_multiplier: float = 1.0) -> pd.DataFrame:
    """Synthetic OHLCV frame: flat close, uniform volume, one-day spike at end."""
    idx = pd.date_range(end="2026-05-13", periods=n, freq="B")
    vols = [base_vol] * (n - 1) + [int(base_vol * today_multiplier)]
    closes = [50.0 + i * 0.01 for i in range(n)]
    return pd.DataFrame({"Volume": vols, "Close": closes, "High": closes,
                          "Low": closes, "Open": closes}, index=idx)


def _patch_ticker(df: pd.DataFrame):
    mock = MagicMock()
    mock.history.return_value = df
    return mock


# --- structure ---

@patch("scanner.volume_buzz.yf.Ticker")
def test_score_returns_required_keys(mock_cls):
    mock_cls.return_value = _patch_ticker(_make_df(n=120, today_multiplier=2.0))
    result = score("NVDA")
    required = {"ticker", "vol_buzz_pct", "buzz", "ud_ratio", "is_hve", "is_hv1",
                 "today_vol", "ma_vol", "avg_dol_vol", "ma_len"}
    assert required.issubset(result.keys())


@patch("scanner.volume_buzz.yf.Ticker")
def test_score_ticker_preserved(mock_cls):
    mock_cls.return_value = _patch_ticker(_make_df())
    result = score("AAPL")
    assert result["ticker"] == "AAPL"


# --- buzz threshold ---

@patch("scanner.volume_buzz.yf.Ticker")
def test_buzz_true_when_volume_2x_ma(mock_cls):
    mock_cls.return_value = _patch_ticker(_make_df(n=120, today_multiplier=2.0))
    result = score("X")
    assert result["buzz"] is True
    assert result["vol_buzz_pct"] > THRESHOLD


@patch("scanner.volume_buzz.yf.Ticker")
def test_buzz_false_when_volume_at_ma(mock_cls):
    mock_cls.return_value = _patch_ticker(_make_df(n=120, today_multiplier=1.0))
    result = score("X")
    assert result["buzz"] is False
    assert result["vol_buzz_pct"] == pytest.approx(0.0, abs=1.0)


@patch("scanner.volume_buzz.yf.Ticker")
def test_buzz_false_when_volume_slightly_above_ma(mock_cls):
    mock_cls.return_value = _patch_ticker(_make_df(n=120, today_multiplier=1.1))
    result = score("X")
    assert result["buzz"] is False


# --- edge cases ---

@patch("scanner.volume_buzz.yf.Ticker")
def test_insufficient_history_returns_error(mock_cls):
    mock_cls.return_value = _patch_ticker(_make_df(n=10))
    result = score("X")
    assert "error" in result
    assert result["buzz"] is False
    assert result["vol_buzz_pct"] is None


@patch("scanner.volume_buzz.yf.Ticker")
def test_empty_dataframe_returns_error(mock_cls):
    mock = MagicMock()
    mock.history.return_value = pd.DataFrame()
    mock_cls.return_value = mock
    result = score("X")
    assert result["buzz"] is False
    assert "error" in result


# --- vol_buzz_pct formula ---

@patch("scanner.volume_buzz.yf.Ticker")
def test_vol_buzz_pct_formula(mock_cls):
    """vol_buzz_pct = 100 * (today_vol / ma_vol) - 100."""
    mock_cls.return_value = _patch_ticker(_make_df(n=120, today_multiplier=1.5))
    result = score("X")
    # With 119 identical base_vol days and one 1.5x day:
    # ma ≈ base_vol * (118 + 1.5) / 50 ... exact value depends on rolling window
    # but the formula relationship should hold:
    if result["vol_buzz_pct"] is not None and result["ma_vol"]:
        expected = round(100 * (result["today_vol"] / result["ma_vol"]) - 100, 1)
        assert result["vol_buzz_pct"] == pytest.approx(expected, abs=0.2)
