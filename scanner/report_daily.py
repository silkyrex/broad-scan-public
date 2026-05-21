"""Daily close-of-day scan report.

Queries today's highest-conviction tickers, posts a Discord summary, then
downloads SC weekly charts (chartstyle 0003) for the top names and posts them
as Discord image attachments.

Runs after run-close.sh completes (cron: 1:30 PM PT M-F).

Usage:
  python -m scanner.report_daily
  python -m scanner.report_daily --date 2026-05-15
  python -m scanner.report_daily --top 10 --min-signals 3
  python -m scanner.report_daily --no-discord --no-charts   # dry-run print
"""
from __future__ import annotations

import argparse
import io
import json
import os
import sqlite3
import time
import urllib.request
from datetime import date
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from . import discord_router

DB_PATH   = Path(os.environ.get("BROAD_SCAN_DB", Path(__file__).parent.parent / "broad-scan.db"))
WEBHOOK   = os.environ.get("DISCORD_SCANS_WEBHOOK", "")
# operator's chartstyle 0003 -- extract once and hardcode; update if SC rotates it
SC_STYLE  = os.environ.get("SC_CHART_STYLE", "your-chartstyle-id")
SC_ENV    = Path.home() / ".config/credentials/stockcharts.env"
SC_CONTEXT = Path.home() / ".config/stockcharts/playwright"

SIGNAL_TYPES = {"rsi_daily", "rsi_weekly", "rsi_monthly", "52w_high", "ath", "12w_high"}
SIGNAL_LABELS = {
    "rsi_daily": "RSI-D", "rsi_weekly": "RSI-W", "rsi_monthly": "RSI-M",
    "52w_high": "52W", "ath": "ATH", "12w_high": "12W",
}
SIGNAL_SQL = "('rsi_daily','rsi_weekly','rsi_monthly','52w_high','ath','12w_high')"


def top_signals(types: list[str]) -> str:
    """Reduce fired signals to one RSI + one high label, priority order.

    RSI priority:  weekly > daily > monthly
    High priority: ATH > 52w > 12w
    """
    t = set(types)
    rsi = ("RSI-W" if "rsi_weekly" in t else
           "RSI-D" if "rsi_daily"  in t else
           "RSI-M" if "rsi_monthly" in t else None)
    high = ("ATH" if "ath"      in t else
            "52W" if "52w_high" in t else
            "12W" if "12w_high" in t else None)
    return "  ".join(x for x in [rsi, high] if x) or "--"

# Close-scan labels (new entries at close that aren't in morning signal set)
CLOSE_LABELS = {
    "sc_email_52w_high_close":          "52W-c",
    "sc_email_ath_close":               "ATH-c",
    "sc_email_12w_high_close":          "12W-c",
    "sc_email_rsi_daily_above_close":   "RSI-D>",
    "sc_email_rsi_daily_cross_close":   "RSI-Dx",
    "sc_email_rsi_weekly_above_close":  "RSI-W>",
    "sc_email_rsi_weekly_cross_close":  "RSI-Wx",
    "sc_email_rsi_monthly_above_close": "RSI-M>",
    "sc_email_rsi_monthly_cross_close": "RSI-Mx",
}


# ---------------------------------------------------------------------------
# DB queries
# ---------------------------------------------------------------------------

def query_leaders(conn: sqlite3.Connection, scan_date: str, min_signals: int) -> list[dict]:
    """Tickers with the most distinct signal types today, sorted by hit count desc."""
    rows = conn.execute(
        f"""SELECT ticker,
                   GROUP_CONCAT(DISTINCT scan_type) AS types,
                   COUNT(DISTINCT scan_type)         AS n_signals
            FROM scans
            WHERE scan_date=? AND scan_type IN {SIGNAL_SQL}
            GROUP BY ticker
            HAVING n_signals >= ?
            ORDER BY n_signals DESC, ticker""",
        (scan_date, min_signals),
    ).fetchall()
    return [
        {"ticker": r[0], "types": [t for t in r[1].split(",") if t in SIGNAL_TYPES], "n_signals": r[2]}
        for r in rows
    ]


def query_new_entries(conn: sqlite3.Connection, scan_date: str) -> list[dict]:
    """Morning signal tickers that are new entries today."""
    rows = conn.execute(
        f"""SELECT ticker,
                   GROUP_CONCAT(DISTINCT scan_type) AS types,
                   COUNT(DISTINCT scan_type)         AS n_signals
            FROM scans
            WHERE scan_date=? AND is_new_signal=1 AND scan_type IN {SIGNAL_SQL}
            GROUP BY ticker
            ORDER BY n_signals DESC, ticker""",
        (scan_date,),
    ).fetchall()
    return [
        {"ticker": r[0], "types": [t for t in r[1].split(",") if t in SIGNAL_TYPES], "n_signals": r[2]}
        for r in rows
    ]


