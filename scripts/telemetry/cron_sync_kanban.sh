#!/bin/bash
set -euo pipefail

python3 "$HOME/.hermes/scripts/telemetry/sync_kanban_to_telemetry.py" >/dev/null
