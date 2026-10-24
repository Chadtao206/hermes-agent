#!/usr/bin/env python3
"""Drive the Son of Chad reaction state machine on Boris Slack PR-review posts.

Scheduled/no-argument operation is limited to #boris-reviews. Explicit
``--channel`` arguments retain the historical watched channels as operator-only
rollback and forensic tooling. A source PR post receives exactly one Son of Chad
reaction reflecting the reviewer lane's current state.

The modern Boris mapping is:

    reviewer lane kicked off / still active   -> :rubbing:
    Boris left non-blocking review comments   -> :doge_notes:
    Boris reported blocked / not ready        -> :face_vomiting:
    clean approval (or existing head approval)-> :blinkapproved:

Explicit legacy-channel operation retains ``thinkies``, ``doge_notes``,
``review-no``, and ``white_check_mark`` without rewriting retired-channel
history.

When one Slack post spawned several PR reviews, the post shows the highest-
precedence state across all of its lanes:

    blocked > notes > active > approved

The semantic ``blocked`` state is Boris's not-ready verdict (NEEDS_FIX / REJECT /
explicit blocking findings). It is deliberately not tied to a GitHub blocking
review state, because the review policy forbids REQUEST_CHANGES.

Transitions are idempotent: the desired reaction is added FIRST, then stale
Son-of-Chad-owned reactions are removed, so a failure never leaves the post
bare. ``already_reacted``, ``no_reaction``, and removal-only ``not_reacted`` are
successes. If the add fails, the prior reaction is retained and the post is retried
with backoff.

Three fail-closed invariants hold everywhere:

* **Ambiguity is never resolved in favour of approval.** A malformed surface that
  still reads as blocking classifies as ``review-no``; one that cannot be read at
  all, or an approval claim contradicted by a veto (``posted: false``, a
  non-APPROVED state on any alias), quarantines the run. All review/approval
  aliases are validated together, so a contradiction between two of them cannot be
  settled by whichever happens to be read first.
* **An unresolved lane holds its post, forever if need be.** A completed lane that
  could not be fetched or could not be safely classified may be hiding a
  higher-precedence block, so its post is not touched at all — no add, no removal,
  no transition. Retry exhaustion raises an alert, never a give-up. Every hold
  persists its own provenance (``posts[].unresolved_lanes = {task_id: reason}``) for
  *both* kinds of unresolved lane — unshowable (failed/deferred ``kanban show``) and
  quarantined (read but unsafe to classify) — so the hold is a property of the post
  record and not of whatever the current tick happens to rediscover. It therefore
  survives the ``--lookback-seconds`` boundary, a lane dropping out of a later
  successful ``kanban list``, restarts, and compaction, and ends only when that lane
  reclassifies or an operator passes ``--clear-unresolved``.
* **This watchdog is the single authoritative state writer.** Boris intake is a
  best-effort seam allowed exactly one verb (add the initial ``rubbing``). Its late
  add is invisible to local state, so every transition arms a bounded number of
  blind reconciliation sweeps that re-assert the desired end state; afterwards a
  settled post is back to zero Slack calls.

Discovery is bounded: one reviewer-lane `kanban list` per tick, active lanes are
classified from the list row alone (their source permalink lives in the task
body), and `kanban show` is only spent on newly-completed lanes, capped per tick
with a persistent cursor. There is no full historical scan.

Quiet on no-op. Emits compact lines only when a post transitions or a retryable
failure needs operator visibility.
"""
from __future__ import annotations

import argparse
import contextlib
import fcntl
import json
import os
import re
import subprocess
import time
from pathlib import Path
from typing import Any, Callable, Iterable

HOME = Path(os.environ.get("HERMES_HOME") or "/Users/ctao/.hermes")
STATE_PATH = HOME / "state" / "just_do_it_review_reactions.json"
STATE_BACKUP_SUFFIX = ".bak-pre-reaction-state-machine"
SLACK_REACT = HOME / "scripts" / "slack_react.py"
DEFAULT_LOOKBACK_SECONDS = 7 * 24 * 3600
FAIL_RETRY_SECONDS = 60 * 60  # backoff ceiling (legacy constant name/value)
RETRY_BACKOFF_SECONDS = (0, 120, 600, 1800, FAIL_RETRY_SECONDS)
MAX_SHOW_ATTEMPTS = 5  # alert threshold; the lane keeps deferring its post, it is never given up on
MAX_SHOWS_PER_TICK = 12
SEEN_RETENTION_SECONDS = 14 * 24 * 3600
POST_RETENTION_SECONDS = 30 * 24 * 3600
# Bounded reconciliation after a Boris post converges: the intake seam can land a
# late best-effort `rubbing` add *after* this watchdog removed stale reactions, and
# local state alone can never see it. Each transition therefore schedules a bounded
# number of blind removal sweeps, then convergence returns to zero Slack calls.
RECONCILE_DELAYS_SECONDS = (300, 3600)
# How often an unresolved (deferred / quarantined) post re-alerts while it is held.
UNRESOLVED_ALERT_SECONDS = 6 * 3600
# A show deferred by the per-tick cap or the retry backoff still records a hold (it
# has to: the lane may never become showable and would otherwise silently release the
# post once its row ages out of the lookback window), but such a hold is normally
# resolved on the very next tick. Alerting on it immediately would turn routine
# bounded discovery into operator noise, so an *unshowable* hold is reported once it
# has outlived this grace window. A *quarantined* hold is never transient and is
# reported at once.
HOLD_ALERT_GRACE_SECONDS = 15 * 60
STATE_SCHEMA_VERSION = 3
# Kinds of hold recorded in ``posts[].unresolved_lanes``, stored as a
# ``"<kind>: <why>"`` provenance string per held task id.
HOLD_KIND_QUARANTINE = "quarantine"  # lane was read, but is not safe to classify
HOLD_KIND_UNSHOWABLE = "unshowable"  # lane could not be read (show failed/deferred)
HOLD_KIND_UNKNOWN = "unknown"  # provenance lost (legacy/corrupt state); treated strictly
HOLD_KINDS = (HOLD_KIND_QUARANTINE, HOLD_KIND_UNSHOWABLE, HOLD_KIND_UNKNOWN)
# Strictest first: the label and the alert grace of a multi-lane hold come from the
# strictest kind holding it, so one quarantined (or provenance-less) lane is never
# reported as a routine deferral.
HOLD_KIND_STRICTNESS = (HOLD_KIND_QUARANTINE, HOLD_KIND_UNKNOWN, HOLD_KIND_UNSHOWABLE)
HOLD_REASON_BY_KIND = {
    HOLD_KIND_QUARANTINE: "quarantined_lane",
    HOLD_KIND_UNSHOWABLE: "unclassified_completed_lane",
    HOLD_KIND_UNKNOWN: "unresolved_lane",
}
PERMALINK_RE = re.compile(
    r"https://ylopo\.slack\.com/archives/(?P<channel>[A-Z0-9]+)/p(?P<ts>\d{16})(?!\d)"
    r"(?:\?[^\s)>'\"]*)?",
    re.IGNORECASE,
)
# Source channels whose posts this watchdog may react to. #meeting-room and
# other chatter channels must never be reacted to.
BORIS_CHANNEL = "C0BPCKPEFCG"
LIFECYCLE_CREATED_BY = "boris-slack-intake"

STATE_BLOCKED = "blocked"
STATE_NOTES = "notes"
STATE_ACTIVE = "active"
STATE_APPROVED = "approved"

STATE_PRECEDENCE = {
    STATE_BLOCKED: 4,
    STATE_NOTES: 3,
    STATE_ACTIVE: 2,
    STATE_APPROVED: 1,
}

LEGACY_REACTIONS = {
    STATE_BLOCKED: "review-no",
    STATE_NOTES: "doge_notes",
    STATE_ACTIVE: "thinkies",
    STATE_APPROVED: "white_check_mark",
}
BORIS_REACTIONS = {
    STATE_BLOCKED: "face_vomiting",
    STATE_NOTES: "doge_notes",
    STATE_ACTIVE: "rubbing",
    STATE_APPROVED: "blinkapproved",
}
REACTION_TO_STATE = {
    reaction: state
    for table in (LEGACY_REACTIONS, BORIS_REACTIONS)
    for state, reaction in table.items()
}


def normalize_review_state(value: object) -> str | None:
    text = str(value or "")
    if text in STATE_PRECEDENCE:
        return text
    return REACTION_TO_STATE.get(text)


def source_channel(permalink: str) -> str | None:
    match = PERMALINK_RE.search(str(permalink or ""))
    return match.group("channel").upper() if match else None


def render_reaction(
    permalink: str,
    state: str,
    boris_style: str = "modern",
) -> str:
    if state not in STATE_PRECEDENCE:
        raise ValueError(f"unknown review state: {state}")
    if source_channel(permalink) == BORIS_CHANNEL and boris_style == "modern":
        return BORIS_REACTIONS[state]
    return LEGACY_REACTIONS[state]


def owned_reactions(permalink: str) -> tuple[str, ...]:
    tables = (
        (LEGACY_REACTIONS, BORIS_REACTIONS)
        if source_channel(permalink) == BORIS_CHANNEL
        else (LEGACY_REACTIONS,)
    )
    return tuple(dict.fromkeys(reaction for table in tables for reaction in table.values()))


LEGACY_CHANNELS = {
    "C08K8FQKX9S",  # #just-do-it / ticket-hub
    "C07BU8AAPB3",
    "C09339EGZ9U",  # #eng-midnight-vulcan
}
SUPPORTED_CHANNELS = LEGACY_CHANNELS | {BORIS_CHANNEL}
SCHEDULED_DEFAULT_CHANNELS = {BORIS_CHANNEL}
# Backward-compatible name for callers that deliberately request every supported
# channel. Scheduled/default execution uses SCHEDULED_DEFAULT_CHANNELS instead.
DEFAULT_ALLOWED_CHANNELS = SUPPORTED_CHANNELS

EMOJI_BLOCK = "review-no"
EMOJI_COMMENT = "doge_notes"
EMOJI_ACTIVE = "thinkies"
EMOJI_APPROVED = "white_check_mark"

ACTIVE_STATUSES = {"todo", "triage", "ready", "scheduled", "running", "review"}
DONE_STATUSES = {"done"}

