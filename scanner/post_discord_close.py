#!/usr/bin/env python3
"""Post today's close-trigger scan hits to Discord #signals channel."""
import json
import os
import sqlite3
import urllib.request
from datetime import date
from pathlib import Path

from . import discord_router

DB_PATH = Path(os.environ.get("BROAD_SCAN_DB", Path(__file__).parent.parent / "broad-scan.db"))
OB1_URL = os.environ.get(
    "KB_MCP_URL",
    "http://your-vps.example.com:8000/mcp?key=***"
)


def ob1_capture(content: str) -> None:
    try:
        payload = json.dumps({
            "jsonrpc": "2.0", "method": "tools/call",
            "params": {"name": "capture_thought", "arguments": {"content": content}},
            "id": 1,
        }).encode()
        req = urllib.request.Request(
            OB1_URL, data=payload, method="POST",
            headers={"Content-Type": "application/json",
                     "Accept": "application/json, text/event-stream",
                     "User-Agent": "broad-scan"},
        )
        urllib.request.urlopen(req, timeout=15)
    except Exception:
        pass

CLOSE_TAG_LABELS = {
    "sc_email_52w_high_close":          "52W",
    "sc_email_ath_close":               "ATH",
    "sc_email_12w_high_close":          "12W",
    "sc_email_rsi_daily_above_close":   "RSI-D>",
    "sc_email_rsi_daily_cross_close":   "RSI-Dx",
    "sc_email_rsi_weekly_above_close":  "RSI-W>",
    "sc_email_rsi_weekly_cross_close":  "RSI-Wx",
    "sc_email_rsi_monthly_above_close": "RSI-M>",
    "sc_email_rsi_monthly_cross_close": "RSI-Mx",
}


def fetch_close_hits(scan_date: str) -> dict[str, set[str]]:
    con = sqlite3.connect(DB_PATH)
    rows = con.execute(
        "SELECT ticker, scan_type FROM scans "
        "WHERE scan_date=? AND is_new_signal=1 AND scan_type LIKE '%_close'",
        (scan_date,),
    ).fetchall()
    con.close()
    hits: dict[str, set[str]] = {}
    for ticker, scan_type in rows:
        hits.setdefault(ticker, set()).add(scan_type)
    return hits


def fetch_sctr(tickers: list, scan_date: str) -> dict:
    if not tickers:
        return {}
    placeholders = ",".join("?" * len(tickers))
    con = sqlite3.connect(DB_PATH)
    rows = con.execute(
        f"SELECT ticker, value FROM scans "
        f"WHERE scan_type='sc_workbench' AND scan_date=? AND ticker IN ({placeholders})",
        [scan_date, *tickers],
    ).fetchall()
    con.close()
    return {t: v for t, v in rows if v is not None}


def build_message(hits: dict[str, set[str]], sctr: dict, scan_date: str) -> str:
    if not hits:
        return ""
    tickers = sorted(hits.keys(), key=lambda t: (-len(hits[t]), -(sctr.get(t) or 0)))
    n = len(tickers)
    header = f"**Scan CLOSE {scan_date}** | {n} name{'s' if n != 1 else ''}"
    lines = ["```", f"{'Ticker':<8}  Signals                          SCTR"]
    lines.append("-" * 50)
    for t in tickers:
        labels = " ".join(CLOSE_TAG_LABELS.get(tag, tag) for tag in sorted(hits[t]))
        s = f"{sctr[t]:>6.1f}" if t in sctr else "    --"
        lines.append(f"{t:<8}  {labels:<32}  {s}")
    lines.append("```")
    return header + "\n" + "\n".join(lines)


def post(message: str) -> None:
    discord_router.send_to("signals", message)


if __name__ == "__main__":
    if date.today().weekday() >= 5:
        raise SystemExit(0)
    scan_date = os.environ.get("SCAN_DATE", str(date.today()))
    hits = fetch_close_hits(scan_date)
    if not hits:
        raise SystemExit(0)
    sctr = fetch_sctr(list(hits.keys()), scan_date)
    msg = build_message(hits, sctr, scan_date)
    post(msg)

    # auto_promote removed 2026-05-20 -- 1-signal gate was 4x noisier than 2-signal
    # auto_prospect (step 9 in run-post-close.sh) is the sole admission gate

    # Group by signal type for KB thought
    ath   = [t for t, s in hits.items() if any("ath" in x for x in s)]
    hi52  = [t for t, s in hits.items() if any("52w" in x for x in s)]
    rsi_d = [t for t, s in hits.items() if any("rsi_daily" in x for x in s)]
    rsi_w = [t for t, s in hits.items() if any("rsi_weekly" in x for x in s)]
    top_sctr = sorted([(t, sctr[t]) for t in hits if t in sctr], key=lambda x: -x[1])[:5]
    sctr_str = ", ".join(f"{t} {v:.0f}" for t, v in top_sctr)
    ob1_capture(
        f"Broad scan CLOSE [{scan_date}]: {len(hits)} canonical close signals.\n"
        f"ATH: {', '.join(ath) or 'none'}\n"
        f"52W High: {', '.join(hi52) or 'none'}\n"
        f"RSI Daily: {', '.join(rsi_d) or 'none'}\n"
        f"RSI Weekly: {', '.join(rsi_w) or 'none'}\n"
        f"Top SCTR: {sctr_str or 'n/a'}\n"
        "Source: SC At Close emails -- canonical signal for locker promotion. Agent: broad-scan."
    )
