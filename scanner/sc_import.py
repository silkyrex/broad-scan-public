import os
import sqlite3
import csv
import sys
from datetime import date, timedelta
from pathlib import Path

DB_PATH = Path(os.environ.get("BROAD_SCAN_DB", Path(__file__).parent.parent / "broad-scan.db"))


def week_ending(d: date) -> date:
    return d + timedelta(days=(5 - d.weekday()) % 7)


def import_sc_results(filepath: str, scan_date: date = None, scan_type: str = "sc_workbench") -> None:
    if scan_date is None:
        scan_date = date.today()
    we = week_ending(scan_date)

    conn = sqlite3.connect(DB_PATH)

    # Find the most recent prior scan of same type (handles weekends + missed days)
    prior = conn.execute(
        "SELECT MAX(scan_date) FROM scans WHERE scan_type = ? AND scan_date < ?",
        (scan_type, scan_date.isoformat())
    ).fetchone()[0]

    prior_tickers = set()
    if prior:
        prior_tickers = {r[0] for r in conn.execute(
            "SELECT ticker FROM scans WHERE scan_type = ? AND scan_date = ?",
            (scan_type, prior)
        ).fetchall()}

    inserted = 0

    delimiter = "\t" if str(filepath).endswith(".tsv") else ","
    with open(filepath, newline="", encoding="utf-8") as f:
        reader = csv.DictReader(f, delimiter=delimiter)
        for row in reader:
            ticker = row.get("Symbol", "").strip()
            if not ticker:
                continue

            sctr_raw = row.get("SCTR", "").strip()
            sctr = float(sctr_raw) if sctr_raw else None

            # is_new_signal = ticker was NOT in the previous scan of same type
            is_new = int(ticker not in prior_tickers)

            conn.execute(
                """INSERT OR IGNORE INTO scans (ticker, scan_type, scan_date, value, is_new_signal, week_ending)
                   VALUES (?, ?, ?, ?, ?, ?)""",
                (ticker, scan_type, scan_date.isoformat(), sctr, is_new, we.isoformat())
            )
            inserted += 1

    conn.commit()
    new_count = conn.execute(
        "SELECT COUNT(*) FROM scans WHERE scan_type=? AND scan_date=? AND is_new_signal=1",
        (scan_type, scan_date.isoformat())
    ).fetchone()[0]
    conn.close()
    prior_label = f"vs {prior}" if prior else "first run"
    print(f"Imported {inserted} tickers for {scan_date} ({scan_type}) -- {new_count} new entries ({prior_label})")


def latest_download() -> Path:
    downloads = Path.home() / "Downloads"
    candidates = list(downloads.glob("*.csv")) + list(downloads.glob("*.tsv"))
    if not candidates:
        raise FileNotFoundError("No .csv or .tsv files found in ~/Downloads")
    return max(candidates, key=lambda p: p.stat().st_mtime)


if __name__ == "__main__":
    if len(sys.argv) >= 2:
        filepath = sys.argv[1]
    else:
        filepath = latest_download()
        print(f"Auto-detected: {filepath}")

    scan_date = date.fromisoformat(sys.argv[2]) if len(sys.argv) > 2 else date.today()
    import_sc_results(filepath, scan_date)
