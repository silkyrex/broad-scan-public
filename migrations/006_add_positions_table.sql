CREATE TABLE IF NOT EXISTS positions (
    id                INTEGER PRIMARY KEY AUTOINCREMENT,
    ticker            TEXT    NOT NULL,
    locker_room_id    INTEGER REFERENCES locker_room(id),
    broker            TEXT,
    entry_date        DATE    NOT NULL,
    entry_price       REAL    NOT NULL,
    shares            REAL    NOT NULL,
    stop_price        REAL,
    exit_date         DATE,
    exit_price        REAL,
    exit_reason       TEXT,
    status            TEXT    NOT NULL DEFAULT 'open',
    notes             TEXT
);

CREATE INDEX IF NOT EXISTS idx_positions_ticker ON positions (ticker);
CREATE INDEX IF NOT EXISTS idx_positions_status ON positions (status);
