"""
Post-close: auto-screen sc_workbench tickers into prospects.

Gate 1: SCTR >= 90 (top 10% of all stocks by relative strength)
Gate 2: 2+ distinct signal categories today (is_new_signal=1)
         categories: ATH/52w/12w (HIGH), RSI-D, RSI-W, RSI-M
Gate 3: not already active in prospects or locker_room

Qualifying tickers get an AI-generated thesis (Haiku → Sonnet) and
are inserted into the prospects table with source='auto_workbench'.

Runs as Step 9 in run-post-close.sh, after write_eod_signals.
"""
from __future__ import annotations

import argparse
import os
import sqlite3
import sys
from datetime import date
from pathlib import Path

from locker._helpers import _add_to_db
from scanner import discord_router
from scanner.kb import capture as ob1_capture
from scanner.prospects import _auto_notes, _build_notes_technical, _insert_prospect

DB_PATH = Path(os.environ.get("BROAD_SCAN_DB", Path(__file__).parent.parent / "broad-scan.db"))

SCTR_FLOOR = 80.0
MIN_SIGNAL_CATS = 2
# Explicit display order — do not reorder, labels follow this sequence in Discord output
_SIGNAL_ORDER = ("ath", "52w_high", "12w_high", "rsi_daily", "rsi_weekly", "rsi_monthly")
DISPLAY_LABELS = {
    "ath":         "ATH",
    "52w_high":    "52wH",
    "12w_high":    "12wH",
    "rsi_daily":   "RSI-D",
    "rsi_weekly":  "RSI-W",
    "rsi_monthly": "RSI-M",
}


def _already_active(conn, ticker: str) -> bool:
    in_prospects = conn.execute(
        "SELECT 1 FROM prospects WHERE ticker=? AND status='active'", (ticker,)
    ).fetchone()
    in_locker = conn.execute(
        "SELECT 1 FROM locker_room WHERE ticker=? AND status='active'", (ticker,)
    ).fetchone()
    return bool(in_prospects or in_locker)


def _get_candidates(conn, scan_date: str) -> list[tuple[str, float, list[str]]]:
    """Return [(ticker, sctr, signal_labels)] passing both gates."""
    wb = conn.execute(
        "SELECT ticker, value FROM scans WHERE scan_date=? AND scan_type='sc_workbench' AND value >= ?",
        (scan_date, SCTR_FLOOR),
    ).fetchall()

    results = []
    for ticker, sctr in wb:
        rows = conn.execute(
            """SELECT DISTINCT scan_type FROM scans
               WHERE ticker=? AND scan_date=? AND is_new_signal=1
                 AND scan_type IN ('ath','52w_high','12w_high','rsi_daily','rsi_weekly','rsi_monthly')""",
            (ticker, scan_date),
        ).fetchall()
        cats = {r[0] for r in rows}
        if len(cats) < MIN_SIGNAL_CATS:
            continue
        labels = [DISPLAY_LABELS[c] for c in _SIGNAL_ORDER if c in cats]
        results.append((ticker, sctr, labels))

    return results


