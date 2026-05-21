"""locker pick — regime-aware ranked list of all VALID setups."""

import argparse
import json
import re
import subprocess
import sys
import warnings
from pathlib import Path

warnings.filterwarnings("ignore")


def score_row(ema: float, rsi: float, w52: float) -> int:
    ema_score = -5 if ema < 0 else 1 if ema <= 1 else 3 if ema <= 10 else 0 if ema <= 15 else -2
    rsi_score = 2 if 60 <= rsi <= 75 else 1 if 50 <= rsi < 60 else 0 if rsi <= 80 else -1
    w52_score = 2 if w52 >= -10 else 1 if w52 >= -20 else 0 if w52 >= -30 else -1
    return ema_score + rsi_score + w52_score


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


def get_ob1_context(base_dir: Path, tickers: list[str]) -> dict[str, str]:
    out = {}
    for ticker in tickers:
        try:
            result = subprocess.run(
                [
                    sys.executable, "-c",
                    f"import sys; sys.path.insert(0,'.')\n"
                    f"from scanner.kb import search\n"
                    f"r = search('{ticker} entry exit trade outcome', limit=2, threshold=0.40)\n"
                    f"print(' / '.join(r[:2]) if r else 'no prior history')",
                ],
                capture_output=True, text=True, cwd=base_dir,
            )
            out[ticker] = result.stdout.strip() or "no prior history"
        except Exception:
            out[ticker] = "no prior history"
    return out


def load_sizing(base_dir: Path, regime: str) -> tuple[int, int, int, int]:
    cfg_path = base_dir / "config" / "sizing.json"
    cfg = json.loads(cfg_path.read_text()) if cfg_path.exists() else {}
    equity = int(cfg.get("equity", 10000))
    entry_pcts = cfg.get("regime_entry_pct", {"BULL": 20, "CHOP": 15, "BEAR": 10})
    pyramid_max = int(cfg.get("pyramid_max_pct", 25))
    entry_pct = entry_pcts.get(regime, 10)
    return equity, entry_pct, pyramid_max, pyramid_max - entry_pct


def run_pick(db_path: Path, base_dir: Path) -> None:
    data = json.loads(db_path.read_text())

    rows = []
    for r in data:
        ts = r.get("toby_status", "")
        if ts not in ("hold", "reclaim"):
            continue
        if r.get("has_position"):
            continue
        ema = r.get("ema_4e_pct") or 0
        rsi = r.get("rsi_d") or 0
        w52 = r.get("w52_high_pct") or 0
        score = score_row(ema, rsi, w52)
        ema_fmt = ("^" + str(round(abs(ema), 1))) if ema >= 0 else ("v" + str(round(abs(ema), 1)))
        rows.append((score, r["ticker"], ema_fmt, rsi, w52, ts, r.get("tape", "")))

    rows.sort(reverse=True)

    if not rows:
        print("\n  No VALID setups right now.\n")
        return

    regime = get_regime(base_dir)
    equity, entry_pct, pyramid_max, pyramid_room = load_sizing(base_dir, regime)
    entry_dollars = int(equity * entry_pct / 100)
    pyramid_room_dollars = int(equity * pyramid_room / 100)

    tickers = [r[1] for r in rows]
    kb = get_ob1_context(base_dir, tickers)

    top_valid_lines = "\n".join(
        f"{ticker}|{ema_fmt}|{rsi:.0f}|{w52:.1f}|{ts}|{tape}|{score}"
        for score, ticker, ema_fmt, rsi, w52, ts, tape in rows
    )
    ob1_lines = "\n".join(f"  {t}: {kb.get(t, 'no prior history')}" for t in tickers)

    prompt = (
        f"You are a concise trading brief assistant. Rules: Toby entry protocol "
        f"(fresh 4 EMA reclaim preferred; toby_status=reclaim > hold).\n\n"
        f"Regime: {regime}. Entry size: {entry_pct}% of equity (${entry_dollars}). "
        f"Pyramid cap: {pyramid_max}% max total deployed per ticker. "
        f"Add room: {pyramid_room}% = ${pyramid_room_dollars}.\n"
        f"Fresh reclaim = previous close below 4 EMA, today crossed above with rising EMA slope.\n\n"
        f"ALL VALID setups (best quality first, format: TICKER|EMA%|RSI|52wH%|status|tape|score):\n"
        f"{top_valid_lines}\n\n"
        f"KB prior history:\n{ob1_lines}\n"
        f"Output one line per ticker in this exact format:\n"
        f"TICKER -- [one-phrase reason: EMA tightness, RSI, 52wH%, status; buzz if elevated; "
        f"append KB note if prior history exists] -- "
        f"[ENTRY ({entry_pct}% = ${entry_dollars}) / PULLBACK ENTRY / LOWER PRIORITY / PASS]\n\n"
        f"Use ENTRY for clean reclaim or tight EMA (<=10%) + RSI 60-75 + within 10% of 52wh.\n"
        f"Use PULLBACK ENTRY if momentum is strong but EMA extended (10-20%) -- wait for retrace.\n"
        f"Use LOWER PRIORITY if setup is valid but weak signal or slow mover.\n"
        f"Use PASS if EMA >20% extended OR more than 25% off 52wh.\n"
        f"If KB shows a prior bad exit on a ticker, downgrade one level (ENTRY -> PULLBACK, etc).\n\n"
        f"After the ranked list, one final line: regime + entry size + pyramid room in one sentence.\n"
        f"No preamble. No headers. Tight."
    )

    print()
    print(
        f"\033[1m🎯 locker pick\033[0m  "
        f"\033[2m{regime} regime · {entry_pct}% entry (${entry_dollars}) · "
        f"+{pyramid_room}% pyramid room\033[0m"
    )
    print()

    result = subprocess.run(
        ["claude", "-p", "--model", "claude-sonnet-4-6"],
        input=prompt, capture_output=True, text=True,
    )
    for line in result.stdout.splitlines():
        print(f"  {line}")
    print()


def main() -> None:
    parser = argparse.ArgumentParser(description="locker pick — ranked VALID setups")
    parser.add_argument("--db", required=True, help="path to momentum JSON temp file")
    parser.add_argument("--base-dir", default=None, help="broad-scan repo root")
    args = parser.parse_args()

    db_path = Path(args.db)
    base_dir = Path(args.base_dir) if args.base_dir else db_path.parent.parent
    # If base_dir looks wrong, try to find it from the script location
    script_dir = Path(__file__).resolve().parent.parent
    if not (script_dir / "config" / "sizing.json").exists():
        base_dir = script_dir
    else:
        base_dir = script_dir

    run_pick(db_path, base_dir)


if __name__ == "__main__":
    main()
