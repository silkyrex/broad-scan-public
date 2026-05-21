"""End-of-week scan report.

Generates a Discord post + PDF summary for the current (or specified) week.

By default pulls the DB from the VPS before running so local is always
current. Pass --no-sync to skip (e.g. when running on the VPS itself).

Sections:
  1. New Entries   -- tickers with is_new_signal=1 for the first time this week
  2. Multi-Signal Leaders -- tickers with the most distinct scan types hit
  3. Persistent Names -- tickers that appeared in scans 3+ days (default)
  4. Locker Cross-Reference -- active locker members that showed up in scans

Usage:
  python -m scanner.report_weekly
  python -m scanner.report_weekly --week 2026-05-16
  python -m scanner.report_weekly --min-signals 3 --min-days 4
  python -m scanner.report_weekly --no-discord --no-pdf   # print only
  python -m scanner.report_weekly --no-sync               # skip DB pull
"""
from __future__ import annotations

import argparse
import json
import os
import sqlite3
import subprocess
import urllib.request
from datetime import date, timedelta
from pathlib import Path

from . import discord_router

DB_PATH  = Path(os.environ.get("BROAD_SCAN_DB", Path(__file__).parent.parent / "broad-scan.db"))
WEBHOOK  = os.environ.get("DISCORD_SCANS_WEBHOOK", "")
DROPLET  = os.environ.get("BROAD_SCAN_DROPLET", "root@your-vps.example.com")
REMOTE_DB = os.environ.get("BROAD_SCAN_REMOTE_DB", "/opt/broad-scan/broad-scan.db")


def sync_db_from_droplet() -> None:
    """Pull the live DB from the VPS. Skipped automatically when running on the VPS."""
    result = subprocess.run(
        ["scp", f"{DROPLET}:{REMOTE_DB}", str(DB_PATH)],
        capture_output=True, text=True,
    )
    if result.returncode != 0:
        print(f"  WARNING: DB sync failed -- {result.stderr.strip()}. Using existing local copy.")
    else:
        size_kb = DB_PATH.stat().st_size // 1024
        print(f"  DB synced from {DROPLET} ({size_kb} KB)")

SIGNAL_TYPES = {"rsi_daily", "rsi_weekly", "rsi_monthly", "52w_high", "ath", "12w_high"}
SIGNAL_LABELS = {
    "rsi_daily": "RSI-D", "rsi_weekly": "RSI-W", "rsi_monthly": "RSI-M",
    "52w_high": "52W", "ath": "ATH", "12w_high": "12W",
}
SIGNAL_SQL = "('rsi_daily','rsi_weekly','rsi_monthly','52w_high','ath','12w_high')"


def current_week_ending() -> str:
    today = date.today()
    days_to_sat = (5 - today.weekday()) % 7
    return (today + timedelta(days=days_to_sat)).isoformat()


def week_dates(conn: sqlite3.Connection, week_ending: str) -> list[str]:
    rows = conn.execute(
        "SELECT DISTINCT scan_date FROM scans WHERE week_ending=? ORDER BY scan_date",
        (week_ending,),
    ).fetchall()
    return [r[0] for r in rows]


def fetch_sctr(conn: sqlite3.Connection, tickers: list[str], week_ending: str) -> dict[str, float]:
    if not tickers:
        return {}
    placeholders = ",".join("?" * len(tickers))
    rows = conn.execute(
        f"""SELECT s.ticker, s.value FROM scans s
            INNER JOIN (
                SELECT ticker, MAX(scan_date) AS latest
                FROM scans WHERE scan_type='sc_workbench' AND week_ending=?
                GROUP BY ticker
            ) m ON s.ticker=m.ticker AND s.scan_date=m.latest
            WHERE s.scan_type='sc_workbench' AND s.value IS NOT NULL
              AND s.ticker IN ({placeholders})""",
        [week_ending, *tickers],
    ).fetchall()
    return {t: v for t, v in rows}


def query_new_entries(conn: sqlite3.Connection, week_ending: str) -> list[dict]:
    rows = conn.execute(
        f"""SELECT ticker,
                   GROUP_CONCAT(DISTINCT scan_type) as types,
                   COUNT(DISTINCT scan_type)         as n_signals,
                   COUNT(DISTINCT scan_date)         as n_days,
                   MIN(scan_date)                    as first_seen
            FROM scans
            WHERE week_ending=? AND is_new_signal=1 AND scan_type IN {SIGNAL_SQL}
            GROUP BY ticker
            ORDER BY n_signals DESC, n_days DESC""",
        (week_ending,),
    ).fetchall()
    return [{"ticker": r[0], "types": r[1].split(","), "n_signals": r[2],
             "n_days": r[3], "first_seen": r[4]} for r in rows]


