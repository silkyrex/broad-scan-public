# Entry Rubric

How to enter and exit a position from the Locker Room.

## Entry signal -- 4 EMA cross (daily chart)

Price crosses from below the 4 EMA to above it on the daily chart.

- Yesterday: price closed **below** the 4 EMA
- Today: price is **above** the 4 EMA (intraday or close)
- = Valid entry signal (`locker show` will display ★ RECLAIM)

Best entries start working immediately. A trade that chops right after entry is a lower-quality setup.

**Two valid entry windows:**
1. **Day of the cross** -- enter same day
2. **Day after the cross** -- next session also valid

## Logging the entry

**Primary: `buy TICKER`**

```bash
buy NVDA
```

- Fetches regime (BULL/CHOP/BEAR) and reads equity from `config/sizing.json`
- Auto-calculates shares: `equity × regime_pct / current_price`
- Runs 30-day wash sale check — prompts to confirm if a prior loss exists
- Prompts for stop price, then a final confirm line showing shares, entry, stop, R-risk
- On confirm: inserts into `positions`, logs to `locker_history`, captures to KB, pushes DB to VPS

Overrides for when you already know the numbers:
```bash
buy NVDA --entry 131.00 --shares 80 --stop 127.50
```

**Manual override: `locker open`**

Use when skipping the auto-sizing flow entirely:
```bash
python -m locker.cli open TICKER SHARES ENTRY STOP [--size full|pilot] [--setup 4ema_reclaim|...] [--broker alpaca]
```

Both commands require the ticker to be in active locker_room first. Creates a row in the `positions` table. Auto-calculates `r_risk = shares × (entry - stop)`. Shows in `locker show` under **In Position**.

## Exit signal -- 4 EMA close below

**Day 1 (close below 4 EMA):** Discretionary. You may sell or hold.

**Day 2 -- two outcomes:**
- Price **cannot reclaim** 4 EMA → exit required. No more discretion.
- Price **reclaims** 4 EMA → sell is saved. Potential add-on opportunity.

Stay in the trade as long as price holds above the 4 EMA.

## Logging the exit

```bash
python -m locker.cli close TICKER --price X --reason "4 EMA close below day 2"
python -m scanner.cli exit TICKER   # removes from Locker Room entirely
```

`locker close` closes the position row, fetches MFE/MAE from yfinance, prints P&L + excursion stats, logs to locker_history. `scanner exit` removes the ticker from locker_room -- run both when fully done with a name.

## Pass / skip

If a Locker Room ticker hits the entry signal but context is wrong (regime, news, discretion), skip for that day. Signal resets -- wait for the next 4 EMA cross.

## Reading EMA4 status in locker show

| Status | Meaning |
|--------|---------|
| ★ RECLAIM | Yesterday below EMA4, today above -- entry signal active |
| ↑ above | Price above EMA4 -- holding, no new cross |
| ↓ below | Price below EMA4 -- watch for exit trigger |
| ⚠ (age flag) | In locker 14+ calendar days (2 trading weeks) with no reclaim -- fading, consider removing |
