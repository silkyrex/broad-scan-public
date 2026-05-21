import argparse
import os
import sqlite3
import subprocess
from datetime import date
from pathlib import Path

DB_PATH = Path(os.environ.get("BROAD_SCAN_DB", Path(__file__).parent.parent / "broad-scan.db"))
TRIAGE = Path.home() / "projects/do-infra-local/scripts/triage_pdf.py"


def scan_today(scan_date=None, charts=False):
    if scan_date is None:
        scan_date = date.today().isoformat()

    conn = sqlite3.connect(DB_PATH)
    rows = conn.execute("""
        SELECT
          s.ticker,
          MAX(CASE WHEN s.scan_type='rsi_daily'  AND s.is_new_signal=1 THEN 1 ELSE 0 END) as rsi_d,
          MAX(CASE WHEN s.scan_type='rsi_weekly' AND s.is_new_signal=1 THEN 1 ELSE 0 END) as rsi_w,
          MAX(CASE WHEN s.scan_type='ath'        AND s.is_new_signal=1 THEN 1 ELSE 0 END) as new_ath,
          COALESCE(ROUND(sc.value, 1), -1) as sctr
        FROM scans s
        LEFT JOIN scans sc ON s.ticker = sc.ticker
          AND sc.scan_date = ? AND sc.scan_type = 'sc_workbench'
        WHERE s.scan_date = ?
          AND s.is_new_signal = 1
          AND s.scan_type IN ('rsi_daily', 'rsi_weekly', 'ath')
        GROUP BY s.ticker
        ORDER BY
          (MAX(CASE WHEN s.scan_type='rsi_daily'  AND s.is_new_signal=1 THEN 1 ELSE 0 END) +
           MAX(CASE WHEN s.scan_type='rsi_weekly' AND s.is_new_signal=1 THEN 1 ELSE 0 END) +
           MAX(CASE WHEN s.scan_type='ath'        AND s.is_new_signal=1 THEN 1 ELSE 0 END)) DESC,
          sctr DESC
    """, (scan_date, scan_date)).fetchall()
    conn.close()

    print(f"\nScan Today  {scan_date}")
    print(f"{'Ticker':<8}  {'RSI-D':>5}  {'RSI-W':>5}  {'ATH':>4}  {'SCTR':>6}")
    print("-" * 38)

    tickers = []
    for ticker, rsi_d, rsi_w, ath, sctr in rows:
        if not any([rsi_d, rsi_w, ath]):
            continue
        d = "  ✓ " if rsi_d else "    "
        w = "  ✓ " if rsi_w else "    "
        a = " ✓ " if ath else "   "
        s = f"{sctr:.1f}" if sctr >= 0 else "  --"
        print(f"{ticker:<8}  {d}  {w}  {a}  {s:>6}")
        tickers.append(ticker)

    print(f"\n{len(tickers)} names to evaluate")

    if charts:
        if not TRIAGE.exists():
            print(f"ERROR: triage_pdf.py not found at {TRIAGE}")
            return
        print(f"\nFetching charts for {len(tickers)} names (PDF → Desktop)...")
        subprocess.run(["/Library/Developer/CommandLineTools/usr/bin/python3", str(TRIAGE)] + tickers)


if __name__ == "__main__":
    ap = argparse.ArgumentParser(prog="scan_today", description="Show today's new scan entries")
    ap.add_argument("date", nargs="?", default=None, help="Scan date YYYY-MM-DD (default: today)")
    ap.add_argument("--charts", action="store_true", help="Fetch weekly chart PDF to Desktop")
    args = ap.parse_args()
    scan_today(args.date, charts=args.charts)
