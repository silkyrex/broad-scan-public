"""locker sizing — show position sizing framework + open position pyramid status."""

import argparse
import json
import re
import sqlite3
import subprocess
import sys
import warnings
from pathlib import Path

warnings.filterwarnings("ignore")


def load_config(base_dir: Path) -> dict:
    cfg_path = base_dir / "config" / "sizing.json"
    return json.loads(cfg_path.read_text())


def save_config(base_dir: Path, cfg: dict) -> None:
    cfg_path = base_dir / "config" / "sizing.json"
    cfg_path.write_text(json.dumps(cfg, indent=2) + "\n")


def get_regime(base_dir: Path) -> str:
    try:
        result = subprocess.run(
            [sys.executable, "-W", "ignore", "bin/regime-check.py"],
            capture_output=True, text=True, cwd=base_dir,
        )
        m = re.search(r"(BULL|CHOP|BEAR)", result.stdout)
        return m.group(1) if m else "UNKNOWN"
    except Exception:
        return "UNKNOWN"


def get_live_prices(tickers: list[str]) -> dict:
    try:
        import yfinance as yf
        if len(tickers) == 1:
            h = yf.Ticker(tickers[0]).history(period="2d")
            return {tickers[0]: float(h["Close"].iloc[-1])}
        data = yf.download(tickers, period="2d", progress=False, auto_adjust=True)
        out = {}
        for t in tickers:
            try:
                out[t] = float(data["Close"][t].iloc[-1])
            except Exception:
                out[t] = None
        return out
    except Exception:
        return {}


def cmd_show(base_dir: Path) -> None:
    cfg = load_config(base_dir)
    equity = int(cfg.get("equity", 10000))
    entry_pcts = cfg.get("regime_entry_pct", {"BULL": 20, "CHOP": 15, "BEAR": 10})
    pyramid_max = int(cfg.get("pyramid_max_pct", 25))

    regime = get_regime(base_dir)
    entry_pct = entry_pcts.get(regime, 10)
    pyramid_room = pyramid_max - entry_pct
    entry_dollars = int(equity * entry_pct / 100)
    room_dollars = int(equity * pyramid_room / 100)
    cap_dollars = int(equity * pyramid_max / 100)

    print()
    print(f"── SIZING FRAMEWORK  {regime} regime ──────────────────")
    print(f"  Equity:     ${equity:,}")
    print(f"  New entry:  {entry_pct}%  =  ${entry_dollars:,}")
    print(f"  Add room:   +{pyramid_room}% =  ${room_dollars:,}  (pyramid cap: {pyramid_max}% = ${cap_dollars:,})")
    print()

    db_path = base_dir / "broad-scan.db"
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    open_pos = conn.execute(
        "SELECT ticker, shares, entry_price, stop_price, entry_date "
        "FROM positions WHERE status = 'open'"
    ).fetchall()
    conn.close()

    if not open_pos:
        print("── OPEN POSITIONS ──────────────────────────────────")
        print("  No open positions. Clean slate.")
        print()
        return

    tickers = [r["ticker"] for r in open_pos]
    prices = get_live_prices(tickers)

    print("── OPEN POSITIONS ──────────────────────────────────")
    for row in open_pos:
        t = row["ticker"]
        shares = row["shares"] or 0
        entry = row["entry_price"] or 0
        live = prices.get(t)
        if live:
            cur_val = shares * live
            cur_pct = cur_val / equity * 100
            gain_pct = (live - entry) / entry * 100 if entry else 0
            gain_str = ("+" if gain_pct >= 0 else "") + f"{gain_pct:.1f}%"
            deployed = shares * entry
            deployed_pct = deployed / equity * 100
            room_to_add_pct = pyramid_max - deployed_pct
            room_to_add_dollars = int(equity * max(room_to_add_pct, 0) / 100)
            print(
                f"  {t}   {int(shares)}sh @ ${entry:.2f}   "
                f"live: ${live:.2f} ({gain_str})   "
                f"mktval: ${cur_val:,.0f} ({cur_pct:.1f}% of equity)"
            )
            if deployed_pct >= pyramid_max:
                print(
                    f"       deployed: {deployed_pct:.1f}% = ${deployed:,.0f}  |  "
                    f"pyramid: CLOSED -- above {pyramid_max}% cap (natural growth ok)"
                )
            else:
                print(
                    f"       deployed: {deployed_pct:.1f}% = ${deployed:,.0f}  |  "
                    f"pyramid room: +{room_to_add_pct:.1f}% = ${room_to_add_dollars:,}"
                )
        else:
            print(f"  {t}   {int(shares)}sh @ ${entry:.2f}   (price unavailable)")
        print()


def cmd_equity(base_dir: Path, amount: int) -> None:
    cfg = load_config(base_dir)
    cfg["equity"] = amount
    save_config(base_dir, cfg)
    print(f"Equity updated to ${amount:,}")


def main() -> None:
    parser = argparse.ArgumentParser(description="locker sizing framework")
    parser.add_argument("base_dir", help="broad-scan repo root")
    parser.add_argument("subcommand", nargs="?", default="show")
    parser.add_argument("value", nargs="?", default=None)
    args = parser.parse_args()

    base_dir = Path(args.base_dir)

    if args.subcommand == "equity" and args.value is not None:
        cmd_equity(base_dir, int(args.value))
    else:
        cmd_show(base_dir)


if __name__ == "__main__":
    main()
