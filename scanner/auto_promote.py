#!/usr/bin/env python3
"""
Auto-admit workbench tickers to prospects on fresh signal.

Gate: SCTR >= 90 + fresh signal today (is_new_signal=1)
      signal types: ATH, 52w high, 12w high, RSI-D, RSI-W, RSI-M

Tickers land in prospects (hunting ground), not locker_room directly.
Locker_room admission requires a 4EMA reclaim/clarity signal + operator's call.
Skips tickers already active in prospects or locker_room.
Regime flag: SPY+QQQ both below EMA4 → notes ⚠ REGIME (never blocks).

Called from run-post-close.sh (Step 10). Also runnable standalone with --dry-run.
"""
import json
import os
import sqlite3
import warnings
from datetime import date
from pathlib import Path

from locker._helpers import _log_history
from scanner import discord_router
from scanner.kb import capture as ob1_capture
from scanner.prospects import _insert_prospect

DB_PATH = Path(os.environ.get("BROAD_SCAN_DB", Path(__file__).parent.parent / "broad-scan.db"))

SIGNAL_CATEGORIES = {
    'ath':          'HIGH',
    '52w_high':     'HIGH',
    '12w_high':     'HIGH',
    'rsi_daily':    'RSI-D',
    'rsi_weekly':   'RSI-W',
    'rsi_monthly':  'RSI-M',
    'sc_workbench': 'SCTR',
}
HIGH_PRIORITY = ['ath', '52w_high', '12w_high']
DISPLAY_LABELS = {
    'ath':          'ATH',
    '52w_high':     '52wH',
    '12w_high':     '12wH',
    'rsi_daily':    'RSI-D',
    'rsi_weekly':   'RSI-W',
    'rsi_monthly':  'RSI-M',
    'sc_workbench': 'SCTR',
}
LOOKBACK_DAYS = 14


SCTR_FLOOR = 80.0


def get_candidates(conn, scan_date: str) -> dict:
    """
    Tickers with SCTR >= 90 AND a fresh signal today (is_new_signal=1).
    Returns {ticker: {'sctr': float, 'labels': list}}
    """
    rows = conn.execute(
        """SELECT s.ticker,
                  MAX(CASE WHEN s.scan_type='sc_workbench' THEN s.value END) as sctr,
                  GROUP_CONCAT(DISTINCT s.scan_type) as signal_types
           FROM scans s
           WHERE s.scan_date = ?
             AND s.scan_type IN ('ath','52w_high','12w_high','rsi_daily','rsi_weekly','rsi_monthly','sc_workbench')
           GROUP BY s.ticker
           HAVING sctr >= ?
             AND SUM(CASE WHEN s.is_new_signal=1
                           AND s.scan_type IN ('ath','52w_high','12w_high','rsi_daily','rsi_weekly','rsi_monthly')
                          THEN 1 ELSE 0 END) >= 1""",
        (scan_date, SCTR_FLOOR)
    ).fetchall()

    candidates = {}
    for ticker, sctr, signal_types_str in rows:
        if not sctr:
            continue
        sig_types = set(signal_types_str.split(',')) if signal_types_str else set()
        labels = [DISPLAY_LABELS[s] for s in ('ath','52w_high','12w_high','rsi_daily','rsi_weekly','rsi_monthly')
                  if s in sig_types]
        candidates[ticker] = {'sctr': round(sctr, 1), 'labels': labels}

    return candidates


def check_regime(conn, scan_date: str) -> bool:
    """Gate 4 flag: True if SPY and QQQ both below EMA4 today."""
    rows = conn.execute(
        "SELECT ticker, ema_4e_pct FROM eod_signals WHERE signal_date=? AND ticker IN ('SPY','QQQ')",
        (scan_date,)
    ).fetchall()
    data = {r[0]: r[1] for r in rows}
    return data.get('SPY', 1) < 0 and data.get('QQQ', 1) < 0


def already_active(conn, ticker: str) -> bool:
    """True if ticker already active in prospects or locker_room."""
    return bool(conn.execute(
        "SELECT 1 FROM prospects WHERE ticker=? AND status='active' "
        "UNION ALL "
        "SELECT 1 FROM locker_room WHERE ticker=? AND status='active' LIMIT 1",
        (ticker, ticker),
    ).fetchone())


