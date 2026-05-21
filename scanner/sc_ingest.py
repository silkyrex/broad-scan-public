"""
Fetch tickers from StockCharts Broad scan alert emails via Gmail IMAP.

Reads all unread emails from support@stockcharts.com whose subject contains
SC_SCAN_PREFIX (default: "Broad"), extracts tickers from the HTML body,
and returns a deduplicated sorted list.

Credentials: broad-scan/.env  (GMAIL_USER, GMAIL_APP_PASSWORD, SC_SCAN_PREFIX)
"""
from __future__ import annotations

import email
import imaplib
import os
import re
from pathlib import Path

IMAP_HOST = "imap.gmail.com"
IMAP_PORT = 993
SENDER = "support@stockcharts.com"
ENV_PATH = Path(__file__).parent.parent / ".env"

SYM_RE = re.compile(r"sym=([A-Z]{1,5}(?:\.[A-Z])?)")
# Capture both the scan name and the trigger time ("Open" or "Close").
# `Close` is the canonical daily-bar signal -- the official print. `Open` is
# a provisional heads-up that lets operator get ahead of a potential print, but
# a name that opens above the threshold and reverses by close DID NOT trigger
# on the daily bar of record. Treat `_close` rows as authoritative for
# locker promotion / scoring; treat `_open` rows as preview only.
SUBJECT_RE = re.compile(r"Broad\s*-\s*(.+?)\s*\(At (Open|Close)\)", re.IGNORECASE)
# "Technical Alert | 01 - Broad Scan RSI > 67 | May 20, 2026 | StockCharts.com"
WORKBENCH_RE = re.compile(r"Technical Alert.*01.*Broad Scan RSI", re.IGNORECASE)

# SC alert names -> canonical scan_type stems. The full scan_type also
# carries the trigger-time suffix, e.g. `sc_email_rsi_daily_cross_close`.
SUBJECT_TAG_MAP = {
    "52 week high":      "sc_email_52w_high",
    "all time high":     "sc_email_ath",
    "12 week high":      "sc_email_12w_high",
    "rsi daily above":   "sc_email_rsi_daily_above",
    "rsi daily cross":   "sc_email_rsi_daily_cross",
    "rsi weekly above":  "sc_email_rsi_weekly_above",
    "rsi weekly cross":  "sc_email_rsi_weekly_cross",
    "rsi monthly above": "sc_email_rsi_monthly_above",
    "rsi monthly cross": "sc_email_rsi_monthly_cross",
}


def subject_to_tag(subject: str) -> str | None:
    """Map an SC alert subject to `sc_email_<scan>_<open|close>`.

    Returns None when the subject doesn't match the Broad alert pattern.
    Unknown scan names fall back to a slugified stem plus the trigger
    suffix, and emit a WARN so the explicit mapping can be added.
    """
    # Saved scan "01 - Broad Scan RSI > 67": tickers currently at RSI >= 67
    # (or weekly RSI >= 67). SC caps alert email results; full universe comes
    # from the Playwright CSV download. Distinct from the category cross alerts.
    if WORKBENCH_RE.search(subject):
        return "sc_email_workbench_open"

    m = SUBJECT_RE.search(subject)
    if not m:
        return None
    name = m.group(1).strip().lower()
    trigger = m.group(2).strip().lower()  # "open" or "close"
    if name in SUBJECT_TAG_MAP:
        stem = SUBJECT_TAG_MAP[name]
    else:
        slug = re.sub(r"[^a-z0-9]+", "_", name).strip("_")
        stem = f"sc_email_{slug}"
        print(f"[sc_ingest] WARN: unknown Broad scan {name!r} -> {stem} (add to SUBJECT_TAG_MAP)")
    return f"{stem}_{trigger}"


def _load_env() -> dict[str, str]:
    out: dict[str, str] = {}
    if ENV_PATH.exists():
        for line in ENV_PATH.read_text().splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            k, v = line.split("=", 1)
            out[k.strip()] = v.strip().strip('"').strip("'")
    # env vars override file
    for key in ("GMAIL_USER", "GMAIL_APP_PASSWORD", "SC_SCAN_PREFIX"):
        if key in os.environ:
            out[key] = os.environ[key]
    return out


