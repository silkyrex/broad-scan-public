"""
theme_buzz.py

Scores tickers by how many theme ETFs they appear in and whether those themes
have positive 1-month momentum. Replaces product_buzz.py.

Signal logic:
  in_theme      = ticker appears in >= 1 theme ETF top holdings
  theme_momentum = avg 1-month return of matched ETFs (weighted by holding %)
  buzz          = in_theme AND theme_momentum > 0

Score = etf_count + (theme_momentum / 10)
  etf_count is primary (breadth of theme exposure)
  theme_momentum adds nuance (is the theme hot right now?)

Holdings are cached daily at ~/.cache/buzz/theme_map.json.
"""
import argparse
import json
import warnings
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import date
from pathlib import Path

import pandas as pd
import yfinance as yf

warnings.filterwarnings("ignore", category=FutureWarning)

CACHE_DIR  = Path.home() / ".cache" / "buzz"
CACHE_FILE = CACHE_DIR / "theme_map.json"

MAX_WORKERS = 8

# Theme -> ETFs. Add a new ETF ticker here to add a new theme.
THEME_ETFS: dict[str, list[str]] = {
    "ai_robotics":   ["BOTZ", "ROBO", "IRBO"],
    "space":         ["UFO"],
    "autonomous":    ["DRIV"],
    "genomics":      ["ARKG"],
    "nuclear":       ["NLR", "URNM"],
    "cybersecurity": ["HACK"],
    "defense":       ["ITA", "XAR"],
    "clean_energy":  ["ICLN"],
    "disruptive":    ["ARKK"],
}


def _etf_data(etf: str) -> dict:
    """Fetch top holdings + 1-month return for one ETF."""
    try:
        holdings_df = yf.Ticker(etf).funds_data.top_holdings
        hist        = yf.Ticker(etf).history(period="1mo")
        ret         = round(
            (hist["Close"].iloc[-1] / hist["Close"].iloc[0] - 1) * 100, 2
        ) if len(hist) >= 2 else 0.0

        # Filter to US-listed tickers (no dots -- excludes JP, HK, EU exchanges)
        holdings = {
            sym: float(row["Holding Percent"])
            for sym, row in holdings_df.iterrows()
            if "." not in sym
        }
        return {"etf": etf, "holdings": holdings, "ret_1mo": ret}
    except Exception as e:
        return {"etf": etf, "holdings": {}, "ret_1mo": 0.0, "error": str(e)}


def build_theme_map(force: bool = False) -> dict:
    """
    Build ticker -> theme metadata map. Cached daily.
    Returns: { ticker: { "themes": [...], "etfs": [...], "etf_count": N,
                          "avg_momentum": X, "holdings": {etf: pct} } }
    """
    CACHE_DIR.mkdir(parents=True, exist_ok=True)

    if not force and CACHE_FILE.exists():
        cached = json.loads(CACHE_FILE.read_text())
        if cached.get("date") == date.today().isoformat():
            return cached["data"]

    print("Building theme map from ETF holdings...")
    all_etfs = [etf for etfs in THEME_ETFS.values() for etf in etfs]

    etf_results: dict[str, dict] = {}
    with ThreadPoolExecutor(max_workers=MAX_WORKERS) as pool:
        futures = {pool.submit(_etf_data, etf): etf for etf in all_etfs}
        for f in as_completed(futures):
            r = f.result()
            etf_results[r["etf"]] = r

    # Build ticker -> theme metadata
    theme_map: dict[str, dict] = {}
    for theme, etfs in THEME_ETFS.items():
        for etf in etfs:
            r = etf_results.get(etf, {})
            for ticker, hold_pct in r.get("holdings", {}).items():
                if ticker not in theme_map:
                    theme_map[ticker] = {
                        "themes": [], "etfs": [], "etf_count": 0,
                        "holdings": {}, "ret_1mo_by_etf": {},
                    }
                entry = theme_map[ticker]
                if theme not in entry["themes"]:
                    entry["themes"].append(theme)
                if etf not in entry["etfs"]:
                    entry["etfs"].append(etf)
                entry["etf_count"] = len(entry["etfs"])
                entry["holdings"][etf] = hold_pct
                entry["ret_1mo_by_etf"][etf] = r.get("ret_1mo", 0.0)

    # Compute weighted avg momentum per ticker
    for ticker, entry in theme_map.items():
        rets    = list(entry["ret_1mo_by_etf"].values())
        weights = [entry["holdings"].get(etf, 1.0) for etf in entry["etfs"]]
        if weights and sum(weights) > 0:
            avg_mom = sum(r * w for r, w in zip(rets, weights)) / sum(weights)
        else:
            avg_mom = 0.0
        entry["avg_momentum"] = round(avg_mom, 2)

    CACHE_FILE.write_text(json.dumps({"date": date.today().isoformat(), "data": theme_map}))
    print(f"  {len(theme_map)} tickers mapped across {len(all_etfs)} ETFs")
    return theme_map


def score(ticker: str, theme_map: dict | None = None) -> dict:
    """Score a single ticker for theme buzz."""
    if theme_map is None:
        theme_map = build_theme_map()

    entry = theme_map.get(ticker.upper())
    if not entry:
        return {
            "ticker": ticker, "in_theme": False, "theme_score": 0.0,
            "buzz": False, "themes": [], "etfs": [], "etf_count": 0,
            "avg_momentum": 0.0,
        }

    etf_count    = entry["etf_count"]
    avg_momentum = entry["avg_momentum"]
    theme_score  = round(etf_count + (avg_momentum / 10), 2)

    return {
        "ticker":       ticker,
        "in_theme":     True,
        "theme_score":  theme_score,
        "buzz":         avg_momentum > 0,   # theme must be trending up
        "themes":       entry["themes"],
        "etfs":         entry["etfs"],
        "etf_count":    etf_count,
        "avg_momentum": avg_momentum,
    }


def scan(tickers: list[str], theme_map: dict | None = None) -> pd.DataFrame:
    """Batch score -- returns DataFrame ready for broad_scan_bridge."""
    if theme_map is None:
        theme_map = build_theme_map()
    rows = [score(t, theme_map) for t in tickers]
    return (
        pd.DataFrame(rows)
        .sort_values("theme_score", ascending=False)
        .reset_index(drop=True)
    )


def main():
    parser = argparse.ArgumentParser(description="Theme buzz scanner (ETF-based)")
    parser.add_argument("--ticker",      help="Ticker or comma-separated list")
    parser.add_argument("--rebuild",     action="store_true", help="Force refresh ETF cache")
    parser.add_argument("--show-map",    action="store_true", help="Print full theme map")
    args = parser.parse_args()

    theme_map = build_theme_map(force=args.rebuild)

    if args.show_map:
        for t, e in sorted(theme_map.items(), key=lambda x: -x[1]["etf_count"]):
            print(f"{t:<8} {e['etf_count']} ETFs  mom={e['avg_momentum']:+.1f}%  "
                  f"themes={e['themes']}")
        return

    if args.ticker:
        tickers = [t.strip().upper() for t in args.ticker.split(",")]
        if len(tickers) == 1:
            r = score(tickers[0], theme_map)
            for k, v in r.items():
                print(f"{k}: {v}")
        else:
            df = scan(tickers, theme_map)
            cols = ["ticker", "theme_score", "buzz", "etf_count", "avg_momentum", "themes"]
            print(df[[c for c in cols if c in df.columns]].to_string(index=False))


if __name__ == "__main__":
    main()