def run(scan_date: str, dry_run: bool = False) -> dict:
    conn = sqlite3.connect(DB_PATH)
    try:
        candidates = _get_candidates(conn, scan_date)
        added = []
        skipped_existing = []
        skipped_error = []

        for ticker, sctr, labels in candidates:
            if _already_active(conn, ticker):
                skipped_existing.append(ticker)
                continue

            if dry_run:
                print(f"  DRY-RUN would add: {ticker:<8} SCTR={sctr:.1f} signals={'+'.join(labels)}")
                added.append({"ticker": ticker, "sctr": sctr, "labels": labels})
                continue

            try:
                notes, enrichment = _auto_notes(ticker)
                notes_technical = _build_notes_technical(ticker, conn)

                # Price + 4EMA status from eod_signals (written by step 8, before step 9)
                eod_row = conn.execute(
                    "SELECT price, ema_4e_pct FROM eod_signals WHERE ticker=? AND signal_date=? LIMIT 1",
                    (ticker, scan_date),
                ).fetchone()
                if eod_row is None:
                    print(f"  ! {ticker}: no eod_signals row for {scan_date} -- step 8 may not have run; routing to prospects", file=sys.stderr)

                # Guard price and ema_4e_pct independently: a row can have
                # price=NULL while ema_4e_pct is still valid (yfinance gap).
                if eod_row:
                    add_price = float(eod_row[0]) if eod_row[0] is not None else None
                    above_ema = eod_row[1] is not None and eod_row[1] > 0
                else:
                    add_price = None
                    above_ema = False

                notes_str = notes or ""

                if above_ema:
                    # All 3 gates met: SCTR>=90 + 2 signals + above 4EMA -> locker directly
                    _add_to_db(conn, ticker, source="auto_workbench",
                               notes=notes_str, scan_signals=labels)
                    dest = "locker"
                else:
                    # Strong signal but below 4EMA -> prospects (hunt for reclaim)
                    _insert_prospect(
                        conn, ticker,
                        source="auto_workbench",
                        notes=notes_str,
                        notes_technical=notes_technical,
                        enrichment=enrichment,
                        add_price=add_price,
                    )
                    dest = "prospects"

                conn.commit()
                added.append({"ticker": ticker, "sctr": sctr, "labels": labels,
                              "notes": notes_str, "dest": dest})
                print(f"  + {ticker}  SCTR={sctr:.1f}  {'+'.join(labels)}  → {dest}")
            except Exception as e:
                skipped_error.append(f"{ticker}: {e}")
                print(f"  ! {ticker} failed: {e}", file=sys.stderr)

    finally:
        conn.close()

    return {
        "scan_date": scan_date,
        "candidates": len(candidates),
        "added": added,
        "skipped_existing": skipped_existing,
        "skipped_error": skipped_error,
        "dry_run": dry_run,
    }


def build_discord_message(result: dict) -> str:
    d = result["scan_date"]
    to_locker    = [p for p in result["added"] if p.get("dest") == "locker"]
    to_prospects = [p for p in result["added"] if p.get("dest") != "locker"]
    n = len(result["added"])
    lines = [f"📋 **AUTO-PROSPECT {d}** | {result['candidates']} candidates (SCTR≥90 + 2 signals) → **{n} added**"]

    if to_locker:
        lines.append(f"  → locker ({len(to_locker)}): above 4EMA, all gates met")
        lines.append("```")
        for p in to_locker:
            sig = "+".join(p["labels"])
            lines.append(f"  {p['ticker']:<8}  SCTR {p['sctr']:.0f}  {sig}")
        lines.append("```")

    if to_prospects:
        lines.append(f"  → prospects ({len(to_prospects)}): below 4EMA, hunting for reclaim")
        lines.append("```")
        for p in to_prospects:
            sig = "+".join(p["labels"])
            note = f"  {p['notes'][:40]}" if p.get("notes") else ""
            lines.append(f"  {p['ticker']:<8}  SCTR {p['sctr']:.0f}  {sig:<20}{note}")
        lines.append("```")

    if result["skipped_existing"]:
        lines.append(f"↩ {len(result['skipped_existing'])} already in prospects/locker — skipped")

    if result["skipped_error"]:
        lines.append(f"⚠ {len(result['skipped_error'])} error(s): {', '.join(result['skipped_error'][:3])}")

    return "\n".join(lines)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dry-run", action="store_true", help="Print without writing to DB")
    parser.add_argument("--date", default=str(date.today()), help="Scan date YYYY-MM-DD")
    args = parser.parse_args()

    scan_date = args.date
    print(f"auto_prospect {scan_date}  dry_run={args.dry_run}")

    try:
        result = run(scan_date, dry_run=args.dry_run)
        msg = build_discord_message(result)
        print(msg)
        if not args.dry_run:
            discord_router.send_to("locker", msg)
            ob1_capture(
                f"auto_prospect {scan_date}: {result['candidates']} candidates, "
                f"{len(result['added'])} added to prospects. "
                f"Skipped existing: {len(result['skipped_existing'])}."
            )
    except Exception as e:
        err = f"🔴 auto_prospect FAILED {scan_date}: {e}"
        print(err, file=sys.stderr)
        try:
            discord_router.send_to("locker", err)
        except Exception:
            pass
        raise
