"""Locker CLI entry point — argparse + dispatch only."""
import argparse
from pathlib import Path

import locker._helpers as _helpers
from locker._helpers import VALID_SETUPS, VALID_SIZES, cmd_sync
from locker.reclaim import cmd_reclaim
from locker.room import (
    cmd_add, cmd_del, cmd_pull, cmd_show, cmd_refresh, cmd_enrich,
    cmd_stale, cmd_prospects, cmd_note, cmd_history,
)
from locker.position import (
    cmd_open_pos, cmd_buy, cmd_close_pos, cmd_stop_pos, cmd_scale_pos,
    cmd_refresh_positions, cmd_clarity,
)


def main():
    ap = argparse.ArgumentParser(prog="locker", description="Locker Room -- EMA4, positions, history")
    ap.add_argument("--db", type=Path, default=None, help="Override DB path")
    sub = ap.add_subparsers(dest="cmd", required=True)

    p = sub.add_parser("add", help="Add a ticker directly to locker_room (bypasses prospect flow)")
    p.add_argument("ticker")
    p.add_argument("--source", default="manual")
    p.add_argument("--sector", default="")
    p.add_argument("--notes", default="")
    p.add_argument("--bucket", default="prospect")
    p.add_argument("--scan-signals", default="[]", dest="scan_signals")

    p = sub.add_parser("del", help="Remove a ticker from locker_room (soft delete)")
    p.add_argument("ticker")

    p = sub.add_parser("pull", help="Fetch technicals (price, RSI, 52w, ATH, SMAs) from yfinance")
    p.add_argument("ticker", nargs="?", default=None)

    sub.add_parser("show", help="Show locker room (watching + open + closed)")

    p = sub.add_parser("refresh", help="Refresh EMA4 status (all or one ticker)")
    p.add_argument("ticker", nargs="?", default=None)
    p.add_argument("--live", action="store_true",
                   help="Use live intraday price instead of last close")

    sub.add_parser("reclaim", help="Show 4 EMA reclaim status for all locker names")

    p = sub.add_parser("enrich", help="Backfill sector/industry/market_cap from yfinance")
    p.add_argument("ticker", nargs="?", default=None)

    p = sub.add_parser("open", help="Log position open")
    p.add_argument("ticker")
    p.add_argument("shares", type=float)
    p.add_argument("entry", type=float)
    p.add_argument("stop", type=float)
    p.add_argument("--broker", default="alpaca", help="Broker (default: alpaca)")
    p.add_argument("--size", choices=VALID_SIZES, default="full", help="full or pilot (default: full)")
    p.add_argument("--setup", choices=VALID_SETUPS, default="4ema_reclaim",
                   help="Entry setup (default: 4ema_reclaim)")

    p = sub.add_parser("buy", help="Auto-size from regime + equity, log position")
    p.add_argument("ticker")
    p.add_argument("--stop",   type=float, default=None, help="Stop price (prompted if omitted)")
    p.add_argument("--entry",  type=float, default=None, help="Entry price (default: last close)")
    p.add_argument("--shares", type=float, default=None, help="Override auto-sized share count")
    p.add_argument("--broker", default="alpaca")
    p.add_argument("--size",   choices=VALID_SIZES,   default="full")
    p.add_argument("--setup",  choices=VALID_SETUPS,  default="4ema_reclaim")

    p = sub.add_parser("close", help="Log position closed")
    p.add_argument("ticker")
    p.add_argument("--price", type=float, default=None)
    p.add_argument("--reason", default=None)

    p = sub.add_parser("stop", help="Move stop price on an open position")
    p.add_argument("ticker")
    p.add_argument("--price", type=float, required=True, help="New stop price")

    p = sub.add_parser("scale", help="Add shares to an existing open position (blended avg entry)")
    p.add_argument("ticker")
    p.add_argument("shares", type=float, help="Number of additional shares")
    p.add_argument("--entry", type=float, required=True, help="Fill price for the add-on shares")
    p.add_argument("--stop", type=float, default=None, help="Updated stop price (optional)")
    p.add_argument("--setup", choices=["add_on", "day2_entry"], default="add_on")

    p = sub.add_parser("stale", help="Show names N+ trading days with no reclaim (default 14 = 2 weeks)")
    p.add_argument("days", type=int, nargs="?", default=14)

    sub.add_parser("prospects", help="Show active prospects with live EMA4 + write to intraday_signals")

    p = sub.add_parser("clarity", help="Intraday CLARITY check: exit-d1 tickers now above 4 EMA live")
    p.add_argument("--dry-run", action="store_true", help="Print without writing or pinging Discord")

    sub.add_parser("sync", help="Pull VPS DB → local (full replace + migrate)")
    sub.add_parser("refresh-positions", help="Update MFE/MAE for all open positions")

    p = sub.add_parser("history", help="Show event history for a ticker")
    p.add_argument("ticker")

    p = sub.add_parser("note", help="Update thesis note for a locker room ticker")
    p.add_argument("ticker")
    p.add_argument("text", help="New thesis note")

    args = ap.parse_args()
    if args.db:
        _helpers.DB_PATH = args.db
    {
        "add":               cmd_add,
        "del":               cmd_del,
        "pull":              cmd_pull,
        "show":              cmd_show,
        "refresh":           cmd_refresh,
        "reclaim":           cmd_reclaim,
        "enrich":            cmd_enrich,
        "buy":               cmd_buy,
        "open":              cmd_open_pos,
        "close":             cmd_close_pos,
        "stop":              cmd_stop_pos,
        "scale":             cmd_scale_pos,
        "clarity":           cmd_clarity,
        "stale":             cmd_stale,
        "prospects":         cmd_prospects,
        "sync":              cmd_sync,
        "refresh-positions": cmd_refresh_positions,
        "history":           cmd_history,
        "note":              cmd_note,
    }[args.cmd](args)


if __name__ == "__main__":
    main()
