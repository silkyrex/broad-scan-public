#!/usr/bin/env python3
"""Generate a triage PDF for a batch of locker candidates using StockCharts 0003 weekly.

Usage:
    python3 triage_pdf.py MRVL GLW JBL          # explicit tickers
    python3 triage_pdf.py --batch 2026-05-06     # all locker names added on date
    python3 triage_pdf.py --pending              # from pending_promotions.json
Output: ~/Desktop/locker-triage-YYYY-MM-DD.pdf  (auto-opens)
"""
import argparse
import json
import os
import subprocess
import sys
import tempfile
import time
from datetime import date
from pathlib import Path
from urllib.parse import urlparse, parse_qs

import requests
from playwright.sync_api import sync_playwright
from reportlab.lib import colors
from reportlab.lib.enums import TA_CENTER, TA_LEFT
from reportlab.lib.pagesizes import letter
from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
from reportlab.lib.units import inch
from reportlab.platypus import Image, PageBreak, Paragraph, SimpleDocTemplate, Spacer

SC_CREDS   = Path.home() / ".config/credentials/stockcharts.env"
LOCKER     = Path.home() / "projects/do-infra-local/trading/locker_room.json"
PENDING    = Path("/opt/do-infra-drop/trading/pending_promotions.json")
PENDING_LOCAL = Path.home() / "projects/do-infra-local/trading/pending_promotions.json"
OUT_DIR    = Path.home() / "Desktop"
PROBE      = "SPY"

SC_SYM_MAP = {"DX-Y.NYB": "$USD", "^VIX": "$VIX"}


# ── StockCharts ───────────────────────────────────────────────────────────────

def load_creds():
    env = {}
    for line in SC_CREDS.read_text().splitlines():
        if "=" in line and not line.startswith("#"):
            k, v = line.split("=", 1)
            env[k.strip()] = v.strip()
    return env


def login_sc(page, email, password):
    print("  Logging into StockCharts...", end=" ", flush=True)
    page.goto("https://stockcharts.com/login/", wait_until="domcontentloaded")
    page.wait_for_timeout(1000)
    page.fill("#form_UserID", email)
    page.fill("#form_UserPassword", password)
    page.press("#form_UserPassword", "Enter")
    page.wait_for_load_state("networkidle")
    print("ok", flush=True)


def get_style_id(page):
    page.goto(
        f"https://stockcharts.com/h-sc/ui?s={PROBE}&p=W&yr=2&mn=0&dy=0",
        wait_until="networkidle", timeout=45000,
    )
    page.wait_for_timeout(3000)
    el = page.query_selector('img[src*="stockcharts.com/c-sc/sc"]')
    if not el:
        raise RuntimeError("Could not find chart image -- login may have failed")
    src = el.get_attribute("src")
    style_id = parse_qs(urlparse(src).query).get("i", [None])[0]
    if not style_id:
        raise RuntimeError(f"No 'i=' in chart src: {src}")
    print(f"  Chartstyle ID: {style_id}", flush=True)
    return style_id


def download_chart(ticker, style_id, cookies, tmp_dir):
    sym = SC_SYM_MAP.get(ticker, ticker)
    url = (f"https://stockcharts.com/c-sc/sc?s={sym}&p=W&yr=2&mn=0&dy=0"
           f"&i={style_id}&r={int(time.time()*1000)}")
    resp = requests.get(url, cookies=cookies, timeout=20, headers={
        "Referer": "https://stockcharts.com/",
        "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36",
    })
    resp.raise_for_status()
    if "image" not in resp.headers.get("Content-Type", ""):
        raise RuntimeError(f"Expected image, got {resp.headers.get('Content-Type')}")
    path = os.path.join(tmp_dir, f"{ticker.replace('^','').replace('.','_')}.png")
    with open(path, "wb") as f:
        f.write(resp.content)
    return path


# ── Data sources ──────────────────────────────────────────────────────────────

def load_by_date(added_date):
    d = json.loads(LOCKER.read_text())
    return [(r["ticker"], r.get("reason","")) for r in d if r.get("added") == added_date]


def load_pending():
    path = PENDING if PENDING.exists() else PENDING_LOCAL
    if not path.exists():
        return []
    d = json.loads(path.read_text())
    return [(r["ticker"], r.get("reason","")) for r in d]


# ── PDF ───────────────────────────────────────────────────────────────────────

def build_pdf(pairs, chart_paths, out_path):
    styles = getSampleStyleSheet()
    title_s = ParagraphStyle("t", parent=styles["Heading1"], alignment=TA_CENTER,
                             fontSize=14, spaceAfter=6)
    reason_s = ParagraphStyle("r", parent=styles["Normal"], fontSize=9,
                              textColor=colors.HexColor("#555577"), spaceAfter=4)

    doc = SimpleDocTemplate(str(out_path), pagesize=letter,
                            leftMargin=0.4*inch, rightMargin=0.4*inch,
                            topMargin=0.4*inch, bottomMargin=0.4*inch)
    story = []
    for ticker, reason in pairs:
        story.append(Paragraph(ticker, title_s))
        if reason:
            story.append(Paragraph(reason, reason_s))
        path = chart_paths.get(ticker)
        if path and os.path.exists(path):
            story.append(Image(path, width=7.3*inch, height=5.2*inch))
        else:
            story.append(Paragraph("[chart unavailable]", styles["Normal"]))
        story.append(PageBreak())
    doc.build(story)


# ── Main ─────────────────────────────────────────────────────────────────────

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("tickers", nargs="*")
    ap.add_argument("--batch", metavar="YYYY-MM-DD")
    ap.add_argument("--pending", action="store_true")
    args = ap.parse_args()

    if args.pending:
        pairs = load_pending()
        if not pairs:
            sys.exit("No pending promotions found.")
    elif args.batch:
        pairs = load_by_date(args.batch)
        if not pairs:
            sys.exit(f"No locker names with added={args.batch}")
    elif args.tickers:
        pairs = [(t.upper(), "") for t in args.tickers]
    else:
        ap.print_help(); sys.exit(1)

    creds = load_creds()
    out = OUT_DIR / f"locker-triage-{date.today()}.pdf"

    print(f"Triage PDF — {len(pairs)} tickers", flush=True)

    chart_paths = {}
    with tempfile.TemporaryDirectory() as tmp:
        with sync_playwright() as pw:
            browser = pw.chromium.launch(headless=True)
            page = browser.new_context(viewport={"width": 1400, "height": 900}).new_page()
            login_sc(page, creds["SC_EMAIL"], creds["SC_PASSWORD"])
            style_id = get_style_id(page)
            cookies = {c["name"]: c["value"] for c in page.context.cookies()}
            browser.close()

        print(f"Downloading {len(pairs)} charts...", flush=True)
        for ticker, _ in pairs:
            try:
                chart_paths[ticker] = download_chart(ticker, style_id, cookies, tmp)
                print(f"  {ticker} ok", flush=True)
            except Exception as e:
                print(f"  {ticker} SKIP: {e}", flush=True)

        print("Building PDF...", flush=True)
        build_pdf(pairs, chart_paths, out)

    print(f"\nSaved: {out}")
    subprocess.Popen(["open", str(out)])


if __name__ == "__main__":
    main()
