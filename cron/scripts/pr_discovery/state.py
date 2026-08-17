"""The discovery ledger: durable, per-head idempotency for "did we already
post this".

`boris_intake` dedups too, but on a NARROWER key than a per-head one, and
the difference is the reason this ledger exists rather than being belt and
braces. Its durable key is `(review_identity, gate_fingerprint)`
(`store.py::admit_event`): `review_identity` is
`borisslack:{owner}/{repo}#{pr}@{head_sha}:policy={fp}`, per-head as the name
suggests -- but `gate_fingerprint` is a sha256 over the PR's whole CI check
list, INCLUDING each check's run URL (`result.py::build_gate_snapshot`).
A CI re-run at an unchanged head changes those URLs, so it changes the
fingerprint, so the same head admits as a NEW review rather than a duplicate.

So the intake gate is a backstop that holds for a re-post in the same CI
state and not otherwise, and even where it holds a duplicate still costs a
real Slack message and a reject reply, i.e. noise a human reads. This ledger
is what actually prevents the noise, by letting the poster ask "have we
already posted this exact (owner, repo, number, head_sha)" BEFORE it ever
sends anything, the same way `pr_remediation/store.py`'s `mark_verdict_seen`
guards its own send path. It follows that losing `state/pr_discovery.db`
does not merely cost a round of duplicate messages: it can cost a round of
duplicate REVIEWS, one per pull request whose CI has moved since it was
posted.

Deliberately does not gate on `candidates()`'s output -- `discover.py` stays
pure with respect to persistent state; only the poster consults this ledger.
"""
from __future__ import annotations

import sqlite3
from pathlib import Path

SCHEMA = """
CREATE TABLE IF NOT EXISTS posted (
    owner TEXT NOT NULL,
    repo TEXT NOT NULL,
    number INTEGER NOT NULL,
    head_sha TEXT NOT NULL,
    at REAL NOT NULL
);
CREATE UNIQUE INDEX IF NOT EXISTS posted_identity
    ON posted (owner, repo, number, head_sha);
"""


class Ledger:
    """A tiny SQLite-backed set of "already posted" identities.

    Schema created on open (mirrors `pr_remediation/store.py::Store`). Every
    query and write is parameterized -- there is no string-interpolated SQL
    anywhere in this file, and no dynamic column set to allowlist, unlike
    `Store.transition`'s `_TRANSITION_FIELDS` guard: `posted` has exactly
    four data columns and every one of them is always written, so there is
    no caller-supplied field name that could ever reach a query string. A
    SQL-injection Critical was found and fixed in `pr_remediation/store.py`
    during review; the discipline that fix established -- never trust a
    caller-shaped value into anything but a `?` placeholder -- is followed
    here from the start rather than retrofitted.
    """

    def __init__(self, db_path: Path):
        self.path = Path(db_path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._db = sqlite3.connect(str(self.path), timeout=10)
        self._db.row_factory = sqlite3.Row
        self._db.execute("PRAGMA journal_mode=WAL")
        self._db.executescript(SCHEMA)
        self._db.commit()

    def close(self) -> None:
        self._db.close()

    def already_posted(self, owner: str, repo: str, number: int,
                       head_sha: str) -> bool:
        """True iff this exact (owner, repo, number, head_sha) was already
        recorded. A later head for the same PR is a DIFFERENT identity and
        answers False -- the PR re-qualifies once it has moved, which is the
        whole point of keying on `head_sha` rather than just `number`."""
        cur = self._db.execute(
            "SELECT 1 FROM posted WHERE owner=? AND repo=? AND number=? "
            "AND head_sha=? LIMIT 1",
            (owner, repo, number, head_sha),
        )
        return cur.fetchone() is not None

    def record_posted(self, owner: str, repo: str, number: int,
                      head_sha: str, *, now: float) -> bool:
        """Record that this identity was posted. Returns True if this call
        was the one that recorded it, False if it was already there.

        `now` is required, never read from the clock internally -- matching
        `pr_remediation/store.py`'s mutators, which take `now` as an
        explicit parameter throughout so callers (and their tests) control
        time rather than this method reaching for `time.time()` itself.

        Idempotent under concurrent ticks by construction: the UNIQUE index
        on `(owner, repo, number, head_sha)` (see `SCHEMA`) is what makes a
        second `record_posted` for the same identity a caught
        `IntegrityError`, not a duplicate row and not a crash -- two
        discovery ticks racing to post the same PR at the same head can both
        call this safely.
        """
        try:
            self._db.execute(
                "INSERT INTO posted (owner, repo, number, head_sha, at) "
                "VALUES (?,?,?,?,?)",
                (owner, repo, number, head_sha, now),
            )
            self._db.commit()
            return True
        except sqlite3.IntegrityError:
            self._db.rollback()
            return False
