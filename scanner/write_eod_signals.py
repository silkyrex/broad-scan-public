"""
write_eod_signals.py

Post-close writer for the eod_signals table.
Pulls all active locker + prospect tickers, computes signals via yfinance,
and upserts one row per ticker for the given date.

Run after run-close.sh so close prices are settled.

Usage:
  python -m scanner.write_eod_signals
  python -m scanner.write_eod_signals --date 2026-05-18   # backfill
  python -m scanner.write_eod_signals --dry-run
"""
from __future__ import annotations

import argparse
import os
import sqlite3
import warnings
from datetime import date
from pathlib import Path

import pandas as pd

warnings.filterwarnings("ignore")

DB_PATH = Path(os.environ.get("BROAD_SCAN_DB", Path(__file__).parent.parent / "broad-scan.db"))

# Indexes, bonds, sector ETFs, commodities, and crypto tracked for /axe macro regime reads
MACRO_TICKERS: dict[str, str] = {
    # Indexes
    "SPY":     "index",
    "QQQ":     "index",
    "IWM":     "index",
    "DIA":     "index",
    # Bonds
    "TLT":     "bond",
    # Sector ETFs
    "XLK":     "sector",
    "XLF":     "sector",
    "XLV":     "sector",
    "XLE":     "sector",
    "XLI":     "sector",
    "XLC":     "sector",
    "XLY":     "sector",
    "XLP":     "sector",
    "XLB":     "sector",
    "XLRE":    "sector",
    "XLU":     "sector",
    # Commodities
    "GLD":     "commodity",
    "SLV":     "commodity",
    "USO":     "commodity",
    # Crypto (24/7; buzz_d computed but less comparable to equity norms)
    "BTC-USD": "crypto",
    "ETH-USD": "crypto",
    "XRP-USD": "crypto",
    "SOL-USD": "crypto",
    # Bond yields (index symbols; volume=null, RSI/MACD/tape compute off close only)
    "^TNX":    "yield",   # 10-year Treasury yield
    "^TYX":    "yield",   # 30-year Treasury yield
    "^FVX":    "yield",   # 5-year Treasury yield
}


def _macd_state(closes: pd.Series | None) -> str | None:
    """4-state histogram color matching macd-your-org.pine.

    green  = growing above zero (bullish accelerating)
    yellow = falling above zero (bullish decelerating)
    orange = growing below zero (bearish recovering)
    white  = falling below zero (full bearish)
    """
    if closes is None or len(closes) < 35:
        return None
    fast = closes.ewm(span=12, adjust=False).mean()
    slow = closes.ewm(span=26, adjust=False).mean()
    macd = fast - slow
    sig  = macd.ewm(span=9, adjust=False).mean()
    hist = (macd - sig).dropna()
    if len(hist) < 2:
        return None
    curr = float(hist.iloc[-1])
    prev = float(hist.iloc[-2])
    if curr >= 0:
        return "green" if curr > prev else "yellow"
    return "orange" if curr > prev else "white"


def _rsi14(closes: pd.Series | None) -> float | None:
    if closes is None or len(closes) < 15:
        return None
    delta = closes.diff()
    gain  = delta.clip(lower=0).ewm(com=13, adjust=False).mean()
    loss  = (-delta.clip(upper=0)).ewm(com=13, adjust=False).mean()
    rs    = gain / loss.replace(0, float("nan"))
    return round(float(100 - 100 / (1 + rs.iloc[-1])), 1)


def _buzz_pct(volumes: pd.Series, ma_len: int) -> float | None:
    """vol_buzz_pct = 100*(vol/ma)-100 -- matches volume_buzz.py formula."""
    if volumes is None or len(volumes) < ma_len + 1:
        return None
    ma = float(volumes.rolling(ma_len).mean().iloc[-1])
    if ma == 0:
        return None
    return round(100 * float(volumes.iloc[-1]) / ma - 100, 1)


def _compute(ticker: str, closes_d: pd.Series, closes_w: pd.Series | None,
             vols_d: pd.Series | None, vols_w: pd.Series | None,
             source: str, weekly_closed: bool = True) -> dict | None:
    if closes_d is None or len(closes_d) < 5:
        return None

    ema4      = closes_d.ewm(span=4, adjust=False).mean()
    price     = float(closes_d.iloc[-1])
    curr_ema4 = float(ema4.iloc[-1])
    prev_close = float(closes_d.iloc[-2])
    prev_ema4  = float(ema4.iloc[-2])

    tape       = "green" if price > curr_ema4 else "red"
    ema_4e_pct = round((price - curr_ema4) / curr_ema4 * 100, 2) if curr_ema4 else None

    if prev_close < prev_ema4 and price > curr_ema4:
        toby = "reclaim"
    elif price >= curr_ema4:
        toby = "hold"
    elif source == "locker":
        toby = "exit-d2" if prev_close < prev_ema4 else "exit-d1"
    else:
        toby = "na"

    return {
        "ticker":       ticker,
        "source":       source,
        "price":        round(price, 2),
        "tape":         tape,
        "rsi_d":        _rsi14(closes_d),
        "rsi_w":        _rsi14(closes_w),
        "macd_d_state": _macd_state(closes_d),
        "macd_w_state": _macd_state(closes_w),
        "buzz_d":       _buzz_pct(vols_d, 50),
        "buzz_w":       _buzz_pct(
            vols_w if weekly_closed else (vols_w.iloc[:-1] if vols_w is not None and len(vols_w) > 1 else vols_w),
            10,
        ),
        "ema_4e_pct":   ema_4e_pct,
        "toby_status":  toby,
    }


