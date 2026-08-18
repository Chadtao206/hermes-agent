"""Read-only view of Boris verdicts.

Opened with SQLite's read-only URI so this process cannot write to the review
database even by accident — the review path's integrity does not depend on this
service behaving.
"""
from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass
from enum import Enum
from pathlib import Path

from pr_remediation.config import RemediationConfig


@dataclass(frozen=True)
class EvidenceAnchor:
    kind: str
    reference: str
    detail: str | None = None


@dataclass(frozen=True)
class Issue:
    """One blocking issue as boris_intake's review produced it.

    A local type, deliberately NOT boris_intake.result.Issue: this reader's
    whole discipline is defending against a payload whose shape it does not
    own (see the module docstring and `_verdict` below, and
    `_issue`/`_anchor` immediately above it). Importing the writer's own
    dataclass would blur that boundary -- a strict, non-optional field on
    THAT type would let a malformed payload raise here as an unhandled
    TypeError instead of degrading the way every other field in this file
    does. Every field but `title` therefore defaults to an empty value, so a
    partially-unusable issue (a non-string `expected_behavior`, an
    unparsable `evidence_anchors`) still constructs and still carries its
    title -- see `_issue`.
    """
    title: str
    expected_behavior: str = ""
    changed_mechanism: str = ""
    trigger: str = ""
    consequence: str = ""
    impact_scope: str = ""
    action: str = ""
    evidence_anchors: tuple[EvidenceAnchor, ...] = ()


@dataclass(frozen=True)
class Verdict:
    request_id: str
    owner: str
    repo: str
    pr_number: int
    head_sha: str
    root_thread_ts: str
    outcome: str
    incomplete_origin: str | None
    # Widened from tuple[str, ...] (title only) to tuple[Issue, ...] (the
    # whole finding boris_intake produced) so pr_remediation/lanes.py's
    # build_brief can render what breaks, how to trigger it, and where the
    # evidence lives -- instead of a bare title the engineer agent has to
    # rediscover the rest of on its own. See `_issue` for how a payload that
    # cannot fill every field degrades rather than crashing this poller.
    blocking_issues: tuple[Issue, ...]
    # boris_intake's status_message_ts: the message Boris posts and later
    # replaces with its verdict, distinct from both source_message_ts and
    # root_thread_ts. This is the reaction anchor (see pr_remediation/loop.py's
    # _clear_running_signal / _open_lane) -- root_thread_ts stays the
    # re-review post target only. Nullable in the source schema, so this
    # trails with a default: a verdict read before this field existed on the
    # Verdict type, or a row admitted before boris_intake ever recorded a
    # status message, must still construct.
    status_message_ts: str | None = None

    @property
    def slug(self) -> str:
        return f"{self.owner}/{self.repo}#{self.pr_number}"


class AdmissionStatus(str, Enum):
    """Why an admission read answered the way it did. Total and distinct.

    Modelled the way `boris_intake.models` models its own lookups
    (`GitHubReadStatus`, `SnapshotStatus`) rather than as the `bool | None` this
    read used to answer, because the answer now carries a *time* as well as a
    verdict and the two cannot share one value. A float for "admitted at t"
    beside `False` and `None` for the other two is one `if not admitted:` away
    from reading an epoch `0.0` -- or any falsy timestamp a bad row could carry
    -- as "never admitted", which is the reading that re-posts a re-review on
    top of a live review. An `AdmissionLookup` is always truthy, so that
    mistake cannot be written here at all.

    UNREADABLE is NOT_ADMITTED's opposite and must never be folded into it:
    `loop._route_rereview_ack` re-posts on NOT_ADMITTED, and taking that act on
    a database this process merely could not open buys a duplicate review for
    nothing. Every unclassifiable failure is UNREADABLE, the same way
    `GitHubReadStatus` reserves ABSENT for a positive match and calls everything
    else UNAVAILABLE.
    """

    ADMITTED = "admitted"
    NOT_ADMITTED = "not_admitted"
    UNREADABLE = "unreadable"


@dataclass(frozen=True)
class AdmissionLookup:
    """The three-state result of one `request_exists` read.

    `admitted_at` is the epoch of the LATEST matching admission (see
    `request_exists`), and it is present on exactly one of the three states --
    the other two have no time to report, and `__post_init__` makes that an
    invariant rather than a convention, so a caller that reaches for the
    timestamp on the strength of the status is right by construction.
    """

    status: AdmissionStatus
    admitted_at: float | None = None

    def __post_init__(self) -> None:
        carries_a_time = self.admitted_at is not None
        if carries_a_time is not (self.status is AdmissionStatus.ADMITTED):
            raise ValueError("an AdmissionLookup carries an admitted_at "
                             "exactly when it is ADMITTED")

    @property
    def admitted(self) -> bool:
        return self.status is AdmissionStatus.ADMITTED

    @property
    def not_admitted(self) -> bool:
        return self.status is AdmissionStatus.NOT_ADMITTED

    @property
    def unreadable(self) -> bool:
        return self.status is AdmissionStatus.UNREADABLE


