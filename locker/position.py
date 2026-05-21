"""Position commands: open, buy, close, stop, scale, refresh-positions, clarity."""
import json
import sqlite3
from datetime import date
from pathlib import Path

from locker import _helpers as _h
from locker._helpers import (
    _push_to_droplet, _log_history, _pyramid_check, _wash_sale_check,
    _batch_live_ema4,
)
from locker.room import _check_and_fire_clarity
from scanner import discord_router
from scanner.kb import capture as _ob1_capture
from scanner.prospect_signals import run as _prospect_signals_run, build_discord_messages as _prospect_signals_msgs


def cmd_open_pos(args):
    ticker = args.ticker.upper()
    conn = sqlite3.connect(_h.DB_PATH)
    lr = conn.execute(
        "SELECT id FROM locker_room WHERE ticker=? AND status='active'", (ticker,)
    ).fetchone()
    if not lr:
        in_prospects = conn.execute(
            "SELECT id FROM prospects WHERE ticker=? AND status='active'", (ticker,)
        ).fetchone()
        hint = " (in prospects -- run `locker add TICKER SECTOR` first)" if in_prospects else " -- add with `locker add TICKER SECTOR`"
        print(f"{ticker} not in active locker room{hint}")
        conn.close()
        return
    existing = conn.execute(
        "SELECT id FROM positions WHERE ticker=? AND status='open'", (ticker,)
    ).fetchone()
    if existing:
        print(f"{ticker} already has an open position -- use `locker scale` to add shares")
        conn.close()
        return
    if not _pyramid_check(conn, ticker, args.shares, args.entry):
        conn.close()
        return
    broker    = getattr(args, "broker", None) or "alpaca"
    size_type = getattr(args, "size", "full")
    setup     = getattr(args, "setup", "4ema_reclaim")
    r_risk    = round(args.shares * (args.entry - args.stop), 2) if args.stop else None
    conn.execute(
        """INSERT INTO positions
           (ticker, locker_room_id, broker, entry_date, entry_price, shares,
            stop_price, size_type, setup, r_risk, status)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'open')""",
        (ticker, lr[0], broker, date.today().isoformat(),
         args.entry, args.shares, args.stop, size_type, setup, r_risk)
    )
    _log_history(conn, ticker, "position_opened", detail={
        "entry": args.entry, "stop": args.stop, "shares": args.shares,
        "broker": broker, "size_type": size_type, "setup": setup,
        "r_risk": r_risk
    })
    conn.commit()
    conn.close()
    r_str = f"  R=${r_risk:.0f}" if r_risk else ""
    print(f"  {ticker} opened  {args.shares}sh @ ${args.entry:.2f}  stop ${args.stop:.2f}  [{size_type}] [{setup}]{r_str}  [{broker}]")
    _push_to_droplet()
    _ob1_capture(
        f"Position opened: {ticker}\n"
        f"date={date.today().isoformat()} entry=${args.entry:.2f} stop=${args.stop:.2f} "
        f"shares={args.shares} size={size_type} setup={setup} broker={broker}"
        + (f" R=${r_risk:.0f}" if r_risk else "") + "\n"
        f"Agent: broad-scan open-position"
    )


