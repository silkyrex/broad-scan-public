-- Add technicals + metadata columns to locker_room
-- Allows locker_room to serve as the single canonical locker DB
-- replacing the separate locker-actionables/data/locker.db

ALTER TABLE locker_room ADD COLUMN added TEXT;
ALTER TABLE locker_room ADD COLUMN source TEXT NOT NULL DEFAULT 'manual';
ALTER TABLE locker_room ADD COLUMN theme TEXT NOT NULL DEFAULT '';
ALTER TABLE locker_room ADD COLUMN product TEXT NOT NULL DEFAULT '';
ALTER TABLE locker_room ADD COLUMN scan_signals TEXT NOT NULL DEFAULT '[]';
ALTER TABLE locker_room ADD COLUMN notes_technical TEXT NOT NULL DEFAULT '';
ALTER TABLE locker_room ADD COLUMN bucket TEXT NOT NULL DEFAULT 'prospect';
ALTER TABLE locker_room ADD COLUMN source_scan TEXT NOT NULL DEFAULT '';
ALTER TABLE locker_room ADD COLUMN tech_updated TEXT;
ALTER TABLE locker_room ADD COLUMN price REAL;
ALTER TABLE locker_room ADD COLUMN w52_high REAL;
ALTER TABLE locker_room ADD COLUMN w52_high_pct REAL;
ALTER TABLE locker_room ADD COLUMN ath REAL;
ALTER TABLE locker_room ADD COLUMN ath_pct REAL;
ALTER TABLE locker_room ADD COLUMN rsi_d REAL;
ALTER TABLE locker_room ADD COLUMN rsi_w REAL;
ALTER TABLE locker_room ADD COLUMN rsi_m REAL;
ALTER TABLE locker_room ADD COLUMN sma_10 REAL;
ALTER TABLE locker_room ADD COLUMN sma_20 REAL;
ALTER TABLE locker_room ADD COLUMN sma_50 REAL;
ALTER TABLE locker_room ADD COLUMN sma_100 REAL;
ALTER TABLE locker_room ADD COLUMN sma_200 REAL;

-- Back-fill added = promoted_date for existing rows
UPDATE locker_room SET added = promoted_date WHERE added IS NULL;
