# Auto-Promote

The scanner runs every night post-close. When it finds a stock with two or more independent reasons to watch, and that stock is above its 4 EMA, it adds it to the locker automatically.

This replaces the manual `locker add` step. The goal: VALID zone stays full of live setups, DROP zone stays empty.

---

## The 4 Gates

A ticker must pass all four to be promoted.

### Gate 1 — Two Signals

The scanner scores every ticker that fired a `is_new_signal=1` event in the last **14 days**. Signals collapse into categories:

| Scan signal | Category |
|-------------|----------|
| ATH | HIGH (best of the group) |
| 52-week high | HIGH |
| 12-week high | HIGH |
| RSI daily > 70 | RSI-D |
| RSI weekly > 70 | RSI-W |
| RSI monthly > 70 | RSI-M |
| SCTR rank | SCTR |
| Volume buzz | VOL |

**Pass condition:** 2+ distinct categories. HIGH counts once regardless of how many high signals fired (ATH + 52wH = still just HIGH). VOL (volume buzz) can be one of the two, but there must be at least one price/momentum signal alongside it.

**Why 14 days, not 7:** RSI-M fires monthly. A 7-day window would miss RSI-M almost every time. The EMA4 check (Gate 2) handles staleness — a 14-day-old signal on a stock that's now below the EMA gets blocked at Gate 2, not here.

### Gate 2 — Above 4 EMA

Live yfinance check at run time. If the stock is below the 4 EMA, the setup is broken. Promoting a stock below the EMA means it lands in WATCH or DROP the same day — wasted locker slot.

Each ticker is checked individually. If yfinance errors on one ticker, it gets skipped and logged. Other tickers still process normally.

### Gate 3 — Not Already in Locker

If the ticker is already in `locker_room` with status `active`, skip it. No double-adds.

No cooldown period. If you ran `locker del AXTI` yesterday because it dropped below the EMA, and today AXTI reclaims the EMA AND has 2+ fresh signals, it gets promoted again. The 4 EMA reclaim IS the re-entry gate — not a calendar.

### Gate 4 — Regime Flag (never blocks)

If SPY and QQQ are both below their 4 EMA today, promoted tickers get a `⚠ REGIME` note in their locker entry. The Discord summary also shows the warning.

This never blocks a promotion. A strong setup during a soft market is still worth watching — you just size smaller or wait for the regime to recover.

---

## What Happens on Promotion

1. Row inserted into `locker_room` with `source='auto'`
2. Event logged to `locker_history` with `event='auto_promote'`
3. Captured to KB with signal categories and EMA distance
4. Discord summary posted to #signals (see below)

The promoted ticker appears in the **VALID** zone the next time you run `locker`.

---

## Discord Summary

Posts to #signals every night, even if 0 tickers were promoted. If this message stops appearing, the pipeline is down.

```
AUTO-PROMOTE 2026-05-20
Gate 1: 8 candidates
Gate 2: 5 above EMA4
Gate 3: 3 not in locker
Promoted: 3
  SNAL     ATH+RSI-D+vol_buzz        +2.3% above EMA4
  CBRS     RSI-W+RSI-M               +1.1% above EMA4
  RVI      RSI-D+RSI-W               +0.8% above EMA4
⚠ REGIME: SPY+QQQ below EMA4 — size with caution
```

If the pipeline fails hard, a 🔴 error message fires to #signals instead.

---

## Dry Run

Test against any past scan date without writing to the DB:

```bash
python3 -m scanner.auto_promote --dry-run --date 2026-05-15
```

Output shows every Gate 1 candidate, its signals, EMA4 status, and whether it would have been promoted.

---

## Reading the Locker After Auto-Promote

Auto-promoted tickers show up in **VALID** (green hold) if they're above the EMA4. They follow the same rules as manually added tickers from that point:

- Stay in **VALID** while above 4 EMA → watch for entry trigger
- Move to **WATCH** if they drop below EMA4 for 1 day (exit-d1)
- Move to **DROP** after 2+ days below EMA4 (exit-d2) → auto-dropped post-close by `auto_exit.py`

The `source='auto'` field in the DB distinguishes auto-promoted entries from manual ones, but the locker display treats them identically.
