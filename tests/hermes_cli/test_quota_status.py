import json
from pathlib import Path

from hermes_cli.quota_status import get_claude_quota_status, get_codex_quota_status


def _write_jsonl(path: Path, rows: list[dict]):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(json.dumps(row) for row in rows), encoding="utf-8")


def test_codex_quota_status_reads_weekly_and_five_hour_windows(tmp_path, monkeypatch):
    monkeypatch.delenv("CODEX_HOME", raising=False)
    session = tmp_path / ".codex" / "sessions" / "2026" / "06" / "01" / "rollout.jsonl"
    _write_jsonl(
        session,
        [
            {
                "timestamp": "2026-06-01T12:00:00Z",
                "payload": {
                    "rate_limits": {
                        "plan_type": "plus",
                        "primary": {
                            "used_percent": 12.4,
                            "window_minutes": 300,
                            "resets_at": 1780322400,
                        },
                        "secondary": {
                            "used_percent": 55,
                            "window_minutes": 10080,
                            "resets_at": 1780867200,
                        },
                    }
                },
            }
        ],
    )

    status = get_codex_quota_status(tmp_path)

    assert status["available"] is True
    assert status["windows"]["five_hour"]["label"] == "5h"
    assert status["windows"]["five_hour"]["used_percent"] == 12.4
    assert status["windows"]["weekly"]["label"] == "W"
    assert status["windows"]["weekly"]["used_percent"] == 55
    assert status["plan_type"] == "plus"
    assert status["error"] is None


def test_codex_quota_status_reports_no_telemetry(tmp_path, monkeypatch):
    monkeypatch.delenv("CODEX_HOME", raising=False)

    status = get_codex_quota_status(tmp_path)

    assert status["available"] is False
    assert status["windows"] == {}
    assert status["error"] == "no_telemetry_files_found"


def test_claude_quota_status_distinguishes_usage_logs_without_quota_windows(tmp_path, monkeypatch):
    monkeypatch.delenv("CLAUDE_HOME", raising=False)
    monkeypatch.delenv("CLAUDE_CONFIG_DIR", raising=False)
    session = tmp_path / ".claude" / "projects" / "demo" / "session.jsonl"
    _write_jsonl(
        session,
        [
            {
                "timestamp": "2026-06-01T12:00:00Z",
                "message": {
                    "model": "claude-opus-4-8",
                    "usage": {"input_tokens": 10, "output_tokens": 3},
                },
            }
        ],
    )

    status = get_claude_quota_status(tmp_path)

    assert status["available"] is False
    assert status["windows"] == {}
    assert status["error"] == "claude_code_quota_windows_not_exposed_locally"
