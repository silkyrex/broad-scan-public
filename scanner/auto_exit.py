"""
Post-close: automated locker exit handling + DB maintenance.

Three events confirmed at close (Step 11 in run-post-close.sh):

  EXIT-D2:  toby_status='exit-d2' → auto-drop all active locker tickers + recycle
  EXIT-D1:  toby_status='exit-d1' → warn positions only, keep in locker
  CLARITY:  yesterday='exit-d1' AND today='reclaim' → flag + Discord ping

Trim alerts (RSI-D/W extended, time trim, exit-d2 warning) live in
scanner/position_alerts.py which runs at noon so operator can submit
MOC the same session.
"""
from __future__ import annotations

import argparse
import json
import os
import sqlite3
import sys
from datetime import date, timedelta
from pathlib import Path

from locker._helpers import _log_history
from scanner import discord_router
from scanner.kb import capture as ob1_capture

DB_PATH = Path(os.environ.get("BROAD_SCAN_DB", Path(__file__).parent.parent / "broad-scan.db"))


def _drop_ticker(conn, ticker: str, scan_date: str) -> None:
    conn.execute(
        "UPDATE locker_room SET status='removed', removed_date=? WHERE ticker=? AND status='active'",
        (scan_date, ticker),
    )
    _log_history(conn, ticker, "auto_drop",
                 reason="exit-d2: closed below 4 EMA two consecutive days",
                 detail={"trigger": "exit-d2", "date": scan_date})


def _recycle_to_prospects(conn, ticker: str, scan_date: str) -> None:
    """Re-add a dropped locker ticker to prospects so it stays on the hunting radar."""
    note = f"recycled from locker exit-d2 {scan_date}"

    # Already active in prospects -- nothing to do
    if conn.execute(
        "SELECT 1 FROM prospects WHERE ticker=? AND status='active'", (ticker,)
    ).fetchone():
        return

    # Reactivate an existing dropped row
    existing = conn.execute(
        "SELECT id, notes FROM prospects WHERE ticker=? ORDER BY added_date DESC LIMIT 1",
        (ticker,),
    ).fetchone()
    if existing:
        pid, old_notes = existing
        new_notes = f"{old_notes}\n{note}".strip() if old_notes else note
        conn.execute(
            "UPDATE prospects SET status='active', notes=? WHERE id=?",
            (new_notes, pid),
        )
    else:
        conn.execute(
            """INSERT INTO prospects (ticker, added_date, source_scan, notes, status, bucket)
               VALUES (?, ?, 'locker_recycle', ?, 'active', 'prospect')""",
            (ticker, scan_date, note),
        )

    _log_history(conn, ticker, "recycled_to_prospect",
                 reason="recycled to prospects after exit-d2 drop",
                 detail={"trigger": "exit-d2", "date": scan_date})


def _set_exit_d1(conn, ticker: str, scan_date: str) -> None:
    conn.execute(
        "UPDATE locker_room SET ema4_status='exit-d1', ema4_updated=? WHERE ticker=? AND status='active'",
        (scan_date, ticker),
    )
    _log_history(conn, ticker, "exit_d1_warn",
                 reason="exit-d1: closed below 4 EMA day 1 — watching day 2",
                 detail={"trigger": "exit-d1", "date": scan_date})


def _set_clarity(conn, ticker: str, scan_date: str) -> None:
    conn.execute(
        "UPDATE locker_room SET ema4_status='clarity', ema4_updated=? WHERE ticker=? AND status='active'",
        (scan_date, ticker),
    )
    _log_history(conn, ticker, "clarity",
                 reason="CLARITY: reclaimed 4 EMA after exit-d1 — awaiting operator confirmation",
                 detail={"trigger": "clarity", "date": scan_date})


