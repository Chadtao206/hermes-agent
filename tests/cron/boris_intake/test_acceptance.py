import json
from types import SimpleNamespace

import pytest

from boris_intake.acceptance import JiraAcceptanceAdapter, ticket_keys


@pytest.mark.parametrize(
    "title,branch,expected",
    [
        # Every pull request reviewed so far names its ticket both ways.
        (
            "feat(branded-builder): [PLAT-2345] revamp the first-run experience",
            "feat/PLAT-2345-branded-builder-first-run",
            ("PLAT-2345",),
        ),
        (
            "feat: ✨ [HSS-9054] Performance — Texting section",
            "feature/HSS-9054-texting-performance",
            ("HSS-9054",),
        ),
        (
            "fix(vail-llm): [MID-949] don't resume qualification",
            "MID-949_send_listings_nurture_script_suppression",
            ("MID-949",),
        ),
        # Branch-only is still enough.
        ("outbox delivery poller", "feature/PLAT-2266", ("PLAT-2266",)),
        # Nothing to find is not an error.
        ("chore: bump deps", "chore/bump-deps", ()),
    ],
)
def test_ticket_keys_finds_the_claimed_ticket(title, branch, expected):
    assert ticket_keys(title, branch) == expected


@pytest.mark.parametrize(
    "title", ["fix: handle UTF-8 in slugs", "chore: bump to HTTP-2", "docs: cite RFC-7231"]
)
def test_ticket_keys_ignores_tokens_that_only_look_like_issue_keys(title):
    assert ticket_keys(title, "") == ()


def test_ticket_keys_prefers_the_bracketed_claim_over_an_incidental_mention():
    keys = ticket_keys("feat: [PLAT-2345] supersedes PLAT-2344", "feat/PLAT-2345-x")
    assert keys[0] == "PLAT-2345"


def adapter_with(runner):
    adapter = JiraAcceptanceAdapter()
    type(adapter).runner = staticmethod(runner)
    return adapter


def test_adapter_returns_evidence_the_helper_produced():
    evidence = {"kind": "ticket", "reference": "PLAT-2345", "content": "Ship the thing."}

    def runner(command, **kwargs):
        assert command[-1] == "PLAT-2345"
        return SimpleNamespace(returncode=0, stdout=json.dumps(evidence), stderr="")

    found = adapter_with(runner).fetch("feat: [PLAT-2345] x", "feat/PLAT-2345-x")

    assert found == (("PLAT-2345", evidence),)


@pytest.mark.parametrize(
    "outcome",
    [
        SimpleNamespace(returncode=1, stdout="", stderr="boom"),
        SimpleNamespace(returncode=0, stdout="", stderr=""),
        SimpleNamespace(returncode=0, stdout="not json", stderr=""),
        SimpleNamespace(returncode=0, stdout=json.dumps({"content": ""}), stderr=""),
    ],
)
def test_adapter_treats_every_helper_failure_as_simply_no_evidence(outcome):
    assert adapter_with(lambda command, **kw: outcome).fetch(
        "feat: [PLAT-2345] x", ""
    ) == ()


def test_adapter_survives_a_helper_that_hangs_or_crashes():
    def runner(command, **kwargs):
        raise TimeoutError("helper never answered")

    assert adapter_with(runner).fetch("feat: [PLAT-2345] x", "") == ()