def query_close_new(conn: sqlite3.Connection, scan_date: str) -> dict[str, set[str]]:
    """Close-scan new entries keyed by ticker."""
    rows = conn.execute(
        "SELECT ticker, scan_type FROM scans "
        "WHERE scan_date=? AND is_new_signal=1 AND scan_type LIKE '%_close'",
        (scan_date,),
    ).fetchall()
    hits: dict[str, set[str]] = {}
    for ticker, scan_type in rows:
        hits.setdefault(ticker, set()).add(scan_type)
    return hits


def query_locker(conn: sqlite3.Connection, scan_date: str) -> list[dict]:
    """Active locker members that hit any signal today."""
    rows = conn.execute(
        f"""SELECT lr.ticker,
                   COUNT(DISTINCT s.scan_type) AS n_signals,
                   GROUP_CONCAT(DISTINCT s.scan_type) AS types
            FROM locker_room lr
            JOIN scans s ON lr.ticker=s.ticker AND s.scan_date=?
                         AND s.scan_type IN {SIGNAL_SQL}
            WHERE lr.status='active'
            GROUP BY lr.ticker
            ORDER BY n_signals DESC, lr.ticker""",
        (scan_date,),
    ).fetchall()
    return [
        {"ticker": r[0], "n_signals": r[1],
         "types": [t for t in r[2].split(",") if t in SIGNAL_TYPES]}
        for r in rows
    ]


def query_sctr(conn: sqlite3.Connection, tickers: list[str], scan_date: str) -> dict[str, float]:
    if not tickers:
        return {}
    placeholders = ",".join("?" * len(tickers))
    rows = conn.execute(
        f"SELECT ticker, value FROM scans "
        f"WHERE scan_type='sc_workbench' AND scan_date=? AND value IS NOT NULL "
        f"AND ticker IN ({placeholders})",
        [scan_date, *tickers],
    ).fetchall()
    return {t: v for t, v in rows}


# ---------------------------------------------------------------------------
# Discord helpers
# ---------------------------------------------------------------------------

def _post_json(webhook: str, payload: dict) -> None:
    discord_router.send_to("research", payload["content"])


def _post_file(webhook: str, filename: str, image_bytes: bytes, content: str = "") -> None:
    """Post a PNG as a Discord webhook attachment (multipart/form-data)."""
    if content:
        discord_router.send_to("research", content)
    
    boundary = "----BroadScanBoundary"
    body_parts = []

    body_parts.append(
        f"--{boundary}\r\nContent-Disposition: form-data; name=\"file\"; "
        f"filename=\"{filename}\"\r\nContent-Type: image/png\r\n\r\n".encode()
        + image_bytes
        + f"\r\n--{boundary}--\r\n".encode()
    )

    body = b"".join(body_parts)
    req = urllib.request.Request(
        webhook,
        data=body,
        headers={
            "Content-Type": f"multipart/form-data; boundary={boundary}",
            "User-Agent": "broad-scan",
        },
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=30) as resp:
        if resp.status not in (200, 204):
            raise RuntimeError(f"Discord returned {resp.status}")


def _build_pdf(charts: list[tuple[str, bytes]], scan_date: str) -> bytes:
    """Compile (ticker, png_bytes) list into a single PDF, one chart per page."""
    from reportlab.lib.pagesizes import letter
    from reportlab.platypus import SimpleDocTemplate, Image as RLImage, Spacer, Paragraph
    from reportlab.lib.styles import getSampleStyleSheet
    from reportlab.lib.units import inch
    import io as _io

    buf = _io.BytesIO()
    doc = SimpleDocTemplate(buf, pagesize=letter,
                            leftMargin=0.5*inch, rightMargin=0.5*inch,
                            topMargin=0.5*inch, bottomMargin=0.5*inch)
    styles = getSampleStyleSheet()
    story = []
    for ticker, png_bytes in charts:
        story.append(Paragraph(f"<b>{ticker}</b> — {scan_date}", styles["Heading2"]))
        story.append(RLImage(_io.BytesIO(png_bytes), width=7.5*inch, height=4.5*inch))
        story.append(Spacer(1, 0.2*inch))
    doc.build(story)
    return buf.getvalue()


