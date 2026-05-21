"""Shared constants, DB helpers, and EMA4 utilities for the locker module."""
import json
import os
import sqlite3
from datetime import date, timedelta
from pathlib import Path

_DEFAULT_DB = Path(os.environ.get("BROAD_SCAN_DB", Path(__file__).parent.parent / "broad-scan.db"))
DB_PATH = _DEFAULT_DB
DROPLET = "root@your-vps.example.com"
DROPLET_DB = "/opt/broad-scan/broad-scan.db"
_SIZING_CFG = Path(__file__).parent.parent / "config" / "sizing.json"

VALID_SETUPS = ["4ema_reclaim", "day2_entry", "add_on", "manual"]
VALID_SIZES  = ["full", "pilot"]


def _pyramid_check(conn: sqlite3.Connection, ticker: str, new_shares: float, new_entry: float) -> bool:
    """Warn + prompt if position would exceed deployed cap. Returns True = proceed."""
    import sys
    if not _SIZING_CFG.exists():
        return True
    cfg = json.loads(_SIZING_CFG.read_text())
    equity = float(cfg.get("equity", 0))
    pyramid_max = float(cfg.get("pyramid_max_pct", 25))
    if equity <= 0:
        print(f"  ⚠ sizing.json equity is {equity} -- cap check skipped. Run: locker sizing equity AMOUNT")
        return True
    existing_deployed = conn.execute(
        "SELECT COALESCE(SUM(shares * entry_price), 0) FROM positions WHERE ticker=? AND status='open'",
        (ticker,)
    ).fetchone()[0]
    total_deployed = existing_deployed + new_shares * new_entry
    total_pct = total_deployed / equity * 100
    if total_pct > pyramid_max:
        print(f"  ⚠ Pyramid cap: {ticker} {total_pct:.1f}% deployed (cap {pyramid_max:.0f}%) -- ${total_deployed:,.0f} of ${equity:,.0f}")
        if not sys.stdin.isatty():
            print("  Non-interactive -- blocking entry.")
            return False
        try:
            return input("  Proceed anyway? [y/N] ").strip().lower() == "y"
        except EOFError:
            return False
    return True


def _wash_sale_check(conn: sqlite3.Connection, ticker: str) -> "dict | None":
    from datetime import datetime
    cutoff = (date.today() - timedelta(days=30)).isoformat()
    row = conn.execute(
        """SELECT exit_date, (exit_price - entry_price) * shares AS pnl
           FROM positions
           WHERE ticker=? AND status='closed' AND exit_date >= ?
             AND (exit_price - entry_price) * shares < 0
           ORDER BY exit_date DESC LIMIT 1""",
        (ticker, cutoff)
    ).fetchone()
    if not row:
        return None
    exit_dt = datetime.strptime(row[0], "%Y-%m-%d")
    days_ago = (datetime.now() - exit_dt).days
    return {
        "exit_date": row[0],
        "loss": abs(row[1]),
        "days_ago": days_ago,
        "days_left": max(0, 30 - days_ago),
    }


def _push_to_droplet():
    import subprocess
    print("  pushing DB to VPS...")
    result = subprocess.run(
        ["scp", str(DB_PATH), f"{DROPLET}:{DROPLET_DB}"],
        capture_output=True, text=True
    )
    if result.returncode == 0:
        print("  pushed.")
    else:
        print(f"  push failed: {result.stderr.strip()}")


def cmd_sync(args):
    """Pull VPS DB → local (full replace + migrate). Morning ritual before locker show."""
    import subprocess
    from db.migrate import run_migrations
    print("  pulling DB from VPS...")
    result = subprocess.run(
        ["scp", f"{DROPLET}:{DROPLET_DB}", str(DB_PATH)],
        capture_output=True, text=True,
    )
    if result.returncode != 0:
        print(f"  pull failed: {result.stderr.strip()}")
        return
    print("  pulled. applying pending migrations...")
    run_migrations()
    print("  done. local DB is now current with VPS.")


def _ema4_status(ticker, live=False):
    """Return 'reclaim' | 'above' | 'below' | 'unknown' via yfinance."""
    try:
        import yfinance as yf
        t = yf.Ticker(ticker)
        hist = t.history(period="15d")
        if hist.empty or len(hist) < 5:
            return "unknown"
        closes = hist["Close"].astype(float)
        ema4 = closes.ewm(span=4, adjust=False).mean()
        curr_ema = float(ema4.iloc[-1])
        prev_ema = float(ema4.iloc[-2])
        prev_close = float(closes.iloc[-2])
        if live:
            info = t.info
            price = info.get("currentPrice") or info.get("regularMarketPrice") or float(closes.iloc[-1])
        else:
            price = float(closes.iloc[-1])
        if prev_close < prev_ema and price > curr_ema:
            return "reclaim"
        elif price > curr_ema:
            return "above"
        else:
            return "below"
    except Exception:
        return "unknown"


def _log_history(conn, ticker, event, reason=None, detail=None):
    conn.execute(
        "INSERT INTO locker_history (ticker, event, date, reason, detail) VALUES (?, ?, ?, ?, ?)",
        (ticker, event, date.today().isoformat(), reason,
         json.dumps(detail) if detail else None)
    )


