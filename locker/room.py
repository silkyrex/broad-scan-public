"""Locker room commands: add, del, pull, show, refresh, enrich, stale, prospects, note, history."""
import json
import sqlite3
from datetime import date

from locker import _helpers as _h
from locker._helpers import (
    _push_to_droplet, _log_history, _add_to_db,
    _ema4_status, _batch_ema4, _batch_live_ema4,
)
from scanner import discord_router
from scanner.kb import capture as _ob1_capture
from scanner.prospect_signals import run as _prospect_signals_run, build_discord_messages as _prospect_signals_msgs


def cmd_add(args):
    ticker = args.ticker.upper()
    today = date.today().isoformat()
    source = getattr(args, "source", None) or "manual"
    sector = getattr(args, "sector", None) or ""
    notes = getattr(args, "notes", None) or ""
    bucket = getattr(args, "bucket", None) or "prospect"
    scan_signals_arg = getattr(args, "scan_signals", None) or "[]"
    conn = sqlite3.connect(_h.DB_PATH)
    sig_list = json.loads(scan_signals_arg) if scan_signals_arg != "[]" else None
    _add_to_db(conn, ticker, source=source, sector=sector, notes=notes,
               bucket=bucket, scan_signals=sig_list)
    conn.commit()
    conn.close()
    print(f"Added {ticker} to locker_room")
    _ob1_capture(
        f"Locker add: {ticker}\n"
        f"date={today} source={getattr(args,'source','manual')} "
        f"sector={getattr(args,'sector','')}\n"
        f"Agent: locker-cli add"
    )


def cmd_del(args):
    ticker = args.ticker.upper()
    today = date.today().isoformat()
    conn = sqlite3.connect(_h.DB_PATH)
    row = conn.execute(
        "SELECT id FROM locker_room WHERE ticker=? AND status='active'", (ticker,)
    ).fetchone()
    if not row:
        print(f"{ticker} not in active locker room")
        conn.close()
        return
    conn.execute(
        "UPDATE locker_room SET status='removed', removed_date=? WHERE ticker=?",
        (today, ticker)
    )
    _log_history(conn, ticker, "removed", detail={"reason": "manual_remove"})
    conn.commit()
    conn.close()
    print(f"Removed {ticker} from locker_room")
    _push_to_droplet()


def cmd_pull(args):
    """Fetch and store technicals: price, RSI D/W/M, 52w%, ATH%, SMAs."""
    ticker = args.ticker.upper() if args.ticker else None
    conn = sqlite3.connect(_h.DB_PATH)
    tickers = [ticker] if ticker else [
        r[0] for r in conn.execute(
            "SELECT ticker FROM locker_room WHERE status='active' ORDER BY ticker"
        ).fetchall()
    ]
    print(f"Pulling technicals for {len(tickers)} ticker(s)...")
    for t in tickers:
        try:
            import yfinance as yf, warnings
            warnings.filterwarnings("ignore")
            hist_d = yf.Ticker(t).history(period="1y")
            hist_w = yf.download(t, period="2y", interval="1wk", progress=False, auto_adjust=True)
            hist_m = yf.download(t, period="5y", interval="1mo", progress=False, auto_adjust=True)
            if hist_d.empty:
                print(f"  {t:<8}  SKIP (no data)")
                continue
            import pandas as pd

            def _to_series(df, fallback):
                if df.empty:
                    return fallback
                col = df["Close"].squeeze()
                return col.astype(float).dropna() if isinstance(col, pd.Series) else fallback

            closes_d = hist_d["Close"].astype(float).dropna()
            closes_w = _to_series(hist_w, closes_d)
            closes_m = _to_series(hist_m, closes_d)

            def rsi(closes, n=14):
                delta = closes.diff()
                gain = delta.clip(lower=0).ewm(com=n-1, adjust=False).mean()
                loss = (-delta.clip(upper=0)).ewm(com=n-1, adjust=False).mean()
                rs = gain / loss.replace(0, float("nan"))
                return round(float(100 - 100 / (1 + rs.iloc[-1])), 1)

            price    = round(float(closes_d.iloc[-1]), 2)
            high_52w = round(float(closes_d.tail(252).max()), 2)
            ath      = round(float(closes_d.max()), 2)
            w52_pct  = round((price - high_52w) / high_52w * 100, 1) if high_52w else None
            ath_pct  = round((price - ath) / ath * 100, 1) if ath else None
            rsi_d    = rsi(closes_d)
            rsi_w    = rsi(closes_w) if len(closes_w) >= 14 else None
            rsi_m    = rsi(closes_m) if len(closes_m) >= 14 else None

            def sma(closes, n):
                return round(float(closes.rolling(n).mean().iloc[-1]), 2) if len(closes) >= n else None

            conn.execute("""
                UPDATE locker_room SET
                  technical_updated=?, price=?, w52_high=?, w52_high_pct=?,
                  ath=?, ath_pct=?, rsi_d=?, rsi_w=?, rsi_m=?,
                  sma_10=?, sma_20=?, sma_50=?, sma_100=?, sma_200=?
                WHERE ticker=?
            """, (date.today().isoformat(), price, high_52w, w52_pct,
                  ath, ath_pct, rsi_d, rsi_w, rsi_m,
                  sma(closes_d, 10), sma(closes_d, 20), sma(closes_d, 50),
                  sma(closes_d, 100), sma(closes_d, 200), t))
            conn.commit()
            print(f"  {t:<8}  ${price}  52w%={w52_pct}  RSI D/W/M={rsi_d}/{rsi_w}/{rsi_m}")
        except Exception as e:
            print(f"  {t:<8}  FAILED: {e}")
    conn.close()