# --- classifier vocabulary -------------------------------------------------
# Markers are matched as substrings of a normalized (UPPER, dash-separated)
# string, so reviewer verdicts that carry trailing prose still classify
# ("NEEDS_FIX; sections 2+ are not safe to enable" -> blocking).
BLOCKING_MARKERS = (
    "NOT-READY",
    "NOT-SAFE",
    "NOT-MERGEABLE",
    "DO-NOT-MERGE",
    "NEEDS-FIX",
    "NEEDS-WORK",
    "NEEDS-REWORK",
    "NEEDS-CHANGES",
    "REQUIRED-CHANGES",
    "REQUIRE-CHANGES",
    "REQUEST-CHANGES",
    "REQUESTED-CHANGES",
    "CHANGES-REQUESTED",
    "MUST-FIX",
    "REJECT",
    "BLOCKER",
    "BLOCKING",
    "BLOCKED",
    "FAILED-REVIEW",
)
# A verdict the reviewer itself labels non-blocking is a comment review, whatever
# blocking marker precedes the label ("NEEDS_FIX_NON_BLOCKING_COMMENT" -> notes).
NONBLOCKING_MARKERS = ("NON-BLOCKING", "COMMENT-ONLY")
COMMENT_MARKERS = (
    "COMMENTED",
    "COMMENT",
    "NEEDS-FOLLOW-UP",
    "FOLLOW-UP",
    "ADVISORY",
    "NIT",
    "SUGGESTION",
)
BLOCKING_SEVERITY_MARKERS = (
    "BLOCKER",
    "BLOCKING",
    "MUST-FIX",
    "CRITICAL",
    "SEV0",
    "SEV-0",
    "P0",
)
# Reviewers write verdicts as "<verdict>; <prose>", and the prose routinely
# mentions blockers in the negative ("APPROVE; ...; no merge blocker; ..."), so
# only the FIRST clause of a verdict is classified.
CLAUSE_SPLIT_RE = re.compile(r"[;.,\n|/]")
# Second line of defense inside a clause: a marker is inert when a nearby token
# negates it ("non-blocking COMMENT", "no merge blocker", "prior_blocker").
NEGATION_TOKENS = {
    "NO", "NON", "NOT", "NEVER", "ZERO", "WITHOUT", "MINUS",
    "PRIOR", "PREVIOUS", "PRE", "FORMER", "HISTORICAL",
    "RESOLVED", "FIXED", "ADDRESSED", "REMEDIATED", "CLEARED",
}
EMPTY_TOKENS = {"NONE", "EMPTY", "ZERO", "NA", "ABSENT", "CLEAR", "CLEARED", "RESOLVED", "FIXED", "ADDRESSED"}
NEGATION_WINDOW = 3
VERDICT_KEYS = (
    "verdict",
    "review_verdict",
    "final_verdict",
    "review_decision",
    "decision",
    "outcome",
    "review_outcome",
    "review_state",
    "github_review_state",
    "state",
)
BLOCK_FLAG_KEYS = (
    "blocked",
    "pr_blocked",
    "merge_blocked",
    "is_blocking",
    "blocking",
    "not_ready",
    "needs_fix",
    "needs_rework",
    "rejected",
    "changes_requested",
    "request_changes",
)
COMMENT_FLAG_KEYS = (
    "review_comments_posted",
    "comments_posted",
    "pr_comments_posted",
    "inline_comments_posted",
    "comment_review_submitted",
    "non_blocking_review_submitted",
)
BLOCKER_LIST_KEYS = ("blockers", "blocking_findings")
FINDING_LIST_KEYS = (
    "findings",
    "blockers",
    "blocking_findings",
    "major_concerns",
    "non_blocking_findings",
    "review_comments",
    "comments",
    "observations",
)
REVIEW_CONTAINER_KEYS = ("review", "github_review")
# Shapes reviewers actually use to record that a Chad approval was submitted. A
# GitHub review id/url only exists once a review was really submitted, so it is
# treated as submission evidence (the legacy flag-only gate left such lanes
# permanently on :thinkies:).
APPROVAL_CONTAINER_KEYS = ("approval", "approval_review", "github_review", "review")
REVIEW_ID_KEYS = ("github_review_id", "review_id", "id")
REVIEW_URL_KEYS = ("github_review_url", "review_url", "url", "html_url")
REVIEW_URL_ID_RE = re.compile(r"#pullrequestreview-(\d+)")
# Authoritative GitHub review states: an explicit non-approved value here vetoes
# approval evidence in that container.
GITHUB_STATE_KEYS = ("github_review_state", "review_state", "state", "review_decision", "review_action")
APPROVED_STATES = {"APPROVE", "APPROVED"}
SUBMITTED_FLAG_KEYS = ("submitted", "confirmed", "approval_submitted", "already_present_for_head")
APPROVED_FLAG_KEYS = ("approved", "approved_review", "chad_approved", "approval_confirmed")
# Every container that can carry a verdict, an approval or a finding list. All of
# them are validated together so a contradiction between two aliases (say
# review.verdict=NEEDS_FIX and github_review.state=APPROVED) cannot be resolved by
# whichever alias happens to be read first.
ALL_CONTAINER_KEYS = tuple(dict.fromkeys(REVIEW_CONTAINER_KEYS + APPROVAL_CONTAINER_KEYS))
# How far a known alias may be nested inside another known alias and still be
# validated (``review.approval.github_review`` is depth 3). Reviewers really do
# write ``review: {approval: {...}}``, and enumerating only the top level was a
# fail-open: the nested blocker list / non-APPROVED state was invisible and the
# lane classified :white_check_mark:. Only *known* alias keys are followed, so
# unrelated metadata is never broad-scanned.
CONTAINER_NEST_DEPTH = 3
SURFACE_LIST_KEYS = tuple(dict.fromkeys(BLOCKER_LIST_KEYS + FINDING_LIST_KEYS))
# "Prepared but never submitted to GitHub" shapes. An approval claim sitting next
# to one of these is a contradiction, not an approval.
POSTED_FLAG_KEYS = ("posted", "review_posted", "posted_to_github", "submitted_to_github")
# A bare ``state`` only means "the review's state" when it reads as review
# vocabulary; ``state: "done"`` is lane bookkeeping and must neither approve nor veto.
AMBIGUOUS_STATE_KEYS = {"state"}
REVIEW_STATE_VOCAB = {
    "APPROVE", "APPROVED", "COMMENT", "COMMENTED", "CHANGES-REQUESTED",
    "REQUEST-CHANGES", "REQUESTED-CHANGES", "DISMISSED", "PENDING", "REJECT", "REJECTED",
}
APPROVALISH_KEYS = (
    set(REVIEW_ID_KEYS) | set(REVIEW_URL_KEYS) | set(GITHUB_STATE_KEYS)
    | set(APPROVED_FLAG_KEYS) | set(SUBMITTED_FLAG_KEYS)
    | {"approval_status", "approved_by_chadtao206", "github_review_submitted",
       "github_approval_submitted", "chadtao206_approval_on_head",
       "existing_chad_approval_for_head"}
)
# Sentinel lane verdict: this run/lane is *not* safe to classify. It is never an
# emoji and never aggregates; it holds its post untouched and alerts instead.
QUARANTINE = "__quarantine__"
DEEP_WALK_DEPTH = 3
BENIGN_EMPTY_SCALARS = {"", "-", "--", "n/a", "na", "null", "nil", "none", "no", "false", "0", "[]", "{}"}


def run(cmd: list[str], *, timeout: int = 120) -> subprocess.CompletedProcess[str]:
    return subprocess.run(cmd, text=True, encoding="utf-8", capture_output=True, timeout=timeout, check=False)


def load_json(path: Path, default: Any) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return default


def save_json(path: Path, data: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(data, indent=2, sort_keys=True), encoding="utf-8")
    tmp.replace(path)


@contextlib.contextmanager
def exclusive_run_lock(path: Path | None = None):
    path = path or STATE_PATH.with_suffix(".lock")
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a+") as handle:
        try:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            yield False
            return
        try:
            yield True
        finally:
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


def kanban_json(args: list[str], *, timeout: int = 90) -> Any:
    proc = run(["hermes", "kanban", *args], timeout=timeout)
    if proc.returncode != 0:
        raise RuntimeError(proc.stderr.strip() or proc.stdout.strip() or f"hermes kanban {' '.join(args)} failed")
    return json.loads(proc.stdout)


def kanban_reviewer_lanes() -> list[dict[str, Any]]:
    """One bounded list call per tick: every non-archived reviewer lane."""
    rows = kanban_json(["list", "--assignee", "reviewer", "--json"])
    return [row for row in rows if isinstance(row, dict)] if isinstance(rows, list) else []


def kanban_show(task_id: str) -> dict[str, Any]:
    show = kanban_json(["show", task_id, "--json"])
    if not isinstance(show, dict):
        raise RuntimeError(f"unexpected kanban show payload for {task_id}")
    return show


def extract_permalinks(text: str, allowed_channels: set[str]) -> list[str]:
    out: list[str] = []
    seen: set[str] = set()
    allowed = {channel.upper() for channel in allowed_channels}
    for m in PERMALINK_RE.finditer(text or ""):
        channel = m.group("channel")
        if allowed and channel.upper() not in allowed:
            continue
        url = m.group(0).split("?")[0]
        if url not in seen:
            seen.add(url)
            out.append(url)
    return out


