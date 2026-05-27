#!/bin/bash
set -euo pipefail

python3 "$HOME/.hermes/scripts/telemetry/normalize_review_block_events.py" >/dev/null
python3 "$HOME/.hermes/scripts/telemetry/build_daily_metrics.py" >/dev/null
