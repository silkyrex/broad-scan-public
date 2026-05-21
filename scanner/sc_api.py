"""StockCharts HTTP API primitives.

Bootstraps a logged-in session via Playwright (one-time per run), then exposes
plain HTTP helpers backed by stockcharts.com/json/api.

Credentials: ~/.config/credentials/stockcharts.env (SC_EMAIL, SC_PASSWORD).
Persistent browser context: ~/.config/stockcharts/playwright/ (shared with
locker-sync so cookies survive across CLIs).
"""
from __future__ import annotations

import os
from pathlib import Path

import requests
from dotenv import load_dotenv
from playwright.sync_api import sync_playwright

SC_BASE    = "https://stockcharts.com/json/api"
SC_CC      = "1393683"
SC_CC_INT  = int(SC_CC)
SC_ENV     = Path.home() / ".config/credentials/stockcharts.env"
SC_CONTEXT = Path.home() / ".config/stockcharts/playwright"


def sc_session() -> requests.Session:
    """Return a requests.Session with SC auth cookies, refreshed via Playwright."""
    load_dotenv(SC_ENV)
    email    = os.environ["SC_EMAIL"]
    password = os.environ["SC_PASSWORD"]

    SC_CONTEXT.mkdir(parents=True, exist_ok=True)

    with sync_playwright() as pw:
        ctx  = pw.chromium.launch_persistent_context(str(SC_CONTEXT), headless=True, args=["--no-sandbox"])
        page = ctx.new_page()
        page.goto("https://stockcharts.com/panels/", wait_until="networkidle")
        if page.query_selector("input[name='form_UserID']"):
            page.fill("input[name='form_UserID']", email)
            page.fill("input[name='form_UserPassword']", password)
            cb = page.query_selector("input[name='form_RememberMe']")
            if cb:
                cb.check()
            page.keyboard.press("Enter")
            page.wait_for_url("**/panels/**", timeout=15000)
        cookies = ctx.cookies()
        ctx.close()

    s = requests.Session()
    for c in cookies:
        s.cookies.set(c["name"], c["value"], domain=c["domain"])
    s.headers.update({
        "User-Agent":       "Mozilla/5.0",
        "Referer":          "https://stockcharts.com/panels/",
        "X-Requested-With": "XMLHttpRequest",
    })
    return s


def sc_clear(s: requests.Session, listnum: int) -> None:
    r = s.get(f"{SC_BASE}?cmd=get-favorites&cc={SC_CC}&ln={listnum}", timeout=10)
    for fav in r.json().get("favorites", []):
        s.post(f"{SC_BASE}?cmd=delete-favorite&cc={SC_CC}",
               json={"recID": fav["recID"], "listNum": listnum, "chartCode": SC_CC_INT},
               timeout=5)


def sc_add(s: requests.Session, listnum: int, tickers: list[str]) -> list[str]:
    """Add tickers to listnum. Returns list of tickers that failed."""
    failed = []
    for ticker in tickers:
        r = s.post(f"{SC_BASE}?cmd=add-favorite&cc={SC_CC}",
                   json={"listNum": listnum, "symbol": ticker,
                         "chartCode": SC_CC_INT, "comments": ""},
                   timeout=5)
        if not r.json().get("success"):
            failed.append(ticker)
    return failed
