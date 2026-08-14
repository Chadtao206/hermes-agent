import json
import sys
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from boris_intake import github
from boris_intake.github import fetch_pr


def fake_runner(payload, returncode=0):
    def run(cmd, **kw):
        return SimpleNamespace(returncode=returncode, stdout=json.dumps(payload), stderr="")
    return run


def test_parses_open_pr():
    info = fetch_pr("Ylopo", "repo", 1, runner=fake_runner(
        {"headRefOid": "a" * 40, "author": {"login": "zjenkins"}, "state": "OPEN"}))
    assert info.head_sha == "a" * 40
    assert info.author == "zjenkins"
    assert info.state == "OPEN"


def test_returns_none_on_nonzero_exit():
    assert fetch_pr("Ylopo", "repo", 1, runner=fake_runner({}, returncode=1)) is None


def test_returns_none_on_garbage_output():
    def run(cmd, **kw):
        return SimpleNamespace(returncode=0, stdout="not json", stderr="")
    assert fetch_pr("Ylopo", "repo", 1, runner=run) is None


def test_returns_none_on_short_sha():
    assert fetch_pr("Ylopo", "repo", 1, runner=fake_runner(
        {"headRefOid": "abc", "author": {"login": "x"}, "state": "OPEN"})) is None


def test_fetch_gate_snapshot_pins_head_and_marks_required_checks():
    calls = []

    def run(cmd, **kwargs):
        calls.append(cmd)
        if "view" in cmd:
            payload = {"headRefOid": "a" * 40}
        elif "--required" in cmd:
            payload = [{"name": "unit-tests", "state": "FAILURE", "link": "https://ci/unit"}]
        else:
            payload = [
                {"name": "unit-tests", "state": "FAILURE", "link": "https://ci/unit"},
                {"name": "optional-lint", "state": "SUCCESS", "link": "https://ci/lint"},
            ]
        return SimpleNamespace(returncode=0, stdout=json.dumps(payload), stderr="")

    snapshot = github.fetch_gate_snapshot(
        "Ylopo", "repo", 1, "a" * 40, "2026-08-14T12:00:00Z", runner=run
    )

    assert snapshot is not None
    required = {check.check_name: check.required for check in snapshot.checks}
    assert required == {"optional-lint": False, "unit-tests": True}
    assert len(calls) == 3


def test_fetch_gate_snapshot_fails_closed_when_head_changed():
    snapshot = github.fetch_gate_snapshot(
        "Ylopo",
        "repo",
        1,
        "a" * 40,
        "2026-08-14T12:00:00Z",
        runner=fake_runner({"headRefOid": "b" * 40}),
    )

    assert snapshot is None


def test_fetch_gate_snapshot_accepts_check_failure_exit_with_valid_json():
    def run(cmd, **kwargs):
        if "view" in cmd:
            return SimpleNamespace(
                returncode=0,
                stdout=json.dumps({"headRefOid": "a" * 40}),
                stderr="",
            )
        return SimpleNamespace(returncode=1, stdout="[]", stderr="checks failed")

    assert github.fetch_gate_snapshot(
        "Ylopo", "repo", 1, "a" * 40, "2026-08-14T12:00:00Z", runner=run
    ) is not None


def test_fetch_gate_snapshot_fails_closed_when_required_check_is_missing_from_rollup():
    def run(cmd, **kwargs):
        if "view" in cmd:
            payload = {"headRefOid": "a" * 40}
        elif "--required" in cmd:
            payload = [{"name": "unit-tests", "state": "FAILURE", "link": "https://ci/unit"}]
        else:
            payload = []
        return SimpleNamespace(returncode=0, stdout=json.dumps(payload), stderr="")

    assert github.fetch_gate_snapshot(
        "Ylopo", "repo", 1, "a" * 40, "2026-08-14T12:00:00Z", runner=run
    ) is None
