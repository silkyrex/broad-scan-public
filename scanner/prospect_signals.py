"""Intraday reclaim/clarity scan for prospect tickers.

Runs during market hours to detect when a prospect crosses back above
the 4 EMA intraday. Fires once per ticker per day.

Two signals:
  RECLAIM  -- yesterday closed below 4 EMA, today live price above (2+ day dip)
  CLARITY  -- day-2 above 4 EMA, yesterday below, today above (one-day dip only)

Uses Option A: live price compared against yesterday's 4 EMA value.
Posts to Discord #locker. One alert per ticker per day (gated via locker_history).
"""
from __future__ import annotations

import argparse
import json
import os
import sqlite3
import warnings
from datetime import date
from pathlib import Path

import yfinance as yf

from scanner import discord_router
from scanner.kb import capture as ob1_capture

warnings.filterwarnings("ignore")

DB_PATH = Path(os.environ.get("BROAD_SCAN_DB", Path(__file__).parent.parent / "broad-scan.db"))


def _analyze(closes):
    """
    Returns (status, live_price, ema4_yesterday) or (None, 0.0, 0.0) if no signal.

    status is 'reclaim' or 'clarity':
      reclaim -- yesterday below 4 EMA, today live above (multi-day dip)
      clarity -- day-2 above, yesterday below, today live above (one-day dip)

    Uses Option A: live_price vs yesterday's 4 EMA value.
    closes: pd.Series of daily closes; last row is today's partial bar (live price).
    """
    if closes is None or len(closes) < 5:
        return None, 0.0, 0.0

    # Exclude today's partial bar from EMA calculation so ema4_yest is a clean
    # historical value uncontaminated by intraday price movement (Option A).
    ema4_hist   = closes.iloc[:-1].ewm(span=4, adjust=False).mean()
    live_price  = float(closes.iloc[-1])      # today's partial bar close
    prev_close  = float(closes.iloc[-2])      # yesterday close
    prev2_close = float(closes.iloc[-3])      # day-2 close
    ema4_yest = float(ema4_hist.iloc[-1])   # yesterday's clean 4 EMA (Option A ref)
    ema4_d2   = float(ema4_hist.iloc[-2])   # 4 EMA two bars ago (day-2)

    # No signal: not above yesterday's EMA, or was already above (no cross)
    if live_price <= ema4_yest or prev_close >= ema4_yest:
        return None, 0.0, 0.0

    # Was below yesterday, above today -- clarity if day-2 was above (one-day dip)
    if prev2_close > ema4_d2:
        return "clarity", live_price, ema4_yest

    return "reclaim", live_price, ema4_yest


def _already_alerted(conn: sqlite3.Connection, ticker: str, today: str) -> bool:
    return conn.execute(
        "SELECT 1 FROM locker_history "
        "WHERE ticker=? AND event IN ('prospect_reclaim','prospect_clarity') AND date=?",
        (ticker, today),
    ).fetchone() is not None


def run(dry_run: bool = False) -> dict:
    today = date.today().isoformat()
    conn = sqlite3.connect(DB_PATH)

    tickers = [r[0] for r in conn.execute(
        "SELECT ticker FROM prospects WHERE status='active' AND ticker != 'TEST'"
    )]

    reclaims: list[dict] = []
    clarities: list[dict] = []

    if not tickers:
        conn.close()
        return {"reclaims": reclaims, "clarities": clarities, "dry_run": dry_run}

    data = yf.download(
        tickers, period="20d", interval="1d",
        progress=False, auto_adjust=True, group_by="ticker",
    )

    for ticker in tickers:
        if _already_alerted(conn, ticker, today):
            continue

        try:
            closes = (
                data[ticker]["Close"].dropna()
                if len(tickers) > 1
                else data["Close"].dropna()
            )
        except Exception:
            continue

        status, live_price, ema4_yest = _analyze(closes)
        if status is None:
            continue

        pct = (live_price - ema4_yest) / ema4_yest * 100 if ema4_yest else 0.0
        entry = {"ticker": ticker, "price": live_price, "ema4": ema4_yest, "pct": pct}

        if not dry_run:
            conn.execute(
                "INSERT INTO locker_history (ticker, event, date, reason, detail) "
                "VALUES (?, ?, ?, ?, ?)",
                (
                    ticker,
                    f"prospect_{status}",
                    today,
                    f"prospect {status}: 4 EMA signal intraday",
                    json.dumps({"live_price": round(live_price, 2), "ema4": round(ema4_yest, 2)}),
                ),
            )

        if status == "clarity":
            clarities.append(entry)
        else:
            reclaims.append(entry)

    if not dry_run:
        conn.commit()
    conn.close()

    return {"reclaims": reclaims, "clarities": clarities, "dry_run": dry_run}


def build_discord_messages(result: dict) -> list[str]:
    today = date.today().isoformat()
    messages = []

    if result["clarities"]:
        lines = [
            f"✨ **CLARITY {today}** | {len(result['clarities'])} prospect(s) — reclaimed 4 EMA after 1-day dip. Promote?",
            "```",
        ]
        for p in result["clarities"]:
            lines.append(f"  {p['ticker']:<8}  ${p['price']:.2f}  {p['pct']:+.1f}% vs EMA4")
        lines.append("```")
        messages.append("\n".join(lines))

    if result["reclaims"]:
        lines = [
            f"★ **RECLAIM {today}** | {len(result['reclaims'])} prospect(s) crossed above 4 EMA",
            "```",
        ]
        for p in result["reclaims"]:
            lines.append(f"  {p['ticker']:<8}  ${p['price']:.2f}  {p['pct']:+.1f}% vs EMA4")
        lines.append("```")
        messages.append("\n".join(lines))

    return messages


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()

    result = run(dry_run=args.dry_run)
    msgs = build_discord_messages(result)

    for msg in msgs:
        print(msg)

    if not args.dry_run and (result["reclaims"] or result["clarities"]):
        for msg in msgs:
            discord_router.send_to("locker", msg)
        tickers = [p["ticker"] for p in result["clarities"] + result["reclaims"]]
        ob1_capture(
            f"prospect_signals {date.today().isoformat()}: "
            f"reclaims={[p['ticker'] for p in result['reclaims']]} "
            f"clarities={[p['ticker'] for p in result['clarities']]}"
        )

    if not msgs:
        print("No prospect reclaim/clarity signals.")


if __name__ == "__main__":
    main()
