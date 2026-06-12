from __future__ import annotations

import fcntl
import json
import os
import tempfile
from contextlib import contextmanager
from pathlib import Path
from typing import Optional


@contextmanager
def _flock(lock_path: str):
    with open(lock_path, "w", encoding="utf-8") as fh:
        fcntl.flock(fh, fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(fh, fcntl.LOCK_UN)


def _atomic_write(path: Path, data: dict) -> None:
    fd, tmp = tempfile.mkstemp(dir=str(path.parent), suffix=".tmp")
    with os.fdopen(fd, "w", encoding="utf-8") as fh:
        json.dump(data, fh, indent=2)
    os.replace(tmp, path)


def ensure_trusted(workdir: str, *, claude_json: Optional[Path] = None) -> bool:
    """Additively set hasTrustDialogAccepted for `workdir` in ~/.claude.json.

    Merges (never overwrites); atomic write; locked so we don't race a live
    Claude Code writer. Returns True if a write happened, False if already set.
    """
    path = Path(claude_json) if claude_json else Path.home() / ".claude.json"
    abs_wd = str(Path(workdir).resolve())
    path.parent.mkdir(parents=True, exist_ok=True)
    with _flock(f"{path}.hermes-lock"):
        data = {}
        if path.exists():
            try:
                data = json.loads(path.read_text("utf-8"))
            except (json.JSONDecodeError, OSError):
                data = {}
        proj = data.setdefault("projects", {}).setdefault(abs_wd, {})
        if proj.get("hasTrustDialogAccepted") is True:
            return False
        proj["hasTrustDialogAccepted"] = True
        _atomic_write(path, data)
        return True