def _batch_ema4(tickers):
    """Batch-fetch 15d of history for all tickers, return {ticker: status}."""
    try:
        import yfinance as yf
        import warnings
        warnings.filterwarnings("ignore")
        if not tickers:
            return {}
        data = yf.download(tickers, period="15d", progress=False, auto_adjust=True)
        closes = data["Close"]
        if hasattr(closes, "columns") and len(tickers) == 1 and tickers[0] not in closes.columns:
            closes.columns = tickers
        result = {}
        for t in tickers:
            try:
                s = closes[t].dropna().astype(float)
                if len(s) < 5:
                    result[t] = "unknown"
                    continue
                ema4 = s.ewm(span=4, adjust=False).mean()
                price      = float(s.iloc[-1])
                prev_close = float(s.iloc[-2])
                prev_ema   = float(ema4.iloc[-2])
                curr_ema   = float(ema4.iloc[-1])
                if prev_close < prev_ema and price > curr_ema:
                    result[t] = "reclaim"
                elif price > curr_ema:
                    result[t] = "above"
                else:
                    result[t] = "below"
            except Exception:
                result[t] = "unknown"
        return result
    except Exception:
        return {t: "unknown" for t in tickers}


def _batch_live_ema4(tickers):
    """Like _batch_ema4 but fetches today's intraday price for live tape/ema_4e_pct.
    Returns {ticker: {"price", "ema4", "ema_4e_pct", "tape", "status"}} or None per ticker.
    """
    try:
        import yfinance as yf
        import warnings
        warnings.filterwarnings("ignore")
        if not tickers:
            return {}

        hist      = yf.download(tickers, period="15d", progress=False, auto_adjust=True)
        live_data = yf.download(tickers, period="2d",  progress=False, auto_adjust=True)

        def _closes(data):
            c = data["Close"]
            if hasattr(c, "columns") and len(tickers) == 1 and tickers[0] not in c.columns:
                c = c.copy()
                c.columns = tickers
            return c

        hist_c = _closes(hist)
        live_c = _closes(live_data)
        alpha  = 2 / (4 + 1)
        result = {}

        for t in tickers:
            try:
                s  = hist_c[t].dropna().astype(float) if hasattr(hist_c, "columns") else hist_c.dropna().astype(float)
                sl = live_c[t].dropna().astype(float) if hasattr(live_c, "columns") else live_c.dropna().astype(float)

                if len(s) < 5:
                    result[t] = None
                    continue

                ema4_series = s.ewm(span=4, adjust=False).mean()
                prev_close  = float(s.iloc[-1])
                prev_ema    = float(ema4_series.iloc[-1])
                live_price  = float(sl.iloc[-1]) if len(sl) >= 1 else prev_close
                curr_ema    = prev_ema * (1 - alpha) + live_price * alpha
                ema_4e_pct  = round((live_price - curr_ema) / curr_ema * 100, 2)
                tape        = "green" if live_price > curr_ema else "red"

                if prev_close < prev_ema and live_price > curr_ema:
                    status = "reclaim"
                elif live_price > curr_ema:
                    status = "above"
                else:
                    status = "below"

                result[t] = {
                    "price":      round(live_price, 2),
                    "ema4":       round(curr_ema, 2),
                    "ema_4e_pct": ema_4e_pct,
                    "tape":       tape,
                    "status":     status,
                }
            except Exception:
                result[t] = None

        return result
    except Exception:
        return {t: None for t in tickers}


def _add_to_db(conn, ticker, source="auto", sector="", notes="",
               bucket="prospect", scan_signals=None, lookback_days=7):
    """Insert or update locker_room. Does not commit — caller must commit."""
    today    = date.today().isoformat()
    sig_json = json.dumps(scan_signals) if scan_signals else "[]"
    existing = conn.execute(
        "SELECT id FROM locker_room WHERE ticker=? AND status='active'", (ticker,)
    ).fetchone()
    if existing:
        conn.execute(
            "UPDATE locker_room SET source=?, sector=?, notes=?, bucket=?, scan_signals=? WHERE id=?",
            (source, sector, notes, bucket, sig_json, existing[0])
        )
    else:
        conn.execute("""
            INSERT INTO locker_room
              (ticker, promoted_date, added, source, sector, notes, status, bucket, scan_signals)
            VALUES (?, ?, ?, ?, ?, ?, 'active', ?, ?)
        """, (ticker, today, today, source, sector, notes, bucket, sig_json))
    if not scan_signals:
        label_map = {
            "rsi_daily": "RSI-D", "rsi_weekly": "RSI-W", "rsi_monthly": "RSI-M",
            "52w_high": "52w-high", "ath": "ATH", "12w_high": "12w-high",
        }
        scan_rows = conn.execute(
            f"""SELECT DISTINCT scan_type FROM scans
               WHERE ticker=? AND scan_date >= date('now','-{lookback_days} days')
                 AND is_new_signal=1
                 AND scan_type IN ('rsi_daily','rsi_weekly','rsi_monthly','52w_high','ath','12w_high')
               ORDER BY scan_date DESC""",
            (ticker,)
        ).fetchall()
        auto_signals = [label_map[r[0]] for r in scan_rows if r[0] in label_map]
        if auto_signals:
            conn.execute(
                "UPDATE locker_room SET scan_signals=? WHERE ticker=? AND status='active'",
                (json.dumps(auto_signals), ticker)
            )
    _log_history(conn, ticker, "added", detail={"source": source, "sector": sector})