def run(scan_date: str) -> dict:
    """
    Promote prospects with toby_status='reclaim' today into locker_room.
    Returns summary dict for Discord + KB reporting.
    """
    conn = sqlite3.connect(DB_PATH)
    try:
        candidates = get_candidates(conn, scan_date)
        regime_warn = check_regime(conn, scan_date)

        promoted = []
        skipped_active = []

        for ticker, signal_data in candidates.items():
            if already_active(conn, ticker):
                skipped_active.append(ticker)
                continue

            sctr = signal_data['sctr']
            notes = f"auto_promote SCTR={sctr:.0f} signals={'+'.join(signal_data['labels'])}"
            if regime_warn:
                notes += ' ⚠ REGIME'

            # Get close price from eod_signals (written by step 8 before this step 10)
            price_row = conn.execute(
                "SELECT price FROM eod_signals WHERE ticker=? AND signal_date=? AND price IS NOT NULL LIMIT 1",
                (ticker, scan_date),
            ).fetchone()
            add_price = float(price_row[0]) if (price_row is not None and price_row[0] is not None) else None

            _insert_prospect(conn, ticker, source='auto_promote', notes=notes, add_price=add_price)
            promoted.append({'ticker': ticker, 'labels': signal_data['labels'],
                             'sctr': sctr})

        conn.commit()

        # Log history after commit so audit rows only exist for committed inserts
        for p in promoted:
            _log_history(conn, p['ticker'], 'auto_promote',
                         detail={'trigger': 'signal', 'sctr': p['sctr'],
                                 'dest': 'prospects', 'regime_warn': regime_warn})
        conn.commit()
    finally:
        conn.close()

    return {
        'scan_date': scan_date,
        'candidates': len(candidates),
        'promoted': promoted,
        'skipped_active': skipped_active,
        'regime_warn': regime_warn,
    }


def build_discord_summary(result: dict) -> str:
    """Nightly summary — always fires, even if 0 admitted. Silence = pipeline down."""
    d = result['scan_date']
    n = len(result['promoted'])
    lines = [f"📋 **AUTO-ADMIT {d}** | {result['candidates']} candidates (SCTR≥90 + signal) → **{n} added to prospects**"]

    if result['promoted']:
        lines.append("```")
        for p in result['promoted']:
            sig = '+'.join(p['labels']) if p['labels'] else '--'
            lines.append(f"  {p['ticker']:<8}  SCTR {p['sctr']:.0f}  {sig}")
        lines.append("```")

    if result['skipped_active']:
        lines.append(f"↩ {len(result['skipped_active'])} already in prospects/locker — skipped")

    if result['regime_warn']:
        lines.append("⚠ REGIME: SPY+QQQ below EMA4 — size with caution")

    return "\n".join(lines)


if __name__ == "__main__":
    import argparse
    import sys

    parser = argparse.ArgumentParser(description="Auto-promote scan candidates to locker")
    parser.add_argument("--dry-run", action="store_true",
                        help="Print gate results without writing to DB")
    parser.add_argument("--date", default=str(date.today()),
                        help="Scan date YYYY-MM-DD (default: today)")
    args = parser.parse_args()

    if args.dry_run:
        conn = sqlite3.connect(DB_PATH)
        candidates = get_candidates(conn, args.date)
        regime_warn = check_regime(conn, args.date)
        print(f"DRY RUN {args.date}  |  {len(candidates)} candidates (SCTR>={SCTR_FLOOR:.0f} + fresh signal)  |  regime_warn={regime_warn}")
        print(f"{'Ticker':<8}  {'SCTR':<8}  {'Signals':<30}  Status")
        print("-" * 70)
        for ticker, data in sorted(candidates.items(), key=lambda x: -x[1]['sctr']):
            active = already_active(conn, ticker)
            sig = '+'.join(data['labels']) if data['labels'] else '--'
            status = "ACTIVE (skip)" if active else "ADMIT to prospects"
            print(f"{ticker:<8}  {data['sctr']:<8.1f}  {sig:<30}  {status}")
        conn.close()
        sys.exit(0)

    try:
        result = run(args.date)
        msg = build_discord_summary(result)
        discord_router.send_to("locker", msg)
        ob1_capture(
            f"auto_promote {args.date}: {result['candidates']} reclaim signals in prospects, "
            f"{len(result['promoted'])} promoted to locker. regime_warn={result['regime_warn']}"
        )
    except Exception as e:
        try:
            discord_router.send_to("locker", f"🔴 auto_promote FAILED {args.date}: {e}")
        except Exception:
            pass
        raise
