#!/usr/bin/env python3
"""Regime check — run weekly before the session. Use --discord to post on CHOP/BEAR."""

import argparse
import json
import os
import sqlite3
import urllib.request
from pathlib import Path

DB_PATH = Path(os.environ.get("BROAD_SCAN_DB", Path(__file__).parent.parent / "broad-scan.db"))

BULL = "BULL"
CHOP = "CHOP"
BEAR = "BEAR"

COLORS = {BULL: "\033[92m", CHOP: "\033[93m", BEAR: "\033[91m"}
EMOJI  = {BULL: "🟢", CHOP: "🟡", BEAR: "🔴"}
RESET  = "\033[0m"


def colorize(signal):
    return f"{COLORS[signal]}{signal}{RESET}"


def load_reminders_webhook() -> str:
    creds = Path.home() / ".config/credentials/discord-reminders.env"
    for line in creds.read_text().splitlines():
        if line.startswith("DISCORD_WEBHOOK_URL="):
            return line.split("=", 1)[1].strip()
    raise RuntimeError("DISCORD_WEBHOOK_URL not found in discord-reminders.env")


def post_discord(message: str) -> None:
    url = load_reminders_webhook()
    data = json.dumps({"content": message}).encode()
    req = urllib.request.Request(
        url, data=data,
        headers={"Content-Type": "application/json", "User-Agent": "regime-check/1.0"},
        method="POST",
    )
    try:
        urllib.request.urlopen(req, timeout=10)
    except Exception as e:
        print(f"[regime-check] discord post failed: {e}")


def condition_breadth(conn):
    """% of scan universe with positive weekly vol buzz. Proxy for above-50MA trend."""
    row = conn.execute("""
        SELECT
            COUNT(DISTINCT ticker) AS universe,
            COUNT(DISTINCT CASE WHEN buzz_w > 0 THEN ticker END) AS positive
        FROM eod_signals
        WHERE signal_date = (SELECT MAX(signal_date) FROM eod_signals)
    """).fetchone()
    universe, positive = row
    if not universe:
        return CHOP, 0.0, 0
    pct = round(100.0 * positive / universe, 1)
    if pct > 55:
        signal = BULL
    elif pct < 35:
        signal = BEAR
    else:
        signal = CHOP
    return signal, pct, universe


def condition_vol_buzz(conn):
    """% of positive buzz_d signals firing on green tape days — 14 calendar days (~10 trading days)."""
    row = conn.execute("""
        SELECT
            SUM(CASE WHEN buzz_d > 0 AND tape = 'green' THEN 1 ELSE 0 END),
            SUM(CASE WHEN buzz_d > 0 THEN 1 ELSE 0 END)
        FROM eod_signals
        WHERE signal_date >= date('now', '-14 days')
    """).fetchone()
    on_green, total = row
    if not total:
        return CHOP, 0.0, 0
    pct = round(100.0 * on_green / total, 1)
    if pct > 50:
        signal = BULL
    elif pct < 35:
        signal = BEAR
    else:
        signal = CHOP
    return signal, pct, total


def condition_locker(conn):
    """% of locker_room names with price < sma_50 (broken structure)."""
    row = conn.execute("""
        SELECT
            COUNT(*) AS total,
            COUNT(CASE WHEN sma_50 IS NOT NULL AND price < sma_50 THEN 1 END) AS broken
        FROM locker_room
        WHERE status IS NULL OR status != 'removed'
    """).fetchone()
    total, broken = row
    if not total:
        return CHOP, 0.0, 0
    pct = round(100.0 * broken / total, 1)
    # absolute floor: 3+ breaks always flags bear regardless of % (protects small locker)
    if pct > 25 or broken >= 3:
        signal = BEAR
    elif pct > 15:
        signal = CHOP
    else:
        signal = BULL
    return signal, pct, broken


def verdict(signals):
    # split [BULL, BEAR, CHOP] with no majority defaults to CHOP (no bias)
    bear_count = signals.count(BEAR)
    chop_count = signals.count(CHOP)
    if bear_count >= 2:
        return BEAR
    if chop_count >= 2:
        return CHOP
    if bear_count == 1 and chop_count == 1:
        return CHOP
    return BULL


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--discord", action="store_true", help="Post to Discord #reminders on CHOP or BEAR")
    args = ap.parse_args()

    with sqlite3.connect(DB_PATH) as conn:
        b_signal, b_pct, b_universe = condition_breadth(conn)
        v_signal, v_pct, v_total    = condition_vol_buzz(conn)
        l_signal, l_pct, l_broken   = condition_locker(conn)

    final = verdict([b_signal, v_signal, l_signal])

    print()
    print("  Regime Check")
    print("  " + "-" * 52)
    print(f"  Breadth      {b_pct:5.1f}% buzz_w positive ({b_universe} tickers)     {colorize(b_signal)}")
    print(f"  Vol-buzz     {v_pct:5.1f}% signals on green ({v_total} signals, 14d)   {colorize(v_signal)}")
    print(f"  Locker       {l_pct:5.1f}% broken vs 50MA ({l_broken} names)           {colorize(l_signal)}")
    print("  " + "-" * 52)
    print(f"  Verdict      2-of-3 rule  ->  {colorize(final)}")
    print()
    print("  NOTE: Thresholds provisional -- recalibrate after 4 weeks of data.")
    print()

    if args.discord:
        msg = (
            f"{EMOJI[final]} **regime: {final}** (2-of-3)\n"
            f"  breadth   {b_pct:.1f}% buzz_w positive  {EMOJI[b_signal]} {b_signal}\n"
            f"  vol-buzz  {v_pct:.1f}% signals on green  {EMOJI[v_signal]} {v_signal}\n"
            f"  locker    {l_pct:.1f}% broken vs 50MA ({l_broken} names)  {EMOJI[l_signal]} {l_signal}"
        )
        post_discord(msg)
        print("[regime-check] discord posted.")


if __name__ == "__main__":
    main()
