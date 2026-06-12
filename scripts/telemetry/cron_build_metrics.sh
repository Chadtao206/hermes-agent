#!/bin/bash
set -euo pipefail

python3 "$HOME/.hermes/scripts/telemetry/normalize_review_block_events.py" >/dev/null
# --days 3: recompute today plus the two prior days. The digest runs this at
# 07:30, before the current day's tasks close; without the trailing window
# each day's bench/profile rows freeze at their morning snapshot and routing
# accuracy/coverage permanently read 0 (the Jun 3-11 "regression" artifact).
python3 "$HOME/.hermes/scripts/telemetry/build_daily_metrics.py" --days 3 >/dev/null
