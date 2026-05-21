"""Pre-open prospect brief -- reclaim and clarity candidates for today.

Queries yesterday's EOD signals for active prospects and flags:
  CLARITY CANDIDATE  -- above 4 EMA two days ago, below yesterday (one-day dip)
  RECLAIM CANDIDATE  -- below 4 EMA at last close (multi-day below)

Clarity candidates are shown first and flagged as mandatory exits if they
don't reclaim today. Reclaim candidates are everything else below EMA4,
sorted closest-to-reclaim first.

Runs at 6:35 AM PT (appended to run.sh after post_discord). Posts to
Discord #locker. Silent if zero candidates.
"""
from __future__ import annotations

import argparse
import os
import sqlite3
from datetime import date, timedelta
from pathlib import Path

from scanner import discord_router


def _prev_business_day(d: date) -> date:
    """Last weekday before d. Handles Monday -> Friday correctly."""
    prev = d - timedelta(days=1)
    while prev.weekday() >= 5:
        prev -= timedelta(days=1)
    return prev

DB_PATH = Path(os.environ.get("BROAD_SCAN_DB", Path(__file__).parent.parent / "broad-scan.db"))


def query_candidates(conn: sqlite3.Connection, scan_date: str) -> dict:
    # Use last business day so Monday runs correctly find Friday's eod_signals row.
    # Note: market holidays are not handled -- a post-holiday Monday uses Friday correctly
    # but a post-holiday Tuesday would use Monday which may have no eod_signals row.
    prev_date = _prev_business_day(date.fromisoformat(scan_date)).isoformat()

    # Single query: all prospects below EMA4 at scan_date, with optional prev-day join.
    # y.ema_4e_pct > 0 means was above EMA4 last business day (one-day dip = clarity).
    # y.ema_4e_pct IS NULL or <= 0 means multi-day below or no prev row (reclaim).
    rows = conn.execute(
        """SELECT e.ticker, e.ema_4e_pct,
                  CASE WHEN y.ema_4e_pct > 0 THEN 'clarity' ELSE 'reclaim' END AS signal
           FROM eod_signals e
           JOIN prospects p   ON p.ticker = e.ticker AND p.status = 'active'
           LEFT JOIN eod_signals y ON y.ticker = e.ticker AND y.signal_date = ?
           WHERE e.signal_date = ? AND e.ema_4e_pct < 0
           ORDER BY signal, e.ema_4e_pct DESC""",
        (prev_date, scan_date),
    ).fetchall()

    clarities = [{"ticker": r[0], "pct": r[1]} for r in rows if r[2] == "clarity"]
    reclaims  = [{"ticker": r[0], "pct": r[1]} for r in rows if r[2] == "reclaim"]

    return {"clarities": clarities, "reclaims": reclaims, "scan_date": scan_date}


def build_message(result: dict) -> str | None:
    if not result["clarities"] and not result["reclaims"]:
        return None

    d = result["scan_date"]
    lines = [f"**Prospect Watch -- {d}**"]

    if result["clarities"]:
        lines.append(
            f"\n✨ **CLARITY WATCH** ({len(result['clarities'])}) "
            f"-- one-day dip, must reclaim today or exit"
        )
        lines.append("```")
        for r in result["clarities"]:
            lines.append(f"  {r['ticker']:<8}  {r['pct']:+.1f}% vs EMA4")
        lines.append("```")

    if result["reclaims"]:
        lines.append(
            f"\n★ **RECLAIM WATCH** ({len(result['reclaims'])}) "
            f"-- below EMA4, watch for cross above"
        )
        lines.append("```")
        for r in result["reclaims"]:
            lines.append(f"  {r['ticker']:<8}  {r['pct']:+.1f}% vs EMA4")
        lines.append("```")

    return "\n".join(lines)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--date",    default=None, help="Scan date YYYY-MM-DD (default: today)")
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()

    # Default to last business day's EOD data -- at 6:35 AM today's post-close hasn't run
    scan_date = args.date or _prev_business_day(date.today()).isoformat()
    if date.fromisoformat(scan_date).weekday() >= 5:
        print("Weekend -- skipping.")
        return

    conn = sqlite3.connect(DB_PATH)
    result = query_candidates(conn, scan_date)
    conn.close()

    msg = build_message(result)
    if msg:
        print(msg)
        if not args.dry_run:
            discord_router.send_to("locker", msg)
    else:
        print(f"No prospect candidates for {scan_date}.")


if __name__ == "__main__":
    main()
