import argparse
import os
import sqlite3
from datetime import date
from pathlib import Path

from scanner.kb import capture as _ob1_capture
from scanner.prospects import (
    _auto_notes,
    _build_notes_technical,
    _insert_prospect,
    _is_stale,
    push_to_droplet as _push_to_droplet,
)
from scanner.sessions import cmd_prospect_session, cmd_promote_session, cmd_drop_session

DB_PATH = Path(os.environ.get("BROAD_SCAN_DB", Path(__file__).parent.parent / "broad-scan.db"))

SC_LIST_FOR_BUCKET = {
    "prospect":     25,  # ! [0000] Prospects
    "buy_and_hold": 26,  # ! [0102] Manual SMA200
}
BUCKETS = list(SC_LIST_FOR_BUCKET.keys())


def cmd_add_prospect(args):
    conn = sqlite3.connect(DB_PATH)
    added = []
    for t in args.tickers:
        notes = args.notes
        enrichment = {}
        if notes is None:
            print(f"  generating note for {t.upper()}...")
            notes, enrichment = _auto_notes(t.upper())
        _insert_prospect(conn, t.upper(), args.source, notes, args.bucket, enrichment=enrichment)
        row = conn.execute(
            "SELECT notes_technical FROM prospects WHERE ticker=? AND status='active' ORDER BY added_date DESC LIMIT 1",
            (t.upper(),)
        ).fetchone()
        notes_tech = row[0] if row else None
        added.append((t.upper(), notes, args.bucket, enrichment, notes_tech))
    conn.commit()
    conn.close()

    for ticker, notes, bucket, enrichment, notes_tech in added:
        e = enrichment or {}
        lines = [
            f"Broad scan prospect added: {ticker}",
            f"date={date.today().isoformat()} bucket={bucket} source={args.source or '--'}",
        ]
        if notes:
            lines.append(f"notes={notes}")
        if notes_tech:
            lines.append(f"notes_technical={notes_tech}")
        if e.get("sector"):
            cap = f"  mktcap=${e['market_cap']//1_000_000_000:.0f}B" if e.get("market_cap") else ""
            lines.append(f"sector={e['sector']} / {e.get('industry','')}{cap}")
        lines.append("Agent: broad-scan add-prospect")
        _ob1_capture("\n".join(lines))




def cmd_drop_prospect(args):
    ticker = args.ticker.upper()
    conn = sqlite3.connect(DB_PATH)
    row = conn.execute(
        "SELECT id FROM prospects WHERE ticker=? AND status='active' ORDER BY added_date DESC LIMIT 1",
        (ticker,)
    ).fetchone()
    if not row:
        print(f"{ticker} not found in active prospects")
        conn.close()
        return
    conn.execute("UPDATE prospects SET status='dropped' WHERE id=?", (row[0],))
    conn.commit()
    conn.close()
    print(f"Dropped {ticker} from prospects")
    reason = getattr(args, "reason", None) or "--"
    _ob1_capture(
        f"Prospect exit: {ticker}\n"
        f"date={date.today().isoformat()} reason={reason}\n"
        f"Agent: broad-scan drop-prospect"
    )


def cmd_promote(args):
    ticker = args.ticker.upper()
    conn = sqlite3.connect(DB_PATH)
    prospect = conn.execute(
        "SELECT id, notes, notes_technical FROM prospects WHERE ticker=? AND status='active' ORDER BY added_date DESC LIMIT 1",
        (ticker,)
    ).fetchone()
    if not prospect:
        print(f"{ticker} not in active prospects -- add with: add-prospect {ticker}")
        conn.close()
        return
    prospect_id, prospect_notes, prospect_notes_tech = prospect
    conn.execute("UPDATE prospects SET status='promoted' WHERE id=?", (prospect_id,))
    conn.execute(
        """INSERT INTO locker_room (ticker, promoted_date, source_prospect_id, status, notes)
           VALUES (?, ?, ?, 'active', ?)""",
        (ticker, date.today().isoformat(), prospect_id, prospect_notes)
    )
    conn.commit()

    print(f"  fetching EMA4 + enrichment for {ticker}...")
    from locker._helpers import _ema4_status, _log_history
    import yfinance as yf
    ema4 = _ema4_status(ticker)
    try:
        info = yf.Ticker(ticker).info
        sector    = info.get("sector")
        industry  = info.get("industry")
        mktcap    = info.get("marketCap")
        beta      = info.get("beta")
        short_pct = info.get("shortPercentOfFloat")
    except Exception:
        sector = industry = mktcap = beta = short_pct = None
    conn.execute("""
        UPDATE locker_room SET
            ema4_status=?, ema4_updated=?,
            sector=?, industry=?, market_cap=?, beta=?, short_pct_float=?
        WHERE ticker=? AND status='active'
    """, (ema4, date.today().isoformat(),
          sector, industry, mktcap, beta, short_pct, ticker))
    _log_history(conn, ticker, "added",
                 reason=f"promoted from prospects | ema4={ema4}")
    conn.commit()
    conn.close()

    ema4_label = {"reclaim": "★ RECLAIM", "above": "↑ above EMA4",
                  "below": "↓ below EMA4"}.get(ema4, "?")
    print(f"Promoted {ticker} → Locker Room  [{ema4_label}]  {sector or '--'}")
    _push_to_droplet()

    cap_str = f"  mktcap=${mktcap//1_000_000_000:.0f}B" if mktcap else ""
    _ob1_capture(
        f"Locker Room add: {ticker}\n"
        f"date={date.today().isoformat()} ema4={ema4} sector={sector or '--'}"
        f" industry={industry or '--'}{cap_str}"
        f" beta={beta or '--'} short={short_pct or '--'}%\n"
        f"notes={prospect_notes or '--'}\n"
        f"notes_technical={prospect_notes_tech or '--'}\n"
        f"Agent: broad-scan promote"
    )


