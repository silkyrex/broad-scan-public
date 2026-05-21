# broad-scan Infrastructure (your-vps)

## Deploy chain

```
LOCAL MAC
──────────────────────────────────────────────────────
~/projects/core/                ~/broad-scan/
       │                                │
       │ git push                       │ git push
       ▼                                ▼
GitHub: your-org/core          GitHub: your-org/broad-scan
(private)                      (private)
       │                                │
       │ deploy key (core)              │ deploy key (broad-scan)
       ▼                                ▼
your-vps  your-vps.example.com
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
/opt/core/                     /opt/broad-scan/
  provision/                     scanner/
  workloads/                       run.py
    autodeploy/                    run_close.py
      auto_deploy.sh  ←──┐         report_daily.py
      watchdog.sh         │         report_weekly.py
  data/                   │         signals.py
    scans.csv (SOT)       │         post_discord.py
                          │         post_discord_close.py
                          │         sc_api.py
                          │         sc_fetch_workbench.py
                          │       run.sh
                          │       run-close.sh
                          │       run-eod.sh
                          │       run-eow.sh
                          │       .venv/
                          │       .env
                          │
  crontab (source of truth in your-org/core)
    every 5m   → auto_deploy.sh           (pulls core + broad-scan)
    every 30m  → watchdog.sh              (stale-deploy alert)
    6:35 AM PT → run.sh                   (morning scan + Discord)  M-F
    1:05 PM PT → run-post-close.sh        (full post-close chain)  M-F
    1:10 PM PT → run-eod.sh              (daily conviction report + charts)  M-F
    1:10 PM PT → run-eow.sh              (Fridays only -- weekly summary + Discord)
```

## Two-repo separation

| Repo | Layer | Owns |
|---|---|---|
| `your-org/core` | Infra | Provisioning, auto-deploy, watchdog, crontab, verify, .env mgmt |
| `your-org/broad-scan` | Workload | Scan logic, SQLite DB, self-contained venv + .env |

**Key rule:** `your-org/core` owns the crontab that schedules all runs. Broad-scan owns its code and DB but does not control when it's called -- core does.

## VPS facts

- **IP:** your-vps.example.com (reserved, sfo3 -- survives destroys)
- **Size:** s-2vcpu-4gb ($24/mo)
- **venv:** provisioned by `core/provision/apply.sh`, not managed by broad-scan
- **Watchdog:** alerts Discord if auto-deploy timestamp stale for 30+ min

## Cron schedule

Everything automated runs on your-vps (your-vps.example.com). The Mac is read-only except when you run `prospect-session`.

| Time (PT) | Job | What it does |
|-----------|-----|--------------|
| every 5 min | `auto_deploy.sh` | Pull core + broad-scan, apply on HEAD change |
| every 30 min | `watchdog.sh` | Alert if auto-deploy hasn't run in 30 min |
| 6:35 AM M-F | `run.sh` | SC Workbench fetch + Gmail ingest + yfinance signals + Discord post |
| 11:55 AM M-F | `buzz-bridge/run.sh` | vol_buzz + theme_buzz on sc_workbench tickers |
| 12:00 PM M-F | `locker refresh --live` | EMA4 vs live price for all locker room tickers (intraday read) |
| 1:05 PM M-F | `run-post-close.sh` | Full post-close chain (see below) |
| 11:00 PM daily | `backup/dump_curation.sh` | Dump prospects + locker_room → backup/curation.sql → GitHub |
| 11:35 PM daily | `kb-broad-scan-ingest/ingest.py` | Push new prospect + locker events to KB |
| 11:15 PM Sun | `kb-self-improve/improve.py` | KB trading-sector self-improvement |

All jobs run via `cron_run.sh` (lockfile + logging). Crontab source of truth: `your-org/core/provision/crontab.day1`. Never edit the live crontab directly.

## Post-close chain (`run-post-close.sh`)

Single cron at 1:05 PM PT M-F. Steps run sequentially:

1. Close signal ingest + yfinance + Discord post (`run-close.sh`)
2. EOD report + SC weekly charts to Discord (`run-eod.sh`)
3. EOW report on Fridays (`run-eow.sh`)
4. Sync prospects to SC chartlists `[0000]` and `[0102]`
5. Refresh `notes_technical` for all active prospects
6. Refresh EMA4 (official close price) for all locker room tickers
7. Update MFE/MAE for all open positions

Log: `/var/log/post-close.log`

## On-box paths

```
/opt/broad-scan/               -- git clone of your-org/broad-scan
/opt/broad-scan/.venv/         -- Python 3.12 venv (provisioned by core's apply.sh)
/opt/broad-scan/broad-scan.db  -- SQLite DB (source of truth for all tables)
/var/log/broad-scan.log        -- morning open run
/var/log/post-close.log        -- full post-close chain
/var/log/locker-refresh.log    -- midday locker EMA4 live
/var/log/curation-backup.log   -- nightly curation backup
/var/log/kb-broad-scan-ingest.log -- KB ingest
```

