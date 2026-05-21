# Locker Room Criteria

Tickers promoted from prospects that are ready to trade -- technical signals aligned, thesis intact, entry being planned.

Only the `prospect` bucket graduates to Locker Room. `buy_and_hold` prospects stay in the prospects table and route to SC `[0102]` -- they are position-sizing decisions, not Locker Room candidates.

## What earns a Locker Room spot

**Weekly RSI > 70** -- primary signal. Fresh cross above 70 on the weekly. Track weeks in the run:
- Week 1-2: early, more room ahead
- Week 3-4+: maturing -- biggest moves often happen here but end is closer

**Daily RSI > 70 (when weekly is constructive)** -- daily + weekly both above 70 is the strongest combination. Weekly alone qualifies; both is ideal.

**Price high** -- 52w, ATH, or 12w high alongside RSI confirms price is being bought. Highs + RSI = high conviction.

**Thematic or sector tailwind** -- story, sector rotation, fundamental catalyst. Not required but separates good from great.

## What blocks promotion

**Stale prospect** -- 14+ days in prospects with no RSI-D or RSI-W tag in notes_technical. Signal has faded. Drop it or wait for re-entry.

**No signal in notes_technical** -- notes_technical empty or SCTR only with no RSI tag. Watch zone, not ready.

## Promotion flow

```bash
python -m scanner.cli promote-session   # review all prospects, p=promote d=drop
python -m scanner.cli promote TICKER    # single ticker direct
```

On promotion, the following are fetched and written to the locker_room row automatically:
- Prospect notes (thesis) copied over
- EMA4 status (above/below/reclaim) from yfinance
- sector, industry, market_cap, beta, short_pct_float from yfinance

No extra step needed. If the thesis evolves after promotion, update it with `python -m locker.cli note TICKER "new thesis"` -- logs the old note to history.

## After promotion -- Locker Room tracking

```bash
python -m locker.cli show               # live EMA4 for all active names
buy TICKER                              # log position entry (auto-sizes + wash sale check)
python -m locker.cli close TICKER       # log exit
python -m locker.cli stale [N]          # names 14d+ with no reclaim (default 14 = 2 trading weeks)
python -m locker.cli history TICKER     # full event log
```

EMA4 refreshes automatically on the VPS at 12 PM (live price) and 1:25 PM (close price) M-F. `locker show` always fetches live regardless.

## Position entry

When the 4 EMA reclaim triggers on a watching name, run `buy TICKER`:

```bash
buy NVDA
```

Auto-sizes from regime + equity, runs wash sale check, prompts for stop and confirm. Creates a row in the `positions` table. The locker_room row stays -- positions and locker_room are separate. `locker show` surfaces it under **In Position**.

Manual override (all numbers known):
```bash
python -m locker.cli open TICKER SHARES ENTRY STOP --broker alpaca
```

## Position exit

```bash
python -m locker.cli close TICKER --price X --reason "4 EMA close below day 2"
python -m scanner.cli exit TICKER   # remove from locker_room when fully done
```

## Removal (no trade taken)

```bash
python -m scanner.cli exit TICKER   # marks locker_room status=removed, timestamps exit
```

Nothing is deleted. The ticker drops off `locker show` but remains in the DB for history.