def cmd_buy(args):
    import re
    import subprocess
    import sys as _sys
    import yfinance as yf
    ticker = args.ticker.upper()

    # ── 1. Regime + equity ──────────────────────────────────────────────────
    cfg = json.loads(_h._SIZING_CFG.read_text()) if _h._SIZING_CFG.exists() else {}
    equity = float(cfg.get("equity", 0))
    entry_pcts = cfg.get("regime_entry_pct", {"BULL": 20, "CHOP": 15, "BEAR": 10})

    base_dir = Path(__file__).parent.parent
    try:
        res = subprocess.run(
            [_sys.executable, "-W", "ignore", "bin/regime-check.py"],
            capture_output=True, text=True, cwd=base_dir,
        )
        m = re.search(r"(BULL|CHOP|BEAR)", res.stdout)
        regime = m.group(1) if m else "UNKNOWN"
    except Exception:
        regime = "UNKNOWN"

    entry_pct   = entry_pcts.get(regime, 10)
    pos_dollars = int(equity * entry_pct / 100) if equity > 0 else 0

    # ── 2. Entry price ──────────────────────────────────────────────────────
    if args.entry is not None:
        price = args.entry
    else:
        try:
            h = yf.Ticker(ticker).history(period="2d")
            price = round(float(h["Close"].iloc[-1]), 2)
        except Exception:
            print(f"  price fetch failed for {ticker} -- use --entry PRICE")
            return

    # ── 3. Shares ───────────────────────────────────────────────────────────
    shares = args.shares if args.shares is not None else (
        round(pos_dollars / price) if price and pos_dollars else None
    )

    # ── 4. Sizing banner ────────────────────────────────────────────────────
    print()
    if equity > 0:
        print(f"  Regime: {regime}")
        print(f"  Equity: ${equity:,.0f} x {entry_pct}% = ${pos_dollars:,}")
        if shares:
            print(f"  {ticker}:   ${price:.2f}  ->  {shares:.0f} sh")
    else:
        print(f"  sizing.json equity not set -- run: locker sizing equity AMOUNT")
        if args.shares is None:
            return

    if shares is None:
        print("  Cannot calculate shares -- pass --shares N")
        return

    print()

    # ── 5. Wash sale gate ───────────────────────────────────────────────────
    conn = sqlite3.connect(_h.DB_PATH)
    wash = _wash_sale_check(conn, ticker)
    if wash:
        print(f"  WARNING: WASH SALE RISK -- {ticker}")
        print(f"  Sold at ${wash['loss']:.2f} loss {wash['days_ago']}d ago ({wash['exit_date']})")
        print(f"  {wash['days_left']}d remaining in 30-day window")
        print()
        try:
            confirm = input("  Enter anyway? [y/N] ").strip().lower()
        except EOFError:
            confirm = ""
        if confirm not in ("y", "yes"):
            print("  Aborted.")
            conn.close()
            return

    # ── 6. Stop price ───────────────────────────────────────────────────────
    stop = args.stop
    if stop is None:
        try:
            raw = input("  Stop price: ").strip()
            stop = float(raw) if raw else None
        except (EOFError, ValueError):
            stop = None

    # ── 7. Confirm ──────────────────────────────────────────────────────────
    broker    = getattr(args, "broker", None) or "alpaca"
    size_type = getattr(args, "size", "full")
    setup     = getattr(args, "setup", "4ema_reclaim")
    r_risk    = round(shares * (price - stop), 2) if stop else None
    r_str     = f"  R=${r_risk:.0f}" if r_risk else ""
    stop_str  = f"${stop:.2f}" if stop else "--"
    print()
    print(f"  {shares:.0f} sh {ticker} @ ${price:.2f}, stop {stop_str}{r_str} -- proceed? [y/N] ", end="", flush=True)
    try:
        go = input().strip().lower()
    except EOFError:
        go = ""
    if go not in ("y", "yes"):
        print("  Aborted.")
        conn.close()
        return

    # ── 8. Locker room gate ─────────────────────────────────────────────────
    lr = conn.execute(
        "SELECT id FROM locker_room WHERE ticker=? AND status='active'", (ticker,)
    ).fetchone()
    if not lr:
        in_prospects = conn.execute(
            "SELECT id FROM prospects WHERE ticker=? AND status='active'", (ticker,)
        ).fetchone()
        hint = " (in prospects -- run `locker add TICKER SECTOR` first)" if in_prospects else " -- add with `locker add TICKER SECTOR`"
        print(f"  {ticker} not in active locker room{hint}")
        conn.close()
        return

    existing = conn.execute(
        "SELECT id FROM positions WHERE ticker=? AND status='open'", (ticker,)
    ).fetchone()
    if existing:
        print(f"  {ticker} already has an open position -- use `locker scale` to add shares")
        conn.close()
        return

    if not _pyramid_check(conn, ticker, shares, price):
        conn.close()
        return

    # ── 9. Write ────────────────────────────────────────────────────────────
    conn.execute(
        """INSERT INTO positions
           (ticker, locker_room_id, broker, entry_date, entry_price, shares,
            stop_price, size_type, setup, r_risk, status)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'open')""",
        (ticker, lr[0], broker, date.today().isoformat(),
         price, shares, stop, size_type, setup, r_risk)
    )
    _log_history(conn, ticker, "position_opened", detail={
        "entry": price, "stop": stop, "shares": shares,
        "broker": broker, "size_type": size_type, "setup": setup,
        "r_risk": r_risk, "source": "buy-cmd",
    })
    conn.commit()
    conn.close()

    print(f"  {ticker} opened  {shares:.0f}sh @ ${price:.2f}  stop {stop_str}  [{size_type}] [{setup}]{r_str}  [{broker}]")
    _push_to_droplet()
    _ob1_capture(
        f"Position opened: {ticker}\n"
        f"date={date.today().isoformat()} entry=${price:.2f} stop={stop_str} "
        f"shares={shares:.0f} size={size_type} setup={setup} broker={broker} regime={regime}"
        + (f" R=${r_risk:.0f}" if r_risk else "") + "\n"
        f"Agent: broad-scan buy-cmd"
    )


