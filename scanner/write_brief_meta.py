"""
write_brief_meta.py

Called by Hermes after each midday brief. Reads /tmp/brief_meta.json and
writes the brief's ticker picks to hermes_briefs in broad-scan.db.
Creates tables on first call if they don't exist yet.

Usage:
    python3 write_brief_meta.py /tmp/brief_meta.json
"""
import json
import sqlite3
import sys
from pathlib import Path

DB = "/opt/broad-scan/broad-scan.db"

CREATE_SQL = """
CREATE TABLE IF NOT EXISTS hermes_briefs (
    id               INTEGER PRIMARY KEY AUTOINCREMENT,
    brief_date       TEXT NOT NULL UNIQUE,
    best_bet         TEXT,
    prospect_tickers TEXT,
    buzz_tickers     TEXT,
    created_at       TEXT DEFAULT CURRENT_TIMESTAMP
);
CREATE TABLE IF NOT EXISTS hermes_outcomes (
    id               INTEGER PRIMARY KEY AUTOINCREMENT,
    brief_date       TEXT NOT NULL,
    ticker           TEXT NOT NULL,
    category         TEXT NOT NULL,
    price_at_brief   REAL,
    price_5d_later   REAL,
    pct_change_5d    REAL,
    entered_locker   INTEGER DEFAULT 0,
    checked_at       TEXT DEFAULT CURRENT_TIMESTAMP,
    UNIQUE(brief_date, ticker, category)
);
"""


def main():
    if len(sys.argv) < 2:
        print("usage: write_brief_meta.py <path-to-brief_meta.json>", file=sys.stderr)
        sys.exit(1)

    meta = json.loads(Path(sys.argv[1]).read_text())
    brief_date = meta["brief_date"]

    con = sqlite3.connect(DB)
    con.executescript(CREATE_SQL)
    con.execute(
        """INSERT OR REPLACE INTO hermes_briefs
               (brief_date, best_bet, prospect_tickers, buzz_tickers)
           VALUES (?, ?, ?, ?)""",
        (
            brief_date,
            meta.get("best_bet", ""),
            json.dumps(meta.get("prospect_tickers", [])),
            json.dumps(meta.get("buzz_tickers", [])),
        ),
    )
    con.commit()
    con.close()
    print(f"hermes_briefs: wrote {brief_date}")


if __name__ == "__main__":
    main()
