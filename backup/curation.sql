CREATE TABLE locker_room (
    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
    ticker              TEXT    NOT NULL,
    promoted_date       DATE    NOT NULL,
    source_prospect_id  INTEGER REFERENCES prospects(id),
    status              TEXT    DEFAULT 'active',
    removed_date        DATE
);
CREATE TABLE prospects (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    ticker          TEXT    NOT NULL,
    added_date      DATE    NOT NULL,
    source_scan     TEXT,
    notes           TEXT,
    status          TEXT    DEFAULT 'active',
    dropped_date    DATE,
    bucket          TEXT    NOT NULL DEFAULT 'prospect'
, notes_technical TEXT);
INSERT INTO "prospects" VALUES(1,'BW','2026-05-15','sc_workbench','Nuclear power plant modernization and emissions control technology demand surging','active',NULL,'prospect','SCTR 99.5 | RSI-W');
INSERT INTO "sqlite_sequence" VALUES('prospects',1);
