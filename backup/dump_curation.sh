#!/usr/bin/env bash
# Nightly curation backup: dump prospects + locker_room to SQL text and commit.
set -euo pipefail

REPO=$HOME/broad-scan
DB=$REPO/broad-scan.db
OUT=$REPO/backup/curation.sql

cd "$REPO"

sqlite3 "$DB" ".dump prospects" > "$OUT"
sqlite3 "$DB" ".dump locker_room" >> "$OUT"

git add backup/curation.sql

if git diff --cached --quiet; then
    echo "$(date): no curation changes, skipping commit"
    exit 0
fi

git commit -m "backup: nightly curation snapshot $(date +%Y-%m-%d)"
git push origin main
echo "$(date): curation backup committed and pushed"