def query_leaders(conn: sqlite3.Connection, week_ending: str, min_signals: int) -> list[dict]:
    rows = conn.execute(
        f"""SELECT ticker,
                   GROUP_CONCAT(DISTINCT scan_type) as types,
                   COUNT(DISTINCT scan_type)         as n_signals,
                   COUNT(DISTINCT scan_date)         as n_days,
                   SUM(is_new_signal)                 as new_hits
            FROM scans
            WHERE week_ending=? AND scan_type IN {SIGNAL_SQL}
            GROUP BY ticker
            HAVING n_signals >= ?
            ORDER BY n_signals DESC, new_hits DESC, n_days DESC""",
        (week_ending, min_signals),
    ).fetchall()
    return [{"ticker": r[0], "types": r[1].split(","), "n_signals": r[2],
             "n_days": r[3], "new_hits": r[4]} for r in rows]


def query_persistent(conn: sqlite3.Connection, week_ending: str, min_days: int) -> list[dict]:
    rows = conn.execute(
        f"""SELECT ticker,
                   COUNT(DISTINCT scan_date) as n_days,
                   COUNT(DISTINCT scan_type) as n_signals
            FROM scans
            WHERE week_ending=? AND scan_type IN {SIGNAL_SQL}
            GROUP BY ticker
            HAVING n_days >= ?
            ORDER BY n_days DESC, n_signals DESC""",
        (week_ending, min_days),
    ).fetchall()
    return [{"ticker": r[0], "n_days": r[1], "n_signals": r[2]} for r in rows]


def query_locker_cross(conn: sqlite3.Connection, week_ending: str) -> list[dict]:
    rows = conn.execute(
        f"""SELECT lr.ticker,
                   COUNT(DISTINCT s.scan_type) as n_signals,
                   COUNT(DISTINCT s.scan_date) as n_days,
                   SUM(s.is_new_signal)          as new_hits
            FROM locker_room lr
            JOIN scans s ON lr.ticker=s.ticker AND s.week_ending=?
                         AND s.scan_type IN {SIGNAL_SQL}
            WHERE lr.status='active'
            GROUP BY lr.ticker
            ORDER BY n_signals DESC, n_days DESC""",
        (week_ending,),
    ).fetchall()
    return [{"ticker": r[0], "n_signals": r[1], "n_days": r[2], "new_hits": r[3]} for r in rows]


def fmt_signals(types: list[str]) -> str:
    """One RSI + one high, priority order. RSI: weekly > daily > monthly. High: ATH > 52W > 12W."""
    t = set(types)
    rsi = ("RSI-W" if "rsi_weekly"  in t else
           "RSI-D" if "rsi_daily"   in t else
           "RSI-M" if "rsi_monthly" in t else None)
    high = ("ATH" if "ath"      in t else
            "52W" if "52w_high" in t else
            "12W" if "12w_high" in t else None)
    return "  ".join(x for x in [rsi, high] if x) or "--"


