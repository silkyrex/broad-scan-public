"""
Interactive session commands: prospect-session, promote-session, drop-session.
Each pulls the latest DB from the VPS, runs an interactive loop, then pushes back.
"""
import os
import sqlite3
import subprocess
from datetime import date
from pathlib import Path

from scanner.kb import capture as _ob1_capture, search as _ob1_search
from scanner.prospects import (
    _auto_notes,
    _build_notes_technical,
    _insert_prospect,
    _is_stale,
    push_to_droplet,
)

_DB_PATH = Path(os.environ.get("BROAD_SCAN_DB", Path(__file__).parent.parent / "broad-scan.db"))
_DROPLET_DB = "root@your-vps.example.com:/opt/broad-scan/broad-scan.db"


def _pull_from_droplet():
    print("  pulling latest DB from VPS...")
    result = subprocess.run(
        ["scp", _DROPLET_DB, str(_DB_PATH)],
        capture_output=True, text=True
    )
    if result.returncode == 0:
        print("  ready.\n")
    else:
        print(f"  pull failed ({result.stderr.strip()}) -- using local DB\n")


def cmd_prospect_session(args):
    _pull_from_droplet()
    scan_date = date.today().isoformat()
    conn = sqlite3.connect(_DB_PATH)

    rows = conn.execute("""
        SELECT s.ticker,
          MAX(CASE WHEN s.scan_type='rsi_daily'  AND s.is_new_signal=1 THEN 1 ELSE 0 END) as rd,
          MAX(CASE WHEN s.scan_type='rsi_weekly' AND s.is_new_signal=1 THEN 1 ELSE 0 END) as rw,
          MAX(CASE WHEN s.scan_type='ath'        AND s.is_new_signal=1 THEN 1 ELSE 0 END) as ath,
          COALESCE(ROUND(sc.value,1), -1) as sctr
        FROM scans s
        LEFT JOIN scans sc ON s.ticker=sc.ticker
          AND sc.scan_date=? AND sc.scan_type='sc_workbench'
        WHERE s.scan_date=? AND s.is_new_signal=1
          AND s.scan_type IN ('rsi_daily','rsi_weekly','ath')
        GROUP BY s.ticker
        ORDER BY (rd+rw+ath) DESC, sctr DESC
    """, (scan_date, scan_date)).fetchall()

    already = {r[0] for r in conn.execute(
        "SELECT ticker FROM prospects WHERE status='active'"
    ).fetchall()}

    print(f"\nScan Today  {scan_date}  ({len(rows)} names)")
    print(f"{'Ticker':<8}  {'D':>3}  {'W':>3}  {'ATH':>4}  {'SCTR':>6}")
    print("-" * 34)
    for ticker, rd, rw, ath, sctr in rows:
        d = "✓" if rd else " "
        w = "✓" if rw else " "
        a = "✓" if ath else " "
        s = f"{sctr:.1f}" if sctr >= 0 else "--"
        print(f"{ticker:<8}  {d:>3}  {w:>3}  {a:>4}  {s:>6}{' *' if ticker in already else ''}")

    print("\n* already in prospects")
    print("\nType tickers to add (one per line). Notes optional: COHR photonics tailwind")
    print("Enter blank line or 'done' to finish.\n")

    added = 0
    while True:
        try:
            line = input("> ").strip()
        except (EOFError, KeyboardInterrupt):
            break
        if not line or line.lower() == "done":
            break
        parts = line.split(None, 1)
        ticker = parts[0].upper()
        notes = parts[1] if len(parts) > 1 else None
        _ob1_hits = _ob1_search(f"trading {ticker} prospect exit drop stop", limit=3, threshold=0.60)
        for _hit in _ob1_hits:
            _first = next((l.strip() for l in _hit.splitlines() if l.strip()), "")
            if _first:
                print(f"  [KB] {_first[:120]}")
        if ticker in already:
            print(f"  {ticker} already in prospects -- skipped")
            continue
        enrichment = {}
        if notes is None:
            print(f"  generating note for {ticker}...")
            notes, enrichment = _auto_notes(ticker)
        notes_technical = _build_notes_technical(ticker, conn)
        _insert_prospect(conn, ticker, "sc_workbench", notes,
                         notes_technical=notes_technical, enrichment=enrichment)
        already.add(ticker)
        added += 1

    conn.commit()
    conn.close()
    print(f"\n{added} prospect(s) added.")
    if added > 0:
        push_to_droplet()


