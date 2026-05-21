#!/usr/bin/env python3
"""pos [TICKER] — position ledger with live P&L.

Usage:
  pos           — full book: all open positions with live P&L
  pos TICKER    — single position detail
"""

import os
import sqlite3
import sys
from datetime import date
from pathlib import Path

DB_PATH = Path(os.environ.get("BROAD_SCAN_DB", Path(__file__).parent.parent / "broad-scan.db"))

USE_COLOR = sys.stdout.isatty()
G    = "\033[32m" if USE_COLOR else ""
R    = "\033[31m" if USE_COLOR else ""
Y    = "\033[33m" if USE_COLOR else ""
DIM  = "\033[2m"  if USE_COLOR else ""
X    = "\033[0m"  if USE_COLOR else ""
BOLD = "\033[1m"  if USE_COLOR else ""

BOX_W = 44


def _vlen(s: str) -> int:
    import re
    return len(re.sub(r"\033\[[0-9;]*m", "", s))


def _box(title: str, lines: list) -> str:
    inner_w = max(BOX_W, max((_vlen(l) for l in lines), default=0), len(title) + 2)
    top = "┌─ " + title + " " + "─" * max(0, inner_w - len(title) - 1) + "┐"
    bot = "└" + "─" * (inner_w + 2) + "┘"
    rows = ["│ " + l + " " * (inner_w - _vlen(l)) + " │" for l in lines]
    return "\n".join([top] + rows + [bot])


def _phase(entry: float, stop: float | None) -> str:
    if stop is None:
        return "?"
    if stop > entry * 1.005:
        return "3"
    if stop >= entry * 0.995:
        return "2"
    return "1"


def _phase_label(entry: float, stop: float | None) -> str:
    return {1: "hard stop", 2: "breakeven", 3: "trail"}.get(int(_phase(entry, stop) or 0), "?")


def _pnl_color(val: float) -> str:
    return G if val >= 0 else R


def _draw_position(entry_px: float, live_px: float, stop_px: float, shares: float, entry_date: str) -> str:
    HEIGHT = 10
    top = max(entry_px, live_px) * 1.008
    bot = stop_px * 0.992
    rng = top - bot or 1

    def row(px: float) -> int:
        return max(0, min(HEIGHT, int((top - px) / rng * HEIGHT)))

    entry_row = row(entry_px)
    live_row  = row(live_px)
    stop_row  = row(stop_px)

    pnl_val     = (live_px - entry_px) * shares
    pnl_pct     = (live_px - entry_px) / entry_px * 100
    cushion_pct = (live_px - stop_px) / stop_px * 100
    max_loss    = abs(entry_px - stop_px) * shares
    mid_row     = (live_row + stop_row) // 2

    pnl_sign = "+" if pnl_val >= 0 else ""
    pnl_c    = G if pnl_val >= 0 else R
    cush_c   = G if cushion_pct >= 5 else (Y if cushion_pct >= 2 else R)
    W = 9

    labels: dict = {}
    labels[entry_row] = (f"${entry_px:.2f}", f"ENTRY    ({entry_date})")
    labels[live_row]  = (f"${live_px:.2f}",  f"NOW      {pnl_c}{pnl_sign}${abs(pnl_val):,.0f} ({pnl_sign}{pnl_pct:.1f}%){X}")
    labels[stop_row]  = (f"${stop_px:.2f}",  f"STOP     max loss ${max_loss:,.0f}")

    if live_row == entry_row:
        labels[entry_row] = (f"${entry_px:.2f}", f"ENTRY / NOW  {pnl_c}{pnl_sign}${abs(pnl_val):,.0f}{X}")

    out = []
    for i in range(HEIGHT + 1):
        if i in labels:
            price, label = labels[i]
            marker = "◄──" if i == live_row and i != entry_row else "───"
            out.append(f"  {price:>{W}}  {marker} {label}")
        elif i == mid_row:
            out.append(f"  {'':>{W}}  │   {cush_c}{cushion_pct:.1f}% above stop{X}")
        else:
            out.append(f"  {'':>{W}}  │")
    return "\n".join(out)


def _fetch_prices(tickers: list) -> dict:
    """Batch-fetch last close prices via yfinance."""
    if not tickers:
        return {}
    try:
        import warnings
        import yfinance as yf
        warnings.filterwarnings("ignore")
        if len(tickers) == 1:
            hist = yf.Ticker(tickers[0]).history(period="5d")
            if hist.empty:
                return {}
            return {tickers[0]: float(hist["Close"].iloc[-1])}
        data = yf.download(tickers, period="5d", progress=False, auto_adjust=True)
        closes = data["Close"]
        result = {}
        for t in tickers:
            try:
                col = closes[t] if hasattr(closes, "columns") and t in closes.columns else closes.squeeze()
                result[t] = float(col.dropna().iloc[-1])
            except Exception:
                pass
        return result
    except Exception:
        return {}


def _fetch_live_price(ticker: str):
    """Fetch intraday price for a single ticker."""
    try:
        import yfinance as yf
        info = yf.Ticker(ticker).info
        return info.get("currentPrice") or info.get("regularMarketPrice")
    except Exception:
        return None