def build_discord(week_ending: str, days: list[str], new_entries: list, leaders: list,
                  persistent: list, locker: list, sctr: dict) -> str:
    n_days = len(days)
    lines = [f"**EOW Report -- week ending {week_ending}** | {n_days} trading day{'s' if n_days != 1 else ''}"]

    def tbl(rows: list[dict], cols: list[tuple]) -> list[str]:
        hdr = "  ".join(f"{h:<{w}}" for h, _, w in cols)
        sep = "-" * (sum(w + 2 for _, _, w in cols) - 2)
        out = ["```", hdr, sep]
        for row in rows[:15]:
            out.append("  ".join(f"{str(row.get(k, '--')):<{w}}" for _, k, w in cols))
        if len(rows) > 15:
            out.append(f"  ... and {len(rows) - 15} more")
        out.append("```")
        return out

    def sctr_fmt(ticker: str) -> str:
        v = sctr.get(ticker)
        return f"{v:.1f}" if v is not None else "--"

    # Section 1
    lines.append(f"\n**1. New Entries** -- {len(new_entries)} names -- first-time crossovers this week")
    if new_entries:
        rows_fmt = [{"T": r["ticker"], "Signals": fmt_signals(r["types"]) or "--",
                     "Days": r["n_days"], "SCTR": sctr_fmt(r["ticker"])} for r in new_entries]
        lines += tbl(rows_fmt, [("Ticker", "T", 7), ("Signals", "Signals", 22), ("Days", "Days", 4), ("SCTR", "SCTR", 6)])
    else:
        lines.append("_None_")

    # Section 2
    lines.append(f"\n**2. Conviction Leaders** -- {len(leaders)} names -- 2+ signals confirming strength")
    if leaders:
        rows_fmt = [{"T": r["ticker"], "N": r["n_signals"], "Signals": fmt_signals(r["types"]) or "--",
                     "Days": r["n_days"], "SCTR": sctr_fmt(r["ticker"])} for r in leaders]
        lines += tbl(rows_fmt, [("Ticker", "T", 7), ("N", "N", 2), ("Signals", "Signals", 20), ("Days", "Days", 4), ("SCTR", "SCTR", 6)])
    else:
        lines.append("_None_")

    # Section 3
    lines.append(f"\n**3. Persistent Names** -- {len(persistent)} names ({len(days[0]) if days else '?'}+ days)")
    if persistent:
        rows_fmt = [{"T": r["ticker"], "Days": r["n_days"], "Sigs": r["n_signals"],
                     "SCTR": sctr_fmt(r["ticker"])} for r in persistent]
        lines += tbl(rows_fmt, [("Ticker", "T", 7), ("Days", "Days", 4), ("Sigs", "Sigs", 4), ("SCTR", "SCTR", 6)])
    else:
        lines.append("_None_")

    # Section 4
    lines.append(f"\n**4. Locker in Scans** -- {len(locker)} active members hit")
    if locker:
        rows_fmt = [{"T": r["ticker"], "Sigs": r["n_signals"], "Days": r["n_days"],
                     "New": r["new_hits"]} for r in locker]
        lines += tbl(rows_fmt, [("Ticker", "T", 7), ("Sigs", "Sigs", 4), ("Days", "Days", 4), ("New", "New", 3)])
    else:
        lines.append("_No active locker members in scans_")

    return "\n".join(lines)


def post_discord(message: str) -> None:
    if len(message) > 1950:
        message = message[:1950] + "\n..._(truncated)_"
    discord_router.send_to("research", message)


def build_pdf(week_ending: str, days: list[str], new_entries: list, leaders: list,
              persistent: list, locker: list, sctr: dict, out_path: Path) -> Path:
    from reportlab.lib.pagesizes import letter
    from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle
    from reportlab.lib.units import inch
    from reportlab.platypus import SimpleDocTemplate, Paragraph, Spacer, Table, TableStyle
    from reportlab.lib import colors

    doc = SimpleDocTemplate(str(out_path), pagesize=letter,
                            leftMargin=0.6*inch, rightMargin=0.6*inch,
                            topMargin=0.6*inch, bottomMargin=0.6*inch)
    styles = getSampleStyleSheet()
    h1 = ParagraphStyle("H1", parent=styles["Heading1"], fontSize=14, spaceAfter=4)
    h2 = ParagraphStyle("H2", parent=styles["Heading2"], fontSize=11, spaceAfter=3, spaceBefore=10)
    body = ParagraphStyle("Body", parent=styles["Normal"], fontSize=8)

    ts = TableStyle([
        ("BACKGROUND",  (0, 0), (-1, 0),  colors.HexColor("#2C3E50")),
        ("TEXTCOLOR",   (0, 0), (-1, 0),  colors.white),
        ("FONTNAME",    (0, 0), (-1, 0),  "Helvetica-Bold"),
        ("FONTSIZE",    (0, 0), (-1, -1), 8),
        ("ROWBACKGROUNDS", (0, 1), (-1, -1), [colors.white, colors.HexColor("#F4F6F7")]),
        ("GRID",        (0, 0), (-1, -1), 0.25, colors.HexColor("#BDC3C7")),
        ("TOPPADDING",  (0, 0), (-1, -1), 3),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 3),
        ("LEFTPADDING", (0, 0), (-1, -1), 5),
    ])

    def make_table(header: list, rows: list) -> Table:
        t = Table([header] + rows[:30], hAlign="LEFT")
        t.setStyle(ts)
        return t

    def sctr_fmt(ticker: str) -> str:
        v = sctr.get(ticker)
        return f"{v:.1f}" if v is not None else "--"

    n_days = len(days)
    story = [
        Paragraph(f"EOW Report -- Week ending {week_ending} ({n_days} trading day{'s' if n_days != 1 else ''})", h1),
        Spacer(1, 6),
    ]

    story.append(Paragraph(f"1. New Entries ({len(new_entries)} names) -- first-time crossovers this week", h2))
    if new_entries:
        story.append(make_table(
            ["Ticker", "Signals", "Days", "First Seen", "SCTR"],
            [[r["ticker"], fmt_signals(r["types"]) or "--", r["n_days"], r["first_seen"], sctr_fmt(r["ticker"])]
             for r in new_entries],
        ))
    else:
        story.append(Paragraph("None this week.", body))

    story.append(Paragraph(f"2. Conviction Leaders ({len(leaders)} names) -- 2+ signals confirming strength", h2))
    if leaders:
        story.append(make_table(
            ["Ticker", "N", "Signals", "Days", "SCTR"],
            [[r["ticker"], r["n_signals"], fmt_signals(r["types"]) or "--", r["n_days"], sctr_fmt(r["ticker"])]
             for r in leaders],
        ))
    else:
        story.append(Paragraph("None this week.", body))

    story.append(Paragraph(f"3. Persistent Names ({len(persistent)} names, 3+ days)", h2))
    if persistent:
        story.append(make_table(
            ["Ticker", "Days", "Signals", "SCTR"],
            [[r["ticker"], r["n_days"], r["n_signals"], sctr_fmt(r["ticker"])] for r in persistent],
        ))
    else:
        story.append(Paragraph("None this week.", body))

    story.append(Paragraph(f"4. Locker Room in Scans ({len(locker)} active members)", h2))
    if locker:
        story.append(make_table(
            ["Ticker", "Signals", "Days", "New Entries", "SCTR"],
            [[r["ticker"], r["n_signals"], r["n_days"], r["new_hits"], sctr_fmt(r["ticker"])]
             for r in locker],
        ))
    else:
        story.append(Paragraph("No active locker members in scans this week.", body))

    doc.build(story)
    return out_path