def _check_and_fire_clarity(conn, ticker: str, today: str, live_status: str) -> bool:
    """Fire CLARITY if: live price is above 4 EMA, most recent prior EOD was exit-d1, not fired today."""
    if live_status not in ("above", "reclaim"):
        return False
    prev_date = conn.execute(
        "SELECT MAX(signal_date) FROM eod_signals WHERE ticker=? AND signal_date<?",
        (ticker, today)
    ).fetchone()[0]
    if not prev_date:
        return False
    prev_toby = conn.execute(
        "SELECT toby_status FROM eod_signals WHERE ticker=? AND signal_date=?",
        (ticker, prev_date)
    ).fetchone()
    if not prev_toby or prev_toby[0] not in ("exit-d1", "exit-d2"):
        return False
    if conn.execute(
        "SELECT 1 FROM locker_history WHERE ticker=? AND event='clarity' AND date=?",
        (ticker, today)
    ).fetchone():
        return False
    _log_history(conn, ticker, "clarity",
                 reason="CLARITY: above 4 EMA after exit-d1 close -- confirm hold?",
                 detail={"prev_date": prev_date})
    try:
        discord_router.send_to("locker",
            f"✨ **CLARITY: {ticker}** — prior close exit-d1 ({prev_date}), live now above 4 EMA. Confirm hold?")
    except Exception:
        pass
    _ob1_capture(
        f"CLARITY: {ticker} — above 4 EMA after exit-d1 close on {prev_date}. "
        f"date={today} outcome=TBD\nAgent: broad-scan locker-clarity"
    )
    return True