def _fetch_mfe_mae(ticker, entry_date):
    """Fetch high/low since entry to compute MFE and MAE."""
    try:
        import yfinance as yf
        hist = yf.Ticker(ticker).history(start=entry_date)
        if hist.empty:
            return None, None
        mfe = float(hist["High"].max())
        mae = float(hist["Low"].min())
        return mfe, mae
    except Exception:
        return None, None


def cmd_close_pos(args):
    ticker = args.ticker.upper()
    conn = sqlite3.connect(_h.DB_PATH)
    row = conn.execute(
        "SELECT id, entry_price, shares, entry_date FROM positions WHERE ticker=? AND status='open'",
        (ticker,)
    ).fetchone()
    if not row:
        print(f"{ticker} has no open position")
        conn.close()
        return
    pos_id, entry, shares, entry_date = row
    mfe, mae = _fetch_mfe_mae(ticker, entry_date)
    pnl = round((args.price - entry) * shares, 2) if args.price else None
    conn.execute(
        """UPDATE positions SET status='closed', exit_date=?, exit_price=?,
           exit_reason=?, mfe=?, mae=? WHERE id=?""",
        (date.today().isoformat(), args.price, args.reason, mfe, mae, pos_id)
    )
    _log_history(conn, ticker, "position_closed", reason=args.reason,
                 detail={"exit_price": args.price, "entry_price": entry,
                         "shares": shares, "pnl": pnl, "mfe": mfe, "mae": mae})
    conn.commit()
    conn.close()
    pnl_str = f"  P&L: ${pnl:+.0f}" if pnl is not None else ""
    mfe_str = f"  MFE: ${mfe:.2f}" if mfe else ""
    mae_str = f"  MAE: ${mae:.2f}" if mae else ""
    print(f"  {ticker} closed{pnl_str}{mfe_str}{mae_str}")
    _push_to_droplet()
    _ob1_capture(
        f"Position closed: {ticker}\n"
        f"date={date.today().isoformat()} entry=${entry:.2f} exit=${args.price:.2f} "
        f"shares={shares} reason={args.reason or '--'}"
        + (f" pnl=${pnl:+.0f}" if pnl is not None else "")
        + (f" mfe=${mfe:.2f}" if mfe else "")
        + (f" mae=${mae:.2f}" if mae else "") + "\n"
        f"Agent: broad-scan close-position"
    )


def cmd_stop_pos(args):
    ticker = args.ticker.upper()
    conn = sqlite3.connect(_h.DB_PATH)
    row = conn.execute(
        "SELECT id, stop_price FROM positions WHERE ticker=? AND status='open'", (ticker,)
    ).fetchone()
    if not row:
        print(f"{ticker} has no open position")
        conn.close()
        return
    pos_id, old_stop = row
    conn.execute("UPDATE positions SET stop_price=? WHERE id=?", (args.price, pos_id))
    _log_history(conn, ticker, "stop_updated", detail={"old": old_stop, "new": args.price})
    conn.commit()
    conn.close()
    old_str = f"${old_stop:.2f}" if old_stop else "--"
    print(f"  {ticker} stop moved {old_str} -> ${args.price:.2f}")
    _push_to_droplet()


def cmd_scale_pos(args):
    ticker = args.ticker.upper()
    conn = sqlite3.connect(_h.DB_PATH)
    row = conn.execute(
        "SELECT id, entry_price, shares, stop_price FROM positions WHERE ticker=? AND status='open'",
        (ticker,)
    ).fetchone()
    if not row:
        print(f"{ticker} has no open position to scale")
        conn.close()
        return
    pos_id, old_entry, old_shares, old_stop = row
    if not _pyramid_check(conn, ticker, args.shares, args.entry):
        conn.close()
        return
    new_shares = old_shares + args.shares
    new_entry  = round((old_shares * old_entry + args.shares * args.entry) / new_shares, 4)
    new_stop   = args.stop if args.stop else old_stop
    new_r      = round(new_shares * (new_entry - new_stop), 2) if new_stop else None
    setup      = getattr(args, "setup", "add_on") or "add_on"
    conn.execute(
        "UPDATE positions SET shares=?, entry_price=?, stop_price=?, r_risk=? WHERE id=?",
        (new_shares, new_entry, new_stop, new_r, pos_id)
    )
    _log_history(conn, ticker, "position_scaled", detail={
        "add_shares": args.shares, "add_entry": args.entry,
        "old_shares": old_shares, "old_entry": old_entry,
        "new_shares": new_shares, "new_entry": new_entry,
        "setup": setup,
    })
    conn.commit()
    conn.close()
    r_str = f"  R=${new_r:.0f}" if new_r else ""
    print(
        f"  {ticker} scaled  +{args.shares:.0f}sh @ ${args.entry:.2f}  "
        f"-> {new_shares:.0f}sh avg ${new_entry:.2f}{r_str}"
    )
    _push_to_droplet()
    _ob1_capture(
        f"Position scaled: {ticker}\n"
        f"date={date.today().isoformat()} add={args.shares}sh@${args.entry:.2f} "
        f"new_total={new_shares:.0f}sh avg_entry=${new_entry:.2f} setup={setup}\n"
        f"Agent: broad-scan scale-position"
    )


