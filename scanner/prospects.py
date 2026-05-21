"""
Prospect data layer: enrichment, notes, insert, staleness, and DB sync.
All I/O with yfinance and Anthropic lives here; cli.py and sessions.py stay dry.
"""
import os
import subprocess
from datetime import date
from pathlib import Path

_DB_PATH = Path(os.environ.get("BROAD_SCAN_DB", Path(__file__).parent.parent / "broad-scan.db"))
_DROPLET = "root@your-vps.example.com"
_DROPLET_DB = "/opt/broad-scan/broad-scan.db"


def push_to_droplet():
    print("  pushing DB to VPS...")
    result = subprocess.run(
        ["scp", str(_DB_PATH), f"{_DROPLET}:{_DROPLET_DB}"],
        capture_output=True, text=True
    )
    if result.returncode == 0:
        print("  pushed.")
    else:
        print(f"  push failed: {result.stderr.strip()}")


def _build_notes_technical(ticker, conn):
    today = date.today().isoformat()
    sctr_row = conn.execute(
        "SELECT value FROM scans WHERE ticker=? AND scan_type='sc_workbench' ORDER BY scan_date DESC LIMIT 1",
        (ticker,)
    ).fetchone()
    recent = {r[0] for r in conn.execute(
        """SELECT scan_type FROM scans
           WHERE ticker=? AND is_new_signal=1
           AND scan_date >= date(?, '-7 days')
           AND scan_type IN ('rsi_daily','rsi_weekly','rsi_monthly','ath','52w_high','12w_high')
           GROUP BY scan_type""",
        (ticker, today)
    )}
    parts = []
    if sctr_row and sctr_row[0] is not None:
        parts.append(f"SCTR {sctr_row[0]:.1f}")
    for tag, key in [("RSI-D", "rsi_daily"), ("RSI-W", "rsi_weekly"), ("RSI-M", "rsi_monthly"),
                     ("ATH", "ath"), ("52w-high", "52w_high"), ("12w-high", "12w_high")]:
        if key in recent:
            parts.append(tag)
    return " | ".join(parts) if parts else None


def _fetch_enrichment(ticker):
    import yfinance as yf
    blank = {"sector": None, "industry": None, "market_cap": None,
             "beta": None, "short_pct_float": None}
    try:
        info = yf.Ticker(ticker).info
        return {
            "sector":          info.get("sector"),
            "industry":        info.get("industry"),
            "market_cap":      info.get("marketCap"),
            "beta":            info.get("beta"),
            "short_pct_float": info.get("shortPercentOfFloat"),
            "_info":           info,
        }
    except Exception:
        return blank


def _eval_thesis(thesis: str, ticker: str, sector: str | None = None) -> bool:
    """Sonnet eval: returns True if thesis passes quality bar, False to trigger sector fallback."""
    import sys
    # local pre-check: skip Sonnet call for obvious word-count failures
    word_count = len(thesis.split())
    if word_count > 12:
        print(f"  [eval] thesis rejected: {word_count} words (local pre-check)", file=sys.stderr)
        return False

    import anthropic
    import json
    import re
    prompt = (
        f"Ticker: {ticker}\nSector: {sector or 'unknown'}\n"
        f"Thesis: \"{thesis}\"\n\n"
        "Evaluate this momentum thesis. PASS only if ALL are true:\n"
        "1. Does NOT mention the company name or ticker symbol\n"
        "2. Does NOT contain signal tags (RSI, ATH, 52W-high, 12W-high, EMA, MACD, SMA)\n"
        "3. Names a SPECIFIC business catalyst -- not generic ('strong growth', 'revenue expansion', 'market leader')\n"
        "Return JSON only, reason 5 words max: {\"pass\": true/false, \"reason\": \"...\"}"
    )
    try:
        client = anthropic.Anthropic(timeout=10)
        msg = client.messages.create(
            model="claude-sonnet-4-6",
            max_tokens=60,
            messages=[{"role": "user", "content": prompt}],
        )
        raw = msg.content[0].text.strip()
        m = re.search(r"\{.*\}", raw, re.DOTALL)
        result = json.loads(m.group(0) if m else raw)
        passes = result.get("pass", True)
        if not passes:
            print(f"  [eval] thesis rejected: {result.get('reason', '')}", file=sys.stderr)
        return passes
    except Exception as e:
        print(f"  [eval] Sonnet eval failed ({e}), defaulting to pass", file=sys.stderr)
        return True


