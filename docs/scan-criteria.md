# Scan Criteria

## Universe

Momentum stocks, long-only. We are looking for stocks that are being bought aggressively -- price making new highs, relative strength in overbought territory. Overbought is a feature, not a warning.

Short-side (new lows, RSI < 30) is deferred until a long-short fund structure is in place.

### Filters (StockCharts)

- US-listed common stock (not ETF, not OTC)
- Daily volume (20-day SMA) above 300,000
- Latest close at least $0.20
- Daily RSI(14) >= 67 OR weekly RSI(14) >= 67

```
[type = stock]
and [country = us]
and [daily sma(1,daily volume) > 300000]
and [daily close >= .2]
and [group is not ETF]
and [exchange is not OTCMKT]
and [ [daily rsi(14) >= 67]
    or [weekly rsi(14) >= 67] ]
```

### Delivery paths

| Path | Loader | Trigger | scan_type prefix |
|---|---|---|---|
| SC Workbench saved scan (Playwright) | `sc_fetch_workbench.py` + `sc_import.py` | 6:35 AM PT cron | `sc_workbench` |
| SC saved scan alert email -- "Technical Alert \| 01 - Broad Scan RSI > 67" | `sc_ingest.py` → `run.py` | 6:35 AM PT cron (~6:32 AM arrival) | `sc_email_workbench_open` |
| SC alert email -- At Open (alerts 50-58) | `sc_ingest.py` → `run.py` | 6:35 AM PT cron | `sc_email_*_open` |
| SC alert email -- At Close (alerts 59-67) | `sc_ingest.py` → `run_close.py` | 1:05 PM PT cron (post-close chain) | `sc_email_*_close` |

> **sc_workbench vs sc_email_workbench_open:** Both cover "RSI >= 67 or weekly RSI >= 67." The Playwright CSV has the full universe (~519/day). The email alert is capped by SC (~72/day in testing). The Playwright step is the fallback — if it fails, `sc_email_workbench_open` provides partial coverage. If both run, they complement each other.

Open emails = provisional (price can reverse by close). Close emails = authoritative daily-bar signal. Both persist in `scans` with distinct `scan_type` suffixes so they can be queried independently.

## Signals

From the filtered universe, `check_signals` determines which conditions fired:

| # | Condition | Type |
|---|-----------|------|
| 1 | New 52-week high | Price high |
| 2 | New all-time high | Price high |
| 3 | New 12-week high | Price high |
| 4 | Daily RSI(14) > 70 | RSI -- above |
| 5 | Daily RSI(14) crossed above 70 | RSI -- cross |
| 6 | Weekly RSI(14) > 70 | RSI -- above |
| 7 | Weekly RSI(14) crossed above 70 | RSI -- cross |
| 8 | Monthly RSI(14) > 70 | RSI -- above |
| 9 | Monthly RSI(14) crossed above 70 | RSI -- cross |

A ticker can fire on multiple conditions in the same run. The cross conditions (5, 7, 9) and new all-time high (2) are the highest-signal events.

## Scan cadence

**Daily** -- run every market day:
- Which tickers are in each scan (currently above threshold)
- Which tickers are new entries today -- crossed above 70 RSI / made a new high today that they did not make yesterday

**Weekly** (end of week, Friday close):
- Which tickers are in each scan this week
- Which tickers entered each scan this week
- Weekly RSI crossover: below 70 last Friday, above 70 this Friday

New entries are the highest-signal moment. A stock that has been above 70 RSI for three weeks is less interesting than one that just crossed today.
