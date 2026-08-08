import json
import sys
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

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
