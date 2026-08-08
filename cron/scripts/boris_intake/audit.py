"""Append-only JSONL decision log.

Spec R6: because Slack-invoked lanes share a credential with the existing crons,
GitHub's audit log cannot distinguish them. This file is the only provenance
record, so every decision — accept and reject alike — must land here.
"""
from __future__ import annotations

import json
import time
from pathlib import Path

DEFAULT_PATH = Path("/Users/ctao/.hermes/state/boris_intake_audit.jsonl")


def write(path: Path, record: dict) -> None:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    entry = {"at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()), **record}
    with path.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(entry, ensure_ascii=False) + "\n")
