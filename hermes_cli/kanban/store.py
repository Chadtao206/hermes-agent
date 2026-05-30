from __future__ import annotations
from typing import Optional

_VALID_BACKENDS = {"sqlite", "postgres"}


def resolve_backend() -> str:
    """Return the configured kanban backend ('sqlite' default). Reads config
    defensively; any failure falls back to 'sqlite' so default deployments and
    upstream are unaffected."""
    try:
        from hermes_cli.config import load_config
        cfg = load_config()
        kanban_cfg = (cfg.get("kanban") or {}) if isinstance(cfg, dict) else {}
        backend = str(kanban_cfg.get("backend") or "sqlite").strip().lower()
    except Exception:
        return "sqlite"
    return backend if backend in _VALID_BACKENDS else "sqlite"