def cmd_show(args):
    conn = sqlite3.connect(_h.DB_PATH)
    rows = conn.execute("""
        SELECT l.ticker, l.promoted_date, l.sector,
               p.entry_price, p.stop_price, p.shares, p.broker,
               p.size_type, p.setup, p.r_risk
        FROM locker_room l
        LEFT JOIN positions p ON p.ticker = l.ticker AND p.status = 'open'
        WHERE l.status = 'active'
        ORDER BY p.entry_price IS NULL, l.promoted_date DESC
    """).fetchall()

    if not rows:
        print("Locker Room is empty.")
        conn.close()
        return
    conn.close()

    tickers = [r[0] for r in rows]
    print(f"  fetching live EMA4 for {len(tickers)} ticker(s)...")
    live_ema4 = _batch_ema4(tickers)

    conn2 = sqlite3.connect(_h.DB_PATH)
    stored_status = {r[0]: r[1] for r in conn2.execute(
        "SELECT ticker, ema4_status FROM locker_room WHERE status='active'"
    ).fetchall()}
    conn2.close()

    EMA4_ICON = {
        "reclaim": "★ RECLAIM",
        "clarity": "✨ CLARITY",
        "above":   "↑ above  ",
        "below":   "↓ below  ",
        "unknown": "? ------  ",
    }

    in_pos   = [r for r in rows if r[3] is not None]
    watching = [r for r in rows if r[3] is None]

    def _ema4_display(ticker):
        if stored_status.get(ticker) == "clarity":
            return "clarity"
        return live_ema4.get(ticker, "unknown")

    def _print_row(r):
        ticker, promoted, sector, entry, stop, shares, broker, size_type, setup, r_risk = r
        ema4 = _ema4_display(ticker)
        age = (date.today() - date.fromisoformat(promoted)).days
        ema4_str = EMA4_ICON.get(ema4, "?")
        pos_str = ""
        if entry:
            pos_str = f"  {shares}sh @ ${entry:.2f}"
            if stop:
                pos_str += f"  stop ${stop:.2f}"
            if r_risk:
                pos_str += f"  R=${r_risk:.0f}"
            tags = []
            if size_type and size_type != "full":
                tags.append(size_type)
            if setup and setup != "4ema_reclaim":
                tags.append(setup)
            if tags:
                pos_str += f"  [{' '.join(tags)}]"
        stale_flag = "  ⚠" if age >= 14 and ema4 not in ("reclaim", "clarity") else ""
        print(f"  {ticker:<8}  {ema4_str}  {age}d  {(sector or '--'):<12}{pos_str}{stale_flag}")

    if in_pos:
        print(f"\nIn Position ({len(in_pos)})")
        for r in in_pos:
            _print_row(r)

    clarity_watch = [r for r in watching if _ema4_display(r[0]) == "clarity"]
    if clarity_watch:
        print(f"\n✨ CLARITY ({len(clarity_watch)})  — reclaimed after prior dip")
        for r in clarity_watch:
            _print_row(r)

    valid = [r for r in watching
             if _ema4_display(r[0]) in ("above", "reclaim")
             and _ema4_display(r[0]) != "clarity"]
    if valid:
        print(f"\nValid ({len(valid)})  — above 4 EMA")
        for r in valid:
            _print_row(r)

    below = [r for r in watching if _ema4_display(r[0]) == "below"]
    if below:
        print(f"\nWatch ({len(below)})  — below 4 EMA, no position")
        for r in below:
            _print_row(r)

    unk = [r for r in watching if _ema4_display(r[0]) == "unknown"]
    if unk:
        print(f"\nUnknown ({len(unk)})")
        for r in unk:
            _print_row(r)

    print()


def cmd_refresh(args):
    ticker = args.ticker.upper() if args.ticker else None
    live = getattr(args, "live", False)
    conn = sqlite3.connect(_h.DB_PATH)

    if ticker:
        rows = [(ticker,)]
    else:
        rows = conn.execute(
            "SELECT ticker FROM locker_room WHERE status='active' ORDER BY ticker"
        ).fetchall()

    mode = "live price" if live else "close price"
    today = date.today().isoformat()
    print(f"Refreshing EMA4 ({mode}) for {len(rows)} ticker(s)...")
    for (t,) in rows:
        old = conn.execute(
            "SELECT ema4_status FROM locker_room WHERE ticker=?", (t,)
        ).fetchone()

        eod_row = None if live else conn.execute(
            "SELECT ema_4e_pct, toby_status FROM eod_signals WHERE ticker=? AND signal_date=?",
            (t, today)
        ).fetchone()

        if eod_row and not live:
            ema4_pct, toby = eod_row
            status = {
                "reclaim": "reclaim",
                "hold":    "above",
                "exit-d1": "below",
                "exit-d2": "below",
            }.get(toby, "unknown")
            mode_used = "eod_signals cache"
        else:
            status = _ema4_status(t, live=live)
            mode_used = mode

        if live and _check_and_fire_clarity(conn, t, today, status):
            status = "clarity"
            print(f"  {t:<8}  ✨ CLARITY fired — Discord pinged")

        conn.execute(
            "UPDATE locker_room SET ema4_status=?, ema4_updated=? WHERE ticker=?",
            (status, date.today().isoformat(), t)
        )
        changed = old and old[0] != status
        flag = "  <- changed" if changed else ""
        cache_flag = " [cached]" if eod_row and not live else ""
        print(f"  {t:<8}  {status}{flag}{cache_flag}")
        if changed and status != "clarity":
            _log_history(conn, t, "ema4_change",
                         detail={"from": old[0], "to": status, "mode": mode})
            _ob1_capture(
                f"EMA4 status change: {t}\n"
                f"date={date.today().isoformat()} from={old[0]} to={status} mode={mode}\n"
                + ("Signal: RECLAIM -- 4 EMA reclaim is the P1 entry trigger\n" if status == "reclaim" else "")
                + "Agent: broad-scan locker-refresh"
            )
    conn.commit()
    conn.close()


