# broad-scan Pipeline

## System map

```
INPUT                          PIPELINE                              OUTPUT
─────                          ────────                              ──────

SC Workbench (Playwright) ──► sc_fetch_workbench.py ──┐
  RSI >= 67, ~530 tickers       imports CSV            │
                                                       │
SC alert emails (open)    ──► sc_ingest.py            ├──► scans ──► post_discord.py      ──► Discord #broad-scan
  9 alerts, At Open            run.py                 │   (TABLE)    post_discord_close.py ──► Discord #broad-scan
  fires 9:30 AM ET               │                   │              report_daily.py        ──► Discord #broad-scan
                                  ▼                   │              report_weekly.py       ──► Discord #broad-scan + PDF
SC alert emails (close)   ──► sc_ingest.py         signals.py
  9 alerts, At Close           run_close.py            │    6 checks per ticker (yfinance 5yr history):
  fires 4:00 PM ET                                     │    52w_high  ATH  12w_high
                                                        │    rsi_daily  rsi_weekly  rsi_monthly
                                                        └──► fires if above threshold; is_new_signal=1 if crossed today

                                                       prospects (TABLE)  ──► sc_api.py ──► SC [0000] / [0102]
operator (manual)          ──► cli.py ──────────────►   locker_room (TABLE)
```

## Plain-English summary

- **Workbench scan** = wide net (~530 names). Playwright fetches the "01 - Broad Scan RSI > 67" saved scan daily, downloads CSV, imports via `sc_import.py`. Writes `sc_workbench` rows with SCTR.
- **Email scan (open)** = 9 SC alerts firing At Open (alerts 50-58). `sc_ingest.py` parses Gmail, `run.py` runs yfinance + signals, writes `sc_email_*_open` and signal rows. Runs at **6:35 AM PT**.
- **Email scan (close)** = same 9 alert types, At Close (alerts 59-67). `run_close.py` ingests close emails at **1:15 PM PT**, writes `sc_email_*_close` rows. Separate Discord post via `post_discord_close.py`.
- **`scans` table** is raw signal history. `scan_today.py` is the daily rank report on top of it.
- **`prospects` table** is the human-curated subset, split by `bucket`:
  - `prospect` → momentum/breakout candidates, routes to SC [0000].
  - `buy_and_hold` → SMA reclaim theses, routes to SC [0102].
- **`prospect-sc-sync`** pushes prospects to the matching SC list via `sc_api.py`.
- **`locker_room`** is the next stage after prospect (`cli.py promote`).
- A ticker appearing in both workbench and email runs on the same day = high-signal candidate for prospects.

## Mac + VPS view

```
 YOUR VPS (auto, 6:35 AM PT M-F)       YOUR MAC
 ──────────────────────────────────────      ──────────────────────────────────────

 Gmail IMAP                                  [6:45 AM auto] scan-pull
 SC alert emails (~99 tickers/day)           scp VPS DB → ~/broad-scan/
         │                                            │
         ▼                                            │
   sc_ingest.py                                       │
   extract tickers                                    │
         │                                            │
         ▼                                            │
     run.py                                           │
     yfinance signals per ticker                      │
     is_new_entry = RSI crossed 70 today              │
         │                                            │
         ▼                                            │
  /opt/broad-scan/broad-scan.db ──────────────────────┘
         │
         ▼                               ~/broad-scan/broad-scan.db
  post_discord.py                        (scans table: SCTR + RSI signals)
  ranked table → Discord #scans                  │
         │                               [YOU REVIEW Discord post]
         │                                        │
         │                            ┌───────────┴───────────┐
         │                            ▼                       ▼
         │                      prospect-session        skip / ignore
         │                      [manual, interactive]
         │                            │
         │                       auto-generates:
         │                         notes (Haiku → Sonnet eval → thesis or sector fallback)
         │                         notes_technical (from scans)
         │                            │
         │                            │  bucket = prospect | buy_and_hold
         │                            ▼
         │                        prospects
         │                            │
         │         [1:15 PM auto] prospect-sc-sync + refresh-technical
         │                            │
         │               ┌────────────┴────────────┐
         │               ▼                         ▼
         │        SC [0000] or [0102]     notes_technical refreshed
         │                                from latest scans
         │                            │
         │                            ▼
         │                      promote → locker_room
         │
         └── optional: scan_today.py --charts → PDF on Desktop
```

## DB ownership

The VPS owns the full DB. The Mac is read-only except during `prospect-session`.

