#!/bin/bash
set -euo pipefail

python3 "$HOME/.hermes/scripts/telemetry/export_weekly_report.py" >/dev/null
LATEST_REPORT="$(find "$HOME/.hermes/telemetry/reports" -maxdepth 1 -name 'weekly-report-*.md' | sort | tail -n 1)"
if [ -n "$LATEST_REPORT" ] && [ -f "$LATEST_REPORT" ]; then
  printf '%s\n' "$LATEST_REPORT"
fi