def cmd_enrich(args):
    """Backfill sector/industry/market_cap/beta/short_pct_float from yfinance."""
    ticker = args.ticker.upper() if args.ticker else None
    conn = sqlite3.connect(_h.DB_PATH)

    if ticker:
        rows = [(ticker,)]
    else:
        rows = conn.execute(
            "SELECT ticker FROM locker_room WHERE status='active' AND sector IS NULL ORDER BY ticker"
        ).fetchall()

    print(f"Enriching {len(rows)} ticker(s)...")
    for (t,) in rows:
        try:
            import yfinance as yf
            info = yf.Ticker(t).info
            conn.execute("""
                UPDATE locker_room SET
                    sector=?, industry=?, market_cap=?, beta=?, short_pct_float=?
                WHERE ticker=?
            """, (
                info.get("sector"), info.get("industry"),
                info.get("marketCap"), info.get("beta"),
                info.get("shortPercentOfFloat"), t
            ))
            conn.commit()
            cap = f"${info.get('marketCap', 0)//1_000_000_000:.0f}B" if info.get("marketCap") else "--"
            print(f"  {t:<8}  {info.get('sector', '--'):<20}  {cap}")
        except Exception as e:
            print(f"  {t:<8}  FAILED: {e}")

    conn.close()


def cmd_stale(args):
    threshold = args.days
    conn = sqlite3.connect(_h.DB_PATH)
    rows = conn.execute("""
        SELECT ticker, promoted_date, ema4_status, sector
        FROM locker_room WHERE status='active'
        ORDER BY promoted_date
    """).fetchall()
    conn.close()

    stale = []
    for ticker, promoted, ema4, sector in rows:
        age = (date.today() - date.fromisoformat(promoted)).days
        if age >= threshold and ema4 != "reclaim":
            stale.append((ticker, age, ema4, sector))

    if not stale:
        print(f"No stale names ({threshold}d+ without reclaim).")
        return

    print(f"\nStale ({threshold}d+, no reclaim) -- {len(stale)} names\n")
    for ticker, age, ema4, sector in stale:
        print(f"  {ticker:<8}  {age}d  {(ema4 or '--'):<10}  {sector or '--'}")