```
your-vps VPS                         Mac (local)
──────────────────────────────        ──────────────────────
broad-scan.db (source of truth)
  scans        (auto, daily)   ──►    read-only pull on login
  prospects    (curation)      ◄──    scp push after prospect-session
  locker_room  (curation)

All automated jobs run on VPS:
  refresh-technical  (1:20 PM M-F)
  prospect-sc-sync   (1:15 PM M-F)
  curation backup    (11 PM daily)
```

`backup/curation.sql` is a nightly SQL dump of prospects + locker_room committed to GitHub -- survives both Mac failure and VPS failure.

Full cron schedule: see `docs/infra.md`.

## Post-close chain (`run-post-close.sh`)

Runs at 1:05 PM PT M-F as a single cron entry. Steps run sequentially:

1. Close signal ingest + yfinance + Discord post
2. EOD report + SC weekly charts to Discord
3. EOW report (Fridays only)
4. Sync prospects to SC chartlists `[0000]` and `[0102]`
5. Refresh `notes_technical` for all active prospects
6. Refresh EMA4 (official close price) for all locker room tickers
7. Update MFE/MAE for all open positions
8. Write EOD signals (`write_eod_signals.py`) -- tape, RSI D/W, MACD state, buzz D/W, EMA4 pct, toby status for all active locker + prospect tickers
9. **Auto-prospect** (`auto_prospect.py`) -- screen sc_workbench tickers (SCTR>=90 + 2 fresh signals) into prospects with AI thesis. ~49 tickers/day.
10. **Auto-promote** (`auto_promote.py`) -- promote sc_workbench tickers with SCTR>=90 + any fresh signal directly into locker_room. Signal earns admission; 4 EMA is the entry gate inside the locker.
11. **Auto-exit** (`auto_exit.py`) -- exit-d2 drop (all active locker tickers) + exit-d1 warn (positions only). CLARITY post-close detection (exit-d1/d2 → reclaim). Discord to `#locker-actions`.

Log: `/var/log/post-close.log`

## EOD signals (`eod_signals` table)

Written by `scanner/write_eod_signals.py` after step 8 above. One row per ticker per day. Consumed by `/tradingview-opinion` cross-check and `locker refresh` cache.

**`buzz_w` alignment:** On Mon-Thu, the last yfinance weekly bar is an incomplete partial week. The script drops it and uses the previous closed bar, matching TradingView's `request.security lookahead_off` behavior. On Fridays, the current week bar is used (it just closed). This means `buzz_w` always reflects a completed trading week.

## Daily pipeline detail

### Morning (6:35 AM PT, M-F) -- run.sh

```
  StockCharts Workbench              Gmail (9 SC alert emails)
  RSI >= 67 saved scan               RSI/High/ATH alerts
         │ Playwright                       │ subject parse
         ▼                                  ▼
  sc_fetch_workbench.py          get_email_signals()
         │                                  │
         └────────────┬─────────────────────┘
                      │ yfinance recompute on email tickers
                      ▼
              scans DB (SQLite)
         ┌──────────────────────────────────────┐
         │ sc_workbench      (RSI >= 67, ~530)  │
         │ sc_email_×9       (> 70 / new-high)  │
         │ rsi_daily/weekly/monthly             │
         │ 52w_high, 12w_high, ath              │
         └──────────────────────────────────────┘
                      │
                      ▼
             post_discord.py  →  Discord #scans (morning cockpit)
```

### Close (1:05 PM PT, M-F) -- run-close.sh

```
  Gmail (9 SC close-alert emails)
  RSI cross/above + high/ATH at close
         │ subject parse
         ▼
  run_close.py  →  scans DB  (scan_type ends in _close)
         │
         ▼
  post_discord_close.py  →  Discord #scans (close alerts)
```

### EOD Report (1:10 PM PT, M-F) -- run-eod.sh

```
  scans DB (today)
         │
         ▼
  report_daily.py
    query_leaders()    -- tickers with 3+ distinct signals, sorted SCTR desc
    query_new_entries()-- is_new_entry=1 on morning signal types
    query_close_new()  -- is_new_entry=1 on *_close scan types
    query_locker()     -- active locker members that hit any signal
         │
         ├── Discord text post (conviction table + new entries + close alerts + locker)
         │
         └── SC weekly chart images (top 8 by SCTR, chartstyle 0003)
                  │ download via sc_api.py session + c-sc/sc PNG endpoint
                  ▼
             Discord image attachments (one per ticker)
```

