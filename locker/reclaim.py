"""4 EMA reclaim status across the locker room.

Reads tickers from broad-scan.db (locker_room table), batch-fetches daily
bars via yfinance in one call, reports per-ticker 4 EMA status and flags
names firing the 3-condition reclaim trigger:
  1. Rising slope: today's 4 EMA > yesterday's 4 EMA
  2. Crossover:    previous bar closed below 4 EMA
  3. Reclaim:      today's price > today's 4 EMA
"""
import sqlite3
import warnings
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import yfinance as yf

warnings.filterwarnings("ignore")

DB = Path(__file__).parent.parent / "broad-scan.db"
G, R, Y, B, D, X = "\033[32m", "\033[31m", "\033[33m", "\033[1m", "\033[2m", "\033[0m"


def _analyze(closes):
    """Closes series → (price, ema, slope, status) where status in {reclaim, above, below}."""
    if closes is None or len(closes) < 6:
        return None, None, None, "below"
    ema = closes.ewm(span=4, adjust=False).mean()
    today_ema, yest_ema = float(ema.iloc[-1]), float(ema.iloc[-2])
    price, prev_close = float(closes.iloc[-1]), float(closes.iloc[-2])
    slope = "UP" if today_ema > yest_ema else "DN"
    if today_ema > yest_ema and prev_close < ema.iloc[-2] and price > today_ema:
        status = "reclaim"
    elif price > today_ema:
        status = "above"
    else:
        status = "below"
    return price, today_ema, slope, status


def cmd_reclaim(args=None):
    db = getattr(args, "db", None) or DB
    con = sqlite3.connect(db)
    tickers = sorted(r[0] for r in con.execute(
        "SELECT ticker FROM locker_room WHERE status='active' AND ticker != 'TEST'"
    ))
    con.close()

    now = datetime.now(ZoneInfo("America/Los_Angeles")).strftime("%Y-%m-%d %H:%M PT")
    print(f"\n{B}LOCKER 4 EMA STATUS — {now}{X}\n")
    print(f"  {'Ticker':<8}{'Price':>10}  {'4 EMA':>10}  {'Slope':>6}  Status")
    print("  " + "─" * 50)

    data = yf.download(tickers, period="20d", interval="1d",
                       progress=False, auto_adjust=True, group_by="ticker")

    reclaims = []
    for t in tickers:
        try:
            closes = data[t]["Close"].dropna() if len(tickers) > 1 else data["Close"].dropna()
        except Exception:
            closes = None
        price, ema, slope, status = _analyze(closes)
        label = {
            "reclaim": f"{G}✓ RECLAIM{X}",
            "above":   f"{Y}above{X}",
            "below":   f"{R}below{X}",
        }[status]
        if price is None:
            print(f"  {B}{t:<7}{X}  {R}no data{X}")
        else:
            print(f"  {B}{t:<7}{X} {price:>9.2f}  {ema:>10.2f}  {slope:>6}  {label}")
        if status == "reclaim":
            reclaims.append(t)

    print()
    if reclaims:
        print(f"  {G}{B}RECLAIM TRIGGERED:{X} {', '.join(reclaims)}")
    else:
        print(f"  {D}No reclaims. If regime is red, close it.{X}")
    print()
