"""CLI for paper trading agent."""

import argparse
import os
import sqlite3
import sys

BROKER = "paper-agent"


def cmd_run(args):
    from paper_agent.agent import run
    webhook = os.getenv("DISCORD_PAPER_WEBHOOK") or os.getenv("DISCORD_WEBHOOK_URL")
    run(db_path=args.db, discord_webhook=webhook, dry_run=args.dry_run)


def cmd_status(args):
    conn = sqlite3.connect(args.db)
    rows = conn.execute(
        """SELECT ticker, entry_date, entry_price, shares, stop_price
           FROM positions WHERE broker=? AND status='open'
           ORDER BY entry_date""",
        (BROKER,),
    ).fetchall()
    if not rows:
        print("No open positions.")
        return
    print(f"{'TICKER':<8} {'DATE':<12} {'ENTRY':>8} {'SHARES':>8} {'STOP':>8}")
    print("-" * 50)
    for r in rows:
        print(f"{r[0]:<8} {r[1]:<12} {r[2]:>8.2f} {r[3]:>8.4f} {r[4]:>8.2f}")
    conn.close()


def cmd_kill(args):
    from paper_agent.agent import kill_all
    killed = kill_all(db_path=args.db)
    if killed:
        print(f"Killed {len(killed)} positions: {', '.join(killed)}")
    else:
        print("No open positions to kill.")


def cmd_history(args):
    conn = sqlite3.connect(args.db)
    rows = conn.execute(
        """SELECT ticker, entry_date, exit_date, entry_price, exit_price, exit_reason,
                  ROUND((exit_price - entry_price) / entry_price * 100, 2) AS pnl_pct
           FROM positions WHERE broker=? AND status='closed'
           ORDER BY exit_date DESC""",
        (BROKER,),
    ).fetchall()
    if not rows:
        print("No closed positions.")
        return
    total_pct = sum(r[6] or 0 for r in rows)
    wins = sum(1 for r in rows if (r[6] or 0) > 0)
    print(f"{'TICKER':<8} {'ENTRY':<12} {'EXIT':<12} {'IN':>7} {'OUT':>7} {'P&L%':>7}  REASON")
    print("-" * 68)
    for r in rows:
        pnl = r[6] or 0
        sign = "+" if pnl >= 0 else ""
        print(f"{r[0]:<8} {r[1]:<12} {r[2]:<12} {r[3]:>7.2f} {r[4]:>7.2f} {sign}{pnl:>6.2f}%  {r[5]}")
    print("-" * 68)
    print(f"  {len(rows)} trades | {wins}W/{len(rows)-wins}L | avg {total_pct/len(rows):+.2f}%")
    conn.close()


def main():
    parser = argparse.ArgumentParser(description="Paper Trading Agent")
    parser.add_argument("--db", default="broad-scan.db")
    sub = parser.add_subparsers(dest="cmd", required=True)

    run_p = sub.add_parser("run", help="Daily cycle: check exits -> find candidates -> enter")
    run_p.add_argument("--dry-run", action="store_true", help="Print candidates only, no DB writes")
    run_p.set_defaults(func=cmd_run)

    status_p = sub.add_parser("status", help="Show open positions")
    status_p.set_defaults(func=cmd_status)

    hist_p = sub.add_parser("history", help="Show closed positions with P&L")
    hist_p.set_defaults(func=cmd_history)

    kill_p = sub.add_parser("kill", help="Close all open positions immediately (kill switch)")
    kill_p.set_defaults(func=cmd_kill)

    args = parser.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