def cmd_exit(args):
    ticker = args.ticker.upper()
    conn = sqlite3.connect(DB_PATH)
    row = conn.execute(
        "SELECT id FROM locker_room WHERE ticker=? AND status='active' ORDER BY promoted_date DESC LIMIT 1",
        (ticker,)
    ).fetchone()
    if not row:
        print(f"{ticker} not found in active Locker Room")
        conn.close()
        return
    conn.execute(
        "UPDATE locker_room SET status='removed', removed_date=? WHERE id=?",
        (date.today().isoformat(), row[0])
    )
    conn.commit()
    conn.close()
    print(f"Exited {ticker} from Locker Room")
    reason = getattr(args, "reason", None) or "--"
    _ob1_capture(
        f"Locker Room exit: {ticker}\n"
        f"date={date.today().isoformat()} reason={reason}\n"
        f"Agent: broad-scan locker-exit"
    )


def cmd_refresh_technical(args):
    """Refresh notes_technical for all active prospects from latest scans."""
    conn = sqlite3.connect(DB_PATH)
    rows = conn.execute(
        "SELECT id, ticker FROM prospects WHERE status='active' ORDER BY ticker"
    ).fetchall()
    for pid, ticker in rows:
        nt = _build_notes_technical(ticker, conn)
        conn.execute("UPDATE prospects SET notes_technical=? WHERE id=?", (nt, pid))
        print(f"  {ticker:<8}  {nt or '--'}")
    conn.commit()
    conn.close()
    print(f"\nRefreshed {len(rows)} prospect(s).")


def cmd_list_prospects(args):
    conn = sqlite3.connect(DB_PATH)
    sql = """SELECT ticker, added_date, bucket, notes, notes_technical,
             CAST((julianday('now') - julianday(added_date)) AS INTEGER) as days_old
             FROM prospects WHERE status='active'"""
    params = ()
    if getattr(args, "ticker", None):
        sql += " AND ticker=?"
        params = (args.ticker.upper(),)
    elif args.bucket:
        sql += " AND bucket=?"
        params = (args.bucket,)
    sql += " ORDER BY bucket, added_date DESC"
    rows = conn.execute(sql, params).fetchall()
    conn.close()
    if not rows:
        t = getattr(args, "ticker", None)
        print(f"{t.upper()} not found in active prospects." if t else "No active prospects.")
        return

    stale = [r for r in rows if _is_stale(r[1], r[4])]
    if not getattr(args, "ticker", None):
        print(f"\nProspects ({len(rows)}){f'  --  {len(stale)} stale' if stale else ''}\n")
    for ticker, added, bucket, notes, notes_technical, days_old in rows:
        stale_flag = "  ⚠ stale" if _is_stale(added, notes_technical) else ""
        print(f"{ticker:<8}  {added}  {days_old}d  [{bucket}]{stale_flag}")
        print(f"         {notes or '--'}")
        if notes_technical:
            print(f"         {notes_technical}")
        print()


