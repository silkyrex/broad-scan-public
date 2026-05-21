CREATE TABLE IF NOT EXISTS eod_signals (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    signal_date  TEXT NOT NULL,
    ticker       TEXT NOT NULL,
    source       TEXT NOT NULL,
    price        REAL,
    tape         TEXT,
    rsi_d        REAL,
    rsi_w        REAL,
    macd_d_state TEXT,
    macd_w_state TEXT,
    buzz_d       REAL,
    buzz_w       REAL,
    ema_4e_pct   REAL,
    toby_status  TEXT,
    UNIQUE(ticker, signal_date)
);

CREATE INDEX IF NOT EXISTS idx_eod_signals_date   ON eod_signals (signal_date);
CREATE INDEX IF NOT EXISTS idx_eod_signals_ticker ON eod_signals (ticker);