def cmd_promote_session(args):
    _pull_from_droplet()
    conn = sqlite3.connect(_DB_PATH)
    rows = conn.execute("""
        SELECT p.id, p.ticker, p.added_date, p.notes, p.notes_technical,
               CAST((julianday('now') - julianday(p.added_date)) AS INTEGER) as days_old
        FROM prospects p
        WHERE p.status='active'
        ORDER BY days_old DESC
    """).fetchall()

    if not rows:
        print("No active prospects.")
        conn.close()
        return

    print(f"Prospects to review ({len(rows)})")
    print("Commands: p = promote  d = drop  Enter = skip  done = quit\n")

    promoted = dropped = 0
    for pid, ticker, added_date, notes, notes_technical, days_old in rows:
        stale_flag = "  ⚠ stale" if _is_stale(added_date, notes_technical) else ""
        print(f"{'─' * 50}")
        print(f"{ticker}  ({days_old}d old, added {added_date}){stale_flag}")
        print(f"  {notes or '--'}")
        if notes_technical:
            print(f"  {notes_technical}")
        print()
        _ob1_hits = _ob1_search(f"trading {ticker} promote exit drop stop locker", limit=3, threshold=0.60)
        if _ob1_hits:
            for _hit in _ob1_hits:
                _first = next((l.strip() for l in _hit.splitlines() if l.strip()), "")
                if _first:
                    print(f"  [KB] {_first[:120]}")
            print()

        try:
            cmd = input("  [p/d/Enter] > ").strip().lower()
        except (EOFError, KeyboardInterrupt):
            break

        if cmd == "done":
            break
        elif cmd == "p":
            conn.execute("UPDATE prospects SET status='promoted' WHERE id=?", (pid,))
            conn.execute(
                "INSERT INTO locker_room (ticker, promoted_date, source_prospect_id, status, notes) VALUES (?, ?, ?, 'active', ?)",
                (ticker, date.today().isoformat(), pid, notes)
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

            ema4_label = {"reclaim": "★ RECLAIM", "above": "↑ above EMA4",
                          "below": "↓ below EMA4"}.get(ema4, "?")
            print(f"  {ticker} → Locker Room  [{ema4_label}]  {sector or '--'}\n")
            cap_str = f"  mktcap=${mktcap//1_000_000_000:.0f}B" if mktcap else ""
            _ob1_capture(
                f"Locker Room add: {ticker}\n"
                f"date={date.today().isoformat()} ema4={ema4} sector={sector or '--'}"
                f" industry={industry or '--'}{cap_str}"
                f" beta={beta or '--'} short={short_pct or '--'}%\n"
                f"notes={notes or '--'}\n"
                f"notes_technical={notes_technical or '--'}\n"
                f"Agent: broad-scan promote-session"
            )
            promoted += 1
        elif cmd == "d":
            conn.execute(
                "UPDATE prospects SET status='dropped', dropped_date=? WHERE id=?",
                (date.today().isoformat(), pid)
            )
            conn.commit()
            print(f"  {ticker} dropped\n")
            dropped += 1
        else:
            print(f"  skipped\n")

    conn.close()
    print(f"\n{promoted} promoted  {dropped} dropped.")
    if promoted + dropped > 0:
        push_to_droplet()


def cmd_drop_session(args):
    conn = sqlite3.connect(_DB_PATH)
    rows = conn.execute("""
        SELECT id, ticker, added_date, notes, notes_technical,
               CAST((julianday('now') - julianday(added_date)) AS INTEGER) as days_old
        FROM prospects WHERE status='active'
        ORDER BY days_old DESC
    """).fetchall()

    stale = [(pid, t, a, n, nt, d) for pid, t, a, n, nt, d in rows if _is_stale(a, nt)]

    if not stale:
        print("No stale prospects -- nothing to clean up.")
        conn.close()
        return

    print(f"\nStale prospects ({len(stale)})  --  14+ days, no RSI signal")
    print("Commands: d = drop  p = promote  Enter = skip  done = quit\n")

    dropped = promoted = 0
    for pid, ticker, added_date, notes, notes_technical, days_old in stale:
        print(f"{'─' * 50}")
        print(f"{ticker}  ({days_old}d old, added {added_date})  ⚠ stale")
        print(f"  {notes or '--'}")
        if notes_technical:
            print(f"  {notes_technical}")
        print()

        try:
            cmd = input("  [d/p/Enter] > ").strip().lower()
        except (EOFError, KeyboardInterrupt):
            break

        if cmd == "done":
            break
        elif cmd == "d":
            conn.execute(
                "UPDATE prospects SET status='dropped', dropped_date=? WHERE id=?",
                (date.today().isoformat(), pid)
            )
            conn.commit()
            print(f"  {ticker} dropped\n")
            dropped += 1
        elif cmd == "p":
            conn.execute("UPDATE prospects SET status='promoted' WHERE id=?", (pid,))
            conn.execute(
                "INSERT INTO locker_room (ticker, promoted_date, source_prospect_id, status) VALUES (?, ?, ?, 'active')",
                (ticker, date.today().isoformat(), pid)
            )
            conn.commit()
            print(f"  {ticker} → Locker Room\n")
            promoted += 1
        else:
            print(f"  skipped\n")

    conn.close()
    print(f"\n{dropped} dropped  {promoted} promoted.")
    if dropped + promoted > 0:
        push_to_droplet()