def run(scan_date: str, dry_run: bool = False) -> dict:
    conn = sqlite3.connect(DB_PATH)
    prev_date = (date.fromisoformat(scan_date) - timedelta(days=1)).isoformat()

    try:
        # --- EXIT-D2: auto-drop all active locker tickers ---
        d2_rows = conn.execute(
            """SELECT e.ticker, e.ema_4e_pct FROM eod_signals e
               JOIN locker_room l ON l.ticker = e.ticker AND l.status = 'active'
               WHERE e.signal_date=? AND e.toby_status='exit-d2'""",
            (scan_date,),
        ).fetchall()

        dropped = []
        for ticker, ema_pct in d2_rows:
            if not dry_run:
                _drop_ticker(conn, ticker, scan_date)
                _recycle_to_prospects(conn, ticker, scan_date)
            dropped.append({"ticker": ticker, "ema_pct": ema_pct or 0.0})

        # --- EXIT-D1: warn positions only ---
        # Watchlist names below EMA4 day 1 = just watching, no action needed.
        d1_rows = conn.execute(
            """SELECT e.ticker, e.ema_4e_pct FROM eod_signals e
               JOIN locker_room l ON l.ticker = e.ticker AND l.status = 'active'
               WHERE e.signal_date=? AND e.toby_status='exit-d1'""",
            (scan_date,),
        ).fetchall()

        warned = []
        for ticker, ema_pct in d1_rows:
            has_position = conn.execute(
                "SELECT 1 FROM positions WHERE ticker=? AND status='open'", (ticker,)
            ).fetchone()
            if has_position:
                if not dry_run:
                    _set_exit_d1(conn, ticker, scan_date)
                warned.append({"ticker": ticker, "ema_pct": ema_pct or 0.0})

        # --- CLARITY: exit-d1 yesterday + reclaim today ---
        clarity_rows = conn.execute(
            """SELECT t.ticker, t.ema_4e_pct FROM eod_signals t
               JOIN eod_signals y ON y.ticker = t.ticker AND y.signal_date = ?
               JOIN locker_room l ON l.ticker = t.ticker AND l.status = 'active'
               WHERE t.signal_date=? AND t.toby_status='reclaim' AND y.toby_status='exit-d1'""",
            (prev_date, scan_date),
        ).fetchall()

        clarity = []
        for ticker, ema_pct in clarity_rows:
            if not dry_run:
                _set_clarity(conn, ticker, scan_date)
            clarity.append({"ticker": ticker, "ema_pct": ema_pct or 0.0})

        if not dry_run:
            conn.commit()

    finally:
        conn.close()

    return {
        "scan_date": scan_date,
        "dropped": dropped,
        "warned": warned,
        "clarity": clarity,
        "dry_run": dry_run,
    }


def build_discord_messages(result: dict) -> list[str]:
    messages = []
    d = result["scan_date"]

    if result["dropped"]:
        lines = [f"🔴 **AUTO-DROP {d}** | {len(result['dropped'])} ticker(s) — Day 2 below 4 EMA"]
        lines.append("```")
        for p in result["dropped"]:
            pct = f"{p['ema_pct']:+.1f}%"
            lines.append(f"  {p['ticker']:<8}  {pct} from EMA4")
        lines.append("```")
        messages.append("\n".join(lines))

    if result["warned"]:
        parts = []
        for p in result["warned"]:
            parts.append(f"{p['ticker']} ({p['ema_pct']:+.1f}%)")
        messages.append(f"⚠️ **EXIT-D1 {d}**: {', '.join(parts)} — watching day 2")

    for p in result["clarity"]:
        messages.append(
            f"✨ **CLARITY {d}: {p['ticker']}** — reclaimed 4 EMA after Day 1 dip "
            f"({p['ema_pct']:+.1f}%). Confirm hold?"
        )

    return messages


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dry-run", action="store_true", help="Print without writing to DB")
    parser.add_argument("--date", default=str(date.today()), help="Scan date YYYY-MM-DD")
    args = parser.parse_args()

    print(f"auto_exit {args.date}  dry_run={args.dry_run}")

    try:
        result = run(args.date, dry_run=args.dry_run)

        msgs = build_discord_messages(result)
        for msg in msgs:
            print(msg)

        if not args.dry_run:
            for msg in msgs:
                discord_router.send_to("locker", msg)

            summary = (
                f"auto_exit {args.date}: "
                f"dropped={len(result['dropped'])} "
                f"warned={len(result['warned'])} "
                f"clarity={len(result['clarity'])}"
            )
            ob1_capture(summary)
            print(f"\n{summary}")

        if not msgs:
            print(f"  No exit events today ({args.date})")

    except Exception as e:
        err = f"🔴 auto_exit FAILED {args.date}: {e}"
        print(err, file=sys.stderr)
        try:
            discord_router.send_to("locker", err)
        except Exception:
            pass
        raise
