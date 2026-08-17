"""Tests for the discovery ledger (`pr_discovery.state.Ledger`)."""
from __future__ import annotations

import sqlite3

from pr_discovery.state import Ledger


def test_fresh_identity_is_not_already_posted(tmp_path):
    ledger = Ledger(tmp_path / "ledger.db")
    assert ledger.already_posted("Ylopo", "million-stars", 42, "abc123") is False


def test_record_posted_makes_already_posted_true(tmp_path):
    ledger = Ledger(tmp_path / "ledger.db")
    ledger.record_posted("Ylopo", "million-stars", 42, "abc123", now=1000.0)
    assert ledger.already_posted("Ylopo", "million-stars", 42, "abc123") is True


def test_record_posted_is_idempotent_under_a_repeat_call(tmp_path):
    """Re-posting at an UNCHANGED head must not raise, and must not create a
    second row -- this is the ledger's whole reason to exist per the brief:
    a duplicate is a backstop, not something to let happen twice at this
    layer."""
    db_path = tmp_path / "ledger.db"
    ledger = Ledger(db_path)
    first = ledger.record_posted("Ylopo", "million-stars", 42, "abc123", now=1000.0)
    second = ledger.record_posted("Ylopo", "million-stars", 42, "abc123", now=2000.0)
    assert first is True
    assert second is False

    raw = sqlite3.connect(str(db_path))
    count = raw.execute(
        "SELECT COUNT(*) FROM posted WHERE owner=? AND repo=? AND number=? "
        "AND head_sha=?",
        ("Ylopo", "million-stars", 42, "abc123"),
    ).fetchone()[0]
    raw.close()
    assert count == 1


def test_a_new_head_is_a_different_identity_and_reqalifies(tmp_path):
    """The exact semantics the brief calls out: 'a re-post at an unchanged
    head is admitted as a duplicate' implies the converse -- a CHANGED head
    is not a duplicate at all, and must not be shadowed by an old post at a
    stale head for the same PR number."""
    ledger = Ledger(tmp_path / "ledger.db")
    ledger.record_posted("Ylopo", "million-stars", 42, "abc123", now=1000.0)
    assert ledger.already_posted("Ylopo", "million-stars", 42, "abc123") is True
    assert ledger.already_posted("Ylopo", "million-stars", 42, "def456") is False


def test_different_pr_numbers_are_independent_identities(tmp_path):
    ledger = Ledger(tmp_path / "ledger.db")
    ledger.record_posted("Ylopo", "million-stars", 42, "abc123", now=1000.0)
    assert ledger.already_posted("Ylopo", "million-stars", 43, "abc123") is False


def test_different_repos_are_independent_identities(tmp_path):
    ledger = Ledger(tmp_path / "ledger.db")
    ledger.record_posted("Ylopo", "million-stars", 42, "abc123", now=1000.0)
    assert ledger.already_posted("Ylopo", "other-repo", 42, "abc123") is False


def test_schema_created_on_open_with_the_unique_index(tmp_path):
    db_path = tmp_path / "ledger.db"
    Ledger(db_path)
    raw = sqlite3.connect(str(db_path))
    tables = {row[0] for row in raw.execute(
        "SELECT name FROM sqlite_master WHERE type='table'")}
    indexes = {row[0] for row in raw.execute(
        "SELECT name FROM sqlite_master WHERE type='index'")}
    raw.close()
    assert "posted" in tables
    assert "posted_identity" in indexes


def test_unique_index_rejects_a_direct_duplicate_insert(tmp_path):
    """Pins the UNIQUE constraint itself (not just record_posted's swallow
    of it) -- the brief's Step 3 explicitly requires this index so
    `record_posted` is idempotent 'under concurrent ticks', i.e. two
    separate connections racing each other, not just two calls on the same
    Ledger object."""
    db_path = tmp_path / "ledger.db"
    Ledger(db_path)
    raw = sqlite3.connect(str(db_path))
    raw.execute(
        "INSERT INTO posted (owner, repo, number, head_sha, at) "
        "VALUES (?,?,?,?,?)",
        ("Ylopo", "million-stars", 42, "abc123", 1000.0),
    )
    raw.commit()
    try:
        raw.execute(
            "INSERT INTO posted (owner, repo, number, head_sha, at) "
            "VALUES (?,?,?,?,?)",
            ("Ylopo", "million-stars", 42, "abc123", 2000.0),
        )
        raised = False
    except sqlite3.IntegrityError:
        raised = True
    finally:
        raw.close()
    assert raised is True


