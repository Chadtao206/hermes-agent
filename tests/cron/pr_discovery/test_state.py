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
