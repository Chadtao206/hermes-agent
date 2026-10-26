from __future__ import annotations

import os
import shutil
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

SOURCE_ROOT = Path(
    os.environ.get(
        "BORIS_INTAKE_SOURCE_ROOT",
        Path(__file__).resolve().parents[3] / "cron" / "scripts",
    )
)
sys.path.insert(0, str(SOURCE_ROOT))


@pytest.fixture
def hermetic_selftest(monkeypatch, tmp_path):
    """Redirect hard-coded deployment checks to disposable test assets."""
    from boris_intake import selftest

    assets = tmp_path / "assets"
    assets.mkdir()
    for name in ("python", "review_tools_mcp.py"):
        path = assets / name
        path.write_text("#!/bin/sh\nexit 0\n")
        path.chmod(0o755)
    shutil.copy2(
        Path(__file__).with_name("fixtures") / "ghx_ro.sh",
        assets / "ghx_ro.sh",
    )

    token_path = tmp_path / "token"
    db_path = tmp_path / "intake.db"
    registry_path = tmp_path / "gate_registry.json"
    registry_path.write_text(
        '{"version":1,"security_checks":["CodeQL"],'
        '"ordinary_checks":["test"]}'
    )
    profile_path = tmp_path / "profile.yaml"
    profile_path.write_text(
        "toolsets:\n"
        "  - mcp-boris_review\n"
        "mcp_servers:\n"
        "  boris_review:\n"
        f"    command: {assets / 'python'}\n"
        "    args:\n"
        f"      - {assets / 'review_tools_mcp.py'}\n"
        "    env:\n"
        "      HERMES_PROFILE: boris_on_demand\n"
        "      BORIS_KANBAN_TASK: ${HERMES_KANBAN_TASK}\n"
        "      BORIS_KANBAN_RUN_ID: ${HERMES_KANBAN_RUN_ID}\n"
        "    enabled: true\n"
        "    tools:\n"
        "      include:\n"
        "        - read_github_evidence\n"
        "        - read_acceptance_evidence\n"
        "        - submit_review_result\n"
    )

    monkeypatch.setattr(selftest, "GHX_RO", assets / "ghx_ro.sh")
    monkeypatch.setattr(selftest, "DB_PATH", db_path)
    monkeypatch.setattr(selftest, "GATE_REGISTRY", registry_path)
    monkeypatch.setattr(selftest, "PROFILE_CONFIG", profile_path)

    calls = []
    original_run = subprocess.run

    def recording_runner(argv, **kwargs):
        calls.append((argv, kwargs))
        return original_run(argv, **kwargs)

    monkeypatch.setattr(selftest.subprocess, "run", recording_runner)
    return SimpleNamespace(
        token_path=token_path,
        calls=calls,
        db_path=db_path,
        registry_path=registry_path,
        profile_path=profile_path,
    )