def main() -> None:
    p = argparse.ArgumentParser(description="EOW broad-scan report")
    p.add_argument("--week", default=None, help="Week-ending YYYY-MM-DD (default: current)")
    p.add_argument("--min-signals", type=int, default=2, help="Min signals for leaders section (default: 2)")
    p.add_argument("--min-days", type=int, default=3, help="Min days for persistent section (default: 3)")
    p.add_argument("--no-discord", action="store_true")
    p.add_argument("--no-pdf", action="store_true")
    p.add_argument("--no-sync", action="store_true", help="Skip DB pull from VPS")
    args = p.parse_args()

    week = args.week or current_week_ending()
    print(f"EOW report -- week ending {week}")

    if not args.no_sync:
        sync_db_from_droplet()

    conn = sqlite3.connect(DB_PATH)
    days = week_dates(conn, week)
    if not days:
        print(f"No scan data for week ending {week}. Exiting.")
        conn.close()
        return

    print(f"  {len(days)} trading days: {', '.join(days)}")

    new_entries = query_new_entries(conn, week)
    leaders     = query_leaders(conn, week, args.min_signals)
    persistent  = query_persistent(conn, week, args.min_days)
    locker      = query_locker_cross(conn, week)

    all_tickers = list({r["ticker"] for rows in [new_entries, leaders, persistent, locker] for r in rows})
    sctr = fetch_sctr(conn, all_tickers, week)
    conn.close()

    # Sort all sections by SCTR desc (missing SCTR sorts last)
    def by_sctr(r: dict) -> tuple:
        v = sctr.get(r["ticker"])
        return (0 if v is None else 1, v or 0)

    new_entries = sorted(new_entries, key=by_sctr, reverse=True)
    leaders     = sorted(leaders,     key=by_sctr, reverse=True)
    persistent  = sorted(persistent,  key=by_sctr, reverse=True)
    locker      = sorted(locker,      key=by_sctr, reverse=True)

    print(f"  New: {len(new_entries)} | Leaders: {len(leaders)} | Persistent: {len(persistent)} | Locker: {len(locker)}")

    if not args.no_discord:
        msg = build_discord(week, days, new_entries, leaders, persistent, locker, sctr)
        post_discord(msg)
        print("  Discord posted")

    if not args.no_pdf:
        out = Path.home() / "Desktop" / f"eow-report-{week}.pdf"
        build_pdf(week, days, new_entries, leaders, persistent, locker, sctr, out)
        print(f"  PDF: {out}")
        subprocess.Popen(["open", str(out)])


if __name__ == "__main__":
    main()