def _boolish(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        return value.strip().lower() in {"1", "true", "yes", "y", "approved", "submitted"}
    return bool(value)


def _flag_true(value: Any) -> bool:
    """Strict truthiness for named boolean/count flags (containers never count)."""
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return value > 0
    if isinstance(value, str):
        return value.strip().lower() in {"1", "true", "yes", "y", "submitted"}
    return False


def _text(value: Any) -> str:
    return str(value or "").strip().lower()


def _norm(value: Any) -> str:
    """UPPER, dash-separated normalization for marker matching."""
    if not isinstance(value, str):
        return ""
    return re.sub(r"[^A-Z0-9]+", "-", value.strip().upper()).strip("-")


def _first_clause(value: Any) -> str:
    """Normalized first clause of a reviewer verdict ("NEEDS_FIX; prose" -> NEEDS-FIX)."""
    if not isinstance(value, str) or not value.strip():
        return ""
    return _norm(CLAUSE_SPLIT_RE.split(value.strip(), 1)[0])


def _marker_hit(text: str, markers: tuple[str, ...]) -> str | None:
    """First token-aligned, non-negated marker in a normalized string.

    A marker must start on a token boundary, must not be preceded within
    NEGATION_WINDOW tokens by a negation ("no merge blocker", "prior_blocker"),
    and must not be immediately emptied ("blockers none").
    """
    if not text:
        return None
    tokens = text.split("-")
    for marker in markers:
        start = 0
        while True:
            index = text.find(marker, start)
            if index < 0:
                break
            prefix = text[:index]
            if index == 0 or prefix.endswith("-"):
                first = prefix.count("-")
                before = tokens[max(0, first - NEGATION_WINDOW):first]
                after = tokens[first + marker.count("-") + 1:first + marker.count("-") + 2]
                if not (set(before) & NEGATION_TOKENS) and not (set(after) & EMPTY_TOKENS):
                    return marker
            start = index + 1
    return None


def _metadata(run_row: Any) -> dict[str, Any] | None:
    if not isinstance(run_row, dict):
        return None
    md = run_row.get("metadata")
    return md if isinstance(md, dict) else None


def _enumerate_containers(
    md: dict[str, Any],
) -> tuple[list[tuple[str, dict[str, Any]]], list[str]]:
    """((label, dict) containers, labels of aliases left unread by the depth bound).

    The authoritative enumeration. Known review/approval aliases are followed
    *recursively* to ``CONTAINER_NEST_DEPTH``, so an alias nested inside another
    alias (``review.approval``, ``review.github_review``, ``approval.review``)
    cannot hide a blocker, a non-APPROVED state or an unreadable surface behind a
    top-level approval flag.

    Enumeration is breadth-first over ``ALL_CONTAINER_KEYS`` in declaration order,
    so the result order is a deterministic function of the alias tuple and not of
    ``dict`` insertion order. Only known alias keys are followed: unrelated
    metadata subtrees are never walked. Repeat/self-referential dicts are visited
    once, so a metadata cycle terminates.

    A bound has to fail closed too, or it is just a deeper version of the same
    defect: any known alias dict sitting *below* the bound is reported as unread so
    ``metadata_defects`` quarantines the run instead of approving over it.
    """
    out: list[tuple[str, dict[str, Any]]] = [("", md)]
    seen = {id(md)}
    frontier = out[:]
    for _depth in range(CONTAINER_NEST_DEPTH):
        deeper: list[tuple[str, dict[str, Any]]] = []
        for label, container in frontier:
            for key in ALL_CONTAINER_KEYS:
                nested = container.get(key)
                if not isinstance(nested, dict) or id(nested) in seen:
                    continue
                seen.add(id(nested))
                deeper.append((f"{label}{key}.", nested))
        if not deeper:
            return out, []
        out.extend(deeper)
        frontier = deeper
    unread = [
        f"{label}{key}"
        for label, container in frontier
        for key in ALL_CONTAINER_KEYS
        if isinstance(container.get(key), dict) and id(container[key]) not in seen
    ]
    return out, unread


def _labelled_containers(md: dict[str, Any]) -> list[tuple[str, dict[str, Any]]]:
    """(label, dict) for the metadata plus every known alias container beneath it.

    The one authoritative container set. Every classifier surface — blocking,
    comments, malformed/defect detection, state vetoes and approval evidence —
    consumes this, so no surface can see an alias another surface misses, and all
    aliases are validated together rather than first-hit-wins.
    """
    return _enumerate_containers(md)[0]


def _containers(md: dict[str, Any]) -> list[dict[str, Any]]:
    """The metadata dict plus every known nested review/approval container."""
    return [container for _label, container in _labelled_containers(md)]


def _benign_empty(value: Any) -> bool:
    """True when a scalar/collection carries no information at all.

    Used to tell "this surface is absent" apart from "this surface is malformed":
    ``findings: "none"`` says nothing, ``findings: "BLOCKER: unsafe"`` says a lot.
    """
    if value is None or value is False:
        return True
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return value == 0
    if isinstance(value, (list, tuple, set, dict)):
        return not value
    if isinstance(value, str):
        if value.strip().lower() in BENIGN_EMPTY_SCALARS:
            return True
        norm = _norm(value)
        return not norm or all(token in EMPTY_TOKENS for token in norm.split("-"))
    return False


def _surface_blocks(value: Any, depth: int = 0) -> bool:
    """Does an arbitrary (possibly malformed) surface assert a blocking verdict?

    A schema mistake must not hide a block, so strings, dicts and lists found where
    a list of findings was expected are all searched for a blocking marker.
    """
    if depth > DEEP_WALK_DEPTH:
        return False
    if isinstance(value, str):
        return _marker_hit(_norm(value), BLOCKING_MARKERS) is not None
    if isinstance(value, dict):
        if _finding_blocks(value):
            return True
        return any(_surface_blocks(item, depth + 1) for item in value.values())
    if isinstance(value, (list, tuple, set)):
        return any(_surface_blocks(item, depth + 1) for item in value)
    return False


def malformed_surfaces(md: dict[str, Any]) -> list[tuple[str, str]]:
    """(kind, reason) for every surface whose *shape* is not what the schema expects.

    ``kind`` is ``block``/``comment`` when the malformed value can still be read,
    and ``defect`` when it cannot. A defect fails closed (the run is quarantined);
    it is never silently dropped in favour of :white_check_mark:.
    """
    containers, unread = _enumerate_containers(md)
    out: list[tuple[str, str]] = [
        ("defect", f"{label} review alias nested deeper than {CONTAINER_NEST_DEPTH} levels (unread)")
        for label in unread
    ]
    for label, container in containers:
        for key in SURFACE_LIST_KEYS:
            if key not in container:
                continue
            value = container[key]
            if isinstance(value, list) or _benign_empty(value):
                continue
            where = f"{label}{key}"
            if isinstance(value, (int, float)) and not isinstance(value, bool):
                continue  # a positive count is an understood shape (comments posted)
            if key in BLOCKER_LIST_KEYS:
                out.append(("block", f"{where} non-list non-empty blocker surface"))
            elif _surface_blocks(value):
                out.append(("block", f"{where} non-list blocking finding surface"))
            elif isinstance(value, (str, dict, tuple)):
                out.append(("comment", f"{where} non-list non-empty finding surface"))
            else:
                out.append(("defect", f"{where} unreadable finding surface ({type(value).__name__})"))
        for key in ALL_CONTAINER_KEYS:
            if key not in container:
                continue
            value = container[key]
            if isinstance(value, dict) or _benign_empty(value):
                continue
            where = f"{label}{key}"
            if _surface_blocks(value):
                out.append(("block", f"{where} non-dict blocking review container"))
            elif isinstance(value, str) and _marker_hit(_norm(value), COMMENT_MARKERS):
                out.append(("comment", f"{where} non-dict comment review container"))
            elif isinstance(value, str) and _norm(value) in APPROVED_STATES:
                continue  # readable, but carries no signal a flag does not already carry
            else:
                out.append(("defect", f"{where} unreadable review container ({type(value).__name__})"))
    return out


def metadata_defects(md: dict[str, Any]) -> list[str]:
    """Malformed surfaces that cannot be read at all — the fail-closed trigger."""
    return [reason for kind, reason in malformed_surfaces(md) if kind == "defect"]


def _walk_dicts(value: Any, label: str = "", depth: int = 0):
    """(label, dict) for every dict in a bounded metadata subtree."""
    if depth > DEEP_WALK_DEPTH or not isinstance(value, dict):
        return
    yield label, value
    for key, item in value.items():
        if isinstance(item, dict):
            yield from _walk_dicts(item, f"{label}{key}.", depth + 1)
        elif isinstance(item, (list, tuple)):
            for index, entry in enumerate(item):
                if isinstance(entry, dict):
                    yield from _walk_dicts(entry, f"{label}{key}[{index}].", depth + 1)


def _explicitly_false(value: Any) -> bool:
    """An affirmatively negative flag (not merely absent)."""
    if value is False:
        return True
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return value == 0
    if isinstance(value, str):
        return value.strip().lower() in {"false", "no", "0", "none", "not_posted", "unposted", "skipped"}
    return False


def _review_scope(label: str, container: dict[str, Any]) -> bool:
    """Is this dict talking about a review/approval (rather than something else)?"""
    if "review" in label.lower() or "approv" in label.lower():
        return True
    return bool(set(container) & APPROVALISH_KEYS)


def approval_vetoes(md: dict[str, Any]) -> list[str]:
    """Reasons an approval claim in this metadata must not be trusted.

    Two shapes reviewers actually produce: a review that was prepared but never
    ``posted`` to GitHub, and a container whose authoritative review state is
    something other than APPROVED. An approval claim next to either of these is a
    contradiction and is quarantined, never resolved into :white_check_mark:.
    """
    vetoes: list[str] = []
    for label, container in _walk_dicts(md):
        if not _review_scope(label, container):
            continue
        for key in POSTED_FLAG_KEYS:
            if key in container and _explicitly_false(container[key]):
                vetoes.append(f"{label}{key}=false")
    for label, container in _labelled_containers(md):
        for key, clause in _container_states(container):
            if clause not in APPROVED_STATES:
                vetoes.append(f"{label}{key}={clause}")
    return sorted(set(vetoes))


def _verdict_texts(md: dict[str, Any]) -> list[tuple[str, str]]:
    """(key, normalized first clause) for every verdict-ish string field."""
    out: list[tuple[str, str]] = []
    for container in _containers(md):
        for key in VERDICT_KEYS:
            clause = _first_clause(container.get(key))
            if clause:
                out.append((key, clause))
    return out


def _list_items(container: dict[str, Any], key: str) -> list[Any]:
    """Malformed (non-list) values yield no items rather than raising."""
    value = container.get(key)
    return [item for item in value] if isinstance(value, list) else []


def _severity_text(item: Any) -> str:
    if isinstance(item, str):
        return _norm(item)
    if isinstance(item, dict):
        parts = [
            item.get(key)
            for key in ("severity", "level", "impact", "classification", "verdict")
            if isinstance(item.get(key), str)
        ]
        return _norm(" ".join(str(part) for part in parts))
    return ""


def _finding_blocks(item: Any) -> bool:
    if isinstance(item, dict) and _flag_true(item.get("blocking")):
        return True
    if isinstance(item, dict) and _flag_true(item.get("blocker")):
        return True
    severity = _severity_text(item)
    if severity and _marker_hit(severity, BLOCKING_SEVERITY_MARKERS):
        return True
    if isinstance(item, str):
        return _marker_hit(_first_clause(item), BLOCKING_MARKERS) is not None
    return False


def _no_blockers(md: dict[str, Any]) -> bool:
    """Legacy approval gate: any explicit blocker suppresses ✅ (unchanged)."""
    for key in ("blockers", "findings", "major_concerns"):
        val = md.get(key)
        if isinstance(val, list) and val:
            # Findings/major_concerns can include non-blocking caveats; only treat
            # explicit blocker severities as blocking. Plain blockers is blocking.
            if key == "blockers":
                return False
            if any(_text((item or {}).get("severity") if isinstance(item, dict) else item) == "blocker" for item in val):
                return False
    review = md.get("review") if isinstance(md.get("review"), dict) else {}
    blockers = review.get("blockers")
    if isinstance(blockers, list) and blockers:
        return False
    return True


def _positive_id(value: Any) -> bool:
    if isinstance(value, bool):
        return False
    if isinstance(value, int):
        return value > 0
    if isinstance(value, str) and value.strip().isdigit() and len(value.strip()) <= 20:
        return int(value) > 0
    return False


def _has_review_id(container: dict[str, Any]) -> bool:
    """A submitted-review id, directly or inside a #pullrequestreview- URL."""
    if any(_positive_id(container.get(key)) for key in REVIEW_ID_KEYS):
        return True
    for key in REVIEW_URL_KEYS:
        value = container.get(key)
        if isinstance(value, str) and REVIEW_URL_ID_RE.search(value):
            return True
    return False


def _container_states(container: dict[str, Any]) -> list[tuple[str, str]]:
    """(key, clause) for every key that authoritatively carries a review state."""
    out: list[tuple[str, str]] = []
    for key in GITHUB_STATE_KEYS:
        clause = _first_clause(container.get(key))
        if not clause:
            continue
        if key in AMBIGUOUS_STATE_KEYS and clause not in REVIEW_STATE_VOCAB:
            continue
        out.append((key, clause))
    return out


def _github_state(container: dict[str, Any]) -> str | None:
    states = _container_states(container)
    return states[0][1] if states else None


def run_indicates_approval_evidence(md: dict[str, Any]) -> bool:
    """Approval proven by submission evidence rather than a single boolean flag."""
    if _flag_true(md.get("approved_by_chadtao206")):
        return True
    if "SUBMITTED" in _first_clause(md.get("approval_status")).split("-"):
        return True
    approved_flag = any(_flag_true(md.get(key)) for key in APPROVED_FLAG_KEYS)
    # Same authoritative container set every other surface uses, so a nested alias
    # that carries submission evidence is read by evidence and veto detection alike.
    for container in _containers(md):
        state = _github_state(container)
        if state is not None and state not in APPROVED_STATES:
            continue  # an explicit COMMENTED/CHANGES-REQUESTED state vetoes this container
        if not _has_review_id(container):
            continue
        submitted = any(_flag_true(container.get(key)) for key in SUBMITTED_FLAG_KEYS)
        if state in APPROVED_STATES or submitted or approved_flag:
            return True
        if _first_clause(container.get("verdict")) in APPROVED_STATES:
            return True
    return False


def approval_claimed(md: dict[str, Any]) -> bool:
    """Does this metadata *claim* a submitted/existing Chad approval?

    Claim detection only. Whether the claim may be believed is decided by
    ``approval_vetoes`` and ``metadata_defects`` — see ``run_indicates_approval``.
    """
    if _boolish(md.get("github_review_submitted")) and _text(md.get("github_review_state")) == "approved":
        return True
    if _boolish(md.get("github_approval_submitted")):
        return True
    if _boolish(md.get("chadtao206_approval_on_head")) or _boolish(md.get("existing_chad_approval_for_head")):
        return True

    review = md.get("review") if isinstance(md.get("review"), dict) else {}
    if _boolish(review.get("submitted")) and _text(review.get("verdict")) in {"approve", "approved"}:
        return True
    if _boolish(review.get("existing_chad_approval_for_head")) and _text(review.get("verdict")) in {"approve", "approved"}:
        return True

    # Some reviewers record this flatter shape when they used gh review --approve.
    if _text(md.get("review_decision")) == "approved" and _boolish(md.get("approval_submitted")):
        return True
    if _flag_true(md.get("approved_review")) and _text(md.get("github_review_state")) == "approved":
        return True
    return run_indicates_approval_evidence(md)


def run_indicates_approval(run_row: dict[str, Any]) -> bool:
    """A believable clean approval: claimed, and contradicted by nothing.

    Fail closed. Any explicit blocker, any malformed surface the classifier cannot
    read, and any veto (``posted: false``, a non-APPROVED review state on any
    alias) means "not an approval" — never "approval by default".
    """
    md = run_row.get("metadata") or {}
    if not isinstance(md, dict) or not _no_blockers(md):
        return False
    # `_no_blockers` is the legacy list-shaped gate only. A malformed blocking
    # surface (`findings: "BLOCKER: unsafe"`) is invisible to it, so the full
    # blocking classifier is consulted too — otherwise this retained entry point,
    # and `task_approval` through it, would still approve a blocked PR.
    if run_indicates_block(run_row)[0]:
        return False
    if metadata_defects(md) or approval_vetoes(md):
        return False
    return approval_claimed(md)


def run_indicates_block(run_row: dict[str, Any]) -> tuple[bool, str]:
    """Boris reported the PR blocked / not ready (semantic, not a GitHub state)."""
    md = _metadata(run_row)
    if md is None:
        return False, "no metadata dict"
    for key, text in _verdict_texts(md):
        if _marker_hit(text, NONBLOCKING_MARKERS):
            continue
        marker = _marker_hit(text, BLOCKING_MARKERS)
        if marker:
            return True, f"{key}~{marker}"
    for container in _containers(md):
        for key in BLOCK_FLAG_KEYS:
            if key in container and _flag_true(container[key]):
                return True, f"flag:{key}"
        for key in BLOCKER_LIST_KEYS:
            if _list_items(container, key):
                return True, f"{key}[] non-empty"
        for key in FINDING_LIST_KEYS:
            for item in _list_items(container, key):
                if _finding_blocks(item):
                    return True, f"{key}[] blocking severity"
    for kind, reason in malformed_surfaces(md):
        if kind == "block":
            return True, reason
    return False, "no blocking signal"


def run_indicates_comments(run_row: dict[str, Any]) -> tuple[bool, str]:
    """Boris delivered non-blocking review comments / substantive findings."""
    md = _metadata(run_row)
    if md is None:
        return False, "no metadata dict"
    for key, text in _verdict_texts(md):
        marker = _marker_hit(text, COMMENT_MARKERS)
        if not marker and _marker_hit(text, NONBLOCKING_MARKERS) and _marker_hit(text, BLOCKING_MARKERS):
            marker = "NON-BLOCKING"
        if marker:
            return True, f"{key}~{marker}"
    for container in _containers(md):
        for key in COMMENT_FLAG_KEYS:
            if key in container and _flag_true(container[key]):
                return True, f"flag:{key}"
        for key in FINDING_LIST_KEYS:
            items = _list_items(container, key)
            if items:
                return True, f"{key}[] non-empty"
            if isinstance(container.get(key), (int, float)) and _flag_true(container.get(key)):
                return True, f"count:{key}"
    for kind, reason in malformed_surfaces(md):
        if kind == "comment":
            return True, reason
    return False, "no comment signal"


def classify_run_status(run_row: dict[str, Any]) -> tuple[str, str | None, str]:
    """(status, state, why) for one reviewer run. Status is the fail-closed axis.

    ``signal``      the run states a verdict; ``state`` is it.
    ``quarantine``  the run is ambiguous (unreadable surface, or an approval claim
                    contradicted by a veto). The lane holds its post and alerts.
    ``none``        the run states nothing at all.

    Order is block > defect > comments > approval. Defects are resolved before
    comments and approval because a surface the classifier cannot read may be
    hiding a block, which outranks both.
    """
    md = run_row.get("metadata") if isinstance(run_row, dict) else None
    if md is None:
        return "none", None, "no metadata dict"
    if not isinstance(md, dict):
        return "quarantine", None, f"metadata is not a dict ({type(md).__name__})"
    blocked, why = run_indicates_block(run_row)
    if blocked:
        return "signal", STATE_BLOCKED, why
    defects = metadata_defects(md)
    if defects:
        return "quarantine", None, "malformed: " + "; ".join(defects[:3])
    commented, why = run_indicates_comments(run_row)
    if commented:
        return "signal", STATE_NOTES, why
    if approval_claimed(md) and _no_blockers(md):
        vetoes = approval_vetoes(md)
        if vetoes:
            return "quarantine", None, "approval contradicted by " + ",".join(vetoes[:3])
        return "signal", STATE_APPROVED, "approval submitted/existing for head"
    return "none", None, "no reviewer signal"


def classify_run(run_row: dict[str, Any]) -> tuple[str | None, str]:
    """Map one completed reviewer run onto its semantic review state.

    Within a lane the order is block > comments > approval, matching Chad's
    aggregation precedence: a lane that approved AND delivered substantive
    non-blocking findings shows :doge_notes:, not :white_check_mark:.

    Returns ``None`` for both "nothing said" and "not safe to say"; callers that
    must tell those apart (``classify_task``) use ``classify_run_status``.
    """
    _status, emoji, why = classify_run_status(run_row)
    return emoji, why


def _epoch(value: Any) -> int:
    if isinstance(value, bool) or value is None:
        return 0
    if isinstance(value, (int, float)):
        return int(value) if value > 0 else 0
    if isinstance(value, str):
        raw = value.strip()
        if raw.isdigit():
            return int(raw)
        try:
            from datetime import datetime

            return int(datetime.fromisoformat(raw).timestamp())
        except ValueError:
            return 0
    return 0


def _run_epoch(run_row: dict[str, Any]) -> int:
    return max(_epoch(run_row.get("ended_at")), _epoch(run_row.get("completed_at")))


def row_epoch(task: dict[str, Any]) -> int:
    """Newest list-row timestamp; the cursor key for 'has this lane changed?'."""
    return max(
        _epoch(task.get("completed_at")),
        _epoch(task.get("updated_at")),
        _epoch(task.get("started_at")),
        _epoch(task.get("created_at")),
    )


def task_approval(show: dict[str, Any]) -> tuple[bool, str]:
    """Retained for compatibility: does this lane's NEWEST run prove a clean approval?

    Delegates to ``classify_task`` so the module has exactly one decision procedure.
    The baseline scanned every run and returned the first approval found anywhere,
    which is the fail-open fallback the review rejected: a newer blocking or
    unreadable run must win here too, not just on the reaction path.
    """
    emoji, why = classify_task(show)
    return emoji == STATE_APPROVED, why


def classify_task(show: dict[str, Any]) -> tuple[str | None, str]:
    """The newest reviewer run decides the lane, and only it may say ✅.

    Fail closed on newer-run ambiguity: if the newest reviewer run is quarantined
    the lane is quarantined, and if the newest reviewer run says nothing at all the
    lane does **not** inherit an older approval or comment verdict — that older
    verdict could be hiding whatever the unreadable newer run actually decided.
    An older ``review-no`` is still adopted: it is the top of the precedence order,
    so nothing it could be hiding outranks it.

    Returns ``QUARANTINE`` as the state when the lane is unsafe to classify.
    """
    runs = show.get("runs") if isinstance(show, dict) else None
    if not isinstance(runs, list):
        return None, "no runs"
    ordered = sorted(
        (r for r in runs if isinstance(r, dict)),
        key=_run_epoch,
        reverse=True,
    )
    unclassified: list[str] = []
    for run_row in ordered:
        if "profile" in run_row and run_row.get("profile") not in (None, "reviewer"):
            continue
        status, emoji, why = classify_run_status(run_row)
        if status == "quarantine":
            return QUARANTINE, f"run={run_row.get('id')} {why}"
        if status == "none":
            unclassified.append(str(run_row.get("id")))
            continue
        if unclassified and emoji != STATE_BLOCKED:
            return QUARANTINE, (
                f"run={run_row.get('id')} {emoji} is behind unclassified newer "
                f"run(s) {','.join(unclassified[:3])}"
            )
        return emoji, f"run={run_row.get('id')} {why}"
    return None, "no classifiable reviewer run metadata"


def _jev_shadow(task_id: str, show: dict[str, Any], lane_state: str | None, why: str) -> None:
    """Shadow-log a Jev judgment of the newest reviewer run; JEV_SHADOW=1 only, never decides."""
    if os.environ.get("JEV_SHADOW") != "1":
        return
    try:
        import jev_judge

        runs = [r for r in (show.get("runs") or []) if isinstance(r, dict)
                and r.get("profile") in (None, "reviewer")]
        jev_judge.shadow(task_id, max(runs, key=_run_epoch, default=None), lane_state, why)
    except Exception:
        pass


def lane_status_kind(status: Any) -> str:
    value = _text(status)
    if value in ACTIVE_STATUSES:
        return "active"
    if value in DONE_STATUSES:
        return "done"
    return "inert"


def source_text(show: dict[str, Any]) -> str:
    task = show.get("task") or {}
    comments = show.get("comments") or []
    runs = show.get("runs") or []
    parts = [task.get("title") or "", task.get("body") or "", task.get("result") or ""]
    parts += [str((c or {}).get("body") or "") for c in comments]
    for r in runs:
        md = r.get("metadata") or {}
        if isinstance(md, dict):
            for key in ("source_slack_permalink", "slack_permalink", "source_permalink"):
                if md.get(key):
                    parts.append(str(md[key]))
    return "\n".join(parts)


def aggregate_state(states: Iterable[str]) -> str | None:
    normalized = [normalize_review_state(state) for state in states]
    if not normalized or any(state is None for state in normalized):
        return None
    return max(normalized, key=lambda state: STATE_PRECEDENCE[state])


# --- reaction plumbing ----------------------------------------------------


def slack_reaction(permalink: str, emoji: str, remove: bool, *, timeout: int = 60) -> dict[str, Any]:
    """Add/remove one Son of Chad reaction via slack_react.py (subprocess).

    Fails closed on the helper's *result*, not just its exit code: success requires
    a parsed JSON object that explicitly says ``{"ok": true}``, except that an
    explicit ``no_reaction`` or ``not_reacted`` removal outcome also proves the
    ensure-absent postcondition. The helper reports ``not_reacted`` with its
    dedicated semantic exit code 3; no other nonzero result is accepted. An rc=0
    run that printed nothing, printed non-JSON, or printed a non-object proves
    nothing about the post, so it is reported as a retryable failure instead of
    being believed.
    """
    cmd = [str(SLACK_REACT), permalink, "--emoji", emoji]
    if remove:
        cmd.append("--remove")
    try:
        proc = run(cmd, timeout=timeout)
    except subprocess.SubprocessError as exc:
        return {"ok": False, "error": f"slack_react invocation failed: {exc}", "raw": ""}
    raw = ((proc.stdout or "").strip() or (proc.stderr or "").strip())[-500:]
    payload: Any = None
    try:
        payload = json.loads(raw) if raw else None
    except ValueError:
        payload = None
    if not isinstance(payload, dict):
        return {
            "ok": False,
            "error": f"slack_react rc={proc.returncode} produced no valid JSON result: {raw!r}"[-300:],
            "already_reacted": False,
            "no_reaction": False,
            "raw": raw,
        }
    error = payload.get("error")
    already_absent = remove and (
        (proc.returncode == 0 and error in ("no_reaction", "not_reacted"))
        or (
            proc.returncode == 3
            and payload.get("ok") is False
            and error == "not_reacted"
        )
    )
    ok = (proc.returncode == 0 and payload.get("ok") is True) or already_absent
    return {
        "ok": bool(ok),
        "error": (
            None
            if already_absent
            else payload.get("error") or (None if ok else f"slack_react rc={proc.returncode}")
        ),
        "already_reacted": bool(payload.get("already_reacted")),
        "no_reaction": bool(payload.get("no_reaction") or already_absent),
        "raw": raw,
    }


def apply_transition(
    permalink: str,
    desired: str,
    record: dict[str, Any] | None,
    reactor: Callable[[str, str, bool], dict[str, Any]],
    owned: tuple[str, ...],
) -> dict[str, Any]:
    """Add the desired reaction, then drop stale owned ones. Never bare-first.

    An unknown prior state (fresh install, migrated legacy state, lost state
    file) sweeps every other owned emoji exactly once so the post converges to
    exactly one reaction. A failed add leaves the prior reaction untouched.
    """
    prior = record.get("emoji") if isinstance(record, dict) else None
    pending = [
        reaction
        for reaction in ((record or {}).get("pending_removals") or [])
        if reaction in owned
    ]
    calls = 0
    add = reactor(permalink, desired, False)
    calls += 1
    if not add.get("ok"):
        return {
            "ok": False,
            "emoji": desired,
            "prior": prior,
            "error": add.get("error") or add.get("raw") or "reaction add failed",
            "calls": calls,
            "pending_removals": pending,
        }

    stale = set(pending)
    if prior in owned:
        stale.add(prior)
    else:
        stale.update(owned)  # unknown prior: converge to exactly one
    stale.discard(desired)

    removed: list[str] = []
    still_pending: list[str] = []
    for emoji in owned:
        if emoji not in stale:
            continue
        resp = reactor(permalink, emoji, True)
        calls += 1
        if resp.get("ok"):
            removed.append(emoji)
        else:
            still_pending.append(emoji)
    return {
        "ok": True,
        "emoji": desired,
        "prior": prior,
        "already_reacted": bool(add.get("already_reacted")),
        "removed": removed,
        "pending_removals": still_pending,
        "calls": calls,
    }


def schedule_reconcile(record: dict[str, Any], now: int, sweeps: int = 0) -> None:
    """Arm (or disarm) the next bounded repair sweep on a converged post.

    The schedule is an explicit absolute deadline in the persisted record rather
    than an offset from ``applied_at``, so it survives restarts exactly once and so
    records this process never transitioned (legacy state migrated from the
    approval-only watchdog) are never swept — they have no writer race to repair.
    """
    record["reconcile_sweeps"] = sweeps
    if sweeps < len(RECONCILE_DELAYS_SECONDS):
        record["reconcile_due_at"] = now + RECONCILE_DELAYS_SECONDS[sweeps]
    else:
        record.pop("reconcile_due_at", None)


def reconcile_due(record: dict[str, Any] | None, now: int) -> bool:
    """Is a bounded post-convergence repair sweep due for this post?

    Sweeps are armed by ``schedule_reconcile`` on each transition and capped at
    ``len(RECONCILE_DELAYS_SECONDS)``, so a settled post goes back to costing zero
    Slack calls. A post with no armed deadline is never swept.
    """
    if record_state(record) is None:
        return False
    sweeps = record.get("reconcile_sweeps")
    sweeps = sweeps if isinstance(sweeps, int) and sweeps > 0 else 0
    if sweeps >= len(RECONCILE_DELAYS_SECONDS):
        return False
    due_at = _epoch(record.get("reconcile_due_at"))
    return bool(due_at) and now >= due_at


def reconcile_sweep(
    permalink: str,
    desired: str,
    reactor: Callable[[str, str, bool], dict[str, Any]],
    owned: tuple[str, ...],
) -> dict[str, Any]:
    """Blindly remove every owned emoji except ``desired``.

    This is the repair half of the race-safe handoff with the best-effort prompt-side
    writer: local state cannot see a reaction someone else added after this watchdog
    converged, so the sweep asserts the desired end state instead of trusting state.
    ``no_reaction: true`` (nothing was there) is the normal, quiet outcome; anything
    actually removed is a contradiction that had to be repaired.
    """
    calls = 0
    removed: list[str] = []
    pending: list[str] = []
    for emoji in owned:
        if emoji == desired:
            continue
        resp = reactor(permalink, emoji, True)
        calls += 1
        if not resp.get("ok"):
            pending.append(emoji)
        elif not resp.get("no_reaction"):
            removed.append(emoji)
    return {"ok": not pending, "calls": calls, "removed": removed, "pending_removals": pending}


def _reconcile_converged(
    permalink: str,
    desired: str,
    record: dict[str, Any],
    now: int,
    args: argparse.Namespace,
    reactor: Callable[[str, str, bool], dict[str, Any]],
    outputs: list[str],
    metrics: dict[str, int],
) -> None:
    """Run the bounded repair sweep on an already-converged post, if one is due.

    This is the watchdog half of the race-safe handoff. Boris intake is a
    best-effort writer that may only ever *add* an initial ``rubbing``; that add can
    land after this watchdog converged the post, and local state can never see it.
    Rather than trusting state, each transition schedules a bounded number of blind
    sweeps that re-assert "exactly ``desired``". After the last scheduled sweep a
    settled post is back to costing zero Slack calls.
    """
    if not reconcile_due(record, now):
        return
    sweeps = record.get("reconcile_sweeps")
    sweeps = sweeps if isinstance(sweeps, int) and sweeps > 0 else 0
    if args.dry_run:
        outputs.append(
            f"WOULD_RECONCILE source={permalink} desired={desired} sweep={sweeps + 1}"
        )
        return
    result = reconcile_sweep(permalink, desired, reactor, owned_reactions(permalink))
    metrics["reaction_calls"] += int(result.get("calls") or 0)
    metrics["reconcile_sweeps"] += 1
    record["reconciled_at"] = now
    schedule_reconcile(record, now, sweeps + 1)
    removed = result.get("removed") or []
    if removed:
        # Something else wrote to this post after we converged it.
        metrics["reconcile_repairs"] += 1
        outputs.append(
            f"JUST_DO_IT_REVIEW_REACTIONS reconciled source={permalink} "
            f"desired={desired} removed_contradiction={','.join(removed)}"
        )
    if result.get("pending_removals"):
        # Leave the failed removals as ordinary repair work: the next tick takes the
        # normal transition path, which re-asserts `desired` and retries them.
        record["pending_removals"] = list(result["pending_removals"])
        outputs.append(
            f"JUST_DO_IT_REVIEW_REACTIONS reconcile_remove_failed source={permalink} "
            f"stale={','.join(result['pending_removals'])} (retries next tick)"
        )


# --- state ---------------------------------------------------------------


def record_state(record: object) -> str | None:
    if not isinstance(record, dict):
        return None
    return normalize_review_state(record.get("state") or record.get("emoji"))


def lane_states(record: object) -> dict[str, str]:
    lanes = record.get("lanes") if isinstance(record, dict) else None
    return {
        str(task_id): state
        for task_id, raw in (lanes.items() if isinstance(lanes, dict) else [])
        if (state := normalize_review_state(raw)) is not None
    }


def unknown_lane_values(record: object) -> dict[str, object]:
    lanes = record.get("lanes") if isinstance(record, dict) else None
    return {
        str(task_id): raw
        for task_id, raw in (lanes.items() if isinstance(lanes, dict) else [])
        if normalize_review_state(raw) is None
    }


def matches_selected_tasks(
    record: object,
    selected_tasks: set[str] | None,
    *,
    extra_task_ids: Iterable[str] = (),
) -> bool:
    """Whether exact, trustworthy lane provenance intersects a task selector."""
    if not selected_tasks:
        return True
    provenance = {str(task_id) for task_id in extra_task_ids}
    if isinstance(record, dict):
        for field in ("lanes", "unresolved_lanes", "cleared_lanes"):
            task_map = record.get(field)
            if isinstance(task_map, dict):
                provenance.update(str(task_id) for task_id in task_map)
    return not selected_tasks.isdisjoint(provenance)


def state_has_selected_task_provenance(
    state: object,
    selected_tasks: set[str],
    allowed_channels: set[str],
) -> bool:
    """Whether persisted post or legacy-source provenance enters this task scope."""
    if not selected_tasks or not isinstance(state, dict):
        return not selected_tasks
    posts = state.get("posts")
    for permalink, record in (posts.items() if isinstance(posts, dict) else []):
        if (source_channel(str(permalink)) in allowed_channels
                and matches_selected_tasks(record, selected_tasks)):
            return True
    reacted = state.get("reacted")
    for key in (reacted if isinstance(reacted, dict) else {}):
        if not isinstance(key, str) or ":" not in key:
            continue
        task_id, permalink = key.split(":", 1)
        if (task_id in selected_tasks and PERMALINK_RE.fullmatch(permalink)
                and source_channel(permalink) in allowed_channels):
            return True
    return False


def _new_state(now: int, *, backfill: bool = False) -> dict[str, Any]:
    return {
        "schema_version": STATE_SCHEMA_VERSION,
        "baseline_completed_at": 0 if backfill else now,
        "machine_baseline_at": 0 if backfill else now,
        "posts": {},
        "reacted": {},
        "failures": {},
        "cursor": {"seen": {}, "attempts": {}, "last_list_at": 0},
    }


def migrate_state(
    raw: Any,
    now: int,
    *,
    backfill: bool = False,
    allowed_channels: set[str] | None = None,
    selected_tasks: set[str] | None = None,
) -> tuple[dict[str, Any], list[str]]:
    """Upgrade legacy ✅-only state in place; never lose or replay history.

    Legacy `reacted` entries become known :white_check_mark: post states so a
    restart neither re-adds nor contradicts them, and the legacy keys are left
    intact so a rollback to the approval-only watchdog still reads its own state.
    """
    notes: list[str] = []
    if not isinstance(raw, dict):
        return _new_state(now, backfill=backfill), (["initialized fresh state"] if raw is not None else [])

    state = dict(raw)
    version = state.get("schema_version")
    state.setdefault("reacted", {})
    state.setdefault("failures", {})
    if not isinstance(state["reacted"], dict):
        state["reacted"] = {}
    if not isinstance(state["failures"], dict):
        state["failures"] = {}
    if not isinstance(state.get("posts"), dict):
        state["posts"] = {}
    cursor = state.get("cursor")
    if not isinstance(cursor, dict):
        cursor = {}
    cursor.setdefault("seen", {})
    cursor.setdefault("attempts", {})
    cursor.setdefault("last_list_at", 0)
    if not isinstance(cursor["seen"], dict):
        cursor["seen"] = {}
    if not isinstance(cursor["attempts"], dict):
        cursor["attempts"] = {}
    state["cursor"] = cursor

    if version != STATE_SCHEMA_VERSION:
        migrated = 0
        deferred = 0
        completed_raw = state.get("legacy_reacted_completed")
        completed = {
            key for key in completed_raw if isinstance(key, str)
        } if isinstance(completed_raw, list) else set()
        for key, entry in state["reacted"].items():
            if not isinstance(key, str) or ":" not in key:
                continue
            task_id, permalink = key.split(":", 1)
            if not PERMALINK_RE.fullmatch(permalink):
                continue
            if ((allowed_channels is not None
                 and source_channel(permalink) not in allowed_channels)
                    or (selected_tasks and task_id not in selected_tasks)):
                deferred += 1
                continue
            if key in completed:
                continue
            record = state["posts"].get(permalink)
            if record_state(record) is not None:
                completed.add(key)
                continue
            at = _epoch((entry or {}).get("at")) if isinstance(entry, dict) else 0
            state["posts"][permalink] = {
                "emoji": EMOJI_APPROVED,
                "applied_at": at or now,
                "pending_removals": [],
                "lanes": {task_id: EMOJI_APPROVED},
                "source": "legacy_reacted",
            }
            completed.add(key)
            migrated += 1
        if completed:
            state["legacy_reacted_completed"] = sorted(completed)
        # New reaction states must not backfill history: only lanes that change
        # after this migration are eligible.
        if "machine_baseline_at" not in state:
            state["machine_baseline_at"] = 0 if backfill else now
        if deferred:
            notes.append(
                f"deferred legacy migration: posts_seeded={migrated} "
                f"posts_deferred={deferred} machine_baseline_at={state['machine_baseline_at']}"
            )
        else:
            state["schema_version"] = STATE_SCHEMA_VERSION
            notes.append(
                f"migrated legacy state: posts_seeded={migrated} "
                f"machine_baseline_at={state['machine_baseline_at']}"
            )

    if not isinstance(state.get("baseline_completed_at"), int):
        state["baseline_completed_at"] = 0 if backfill else now
    if not isinstance(state.get("machine_baseline_at"), int):
        state["machine_baseline_at"] = 0 if backfill else now

    # Drop unusable post records rather than acting on them. A record with no
    # applied emoji is kept only while it still carries repair intent.
    for permalink in list(state["posts"]):
        if allowed_channels is not None and source_channel(permalink) not in allowed_channels:
            continue
        record = state["posts"][permalink]
        if not matches_selected_tasks(record, selected_tasks):
            continue
        if not isinstance(record, dict):
            state["posts"].pop(permalink, None)
            continue
        lanes = record.get("lanes")
        record["lanes"] = {
            str(task): raw_state
            for task, raw_state in (lanes.items() if isinstance(lanes, dict) else [])
        }
        unknown_lanes = unknown_lane_values(record)
        if record_state(record) is None:
            record["emoji"] = None
            if not (normalize_review_state(record.get("want")) is not None or record.get("last_error")
                    or record.get("pending_removals") or record.get("deferred_at")
                    or record.get("unresolved_lanes") or record.get("cleared_lanes")
                    or unknown_lanes):
                state["posts"].pop(permalink, None)
                continue
        record["pending_removals"] = [
            reaction
            for reaction in (record.get("pending_removals") or [])
            if reaction in owned_reactions(permalink)
        ]
        record["cleared_lanes"] = {
            str(task): _epoch(at)
            for task, at in ((record.get("cleared_lanes") or {}).items()
                             if isinstance(record.get("cleared_lanes"), dict) else [])
        }
        if not record["cleared_lanes"]:
            record.pop("cleared_lanes", None)
        # A hold must survive a restart, so its provenance is normalized rather
        # than dropped; an unreadable value still counts as "held, reason lost".
        unresolved = record.get("unresolved_lanes")
        if unresolved is not None:
            record["unresolved_lanes"] = {
                str(task): str(why)
                for task, why in (unresolved.items() if isinstance(unresolved, dict) else [])
            } or ({"unknown": "unreadable hold provenance"} if unresolved else {})
            if not record["unresolved_lanes"]:
                record.pop("unresolved_lanes", None)
        # Fail closed on a hold whose provenance is missing entirely. Every hold this
        # build records names its lanes, so this only fires on state written before
        # unshowable holds carried provenance (or on a hand-edited record): the post
        # was held, the reason is lost, and releasing it on our own authority is the
        # exact defect this normalization exists to prevent. It stays held until an
        # operator uses --clear-unresolved. Version-independent on purpose — it must
        # also protect a record whose schema_version already reads as current.
        if record.get("deferred_at") and not record.get("unresolved_lanes"):
            record["unresolved_lanes"] = {
                "unknown": "hold persisted without lane provenance (pre-provenance state)"
            }
        for task_id, raw_state in unknown_lanes.items():
            if task_id in record.get("cleared_lanes", {}):
                continue
            record.setdefault("unresolved_lanes", {}).setdefault(
                task_id,
                hold_provenance(
                    HOLD_KIND_UNKNOWN,
                    f"unknown persisted lane state: {raw_state!r}",
                ),
            )
    return state, notes


def compact_state(
    state: dict[str, Any],
    now: int,
    *,
    allowed_channels: set[str] | None = None,
    selected_tasks: set[str] | None = None,
) -> None:
    """Keep the cursor and post registry bounded across long uptimes."""
    cursor = state["cursor"]
    cursor["seen"] = {
        task_id: epoch
        for task_id, epoch in cursor["seen"].items()
        if ((selected_tasks and str(task_id) not in selected_tasks)
            or (isinstance(epoch, int) and now - epoch <= SEEN_RETENTION_SECONDS))
    }
    cursor["attempts"] = {
        task_id: attempt
        for task_id, attempt in cursor["attempts"].items()
        if ((selected_tasks and str(task_id) not in selected_tasks)
            or (isinstance(attempt, dict)
                and now - _epoch(attempt.get("last_at")) <= SEEN_RETENTION_SECONDS))
    }
    for permalink, record in list(state["posts"].items()):
        if allowed_channels is not None and source_channel(permalink) not in allowed_channels:
            continue
        if not matches_selected_tasks(record, selected_tasks):
            continue
        applied = _epoch(record.get("applied_at"))
        # A held post is never compacted away: dropping it would silently release
        # the hold and let the post transition on its remaining lanes.
        converged = not (
            record.get("pending_removals") or record.get("last_error")
            or record.get("unresolved_lanes") or record.get("deferred_at")
        )
        if converged and applied and now - applied > POST_RETENTION_SECONDS:
            state["posts"].pop(permalink, None)


def _retry_due(attempt: Any, now: int) -> bool:
    if not isinstance(attempt, dict):
        return True
    count = attempt.get("count")
    count = count if isinstance(count, int) and count > 0 else 0
    backoff = RETRY_BACKOFF_SECONDS[min(count, len(RETRY_BACKOFF_SECONDS) - 1)]
    return now - _epoch(attempt.get("last_at")) >= backoff


def _note_attempt(bucket: dict[str, Any], key: str, now: int) -> None:
    attempt = bucket.get(key)
    count = attempt.get("count", 0) if isinstance(attempt, dict) else 0
    alerted_at = attempt.get("alerted_at") if isinstance(attempt, dict) else None
    entry = {"count": (count if isinstance(count, int) else 0) + 1, "last_at": now}
    if alerted_at:
        entry["alerted_at"] = alerted_at  # keep the alert throttle across retries
    bucket[key] = entry


def hold_provenance(kind: str, why: Any) -> str:
    """One persisted ``unresolved_lanes`` value: which kind of hold, and why.

    Provenance is recorded for *every* unresolved lane, not just quarantined ones.
    A lane whose ``kanban show`` failed or was deferred is exactly as capable of
    hiding a ``review-no`` as one that was read and quarantined, and it is the one
    that disappears from later ticks (lookback boundary, archival, list omission) —
    so it is the one whose hold has to live in the post record.
    """
    return f"{kind}: {str(why)[-300:]}"


def hold_kind(why: Any) -> str:
    """The kind tag of a persisted provenance string.

    Anything unrecognized (legacy provenance written before holds were tagged, or a
    value normalized out of corrupt state) is ``unknown``, which is treated as
    strictly as a quarantine: reported immediately, released only by reclassification
    or ``--clear-unresolved``.
    """
    head = str(why or "").split(":", 1)[0].strip().lower()
    return head if head in HOLD_KINDS else HOLD_KIND_UNKNOWN


def hold_report(unresolved: dict[str, str]) -> tuple[str, int]:
    """(reason label, alert grace seconds) for a set of holds on one post.

    The label names the strictest kind holding the post, and only an all-``unshowable``
    hold gets the grace window — one quarantined or provenance-less lane in the set is
    reported at once.
    """
    kinds = {hold_kind(why) for why in unresolved.values()}
    for kind in HOLD_KIND_STRICTNESS:
        if kind in kinds:
            grace = HOLD_ALERT_GRACE_SECONDS if kind == HOLD_KIND_UNSHOWABLE else 0
            return HOLD_REASON_BY_KIND[kind], grace
    return HOLD_REASON_BY_KIND[HOLD_KIND_UNKNOWN], 0


def _hold_links(
    bucket: dict[str, dict[str, str]],
    links: list[str],
    task_id: str,
    kind: str,
    why: Any,
) -> None:
    """Record one lane's hold, with provenance, against every post it cites."""
    provenance = hold_provenance(kind, why)
    for link in links:
        bucket.setdefault(link, {})[task_id] = provenance


def _alert_due(record: Any, now: int, interval: int = UNRESOLVED_ALERT_SECONDS) -> bool:
    """Throttle for the repeated 'still unresolved' alert on a held lane/post.

    An unresolved lane or post is never given up on, so its alert would otherwise
    repeat every tick forever. It fires once, then once per ``interval``.
    """
    if not isinstance(record, dict):
        return True
    last = _epoch(record.get("alerted_at"))
    return not last or now - last >= interval


# --- tick ----------------------------------------------------------------


def process_tick(
    args: argparse.Namespace,
    now: int,
    state: dict[str, Any],
    *,
    lister: Callable[[], list[dict[str, Any]]],
    shower: Callable[[str], dict[str, Any]],
    reactor: Callable[[str, str, bool], dict[str, Any]],
) -> tuple[int, list[str], dict[str, int]]:
    """One bounded discovery + aggregation + transition pass."""
    outputs: list[str] = []
    metrics = {
        "lane_list_calls": 0,
        "task_show_calls": 0,
        "shows_deferred": 0,
        "reaction_calls": 0,
        "posts_transitioned": 0,
        "posts_deferred": 0,
        "posts_held": 0,
        "posts_converged": 0,
        "reconcile_sweeps": 0,
        "reconcile_repairs": 0,
    }
    hard_failed = False
    allowed_channels = set(args.channels or SCHEDULED_DEFAULT_CHANNELS)
    only_tasks = set(args.only_task or [])
    forced = bool(only_tasks or args.backfill_existing)
    baseline = 0 if forced else int(state.get("machine_baseline_at") or 0)
    cursor = state["cursor"]
    seen: dict[str, Any] = cursor["seen"]
    attempts: dict[str, Any] = cursor["attempts"]
    posts: dict[str, Any] = state["posts"]
    max_shows = int(getattr(args, "max_shows", MAX_SHOWS_PER_TICK) or MAX_SHOWS_PER_TICK)
    cleared_posts: set[str] = set()

    def is_selected_permalink(
        permalink: str, *, extra_task_ids: Iterable[str] = ()
    ) -> bool:
        return (
            source_channel(permalink) in allowed_channels
            and matches_selected_tasks(
                posts.get(permalink), only_tasks, extra_task_ids=extra_task_ids
            )
        )

    # Operator escape hatch: explicitly release a hold an operator has classified by
    # hand. This is the only way a hold ends without the lane reclassifying, and it
    # is deliberately manual — it is never reached from a cron tick.
    #
    # The released lane ids are remembered in `cleared_lanes`, because clearing the
    # record alone is not enough: an unshowable lane is re-deferred later in this very
    # tick (and on every tick until it ages out), which would re-create the hold and
    # make the override a no-op. The clearance suppresses only holds that carry no new
    # information — a lane that is read again and quarantines again consumes it.
    for permalink in set(getattr(args, "clear_unresolved", None) or []):
        if not is_selected_permalink(permalink):
            continue
        record = posts.get(permalink)
        held = isinstance(record, dict) and (record.get("unresolved_lanes") or record.get("deferred_at"))
        if isinstance(record, dict) and held:
            released = sorted(record.get("unresolved_lanes") or {})
            cleared = {str(task): now for task in released}
            record["cleared_lanes"] = {**(record.get("cleared_lanes") or {}), **cleared}
            record.pop("unresolved_lanes", None)
            record.pop("alerted_at", None)
            record.pop("deferred_at", None)
            cleared_posts.add(permalink)
            outputs.append(
                f"JUST_DO_IT_REVIEW_REACTIONS hold_cleared source={permalink} "
                f"lanes={','.join(released) or 'unknown'} (operator override)"
            )
        else:
            outputs.append(
                f"JUST_DO_IT_REVIEW_REACTIONS hold_clear_noop source={permalink} (no hold recorded)"
            )

    try:
        rows = lister()
        metrics["lane_list_calls"] += 1
    except Exception as exc:
        outputs.append(f"JUST_DO_IT_REVIEW_REACTIONS kanban_list_failed: {str(exc)[-500:]}")
        return 1, outputs, metrics
    fresh: dict[str, dict[str, dict[str, str]]] = {}
    queue: list[tuple[int, str, dict[str, Any], list[str]]] = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        task_id = str(row.get("id") or row.get("task_id") or "")
        if not task_id or (only_tasks and task_id not in only_tasks):
            continue
        lifecycle_watermark = _epoch(
            getattr(args, "lifecycle_rollout_watermark", 0)
        )
        if (
            lifecycle_watermark
            and row.get("created_by") == LIFECYCLE_CREATED_BY
            and _epoch(row.get("created_at")) >= lifecycle_watermark
        ):
            continue
        if row.get("assignee") != "reviewer":
            continue
        kind = lane_status_kind(row.get("status"))
        if kind == "inert":
            continue
        links = extract_permalinks(str(row.get("body") or ""), allowed_channels)
        if not links:
            continue  # source-less lane: never react
        if kind == "active":
            if baseline and row_epoch(row) <= baseline:
                continue
            for link in links:
                fresh.setdefault(link, {})[task_id] = {
                    "state": STATE_ACTIVE,
                    "reason": f"active lane status={row.get('status')}",
                }
            continue
        completed = _epoch(row.get("completed_at")) or row_epoch(row)
        if not forced:
            if baseline and completed <= baseline:
                continue
            if args.lookback_seconds and completed and now - completed > args.lookback_seconds:
                continue
        queue.append((completed, task_id, row, links))

    # Bounded `kanban show`: newest completions first, only lanes whose row moved
    # since the cursor last classified them.
    queue.sort(key=lambda item: item[0], reverse=True)
    # permalink -> {task_id: provenance} for lanes that could not be read this tick
    # (show failed, retry backoff, or per-tick cap). These holds are persisted rather
    # than kept only in this tick's locals: the lane may never become showable, and
    # once its row ages past --lookback-seconds (or drops out of the lane list) it
    # stops being rediscovered, which used to release the post and let a sibling
    # lane's lower-precedence verdict transition it.
    unshowable: dict[str, dict[str, str]] = {}
    # permalink -> {task_id: provenance} for lanes read this tick but unsafe to classify.
    quarantined: dict[str, dict[str, str]] = {}
    # permalink -> {task_id} for lanes read AND safely classified this tick. These are
    # the only lanes that release their own hold; "the lane said nothing" counts, since
    # the lane was read successfully.
    resolved: dict[str, set[str]] = {}
    selected: list[tuple[str, list[str]]] = []
    for _completed, task_id, row, links in queue:
        if not forced and task_id in seen and row_epoch(row) <= _epoch(seen.get(task_id)):
            continue  # already classified at this revision; posts[] holds the verdict
        attempt = attempts.get(task_id)
        count = attempt.get("count", 0) if isinstance(attempt, dict) else 0
        count = count if isinstance(count, int) else 0
        if count >= MAX_SHOW_ATTEMPTS:
            # Retry exhaustion is an alert, never a give-up. A completed lane that
            # has never been classified can be hiding a `review-no`, so its post
            # keeps deferring and an operator is told; the lane is NOT marked seen
            # and keeps retrying at the backoff ceiling so it can still self-heal.
            if _alert_due(attempt, now):
                attempts[task_id] = {**(attempt if isinstance(attempt, dict) else {}),
                                     "alerted_at": now}
                outputs.append(
                    f"JUST_DO_IT_REVIEW_REACTIONS show_unresolved task={task_id} "
                    f"attempts={count} posts_held={len(links)} "
                    f"(post is held, not transitioned)"
                )
                hard_failed = True
        if not _retry_due(attempt, now):
            _hold_links(unshowable, links, task_id,
                        HOLD_KIND_UNSHOWABLE, f"show retry backoff (attempts={count})")
            metrics["shows_deferred"] += 1
            continue
        if len(selected) >= max_shows:
            _hold_links(unshowable, links, task_id,
                        HOLD_KIND_UNSHOWABLE, f"show cap reached this tick (max_shows={max_shows})")
            metrics["shows_deferred"] += 1
            continue
        selected.append((task_id, links))
        metrics["task_show_calls"] += 1
        try:
            show = shower(task_id)
        except Exception as exc:
            _note_attempt(attempts, task_id, now)
            _hold_links(unshowable, links, task_id,
                        HOLD_KIND_UNSHOWABLE, f"show failed: {str(exc)[-160:]}")
            outputs.append(f"JUST_DO_IT_REVIEW_REACTIONS show_failed task={task_id}: {str(exc)[-300:]}")
            hard_failed = True
            continue
        attempts.pop(task_id, None)
        seen[task_id] = row_epoch(row)
        lane_state, why = classify_task(show)
        _jev_shadow(task_id, show, lane_state, why)
        # Metadata/comment provenance also counts for lanes we already fetched
        # (legacy reach), unioned with the body links that queued the lane.
        show_links = links + [
            link for link in extract_permalinks(source_text(show), allowed_channels) if link not in links
        ]
        if lane_state == QUARANTINE:
            # The lane was read but is not safe to classify, and an unsafe lane can
            # be hiding a `review-no`. Hold every post it cites and persist the
            # provenance, so the hold survives ticks and restarts even though the
            # lane is now `seen` and will not be re-shown until its row moves.
            for link in show_links:
                fresh.setdefault(link, {})
            _hold_links(quarantined, show_links, task_id, HOLD_KIND_QUARANTINE, why)
            continue
        if lane_state is None:
            # Read successfully, and it says nothing. That is a resolution: the lane
            # is not hiding anything, so it releases any hold it was holding.
            for link in show_links:
                fresh.setdefault(link, {})
                resolved.setdefault(link, set()).add(task_id)
            continue
        for link in show_links:
            fresh.setdefault(link, {})[task_id] = {"state": lane_state, "reason": why}
            resolved.setdefault(link, set()).add(task_id)

    # Evaluate posts with new information, plus any post still owing repair work or
    # a due post-convergence reconciliation sweep.
    candidates = set(fresh) | set(quarantined) | set(unshowable) | cleared_posts
    for permalink, record in posts.items():
        if not isinstance(record, dict):
            continue
        if not is_selected_permalink(permalink):
            continue
        if (record.get("pending_removals") or record.get("last_error")
                or record.get("want") or record.get("deferred_at")
                or record.get("unresolved_lanes") or reconcile_due(record, now)):
            candidates.add(permalink)

    def discovered_task_ids(permalink: str) -> set[str]:
        return (
            set(fresh.get(permalink) or {})
            | set(quarantined.get(permalink) or {})
            | set(unshowable.get(permalink) or {})
            | set(resolved.get(permalink) or ())
        )

    selected_candidates = {
        permalink
        for permalink in candidates
        if is_selected_permalink(
            permalink, extra_task_ids=discovered_task_ids(permalink)
        )
    }

    if selected_candidates:
        cursor["last_list_at"] = now

    for permalink in sorted(selected_candidates):
        record = posts.get(permalink) if isinstance(posts.get(permalink), dict) else None
        semantic_lanes = lane_states(record)
        unknown_lanes = unknown_lane_values(record)
        for task_id, contribution in (fresh.get(permalink) or {}).items():
            semantic_lanes[task_id] = contribution["state"]
        is_boris = source_channel(permalink) == BORIS_CHANNEL
        # Persisted hold provenance, refreshed with anything found this tick. The
        # persisted set is authoritative: a held lane that is no longer rediscovered
        # (aged past --lookback-seconds, archived, or simply absent from this tick's
        # list) keeps holding its post, because "we stopped looking" is not evidence
        # that the lane was safe.
        # A selected fresh lane can admit a record that task-scoped migration had
        # deliberately left untouched. Consume its persisted mappings defensively:
        # malformed hold provenance must hold fail-closed, while malformed release
        # bookkeeping grants no release.
        raw_unresolved = (record or {}).get("unresolved_lanes")
        unresolved: dict[str, str] = (
            {str(task_id): str(why) for task_id, why in raw_unresolved.items()}
            if isinstance(raw_unresolved, dict)
            else ({"unknown": "unreadable hold provenance"} if raw_unresolved else {})
        )
        raw_cleared = (record or {}).get("cleared_lanes")
        cleared_lanes: dict[str, int] = (
            {str(task_id): _epoch(at) for task_id, at in raw_cleared.items()}
            if isinstance(raw_cleared, dict)
            else {}
        )
        unresolved.update(unshowable.get(permalink) or {})
        # An operator release applies to holds carrying no new information: a persisted
        # hold, or the same lane deferring again.
        for task_id in cleared_lanes:
            unresolved.pop(task_id, None)
        # A lane read again this tick and quarantined again is new information, so it
        # re-holds the post and consumes the operator's release.
        unresolved.update(quarantined.get(permalink) or {})
        # Only a lane that was actually read and safely classified releases its hold.
        for task_id in resolved.get(permalink) or ():
            unresolved.pop(task_id, None)
            unknown_lanes.pop(task_id, None)
        for task_id in set(quarantined.get(permalink) or {}) | set(resolved.get(permalink) or ()):
            cleared_lanes.pop(task_id, None)
        persisted_lanes = (
            dict(semantic_lanes)
            if is_boris
            else {
                task_id: render_reaction(permalink, lane_state, "legacy")
                for task_id, lane_state in semantic_lanes.items()
            }
        )
        persisted_lanes.update(unknown_lanes)
        desired_state = aggregate_state(list(semantic_lanes.values()))
        desired_reaction = (
            render_reaction(permalink, desired_state, args.boris_reaction_style)
            if desired_state is not None
            else None
        )
        current = record.get("emoji") if record else None

        # A post is HELD when any lane citing it is unresolved: either a completed
        # lane that could not be classified yet (unshowable) or one that was read
        # but quarantined as unsafe. Either can be hiding a higher-precedence
        # verdict, so the post is not touched at all — no add, no removal, no
        # transition, not even after retry exhaustion.
        if unresolved:
            metrics["posts_held"] += 1
            metrics["posts_deferred"] += 1  # legacy metric name, same event
            new_record = {**(record or {}), "emoji": current, "lanes": persisted_lanes,
                          "unresolved_lanes": unresolved,
                          "deferred_at": (record or {}).get("deferred_at") or now}
            if cleared_lanes:
                new_record["cleared_lanes"] = cleared_lanes
            else:
                new_record.pop("cleared_lanes", None)
            if desired_reaction is not None:
                new_record["want"] = desired_reaction
            reason, grace = hold_report(unresolved)
            held_since = _epoch(new_record.get("deferred_at")) or now
            if now - held_since >= grace and _alert_due(new_record, now):
                new_record["alerted_at"] = now
                outputs.append(
                    f"JUST_DO_IT_REVIEW_REACTIONS held source={permalink} reason={reason} "
                    f"want={desired_reaction or 'unknown'} retained={current or 'none'} "
                    f"unresolved={','.join(sorted(unresolved)) or 'none'} "
                    f"held_for={max(0, now - held_since)}s"
                )
            posts[permalink] = new_record
            continue

        # Not held any more. Drop the hold bookkeeping so the post stops being an
        # every-tick candidate, becomes compactable again, and re-alerts promptly if
        # it is ever held a second time. `cleared_lanes` is kept: it is the operator's
        # standing release, and dropping it would let a lane that is still unshowable
        # re-hold the post on the next tick.
        if record is not None:
            record.pop("deferred_at", None)
            record.pop("alerted_at", None)
            record.pop("unresolved_lanes", None)
            if cleared_lanes:
                record["cleared_lanes"] = cleared_lanes
            else:
                record.pop("cleared_lanes", None)

        if desired_reaction is None:
            # Released, and nothing left to say. Recording a hold for every routine
            # cap/backoff deferral would otherwise leave behind empty records that
            # `compact_state` cannot age out (they have no `applied_at`). Same rule
            # `migrate_state` applies on restart, applied eagerly.
            if record is not None and not (
                record_state(record) is not None
                or normalize_review_state(record.get("want")) is not None
                or record.get("last_error") or record.get("pending_removals")
                or record.get("cleared_lanes")
            ):
                posts.pop(permalink, None)
            continue

        pending = list((record or {}).get("pending_removals") or [])
        if current == desired_reaction and not pending:
            metrics["posts_converged"] += 1
            if record is not None:
                if is_boris:
                    record["state"] = desired_state
                record["emoji"] = desired_reaction
                record["lanes"] = persisted_lanes
                record.pop("last_error", None)
                record.pop("last_error_at", None)
                record.pop("want", None)
                _reconcile_converged(
                    permalink,
                    desired_reaction,
                    record,
                    now,
                    args,
                    reactor,
                    outputs,
                    metrics,
                )
            continue

        failure_key = f"react|{permalink}"
        if not _retry_due(state["failures"].get(failure_key), now):
            continue
        reasons = ",".join(
            sorted({contribution["reason"] for contribution in (fresh.get(permalink) or {}).values()})
        )
        if args.dry_run:
            outputs.append(
                f"WOULD_TRANSITION source={permalink} {current or 'none'} -> {desired_reaction} "
                f"lanes={len(semantic_lanes)} reason={reasons or 'persisted lane state'}"
            )
            continue

        result = apply_transition(
            permalink,
            desired_reaction,
            record,
            reactor,
            owned_reactions(permalink),
        )
        metrics["reaction_calls"] += int(result.get("calls") or 0)
        if not result.get("ok"):
            _note_attempt(state["failures"], failure_key, now)
            # Persist the retained state plus the unmet intent, so the post stays a
            # retry candidate even after its lanes stop producing fresh signal.
            new_record = dict(record or {})
            new_record.update({
                "emoji": current,
                "want": desired_reaction,
                "lanes": persisted_lanes,
                "pending_removals": list(result.get("pending_removals") or pending),
                "last_error": str(result.get("error"))[-300:],
                "last_error_at": now,
            })
            posts[permalink] = new_record
            outputs.append(
                f"JUST_DO_IT_REVIEW_REACTIONS react_failed source={permalink} want={desired_reaction} "
                f"retained={current or 'none'}: {str(result.get('error'))[-300:]}"
            )
            hard_failed = True
            continue

        state["failures"].pop(failure_key, None)
        posts[permalink] = {
            **({"state": desired_state} if is_boris else {}),
            "emoji": desired_reaction,
            "applied_at": now,
            "pending_removals": result.get("pending_removals") or [],
            "lanes": persisted_lanes,
            "removed": result.get("removed") or [],
            "reason": reasons or "persisted lane state",
            # A transition does not revoke the operator's standing release: the lane
            # it released may still be unshowable and would otherwise re-hold the post
            # on the very next tick.
            **({"cleared_lanes": cleared_lanes} if cleared_lanes else {}),
        }
        # Every transition re-arms the bounded repair sweeps: a late best-effort
        # add from the prompt-side writer is only possible around a state change.
        schedule_reconcile(posts[permalink], now)
        metrics["posts_transitioned"] += 1
        outputs.append(
            f"JUST_DO_IT_REVIEW_REACTIONS transitioned source={permalink} "
            f"{current or 'none'} -> {desired_reaction} removed={','.join(result.get('removed') or []) or 'none'}"
            + (f" pending_removals={','.join(result['pending_removals'])}" if result.get("pending_removals") else "")
        )
        if result.get("pending_removals"):
            outputs.append(
                f"JUST_DO_IT_REVIEW_REACTIONS remove_failed source={permalink} "
                f"stale={','.join(result['pending_removals'])} (retries next tick)"
            )
        # Keep the legacy ledger current so a rollback to the approval-only
        # watchdog stays quiet on posts already marked ✅.
        if desired_state == STATE_APPROVED:
            for task_id, lane_state in semantic_lanes.items():
                if lane_state == STATE_APPROVED:
                    state["reacted"].setdefault(
                        f"{task_id}:{permalink}",
                        {"at": now, "task_id": task_id, "permalink": permalink,
                         "reason": reasons or "reaction state machine", "output": "reaction-state-machine"},
                    )

    if selected_candidates:
        compact_state(
            state,
            now,
            allowed_channels=allowed_channels,
            selected_tasks=only_tasks,
        )
    return (1 if hard_failed else 0), outputs, metrics


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0] if __doc__ else None)
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--lookback-seconds", type=int, default=DEFAULT_LOOKBACK_SECONDS)
    ap.add_argument("--channel", action="append", dest="channels",
                    help="Allowed source Slack channel id; repeatable. "
                         "Default: #boris-reviews only. Legacy channels are explicit "
                         "operator rollback/forensic inputs.")
    ap.add_argument("--only-task", action="append", default=[])
    ap.add_argument("--backfill-existing", action="store_true",
                    help="Consider lanes that changed before the state-machine baseline (historical backfill).")
    ap.add_argument("--max-shows", type=int, default=MAX_SHOWS_PER_TICK,
                    help=f"Cap kanban show calls per tick (default {MAX_SHOWS_PER_TICK}).")
    ap.add_argument("--clear-unresolved", action="append", default=[], metavar="PERMALINK",
                    help="Release a held post an operator has classified by hand; repeatable. "
                         "The release is standing: a lane that keeps failing to show does not "
                         "re-hold the post, but a lane that is read again and quarantines again "
                         "does.")
    ap.add_argument(
        "--boris-reaction-style",
        choices=("modern", "legacy"),
        default="modern",
        help="Render #boris-reviews with modern reactions (default) or legacy reactions for rollback.",
    )
    ap.add_argument(
        "--lifecycle-rollout-watermark",
        type=int,
        default=0,
        help="Skip lifecycle-managed lanes created at or after this epoch (disabled at 0).",
    )
    ap.add_argument("--verbose", action="store_true", help="Always print the METRICS line.")
    args = ap.parse_args(argv)

    with exclusive_run_lock() as acquired:
        if not acquired:
            return 0

        now = int(time.time())
        raw_text = None
        try:
            raw_text = STATE_PATH.read_text(encoding="utf-8")
        except OSError:
            raw_text = None
        raw = load_json(STATE_PATH, None)
        selected_tasks = set(args.only_task or [])
        allowed_channels = set(args.channels or SCHEDULED_DEFAULT_CHANNELS)
        state, notes = migrate_state(
            raw,
            now,
            backfill=args.backfill_existing,
            allowed_channels=allowed_channels,
            selected_tasks=selected_tasks,
        )
        has_selected_work = state_has_selected_task_provenance(
            state, selected_tasks, allowed_channels
        )
        code, outputs, metrics = process_tick(
            args,
            now,
            state,
            lister=kanban_reviewer_lanes,
            shower=kanban_show,
            reactor=slack_reaction,
        )
        has_selected_work = has_selected_work or (
            state_has_selected_task_provenance(state, selected_tasks, allowed_channels)
            or metrics["task_show_calls"] > 0
        )
        persist_state = raw_text is None or not selected_tasks or has_selected_work
        if notes and raw_text is not None and not args.dry_run and persist_state:
            backup = STATE_PATH.with_name(STATE_PATH.name + STATE_BACKUP_SUFFIX)
            if not backup.exists():
                try:
                    backup.write_text(raw_text, encoding="utf-8")
                except OSError as exc:  # pragma: no cover - disk failure only
                    notes.append(f"state backup failed: {exc}")
        if not args.dry_run and persist_state:
            save_json(STATE_PATH, state)

        lines = [f"JUST_DO_IT_REVIEW_REACTIONS {note}" for note in notes] + outputs
        if lines or args.verbose:
            lines.append("METRICS " + json.dumps(
                {**metrics, "posts_tracked": len(state["posts"]), "cursor_size": len(state["cursor"]["seen"])},
                sort_keys=True,
            ))
            print("\n".join(lines))
        return code


if __name__ == "__main__":
    raise SystemExit(main())