## Env vars (in `/opt/core/.env` on VPS)

| Key | Purpose | Local source |
|-----|---------|-------------|
| `DISCORD_SCANS_WEBHOOK` | Post daily hit list to `#scans` | `~/projects/core/.env` |
| `DISCORD_PIPELINE_WEBHOOK` | Pipeline health alerts (✅/⚠️/❌) to `#pipeline-health` | `~/projects/core/.env` |
| `GMAIL_USER` | IMAP login for SC alert emails | `~/broad-scan/.env` |
| `GMAIL_APP_PASSWORD` | Gmail app password | `~/broad-scan/.env` |
| `SC_SCAN_PREFIX` | Email subject filter (default: `Broad`) | `~/broad-scan/.env` |

> **Gmail creds gotcha:** `GMAIL_USER`, `GMAIL_APP_PASSWORD`, and `SC_SCAN_PREFIX` live in `~/broad-scan/.env` locally (not in `~/projects/core/.env`). When updating the VPS `.env`, pull from both files:
> ```bash
> grep -E '^(GMAIL|SC_SCAN)' ~/broad-scan/.env >> ~/projects/core/.env
> scp ~/projects/core/.env root@your-vps:/opt/core/.env
> ```
| `ANTHROPIC_API_KEY` | Haiku (thesis generation) + Sonnet (thesis eval) in prospect auto-notes |
| `SUPABASE_URL` | KB ingest |
| `SUPABASE_SERVICE_ROLE_KEY` | KB ingest |
| `OPENROUTER_API_KEY` | KB embeddings |

DB path set via `BROAD_SCAN_DB=/opt/broad-scan/broad-scan.db`.

## Deploy keys

Two SSH keys in `/root/.ssh/`:

| Key file | Access | Used by |
|----------|--------|---------|
| `broad_scan_github` | Read-only | auto-deploy (pull code) |
| `broad-scan-backup` | Write | `backup/dump_curation.sh` (push backup commits) |

SSH config aliases: `github-broad-scan` (read) and `github-broad-scan-backup` (write).

## Schema migrations

Schema changes go in `migrations/` as numbered `.sql` files. They run automatically on every deploy via `db/init.py`.

1. Write SQL in `migrations/NNN_description.sql`
2. Push to GitHub
3. VPS picks it up within 5 minutes and applies automatically

Applied migrations tracked in `schema_migrations` table.

## Chart style

EOD charts use SC chartstyle `your-chartstyle-id` (operator's saved chartstyle 0003, weekly `p=W yr=2`).
Style ID is read live from the active SC session at runtime; `SC_CHART_STYLE` env var overrides if SC rotates it.

## How code changes reach the VPS

`auto_deploy.sh` runs every 5 minutes. If `your-org/broad-scan` HEAD moved, `provision/apply.sh` pulls code, syncs venv, runs `db/init.py`.

Push to broad-scan → VPS updated within 5 minutes. No SSH needed.

## Checking health

**First check:** Discord `#pipeline-health` — morning run posts ✅/⚠️/❌ at ~6:35 AM PT with ticker count. If the channel is silent after 6:40 AM, check the log below.

```bash
# Post-close chain last run
ssh root@your-vps.example.com 'tail -20 /var/log/post-close.log'

# Morning scan last run (email ingest + signals)
ssh root@your-vps.example.com 'tail -20 /var/log/broad-scan.log'

# DB row counts
ssh root@your-vps.example.com 'python3 -c "
import sqlite3; c = sqlite3.connect(\"/opt/broad-scan/broad-scan.db\")
print(\"scans:\", c.execute(\"SELECT COUNT(*), MAX(scan_date) FROM scans\").fetchone())
print(\"prospects:\", c.execute(\"SELECT COUNT(*) FROM prospects WHERE status=\\\"active\\\"\").fetchone()[0])
print(\"locker:\", c.execute(\"SELECT COUNT(*) FROM locker_room WHERE status=\\\"active\\\"\").fetchone()[0])
print(\"positions:\", c.execute(\"SELECT COUNT(*) FROM positions WHERE status=\\\"open\\\"\").fetchone()[0])
"'

# Applied migrations
ssh root@your-vps.example.com 'python3 -c "
import sqlite3; c = sqlite3.connect(\"/opt/broad-scan/broad-scan.db\")
[print(r) for r in c.execute(\"SELECT name, applied_at FROM schema_migrations ORDER BY name\")]
"'
```

## Playwright prerequisites (one-time)

```bash
ssh root@your-vps.example.com
cd /opt/broad-scan
.venv/bin/playwright install --with-deps chromium
scp ~/.config/credentials/stockcharts.env root@your-vps.example.com:/root/.config/credentials/
```