def _fetch_tickers(conn: sqlite3.Connection) -> tuple[list[str], list[str]]:
    locker = [r[0] for r in conn.execute(
        "SELECT ticker FROM locker_room WHERE status='active' AND ticker != 'TEST'"
    ).fetchall()]
    prospects = [r[0] for r in conn.execute(
        "SELECT ticker FROM prospects WHERE status='active'"
    ).fetchall()]
    return locker, prospects


def _upsert(conn: sqlite3.Connection, signal_date: str, rows: list[dict]) -> int:
    written = 0
    for r in rows:
        conn.execute("""
            INSERT OR REPLACE INTO eod_signals
              (signal_date, ticker, source, price, tape,
               rsi_d, rsi_w, macd_d_state, macd_w_state,
               buzz_d, buzz_w, ema_4e_pct, toby_status)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, (
            signal_date,
            r["ticker"], r["source"], r["price"], r["tape"],
            r["rsi_d"], r["rsi_w"], r["macd_d_state"], r["macd_w_state"],
            r["buzz_d"], r["buzz_w"], r["ema_4e_pct"], r["toby_status"],
        ))
        written += 1
    conn.commit()
    return written


def _col(df: pd.DataFrame, col: str, ticker: str) -> pd.Series | None:
    """Extract a column series for a single ticker from a yf.download DataFrame."""
    try:
        if isinstance(df.columns, pd.MultiIndex):
            s = df[col][ticker]
        else:
            s = df[col]
        return s.dropna().astype(float) if s is not None else None
    except (KeyError, TypeError):
        return None


def main() -> None:
    p = argparse.ArgumentParser(description="Write EOD signals to eod_signals table")
    p.add_argument("--date",    default=None, help="Signal date YYYY-MM-DD (default: today)")
    p.add_argument("--dry-run", action="store_true", help="Compute but do not write to DB")
    args = p.parse_args()

    signal_date = args.date or str(date.today())
    signal_weekday = date.fromisoformat(signal_date).weekday()
    if signal_weekday >= 5:
        print("Weekend -- skipping.")
        raise SystemExit(0)
    weekly_closed = signal_weekday == 4  # Friday: weekly bar just closed

    import yfinance as yf

    conn = sqlite3.connect(DB_PATH)
    locker_tickers, prospect_tickers = _fetch_tickers(conn)
    macro_tickers = list(MACRO_TICKERS.keys())
    all_tickers = list(dict.fromkeys(locker_tickers + prospect_tickers + macro_tickers))

    print(f"EOD signals -- {signal_date}")
    print(f"  {len(locker_tickers)} locker · {len(prospect_tickers)} prospects · "
          f"{len(macro_tickers)} macro · {len(all_tickers)} unique")

    if not all_tickers:
        print("  No tickers -- nothing to write.")
        conn.close()
        return

    print("  Downloading daily 1y...")
    daily = yf.download(all_tickers, period="1y", progress=False, auto_adjust=True)
    print("  Downloading weekly 2y...")
    weekly = yf.download(all_tickers, period="2y", interval="1wk",
                         progress=False, auto_adjust=True)

    _SOURCE_LABEL = {"locker": "L", "prospect": "P", "index": "I",
                     "bond": "B", "sector": "S", "commodity": "C", "crypto": "X"}

    rows: list[dict] = []
    for ticker in all_tickers:
        if ticker in locker_tickers:
            source = "locker"
        elif ticker in prospect_tickers:
            source = "prospect"
        else:
            source = MACRO_TICKERS.get(ticker, "sector")

        closes_d = _col(daily,  "Close",  ticker)
        closes_w = _col(weekly, "Close",  ticker)
        vols_d   = _col(daily,  "Volume", ticker)
        vols_w   = _col(weekly, "Volume", ticker)

        row = _compute(ticker, closes_d, closes_w, vols_d, vols_w, source, weekly_closed)
        if row is None:
            print(f"  {ticker:<8}  SKIP (insufficient data)")
            continue

        rows.append(row)
        print(f"  {ticker:<8}  [{_SOURCE_LABEL.get(source, source)}]"
              f"  ${row['price']}  tape={row['tape']}"
              f"  rsi_d={row['rsi_d']}  4e%={row['ema_4e_pct']}"
              f"  macd_d={row['macd_d_state']}  toby={row['toby_status']}")

    if args.dry_run:
        print(f"\n[dry-run] Would write {len(rows)} rows -- DB not updated.")
    else:
        n = _upsert(conn, signal_date, rows)
        print(f"\nWrote {n} rows to eod_signals for {signal_date}.")

    conn.close()


if __name__ == "__main__":
    main()