def _get_body(msg: email.message.Message) -> str:
    if msg.is_multipart():
        for part in msg.walk():
            if part.get_content_type() == "text/html":
                payload = part.get_payload(decode=True) or b""
                return payload.decode(part.get_content_charset() or "utf-8", errors="replace")
        for part in msg.walk():
            if part.get_content_type() == "text/plain":
                payload = part.get_payload(decode=True) or b""
                return payload.decode(part.get_content_charset() or "utf-8", errors="replace")
    else:
        payload = msg.get_payload(decode=True) or b""
        return payload.decode(msg.get_content_charset() or "utf-8", errors="replace")
    return ""


def _extract_tickers(body: str) -> list[str]:
    seen: set[str] = set()
    tickers: list[str] = []
    for m in SYM_RE.finditer(body):
        sym = m.group(1)
        if sym not in seen:
            seen.add(sym)
            tickers.append(sym)
    return tickers


def get_email_signals(mark_seen: bool = True) -> dict[str, list[str]]:
    """Return {ticker: [scan_type_tag, ...]} from today's Broad alert emails.

    Each ticker maps to the list of scan tags whose email it appeared in,
    e.g. {"AAPL": ["sc_email_rsi_daily_above", "sc_email_52w_high"]}. Tags
    are stable strings ready to write to scans.scan_type.
    """
    env = _load_env()
    user = env.get("GMAIL_USER")
    password = env.get("GMAIL_APP_PASSWORD")
    prefix = env.get("SC_SCAN_PREFIX", "Broad")

    if not user or not password:
        raise RuntimeError("[sc_ingest] GMAIL_USER / GMAIL_APP_PASSWORD missing in .env")

    imap = imaplib.IMAP4_SSL(IMAP_HOST, IMAP_PORT)
    imap.login(user, password)
    imap.select("INBOX")

    status, data = imap.search(None, "UNSEEN", f'FROM "{SENDER}"')
    if status != "OK":
        raise RuntimeError(f"[sc_ingest] IMAP search failed: {status}")

    msg_ids = data[0].split()
    if not msg_ids:
        print(f"[sc_ingest] No unread emails from {SENDER}")
        imap.logout()
        return {}

    ticker_tags: dict[str, list[str]] = {}
    per_tag_count: dict[str, int] = {}
    matched = 0

    for msg_id in msg_ids:
        status, msg_data = imap.fetch(msg_id, "(RFC822)")
        if status != "OK":
            continue
        raw = msg_data[0][1]
        msg = email.message_from_bytes(raw)

        subject = msg.get("Subject", "")
        if prefix not in subject:
            continue

        tag = subject_to_tag(subject)
        if tag is None:
            # Subject contained "Broad" but not the expected pattern; skip
            continue

        matched += 1
        body = _get_body(msg)
        tickers = _extract_tickers(body)
        per_tag_count[tag] = len(tickers)
        for sym in tickers:
            existing = ticker_tags.setdefault(sym, [])
            if tag not in existing:
                existing.append(tag)

        if mark_seen:
            imap.store(msg_id, "+FLAGS", "\\Seen")

    imap.logout()
    print(f"[sc_ingest] {matched} alert emails -> {len(ticker_tags)} unique tickers")
    for tag, n in sorted(per_tag_count.items()):
        print(f"  {tag}: {n}")
    return ticker_tags


def get_tickers(mark_seen: bool = True) -> list[str]:
    """Back-compat: return just the deduped sorted ticker list."""
    return sorted(get_email_signals(mark_seen=mark_seen).keys())


if __name__ == "__main__":
    ticker_tags = get_email_signals(mark_seen=False)
    for sym in sorted(ticker_tags):
        print(f"{sym}\t{','.join(ticker_tags[sym])}")
