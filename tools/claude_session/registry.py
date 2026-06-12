from __future__ import annotations

import fcntl
import json
import os
import tempfile
from contextlib import contextmanager
from pathlib import Path
from typing import Callable, Dict, List, Optional

from .models import SessionRecord


class Registry:
    """JSON registry of live tmux Claude sessions. reserve() is atomic across
    processes (fcntl.flock) so the pool cap holds under concurrent `run`s."""

    def __init__(self, state_dir: Path):
        self.state_dir = Path(state_dir)
        self.state_dir.mkdir(parents=True, exist_ok=True)
        self.path = self.state_dir / "claude_sessions.json"
        self.lock = self.state_dir / "claude_sessions.lock"

    @contextmanager
    def _locked(self):
        with open(self.lock, "w") as fh:
            fcntl.flock(fh, fcntl.LOCK_EX)
            try:
                yield
            finally:
                fcntl.flock(fh, fcntl.LOCK_UN)

    def _load(self) -> Dict[str, dict]:
        if not self.path.exists():
            return {}
        try:
            return json.loads(self.path.read_text("utf-8"))
        except (json.JSONDecodeError, OSError):
            return {}

    def _save(self, data: Dict[str, dict]) -> None:
        fd, tmp = tempfile.mkstemp(dir=self.state_dir, suffix=".tmp")
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(data, fh)
        os.replace(tmp, self.path)

    def add(self, rec: SessionRecord) -> None:
        with self._locked():
            data = self._load()
            data[rec.name] = rec.to_dict()
            self._save(data)

    def reserve(self, rec: SessionRecord, *, cap: int) -> bool:
        with self._locked():
            data = self._load()
            if len(data) >= cap:
                return False
            data[rec.name] = rec.to_dict()
            self._save(data)
            return True

    def get(self, name: str) -> Optional[SessionRecord]:
        d = self._load().get(name)
        return SessionRecord.from_dict(d) if d else None

    def list(self) -> List[SessionRecord]:
        return [SessionRecord.from_dict(d) for d in self._load().values()]

    def remove(self, name: str) -> None:
        with self._locked():
            data = self._load()
            if name in data:
                del data[name]
                self._save(data)

    def active_count(self) -> int:
        return len(self._load())

    def reap(self, *, now: float, pane_dead: Callable[[str], bool],
             kill: Callable[[str], None]) -> List[str]:
        reaped: List[str] = []
        for rec in self.list():
            if pane_dead(rec.name) or now >= rec.deadline:
                kill(rec.name)
                self.remove(rec.name)
                reaped.append(rec.name)
        return reaped