### EOW Report (1:10 PM PT, Fridays) -- run-eow.sh

```
  scans DB (current week_ending)
         │
         ▼
  report_weekly.py
    New Entries    -- is_new_entry=1 first appearance this week
    Leaders        -- most distinct signal types hit this week
    Persistent     -- tickers appearing 3+ days this week
    Locker Cross   -- active locker members in week's scans
         │
         ├── Discord text post
         └── (PDF optional -- reportlab not installed on VPS)
```

## Buzz bridge (vol_buzz + theme_buzz)

`scanner/run_buzz.py` -- runs at 11:55 AM PT M-F, 10 min before Hermes midday brief.

**Input:** today's `sc_workbench` tickers (the ~530-name wide net).

**vol_buzz** (`scanner/volume_buzz.py`):
- Formula: `vol_buzz_pct = 100 * (today_vol / 50d_MA_vol) - 100`
- Also computes: `ud_ratio` (sum up-volume / sum down-volume over 50 bars) and HVE/HV1
- `buzz=True` when `vol_buzz_pct >= 25` (default threshold)
- Writes `vol_buzz` rows to `scans` table; `value = vol_buzz_pct`, `is_new_entry = 1` if buzzing

**theme_buzz** (`scanner/theme_buzz.py`):
- 9 ETF baskets: BOTZ, UFO, DRIV, ARKG, NLR/URNM, HACK, ITA/XAR, ICLN, ARKK
- A ticker buzzes when it appears in a top-holding of an ETF whose 1-month return > 0
- `theme_score = etf_count + (avg_momentum / 10)`; `buzz=True` when `avg_momentum > 0`
- ETF holdings cached to `~/.cache/buzz/theme_map.json` (refreshed each run)
- Writes `theme_buzz` rows to `scans` table

**High conviction:** tickers where both `vol_buzz=True` AND `theme_buzz=True`.
- Captured to KB via `capture_thought` JSON-RPC (requires `KB_MCP_URL` in VPS `.env`)
- Also surfaced in Hermes midday brief under "BUZZ WATCH" section

```
python -m scanner.run_buzz                     # today's sc_workbench tickers
python -m scanner.run_buzz --date 2026-05-15   # specific date
python -m scanner.run_buzz --dry-run           # print results, skip DB write + KB
```

## EOD report

`scanner/report_daily.py` -- runs at 1:10 PM PT M-F. Posts to **#broad-scan**.

**Signal display:** one RSI + one high per ticker. RSI priority: weekly > daily > monthly. High priority: ATH > 52w > 12w.

**Two distinct signals:**
- **New Entry** = crossed a threshold for the first time today (`is_new_entry=1`). Highest urgency -- moment of change.
- **Conviction Leader** = 3+ signals firing simultaneously. Higher confidence setup.

**Discord posting sequence (6 steps):**
1. New Entries text (ticker, signal, SCTR)
2. CandleGlance links for new entries (batched at 12/link, SC limit)
3. New entry weekly charts -- labeled `[NEW]`
4. Conviction Leaders text
5. CandleGlance links for conviction
6. Conviction-only charts (tickers not already in new entries) -- labeled `[CONV]`

**Charts:** SC chart images are publicly accessible -- no login needed. Downloads via plain `requests` with hardcoded style ID (`SC_STYLE`). Update `SC_STYLE` in `report_daily.py` if SC rotates it.

**CandleGlance note:** SC caps CandleGlance URLs at 12 tickers. Report batches automatically (e.g., 30 new entries = 3 links labeled 1/3, 2/3, 3/3). TRA-XXX tracks upgrading to a SC ChartList write for a single link.

```
python -m scanner.report_daily                 # full run (charts + Discord)
python -m scanner.report_daily --min-signals 4 # tighter conviction bar
python -m scanner.report_daily --no-charts     # text only
python -m scanner.report_daily --no-discord    # print to terminal
```

## EOW report

`scanner/report_weekly.py` -- 4 sections, sorted by SCTR descending:

1. **New Entries** -- first-time crossovers this week (`is_new_entry=1`)
2. **Conviction Leaders** -- tickers with 2+ distinct signal types this week (tunable via `--min-signals`)
3. **Persistent Names** -- tickers in scans 3+ days this week (tunable via `--min-days`)
4. **Locker Cross-Reference** -- active `locker_room` members that showed up in weekly scans

Output: Discord post to **#broad-scan** + PDF saved to `~/Desktop/eow-report-{week}.pdf`.

