"""
Paper Trading Agent -- DB-only simulation, no broker connection.

Candidate universe: broad-scan.db scans table (RSI>=67 workbench + new highs).

Entry gates (all 3 must pass):
  1. SCTR >= 75         (from sc_workbench)
  2. 52w high or ATH    (from scans table today)
  3. Fresh 4 EMA reclaim -- prior close below 4 EMA, today close above it
     (do-infra trigger_agent logic via locker_actionables.ema)

Exit rules:
  - Close below 4 EMA daily  -> 4ema_break
  - Close <= stop_price       -> hard_stop  (-8% from entry, matches do-infra STOP_PERCENT)

Kill switch: `python -m paper_agent.cli kill` closes all open positions immediately.

Positions tracked in broad-scan.db with broker='paper-agent'.
"""

import json
import sqlite3
import urllib.request
from datetime import date

import pandas as pd
import yfinance as yf
from locker_actionables.ema import check_ema4_actionable

BROKER = "paper-agent"
POSITION_SIZE_USD = 500.0
MAX_POSITIONS = 10
SCTR_GATE = 75.0
HARD_STOP_PCT = 0.08  # matches do-infra STOP_PERCENT


def _conn(db_path: str) -> sqlite3.Connection:
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    return conn


def _close_series(ticker: str, period: str = "6mo") -> pd.Series | None:
    df = yf.download(ticker, period=period, auto_adjust=True, progress=False, multi_level_index=False)
    if df.empty or len(df) < 30:
        return None
    return df["Close"].dropna()


def _ema_scalar(close: pd.Series, period: int) -> float:
    return float(close.ewm(span=period, adjust=False).mean().iloc[-1])


def _gate3_reclaim(ticker: str) -> tuple[bool, float, float]:
    """
    Gate 3: fresh 4 EMA reclaim (do-infra trigger_agent logic).
    Previous close must be below 4 EMA; today's close must be above it.
    Returns (passes, latest_close, ema4).
    """
    close = _close_series(ticker)
    if close is None or len(close) < 5:
        return False, 0.0, 0.0
    result = check_ema4_actionable(ticker, close.tolist())
    passes = result.status == "RECLAIM"
    return passes, result.close, result.ema4


def _open_tickers(conn: sqlite3.Connection) -> set[str]:
    rows = conn.execute(
        "SELECT ticker FROM positions WHERE broker=? AND status='open'", (BROKER,)
    ).fetchall()
    return {r["ticker"] for r in rows}


def find_candidates(conn: sqlite3.Connection, scan_date: str | None = None) -> list[tuple[str, float]]:
    """Gate 1+2: SCTR >= 75 AND (ath OR 52w_high) today. Returns [(ticker, sctr)]."""
    if scan_date is None:
        scan_date = conn.execute("SELECT MAX(scan_date) FROM scans").fetchone()[0]
    rows = conn.execute(
        """
        SELECT DISTINCT w.ticker, w.value AS sctr
        FROM scans w
        JOIN scans h ON w.ticker = h.ticker AND w.scan_date = h.scan_date
        WHERE w.scan_type = 'sc_workbench'
          AND h.scan_type IN ('ath', '52w_high')
          AND w.scan_date = ?
          AND w.value >= ?
        ORDER BY w.value DESC
        """,
        (scan_date, SCTR_GATE),
    ).fetchall()
    return [(r["ticker"], r["sctr"]) for r in rows]


def enter_positions(
    conn: sqlite3.Connection,
    candidates: list[tuple[str, float]],
    today: str | None = None,
) -> list[dict]:
    if today is None:
        today = date.today().isoformat()

    open_t = _open_tickers(conn)
    open_count = len(open_t)
    entered = []

    for ticker, sctr in candidates:
        if ticker in open_t or open_count >= MAX_POSITIONS:
            continue
        passes, close, ema4 = _gate3_reclaim(ticker)
        if not passes or close == 0.0:
            continue

        shares = round(POSITION_SIZE_USD / close, 4)
        stop_price = round(close * (1 - HARD_STOP_PCT), 2)
        r_risk = round((close - stop_price) / close, 4)
        notes = json.dumps({"sctr": round(sctr, 1), "ema4": round(ema4, 2), "gate3": "reclaim"})

        conn.execute(
            """
            INSERT INTO positions
              (ticker, locker_room_id, broker, entry_date, entry_price, shares,
               stop_price, size_type, setup, r_risk, status, notes)
            VALUES (?, NULL, ?, ?, ?, ?, ?, 'pilot', '4ema-reclaim', ?, 'open', ?)
            """,
            (ticker, BROKER, today, round(close, 2), shares, stop_price, r_risk, notes),
        )
        conn.commit()

        open_t.add(ticker)
        open_count += 1
        entered.append({"ticker": ticker, "price": close, "shares": shares, "sctr": sctr})

    return entered


