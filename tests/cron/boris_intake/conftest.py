from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

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
    for name in ("terminal_guard.py", "write_review_body.py", "ghx_ro.sh"):
        path = assets / name
        path.write_text("#!/bin/sh\nexit 0\n")
        path.chmod(0o755)

    token_path = tmp_path / "token"
    monkeypatch.setattr(selftest, "GUARD", assets / "terminal_guard.py")
    monkeypatch.setattr(selftest, "WRITER", assets / "write_review_body.py")
    monkeypatch.setattr(selftest, "GHX_RO", assets / "ghx_ro.sh")

    calls = []

    def refusing_runner(argv, **kwargs):
        calls.append((argv, kwargs))
        return subprocess.CompletedProcess(argv, returncode=3)

    monkeypatch.setattr(selftest.subprocess, "run", refusing_runner)
    return token_path, calls
