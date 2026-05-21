"""Close-time orchestrator -- runs at 4:15 PM ET (1:15 PM PT) M-F.

1. Ingests today's SC close-trigger emails (arrive ~4 PM ET, still UNSEEN)
2. Runs yfinance signal detection for any tickers not already processed today
3. Posts close summary to Discord
"""
import sqlite3
import yfinance as yf
import pandas as pd
from datetime import date
import os
from pathlib import Path
from scanner.signals import check_signals, week_ending
from scanner.sc_ingest import get_email_signals

DB_PATH = Path(os.environ.get("BROAD_SCAN_DB", Path(__file__).parent.parent / "broad-scan.db"))


def write_email_rows(conn: sqlite3.Connection, ticker_tags: dict[str, list[str]], scan_date: date) -> int:
    we = week_ending(scan_date).isoformat()
    written = 0
    tags = sorted({t for ts in ticker_tags.values() for t in ts})
    prior_pairs: set[tuple[str, str]] = set()
    for tag in tags:
        prior = conn.execute(
            "SELECT MAX(scan_date) FROM scans WHERE scan_type=? AND scan_date<?",
            (tag, scan_date.isoformat()),
        ).fetchone()[0]
        if prior:
            for (sym,) in conn.execute(
                "SELECT ticker FROM scans WHERE scan_type=? AND scan_date=?",
                (tag, prior),
            ):
                prior_pairs.add((sym, tag))
    for ticker, ts in ticker_tags.items():
        for tag in ts:
            is_new = int((ticker, tag) not in prior_pairs)
            conn.execute(
                """INSERT OR IGNORE INTO scans (ticker, scan_type, scan_date, value, is_new_signal, week_ending)
                   VALUES (?, ?, ?, NULL, ?, ?)""",
                (ticker, tag, scan_date.isoformat(), is_new, we),
            )
            written += 1
    return written


def write_signal_rows(conn: sqlite3.Connection, ticker: str, results: list[dict]) -> None:
    for r in results:
        conn.execute(
            """INSERT OR IGNORE INTO scans (ticker, scan_type, scan_date, value, is_new_signal, week_ending)
               VALUES (?, ?, ?, ?, ?, ?)""",
            (ticker, r["scan_type"], r["scan_date"], r["value"], r["is_new_signal"], r["week_ending"]),
        )


def main() -> None:
    scan_date = date.today()

    # Ingest close emails (UNSEEN at 4:15 PM; morning run only saw emails up to 6:35 AM)
    all_ticker_tags = get_email_signals()

    # Keep only _close tags
    ticker_tags = {
        ticker: [tag for tag in tags if tag.endswith("_close")]
        for ticker, tags in all_ticker_tags.items()
    }
    ticker_tags = {t: tags for t, tags in ticker_tags.items() if tags}

    if not ticker_tags:
        print("No close email signals today.")
        return

    tickers = sorted(ticker_tags.keys())
    n_scans = len({t for ts in ticker_tags.values() for t in ts})
    print(f"Close signals: {len(tickers)} tickers across {n_scans} close scans")

    conn = sqlite3.connect(DB_PATH)

    n_written = write_email_rows(conn, ticker_tags, scan_date)
    conn.commit()
    print(f"Wrote {n_written} sc_email_*_close rows for {scan_date}")

    # Fetch yfinance only for tickers not yet in today's DB
    already_fetched = {
        row[0] for row in conn.execute(
            "SELECT DISTINCT ticker FROM scans WHERE scan_date=? AND scan_type NOT LIKE 'sc_%'",
            (scan_date.isoformat(),),
        )
    }
    to_fetch = [t for t in tickers if t not in already_fetched]

    if to_fetch:
        print(f"Fetching yfinance for {len(to_fetch)} new tickers...")
        raw = yf.download(to_fetch, period="5y", auto_adjust=True, progress=False, group_by="ticker")
        total_hits = 0
        for ticker in to_fetch:
            try:
                df = raw[ticker].dropna() if len(to_fetch) > 1 else raw.dropna()
                if df.empty:
                    continue
                col = "Close" if "Close" in df.columns else "close"
                if col not in df.columns:
                    continue
                results = check_signals(df[col], scan_date)
                if results:
                    write_signal_rows(conn, ticker, results)
                    total_hits += len(results)
            except Exception as e:
                print(f"  WARNING: {ticker}: {e}")
        conn.commit()
        print(f"yfinance: {total_hits} signal hits")
    else:
        print("All close tickers already have signal rows; skipping yfinance")

    conn.close()
    print(f"Close scan complete for {scan_date}")


if __name__ == "__main__":
    main()
