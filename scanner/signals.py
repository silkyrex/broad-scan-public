import pandas as pd
import pandas_ta as ta
from datetime import date, timedelta


SCAN_TYPES = ["52w_high", "ath", "12w_high", "rsi_daily", "rsi_weekly", "rsi_monthly"]


def week_ending(d: date) -> date:
    """Return the Saturday of the week containing d."""
    return d + timedelta(days=(5 - d.weekday()) % 7)


def _is_new_high(close: pd.Series, window: int) -> tuple[bool, bool]:
    """
    Returns (in_scan, is_new_signal).
    in_scan: today's close >= max of previous `window` closes.
    is_new_signal: in_scan today but not yesterday.
    Requires at least window+2 rows.
    """
    if len(close) < window + 2:
        return False, False
    today = close.iloc[-1]
    yesterday = close.iloc[-2]
    today_window_max = close.iloc[-(window + 1):-1].max()
    yesterday_window_max = close.iloc[-(window + 2):-2].max()
    in_scan = today > today_window_max
    was_in_scan = yesterday > yesterday_window_max
    return in_scan, (in_scan and not was_in_scan)


def _rsi_signal(close: pd.Series, resample: str | None = None) -> tuple[bool, bool, float]:
    """
    Returns (in_scan, is_new_signal, rsi_value).
    resample: None=daily, 'W-FRI'=weekly, 'ME'=monthly.
    """
    series = close if resample is None else close.resample(resample).last().dropna()
    if len(series) < 30:
        return False, False, float("nan")
    rsi = ta.rsi(series, length=14)
    if rsi is None or len(rsi.dropna()) < 2:
        return False, False, float("nan")
    rsi = rsi.dropna()
    current = float(rsi.iloc[-1])
    previous = float(rsi.iloc[-2])
    in_scan = current > 70
    is_new_signal = in_scan and previous <= 70
    return in_scan, is_new_signal, round(current, 2)


def check_signals(close: pd.Series, scan_date: date) -> list[dict]:
    """
    Run all 6 scans against a Close price series.
    Returns a list of result dicts for scan hits only (in_scan=True).
    Each dict: {scan_type, scan_date, value, is_new_signal, week_ending}
    """
    we = week_ending(scan_date)
    results = []

    checks = [
        ("52w_high", lambda: (*_is_new_high(close, 252), float(close.iloc[-1]))),
        ("ath",      lambda: (*_is_new_high(close, len(close) - 2), float(close.iloc[-1]))),
        ("12w_high", lambda: (*_is_new_high(close, 60), float(close.iloc[-1]))),
        ("rsi_daily",   lambda: _rsi_signal(close, None)),
        ("rsi_weekly",  lambda: _rsi_signal(close, "W-FRI")),
        ("rsi_monthly", lambda: _rsi_signal(close, "ME")),
    ]

    for scan_type, fn in checks:
        in_scan, is_new_signal, value = fn()
        if in_scan:
            results.append({
                "scan_type": scan_type,
                "scan_date": scan_date.isoformat(),
                "value": value,
                "is_new_signal": int(is_new_signal),
                "week_ending": we.isoformat(),
            })

    return results
