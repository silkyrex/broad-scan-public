import os
import sqlite3
from pathlib import Path

DB_PATH = Path(os.environ.get("BROAD_SCAN_DB", Path(__file__).parent.parent / "broad-scan.db"))

SCHEMA = """
CREATE TABLE IF NOT EXISTS scans (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    ticker       TEXT    NOT NULL,
    scan_type    TEXT    NOT NULL,
    scan_date    DATE    NOT NULL,
    value        REAL,
    is_new_signal BOOLEAN DEFAULT 0,
    week_ending  DATE
);

CREATE TABLE IF NOT EXISTS prospects (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    ticker          TEXT    NOT NULL,
    added_date      DATE    NOT NULL,
    source_scan     TEXT,
    notes           TEXT,
    notes_technical TEXT,
    status          TEXT    DEFAULT 'active',
    dropped_date    DATE,
    bucket          TEXT    NOT NULL DEFAULT 'prospect',
    sector          TEXT,
    industry        TEXT,
    market_cap      INTEGER,
    beta            REAL,
    short_pct_float REAL,
    add_price       REAL
);

CREATE TABLE IF NOT EXISTS locker_room (
    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
    ticker              TEXT    NOT NULL,
    promoted_date       DATE    NOT NULL,
    source_prospect_id  INTEGER REFERENCES prospects(id),
    status              TEXT    DEFAULT 'active',
    removed_date        DATE,
    ema4_status         TEXT,
    ema4_updated        DATE,
    sector              TEXT,
    industry            TEXT,
    market_cap          INTEGER,
    beta                REAL,
    short_pct_float     REAL,
    notes               TEXT,
    added               TEXT,
    source              TEXT    NOT NULL DEFAULT 'manual',
    theme               TEXT    NOT NULL DEFAULT '',
    product             TEXT    NOT NULL DEFAULT '',
    scan_signals        TEXT    NOT NULL DEFAULT '[]',
    notes_technical     TEXT    NOT NULL DEFAULT '',
    bucket              TEXT    NOT NULL DEFAULT 'prospect',
    source_scan         TEXT    NOT NULL DEFAULT '',
    technical_updated   TEXT,
    price               REAL,
    w52_high            REAL,
    w52_high_pct        REAL,
    ath                 REAL,
    ath_pct             REAL,
    rsi_d               REAL,
    rsi_w               REAL,
    rsi_m               REAL,
    sma_10              REAL,
    sma_20              REAL,
    sma_50              REAL,
    sma_100             REAL,
    sma_200             REAL
);

CREATE TABLE IF NOT EXISTS locker_history (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    ticker     TEXT    NOT NULL,
    event      TEXT    NOT NULL,
    date       DATE    NOT NULL,
    reason     TEXT,
    detail     TEXT
);

CREATE TABLE IF NOT EXISTS positions (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    ticker         TEXT    NOT NULL,
    locker_room_id INTEGER REFERENCES locker_room(id),
    broker         TEXT,
    entry_date     DATE    NOT NULL,
    entry_price    REAL    NOT NULL,
    shares         REAL    NOT NULL,
    stop_price     REAL,
    exit_date      DATE,
    exit_price     REAL,
    exit_reason    TEXT,
    status         TEXT    NOT NULL DEFAULT 'open',
    notes          TEXT,
    size_type      TEXT    DEFAULT 'full',
    setup          TEXT,
    r_risk         REAL,
    mfe            REAL,
    mae            REAL
);

CREATE TABLE IF NOT EXISTS schema_migrations (
    name       TEXT NOT NULL PRIMARY KEY,
    applied_at TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE UNIQUE INDEX IF NOT EXISTS idx_scans_unique
  ON scans (ticker, scan_type, scan_date);
CREATE INDEX IF NOT EXISTS idx_locker_history_ticker ON locker_history (ticker);
CREATE INDEX IF NOT EXISTS idx_positions_ticker ON positions (ticker);
CREATE INDEX IF NOT EXISTS idx_positions_status ON positions (status);
"""


def init_db():
    import sys; sys.path.insert(0, str(Path(__file__).parent.parent))
    from db.migrate import run_migrations
    fresh = not DB_PATH.exists()
    conn = sqlite3.connect(DB_PATH)
    conn.executescript(SCHEMA)
    conn.commit()
    if fresh:
        # fresh install: schema already correct, seed migrations as pre-applied
        run_migrations(conn=conn, seed_only=True)
        print(f"broad-scan.db created at {DB_PATH.resolve()}")
    else:
        # existing DB: run any pending migrations
        run_migrations(conn=conn)
        print(f"broad-scan.db schema verified at {DB_PATH.resolve()}")
    conn.close()


if __name__ == "__main__":
    init_db()
