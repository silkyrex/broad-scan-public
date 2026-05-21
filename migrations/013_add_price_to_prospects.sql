-- Store the close price at the time a ticker was admitted to prospects.
-- Used as the reference price for the stale-flag drawdown check.
ALTER TABLE prospects ADD COLUMN add_price REAL;