def cmd_prospect_sc_sync(args):
    listnum = SC_LIST_FOR_BUCKET[args.bucket]
    conn = sqlite3.connect(DB_PATH)
    rows = conn.execute(
        "SELECT ticker FROM prospects WHERE status='active' AND bucket=? ORDER BY ticker",
        (args.bucket,)
    ).fetchall()
    conn.close()
    push = [r[0] for r in rows]

    if not push:
        print(f"No active prospects in bucket={args.bucket}.")
        return

    print(f"bucket={args.bucket}  →  {len(push)} ticker(s) for SC listNum={listnum}")
    for t in push:
        print(f"  {t}")

    if args.dry_run:
        action = "clear + push" if args.clear else "push (additive)"
        print(f"\n[dry-run] Would {action} {len(push)} to listNum={listnum}.")
        return

    from scanner.sc_api import sc_session, sc_clear, sc_add
    print(f"\nPushing to SC listNum={listnum}...")
    s = sc_session()
    if args.clear:
        sc_clear(s, listnum)
        print(f"  SC: cleared listNum={listnum}")
    failed = sc_add(s, listnum, push)
    print(f"  SC: pushed {len(push) - len(failed)}/{len(push)} ticker(s)")
    if failed:
        print(f"  failed: {', '.join(failed)}")


# scan_type stem -> (yfinance scan_type, restrict to is_new_signal=1 ?)
RECONCILE_PAIRS = [
    ("rsi_daily_cross",   "rsi_daily",   True),
    ("rsi_weekly_cross",  "rsi_weekly",  True),
    ("rsi_monthly_cross", "rsi_monthly", True),
    ("rsi_daily_above",   "rsi_daily",   False),
    ("rsi_weekly_above",  "rsi_weekly",  False),
    ("rsi_monthly_above", "rsi_monthly", False),
    ("52w_high",          "52w_high",    False),
    ("12w_high",          "12w_high",    False),
    ("ath",               "ath",         False),
]


def _ticker_set(conn, scan_type, scan_date, new_only=False):
    sql = "SELECT ticker FROM scans WHERE scan_type=? AND scan_date=?"
    params = [scan_type, scan_date]
    if new_only:
        sql += " AND is_new_signal=1"
    return {r[0] for r in conn.execute(sql, params)}


def cmd_reconcile(args):
    """Compare SC email open vs close vs yfinance for each scan.

    Close = canonical daily-bar signal. Open = provisional preview.
    held = opened AND closed above threshold; faded = open but no close;
    close-only = closed above but didn't open above (intraday rally).
    yf@6:35 column is yfinance's recompute at cron time -- expected to drift
    from SC's open print on volatile names (see insights.md 2026-05-15).
    """
    scan_date = (args.date or date.today().isoformat())
    conn = sqlite3.connect(DB_PATH)
    rows = []
    detail = []
    for stem, yf_type, new_only in RECONCILE_PAIRS:
        opens  = _ticker_set(conn, f"sc_email_{stem}_open",  scan_date)
        closes = _ticker_set(conn, f"sc_email_{stem}_close", scan_date)
        yf     = _ticker_set(conn, yf_type, scan_date, new_only=new_only)
        held   = opens & closes
        faded  = opens - closes
        close_only = closes - opens
        # If we have close data, agreement vs SC close; otherwise vs SC open
        sc_ref = closes if closes else opens
        yf_match = sc_ref & yf
        rows.append((stem, len(opens), len(closes), len(held), len(faded),
                     len(close_only), len(yf), len(yf_match)))
        if args.detail:
            detail.append((stem, sorted(faded), sorted(close_only),
                          sorted(sc_ref - yf), sorted(yf - sc_ref)))
    conn.close()

    print(f"\nReconcile {scan_date}\n")
    h = ("scan", "open", "close", "held", "faded", "close_only", "yf", "yf_match")
    print(f"{h[0]:<22} {h[1]:>5} {h[2]:>6} {h[3]:>5} {h[4]:>6} {h[5]:>11} {h[6]:>4} {h[7]:>9}")
    print("-" * 75)
    for stem, o, c, h_, f, co, y, ym in rows:
        print(f"{stem:<22} {o:>5} {c:>6} {h_:>5} {f:>6} {co:>11} {y:>4} {ym:>9}")
    print("\nLegend: open=SC at-open alert | close=SC at-close (canonical) | "
          "held=opened AND closed | faded=opened, no close | "
          "close_only=closed, didn't open | yf=yfinance is_new_signal@6:35 | "
          "yf_match=close ∩ yf (or open ∩ yf if no close data yet)")

    if args.detail and any(d[1] or d[2] for d in detail):
        print("\n--- ticker detail ---")
        for stem, faded, close_only, sc_no_yf, yf_no_sc in detail:
            if any([faded, close_only, sc_no_yf, yf_no_sc]):
                print(f"\n{stem}:")
                if faded:      print(f"  faded       : {', '.join(faded)}")
                if close_only: print(f"  close_only  : {', '.join(close_only)}")
                if sc_no_yf:   print(f"  SC not yf   : {', '.join(sc_no_yf)}")
                if yf_no_sc:   print(f"  yf not SC   : {', '.join(yf_no_sc)}")


