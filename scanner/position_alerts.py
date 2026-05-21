"""
Intraday position management alerts — runs at 12:05 PM PT M-F.

Fires while market is still open so operator can submit MOC orders same session.
All signals require an open position in the positions table.

Alerts:
  RSI-D EXTENDED   daily RSI (live) crossed above 78   → trim 20%
  RSI-W EXTENDED   weekly RSI (live) crossed above 78  → trim 25%
  TIME TRIM        2-4 trading days held + RSI-D > 70  → trim 20%
  EXIT-D2 WARNING  day 2 below 4EMA intraday           → submit MOC to exit today

All deduped via locker_history (one alert per ticker per event per day).
TIME TRIM also deduped against rsi_d_extended to prevent double-trimming.
"""
from __future__ import annotations

import json
import os
import sqlite3
import warnings
from datetime import date, timedelta
from pathlib import Path

import pandas as pd
import yfinance as yf

from locker._helpers import _log_history
from scanner import discord_router
from scanner.kb import capture as ob1_capture

warnings.filterwarnings("ignore")

DB_PATH = Path(os.environ.get("BROAD_SCAN_DB", Path(__file__).parent.parent / "broad-scan.db"))

RSI_EXTEND_D = 78.0
RSI_EXTEND_W = 78.0
RSI_TRIM_MIN  = 70.0  # minimum RSI-D for time trim
TRIM_D_PCT    = 0.20
TRIM_W_PCT    = 0.25
TRIM_TIME_PCT = 0.20


def _rsi(series: pd.Series, length: int = 14) -> float | None:
    """Compute RSI from a price series. Returns latest value or None."""
    if series is None or len(series) < length + 2:
        return None
    delta  = series.diff()
    gain   = delta.clip(lower=0).ewm(com=length - 1, adjust=False).mean()
    loss   = (-delta.clip(upper=0)).ewm(com=length - 1, adjust=False).mean()
    if loss.iloc[-1] == 0:
        return 100.0
    rs = gain.iloc[-1] / loss.iloc[-1]
    return round(100 - 100 / (1 + rs), 2)


def _already_alerted(conn: sqlite3.Connection, ticker: str, today: str, event: str) -> bool:
    return conn.execute(
        "SELECT 1 FROM locker_history WHERE ticker=? AND event=? AND date=?",
        (ticker, event, today),
    ).fetchone() is not None


def _already_alerted_any(conn: sqlite3.Connection, ticker: str, today: str, events: tuple) -> bool:
    placeholders = ",".join("?" * len(events))
    return conn.execute(
        f"SELECT 1 FROM locker_history WHERE ticker=? AND event IN ({placeholders}) AND date=?",
        (ticker, *events, today),
    ).fetchone() is not None


