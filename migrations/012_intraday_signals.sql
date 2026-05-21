CREATE TABLE IF NOT EXISTS intraday_signals (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    ts           TEXT NOT NULL,
    ticker       TEXT NOT NULL,
    source       TEXT NOT NULL DEFAULT 'prospect',
    price        REAL,
    tape         TEXT,
    ema_4e_pct   REAL,
    rsi_d        REAL,
    rsi_w        REAL,
    toby_status  TEXT
);

CREATE INDEX IF NOT EXISTS idx_intraday_ticker_ts ON intraday_signals (ticker, ts);
