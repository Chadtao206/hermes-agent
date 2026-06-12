#!/bin/bash
set -euo pipefail

export HOME=/Users/ctao
export HERMES_HOME=/Users/ctao/.hermes

cd /Users/ctao/.hermes/hermes-agent
/Users/ctao/.hermes/hermes-agent/venv/bin/python "$HERMES_HOME/scripts/telemetry/sync_kanban_to_telemetry.py" >/dev/null
