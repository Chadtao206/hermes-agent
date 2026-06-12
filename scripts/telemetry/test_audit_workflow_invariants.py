#!/usr/bin/env python3
from __future__ import annotations

import importlib.util
import json
import os
import sqlite3
import stat
import sys
import tempfile
from pathlib import Path

THIS_DIR = Path(__file__).resolve().parent


def _load_module():
    spec = importlib.util.spec_from_file_location(
        "audit_workflow_invariants_under_test",
        THIS_DIR / "audit_workflow_invariants.py",
    )
    if spec is None or spec.loader is None:
        raise RuntimeError("Unable to import audit_workflow_invariants.py")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


audit_mod = _load_module()


PROFILES = ("default", "engineer", "researcher", "reviewer", "ops", "designer")


def _write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def _profile_config(profile: str) -> str:
    disabled = ["baoyu-article-illustrator", "kanban-codex-lane"]
    if profile == "engineer":
        disabled = ["baoyu-article-illustrator"]
    elif profile == "designer":
        disabled = ["kanban-codex-lane"]
    return f"""
terminal:
  cwd: "."
mcp_servers:
  atlassian:
    tools:
      include:
        - search
      resources: false
      prompts: false
skills:
  disabled:
{os.linesep.join(f"    - {skill}" for skill in disabled)}
"""


def _seed_home(root: Path, jobs: list[dict]) -> None:
    launcher = root / "scripts" / "jensen-neutral"
    _write(launcher, "#!/bin/sh\nexec hermes \"$@\"\n")
    launcher.chmod(launcher.stat().st_mode | stat.S_IXUSR)

    _write(root / "config.yaml", _profile_config("default"))
    for profile in PROFILES:
        if profile == "default":
            continue
        _write(root / "profiles" / profile / "config.yaml", _profile_config(profile))

    _write(root / "cron" / "jobs.json", json.dumps({"jobs": jobs}, indent=2))

    db = root / "telemetry" / "events.db"
    db.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(db)
    try:
        conn.execute("CREATE TABLE profile_metrics_daily(date TEXT, profile TEXT)")
        conn.executemany(
            "INSERT INTO profile_metrics_daily(date, profile) VALUES (?, ?)",
            [("2026-06-12", profile) for profile in PROFILES],
        )
        conn.commit()
    finally:
        conn.close()


def _consolidated_jobs() -> list[dict]:
    return [
        {"name": "daily-ops-digest", "enabled": True},
        {"name": "weekly-ops-digest", "enabled": True},
    ]


def case_consolidated_jobs_do_not_require_retired_jobs() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        _seed_home(root, _consolidated_jobs())
        payload = audit_mod.audit(root)
        assert payload["status"] == "pass", payload
        names = {check["name"] for check in payload["checks"]}
        assert "cron_consolidated_enabled_daily-ops-digest" in names, names
        assert "cron_consolidated_enabled_weekly-ops-digest" in names, names
        assert not any(name.startswith("cron_retained_enabled_") for name in names), names
        assert all(
            check["pass"]
            for check in payload["checks"]
            if check["name"].startswith("cron_superseded_removed_or_disabled_")
        ), payload


def case_enabled_superseded_job_fails() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        jobs = _consolidated_jobs() + [{"name": "daily-routing-workflow-audit", "enabled": True}]
        _seed_home(root, jobs)
        payload = audit_mod.audit(root)
        failures = {check["name"] for check in payload["checks"] if not check["pass"]}
        assert "cron_superseded_removed_or_disabled_daily-routing-workflow-audit" in failures, payload


def main() -> int:
    case_consolidated_jobs_do_not_require_retired_jobs()
    case_enabled_superseded_job_fails()
    print("OK")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
