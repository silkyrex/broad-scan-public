"""
Volume buzz: port of MarketSmith Volumes (Pine) to yfinance.

Core formula (matches Pine exactly):
    vol_buzz_pct = 100 * (vol / ma) - 100

Additional metrics ported from Pine:
    ud_ratio  -- up/down volume ratio over 50 bars (Pine: sumUp/sumDn)
    is_hve    -- today is the highest volume ever in available history
    is_hv1    -- today is the highest volume in the past 252 trading days
"""
import argparse
import warnings
from concurrent.futures import ThreadPoolExecutor, as_completed

import pandas as pd
import yfinance as yf

warnings.filterwarnings("ignore", category=FutureWarning)

MA_DAILY   = 50    # Pine default lenDa
MA_WEEKLY  = 10    # Pine default lenWe
UD_WINDOW  = 50    # Pine: sumUp/sumDn lookback
HV1_DAYS   = 252   # one trading year
THRESHOLD  = 25.0  # Pine labels trigger at >25% above MA
MAX_WORKERS = 8


def _fetch(ticker: str, period: str = "2y") -> pd.DataFrame:
    df = yf.Ticker(ticker).history(period=period, auto_adjust=True)
    if df.empty:
        raise ValueError(f"no data for {ticker}")
    return df


def score(ticker: str, ma_len: int = MA_DAILY, threshold: float = THRESHOLD) -> dict:
    """
    Return volume buzz metrics for a single ticker.

    vol_buzz_pct matches Pine's volBuzz = 100*(vol/ma)-100.
    buzz=True when vol_buzz_pct >= threshold (Pine labels trigger at >25%).

    HVE uses full available history (period='max') to match Pine's all-time tracking.
    HV1 uses the 252-day window from the same full fetch.
    """
    try:
        t = yf.Ticker(ticker)
        # Full history for accurate HVE/HV1
        df_full = t.history(period="max", auto_adjust=True)
        if df_full.empty:
            raise ValueError(f"no data for {ticker}")
        # 2y slice for MA and buzz calc (speed + recency)
        df = df_full.iloc[-504:] if len(df_full) > 504 else df_full
    except Exception as e:
        return {"ticker": ticker, "vol_buzz_pct": None, "buzz": False, "error": str(e)}

    vol   = df["Volume"]
    close = df["Close"]

    if len(vol) < ma_len + 1:
        return {"ticker": ticker, "vol_buzz_pct": None, "buzz": False,
                "error": "insufficient history"}

    ma        = vol.rolling(ma_len).mean()
    today_vol = int(vol.iloc[-1])
    ma_vol    = int(ma.iloc[-1])

    # Core buzz -- Pine: volBuzz = 100*(vol/ma)-100
    vol_buzz_pct = round(100 * (today_vol / ma_vol) - 100, 1)

    # Up/Down volume ratio -- Pine: sumUp/sumDn over 50 bars
    up_vol = vol.where(close > close.shift(1), 0.0)
    dn_vol = vol.where(close < close.shift(1), 0.0)
    sum_up = up_vol.rolling(UD_WINDOW).sum().iloc[-1]
    sum_dn = dn_vol.rolling(UD_WINDOW).sum().iloc[-1]
    ud_ratio = round(sum_up / sum_dn, 2) if sum_dn > 0 else None

    # HVE / HV1 -- Pine: hve (all-time), hvone = ta.highest(vol, 252)
    # Use full history for HVE so we match Pine's var accumulation
    all_vol   = df_full["Volume"]
    today_vol_alltime = int(all_vol.iloc[-1])
    is_hve    = today_vol_alltime >= int(all_vol.max())
    hv1_max   = int(all_vol.iloc[-HV1_DAYS:].max())
    is_hv1    = (today_vol_alltime >= hv1_max) and not is_hve

    # Avg $ volume -- Pine: advDol = close * ma
    avg_dol_vol = round(close.iloc[-1] * ma_vol, 0)

    return {
        "ticker":       ticker,
        "vol_buzz_pct": vol_buzz_pct,   # Pine's volBuzz
        "buzz":         vol_buzz_pct >= threshold,
        "ud_ratio":     ud_ratio,        # >1 = more up-vol; Pine's upDnVolRatio
        "is_hve":       is_hve,          # true all-time high volume
        "is_hv1":       is_hv1,          # highest vol in past 252 days
        "today_vol":    today_vol,
        "ma_vol":       ma_vol,
        "avg_dol_vol":  avg_dol_vol,
        "ma_len":       ma_len,
    }


def scan(tickers: list[str], ma_len: int = MA_DAILY, threshold: float = THRESHOLD,
         max_workers: int = MAX_WORKERS) -> pd.DataFrame:
    """Parallel batch score -- returns a DataFrame ready to merge into scans.csv."""
    results = {}
    with ThreadPoolExecutor(max_workers=max_workers) as pool:
        futures = {pool.submit(score, t, ma_len, threshold): t for t in tickers}
        for f in as_completed(futures):
            row = f.result()
            results[row["ticker"]] = row

    # preserve original order
    rows = [results[t] for t in tickers if t in results]
    df = pd.DataFrame(rows)
    return df.sort_values("vol_buzz_pct", ascending=False, na_position="last")


def main():
    parser = argparse.ArgumentParser(description="Volume buzz scanner (MarketSmith-style)")
    parser.add_argument("--ticker",     required=True, help="Ticker or comma-separated list")
    parser.add_argument("--ma",         type=int,   default=MA_DAILY)
    parser.add_argument("--threshold",  type=float, default=THRESHOLD,
                        help="Min %% above MA to flag buzz (default 25)")
    parser.add_argument("--workers",    type=int,   default=MAX_WORKERS)
    args = parser.parse_args()

    tickers = [t.strip().upper() for t in args.ticker.split(",")]

    if len(tickers) == 1:
        result = score(tickers[0], ma_len=args.ma, threshold=args.threshold)
        for k, v in result.items():
            print(f"{k}: {v}")
    else:
        df = scan(tickers, ma_len=args.ma, threshold=args.threshold, max_workers=args.workers)
        cols = ["ticker", "vol_buzz_pct", "buzz", "ud_ratio", "is_hve", "is_hv1",
                "today_vol", "ma_vol", "avg_dol_vol"]
        print(df[[c for c in cols if c in df.columns]].to_string(index=False))


if __name__ == "__main__":
    main()