def cmd_prospects(args):
    """Show active prospects with live EMA4 + write timestamped snapshot to intraday_signals."""
    from datetime import datetime, timezone

    conn = sqlite3.connect(_h.DB_PATH)

    conn.execute("""
        CREATE TABLE IF NOT EXISTS intraday_signals (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            ts          TEXT NOT NULL,
            ticker      TEXT NOT NULL,
            source      TEXT NOT NULL DEFAULT 'prospect',
            price       REAL,
            tape        TEXT,
            ema_4e_pct  REAL,
            rsi_d       REAL,
            rsi_w       REAL,
            toby_status TEXT
        )
    """)
    conn.execute("""
        CREATE INDEX IF NOT EXISTS idx_intraday_ticker_ts
        ON intraday_signals (ticker, ts)
    """)

    tickers = [r[0] for r in conn.execute(
        "SELECT ticker FROM prospects WHERE status='active' ORDER BY ticker"
    ).fetchall()]

    if not tickers:
        print("No active prospects.")
        conn.close()
        return

    placeholders = ",".join("?" * len(tickers))
    rsi_cache = {}
    for row in conn.execute(f"""
        SELECT e.ticker, e.rsi_d, e.rsi_w
        FROM eod_signals e
        INNER JOIN (
            SELECT ticker, MAX(signal_date) AS max_date
            FROM eod_signals WHERE ticker IN ({placeholders})
            GROUP BY ticker
        ) latest ON e.ticker = latest.ticker AND e.signal_date = latest.max_date
    """, tickers).fetchall():
        rsi_cache[row[0]] = {"rsi_d": row[1], "rsi_w": row[2]}

    print(f"  fetching live EMA4 for {len(tickers)} prospect(s)...")
    live = _batch_live_ema4(tickers)

    now    = datetime.now()
    ts_utc = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    hhmm   = now.strftime("%H:%M")

    rows_display = []
    rows_write   = []

    for t in tickers:
        d   = live.get(t)
        rsi = rsi_cache.get(t, {})
        rsi_d = rsi.get("rsi_d")
        rsi_w = rsi.get("rsi_w")
        if d:
            rows_display.append((t, d["tape"], d["price"], d["ema_4e_pct"], rsi_d, rsi_w, d["status"]))
            rows_write.append((ts_utc, t, "prospect", d["price"], d["tape"], d["ema_4e_pct"], rsi_d, rsi_w, d["status"]))
        else:
            rows_display.append((t, "?", None, None, rsi_d, rsi_w, "unknown"))
            rows_write.append((ts_utc, t, "prospect", None, None, None, rsi_d, rsi_w, "unknown"))

    rows_display.sort(key=lambda r: (0 if r[1] == "green" else 1, -(r[3] or -999)))

    print(f"\n─── PROSPECTS (live {hhmm} PT) {'─' * 38}")
    print(f"  {'TICKER':<8}  {'TAPE':<6}  {'PRICE':>8}  {'EMA4%':>7}  {'RSI-D':>6}  {'RSI-W':>6}  STATUS")
    print(f"  {'─'*8}  {'─'*6}  {'─'*8}  {'─'*7}  {'─'*6}  {'─'*6}  {'─'*8}")

    for t, tape, price, ema_pct, rsi_d, rsi_w, status in rows_display:
        price_str = f"${price:>7.2f}" if price is not None else "       --"
        ema_str   = f"{ema_pct:>+6.1f}%" if ema_pct is not None else "      --"
        rsi_d_str = f"{rsi_d:>5.1f}" if rsi_d is not None else "    --"
        rsi_w_str = f"{rsi_w:>5.1f}" if rsi_w is not None else "    --"
        tape_str  = (tape or "?").upper()[:5]
        print(f"  {t:<8}  {tape_str:<6}  {price_str}  {ema_str}  {rsi_d_str}  {rsi_w_str}  {status}")

    print()

    conn.executemany("""
        INSERT INTO intraday_signals (ts, ticker, source, price, tape, ema_4e_pct, rsi_d, rsi_w, toby_status)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
    """, rows_write)
    conn.commit()
    conn.close()
    print(f"  snapshot written -> intraday_signals ({ts_utc})")


def cmd_note(args):
    ticker = args.ticker.upper()
    conn = sqlite3.connect(_h.DB_PATH)
    row = conn.execute(
        "SELECT id, notes FROM locker_room WHERE ticker=? AND status='active'", (ticker,)
    ).fetchone()
    if not row:
        print(f"{ticker} not found in active locker room")
        conn.close()
        return
    old_note = row[1]
    conn.execute("UPDATE locker_room SET notes=? WHERE ticker=? AND status='active'",
                 (args.text, ticker))
    _log_history(conn, ticker, "note_updated",
                 detail={"old": old_note, "new": args.text})
    conn.commit()
    conn.close()
    print(f"  {ticker} note updated:")
    print(f"  {args.text}")
    _push_to_droplet()


def cmd_history(args):
    ticker = args.ticker.upper()
    conn = sqlite3.connect(_h.DB_PATH)
    rows = conn.execute(
        "SELECT date, event, reason, detail FROM locker_history WHERE ticker=? ORDER BY date DESC LIMIT 20",
        (ticker,)
    ).fetchall()
    conn.close()

    if not rows:
        print(f"No history for {ticker}")
        return

    print(f"\n{ticker} history")
    print("-" * 50)
    for dt, event, reason, detail in rows:
        line = f"  {dt}  {event:<16}"
        if reason:
            line += f"  {reason}"
        if detail:
            line += f"  {json.loads(detail)}"
        print(line)
