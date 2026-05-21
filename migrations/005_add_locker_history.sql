CREATE TABLE IF NOT EXISTS locker_history (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    ticker     TEXT    NOT NULL,
    event      TEXT    NOT NULL,
    date       DATE    NOT NULL,
    reason     TEXT,
    detail     TEXT
);

CREATE INDEX IF NOT EXISTS idx_locker_history_ticker ON locker_history (ticker);