def cmd_refresh_positions(args):
    """Update MFE/MAE for all open positions from yfinance."""
    conn = sqlite3.connect(_h.DB_PATH)
    rows = conn.execute(
        "SELECT id, ticker, entry_date FROM positions WHERE status='open' ORDER BY ticker"
    ).fetchall()
    if not rows:
        print("No open positions.")
        conn.close()
        return
    print(f"Refreshing MFE/MAE for {len(rows)} open position(s)...")
    for pos_id, ticker, entry_date in rows:
        mfe, mae = _fetch_mfe_mae(ticker, entry_date)
        conn.execute(
            "UPDATE positions SET mfe=?, mae=? WHERE id=?", (mfe, mae, pos_id)
        )
        mfe_str = f"MFE ${mfe:.2f}" if mfe else "MFE --"
        mae_str = f"MAE ${mae:.2f}" if mae else "MAE --"
        print(f"  {ticker:<8}  {mfe_str}  {mae_str}")
    conn.commit()
    conn.close()


def cmd_clarity(args):
    """On-demand CLARITY check: locker tickers that were exit-d1 at last close, now above 4 EMA live."""
    conn = sqlite3.connect(_h.DB_PATH)
    today   = date.today().isoformat()
    dry_run = getattr(args, "dry_run", False)

    active = [r[0] for r in conn.execute(
        "SELECT ticker FROM locker_room WHERE status='active'"
    ).fetchall()]

    if not active:
        print("Locker is empty.")
        conn.close()
        return

    candidates = []
    for t in active:
        prev_date_row = conn.execute(
            "SELECT MAX(signal_date) FROM eod_signals WHERE ticker=? AND signal_date<?",
            (t, today)
        ).fetchone()
        prev_date = prev_date_row[0] if prev_date_row else None
        if not prev_date:
            continue
        toby_row = conn.execute(
            "SELECT toby_status FROM eod_signals WHERE ticker=? AND signal_date=?",
            (t, prev_date)
        ).fetchone()
        if toby_row and toby_row[0] == "exit-d1":
            candidates.append((t, prev_date))

    if not candidates:
        print("No exit-d1 tickers in locker -- no CLARITY candidates.")
        conn.close()
        return

    print(f"Checking {len(candidates)} exit-d1 ticker(s) live: {[c[0] for c in candidates]}")
    live_results = _batch_live_ema4([c[0] for c in candidates])

    fired = []
    for t, prev_date in candidates:
        result = live_results.get(t)
        if not result:
            print(f"  {t:<8}  live fetch failed")
            continue
        live_status = result["status"]
        ema_pct = result.get("ema_4e_pct", 0)
        if live_status not in ("above", "reclaim"):
            print(f"  {t:<8}  still below 4 EMA ({ema_pct:+.1f}%) -- not CLARITY")
            continue
        already = conn.execute(
            "SELECT 1 FROM locker_history WHERE ticker=? AND event='clarity' AND date=?",
            (t, today)
        ).fetchone()
        if already:
            print(f"  {t:<8}  ✨ CLARITY already fired today ({ema_pct:+.1f}%)")
            continue
        print(f"  {t:<8}  ✨ CLARITY ({ema_pct:+.1f}%) -- {'dry-run' if dry_run else 'firing'}")
        fired.append({"ticker": t, "ema_pct": ema_pct, "prev_date": prev_date})
        if not dry_run:
            conn.execute(
                "UPDATE locker_room SET ema4_status='clarity', ema4_updated=? WHERE ticker=? AND status='active'",
                (today, t)
            )
            _check_and_fire_clarity(conn, t, today, live_status)

    if not dry_run and fired:
        conn.commit()

    conn.close()

    if not fired:
        print("No new CLARITY signals.")
    else:
        print(f"\n{len(fired)} CLARITY signal(s) fired and pinged to Discord.")

    ps_result = _prospect_signals_run(dry_run=dry_run)
    ps_msgs   = _prospect_signals_msgs(ps_result)
    if ps_msgs:
        print("\n--- Prospect signals ---")
        for msg in ps_msgs:
            print(msg)
            if not dry_run:
                discord_router.send_to("locker", msg)
    else:
        print("\nNo prospect reclaim/clarity signals.")
