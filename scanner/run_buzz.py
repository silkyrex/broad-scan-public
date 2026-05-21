"""
run_buzz.py

Reads today's sc_workbench tickers from broad-scan.db, scores vol_buzz and
theme_buzz, writes results back as new scan_type rows, and captures the
high-conviction overlap (vol + theme) to KB.

Run: python -m scanner.run_buzz
     python -m scanner.run_buzz --date 2026-05-15 --dry-run
"""
import argparse
import json
import os
import sqlite3
import urllib.request
from datetime import date
from pathlib import Path

import pandas as pd

from scanner.volume_buzz import scan as vol_scan, THRESHOLD as VOL_THRESHOLD
from scanner.theme_buzz  import scan as theme_scan, build_theme_map

DB_PATH = Path(os.environ.get("BROAD_SCAN_DB", Path(__file__).parent.parent / "broad-scan.db"))
OB1_URL = os.environ.get("KB_MCP_URL", "")


def get_sc_workbench_tickers(db_path: Path, scan_date: str) -> list[str]:
    conn = sqlite3.connect(db_path)
    rows = conn.execute(
        "SELECT DISTINCT ticker FROM scans WHERE scan_type='sc_workbench' AND scan_date=?",
        (scan_date,),
    ).fetchall()
    conn.close()
    return [r[0] for r in rows]


def write_to_db(db_path: Path, rows: list[dict], scan_type: str,
                value_key: str, buzz_key: str, scan_date: str) -> int:
    conn = sqlite3.connect(db_path)
    written = 0
    for row in rows:
        val = row.get(value_key)
        if val is None:
            continue
        conn.execute("""
            INSERT OR REPLACE INTO scans (ticker, scan_type, scan_date, value, is_new_signal)
            VALUES (?, ?, ?, ?, ?)
        """, (row["ticker"], scan_type, scan_date, float(val),
              1 if row.get(buzz_key) else 0))
        written += 1
    conn.commit()
    conn.close()
    return written


def ob1_capture(content: str) -> None:
    """Fire-and-forget KB capture. Never raises. Skips if KB_MCP_URL not set."""
    if not OB1_URL:
        return
    try:
        payload = json.dumps({
            "jsonrpc": "2.0", "method": "tools/call",
            "params": {"name": "capture_thought", "arguments": {"content": content}},
            "id": 1,
        }).encode()
        req = urllib.request.Request(
            OB1_URL, data=payload, method="POST",
            headers={"Content-Type": "application/json",
                     "Accept": "application/json, text/event-stream",
                     "User-Agent": "broad-scan-buzz"},
        )
        urllib.request.urlopen(req, timeout=15)
    except Exception:
        pass


def build_ob1_content(overlap: pd.DataFrame, scan_date: str) -> str:
    lines = [f"Buzz scan [{scan_date}]: {len(overlap)} high conviction (vol_buzz + theme_buzz)."]
    for _, row in overlap.iterrows():
        themes = ", ".join(row.get("themes") or [])
        lines.append(
            f"{row['ticker']}: vol_buzz={row['vol_buzz_pct']:+.0f}%  "
            f"themes=[{themes}]  etf_momentum={row['avg_momentum']:+.1f}%  "
            f"ud_ratio={row.get('ud_ratio', 'n/a')}"
        )
    return "\n".join(lines)


def main():
    parser = argparse.ArgumentParser(description="Run vol_buzz + theme_buzz on sc_workbench")
    parser.add_argument("--date",    default=date.today().isoformat())
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()

    scan_date = args.date
    tickers   = get_sc_workbench_tickers(DB_PATH, scan_date)
    if not tickers:
        print(f"No sc_workbench tickers for {scan_date}")
        return

    # --- vol_buzz ---
    print(f"vol_buzz: scanning {len(tickers)} tickers...")
    vdf = vol_scan(tickers, threshold=VOL_THRESHOLD)
    buzzing_v = vdf[vdf["buzz"] == True]
    print(f"  {len(buzzing_v)} buzzing (>= {VOL_THRESHOLD:.0f}% above 50d MA)")

    if not args.dry_run:
        n = write_to_db(DB_PATH, vdf.to_dict("records"), "vol_buzz",
                        "vol_buzz_pct", "buzz", scan_date)
        print(f"  -> wrote {n} vol_buzz rows")

    # --- theme_buzz ---
    print(f"theme_buzz: building ETF map...")
    theme_map = build_theme_map()
    tdf = theme_scan(tickers, theme_map=theme_map)
    buzzing_t = tdf[tdf["buzz"] == True]
    print(f"  {len(buzzing_t)} in hot theme ETF")

    if not args.dry_run:
        n = write_to_db(DB_PATH, tdf.to_dict("records"), "theme_buzz",
                        "theme_score", "buzz", scan_date)
        print(f"  -> wrote {n} theme_buzz rows")

    # --- overlap: high conviction ---
    vol_set   = set(buzzing_v["ticker"])
    theme_set = set(buzzing_t["ticker"])
    overlap_tickers = vol_set & theme_set

    print(f"\n=== HIGH CONVICTION ({len(overlap_tickers)} names) ===")
    if overlap_tickers:
        overlap = (
            vdf[vdf["ticker"].isin(overlap_tickers)][["ticker", "vol_buzz_pct", "ud_ratio"]]
            .merge(
                tdf[tdf["ticker"].isin(overlap_tickers)][["ticker", "theme_score", "avg_momentum", "themes"]],
                on="ticker"
            )
            .sort_values("vol_buzz_pct", ascending=False)
        )
        print(overlap.to_string(index=False))

        thought = build_ob1_content(overlap, scan_date)
        if not args.dry_run:
            ob1_capture(thought)
            print(f"  -> KB captured")
    else:
        print("  none")

    if args.dry_run:
        print("\n[dry-run] DB not updated")


if __name__ == "__main__":
    main()