def _open_positions(ticker=None) -> list:
    conn = sqlite3.connect(DB_PATH)
    if ticker:
        rows = conn.execute(
            """SELECT ticker, entry_date, entry_price, shares, stop_price,
                      broker, size_type, setup, r_risk, notes
               FROM positions WHERE ticker=? AND status='open'
               ORDER BY entry_date""",
            (ticker.upper(),),
        ).fetchall()
    else:
        rows = conn.execute(
            """SELECT ticker, entry_date, entry_price, shares, stop_price,
                      broker, size_type, setup, r_risk, notes
               FROM positions WHERE status='open'
               ORDER BY entry_date"""
        ).fetchall()
    conn.close()
    return rows


def cmd_status():
    rows = _open_positions()
    if not rows:
        print("No open positions.")
        return

    tickers = [r[0] for r in rows]
    print(f"  fetching prices for {', '.join(tickers)}...")
    prices = _fetch_prices(tickers)

    today = date.today()
    total_cost = 0.0
    total_pnl = 0.0
    lines = []

    for ticker, entry_date, entry_price, shares, stop_price, broker, size_type, setup, r_risk, notes in rows:
        days_held = (today - date.fromisoformat(entry_date)).days
        live = prices.get(ticker)
        cost = entry_price * shares
        total_cost += cost

        if live:
            pnl_val = (live - entry_price) * shares
            pnl_pct = (live - entry_price) / entry_price * 100
            total_pnl += pnl_val
            pc = _pnl_color(pnl_val)
            pnl_str = f"{pc}{pnl_val:+,.0f} ({pnl_pct:+.1f}%){X}"
            live_str = f"${live:.2f}"
        else:
            pnl_str = f"{DIM}--{X}"
            live_str = "--"

        phase = _phase(entry_price, stop_price)

        if stop_price:
            stop_str = f"stop ${stop_price:.2f}"
            if live:
                cushion_pct = (live - stop_price) / stop_price * 100
                cc = G if cushion_pct >= 5 else (Y if cushion_pct >= 2 else R)
                stop_str += f"  {cc}{cushion_pct:.1f}% cushion{X}"
        else:
            stop_str = "stop --"

        tags = []
        if broker and broker != "alpaca":
            tags.append(broker)
        if size_type and size_type != "full":
            tags.append(size_type)
        if setup and setup not in ("4ema_reclaim", "manual"):
            tags.append(setup)
        tag_str = f"  {DIM}[{' '.join(tags)}]{X}" if tags else ""

        line1 = (
            f"  {BOLD}{ticker:<8}{X}  {shares:.0f}sh @ ${entry_price:.2f}  "
            f"{stop_str}  {DIM}{days_held}d  p{phase}{X}{tag_str}"
        )
        line2 = f"  {'':8}  NOW {live_str}  P&L {pnl_str}"
        lines.append((line1, line2))

    book_pct = total_pnl / total_cost * 100 if total_cost else 0
    pc = _pnl_color(total_pnl)
    header = (
        f"{BOLD}BOOK{X} — {today.strftime('%a %b %d %Y')} | "
        f"{len(rows)} open | unrealized: {pc}{total_pnl:+,.0f} ({book_pct:+.1f}%){X}"
    )
    print()
    print(header)
    print()
    for l1, l2 in lines:
        print(l1)
        print(l2)
        print()


def cmd_ticker(ticker: str):
    rows = _open_positions(ticker)
    if not rows:
        print(f"No open position for {ticker.upper()}")
        return

    t, entry_date, entry_price, shares, stop_price, broker, size_type, setup, r_risk, notes = rows[0]
    live = _fetch_live_price(t)

    today = date.today()
    days_held = (today - date.fromisoformat(entry_date)).days

    # ASCII price chart (requires live price + stop)
    if live is not None and stop_price is not None:
        print()
        print(_draw_position(entry_price, live, stop_price, shares, entry_date))
        print()

    # OPEN box
    phase_n = _phase(entry_price, stop_price)
    phase_str = f"  (Phase {phase_n} / {_phase_label(entry_price, stop_price)})" if stop_price else ""
    lines = [
        f"Ticker   {t}  {shares:.0f} sh  {days_held}d held",
        f"Broker   {broker or '--'}  [{size_type or '--'}]  setup: {setup or '--'}",
        f"Entry    ${entry_price:,.2f}  ({entry_date})",
    ]
    if live is not None:
        pnl_val = (live - entry_price) * shares
        pnl_pct = (live - entry_price) / entry_price * 100
        pc = _pnl_color(pnl_val)
        sign = "+" if pnl_val >= 0 else "-"
        lines.append(f"Live     ${live:,.2f}")
        lines.append(f"P&L      {pc}{sign}${abs(pnl_val):,.0f} ({sign}{abs(pnl_pct):.1f}%){X}")
    if stop_price is not None:
        max_loss = abs(entry_price - stop_price) * shares
        lines.append(f"Stop     ${stop_price:,.2f}{phase_str}")
        lines.append(f"Max loss ${max_loss:,.0f}")
        if live is not None:
            dist = live - stop_price
            dist_pct = dist / live * 100
            cc = G if dist_pct >= 5 else (Y if dist_pct >= 2 else R)
            lines.append(f"To stop  {cc}${abs(dist * shares):,.0f} ({dist_pct:.1f}% below live){X}")
    if r_risk:
        lines.append(f"R        ${r_risk:,.0f}")
    if notes:
        lines.append(f"Notes    {notes}")

    print(_box("OPEN", lines))
    print()


def main():
    if len(sys.argv) > 1:
        cmd_ticker(sys.argv[1])
    else:
        cmd_status()


if __name__ == "__main__":
    main()