def cmd_list_locker(args):
    conn = sqlite3.connect(DB_PATH)
    rows = conn.execute(
        "SELECT ticker, promoted_date FROM locker_room WHERE status='active' ORDER BY promoted_date DESC"
    ).fetchall()
    print(f"\nLocker Room ({len(rows)})")
    if not rows:
        print("Empty.")
        conn.close()
        return
    print(f"{'Ticker':<8}  {'Promoted':<12}  {'RSI-W Cross':<14}  Weeks in run")
    print("-" * 55)
    for ticker, promoted in rows:
        cross = conn.execute(
            """SELECT MIN(scan_date) FROM scans
               WHERE ticker=? AND scan_type='rsi_weekly' AND is_new_signal=1 AND scan_date >= ?""",
            (ticker, promoted)
        ).fetchone()[0]
        if cross:
            weeks = (date.today() - date.fromisoformat(cross)).days // 7
            weeks_str = f"{weeks}w"
        else:
            cross = "--"
            weeks_str = "--"
        print(f"{ticker:<8}  {promoted:<12}  {(cross or '--'):<14}  {weeks_str}")
    conn.close()


def main():
    ap = argparse.ArgumentParser(prog="cli", description="broad-scan decision tracker")
    sub = ap.add_subparsers(dest="cmd", required=True)

    p = sub.add_parser("add-prospect", help="Add one or more tickers to prospects (batch)")
    p.add_argument("tickers", nargs="+")
    p.add_argument("--source", default="sc_workbench")
    p.add_argument("--notes", default=None)
    p.add_argument("--bucket", choices=BUCKETS, default="prospect",
                   help="prospect (momentum/breakout, → SC [0000]) or buy_and_hold (SMA reclaim, → SC [0102])")

    sub.add_parser("prospect-session", help="Interactive: review today's scan and add prospects")
    sub.add_parser("promote-session", help="Interactive: review active prospects and promote or drop")
    sub.add_parser("drop-session", help="Interactive: review stale prospects (14d+, no RSI) and drop or promote")

    p = sub.add_parser("promote", help="Move prospect → Locker Room")
    p.add_argument("ticker")

    p = sub.add_parser("exit", help="Remove ticker from Locker Room")
    p.add_argument("ticker")

    p = sub.add_parser("drop-prospect", help="Remove a prospect that didn't pan out")
    p.add_argument("ticker")

    p = sub.add_parser("list-prospects", help="Show active prospects")
    p.add_argument("ticker", nargs="?", default=None, help="Show one ticker only")
    p.add_argument("--bucket", choices=BUCKETS, default=None,
                   help="Filter by bucket (default: show all)")
    sub.add_parser("list-locker", help="Show active Locker Room with RSI week count")
    sub.add_parser("refresh-technical", help="Refresh notes_technical for all active prospects from latest scans")

    p = sub.add_parser("reconcile", help="SC open vs close vs yfinance per scan for a given date")
    p.add_argument("--date", default=None, help="YYYY-MM-DD (default: today)")
    p.add_argument("--detail", action="store_true", help="Print ticker lists for fades and divergences")

    p = sub.add_parser("prospect-sc-sync",
                       help="Push active prospects → SC list (default: prospect→[0000], buy_and_hold→[0102])")
    p.add_argument("--bucket", choices=BUCKETS, default="prospect",
                   help="Which bucket to sync. Default: prospect.")
    p.add_argument("--clear", action="store_true",
                   help="Clear SC list first (replace mode). Default is additive.")
    p.add_argument("--dry-run", action="store_true",
                   help="Show what would be pushed without calling SC")

    args = ap.parse_args()
    {
        "add-prospect":     cmd_add_prospect,
        "prospect-session": cmd_prospect_session,
        "promote-session":  cmd_promote_session,
        "drop-session":     cmd_drop_session,
        "drop-prospect":    cmd_drop_prospect,
        "promote":          cmd_promote,
        "exit":             cmd_exit,
        "list-prospects":   cmd_list_prospects,
        "list-locker":        cmd_list_locker,
        "prospect-sc-sync":   cmd_prospect_sc_sync,
        "reconcile":          cmd_reconcile,
        "refresh-technical":  cmd_refresh_technical,
    }[args.cmd](args)


if __name__ == "__main__":
    main()
