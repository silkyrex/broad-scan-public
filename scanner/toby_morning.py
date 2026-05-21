#!/usr/bin/env python3
"""
Toby morning agent — score broad-scan candidates and post ranked picks to Discord.

Fires at 6:40 AM PT (5 min after broad-scan's 6:35 AM run).
Scores every active locker_room + prospect ticker on 4 criteria:
  - New highs (52w or ATH): 2 pts  [today's scans table]
  - Vol buzz (prev day):    1 pt   [yesterday's scans table, scan_type='vol_buzz']
  - 4 EMA reclaim:          1 pt   [locker_room.ema4_status or fresh yfinance for prospects]
  - RSI 70+ daily:          1 pt   [locker_room.rsi_d or today's scans table]

Posts all tickers with score >= 3 to Discord #broad-scan, ranked by score.
"""
import os
import sqlite3
import warnings
from datetime import date, timedelta
from pathlib import Path

warnings.filterwarnings("ignore")

from . import discord_router
from .kb import capture as ob1_capture

DB_PATH = Path(os.environ.get("BROAD_SCAN_DB", Path(__file__).parent.parent / "broad-scan.db"))

_HIGH_SCAN_TYPES = ("52w_high", "ath", "52w_high_close", "ath_close")
_RSI_SCAN_TYPES  = ("rsi_daily", "rsi_daily_above_close", "rsi_daily_cross_close")


def _fetch_today_highs(conn: sqlite3.Connection, today: str) -> set[str]:
    placeholders = ",".join("?" * len(_HIGH_SCAN_TYPES))
    rows = conn.execute(
        f"SELECT DISTINCT ticker FROM scans WHERE scan_date=? AND scan_type IN ({placeholders})",
        (today, *_HIGH_SCAN_TYPES),
    ).fetchall()
    return {r[0] for r in rows}


def _last_buzz_date(conn: sqlite3.Connection, today: str) -> str:
    """Most recent date with vol_buzz entries before today (skips weekends/holidays)."""
    row = conn.execute(
        "SELECT MAX(scan_date) FROM scans WHERE scan_type='vol_buzz' AND scan_date < ?",
        (today,),
    ).fetchone()
    return row[0] if row and row[0] else str(date.today() - timedelta(days=1))


def _fetch_prev_buzz(conn: sqlite3.Connection, yesterday: str) -> set[str]:
    rows = conn.execute(
        "SELECT DISTINCT ticker FROM scans WHERE scan_date=? AND scan_type='vol_buzz'",
        (yesterday,),
    ).fetchall()
    return {r[0] for r in rows}


def _fetch_today_rsi(conn: sqlite3.Connection, today: str) -> set[str]:
    placeholders = ",".join("?" * len(_RSI_SCAN_TYPES))
    rows = conn.execute(
        f"SELECT DISTINCT ticker FROM scans WHERE scan_date=? AND scan_type IN ({placeholders})",
        (today, *_RSI_SCAN_TYPES),
    ).fetchall()
    return {r[0] for r in rows}


def _score(ticker: str, source: str, ema4_status: str, rsi_d: float | None,
           highs_set: set, buzz_set: set, rsi_set: set) -> dict:
    new_high = ticker in highs_set
    vol_buzz = ticker in buzz_set
    reclaim  = ema4_status == "reclaim"
    rsi_hit  = (rsi_d is not None and rsi_d >= 70) or (ticker in rsi_set)
    score = (2 if new_high else 0) + (1 if vol_buzz else 0) + (1 if reclaim else 0) + (1 if rsi_hit else 0)
    return {"ticker": ticker, "source": source, "score": score,
            "new_high": new_high, "vol_buzz": vol_buzz, "reclaim": reclaim, "rsi_hit": rsi_hit}


def score_locker_tickers(conn: sqlite3.Connection, highs_set: set, buzz_set: set, rsi_set: set) -> list[dict]:
    rows = conn.execute(
        "SELECT ticker, rsi_d FROM locker_room WHERE status='active' AND ticker != 'TEST'"
    ).fetchall()
    tickers = [r[0] for r in rows]
    rsi_map  = {r[0]: r[1] for r in rows}
    ema4_map = _batch_ema4(tickers)
    return [_score(t, "locker", ema4_map.get(t, "below"), rsi_map.get(t), highs_set, buzz_set, rsi_set)
            for t in tickers]


