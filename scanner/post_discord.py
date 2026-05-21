#!/usr/bin/env python3
"""
Post today's new scan entries to Discord.
Routes to #broad-scan channel (morning scan cockpit).
Reads DISCORD_SCANS_WEBHOOK from env.
Exits 0 silently if no hits or not a weekday.
"""
import os
import sqlite3
from datetime import date
from pathlib import Path

from . import discord_router
from .kb import capture as ob1_capture

DB_PATH = Path(os.environ.get("BROAD_SCAN_DB", Path(__file__).parent.parent / "broad-scan.db"))
WEBHOOK = os.environ.get("DISCORD_SCANS_WEBHOOK", "")

HIGH_SIGNALS = {"52w_high", "ath", "12w_high"}
YFINANCE_SIGNALS = {"rsi_daily", "rsi_weekly", "ath"}


def fetch_hits(scan_date: str) -> dict[str, dict]:
    con = sqlite3.connect(DB_PATH)
    # RSI signals: cross-only (fired when previous <= 70, current > 70)
    # High signals: every day the ticker is at a new high, not just first breakout
    rows = con.execute(
        """
        SELECT ticker, scan_type, value FROM scans
        WHERE scan_date=? AND is_new_signal=1
          AND scan_type IN ('rsi_daily','rsi_weekly','rsi_monthly')
        UNION
        SELECT ticker, scan_type, value FROM scans
        WHERE scan_date=? AND scan_type IN ('52w_high','ath','12w_high')
        """,
        (scan_date, scan_date),
    ).fetchall()
    con.close()
    hits: dict[str, dict] = {}
    for ticker, scan_type, value in rows:
        hits.setdefault(ticker, {})[scan_type] = value
    return hits


def fetch_sctr(tickers: list, scan_date: str) -> dict:
    if not tickers:
        return {}
    placeholders = ",".join("?" * len(tickers))
    con = sqlite3.connect(DB_PATH)
    rows = con.execute(
        f"SELECT ticker, value FROM scans "
        f"WHERE scan_type='sc_workbench' AND scan_date=? AND ticker IN ({placeholders})",
        [scan_date, *tickers],
    ).fetchall()
    con.close()
    return {t: v for t, v in rows if v is not None}


def fetch_high_new(tickers: list, scan_date: str) -> set[str]:
    """Tickers where at least one high signal is a first-day breakout today."""
    if not tickers:
        return set()
    placeholders = ",".join("?" * len(tickers))
    con = sqlite3.connect(DB_PATH)
    rows = con.execute(
        f"SELECT DISTINCT ticker FROM scans "
        f"WHERE scan_date=? AND is_new_signal=1 "
        f"AND scan_type IN ('52w_high','ath','12w_high') "
        f"AND ticker IN ({placeholders})",
        [scan_date, *tickers],
    ).fetchall()
    con.close()
    return {r[0] for r in rows}


def fetch_streaks(tickers: list, scan_date: str) -> dict[str, int]:
    """Days in the past 14 days each ticker had any high signal."""
    if not tickers:
        return {}
    placeholders = ",".join("?" * len(tickers))
    con = sqlite3.connect(DB_PATH)
    rows = con.execute(
        f"SELECT ticker, COUNT(DISTINCT scan_date) FROM scans "
        f"WHERE scan_type IN ('52w_high','ath','12w_high') "
        f"AND scan_date <= ? AND scan_date > date(?, '-14 days') "
        f"AND ticker IN ({placeholders}) "
        f"GROUP BY ticker",
        [scan_date, scan_date, *tickers],
    ).fetchall()
    con.close()
    return dict(rows)


def build_message(
    hits: dict, sctr: dict, scan_date: str,
    high_new: set[str] | None = None,
    streaks: dict[str, int] | None = None,
) -> str:
    if not hits:
        return ""
    tickers = [t for t in hits if YFINANCE_SIGNALS & hits[t].keys()]
    if not tickers:
        return ""
    tickers.sort(key=lambda t: (-(len(YFINANCE_SIGNALS & hits[t].keys())), -(sctr.get(t) or 0)))
    high_new = high_new or set()
    streaks = streaks or {}
    n = len(tickers)
    header = f"**Scan {scan_date}** | {n} name{'s' if n != 1 else ''} to evaluate"
    lines = ["```", f"{'Ticker':<8}  RSI-D  RSI-W  ATH   SCTR   Flag"]
    lines.append("-" * 46)
    for t in tickers:
        sigs = hits[t]
        d = "  x  " if "rsi_daily"  in sigs else "     "
        w = "  x  " if "rsi_weekly" in sigs else "     "
        a = " x  " if "ath"        in sigs else "    "
        s = f"{sctr[t]:>6.1f}" if t in sctr else "    --"
        has_high = bool(HIGH_SIGNALS & sigs.keys())
        if has_high:
            flag = "NEW" if t in high_new else f"{streaks.get(t, 1)}d"
        else:
            flag = ""
        lines.append(f"{t:<8}  {d}  {w}  {a}  {s}  {flag}")
    lines.append("```")
    return header + "\n" + "\n".join(lines)


def build_ob1_content(hits: dict, sctr: dict, scan_date: str) -> str:
    tickers = [t for t in hits if YFINANCE_SIGNALS & hits[t].keys()]
    if not tickers:
        return ""
    tickers.sort(key=lambda t: (-(len(YFINANCE_SIGNALS & hits[t].keys())), -(sctr.get(t) or 0)))

    ath   = [t for t in tickers if "ath" in hits[t]]
    rsi_d = [t for t in tickers if "rsi_daily" in hits[t]]
    rsi_w = [t for t in tickers if "rsi_weekly" in hits[t]]
    top_sctr = sorted([(t, sctr[t]) for t in tickers if t in sctr], key=lambda x: -x[1])[:5]
    sctr_str = ", ".join(f"{t} {v:.0f}" for t, v in top_sctr)

    lines = [
        f"Broad scan [{scan_date}]: {len(tickers)} tickers hit momentum signals.",
        f"ATH: {', '.join(ath) or 'none'}",
        f"RSI Daily >70: {', '.join(rsi_d) or 'none'}",
        f"RSI Weekly >70: {', '.join(rsi_w) or 'none'}",
        f"Top SCTR: {sctr_str or 'n/a'}",
        "Source: StockCharts workbench + email alerts. Agent: broad-scan.",
    ]
    return "\n".join(lines)


def post(message: str) -> None:
    discord_router.send_to("broad-scan", message)


if __name__ == "__main__":
    if date.today().weekday() >= 5:
        raise SystemExit(0)
    scan_date = os.environ.get("SCAN_DATE", str(date.today()))
    hits = fetch_hits(scan_date)
    if not hits:
        raise SystemExit(0)
    sctr = fetch_sctr(list(hits.keys()), scan_date)
    high_tickers = [t for t in hits if HIGH_SIGNALS & hits[t].keys()]
    high_new = fetch_high_new(high_tickers, scan_date)
    streaks = fetch_streaks(high_tickers, scan_date)
    msg = build_message(hits, sctr, scan_date, high_new, streaks)
    post(msg)
    thought = build_ob1_content(hits, sctr, scan_date)
    if thought:
        ob1_capture(thought)
