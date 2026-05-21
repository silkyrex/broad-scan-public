import sqlite3
import yfinance as yf
import pandas as pd
from datetime import date
import os
from pathlib import Path
from scanner.signals import check_signals, week_ending
from scanner.sc_ingest import get_email_signals

DB_PATH = Path(os.environ.get("BROAD_SCAN_DB", Path(__file__).parent.parent / "broad-scan.db"))


def fetch_data(tickers: list[str]) -> dict[str, pd.DataFrame]:
    print(f"Fetching data for {len(tickers)} tickers...")
    raw = yf.download(tickers, period="5y", auto_adjust=True, progress=False, group_by="ticker")
    result = {}
    if len(tickers) == 1:
        ticker = tickers[0]
        if not raw.empty:
            result[ticker] = raw
    else:
        for ticker in tickers:
            try:
                df = raw[ticker].dropna()
                if not df.empty:
                    result[ticker] = df
            except KeyError:
                # Try cross-section approach for newer yfinance versions
                try:
                    df = raw.xs(ticker, axis=1, level=1).dropna()
                    if not df.empty:
                        result[ticker] = df
                except KeyError:
                    print(f"  WARNING: no data for {ticker}")
    return result


def write_results(conn: sqlite3.Connection, ticker: str, results: list[dict]) -> None:
    for r in results:
        conn.execute(
            """INSERT OR IGNORE INTO scans (ticker, scan_type, scan_date, value, is_new_signal, week_ending)
               VALUES (?, ?, ?, ?, ?, ?)""",
            (ticker, r["scan_type"], r["scan_date"], r["value"], r["is_new_signal"], r["week_ending"]),
        )


def write_email_rows(conn: sqlite3.Connection, ticker_tags: dict[str, list[str]], scan_date: date) -> int:
    """Write one row per (ticker, sc_email_* tag) for today.

    is_new_signal: 1 if this (ticker, tag) wasn't present on the prior scan_date
    for the same tag.
    """
    we = week_ending(scan_date).isoformat()
    written = 0
    # Build a set of (ticker, tag) pairs that existed on the most recent prior
    # date per tag, so is_new_signal tolerates weekends/missed days.
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


def main() -> None:
    ticker_tags = get_email_signals()
    tickers = sorted(ticker_tags.keys())
    print(f"SC source: {len(tickers)} unique tickers across {len({t for ts in ticker_tags.values() for t in ts})} email scans")

    if not tickers:
        print("No tickers to scan. Exiting.")
        return

    scan_date = date.today()

    conn = sqlite3.connect(DB_PATH)

    # First: record SC's own scan classifications (no yfinance needed)
    n_email_rows = write_email_rows(conn, ticker_tags, scan_date)
    conn.commit()
    print(f"Wrote {n_email_rows} sc_email_* rows for {scan_date}")

    data = fetch_data(tickers)
    total_hits = 0

    for ticker, df in data.items():
        # Handle both uppercase and lowercase column names
        if "Close" in df.columns:
            close = df["Close"]
        elif "close" in df.columns:
            close = df["close"]
        else:
            print(f"  WARNING: no Close column for {ticker}, columns: {list(df.columns)}")
            continue
        results = check_signals(close, scan_date)
        if results:
            write_results(conn, ticker, results)
            total_hits += len(results)
            new_entries = [r["scan_type"] for r in results if r["is_new_signal"]]
            print(f"  {ticker}: {len(results)} hits" + (f" | NEW: {', '.join(new_entries)}" if new_entries else ""))
        else:
            print(f"  {ticker}: no hits")

    conn.commit()
    conn.close()
    print(f"\nDone. {total_hits} scan hits written to broad-scan.db for {scan_date}.")


if __name__ == "__main__":
    main()
