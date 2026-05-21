"""Playwright fetcher for the saved StockCharts Workbench scan.

Drives the SC Advanced Scans UI, runs saved scan "01 - Broad Scan RSI > 67",
downloads the CSV, and (by default) hands it off to scanner.sc_import.

Headless by default. Set BROAD_SCAN_HEADLESS=0 to watch the browser.

Usage:
  python -m scanner.sc_fetch_workbench
  python -m scanner.sc_fetch_workbench --date 2026-05-15
  python -m scanner.sc_fetch_workbench --no-import     # download only
  BROAD_SCAN_HEADLESS=0 python -m scanner.sc_fetch_workbench
"""
from __future__ import annotations

import argparse
import os
import re
import sys
from datetime import date
from pathlib import Path

from dotenv import load_dotenv
from playwright.sync_api import sync_playwright

SC_ENV     = Path.home() / ".config/credentials/stockcharts.env"
SC_CONTEXT = Path.home() / ".config/stockcharts/playwright"
SCAN_NAME  = "01 - Broad Scan RSI > 67"
SCAN_URL   = "https://stockcharts.com/def/servlet/ScanUI"


def fetch_workbench_csv(out_path: Path, headless: bool = False) -> Path:
    load_dotenv(SC_ENV)
    email    = os.environ["SC_EMAIL"]
    password = os.environ["SC_PASSWORD"]

    SC_CONTEXT.mkdir(parents=True, exist_ok=True)
    out_path.parent.mkdir(parents=True, exist_ok=True)

    with sync_playwright() as pw:
        ctx = pw.chromium.launch_persistent_context(
            str(SC_CONTEXT),
            headless=headless,
            accept_downloads=True,
            args=["--no-sandbox"],
        )
        page = ctx.new_page()

        # Land on ScanUI; SC redirects to login if cookies are stale
        page.goto(SCAN_URL, wait_until="networkidle", timeout=60000)
        if page.query_selector("input[name='form_UserID']"):
            print("Login required; submitting credentials")
            page.fill("input[name='form_UserID']", email)
            page.fill("input[name='form_UserPassword']", password)
            cb = page.query_selector("input[name='form_RememberMe']")
            if cb:
                cb.check()
            submit = page.query_selector("button[type='submit'], input[type='submit']")
            if submit:
                submit.click()
            else:
                page.keyboard.press("Enter")
            page.wait_for_load_state("networkidle", timeout=20000)
            page.goto(SCAN_URL, wait_until="networkidle", timeout=60000)

        if page.query_selector("input[name='form_UserID']"):
            raise RuntimeError("Login did not stick — still on login page after auth attempt")

        # Saved scan lives in a <select> dropdown
        select_loc = page.locator(f"select:has(option:has-text('{SCAN_NAME}'))").first
        select_loc.wait_for(timeout=10000)
        select_loc.select_option(label=SCAN_NAME)
        page.wait_for_load_state("networkidle", timeout=20000)

        # Run Scan opens results in a new tab. In headless, the button can take
        # a moment to render after the dropdown change; wait for it explicitly.
        run_btn = page.locator("input[type='submit'][value*='Run Scan' i], input[type='button'][value*='Run Scan' i]")
        try:
            run_btn.first.wait_for(timeout=15000, state="visible")
        except Exception:
            raise RuntimeError("Run Scan control not found")
        with ctx.expect_page(timeout=60000) as popup_info:
            run_btn.first.click()
        results_page = popup_info.value
        results_page.wait_for_load_state("networkidle", timeout=90000)

        # Download CSV from the results page
        dl_locator = results_page.get_by_role("link", name=re.compile(r"CSV", re.I))
        if dl_locator.count() == 0:
            dl_locator = results_page.get_by_role("button", name=re.compile(r"CSV", re.I))
        if dl_locator.count() == 0:
            dl_locator = results_page.locator("input[value*='CSV' i]")
        if dl_locator.count() == 0:
            raise RuntimeError(f"CSV download control not found on results page {results_page.url}")

        with results_page.expect_download(timeout=60000) as dl:
            dl_locator.first.click()
        dl.value.save_as(out_path)

        ctx.close()

    print(f"Downloaded {out_path} ({out_path.stat().st_size:,} bytes)")
    return out_path


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--date", default=date.today().isoformat(),
                   help="Scan date in YYYY-MM-DD (default: today)")
    p.add_argument("--out", default=None,
                   help="Output CSV path (default: /tmp/sc-workbench-<date>.csv)")
    p.add_argument("--no-import", action="store_true",
                   help="Just download; skip sc_import ingest")
    args = p.parse_args()

    scan_date = date.fromisoformat(args.date)
    out_path = Path(args.out) if args.out else Path(f"/tmp/sc-workbench-{scan_date.isoformat()}.csv")
    headless = os.environ.get("BROAD_SCAN_HEADLESS", "1") == "1"

    fetch_workbench_csv(out_path, headless=headless)

    if args.no_import:
        return 0

    from scanner.sc_import import import_sc_results
    import_sc_results(str(out_path), scan_date)
    return 0


if __name__ == "__main__":
    sys.exit(main())