class BorisSource:
    def __init__(self, db_path: Path, cfg: RemediationConfig):
        self._db = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
        self._db.row_factory = sqlite3.Row
        self._cfg = cfg

    def close(self) -> None:
        self._db.close()

    @staticmethod
    def _text(value: object) -> str:
        """Coerce one Issue field to a string, degrading rather than raising.

        boris_intake's own schema requires each of these to be a string, but
        this reader does not own that enforcement -- the payload could have
        been written by an older or a newer version of that service. A field
        of the wrong type contributes an empty string, not an exception and
        not a dropped issue.
        """
        return value if isinstance(value, str) else ""

    @classmethod
    def _anchor(cls, raw: object) -> EvidenceAnchor | None:
        if not isinstance(raw, dict):
            return None
        kind, reference = raw.get("kind"), raw.get("reference")
        if not isinstance(kind, str) or not isinstance(reference, str) or not reference:
            # Both are required to make an anchor usable at all: a kind-less
            # or reference-less anchor points nowhere. Dropped individually
            # -- one bad anchor must not cost the issue its other, good ones.
            return None
        detail = raw.get("detail")
        return EvidenceAnchor(kind=kind, reference=reference,
                              detail=detail if isinstance(detail, str) else None)

    @classmethod
    def _anchors(cls, raw: object) -> tuple[EvidenceAnchor, ...]:
        if not isinstance(raw, list):
            return ()
        return tuple(a for a in (cls._anchor(item) for item in raw) if a is not None)

    @classmethod
    def _issue(cls, raw: object) -> Issue | None:
        """One blocking issue off the wire, or None when `raw` is not even a
        dict (mirrors the old `isinstance(i, dict)` filter in `_verdict`).

        Every field degrades independently via `_text`/`_anchors`: a
        malformed `evidence_anchors` or a non-string `changed_mechanism` must
        not cost the issue its title, which is the one field this reader has
        always been able to surface and must go on surfacing even from a
        payload it otherwise can't fully trust.

        `title` goes through `_text` like its seven siblings. It used to be
        the one field coerced with a bare `str(...)`, which turned a JSON
        null into the literal four-character string "None" -- and because
        "None" is truthy, lanes.py's `issue.title or '(untitled)'` fallback
        never fired and the remediation brief read `1. None`. An empty string
        reaches that placeholder; a stringified null walks straight past it.
        """
        if not isinstance(raw, dict):
            return None
        return Issue(
            title=cls._text(raw.get("title")),
            expected_behavior=cls._text(raw.get("expected_behavior")),
            changed_mechanism=cls._text(raw.get("changed_mechanism")),
            trigger=cls._text(raw.get("trigger")),
            consequence=cls._text(raw.get("consequence")),
            impact_scope=cls._text(raw.get("impact_scope")),
            action=cls._text(raw.get("action")),
            evidence_anchors=cls._anchors(raw.get("evidence_anchors")),
        )

    def new_verdicts(self, limit: int = 50) -> tuple[Verdict, ...]:
        """The completed reviews this loop owns, newest first.

        Every clause is load-bearing, and none of them is enforced anywhere
        else -- `loop.py` routes whatever comes back out of here.

        `workflow_state='complete'` and NOT `slack_unavailable`, even though
        boris_intake treats both as terminal (`store._TERMINAL_STATES`). A
        request reaches `slack_unavailable` from exactly one place:
        `reconciler._reconcile_delivery` catching `SlackRootUnavailable`, i.e.
        the source thread this review was requested in no longer exists. Its
        `semantic_outcome` is already set by then, so it passes every other
        filter here -- but its `root_thread_ts` points at a thread that is
        gone, and `root_thread_ts` is the ONLY place this loop can post a
        re-review (`SlackIO._post_thread_reply`). Routing one would spend a
        remediation round on a pull request whose verdict never reached a
        human, then fail to ask for the re-review that round exists to
        trigger, with `_post` swallowing the Slack error into a bare `None`.
        Excluding it leaves the request where boris_intake left it: terminal,
        undelivered, and visible to a human rather than churned by a loop.

        `semantic_outcome IS NOT NULL` because `loop.py` branches on
        `verdict.outcome` and a NULL would arrive as `None`, matching no
        branch. Redundant with `complete` today (boris_intake sets the
        outcome before it transitions), but it is boris_intake's invariant,
        not this reader's, and this file's whole discipline is not trusting a
        payload whose shape it does not own.

        `requester_user_id` and `pr_author` are the two identity filters:
        Chad asked for the review, and Chad wrote the pull request. The
        second is the SOUL.md third-party gate -- this loop must never push
        commits to someone else's branch. Contrast `request_exists` below,
        which deliberately applies neither.

        `ORDER BY completed_at DESC LIMIT ?` is not cosmetic and not a
        safety net. Nothing marks a row consumed on this side; the loop's own
        `store.mark_verdict_seen` does that, AFTER reading. So the window is
        a window over the whole matching history, and the live database
        already holds 58 rows matching this query against a default `limit`
        of 50. Ordering ascending, or not ordering at all, means a cold start
        after extended downtime silently reads the oldest 50 and never the
        verdicts that arrived while it was down -- no error, no backlog
        anywhere, just a loop that never sees the work it exists to do. DESC
        makes the truncation fall on history the loop has already routed.
        """
        cur = self._db.execute(
            "SELECT request_id, owner, repo, pr_number, head_sha, root_thread_ts, "
            "status_message_ts, semantic_outcome, validated_result_json "
            "FROM review_requests "
            "WHERE workflow_state='complete' AND semantic_outcome IS NOT NULL "
            "AND requester_user_id=? AND pr_author=? "
            "ORDER BY completed_at DESC LIMIT ?",
            (self._cfg.chad_slack_id, self._cfg.chad_github_login, limit),
        )
        return tuple(self._verdict(row) for row in cur.fetchall())

    def request_exists(self, owner: str, repo: str, pr_number: int,
                       head_sha: str, since: float) -> AdmissionLookup:
        """Has a review request been admitted at this head since `since`, and
        when? UNREADABLE if the database could not be read.

        Three-way, the same contract shape `github_io.unresolved_bot_threads`
        uses, and for the same reason: the caller
        (`loop._route_rereview_ack`) has a genuinely different act for each
        of the three, and folding the unreadable case into either answer
        would pick one of those acts on no evidence. NOT_ADMITTED there
        re-posts a re-review; taking it on a database this process merely could
        not open buys a duplicate review for nothing.

        The answer carries *when*, not only *whether*, because the caller's
        absolute deadline has to be anchored at the admission and not at the
        wait: a request admitted late in a long wait was getting whatever was
        left of four hours, so a re-review admitted at 3h50m had ten minutes
        before that guard escalated a review that was running perfectly. The
        state and the time therefore travel together in an `AdmissionLookup`
        rather than as a `float | False | None` -- see `AdmissionStatus` for why
        that shape and not the cheaper one. The name still asks the same
        question it always did; only the answer got richer.

        The question is deliberately "admitted *since* this wait began", not
        "exists at all", and both terms are load-bearing because each catches a
        stale row the other cannot.

        `head_sha` catches the request a remediation round leaves behind: the
        one whose verdict started the round, admitted at the head the lane has
        since moved off. Without it that row answers "already admitted" for
        every head after it, and the loop this read exists to rescue strands
        exactly as it does today.

        `since` catches the one the head term is blind to, because on that path
        the head never moves. `loop._repost` re-sends a re-review after a
        system-origin incomplete without touching `head_sha`, so the request
        that produced *that very verdict* is still sitting at the loop's head:
        a head-only query answers ADMITTED unconditionally there, the guard is
        inert, and a rejected re-post strands the loop silently. `created_at`
        is boris_intake's REAL epoch column and the caller's timestamp comes
        off this same machine's clock, so the two are directly comparable.

        Inclusive (`>=`): the caller's `since` is when it entered the wait, and
        the post that opens that wait is made *before* the row moves, so a
        request admitted in the same instant is this one, not a predecessor.

        Any `workflow_state` counts. A request that is still `reviewing` will
        produce a verdict on its own, so re-posting under it would be
        pointless noise; only `new_verdicts` needs the completed ones,
        because only it is looking for something to route.

        Deliberately NOT filtered by `requester_user_id` / `pr_author` the
        way `new_verdicts` is. Those filters are there to select the verdicts
        this loop owns; the question here is only whether a review already
        exists at this head, whoever asked for it, and a narrower query would
        answer NOT_ADMITTED for a live review and re-post on top of it.

        `COLLATE NOCASE` on the three text columns, matching
        `pr_discovery/discover.py::IntakeSource.already_requested`, which
        queries these same columns of this same table and argues the case at
        length: GitHub resolves owner and repo names case-insensitively and a
        commit SHA is case-insensitive hex, so `Ylopo/Million-Stars` and
        `Ylopo/million-stars` are one repository and must not disagree about
        whether a review exists. It does not misfire today because this loop's
        `owner`/`repo`/`head_sha` come off the very row it is asking about --
        but that is an accident of the caller, not a property of the query, and
        the invariant was documented in one module and not honoured in its
        sibling. A NOT_ADMITTED here re-posts a re-review on top of a live one.

        `MAX(created_at)` picks among the rows those terms match, and it is
        deliberately not `MIN`: the LATEST admission is the one the deadline is
        about. That deadline means "a request was accepted and produced
        nothing"; if an older admission produced nothing but a newer one is in
        flight, the loop is not stuck, and anchoring on the older one would
        escalate out from under a live review -- exactly the bug this shape
        exists to fix. `MIN` is the reading someone will "fix" this to later; it
        is the wrong one.

        An aggregate with no GROUP BY returns exactly one row even when nothing
        matches, so the *row* is never `None` here and `row is not None` would
        answer ADMITTED unconditionally, for every head, forever -- the same
        strand the `head_sha` term above exists to prevent, rebuilt in the
        result handling. NULL in that one row is what "no matching request"
        looks like. (A row whose `created_at` is NULL cannot reach the
        aggregate: `NULL >= since` is NULL, so the `since` term never matches
        it. That was true of the old `SELECT 1` too, so nothing changes there.)

        `float(...)` on the way out, mirroring `boris_intake.store`'s own
        `float(row["created_at"])`: this is the first value from a column this
        reader does not own to reach arithmetic, and the caller compares it
        against a clock. The column is `REAL NOT NULL` over there, so a value
        that will not convert takes a hand-edit or a bad migration -- and when
        one does, it raises inside the `try` below and lands as UNREADABLE,
        which is honest ("could not tell when") and already bounded by the
        caller's deadline. It must never reach the caller as a string, where
        `admitted_at <= now` raises a TypeError that aborts the whole settle.

        Wrapped rather than allowed to raise: this runs inside a routing
        pass, where an exception would abort the whole settle for a loop that
        may owe other writes.
        """
        try:
            row = self._db.execute(
                "SELECT MAX(created_at) FROM review_requests "
                "WHERE owner=? COLLATE NOCASE AND repo=? COLLATE NOCASE "
                "AND pr_number=? AND head_sha=? COLLATE NOCASE "
                "AND created_at>=?",
                (owner, repo, pr_number, head_sha, since),
            ).fetchone()
            if row[0] is None:
                return AdmissionLookup(AdmissionStatus.NOT_ADMITTED)
            return AdmissionLookup(AdmissionStatus.ADMITTED,
                                   admitted_at=float(row[0]))
        except Exception:
            return AdmissionLookup(AdmissionStatus.UNREADABLE)

    def _verdict(self, row: sqlite3.Row) -> Verdict:
        try:
            payload = json.loads(row["validated_result_json"] or "{}")
        except ValueError:
            payload = {}
        # boris_intake owns this payload's shape and evolves it independently
        # of this reader. Malformed JSON *syntax* is handled above; here we
        # additionally guard against JSON that parses fine but into a shape
        # we don't expect (wrong top-level type, wrong field types). Coerce
        # to the empty/absent form rather than raising — one unrecognised
        # row must never take the polling loop down.
        if not isinstance(payload, dict):
            payload = {}
        incomplete = payload.get("incomplete")
        if not isinstance(incomplete, dict):
            # An unreadable `incomplete` payload must never be mistaken for
            # origin="system" (which tells the remediation loop the failure
            # was transient and safe to retry without spending a round).
            # Falling back to {} here means incomplete.get("origin") below
            # naturally yields None, which escalates to a human instead of
            # silently retrying forever on a payload we can't interpret.
            incomplete = {}
        issues = payload.get("blocking_issues")
        if not isinstance(issues, list):
            issues = []
        return Verdict(
            request_id=row["request_id"], owner=row["owner"], repo=row["repo"],
            pr_number=row["pr_number"], head_sha=row["head_sha"],
            root_thread_ts=row["root_thread_ts"],
            outcome=row["semantic_outcome"],
            incomplete_origin=incomplete.get("origin"),
            blocking_issues=tuple(
                issue for issue in (self._issue(i) for i in issues)
                if issue is not None
            ),
            status_message_ts=row["status_message_ts"],
        )