def _post_pdf(webhook: str, filename: str, pdf_bytes: bytes, content: str = "") -> None:
    """Post a PDF as a single Discord webhook attachment."""
    if content:
        discord_router.send_to("broad-scan", content)
    boundary = "----BroadScanPDFBoundary"
    body = (
        f"--{boundary}\r\nContent-Disposition: form-data; name=\"file\"; "
        f"filename=\"{filename}\"\r\nContent-Type: application/pdf\r\n\r\n".encode()
        + pdf_bytes
        + f"\r\n--{boundary}--\r\n".encode()
    )
    req = urllib.request.Request(
        webhook,
        data=body,
        headers={
            "Content-Type": f"multipart/form-data; boundary={boundary}",
            "User-Agent": "broad-scan",
        },
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=60) as resp:
        if resp.status not in (200, 204):
            raise RuntimeError(f"Discord PDF upload returned {resp.status}")


def _sctr_fmt(v: float | None) -> str:
    return f"{v:>5.1f}" if v is not None else "   --"


CG_BATCH = 12  # SC CandleGlance max tickers per URL


def _cg_url(tickers: list[str]) -> str:
    return f"https://stockcharts.com/freecharts/candleglance.html?{','.join(tickers)}" if tickers else ""


def build_new_entries_msg(scan_date: str, new_entries: list, close_new: dict,
                           locker: list, sctr: dict) -> str:
    lines = [f"**Daily Close -- {scan_date}**",
             f"\n**New Entries** ({len(new_entries)} names) -- crossed today for the first time"]
    if new_entries:
        lines += ["```", f"{'Ticker':<8}  {'Signal':<12}  SCTR", "-" * 34]
        for r in new_entries:
            lines.append(f"{r['ticker']:<8}  {top_signals(r['types']):<12}  {_sctr_fmt(sctr.get(r['ticker']))}")
        lines.append("```")
    else:
        lines.append("_None_")

    if close_new:
        lines.append(f"\n**Close Alerts** ({len(close_new)} names)")
        lines += ["```"]
        for ticker in sorted(close_new.keys(), key=lambda t: -len(close_new[t])):
            labels = " ".join(CLOSE_LABELS.get(tag, tag) for tag in sorted(close_new[ticker]))
            lines.append(f"{ticker:<8}  {labels}")
        lines.append("```")

    if locker:
        lines.append(f"\n**Locker in Scans** ({len(locker)} members)")
        lines += ["```"]
        for r in locker:
            lines.append(f"{r['ticker']:<8}  {r['n_signals']}x  {top_signals(r['types'])}")
        lines.append("```")

    return "\n".join(lines)


def build_conviction_msg(scan_date: str, leaders: list, sctr: dict) -> str:
    lines = [f"**Conviction Leaders** ({len(leaders)} names) -- 3+ signals today"]
    if leaders:
        lines += ["```", f"{'Ticker':<8}  {'N':>2}  {'Signal':<12}  SCTR", "-" * 36]
        for r in leaders:
            lines.append(f"{r['ticker']:<8}  {r['n_signals']:>2}  {top_signals(r['types']):<12}  {_sctr_fmt(sctr.get(r['ticker']))}")
        lines.append("```")
    else:
        lines.append("_None_")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Chart download
# ---------------------------------------------------------------------------

def _get_sc_session():
    """Return (requests.Session, style_id). SC chart images are public -- no auth needed."""
    import requests as req_lib
    s = req_lib.Session()
    s.headers.update({"User-Agent": "Mozilla/5.0", "Referer": "https://stockcharts.com/"})
    return s, SC_STYLE


# Map tickers SC doesn't accept as-is
SC_SYM_MAP = {
    "^VIX": "%24VIX", "^SPX": "%24SPX", "^INDU": "%24INDU",
    "^COMPQ": "%24COMPQ", "^NDX": "%24NDX",
}


