# Prospects

Tickers worth watching closely -- not yet ready to trade, but on the radar.

## Two buckets

The `prospects` table has a `bucket` column. Default `prospect` is the momentum / breakout watch -- the rubric below scopes to it. `buy_and_hold` is for long-term thesis adds (e.g. 200w SMA reclaim) and follows position-sizing thinking, not RSI momentum; criteria are TBD. Chartlist routing lives in `docs/pipeline.md`.

## What qualifies as a prospect

A prospect comes from one of two places:

**Scan-sourced:** A ticker appeared in a scan and passed a judgment filter. Not every scan hit becomes a prospect. A biotech making a 52w high you don't want to follow stays in the scan output and goes nowhere. A prospect is a scan hit you actually want to research further.

**Outside sources:**
- News or macro event (e.g., saw a name in the news, want to watch)
- Post-earnings (stock just reported, want to monitor the follow-through)
- Technical chart interest (setup looks interesting)
- Tip or referral

No scan hit required for outside-source prospects.

## How to add

```bash
python -m scanner.cli prospect-session          # interactive -- pulls fresh DB, shows scan list, auto-generates notes
python -m scanner.cli add-prospect TICKER --notes "thesis"  # batch add, no auto-notes
```

On add, `notes` and `notes_technical` (SCTR, RSI tags from scans) are populated automatically. Sector and enrichment fields (market_cap, beta, short_pct_float) also fetched from yfinance.

`notes` generation uses a two-model pipeline: Haiku writes a 12-word catalyst thesis; Sonnet evaluates it for specificity (must name HOW or WHY, not a generic trend like "strong growth"). If Sonnet rejects the thesis, `notes` falls back to the sector name. Eval errors default to pass so a Sonnet outage never blocks a prospect add.

## How to review

Go deeper than the scan. Check:
- Is RSI still constructive on daily and weekly?
- Is price making or holding highs?
- Is there a thematic or sector tailwind behind it?
- Is the original thesis still valid?

Prospects that keep checking out on review get promoted. Ones that stall or lose their story get dropped.

## Promotion criteria (prospects → Locker Room)

A prospect is ready for the Locker Room when technical and thematic picture aligns:

**Weekly RSI > 70** -- primary signal. A fresh cross above 70 on the weekly typically leads to 1-6 more weeks of upside. Knowing where you are in that window matters: week 1 is early, weeks 3-4 approach climactic territory where the biggest moves can happen.

**Daily RSI > 70 (when weekly is constructive)** -- confirms the weekly. Both daily and weekly above 70 is a high-conviction setup.

**A price high** -- 52w, ATH, or 12w high alongside RSI momentum adds conviction.

**Thematic or sector tailwind** -- story, buzz, fundamental catalyst, or sector momentum that explains why other participants are paying attention too. Strengthens the case.

Ideal candidate: weekly RSI > 70 + daily RSI > 70 + making highs + sector tailwind.

## Promotion

```bash
python -m scanner.cli promote-session   # review all prospects, p=promote d=drop
python -m scanner.cli promote TICKER    # single ticker
```

On promotion, EMA4 status and yfinance enrichment are fetched automatically and written to the `locker_room` row. Notes carry over from the prospect.

## Removal criteria (when to drop a prospect)

**Stale signal:** `notes_technical` loses all RSI tags (refresh-technical runs daily -- faded signals drop off within 7 days). If 14+ days old with no RSI-D or RSI-W tag, the momentum has faded. Drop it or wait for re-entry.

**No signal at add time:** SCTR only with no RSI tag = watch zone only. If it hasn't triggered RSI after a few weeks, drop.

**Thesis broken:** sector rotated out, catalyst played out, story no longer valid.

```bash
python -m scanner.cli drop-session      # stale-only cleanup view
python -m scanner.cli drop-prospect TICKER  # single drop
python -m scanner.cli list-prospects    # full list with age and stale flags
```
