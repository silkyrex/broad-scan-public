#!/usr/bin/env bash
# Post-market-close chain (1:05 PM PT = 4:05 PM ET, M-F).
# Replaces individual cron entries for close ingest, EOD, SC sync,
# refresh-technical, locker EMA4, and position MFE/MAE.
set -euo pipefail

cd /opt/broad-scan
export BROAD_SCAN_DB=/opt/broad-scan/broad-scan.db
# Discord webhooks loaded from /opt/core/.env via cron_run.sh -- do not hardcode here

log() { echo "[post-close $(date -Iseconds)] $*"; }

log "starting"

# 1. close signal ingest + yfinance + Discord post
log "step 1: close ingest"
bash /opt/core/workloads/broad-scan/run-close.sh

# 2. EOD report + SC weekly charts compiled into PDF -> single Discord post (non-fatal)
log "step 2: EOD report"
bash /opt/broad-scan/run-eod.sh || log "step 2 failed (non-fatal) -- continuing"

# 3. EOW report on Fridays
DOW=$(date +%u)
if [ "$DOW" -eq 5 ]; then
    log "step 3: EOW report (Friday)"
    bash /opt/broad-scan/run-eow.sh
fi

# 4. sync prospects to SC chartlists [0000] and [0102]
log "step 4: SC chartlist sync"
.venv/bin/python -m scanner.cli prospect-sc-sync --clear
.venv/bin/python -m scanner.cli prospect-sc-sync --bucket buy_and_hold --clear

# 5. refresh notes_technical for active prospects
log "step 5: refresh-technical"
.venv/bin/python -m scanner.cli refresh-technical

# 6. refresh EMA4 (official close price) for locker room
log "step 6: locker EMA4 refresh (close)"
.venv/bin/python -m locker.cli refresh

# 7. update MFE/MAE for open positions
log "step 7: position MFE/MAE refresh"
.venv/bin/python -m locker.cli refresh-positions

# 8. write EOD signals for /tradingview-opinion cross-check + locker refresh cache
log "step 8: EOD signals"
.venv/bin/python -m scanner.write_eod_signals

# 9. auto-prospect: screen sc_workbench (SCTR>=90 + 2 signals) -> prospects
log "step 9: auto-prospect"
.venv/bin/python -m scanner.auto_prospect || log "step 9 failed (non-fatal) -- continuing"

# 10. auto-promote: prospects with fresh 4 EMA reclaim -> locker
log "step 10: auto-promote"
.venv/bin/python -m scanner.auto_promote || log "step 10 failed (non-fatal) -- continuing"

# 11. auto-exit: exit-d2 drop + exit-d1 warn + CLARITY flag
log "step 11: auto-exit"
.venv/bin/python -m scanner.auto_exit || log "step 11 failed (non-fatal) -- continuing"

log "done"
