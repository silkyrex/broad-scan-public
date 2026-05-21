#!/usr/bin/env python3
"""
qa_gate.py -- acceptance criteria gate for the broad-scan close chain.

Reads today's scans table, evaluates 4 criteria, posts pass/warn/fail
to #qa-status Discord channel (DISCORD_QA_WEBHOOK env var), exits 0 or 1.

Exit codes:
  0 = pass or soft warn (chain continues)
  1 = hard fail (chain halts via set -euo pipefail)

Usage:
  python -m scanner.qa_gate          # close run (full checks)
  python -m scanner.qa_gate --morning  # morning run (rows + workbench only)
"""

import argparse
import json
import os
import sqlite3
import sys
import urllib.request
from datetime import date
from pathlib import Path

DB_PATH = os.environ.get(
    "BROAD_SCAN_DB",
    str(Path(__file__).parent.parent / "broad-scan.db"),
)

QA_WEBHOOK = os.environ.get("DISCORD_QA_WEBHOOK", "")

YFINANCE_TYPES = {"rsi_daily", "rsi_weekly", "rsi_monthly", "52w_high", "12w_high", "ath"}


def _post(msg: str) -> None:
    print(msg)
    if not QA_WEBHOOK:
        return
    data = json.dumps({"content": msg}).encode()
    req = urllib.request.Request(
        QA_WEBHOOK,
        data=data,
        headers={"Content-Type": "application/json", "User-Agent": "broad-scan/qa-gate"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=10):
            pass
    except Exception as e:
        print(f"[qa_gate] Discord post failed: {e}", file=sys.stderr)


def run(morning: bool = False) -> int:
    today = date.today().isoformat()
    hard_fails = []
    soft_warns = []

    try:
        conn = sqlite3.connect(DB_PATH)
        cur = conn.cursor()
    except Exception as e:
        _post(f"[QA FAIL broad-scan: DB connect error -- {e}]")
        return 1

    # Check 1: any rows exist for today
    cur.execute("SELECT COUNT(*) FROM scans WHERE scan_date = ?", (today,))
    total_rows = cur.fetchone()[0]
    if total_rows == 0:
        hard_fails.append("no rows for today -- cron may have died")

    # Check 2: sc_workbench count sanity
    cur.execute(
        "SELECT COUNT(*) FROM scans WHERE scan_type = 'sc_workbench' AND scan_date = ?",
        (today,),
    )
    wb_count = cur.fetchone()[0]
    if wb_count == 0:
        hard_fails.append("sc_workbench count = 0 -- Playwright fetch failed")
    elif wb_count < 10:
        soft_warns.append(f"sc_workbench unusually low: {wb_count} names")
    elif wb_count > 800:
        soft_warns.append(f"sc_workbench unusually high: {wb_count} names (threshold reset?)")

    if morning:
        conn.close()
        if hard_fails:
            _post(f"[QA FAIL broad-scan morning: {'; '.join(hard_fails)}]")
            return 1
        if soft_warns:
            _post(f"[QA WARN broad-scan morning: {'; '.join(soft_warns)} | wb={wb_count}]")
            return 0
        _post(f"[QA PASS broad-scan morning: wb={wb_count} names]")
        return 0

    # Check 3: at least one yfinance signal type fired today
    placeholders = ",".join("?" * len(YFINANCE_TYPES))
    cur.execute(
        f"SELECT COUNT(*) FROM scans WHERE scan_type IN ({placeholders}) AND scan_date = ?",
        (*YFINANCE_TYPES, today),
    )
    yf_count = cur.fetchone()[0]
    if yf_count == 0:
        hard_fails.append("no yfinance signals fired today -- signals.py may have failed")

    # Check 4: SC email -> yfinance overlap (skip if no sc_email rows, e.g. local dev)
    cur.execute(
        "SELECT COUNT(*) FROM scans WHERE scan_type LIKE 'sc_email_%' AND scan_date = ?",
        (today,),
    )
    sc_email_total = cur.fetchone()[0]

    overlap_pct = None
    if sc_email_total > 0:
        cur.execute(
            "SELECT DISTINCT ticker FROM scans WHERE scan_type LIKE 'sc_email_%' AND scan_date = ?",
            (today,),
        )
        sc_email_tickers = {r[0] for r in cur.fetchall()}

        cur.execute(
            f"SELECT DISTINCT ticker FROM scans WHERE scan_type IN ({placeholders}) AND scan_date = ?",
            (*YFINANCE_TYPES, today),
        )
        yf_tickers = {r[0] for r in cur.fetchall()}

        overlap = sc_email_tickers & yf_tickers
        overlap_pct = len(overlap) / len(sc_email_tickers) * 100 if sc_email_tickers else 100.0

        if overlap_pct < 80:
            hard_fails.append(f"SC->yfinance overlap {overlap_pct:.0f}% (< 80% hard floor)")
        elif overlap_pct < 95:
            soft_warns.append(f"SC->yfinance overlap {overlap_pct:.0f}% (< 95% TRA-XXX target)")

    conn.close()

    overlap_str = f", overlap={overlap_pct:.0f}%" if overlap_pct is not None else ", overlap=n/a (no sc_email)"

    if hard_fails:
        _post(f"[QA FAIL broad-scan: {'; '.join(hard_fails)}]")
        return 1

    if soft_warns:
        _post(f"[QA WARN broad-scan: {'; '.join(soft_warns)} | wb={wb_count}{overlap_str}]")
        return 0

    _post(f"[QA PASS broad-scan: wb={wb_count}{overlap_str}]")
    return 0


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--morning", action="store_true", help="Morning run (rows + workbench only)")
    args = parser.parse_args()
    sys.exit(run(morning=args.morning))


if __name__ == "__main__":
    main()
