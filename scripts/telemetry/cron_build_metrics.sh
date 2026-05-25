#!/bin/bash
set -euo pipefail

python3 "$HOME/.hermes/scripts/telemetry/build_daily_metrics.py" >/dev/null