def run(dry_run: bool = False) -> dict:
    today  = date.today().isoformat()
    prev   = (date.today() - timedelta(days=1)).isoformat()
    conn   = sqlite3.connect(DB_PATH)

    # Open positions with locker_room context
    positions = conn.execute(
        """SELECT p.ticker, p.shares, p.entry_price, p.entry_date
           FROM positions p
           JOIN locker_room l ON l.ticker = p.ticker AND l.status = 'active'
           WHERE p.status = 'open'"""
    ).fetchall()

    results: dict[str, list] = {
        "rsi_d_extended": [],
        "rsi_w_extended": [],
        "time_trim":      [],
        "exit_d2_warn":   [],
    }

    if not positions:
        conn.close()
        return {**results, "dry_run": dry_run}

    tickers = [p[0] for p in positions]

    # Batch fetch 90 days of daily data (enough for RSI14 to converge)
    data = yf.download(
        tickers, period="90d", interval="1d",
        progress=False, auto_adjust=True, group_by="ticker",
    )

    for ticker, shares, entry_price, entry_date in positions:
        try:
            closes = (
                data[ticker]["Close"].dropna()
                if len(tickers) > 1
                else data["Close"].dropna()
            )
        except Exception:
            continue

        if closes is None or len(closes) < 16:
            continue

        live_price = float(closes.iloc[-1])   # today's partial bar
        prev_close = float(closes.iloc[-2])   # yesterday close

        # Daily RSI: with and without today's bar (for cross detection)
        live_rsi_d = _rsi(closes)
        prev_rsi_d = _rsi(closes.iloc[:-1])

        # Weekly RSI: resample to weekly, same cross detection
        weekly = closes.resample("W-FRI").last().dropna()
        live_rsi_w = _rsi(weekly)
        prev_rsi_w = _rsi(weekly.iloc[:-1])

        # 4EMA: live and previous (for exit-d2 check)
        ema4      = closes.ewm(span=4, adjust=False).mean()
        curr_ema4 = float(ema4.iloc[-1])
        prev_ema4 = float(ema4.iloc[-2])

        # Trading days held (count eod_signals rows from entry to today)
        days_held = conn.execute(
            """SELECT COUNT(*) FROM eod_signals
               WHERE ticker=? AND signal_date >= ? AND signal_date <= ?""",
            (ticker, entry_date, today),
        ).fetchone()[0]

        # --- RSI-D EXTENDED ---
        if (live_rsi_d is not None and live_rsi_d > RSI_EXTEND_D and
                prev_rsi_d is not None and prev_rsi_d <= RSI_EXTEND_D and
                not _already_alerted(conn, ticker, today, "rsi_d_extended")):
            trim_shares = max(1, round(shares * TRIM_D_PCT))
            if not dry_run:
                _log_history(conn, ticker, "rsi_d_extended",
                             reason=f"RSI-D {live_rsi_d:.1f} crossed above {RSI_EXTEND_D} live",
                             detail={"rsi_d": live_rsi_d, "trim_pct": int(TRIM_D_PCT * 100),
                                     "trim_shares": int(trim_shares)})
            results["rsi_d_extended"].append({
                "ticker": ticker, "rsi_d": live_rsi_d,
                "shares": shares, "trim_shares": trim_shares,
            })

        # --- RSI-W EXTENDED ---
        if (live_rsi_w is not None and live_rsi_w > RSI_EXTEND_W and
                prev_rsi_w is not None and prev_rsi_w <= RSI_EXTEND_W and
                not _already_alerted(conn, ticker, today, "rsi_w_extended")):
            trim_shares = max(1, round(shares * TRIM_W_PCT))
            if not dry_run:
                _log_history(conn, ticker, "rsi_w_extended",
                             reason=f"RSI-W {live_rsi_w:.1f} crossed above {RSI_EXTEND_W} live",
                             detail={"rsi_w": live_rsi_w, "trim_pct": int(TRIM_W_PCT * 100),
                                     "trim_shares": int(trim_shares)})
            results["rsi_w_extended"].append({
                "ticker": ticker, "rsi_w": live_rsi_w,
                "shares": shares, "trim_shares": trim_shares,
            })

        # --- TIME TRIM: 2-4 trading days + RSI-D > 70 ---
        # Dedup: skip if rsi_d_extended already fired today (same 20% trim).
        if (2 <= days_held <= 4 and
                live_rsi_d is not None and live_rsi_d > RSI_TRIM_MIN and
                not _already_alerted_any(conn, ticker, today, ("time_trim", "rsi_d_extended"))):
            trim_shares = max(1, round(shares * TRIM_TIME_PCT))
            if not dry_run:
                _log_history(conn, ticker, "time_trim",
                             reason=f"{days_held} trading days held, RSI-D {live_rsi_d:.1f}",
                             detail={"days_held": days_held, "rsi_d": live_rsi_d,
                                     "trim_pct": int(TRIM_TIME_PCT * 100),
                                     "trim_shares": int(trim_shares)})
            results["time_trim"].append({
                "ticker": ticker, "days_held": days_held, "rsi_d": live_rsi_d,
                "shares": shares, "trim_shares": trim_shares,
            })

        # --- EXIT-D2 WARNING: day 2 below 4EMA intraday ---
        # Fires if current price is below 4EMA AND yesterday was also below.
        if (live_price < curr_ema4 and prev_close < prev_ema4 and
                not _already_alerted(conn, ticker, today, "exit_d2_warn")):
            ema_pct = round((live_price - curr_ema4) / curr_ema4 * 100, 1)
            if not dry_run:
                _log_history(conn, ticker, "exit_d2_warn",
                             reason="day 2 below 4EMA intraday — exit warning",
                             detail={"ema_pct": ema_pct, "live_price": round(live_price, 2)})
            results["exit_d2_warn"].append({
                "ticker": ticker, "ema_pct": ema_pct,
                "shares": shares, "live_price": live_price,
            })

    if not dry_run:
        conn.commit()
    conn.close()

    return {**results, "dry_run": dry_run}


def build_discord_messages(result: dict) -> list[str]:
    today = date.today().isoformat()
    messages = []

    for p in result.get("exit_d2_warn", []):
        messages.append(
            f"🔴 **EXIT-D2 {today}: {p['ticker']}** — day 2 below 4 EMA intraday "
            f"({p['ema_pct']:+.1f}%)\n"
            f"  Submit MOC to exit {p['shares']:.0f}sh today."
        )

    for p in result.get("rsi_w_extended", []):
        messages.append(
            f"📈 **RSI-W EXTENDED {today}: {p['ticker']}** — weekly RSI {p['rsi_w']:.1f} crossed 78\n"
            f"  Trim 25% → sell {p['trim_shares']:.0f}sh of {p['shares']:.0f}sh. Submit MOC today."
        )

    for p in result.get("rsi_d_extended", []):
        messages.append(
            f"📊 **RSI-D EXTENDED {today}: {p['ticker']}** — daily RSI {p['rsi_d']:.1f} crossed 78\n"
            f"  Trim 20% → sell {p['trim_shares']:.0f}sh of {p['shares']:.0f}sh. Submit MOC today."
        )

    for p in result.get("time_trim", []):
        messages.append(
            f"⏱ **TIME TRIM {today}: {p['ticker']}** — {p['days_held']} trading days held, "
            f"RSI-D {p['rsi_d']:.1f}\n"
            f"  Trim 20% → sell {p['trim_shares']:.0f}sh of {p['shares']:.0f}sh. Submit MOC today."
        )

    return messages


def main() -> None:
    import argparse
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()

    result = run(dry_run=args.dry_run)
    msgs   = build_discord_messages(result)

    for msg in msgs:
        print(msg)

    if not args.dry_run and msgs:
        for msg in msgs:
            discord_router.send_to("trading", msg)
        counts = {k: len(v) for k, v in result.items() if isinstance(v, list)}
        ob1_capture(f"position_alerts {date.today().isoformat()}: {counts}")

    if not msgs:
        print("No position alerts today.")


if __name__ == "__main__":
    main()