**DB is always live.** Running from laptop auto-pulls `/opt/broad-scan/broad-scan.db` from your-vps via scp before querying -- no manual sync needed. `--no-sync` skips the pull (used by the cron since it runs on your-vps directly).

```
python -m scanner.report_weekly               # pull from VPS, full output
python -m scanner.report_weekly --week 2026-05-16
python -m scanner.report_weekly --min-signals 3 --min-days 4
python -m scanner.report_weekly --no-discord  # PDF only (omit --no-pdf)
python -m scanner.report_weekly --no-pdf      # Discord only (omit --no-discord)
python -m scanner.report_weekly --no-sync     # skip DB pull (on VPS / offline)
```

## StockCharts chartlist routing

| SC ChartList | listNum | Source rule |
|---|---|---|
| `! [0000] Prospects` | 25 | Active prospects with `bucket = prospect` -- momentum / breakout watch |
| `! [0102] Manual SMA200` | 26 | Active prospects with `bucket = buy_and_hold` -- long-term thesis |
| `! [0101] operator Athletes` | -- | Hand-picked names (AAPL/NVDA/etc.) -- not in prospects flow |
| `! [0108] Locker room CLI` | -- | Synced by `locker-sync` from `locker_room.json` |

The `prospects` table is the local decision log. SC chartlists are the visualization surface. `prospect-sc-sync` is the bridge; `bucket` routes the push.

## Two-scanner logic

| Scanner | Threshold | Job |
|---------|-----------|-----|
| StockCharts (`sc_import.py`) | RSI >= 67 | Wide universe -- getting ahead of momentum, adds SCTR |
| yfinance (`scanner/run.py`) | RSI crosses 70 | Confirmed signal -- momentum triggered today |

67 = watch zone. 70 cross = trigger. Names in both = evaluate for prospects or locker.

### What `is_new_entry` means

- **sc_workbench:** ticker was NOT in the previous SC scan -- just entered the RSI >= 67 zone
- **yfinance signals:** RSI crossed above 70 today (was below yesterday)

## SC alert numbering

| # | Name | Trigger |
|---|---|---|
| 50-58 | Broad (At Open) | 9:30 AM ET daily |
| 59-67 | Broad (At Close) | 4:00 PM ET daily |
| 01 | Broad Scan RSI > 67 | Workbench saved scan (Playwright) |

## Locker automation (Steps 9–11, post-close)

```
Workbench (519 tickers, SCTR in DB)
        │
        │  SCTR >= 90 + fresh signal (is_new_signal=1, any of 6 types)
        │  Signal earns locker admission. 4 EMA is the ENTRY gate, not admission.
        ▼
LOCKER ROOM  (strong RS + just had a notable event)
        │
        ├── INTRADAY: 4 EMA reclaim on a locker ticker?
        │   → ✨ CLARITY Discord ping (prior close below, live now above)
        │   → `locker clarity` on-demand or 12 PM PT cron via refresh --live
        │
        ├── exit-d1 (position only) → ⚠️ warn
        └── exit-d2 (all active locker tickers) → 🔴 auto-drop + recycle to prospects
```

**CLARITY trigger:** `_check_and_fire_clarity(conn, ticker, today, live_status)` in `locker/cli.py`
- Fires when: `live_status in ('above','reclaim')` AND most recent prior EOD was `exit-d1` or `exit-d2`
- Once per ticker per day (dedup via `locker_history`)
- Discord to `DISCORD_LOCKER_WEBHOOK` (`#locker-actions`)

**locker show display:**
```
─── OPEN POSITIONS ──  (always top)
─── ✨ CLARITY ───────  (exit-d1/d2 prior close → live above 4 EMA)
─── VALID ────────────  (above 4 EMA; sorted by EMA proximity asc)
─── WATCH ────────────  (below 4 EMA, no position; no action)
─── DROP ─────────────  (exit-d2; auto-dropped post-close)
```

## What stays manual

| Step | Why |
|------|-----|
| `prospect-session` | Human judgment on specific names; auto-prospect fills the queue |
| `scan_today.py --charts` | Optional -- Discord post covers the ranked list |
| CLARITY response | operator decides hold/exit after ping; `locker clarity` provides context |

## Gaps

- No `update-prospect` command -- to change `source_scan`, `notes`, or `notes_technical` on an existing prospect, update the DB directly.
- `refresh-technical` reads last 7 days of scans -- signals older than 7 days drop off notes_technical automatically.
