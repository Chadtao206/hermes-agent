import json
from pathlib import Path

from tools.claude_session.pretrust import ensure_trusted


def test_adds_trust_without_clobbering_other_keys(tmp_path):
    cj = tmp_path / ".claude.json"
    cj.write_text(json.dumps({
        "projects": {"/other": {"hasTrustDialogAccepted": True, "mcp": {"x": 1}}},
        "topLevelSetting": 42,
    }))
    changed = ensure_trusted("/work/dir", claude_json=cj)
    data = json.loads(cj.read_text())
    assert changed is True
    assert data["topLevelSetting"] == 42
    assert data["projects"]["/other"] == {"hasTrustDialogAccepted": True, "mcp": {"x": 1}}
    abs_wd = str(Path("/work/dir").resolve())
    assert data["projects"][abs_wd]["hasTrustDialogAccepted"] is True


def test_idempotent_when_already_trusted(tmp_path):
    cj = tmp_path / ".claude.json"
    abs_wd = str(Path("/w").resolve())
    cj.write_text(json.dumps({"projects": {abs_wd: {"hasTrustDialogAccepted": True}}}))
    assert ensure_trusted("/w", claude_json=cj) is False


def test_creates_file_when_missing(tmp_path):
    cj = tmp_path / "sub" / ".claude.json"
    assert ensure_trusted("/w", claude_json=cj) is True
    assert json.loads(cj.read_text())["projects"]