def download_chart(session, ticker: str, style_id: str) -> bytes | None:
    sym = SC_SYM_MAP.get(ticker, ticker)
    url = (f"https://stockcharts.com/c-sc/sc?s={sym}&p=W&yr=2&mn=0&dy=0"
           f"&i={style_id}&r={int(time.time()*1000)}")
    try:
        resp = session.get(url, timeout=20, headers={"Referer": "https://stockcharts.com/"})
        if resp.status_code == 200 and resp.content[:4] == b"\x89PNG":
            return resp.content
    except Exception as e:
        print(f"  chart {ticker}: {e}")
    return None


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main() -> None:
    p = argparse.ArgumentParser(description="Daily broad-scan close report")
    p.add_argument("--date",        default=None, help="Scan date YYYY-MM-DD (default: today)")
    p.add_argument("--top",         type=int, default=8, help="Max tickers to chart (default: 8)")
    p.add_argument("--min-signals", type=int, default=3, help="Min signals for leaders (default: 3)")
    p.add_argument("--no-discord",  action="store_true")
    p.add_argument("--no-charts",   action="store_true")
    args = p.parse_args()

    scan_date = args.date or str(date.today())
    if date.fromisoformat(scan_date).weekday() >= 5:
        print("Weekend -- skipping.")
        raise SystemExit(0)

    print(f"Daily EOD report -- {scan_date}")

    conn = sqlite3.connect(DB_PATH)

    leaders    = query_leaders(conn, scan_date, args.min_signals)
    new_entries = query_new_entries(conn, scan_date)
    close_new  = query_close_new(conn, scan_date)
    locker     = query_locker(conn, scan_date)

    all_tickers = list({r["ticker"] for rows in [leaders, new_entries, locker] for r in rows}
                       | set(close_new.keys()))
    sctr = query_sctr(conn, all_tickers, scan_date)
    conn.close()

    # Sort both by SCTR desc
    leaders.sort(key=lambda r: (-(sctr.get(r["ticker"]) or 0)))
    new_entries.sort(key=lambda r: (-(sctr.get(r["ticker"]) or 0)))

    print(f"  Leaders: {len(leaders)} | New: {len(new_entries)} | "
          f"Close new: {len(close_new)} | Locker: {len(locker)}")

    if not leaders and not new_entries and not close_new:
        print("  No data -- nothing to post.")
        raise SystemExit(0)

    def post_msg(msg: str) -> None:
        if args.no_discord:
            print(msg)
        elif WEBHOOK:
            _post_json(WEBHOOK, {"content": msg})
        else:
            print(msg)

    def post_cg(tickers: list[str], label: str) -> None:
        if not tickers:
            return
        batches = [tickers[i:i + CG_BATCH] for i in range(0, len(tickers), CG_BATCH)]
        for i, batch in enumerate(batches):
            suffix = f" {i+1}/{len(batches)}" if len(batches) > 1 else ""
            url = _cg_url(batch)
            msg = f"[CandleGlance -- {label}{suffix} ({len(batch)} names)]({url})"
            if args.no_discord:
                print(f"  CandleGlance {label}{suffix}: {url}")
            elif WEBHOOK:
                _post_json(WEBHOOK, {"content": msg})
                print(f"  CandleGlance {label}{suffix}: posted")

    def post_charts(tickers: list[str], lookup: list, label: str,
                    session, style_id: str) -> None:
        print(f"  Downloading {len(tickers)} {label} charts...")
        collected: list[tuple[str, bytes]] = []
        for ticker in tickers:
            img = download_chart(session, ticker, style_id)
            if img is None:
                print(f"  {ticker}: chart unavailable")
                continue
            print(f"  {ticker}: {len(img)} bytes")
            collected.append((ticker, img))

        if not collected:
            return

        pdf_bytes = _build_pdf(collected, scan_date)
        filename = f"eod-{label}-{scan_date}.pdf"
        mb = len(pdf_bytes) / 1_048_576
        done_msg = f"📊 **EOD {label.title()} Charts** — {scan_date} · {len(collected)} tickers · {mb:.1f} MB"
        if args.no_discord:
            print(f"  [dry-run] Would post PDF: {filename} ({mb:.1f} MB)")
        elif WEBHOOK:
            _post_pdf(WEBHOOK, filename, pdf_bytes, content=done_msg)
            print(f"  PDF posted: {filename} ({mb:.1f} MB)")
        Path(f"/tmp/{filename}").write_bytes(pdf_bytes)
        print(f"  Saved: /tmp/{filename}")

    # --- Post sequence ---
    # 1. New Entries text
    post_msg(build_new_entries_msg(scan_date, new_entries, close_new, locker, sctr))
    print("  New entries text posted")

    if args.no_charts:
        # 2. Conviction text (no charts)
        post_msg(build_conviction_msg(scan_date, leaders, sctr))
        print("  Conviction text posted")
        return

    try:
        session, style_id = _get_sc_session()
    except Exception as e:
        print(f"  SC session failed: {e} -- skipping charts")
        post_msg(build_conviction_msg(scan_date, leaders, sctr))
        return

    new_tickers  = [r["ticker"] for r in new_entries]
    conv_tickers = [r["ticker"] for r in leaders if r["ticker"] not in set(new_tickers)]

    # 2. CandleGlance for new entries
    post_cg(new_tickers, "New Entries")

    # 3. New entry charts
    post_charts(new_tickers, new_entries, "new", session, style_id)

    # 4. Conviction Leaders text
    post_msg(build_conviction_msg(scan_date, leaders, sctr))
    print("  Conviction text posted")

    # 5. CandleGlance for conviction
    post_cg([r["ticker"] for r in leaders], "Conviction")

    # 6. Conviction-only charts (not already shown in new entries)
    post_charts(conv_tickers, leaders, "conviction", session, style_id)


if __name__ == "__main__":
    main()