def _auto_notes(ticker, info=None):
    """yfinance + Haiku for one-line momentum thesis. Returns (thesis_str, enrichment_dict)."""
    import yfinance as yf
    import anthropic

    try:
        if info is None:
            info = yf.Ticker(ticker).info
        name = info.get("longName") or info.get("shortName") or ticker
        sector = info.get("sector") or info.get("industry") or ""
        summary = info.get("longBusinessSummary") or ""
        week52_change = info.get("52WeekChange")
    except Exception:
        return None, {}

    enrichment = {
        "sector":          info.get("sector"),
        "industry":        info.get("industry"),
        "market_cap":      info.get("marketCap"),
        "beta":            info.get("beta"),
        "short_pct_float": info.get("shortPercentOfFloat"),
    }

    if not summary:
        return (sector or None), enrichment

    try:
        perf = f", 52w change {week52_change:+.0%}" if week52_change is not None else ""
        prompt = (
            f"Ticker: {ticker}\nName: {name}\nSector: {sector}{perf}\n"
            f"Business: {summary[:400]}\n\n"
            "Write ONE plain sentence (12 words max, no period) capturing the business catalyst "
            "driving momentum. No company name. No signal tags. Just the catalyst."
        )
        client = anthropic.Anthropic(timeout=15)
        msg = client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=40,
            messages=[{"role": "user", "content": prompt}],
        )
        thesis = msg.content[0].text.strip().strip(".")
        if _eval_thesis(thesis, ticker, info.get("sector")):
            return thesis, enrichment
        return (sector or None), enrichment
    except Exception as e:
        import sys
        print(f"  [auto-notes] Haiku failed ({e}), using sector fallback", file=sys.stderr)
        return (sector or None), enrichment


def _insert_prospect(conn, ticker, source, notes, bucket="prospect",
                     notes_technical=None, enrichment=None, add_price=None):
    e = enrichment or {}
    conn.execute(
        """INSERT INTO prospects
               (ticker, added_date, source_scan, notes, notes_technical, status, bucket,
                sector, industry, market_cap, beta, short_pct_float, add_price)
           VALUES (?, ?, ?, ?, ?, 'active', ?, ?, ?, ?, ?, ?, ?)""",
        (ticker, date.today().isoformat(), source, notes, notes_technical, bucket,
         e.get("sector"), e.get("industry"), e.get("market_cap"),
         e.get("beta"), e.get("short_pct_float"), add_price)
    )
    print(f"  Added {ticker}  bucket={bucket}  source={source or '--'}")
    if notes:
        print(f"    notes:     {notes}")
    if notes_technical:
        print(f"    technical: {notes_technical}")
    if e.get("sector"):
        cap = f"  mktcap=${e['market_cap']//1_000_000_000:.0f}B" if e.get("market_cap") else ""
        beta_str = f"  beta={e['beta']:.2f}" if e.get("beta") else ""
        short_str = f"  short={e['short_pct_float']:.1%}" if e.get("short_pct_float") else ""
        print(f"    enriched:  {e['sector']} / {e.get('industry','')}{cap}{beta_str}{short_str}")


def _is_stale(added_date, notes_technical, threshold_days=14):
    age = (date.today() - date.fromisoformat(added_date)).days
    has_rsi = notes_technical and any(
        tag in notes_technical for tag in ("RSI-D", "RSI-W", "RSI-M")
    )
    return age >= threshold_days and not has_rsi
