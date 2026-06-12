#!/bin/bash
set -euo pipefail

export HOME=/Users/ctao
export HERMES_HOME=/Users/ctao/.hermes

cd /Users/ctao/.hermes/hermes-agent
/Users/ctao/.hermes/hermes-agent/venv/bin/python "$HERMES_HOME/scripts/telemetry/export_weekly_report.py" >/dev/null
LATEST_REPORT="$(find "$HOME/.hermes/telemetry/reports" -maxdepth 1 -name 'weekly-report-*.md' | sort | tail -n 1)"
if [ -n "$LATEST_REPORT" ] && [ -f "$LATEST_REPORT" ]; then
  printf '%s\n' "$LATEST_REPORT"
fi
