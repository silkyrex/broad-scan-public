#!/usr/bin/env bash
set -uo pipefail
cd /opt/broad-scan
export BROAD_SCAN_DB=/opt/broad-scan/broad-scan.db
.venv/bin/python -m scanner.report_daily