def score_prospect_tickers(conn: sqlite3.Connection, highs_set: set, buzz_set: set, rsi_set: set) -> list[dict]:
    rows = conn.execute("SELECT ticker FROM prospects WHERE status='active'").fetchall()
    tickers = [r[0] for r in rows]
    if not tickers:
        return []
    ema4_map = _batch_ema4(tickers)
    return [_score(t, "prospect", ema4_map.get(t, "below"), None, highs_set, buzz_set, rsi_set)
            for t in tickers]


def _batch_ema4(tickers: list[str]) -> dict[str, str]:
    """Batch yfinance EMA4 status — mirrors locker/cli.py:_batch_ema4 exactly."""
    if not tickers:
        return {}
    try:
        import yfinance as yf
        data = yf.download(tickers, period="15d", progress=False, auto_adjust=True)
        closes = data["Close"]
        if hasattr(closes, "columns") and len(tickers) == 1 and tickers[0] not in closes.columns:
            closes.columns = tickers
        result = {}
        for t in tickers:
            try:
                s = closes[t].dropna().astype(float)
                if len(s) < 5:
                    result[t] = "unknown"
                    continue
                ema4       = s.ewm(span=4, adjust=False).mean()
                price      = float(s.iloc[-1])
                prev_close = float(s.iloc[-2])
                prev_ema   = float(ema4.iloc[-2])
                curr_ema   = float(ema4.iloc[-1])
                if prev_close < prev_ema and price > curr_ema:
                    result[t] = "reclaim"
                elif price > curr_ema:
                    result[t] = "above"
                else:
                    result[t] = "below"
            except Exception:
                result[t] = "unknown"
        return result
    except Exception:
        return {t: "unknown" for t in tickers}


def build_discord_message(hits: list[dict], today: str, locker_count: int, prospect_count: int) -> str:
    qualified = sorted([h for h in hits if h["score"] >= 3], key=lambda h: -h["score"])
    if not qualified:
        return (
            f"🔍 **Toby** — {today} · no candidates hit 3/4 criteria\n"
            f"_Locker: {locker_count} · Prospects: {prospect_count}_"
        )

    lines = [f"🔍 **Toby** — {today}\n"]
    for h in qualified:
        nh  = "✅ New high"      if h["new_high"] else "·  New high"
        vb  = "✅ Vol buzz"      if h["vol_buzz"] else "·  Vol buzz"
        rec = "✅ 4 EMA reclaim" if h["reclaim"]  else "·  4 EMA reclaim"
        rsi = "✅ RSI 70+"       if h["rsi_hit"]  else "·  RSI 70+"
        src = "L" if h["source"] == "locker" else "P"
        lines.append(f"`{h['ticker']:<6}` [{src}]  {nh}  {vb}  {rec}  {rsi}   [{h['score']}/4]")

    lines.append(f"\n_Locker: {locker_count} · Prospects: {prospect_count} · Vol buzz = prev day · Reclaim = last close_")
    return "\n".join(lines)


def build_ob1_content(hits: list[dict], today: str) -> str:
    qualified = [h for h in hits if h["score"] >= 3]
    if not qualified:
        return ""
    perfect = [h["ticker"] for h in qualified if h["score"] == 5]
    top     = [h["ticker"] for h in qualified]
    return (
        f"[{today}] Toby morning scan: {len(qualified)} candidates hit 3/4+ criteria. "
        f"Top picks: {', '.join(top[:5])}. "
        f"Perfect 4/4: {', '.join(perfect) or 'none'}. "
        "Criteria: new highs(2pt) + vol buzz(1pt) + 4 EMA reclaim(1pt) + RSI 70+(1pt). "
        "Agent: toby_morning."
    )


def run(db_path: Path = DB_PATH) -> None:
    if date.today().weekday() >= 5:
        return

    today = str(date.today())
    conn  = sqlite3.connect(db_path)
    yesterday = _last_buzz_date(conn, today)
    highs_set   = _fetch_today_highs(conn, today)
    buzz_set    = _fetch_prev_buzz(conn, yesterday)
    rsi_set     = _fetch_today_rsi(conn, today)
    locker_hits = score_locker_tickers(conn, highs_set, buzz_set, rsi_set)
    prospect_hits = score_prospect_tickers(conn, highs_set, buzz_set, rsi_set)
    conn.close()

    all_hits = locker_hits + prospect_hits
    msg = build_discord_message(all_hits, today, len(locker_hits), len(prospect_hits))
    discord_router.send_to("broad-scan", msg)

    thought = build_ob1_content(all_hits, today)
    if thought:
        ob1_capture(thought)


if __name__ == "__main__":
    run()