def test_two_ledger_instances_on_the_same_db_do_not_double_record(tmp_path):
    """Simulates two concurrent ticks: separate `Ledger` objects (separate
    connections), same underlying file, racing to record the same
    identity."""
    db_path = tmp_path / "ledger.db"
    ledger_a = Ledger(db_path)
    ledger_b = Ledger(db_path)
    first = ledger_a.record_posted("Ylopo", "million-stars", 42, "abc123", now=1000.0)
    second = ledger_b.record_posted("Ylopo", "million-stars", 42, "abc123", now=1000.1)
    assert {first, second} == {True, False}

    raw = sqlite3.connect(str(db_path))
    count = raw.execute("SELECT COUNT(*) FROM posted").fetchone()[0]
    raw.close()
    assert count == 1


def test_db_directory_is_created_if_missing(tmp_path):
    nested = tmp_path / "does" / "not" / "exist" / "ledger.db"
    Ledger(nested)
    assert nested.exists()


def test_the_intake_backstop_does_not_hold_across_a_ci_rerun(tmp_path):
    """Pins this module's docstring, which used to claim the intake gate
    "already dedups on (repo, pr, head_sha)" -- half true, and the wrong half
    for anyone reasoning about what happens if `state/pr_discovery.db` is lost.

    boris_intake's durable key is `(review_identity, gate_fingerprint)`, and
    the fingerprint hashes each CI check's run URL. A CI re-run at an unchanged
    head therefore admits as a NEW review, not a duplicate -- so the backstop
    underneath this ledger is narrower than per-head. Driven through
    boris_intake's real Store and its real snapshot builder rather than
    restated, so the claim cannot quietly stop being true."""
    from pathlib import Path

    from boris_intake.models import NormalizedEvent, ReviewCommand, ReviewPolicy
    from boris_intake.result import build_gate_snapshot, load_gate_registry
    from boris_intake.store import Store

    registry = load_gate_registry(
        Path(__file__).resolve().parents[3]
        / "cron" / "scripts" / "boris_intake" / "gate_registry.json")

    def snapshot(run_id):
        return build_gate_snapshot(
            [{"name": "unit-tests", "state": "SUCCESS",
              "link": f"https://ci.example/runs/{run_id}", "required": True}],
            "2026-08-14T12:00:00Z", registry)

    command = ReviewCommand(
        owner="Ylopo", repo="million-stars", pr_number=42, head_sha="a" * 40,
        pr_author="Chadtao206", requester_user_id="U1",
        requester_email="a@ylopo.com", channel_id="C1", message_ts="1.0",
        root_thread_ts="1.0",
        permalink="https://ylopo.slack.com/archives/C1/p10",
        policy=ReviewPolicy(ignore_ci=False, rubric_version="2"))

    def event(ts):
        return NormalizedEvent("C1", "channel", "U1", ts, None,
                               "https://github.com/Ylopo/million-stars/pull/42",
                               False, None)

    store = Store(tmp_path / "intake.db")
    first = store.admit_event(event("1.0"), (command,), (snapshot(1),), 1_000_000.0)
    # Same head, same policy, so the same review_identity -- only CI moved.
    rerun = store.admit_event(event("2.0"), (command,), (snapshot(2),), 1_000_100.0)
    # And the control: identical CI really does dedup.
    repost = store.admit_event(event("3.0"), (command,), (snapshot(1),), 1_000_200.0)

    assert len(first.admitted) == 1 and first.duplicates == ()
    assert len(rerun.admitted) == 1, (
        "a CI re-run at an unchanged head is a new review, not a duplicate")
    assert rerun.duplicates == ()
    assert repost.admitted == () and len(repost.duplicates) == 1