def check_exits(conn: sqlite3.Connection, today: str | None = None) -> list[dict]:
    if today is None:
        today = date.today().isoformat()

    rows = conn.execute(
        "SELECT id, ticker, entry_price, stop_price FROM positions WHERE broker=? AND status='open'",
        (BROKER,),
    ).fetchall()

    exited = []
    for pos in rows:
        close = _close_series(pos["ticker"], period="3mo")
        if close is None:
            continue
        latest = float(close.iloc[-1])
        ema4 = _ema_scalar(close, 4)

        reason = None
        if latest < ema4:
            reason = "4ema_break"
        elif pos["stop_price"] and latest <= pos["stop_price"]:
            reason = "hard_stop"

        if reason:
            pnl_pct = round((latest - pos["entry_price"]) / pos["entry_price"] * 100, 2)
            conn.execute(
                "UPDATE positions SET exit_date=?, exit_price=?, exit_reason=?, status='closed' WHERE id=?",
                (today, round(latest, 2), reason, pos["id"]),
            )
            conn.commit()
            exited.append({"ticker": pos["ticker"], "exit_price": latest, "exit_reason": reason, "pnl_pct": pnl_pct})

    return exited


def _post_discord(entered: list, exited: list, open_positions: list, webhook_url: str) -> None:
    lines = ["**Paper Agent -- Daily Report**"]

    if entered:
        lines.append(f"\n**Entered ({len(entered)})**")
        for e in entered:
            lines.append(f"  BUY {e['ticker']} @ ${e['price']:.2f} | SCTR {e['sctr']:.0f}")

    if exited:
        lines.append(f"\n**Exited ({len(exited)})**")
        for e in exited:
            sign = "+" if e["pnl_pct"] >= 0 else ""
            lines.append(f"  SELL {e['ticker']} @ ${e['exit_price']:.2f} | {sign}{e['pnl_pct']}% | {e['exit_reason']}")

    lines.append(f"\n**Open: {len(open_positions)}/{MAX_POSITIONS}**")
    for p in open_positions:
        lines.append(f"  {p['ticker']} (entry ${p['entry_price']:.2f})")

    if not entered and not exited:
        lines.append("\nNo changes today.")

    payload = json.dumps({"content": "\n".join(lines)}).encode()
    req = urllib.request.Request(
        webhook_url,
        data=payload,
        headers={"Content-Type": "application/json", "User-Agent": "broad-scan/1.0"},
    )
    try:
        urllib.request.urlopen(req, timeout=10)
    except Exception as exc:
        print(f"Discord post failed: {exc}")


def kill_all(db_path: str = "broad-scan.db") -> list[str]:
    """Close all open paper-agent positions immediately. Kill switch."""
    conn = _conn(db_path)
    today = date.today().isoformat()
    rows = conn.execute(
        "SELECT id, ticker, entry_price FROM positions WHERE broker=? AND status='open'", (BROKER,)
    ).fetchall()
    killed = []
    for pos in rows:
        close = _close_series(pos["ticker"], period="1mo")
        exit_price = float(close.iloc[-1]) if close is not None else pos["entry_price"]
        conn.execute(
            "UPDATE positions SET exit_date=?, exit_price=?, exit_reason='killed', status='closed' WHERE id=?",
            (today, round(exit_price, 2), pos["id"]),
        )
        killed.append(pos["ticker"])
    conn.commit()
    conn.close()
    return killed


def run(
    db_path: str = "broad-scan.db",
    discord_webhook: str | None = None,
    dry_run: bool = False,
) -> dict:
    conn = _conn(db_path)
    today = date.today().isoformat()
    print(f"Paper Agent -- {today}")

    exited = [] if dry_run else check_exits(conn, today)
    if exited:
        print(f"Exited: {[e['ticker'] for e in exited]}")

    candidates = find_candidates(conn)
    print(f"Gate 1+2 candidates ({len(candidates)}): {[c[0] for c in candidates]}")

    entered = [] if dry_run else enter_positions(conn, candidates, today)
    if entered:
        print(f"Entered: {[e['ticker'] for e in entered]}")

    open_rows = conn.execute(
        "SELECT ticker, entry_price, entry_date FROM positions WHERE broker=? AND status='open'",
        (BROKER,),
    ).fetchall()
    open_positions = [dict(r) for r in open_rows]
    print(f"Open ({len(open_positions)}): {[p['ticker'] for p in open_positions]}")

    if discord_webhook and not dry_run:
        _post_discord(entered, exited, open_positions, discord_webhook)

    conn.close()
    return {"entered": entered, "exited": exited, "open": open_positions}
