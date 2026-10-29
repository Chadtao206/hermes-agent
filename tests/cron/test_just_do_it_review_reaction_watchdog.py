#!/usr/bin/env python3
"""Tests for the Son of Chad review-reaction state machine.

No live Slack or kanban access: the tick takes injected lister/shower/reactor
callables, and every Slack interaction is recorded by FakeSlack.
"""
from __future__ import annotations

import argparse
import fcntl
import importlib.util
import json
import os
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock

REPO_ROOT = Path(__file__).resolve().parents[2]
SCRIPT = Path(
    os.environ.get(
        "HERMES_REACTION_WATCHDOG_SCRIPT",
        REPO_ROOT / "cron" / "scripts" / "just_do_it_review_reaction_watchdog.py",
    )
)

CHANNEL = "C08K8FQKX9S"
OTHER_WATCHED = "C07BU8AAPB3"
UNWATCHED = "C0B5CT0TRFS"  # #meeting-room: chatter, never reacted to
PERMALINK = f"https://ylopo.slack.com/archives/{CHANNEL}/p1785000000000001"
PERMALINK_B = f"https://ylopo.slack.com/archives/{OTHER_WATCHED}/p1785000000000002"
BORIS_CHANNEL = "C0BPCKPEFCG"
BORIS_PERMALINK = (
    f"https://ylopo.slack.com/archives/{BORIS_CHANNEL}/p1785000000000003"
)
NOW = 1785300000


def load_mod():
    spec = importlib.util.spec_from_file_location("jdir", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


m = load_mod()


class ReactionSchemeTest(unittest.TestCase):
    def test_boris_channel_uses_modern_reactions(self):
        expected = {
            m.STATE_ACTIVE: "rubbing",
            m.STATE_NOTES: "doge_notes",
            m.STATE_BLOCKED: "face_vomiting",
            m.STATE_APPROVED: "blinkapproved",
        }
        self.assertEqual(
            {state: m.render_reaction(BORIS_PERMALINK, state) for state in expected},
            expected,
        )

    def test_legacy_channel_keeps_legacy_reactions(self):
        expected = {
            m.STATE_ACTIVE: "thinkies",
            m.STATE_NOTES: "doge_notes",
            m.STATE_BLOCKED: "review-no",
            m.STATE_APPROVED: "white_check_mark",
        }
        self.assertEqual(
            {state: m.render_reaction(PERMALINK, state) for state in expected},
            expected,
        )

    def test_reverse_style_renders_legacy_only_for_boris(self):
        self.assertEqual(
            m.render_reaction(BORIS_PERMALINK, m.STATE_BLOCKED, "legacy"),
            "review-no",
        )
        self.assertEqual(
            m.render_reaction(PERMALINK, m.STATE_BLOCKED, "legacy"),
            "review-no",
        )

    def test_boris_owns_old_and_new_names_but_legacy_does_not(self):
        self.assertEqual(
            set(m.owned_reactions(BORIS_PERMALINK)),
            {
                "thinkies", "doge_notes", "review-no", "white_check_mark",
                "rubbing", "face_vomiting", "blinkapproved",
            },
        )
        self.assertEqual(
            set(m.owned_reactions(PERMALINK)),
            {"thinkies", "doge_notes", "review-no", "white_check_mark"},
        )

    def test_normalizes_legacy_modern_and_semantic_values(self):
        expected = {
            "review-no": m.STATE_BLOCKED,
            "face_vomiting": m.STATE_BLOCKED,
            "doge_notes": m.STATE_NOTES,
            "thinkies": m.STATE_ACTIVE,
            "rubbing": m.STATE_ACTIVE,
            "white_check_mark": m.STATE_APPROVED,
            "blinkapproved": m.STATE_APPROVED,
        }
        for rendered, state in expected.items():
            self.assertEqual(m.normalize_review_state(rendered), state)
            self.assertEqual(m.normalize_review_state(state), state)

    def test_aggregate_state_preserves_precedence(self):
        self.assertEqual(
            m.aggregate_state(
                [m.STATE_APPROVED, "rubbing", "doge_notes", "face_vomiting"]
            ),
            m.STATE_BLOCKED,
        )


def args(**overrides):
    values = {
        "dry_run": False,
        "lookback_seconds": m.DEFAULT_LOOKBACK_SECONDS,
        # Unit-level state-machine tests select their fixtures explicitly. CLI
        # default-channel behavior is covered through main() below.
        "channels": [CHANNEL, OTHER_WATCHED, "C09339EGZ9U", BORIS_CHANNEL],
        "only_task": [],
        "backfill_existing": False,
        "max_shows": m.MAX_SHOWS_PER_TICK,
        "clear_unresolved": [],
        "boris_reaction_style": "modern",
        "lifecycle_rollout_watermark": 0,
        "verbose": False,
    }
    values.update(overrides)
    return argparse.Namespace(**values)


def lane(task_id, status, *, body=None, permalink=PERMALINK, completed_at=None, created_at=NOW - 600):
    return {
        "id": task_id,
        "assignee": "reviewer",
        "status": status,
        "title": f"Boris review {task_id}",
        "body": body if body is not None else f"Source Slack message: {permalink}\nPR: https://github.com/Ylopo/x/pull/1",
        "created_at": created_at,
        "started_at": created_at,
        "completed_at": completed_at if completed_at is not None else (NOW - 60 if status == "done" else 0),
    }


def show(task_id, metadata, *, run_id="run-1", ended_at=NOW - 60, status="done"):
    return {
        "task": {"id": task_id, "status": status, "assignee": "reviewer"},
        "runs": [{"id": run_id, "profile": "reviewer", "ended_at": ended_at, "metadata": metadata}],
    }


class FakeSlack:
    """Records every reaction call; configurable per (emoji, remove) failure."""

    def __init__(self, *, fail_add=(), fail_remove=(), already_reacted=()):
        self.calls: list[tuple[str, str, bool]] = []
        self.fail_add = set(fail_add)
        self.fail_remove = set(fail_remove)
        self.already_reacted = set(already_reacted)

    def __call__(self, permalink, emoji, remove):
        self.calls.append((permalink, emoji, remove))
        if remove and emoji in self.fail_remove:
            return {"ok": False, "error": "slack_down"}
        if not remove and emoji in self.fail_add:
            return {"ok": False, "error": "invalid_name"}
        if remove:
            return {"ok": True, "no_reaction": True}
        return {"ok": True, "already_reacted": emoji in self.already_reacted}

    @property
    def adds(self):
        return [(p, e) for p, e, remove in self.calls if not remove]

    @property
    def removes(self):
        return [(p, e) for p, e, remove in self.calls if remove]


def tick(rows, shows, *, state=None, slack=None, now=NOW, **arg_overrides):
    slack = slack or FakeSlack()
    # backfill=True zeroes the baselines so fixture lanes are in scope; the
    # baseline-gating tests build their own state with a live baseline.
    state = state if state is not None else m.migrate_state(None, now, backfill=True)[0]
    shown: list[str] = []

    def shower(task_id):
        shown.append(task_id)
        if task_id not in shows:
            raise RuntimeError(f"show unavailable for {task_id}")
        return shows[task_id]

    listed: list[int] = []

    def lister():
        listed.append(1)
        return rows

    code, outputs, metrics = m.process_tick(
        args(**arg_overrides), now, state, lister=lister, shower=shower, reactor=slack
    )
    return {
        "code": code,
        "outputs": outputs,
        "metrics": metrics,
        "state": state,
        "slack": slack,
        "shown": shown,
        "list_calls": len(listed),
    }


class LifecycleOwnershipTest(unittest.TestCase):
    def test_managed_lane_at_watermark_is_skipped_before_show_or_classification(self):
        row = lane("t_managed", "done", created_at=NOW - 10)
        row["created_by"] = "boris-slack-intake"

        result = tick(
            [row],
            {},
            lifecycle_rollout_watermark=NOW - 10,
        )

        self.assertEqual(result["shown"], [])
        self.assertEqual(result["slack"].calls, [])
        self.assertNotIn(PERMALINK, result["state"]["posts"])

    def test_older_managed_and_new_unrelated_lanes_keep_legacy_behavior(self):
        old = lane("t_old", "running", created_at=NOW - 20)
        old["created_by"] = "boris-slack-intake"
        unrelated = lane("t_unrelated", "running", created_at=NOW - 5)
        unrelated["created_by"] = "other-review-intake"

        old_result = tick(
            [old],
            {},
            lifecycle_rollout_watermark=NOW - 10,
        )
        unrelated_result = tick(
            [unrelated],
            {},
            lifecycle_rollout_watermark=NOW - 10,
        )

        self.assertEqual(old_result["slack"].adds, [(PERMALINK, "thinkies")])
        self.assertEqual(
            unrelated_result["slack"].adds,
            [(PERMALINK, "thinkies")],
        )


# --- retained approval-path coverage (pre-existing behavior) ---------------


class ApprovalClassifierTest(unittest.TestCase):
    def test_extracts_only_allowed_channel_permalinks(self):
        text = (
            f"one https://ylopo.slack.com/archives/{CHANNEL}/p1782330566468849 "
            f"two https://ylopo.slack.com/archives/{UNWATCHED}/p1782310024044659"
        )
        self.assertEqual(
            m.extract_permalinks(text, {CHANNEL}),
            [f"https://ylopo.slack.com/archives/{CHANNEL}/p1782330566468849"],
        )

    def test_github_approved_submission_counts(self):
        self.assertTrue(m.run_indicates_approval({"metadata": {
            "github_review_submitted": True,
            "github_review_state": "APPROVED",
            "blockers": [],
        }}))

    def test_nested_review_submission_counts(self):
        self.assertTrue(m.run_indicates_approval({"metadata": {
            "review": {"verdict": "approve", "submitted": True, "blockers": []},
        }}))

    def test_approve_recommendation_without_submission_does_not_count(self):
        self.assertFalse(m.run_indicates_approval({"metadata": {
            "verdict": "approve-risk",
            "recommended_next_action": "Chad approve if comfortable",
            "blockers": [],
        }}))

    def test_blocker_suppresses_reaction_even_if_submitted_shape_present(self):
        self.assertFalse(m.run_indicates_approval({"metadata": {
            "github_review_submitted": True,
            "github_review_state": "APPROVED",
            "blockers": ["critical issue"],
        }}))

    def test_clean_approval_classifies_white_check_mark(self):
        emoji, _ = m.classify_run({"metadata": {
            "github_review_submitted": True,
            "github_review_state": "APPROVED",
            "findings": [],
        }})
        self.assertEqual(emoji, m.STATE_APPROVED)

    def test_already_approved_exact_head_classifies_white_check_mark(self):
        emoji, _ = m.classify_run({"metadata": {
            "existing_chad_approval_for_head": True,
            "findings": [],
        }})
        self.assertEqual(emoji, m.STATE_APPROVED)


class ApprovalEvidenceTest(unittest.TestCase):
    """Submission shapes observed on real reviewer lanes (no boolean flag present)."""

    def approves(self, metadata, expected=True):
        self.assertEqual(m.run_indicates_approval({"metadata": metadata}), expected, metadata)

    def test_review_id_with_approved_state(self):
        self.approves({"github_review_id": 4797653504, "github_review_state": "APPROVED", "findings": []})

    def test_review_url_with_approved_state(self):
        self.approves({
            "github_review_url": "https://github.com/Ylopo/x/pull/106#pullrequestreview-4797653504",
            "github_review_state": "APPROVED",
            "findings": [],
        })

    def test_approved_review_flag_with_review_id(self):
        self.approves({"approved_review": True, "github_review_id": 4776235697,
                       "verdict": "accepted for human review", "blockers": []})

    def test_approved_by_chad_flag(self):
        self.approves({"approved": True, "approved_by_chadtao206": True, "findings": []})

    def test_approval_status_submitted(self):
        self.approves({"approval_status": "submitted_and_refetched", "findings": []})

    def test_review_action_approve_with_review_id(self):
        self.approves({"verdict": "APPROVED", "review_action": "APPROVE", "review_id": 4753751914,
                       "significant_findings": []})

    def test_nested_approval_review_confirmed_with_id(self):
        self.approves({"approval_review": {"confirmed": True, "id": 4756668704, "user": "Chadtao206"},
                       "blockers": []})

    def test_nested_approval_submitted_with_state(self):
        self.approves({"verdict": "approved; accepted for human review", "blockers": [], "findings": [],
                       "approval": {"submitted": True, "review_id": 4749733521, "state": "APPROVED"}})

    def test_commented_state_vetoes_review_id_evidence(self):
        self.approves({"review": {"submitted": True, "id": 999, "state": "COMMENTED"}}, expected=False)
        self.assertEqual(
            m.classify_run({"metadata": {"review": {"submitted": True, "id": 999, "state": "COMMENTED"}}})[0],
            m.STATE_NOTES,
        )

    def test_changes_requested_state_vetoes_review_id_evidence(self):
        self.approves({"github_review_id": 5, "github_review_state": "CHANGES_REQUESTED"}, expected=False)

    def test_review_id_alone_is_not_approval(self):
        self.approves({"github_review_id": 4797653504, "verdict": "accepted for human review"}, expected=False)

    def test_blockers_still_veto_evidence_based_approval(self):
        self.approves({"github_review_id": 5, "github_review_state": "APPROVED",
                       "blockers": ["unsafe migration"]}, expected=False)

    def test_third_party_approval_without_a_chad_review_is_not_approval(self):
        # Chad authored the PR, so no Chad review was posted; other people approved.
        self.approves({
            "approved": True,
            "approval_requirement": "satisfied",
            "findings": [],
            "current_head_approvers": [
                {"user": "jbankes", "review_url": "https://github.com/Ylopo/x/pull/1#pullrequestreview-1"},
            ],
            "github_review_action": {"posted": False, "review_state": "APPROVED",
                                     "authenticated_user": "Chadtao206"},
        }, expected=False)

    def test_zero_and_string_ids_are_not_evidence(self):
        self.approves({"github_review_id": 0, "github_review_state": "APPROVED"}, expected=False)
        self.approves({"github_review_id": True, "github_review_state": "APPROVED"}, expected=False)
        self.approves({"github_review_id": "not-an-id", "github_review_state": "APPROVED"}, expected=False)


# --- block / comment classifier -------------------------------------------


class BlockClassifierTest(unittest.TestCase):
    def assert_block(self, metadata, expected=True):
        blocked, why = m.run_indicates_block({"metadata": metadata})
        self.assertEqual(blocked, expected, f"why={why} metadata={metadata}")

    def test_needs_fix_verdict_blocks(self):
        self.assert_block({"verdict": "NEEDS_FIX"})

    def test_reject_verdict_blocks(self):
        self.assert_block({"verdict": "REJECT"})

    def test_lowercase_changes_requested_blocks(self):
        self.assert_block({"verdict": "changes requested"})

    def test_accept_with_required_changes_blocks(self):
        self.assert_block({"verdict": "ACCEPT WITH REQUIRED CHANGES"})

    def test_verdict_with_trailing_prose_blocks(self):
        self.assert_block({"verdict": "NEEDS_FIX; Sections 2+ are not safe to enable yet"})

    def test_github_changes_requested_state_blocks(self):
        self.assert_block({"github_review_state": "CHANGES_REQUESTED"})

    def test_non_empty_blockers_list_blocks(self):
        self.assert_block({"verdict": "COMMENT", "blockers": ["missing migration guard"]})

    def test_blocker_severity_finding_blocks(self):
        self.assert_block({"findings": [{"severity": "blocker", "summary": "unsafe"}]})

    def test_process_blocker_severity_blocks(self):
        self.assert_block({"findings": [{"severity": "PROCESS_BLOCKER", "summary": "no ticket"}]})

    def test_blocking_flag_finding_blocks(self):
        self.assert_block({"findings": [{"blocking": True, "summary": "unsafe"}]})

    def test_nested_review_blockers_block(self):
        self.assert_block({"review": {"blockers": ["broken test"]}})

    def test_not_ready_flag_blocks(self):
        self.assert_block({"not_ready": True})

    def test_prior_blocker_severity_does_not_block(self):
        self.assert_block({"findings": [{"severity": "prior_blocker", "summary": "was fixed"}]}, expected=False)

    def test_non_blocking_prose_does_not_block(self):
        self.assert_block({"verdict": "needs-follow-up; non-blocking COMMENT review submitted"}, expected=False)

    def test_no_blockers_prose_does_not_block(self):
        self.assert_block({"verdict": "ACCEPT", "summary": "no blockers found"}, expected=False)

    def test_major_severity_alone_does_not_block(self):
        self.assert_block({"findings": [{"severity": "major", "summary": "duplicated logic"}]}, expected=False)

    def test_blocking_string_finding_blocks(self):
        self.assert_block({"findings": ["BLOCKER: drops rows on retry"]})

    def test_non_blocking_string_finding_does_not_block(self):
        self.assert_block({"findings": ["non-blocking nit: rename the helper"]}, expected=False)

    # Real reviewer verdicts observed on the board: the verdict token comes first
    # and the following clauses are prose that often names blockers negatively.
    def test_approve_verdict_with_no_merge_blocker_prose_does_not_block(self):
        self.assert_block(
            {"verdict": "APPROVE; accepted for human review; no merge blocker; "
                        "deployment gated on named ops/rollout prerequisites",
             "github_review_state": "APPROVED", "approved_review": True, "findings": []},
            expected=False,
        )

    def test_approve_with_no_merge_blocker_prose_classifies_white_check_mark(self):
        emoji, why = m.classify_run({"metadata": {
            "verdict": "APPROVE; accepted for human review; no merge blocker; deployment gated",
            "github_review_state": "APPROVED",
            "approved_review": True,
            "findings": [],
        }})
        self.assertEqual((emoji, "approval"), (m.STATE_APPROVED, "approval"), why)

    def test_zero_remaining_blockers_prose_does_not_block(self):
        self.assert_block({"verdict": "ACCEPT with zero remaining major blockers"}, expected=False)

    def test_blockers_none_prose_does_not_block(self):
        self.assert_block({"verdict": "blockers none"}, expected=False)

    def test_blocking_clause_after_the_verdict_still_blocks_when_first(self):
        self.assert_block({"verdict": "BLOCKED on failing migration; will re-review"})

    def test_first_clause_verdict_beats_trailing_approval_prose(self):
        self.assert_block({"verdict": "NEEDS_FIX; would otherwise approve"})

    def test_marker_must_start_on_a_token_boundary(self):
        # "INITIAL" contains "NIT" mid-token and must not read as a nit comment.
        self.assertEqual(m.classify_run({"metadata": {"verdict": "INITIAL"}})[0], None)


class CommentClassifierTest(unittest.TestCase):
    def classify(self, metadata):
        return m.classify_run({"metadata": metadata})[0]

    def test_commented_review_state_is_doge_notes(self):
        self.assertEqual(self.classify({"github_review_state": "COMMENTED"}), m.STATE_NOTES)

    def test_needs_follow_up_non_blocking_is_doge_notes(self):
        self.assertEqual(
            self.classify({"verdict": "needs-follow-up; non-blocking COMMENT review submitted"}),
            m.STATE_NOTES,
        )

    def test_major_findings_are_doge_notes(self):
        self.assertEqual(
            self.classify({"verdict": "ACCEPTED_FOR_HUMAN_REVIEW",
                           "findings": [{"severity": "major", "summary": "duplicated logic"}]}),
            m.STATE_NOTES,
        )

    def test_minor_findings_are_doge_notes(self):
        self.assertEqual(self.classify({"findings": [{"severity": "minor", "summary": "typo"}]}), m.STATE_NOTES)

    def test_posted_comment_count_is_doge_notes(self):
        self.assertEqual(self.classify({"review_comments_posted": 3}), m.STATE_NOTES)

    def test_approval_with_substantive_findings_prefers_doge_notes(self):
        # Chad's precedence puts doge_notes above white_check_mark, so a lane that
        # approved AND delivered findings shows notes.
        self.assertEqual(
            self.classify({
                "github_review_submitted": True,
                "github_review_state": "APPROVED",
                "findings": [{"severity": "minor", "summary": "nit"}],
            }),
            m.STATE_NOTES,
        )

    def test_block_wins_over_comments(self):
        self.assertEqual(
            self.classify({
                "github_review_state": "COMMENTED",
                "findings": [{"severity": "blocker", "summary": "unsafe"}],
            }),
            m.STATE_BLOCKED,
        )

    def test_empty_findings_are_not_comments(self):
        self.assertEqual(
            self.classify({"github_review_submitted": True, "github_review_state": "APPROVED", "findings": []}),
            m.STATE_APPROVED,
        )


class MalformedMetadataTest(unittest.TestCase):
    def test_missing_metadata_is_no_signal(self):
        self.assertEqual(m.classify_run({}), (None, "no metadata dict"))

    def test_non_dict_metadata_is_no_signal(self):
        for value in ("NEEDS_FIX", ["NEEDS_FIX"], 7, None):
            self.assertEqual(m.classify_run({"metadata": value})[0], None)

    def test_non_dict_blocking_review_container_still_blocks(self):
        # Boris REJECT t_397531f0: a malformed blocking surface must never be
        # dropped. The shape is wrong but the content is unambiguous.
        self.assertEqual(m.classify_run({"metadata": {"review": "NEEDS_FIX"}})[0], m.STATE_BLOCKED)

    def test_unreadable_review_container_quarantines(self):
        status, emoji, _why = m.classify_run_status({"metadata": {"review": 7}})
        self.assertEqual((status, emoji), ("quarantine", None))

    def test_non_list_findings_are_ignored(self):
        self.assertEqual(m.classify_run({"metadata": {"findings": "none"}})[0], None)

    def test_unknown_verdict_is_no_signal(self):
        self.assertEqual(m.classify_run({"metadata": {"verdict": "SUPERSEDED_HEAD_MOVED"}})[0], None)

    def test_non_reviewer_run_is_skipped(self):
        payload = show("t_1", {"verdict": "NEEDS_FIX"})
        payload["runs"][0]["profile"] = "engineer"
        self.assertEqual(m.classify_task(payload)[0], None)

    def test_newest_run_with_signal_wins(self):
        payload = {
            "task": {"id": "t_1", "status": "done", "assignee": "reviewer"},
            "runs": [
                {"id": "old", "ended_at": NOW - 900, "metadata": {"verdict": "NEEDS_FIX"}},
                {"id": "new", "ended_at": NOW - 60, "metadata": {"github_review_state": "COMMENTED"}},
            ],
        }
        emoji, why = m.classify_task(payload)
        self.assertEqual(emoji, m.STATE_NOTES)
        self.assertIn("run=new", why)

    def test_newest_unclassified_run_quarantines_instead_of_falling_back(self):
        # Boris REJECT t_397531f0: an older approval must not be resurrected behind
        # a newer run the classifier could not read — that newer run may be a block.
        payload = {
            "task": {"id": "t_1", "status": "done", "assignee": "reviewer"},
            "runs": [
                {"id": "old", "ended_at": NOW - 900, "metadata": {
                    "github_review_submitted": True, "github_review_state": "APPROVED", "findings": []}},
                {"id": "new", "ended_at": NOW - 60, "metadata": {"worker_session_id": "abc"}},
            ],
        }
        emoji, why = m.classify_task(payload)
        self.assertEqual(emoji, m.QUARANTINE)
        self.assertIn("new", why)

    def test_older_block_is_still_adopted_behind_an_unclassified_newer_run(self):
        # review-no tops the precedence order, so nothing the unreadable newer run
        # could be hiding outranks it: keeping the block is the safe choice.
        payload = {
            "task": {"id": "t_1", "status": "done", "assignee": "reviewer"},
            "runs": [
                {"id": "old", "ended_at": NOW - 900, "metadata": {"verdict": "NEEDS_FIX"}},
                {"id": "new", "ended_at": NOW - 60, "metadata": {"worker_session_id": "abc"}},
            ],
        }
        self.assertEqual(m.classify_task(payload)[0], m.STATE_BLOCKED)

    def test_older_comment_is_not_adopted_behind_an_unclassified_newer_run(self):
        payload = {
            "task": {"id": "t_1", "status": "done", "assignee": "reviewer"},
            "runs": [
                {"id": "old", "ended_at": NOW - 900, "metadata": {"github_review_state": "COMMENTED"}},
                {"id": "new", "ended_at": NOW - 60, "metadata": {"worker_session_id": "abc"}},
            ],
        }
        self.assertEqual(m.classify_task(payload)[0], m.QUARANTINE)


# --- precedence and aggregation -------------------------------------------


class PrecedenceTest(unittest.TestCase):
    def test_precedence_order(self):
        self.assertEqual(
            sorted(m.STATE_PRECEDENCE, key=lambda state: -m.STATE_PRECEDENCE[state]),
            [m.STATE_BLOCKED, m.STATE_NOTES, m.STATE_ACTIVE, m.STATE_APPROVED],
        )

    def test_aggregate_picks_highest(self):
        self.assertEqual(m.aggregate_state([m.STATE_APPROVED, m.STATE_ACTIVE]), m.STATE_ACTIVE)
        self.assertEqual(m.aggregate_state([m.STATE_ACTIVE, m.STATE_NOTES]), m.STATE_NOTES)
        self.assertEqual(m.aggregate_state([m.STATE_NOTES, m.STATE_BLOCKED]), m.STATE_BLOCKED)
        self.assertEqual(
            m.aggregate_state([m.STATE_BLOCKED, m.STATE_APPROVED, m.STATE_ACTIVE]),
            m.STATE_BLOCKED,
        )

    def test_aggregate_fails_closed_on_unknown_and_empty(self):
        self.assertIsNone(m.aggregate_state([]))
        self.assertIsNone(m.aggregate_state(["eyes"]))
        self.assertIsNone(m.aggregate_state(["eyes", m.STATE_ACTIVE]))

    def test_multi_pr_same_post_aggregates_to_block(self):
        rows = [lane("t_ok", "done"), lane("t_bad", "done")]
        shows = {
            "t_ok": show("t_ok", {"github_review_submitted": True, "github_review_state": "APPROVED", "findings": []}),
            "t_bad": show("t_bad", {"verdict": "NEEDS_FIX"}),
        }
        result = tick(rows, shows)
        self.assertEqual(result["slack"].adds, [(PERMALINK, m.EMOJI_BLOCK)])
        self.assertEqual(result["state"]["posts"][PERMALINK]["emoji"], m.EMOJI_BLOCK)

    def test_second_lane_completion_cannot_downgrade_a_block(self):
        first = tick([lane("t_bad", "done")], {"t_bad": show("t_bad", {"verdict": "NEEDS_FIX"})})
        state = first["state"]
        rows = [lane("t_bad", "done"), lane("t_ok", "done")]
        shows = {"t_ok": show("t_ok", {
            "github_review_submitted": True, "github_review_state": "APPROVED", "findings": []})}
        # Before the first bounded reconciliation sweep is due, a converged post
        # costs no Slack traffic at all (sweeps are covered by ReconciliationTest).
        second = tick(rows, shows, state=state, now=NOW + 120)
        self.assertEqual(state["posts"][PERMALINK]["emoji"], m.EMOJI_BLOCK)
        self.assertEqual(second["slack"].calls, [])  # already converged: no Slack traffic


# --- lane discovery -------------------------------------------------------


class LaneDiscoveryTest(unittest.TestCase):
    def test_active_lane_gets_thinkies_without_a_show_call(self):
        result = tick([lane("t_a", "running")], {})
        self.assertEqual(result["slack"].adds, [(PERMALINK, m.EMOJI_ACTIVE)])
        self.assertEqual(result["shown"], [])
        self.assertEqual(result["metrics"]["task_show_calls"], 0)
        self.assertEqual(result["list_calls"], 1)

    def test_every_active_status_is_thinkies(self):
        for status in sorted(m.ACTIVE_STATUSES):
            result = tick([lane("t_a", status)], {})
            self.assertEqual(result["slack"].adds, [(PERMALINK, m.EMOJI_ACTIVE)], status)

    def test_lane_without_source_permalink_never_reacts(self):
        rows = [lane("t_a", "running", body="No slack source here\nPR: https://github.com/Ylopo/x/pull/1")]
        result = tick(rows, {})
        self.assertEqual(result["slack"].calls, [])
        self.assertEqual(result["state"]["posts"], {})

    def test_unwatched_channel_permalink_is_ignored(self):
        rows = [lane("t_a", "running", permalink=f"https://ylopo.slack.com/archives/{UNWATCHED}/p1785000000000009")]
        result = tick(rows, {})
        self.assertEqual(result["slack"].calls, [])

    def test_channel_override_narrows_the_allowlist(self):
        rows = [lane("t_a", "running", permalink=PERMALINK_B)]
        self.assertEqual(tick(rows, {}, channels=[CHANNEL])["slack"].calls, [])
        self.assertEqual(tick(rows, {}, channels=[OTHER_WATCHED])["slack"].adds, [(PERMALINK_B, m.EMOJI_ACTIVE)])

    def test_malformed_permalink_is_not_a_source(self):
        rows = [lane("t_a", "running", body=f"https://ylopo.slack.com/archives/{CHANNEL}/p178500000000")]
        self.assertEqual(tick(rows, {})["slack"].calls, [])

    def test_non_reviewer_and_inert_lanes_are_skipped(self):
        rows = [lane("t_a", "running"), lane("t_b", "blocked"), lane("t_c", "archived")]
        rows[0]["assignee"] = "engineer"
        self.assertEqual(tick(rows, {})["slack"].calls, [])

    def test_show_calls_are_capped_and_remaining_lanes_defer(self):
        rows = [
            lane(f"t_{index}", "done", permalink=f"https://ylopo.slack.com/archives/{CHANNEL}/p17850000000000{index:02d}",
                 completed_at=NOW - index)
            for index in range(10, 15)
        ]
        shows = {row["id"]: show(row["id"], {"verdict": "NEEDS_FIX"}) for row in rows}
        result = tick(rows, shows, max_shows=2)
        self.assertEqual(result["metrics"]["task_show_calls"], 2)
        self.assertEqual(len(result["slack"].adds), 2)
        self.assertEqual(result["metrics"]["shows_deferred"], 3)

    def test_completed_lane_is_not_shown_twice(self):
        rows = [lane("t_a", "done")]
        shows = {"t_a": show("t_a", {"verdict": "NEEDS_FIX"})}
        first = tick(rows, shows)
        self.assertEqual(first["shown"], ["t_a"])
        second = tick(rows, shows, state=first["state"], now=NOW + 120)
        self.assertEqual(second["shown"], [])
        self.assertEqual(second["slack"].calls, [])

    def test_pre_baseline_lane_is_not_backfilled(self):
        state = m.migrate_state(None, NOW)[0]  # baseline = NOW
        rows = [lane("t_old", "done", completed_at=NOW - 86400, created_at=NOW - 90000)]
        shows = {"t_old": show("t_old", {"verdict": "NEEDS_FIX"})}
        result = tick(rows, shows, state=state, now=NOW + 60)
        self.assertEqual(result["slack"].calls, [])
        self.assertEqual(result["shown"], [])

    def test_backfill_flag_reaches_pre_baseline_lanes(self):
        state = m.migrate_state(None, NOW)[0]
        rows = [lane("t_old", "done", completed_at=NOW - 86400, created_at=NOW - 90000)]
        shows = {"t_old": show("t_old", {"verdict": "NEEDS_FIX"})}
        result = tick(rows, shows, state=state, now=NOW + 60, backfill_existing=True)
        self.assertEqual(result["slack"].adds, [(PERMALINK, m.EMOJI_BLOCK)])

    def test_unclassified_completed_lane_defers_the_post(self):
        # Two lanes on one post; the second cannot be shown, so nothing is applied
        # yet rather than stamping a state that the missing lane may outrank.
        rows = [lane("t_ok", "done"), lane("t_missing", "done")]
        shows = {"t_ok": show("t_ok", {"github_review_state": "COMMENTED"})}
        result = tick(rows, shows)
        self.assertEqual(result["slack"].calls, [])
        self.assertEqual(result["metrics"]["posts_deferred"], 1)
        self.assertEqual(result["code"], 1)

    def test_repeated_show_failures_never_stop_deferring(self):
        # Boris REJECT t_397531f0: retry exhaustion used to release the post and let
        # the *other* lane stamp doge_notes, which can hide an unknown review-no.
        # The post must stay untouched and the operator must be told instead.
        rows = [lane("t_ok", "done"), lane("t_missing", "done")]
        shows = {"t_ok": show("t_ok", {"github_review_state": "COMMENTED"})}
        state = m.migrate_state(None, NOW)[0]
        state["machine_baseline_at"] = 0
        now = NOW
        for _ in range(m.MAX_SHOW_ATTEMPTS):
            now += m.FAIL_RETRY_SECONDS
            tick(rows, shows, state=state, now=now)
        now += m.FAIL_RETRY_SECONDS
        final = tick(rows, shows, state=state, now=now)
        self.assertIn("show_unresolved", "\n".join(final["outputs"]))
        self.assertNotIn("show_gave_up", "\n".join(final["outputs"]))
        self.assertEqual(final["slack"].calls, [])
        self.assertIsNone(state["posts"][PERMALINK]["emoji"])
        self.assertEqual(state["posts"][PERMALINK]["want"], m.EMOJI_COMMENT)
        # Far past exhaustion the post is still held, still never transitioned.
        for _ in range(20):
            now += m.FAIL_RETRY_SECONDS
            later = tick(rows, shows, state=state, now=now)
            self.assertEqual(later["slack"].calls, [])
        self.assertIsNone(state["posts"][PERMALINK]["emoji"])

    def test_unresolved_alert_is_throttled_not_emitted_every_tick(self):
        rows = [lane("t_ok", "done"), lane("t_missing", "done")]
        shows = {"t_ok": show("t_ok", {"github_review_state": "COMMENTED"})}
        state = m.migrate_state(None, NOW)[0]
        state["machine_baseline_at"] = 0
        now, alerts = NOW, 0
        for _ in range(m.MAX_SHOW_ATTEMPTS + 12):
            now += m.FAIL_RETRY_SECONDS
            result = tick(rows, shows, state=state, now=now)
            alerts += "show_unresolved" in "\n".join(result["outputs"])
        # 17 hourly ticks past exhaustion, alerting every UNRESOLVED_ALERT_SECONDS.
        self.assertGreaterEqual(alerts, 1)
        self.assertLessEqual(alerts, 4)

    def test_show_recovery_releases_the_held_post(self):
        rows = [lane("t_ok", "done"), lane("t_missing", "done")]
        shows = {"t_ok": show("t_ok", {"github_review_state": "COMMENTED"})}
        state = m.migrate_state(None, NOW)[0]
        state["machine_baseline_at"] = 0
        now = NOW
        for _ in range(m.MAX_SHOW_ATTEMPTS + 1):
            now += m.FAIL_RETRY_SECONDS
            tick(rows, shows, state=state, now=now)
        self.assertIsNone(state["posts"][PERMALINK]["emoji"])
        # The lane finally becomes showable, and it turns out to have been a block.
        shows["t_missing"] = show("t_missing", {"verdict": "NEEDS_FIX"})
        now += m.FAIL_RETRY_SECONDS
        healed = tick(rows, shows, state=state, now=now)
        self.assertEqual(healed["slack"].adds, [(PERMALINK, m.EMOJI_BLOCK)])
        self.assertEqual(state["posts"][PERMALINK]["emoji"], m.EMOJI_BLOCK)

    def test_only_task_bypasses_the_baseline_and_other_lanes(self):
        state = m.migrate_state(None, NOW)[0]
        rows = [lane("t_old", "done", completed_at=NOW - 86400), lane("t_other", "running")]
        shows = {"t_old": show("t_old", {"verdict": "REJECT"})}
        result = tick(rows, shows, state=state, now=NOW + 60, only_task=["t_old"])
        self.assertEqual(result["slack"].adds, [(PERMALINK, m.EMOJI_BLOCK)])

    def test_metadata_only_permalink_is_used_for_shown_lanes(self):
        rows = [lane("t_a", "done", body=f"Source Slack message: {PERMALINK}")]
        payload = show("t_a", {"verdict": "NEEDS_FIX", "source_slack_permalink": PERMALINK_B})
        result = tick(rows, {"t_a": payload})
        self.assertEqual(
            sorted(emoji_link for emoji_link, _ in result["slack"].adds),
            sorted({PERMALINK, PERMALINK_B}),
        )


# --- transition mechanics -------------------------------------------------


class TransitionTest(unittest.TestCase):
    def test_adds_desired_before_removing_stale(self):
        slack = FakeSlack()
        result = m.apply_transition(
            PERMALINK,
            m.EMOJI_COMMENT,
            {"emoji": m.EMOJI_ACTIVE},
            slack,
            m.owned_reactions(PERMALINK),
        )
        self.assertTrue(result["ok"])
        self.assertEqual(slack.calls, [
            (PERMALINK, m.EMOJI_COMMENT, False),
            (PERMALINK, m.EMOJI_ACTIVE, True),
        ])
        self.assertEqual(result["removed"], [m.EMOJI_ACTIVE])
        self.assertEqual(result["pending_removals"], [])

    def test_unknown_prior_sweeps_every_other_owned_emoji(self):
        slack = FakeSlack()
        result = m.apply_transition(
            PERMALINK, m.EMOJI_ACTIVE, None, slack, m.owned_reactions(PERMALINK)
        )
        self.assertEqual(slack.adds, [(PERMALINK, m.EMOJI_ACTIVE)])
        self.assertEqual(
            [emoji for _, emoji in slack.removes],
            [m.EMOJI_BLOCK, m.EMOJI_COMMENT, m.EMOJI_APPROVED],
        )
        self.assertTrue(result["ok"])

    def test_already_reacted_and_no_reaction_are_success(self):
        slack = FakeSlack(already_reacted=[m.EMOJI_BLOCK])
        result = m.apply_transition(
            PERMALINK,
            m.EMOJI_BLOCK,
            {"emoji": m.EMOJI_COMMENT},
            slack,
            m.owned_reactions(PERMALINK),
        )
        self.assertTrue(result["ok"])
        self.assertTrue(result["already_reacted"])
        self.assertEqual(result["pending_removals"], [])

    def test_failed_add_retains_prior_and_removes_nothing(self):
        slack = FakeSlack(fail_add=[m.EMOJI_BLOCK])
        result = m.apply_transition(
            PERMALINK,
            m.EMOJI_BLOCK,
            {"emoji": m.EMOJI_ACTIVE},
            slack,
            m.owned_reactions(PERMALINK),
        )
        self.assertFalse(result["ok"])
        self.assertEqual(slack.removes, [])
        self.assertEqual(result["prior"], m.EMOJI_ACTIVE)

    def test_failed_removal_is_recorded_for_retry(self):
        slack = FakeSlack(fail_remove=[m.EMOJI_ACTIVE])
        result = m.apply_transition(
            PERMALINK,
            m.EMOJI_COMMENT,
            {"emoji": m.EMOJI_ACTIVE},
            slack,
            m.owned_reactions(PERMALINK),
        )
        self.assertTrue(result["ok"])
        self.assertEqual(result["pending_removals"], [m.EMOJI_ACTIVE])

    def test_pending_removal_is_retried_on_the_next_tick(self):
        rows = [lane("t_a", "done")]
        shows = {"t_a": show("t_a", {"github_review_state": "COMMENTED"})}
        state = m.migrate_state(None, NOW)[0]
        state["machine_baseline_at"] = 0
        state["posts"][PERMALINK] = {
            "emoji": m.EMOJI_ACTIVE, "applied_at": NOW - 600, "pending_removals": [], "lanes": {"t_a": m.EMOJI_ACTIVE},
        }
        first = tick(rows, shows, state=state, slack=FakeSlack(fail_remove=[m.EMOJI_ACTIVE]))
        self.assertEqual(state["posts"][PERMALINK]["pending_removals"], [m.EMOJI_ACTIVE])
        self.assertIn("remove_failed", "\n".join(first["outputs"]))
        second = tick(rows, shows, state=state, now=NOW + m.FAIL_RETRY_SECONDS)
        self.assertEqual(state["posts"][PERMALINK]["pending_removals"], [])
        self.assertEqual(second["slack"].removes, [(PERMALINK, m.EMOJI_ACTIVE)])

    def test_failed_add_is_retried_and_keeps_prior_reaction(self):
        rows = [lane("t_a", "done")]
        shows = {"t_a": show("t_a", {"verdict": "NEEDS_FIX"})}
        state = m.migrate_state(None, NOW)[0]
        state["machine_baseline_at"] = 0
        state["posts"][PERMALINK] = {
            "emoji": m.EMOJI_ACTIVE, "applied_at": NOW - 600, "pending_removals": [], "lanes": {"t_a": m.EMOJI_ACTIVE},
        }
        first = tick(rows, shows, state=state, slack=FakeSlack(fail_add=[m.EMOJI_BLOCK]))
        self.assertEqual(first["code"], 1)
        self.assertEqual(state["posts"][PERMALINK]["emoji"], m.EMOJI_ACTIVE)
        self.assertEqual(state["posts"][PERMALINK]["want"], m.EMOJI_BLOCK)
        # The lane is already classified, so the retry must come from post state.
        second = tick(rows, shows, state=state, now=NOW + m.FAIL_RETRY_SECONDS)
        self.assertEqual(second["shown"], [])
        self.assertEqual(second["slack"].adds, [(PERMALINK, m.EMOJI_BLOCK)])
        self.assertEqual(state["posts"][PERMALINK]["emoji"], m.EMOJI_BLOCK)
        self.assertNotIn("want", state["posts"][PERMALINK])

    def test_converged_post_costs_no_slack_calls(self):
        rows = [lane("t_a", "running")]
        first = tick(rows, {})
        second = tick(rows, {}, state=first["state"], now=NOW + 120)
        self.assertEqual(second["slack"].calls, [])
        self.assertEqual(second["metrics"]["posts_converged"], 1)

    def test_exactly_one_desired_reaction_after_full_lifecycle(self):
        state = m.migrate_state(None, NOW)[0]
        state["machine_baseline_at"] = 0
        slack_calls: list[tuple[str, str, bool]] = []
        stages = [
            ([lane("t_a", "running")], {}, m.EMOJI_ACTIVE),
            ([lane("t_a", "done", completed_at=NOW + 100)],
             {"t_a": show("t_a", {"github_review_state": "COMMENTED"}, run_id="r1", ended_at=NOW + 100)},
             m.EMOJI_COMMENT),
            ([lane("t_a", "done", completed_at=NOW + 200)],
             {"t_a": show("t_a", {"verdict": "NEEDS_FIX"}, run_id="r2", ended_at=NOW + 200)},
             m.EMOJI_BLOCK),
            ([lane("t_a", "done", completed_at=NOW + 300)],
             {"t_a": show("t_a", {"github_review_submitted": True, "github_review_state": "APPROVED",
                                  "findings": []}, run_id="r3", ended_at=NOW + 300)},
             m.EMOJI_APPROVED),
        ]
        for index, (rows, shows, expected) in enumerate(stages):
            slack = FakeSlack()
            result = tick(rows, shows, state=state, slack=slack, now=NOW + 100 * index + 1)
            slack_calls.extend(slack.calls)
            self.assertEqual(state["posts"][PERMALINK]["emoji"], expected)
            self.assertEqual(slack.adds, [(PERMALINK, expected)], f"stage {index}")
            self.assertEqual(result["metrics"]["posts_transitioned"], 1, f"stage {index}")
        # Replay every recorded call: the post ends with exactly the final emoji.
        live: set[str] = set()
        for _permalink, emoji, remove in slack_calls:
            live.discard(emoji) if remove else live.add(emoji)
        self.assertEqual(live, {m.EMOJI_APPROVED})

    def test_dry_run_touches_no_slack_and_no_state(self):
        rows = [lane("t_a", "running")]
        result = tick(rows, {}, dry_run=True)
        self.assertEqual(result["slack"].calls, [])
        self.assertEqual(result["state"]["posts"], {})
        self.assertIn("WOULD_TRANSITION", "\n".join(result["outputs"]))

    def test_kanban_list_failure_is_reported_without_reactions(self):
        state = m.migrate_state(None, NOW)[0]

        def boom():
            raise RuntimeError("kanban exploded")

        slack = FakeSlack()
        code, outputs, _ = m.process_tick(
            args(), NOW, state, lister=boom, shower=lambda task_id: {}, reactor=slack
        )
        self.assertEqual(code, 1)
        self.assertIn("kanban_list_failed", "\n".join(outputs))
        self.assertEqual(slack.calls, [])


class BorisMigrationTest(unittest.TestCase):
    def test_modern_transition_adds_before_removing_legacy(self):
        slack = FakeSlack()
        record = {"emoji": "review-no", "state": m.STATE_BLOCKED}
        result = m.apply_transition(
            BORIS_PERMALINK,
            "face_vomiting",
            record,
            slack,
            m.owned_reactions(BORIS_PERMALINK),
        )
        self.assertTrue(result["ok"])
        self.assertEqual(slack.calls[0], (BORIS_PERMALINK, "face_vomiting", False))
        self.assertIn((BORIS_PERMALINK, "review-no", True), slack.calls[1:])

    def test_failed_modern_add_retains_legacy_reaction(self):
        slack = FakeSlack(fail_add={"face_vomiting"})
        result = m.apply_transition(
            BORIS_PERMALINK,
            "face_vomiting",
            {"emoji": "review-no", "state": m.STATE_BLOCKED},
            slack,
            m.owned_reactions(BORIS_PERMALINK),
        )
        self.assertFalse(result["ok"])
        self.assertEqual(slack.removes, [])

    def test_legacy_post_never_removes_modern_manual_emoji(self):
        slack = FakeSlack()
        m.reconcile_sweep(
            PERMALINK,
            "review-no",
            slack,
            m.owned_reactions(PERMALINK),
        )
        self.assertNotIn((PERMALINK, "face_vomiting", True), slack.calls)
        self.assertNotIn((PERMALINK, "rubbing", True), slack.calls)
        self.assertNotIn((PERMALINK, "blinkapproved", True), slack.calls)

    def test_reverse_style_removes_modern_after_adding_legacy(self):
        state = m.STATE_BLOCKED
        desired = m.render_reaction(BORIS_PERMALINK, state, "legacy")
        slack = FakeSlack()
        result = m.apply_transition(
            BORIS_PERMALINK,
            desired,
            {"emoji": "face_vomiting", "state": state},
            slack,
            m.owned_reactions(BORIS_PERMALINK),
        )
        self.assertTrue(result["ok"])
        self.assertEqual(slack.calls[0], (BORIS_PERMALINK, "review-no", False))
        self.assertIn((BORIS_PERMALINK, "face_vomiting", True), slack.calls[1:])

    def test_kanban_adapter_exposes_only_list_and_show(self):
        completed = subprocess.CompletedProcess(
            args=[], returncode=0, stdout="[]", stderr=""
        )
        with mock.patch.object(m, "run", return_value=completed) as run:
            m.kanban_reviewer_lanes()
            completed.stdout = json.dumps({"id": "lane-1"})
            m.kanban_show("lane-1")
        verbs = [call.args[0][2] for call in run.call_args_list]
        self.assertEqual(verbs, ["list"] * len(m.REVIEWER_IDENTITIES) + ["show"])

    def mixed_channel_state(self):
        state = m.migrate_state(None, NOW)[0]
        state["machine_baseline_at"] = 0
        state["posts"] = {
            BORIS_PERMALINK: {
                "emoji": "doge_notes",
                "applied_at": NOW - 60,
                "pending_removals": [],
                "lanes": {"t_boris": "doge_notes"},
                "source": "boris-fixture",
            },
            PERMALINK: {
                "emoji": "doge_notes",
                "applied_at": NOW - 60,
                "pending_removals": [],
                "lanes": {"t_legacy": "doge_notes"},
                "source": "legacy-fixture",
            },
        }
        return state

    def test_boris_only_backfill_migrates_ledger_without_touching_legacy_record(self):
        state = self.mixed_channel_state()
        legacy_before = json.loads(json.dumps(state["posts"][PERMALINK]))
        result = tick(
            [lane("t_boris", "done", permalink=BORIS_PERMALINK)],
            {"t_boris": show("t_boris", {"github_review_state": "COMMENTED"})},
            state=state,
            only_task=["t_boris"],
            backfill_existing=True,
        )
        self.assertEqual(result["slack"].calls, [])
        self.assertEqual(state["posts"][BORIS_PERMALINK]["state"], m.STATE_NOTES)
        self.assertEqual(
            state["posts"][BORIS_PERMALINK]["lanes"], {"t_boris": m.STATE_NOTES}
        )
        self.assertEqual(state["posts"][PERMALINK], legacy_before)

    def test_boris_style_defaults_modern_and_can_reverse_to_legacy(self):
        modern = tick([lane("t_modern", "running", permalink=BORIS_PERMALINK)], {})
        self.assertEqual(modern["slack"].adds, [(BORIS_PERMALINK, "rubbing")])

        legacy = tick(
            [lane("t_legacy", "running", permalink=BORIS_PERMALINK)],
            {},
            boris_reaction_style="legacy",
        )
        self.assertEqual(legacy["slack"].adds, [(BORIS_PERMALINK, "thinkies")])

    def test_busy_invocation_lock_exits_without_loading_processing_or_saving(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            state_path = Path(temp_dir) / "watchdog.json"
            lock_path = state_path.with_suffix(".lock")
            lock_path.parent.mkdir(parents=True, exist_ok=True)
            with lock_path.open("a+") as handle:
                fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                with (
                    mock.patch.object(m, "STATE_PATH", state_path),
                    mock.patch.object(m, "load_json") as load,
                    mock.patch.object(m, "process_tick") as process,
                    mock.patch.object(m, "slack_reaction") as react,
                    mock.patch.object(m, "save_json") as save,
                ):
                    self.assertEqual(m.main([]), 0)
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
        load.assert_not_called()
        process.assert_not_called()
        react.assert_not_called()
        save.assert_not_called()

    def test_main_roundtrips_boris_style_without_changing_legacy_records(self):
        initial = m.migrate_state(None, NOW, backfill=True)[0]
        initial["posts"] = {
            BORIS_PERMALINK: {
                "emoji": "review-no",
                "applied_at": NOW - 60,
                "pending_removals": [],
                "lanes": {"t_boris": "review-no"},
                "source": "boris-transaction-fixture",
            },
            PERMALINK: {
                "emoji": "doge_notes",
                "applied_at": NOW - 60,
                "pending_removals": [],
                "lanes": {"t_legacy": "doge_notes"},
                "source": "legacy-transaction-fixture",
                "opaque": {"keep": ["deeply", "equal"]},
            },
        }
        legacy_before = json.loads(json.dumps(initial["posts"][PERMALINK]))
        row = lane("t_boris", "done", permalink=BORIS_PERMALINK)
        shown = show("t_boris", {"verdict": "NEEDS_FIX"})
        invocation = ["--backfill-existing", "--only-task", "t_boris"]

        with tempfile.TemporaryDirectory() as temp_dir:
            state_path = Path(temp_dir) / "watchdog.json"
            state_path.write_text(json.dumps(initial))

            modern = FakeSlack()
            with (
                mock.patch.object(m, "STATE_PATH", state_path),
                mock.patch.object(m.time, "time", return_value=NOW),
                mock.patch.object(m, "kanban_reviewer_lanes", return_value=[row]),
                mock.patch.object(m, "kanban_show", return_value=shown),
                mock.patch.object(m, "slack_reaction", modern),
            ):
                self.assertEqual(m.main(invocation), 0)
            after_modern = json.loads(state_path.read_text())
            self.assertEqual(modern.calls, [
                (BORIS_PERMALINK, "face_vomiting", False),
                (BORIS_PERMALINK, "review-no", True),
            ])
            self.assertEqual(after_modern["posts"][BORIS_PERMALINK]["state"], m.STATE_BLOCKED)
            self.assertEqual(after_modern["posts"][BORIS_PERMALINK]["emoji"], "face_vomiting")
            self.assertEqual(after_modern["posts"][PERMALINK], legacy_before)

            legacy = FakeSlack()
            with (
                mock.patch.object(m, "STATE_PATH", state_path),
                mock.patch.object(m.time, "time", return_value=NOW + 1),
                mock.patch.object(m, "kanban_reviewer_lanes", return_value=[row]),
                mock.patch.object(m, "kanban_show", return_value=shown),
                mock.patch.object(m, "slack_reaction", legacy),
            ):
                self.assertEqual(m.main([*invocation, "--boris-reaction-style", "legacy"]), 0)
            after_legacy = json.loads(state_path.read_text())
            self.assertEqual(legacy.calls, [
                (BORIS_PERMALINK, "review-no", False),
                (BORIS_PERMALINK, "face_vomiting", True),
            ])
            self.assertEqual(after_legacy["posts"][BORIS_PERMALINK]["state"], m.STATE_BLOCKED)
            self.assertEqual(after_legacy["posts"][BORIS_PERMALINK]["emoji"], "review-no")
            self.assertEqual(after_legacy["posts"][PERMALINK], legacy_before)

            converged = FakeSlack()
            with (
                mock.patch.object(m, "STATE_PATH", state_path),
                mock.patch.object(m.time, "time", return_value=NOW + 2),
                mock.patch.object(m, "kanban_reviewer_lanes", return_value=[row]),
                mock.patch.object(m, "kanban_show", return_value=shown),
                mock.patch.object(m, "slack_reaction", converged),
            ):
                self.assertEqual(m.main([*invocation, "--boris-reaction-style", "legacy"]), 0)
            self.assertEqual(converged.calls, [])
            self.assertEqual(json.loads(state_path.read_text())["posts"][PERMALINK], legacy_before)

    def run_persisted_main(self, initial, invocation, *, rows=None):
        with tempfile.TemporaryDirectory() as temp_dir:
            state_path = Path(temp_dir) / "watchdog.json"
            state_path.write_text(json.dumps(initial, indent=2, sort_keys=True))
            before_bytes = state_path.read_bytes()
            slack = FakeSlack()
            with (
                mock.patch.object(m, "STATE_PATH", state_path),
                mock.patch.object(m.time, "time", return_value=NOW),
                mock.patch.object(m, "kanban_reviewer_lanes", return_value=rows or []),
                mock.patch.object(m, "slack_reaction", slack),
            ):
                self.assertEqual(m.main(invocation), 0)
            return slack, json.loads(state_path.read_text()), before_bytes, state_path.read_bytes()

    def test_boris_only_clear_does_not_release_a_held_deferred_legacy_post(self):
        # Removing this channel fence would clear the hold, retry the pending
        # removals, and transition this legacy post despite the Boris-only run.
        initial = m.migrate_state(None, NOW, backfill=True)[0]
        initial["machine_baseline_at"] = 0
        initial["cursor"]["last_list_at"] = NOW - 1
        initial["posts"][PERMALINK_B] = {
            "emoji": "review-no", "applied_at": NOW - 600,
            "pending_removals": ["thinkies", "white_check_mark"],
            "lanes": {"t_legacy": "doge_notes"}, "want": "doge_notes",
            "deferred_at": NOW - 60,
            "unresolved_lanes": {
                "t_held": m.hold_provenance(m.HOLD_KIND_UNSHOWABLE, "show failed"),
            },
        }
        legacy_before = json.loads(json.dumps(initial["posts"][PERMALINK_B]))

        slack, after, before_bytes, after_bytes = self.run_persisted_main(
            initial,
            ["--channel", BORIS_CHANNEL, "--clear-unresolved", PERMALINK_B],
        )

        self.assertEqual(slack.calls, [])
        self.assertEqual(after["posts"][PERMALINK_B], legacy_before)
        self.assertEqual(after_bytes, before_bytes)

    def test_boris_only_does_not_sweep_a_due_reconciliation_legacy_post(self):
        # Removing this channel fence would run reconciliation against legacy
        # reactions and advance its persisted reconciliation schedule.
        initial = m.migrate_state(None, NOW, backfill=True)[0]
        initial["machine_baseline_at"] = 0
        initial["cursor"]["last_list_at"] = NOW - 1
        initial["posts"][PERMALINK_B] = {
            "emoji": "review-no", "applied_at": NOW - 600,
            "pending_removals": [], "lanes": {"t_legacy": "review-no"},
            "reconcile_sweeps": 0, "reconcile_due_at": NOW,
        }
        legacy_before = json.loads(json.dumps(initial["posts"][PERMALINK_B]))

        slack, after, before_bytes, after_bytes = self.run_persisted_main(
            initial, ["--channel", BORIS_CHANNEL]
        )

        self.assertEqual(slack.calls, [])
        self.assertEqual(after["posts"][PERMALINK_B], legacy_before)
        self.assertEqual(after_bytes, before_bytes)

    def test_boris_only_does_not_normalize_an_excluded_current_schema_repair_record(self):
        # A current-schema ledger can still contain a sparse repair record. Channel
        # fencing must precede restart normalization, which otherwise adds fields
        # and rewrites this excluded-only ledger despite making no Slack calls.
        initial = m.migrate_state(None, NOW, backfill=True)[0]
        initial["machine_baseline_at"] = 0
        initial["cursor"]["last_list_at"] = NOW - 1
        initial["posts"][PERMALINK_B] = {
            "emoji": "review-no",
            "applied_at": NOW - 600,
            "last_error": "legacy repair still pending",
        }
        legacy_before = json.loads(json.dumps(initial["posts"][PERMALINK_B]))

        slack, after, before_bytes, after_bytes = self.run_persisted_main(
            initial, ["--channel", BORIS_CHANNEL]
        )

        self.assertEqual(slack.calls, [])
        self.assertEqual(after["posts"][PERMALINK_B], legacy_before)
        self.assertEqual(after_bytes, before_bytes)

    def test_boris_only_does_not_mark_an_existing_excluded_legacy_post_complete(self):
        # An already-present post represents this legacy approval, but a narrowed
        # run must not persist that bookkeeping for an excluded channel.
        legacy_key = f"t_legacy:{PERMALINK_B}"
        legacy_reacted = {
            legacy_key: {
                "at": NOW - 600,
                "permalink": PERMALINK_B,
                "reason": "legacy approval fixture",
                "task_id": "t_legacy",
            },
        }
        legacy_post = {
            "emoji": m.EMOJI_APPROVED,
            "applied_at": NOW - 600,
            "pending_removals": [],
            "lanes": {"t_legacy": m.EMOJI_APPROVED},
            "source": "legacy_reacted",
        }
        initial = {
            "schema_version": m.STATE_SCHEMA_VERSION - 1,
            "baseline_completed_at": NOW - 1200,
            "machine_baseline_at": NOW - 1200,
            "posts": {PERMALINK_B: legacy_post},
            "reacted": legacy_reacted,
            "failures": {},
            "cursor": {"seen": {}, "attempts": {}, "last_list_at": NOW - 1},
        }

        slack, after, before_bytes, after_bytes = self.run_persisted_main(
            initial, ["--channel", BORIS_CHANNEL]
        )

        self.assertEqual(slack.calls, [])
        self.assertNotIn("legacy_reacted_completed", after)
        self.assertEqual(after["posts"][PERMALINK_B], legacy_post)
        self.assertEqual(after["reacted"], legacy_reacted)
        self.assertEqual(after, initial)
        self.assertEqual(after_bytes, before_bytes)

    def test_boris_only_legacy_migration_defers_excluded_approval_history_for_explicit_legacy_run(self):
        legacy_reacted = {
            f"t_legacy:{PERMALINK_B}": {
                "at": NOW - 600,
                "permalink": PERMALINK_B,
                "reason": "legacy approval fixture",
                "task_id": "t_legacy",
            },
        }
        initial = {
            "baseline_completed_at": NOW - 1200,
            "reacted": legacy_reacted,
        }

        with tempfile.TemporaryDirectory() as temp_dir:
            state_path = Path(temp_dir) / "watchdog.json"
            state_path.write_text(json.dumps(initial, indent=2, sort_keys=True))
            slack = FakeSlack()
            with (
                mock.patch.object(m, "STATE_PATH", state_path),
                mock.patch.object(m, "kanban_reviewer_lanes", return_value=[]),
                mock.patch.object(m, "slack_reaction", slack),
            ):
                with mock.patch.object(m.time, "time", return_value=NOW):
                    self.assertEqual(m.main(["--channel", BORIS_CHANNEL]), 0)
                after_boris = json.loads(state_path.read_text())
                self.assertEqual(after_boris["reacted"], legacy_reacted)
                self.assertNotIn(PERMALINK_B, after_boris["posts"])

                with mock.patch.object(m.time, "time", return_value=NOW + 1):
                    self.assertEqual(m.main(["--channel", OTHER_WATCHED]), 0)

            after_all = json.loads(state_path.read_text())

        self.assertEqual(slack.calls, [])
        self.assertIn(PERMALINK_B, after_all["posts"])
        self.assertEqual(after_all["posts"][PERMALINK_B], {
            "emoji": m.EMOJI_APPROVED,
            "applied_at": NOW - 600,
            "pending_removals": [],
            "lanes": {"t_legacy": m.EMOJI_APPROVED},
            "source": "legacy_reacted",
        })
        self.assertEqual(after_all["schema_version"], m.STATE_SCHEMA_VERSION)

    def test_deferred_migration_does_not_reseed_selected_legacy_history_after_compaction(self):
        compaction_trigger = (
            f"https://ylopo.slack.com/archives/{BORIS_CHANNEL}/p1785000000000004"
        )
        selected_key = f"t_selected:{BORIS_PERMALINK}"
        excluded_key = f"t_excluded:{PERMALINK_B}"
        legacy_reacted = {
            selected_key: {
                "at": NOW - m.POST_RETENTION_SECONDS - 10,
                "permalink": BORIS_PERMALINK,
                "reason": "selected legacy approval",
                "task_id": "t_selected",
            },
            excluded_key: {
                "at": NOW - 600,
                "permalink": PERMALINK_B,
                "reason": "excluded legacy approval",
                "task_id": "t_excluded",
            },
        }
        initial = {
            "baseline_completed_at": NOW - m.POST_RETENTION_SECONDS - 60,
            "reacted": legacy_reacted,
        }
        trigger_row = lane(
            "t_compaction_trigger",
            "running",
            permalink=compaction_trigger,
            created_at=NOW + 1,
        )

        with tempfile.TemporaryDirectory() as temp_dir:
            state_path = Path(temp_dir) / "watchdog.json"
            state_path.write_text(json.dumps(initial, indent=2, sort_keys=True))
            slack = FakeSlack()
            with (
                mock.patch.object(m, "STATE_PATH", state_path),
                mock.patch.object(
                    m,
                    "kanban_reviewer_lanes",
                    side_effect=[[], [trigger_row], [], []],
                ),
                mock.patch.object(m, "slack_reaction", slack),
            ):
                with mock.patch.object(m.time, "time", return_value=NOW):
                    self.assertEqual(m.main(["--channel", BORIS_CHANNEL]), 0)
                after_seed = json.loads(state_path.read_text())
                self.assertIn(BORIS_PERMALINK, after_seed["posts"])
                self.assertNotIn(PERMALINK_B, after_seed["posts"])

                with mock.patch.object(m.time, "time", return_value=NOW + 1):
                    self.assertEqual(m.main(["--channel", BORIS_CHANNEL]), 0)
                after_compaction = json.loads(state_path.read_text())
                self.assertNotIn(BORIS_PERMALINK, after_compaction["posts"])
                self.assertIn(compaction_trigger, after_compaction["posts"])

                with mock.patch.object(m.time, "time", return_value=NOW + 2):
                    self.assertEqual(m.main(["--channel", BORIS_CHANNEL]), 0)
                with mock.patch.object(m.time, "time", return_value=NOW + 3):
                    self.assertEqual(
                        m.main(
                            [
                                "--channel",
                                BORIS_CHANNEL,
                                "--channel",
                                OTHER_WATCHED,
                            ]
                        ),
                        0,
                    )

            after_all = json.loads(state_path.read_text())

        self.assertNotIn(BORIS_PERMALINK, after_all["posts"])
        self.assertEqual(after_all["posts"][PERMALINK_B], {
            "emoji": m.EMOJI_APPROVED,
            "applied_at": NOW - 600,
            "pending_removals": [],
            "lanes": {"t_excluded": m.EMOJI_APPROVED},
            "source": "legacy_reacted",
        })
        self.assertEqual(after_all["reacted"], legacy_reacted)
        self.assertEqual(after_all["schema_version"], m.STATE_SCHEMA_VERSION)
        self.assertEqual({permalink for permalink, _emoji, _remove in slack.calls}, {
            compaction_trigger,
        })

    def test_boris_only_processes_selected_persisted_repairs_without_mutating_legacy_records(self):
        initial = m.migrate_state(None, NOW, backfill=True)[0]
        initial["machine_baseline_at"] = 0
        initial["posts"] = {
            BORIS_PERMALINK: {
                "emoji": "rubbing", "applied_at": NOW - 600,
                "pending_removals": ["thinkies"],
                "lanes": {"t_boris": "rubbing"},
            },
            PERMALINK_B: {
                "emoji": "review-no", "applied_at": NOW - 600,
                "pending_removals": ["thinkies"],
                "lanes": {"t_legacy": "doge_notes"}, "want": "doge_notes",
                "deferred_at": NOW - 60,
                "unresolved_lanes": {
                    "t_held": m.hold_provenance(m.HOLD_KIND_UNSHOWABLE, "show failed"),
                },
                "reconcile_sweeps": 0, "reconcile_due_at": NOW,
                "opaque": {"keep": ["deeply", "equal"]},
            },
        }
        legacy_before = json.loads(json.dumps(initial["posts"][PERMALINK_B]))

        slack, after, _before_bytes, _after_bytes = self.run_persisted_main(
            initial, ["--channel", BORIS_CHANNEL]
        )

        self.assertEqual(slack.calls, [
            (BORIS_PERMALINK, "rubbing", False),
            (BORIS_PERMALINK, "thinkies", True),
        ])
        self.assertEqual(after["posts"][BORIS_PERMALINK]["pending_removals"], [])
        self.assertEqual(after["posts"][PERMALINK_B], legacy_before)

    @staticmethod
    def legacy_pending_state():
        initial = m.migrate_state(None, NOW, backfill=True)[0]
        initial["machine_baseline_at"] = 0
        initial["posts"][PERMALINK_B] = {
            "emoji": "review-no", "applied_at": NOW - 600,
            "pending_removals": ["thinkies"],
            "lanes": {"t_legacy": "review-no"},
        }
        return initial

    @staticmethod
    def legacy_due_quarantine_state():
        initial = m.migrate_state(None, NOW, backfill=True)[0]
        initial["machine_baseline_at"] = 0
        initial["posts"][PERMALINK_B] = {
            "emoji": "review-no",
            "applied_at": NOW - 600,
            "pending_removals": ["thinkies"],
            "lanes": {"t_legacy": "review-no"},
            "deferred_at": NOW - 600,
            # Deliberately sparse/malformed legacy provenance: main-level restart
            # normalization changes this if its default channel fence regresses,
            # while process_tick refreshes alerted_at if its own fence regresses.
            "unresolved_lanes": ["quarantined legacy lane"],
            "alerted_at": NOW - m.UNRESOLVED_ALERT_SECONDS,
            "opaque": {"preserve": ["exactly", "as-is"]},
        }
        return initial

    def test_scheduled_default_preserves_due_legacy_repair(self):
        initial = self.legacy_due_quarantine_state()
        legacy_post_before = json.loads(json.dumps(initial["posts"][PERMALINK_B]))
        legacy_reacted_before = json.loads(json.dumps(initial["reacted"]))
        slack, after, _before_bytes, _after_bytes = self.run_persisted_main(initial, [])

        self.assertEqual(slack.calls, [])
        self.assertEqual(after["posts"][PERMALINK_B], legacy_post_before)
        self.assertEqual(
            after["posts"][PERMALINK_B]["alerted_at"],
            NOW - m.UNRESOLVED_ALERT_SECONDS,
        )
        self.assertEqual(after["reacted"], legacy_reacted_before)

    def test_explicit_legacy_channel_still_repairs_due_legacy_record(self):
        initial = self.legacy_pending_state()
        slack, after, _before_bytes, _after_bytes = self.run_persisted_main(
            initial, ["--channel", OTHER_WATCHED]
        )

        self.assertEqual(slack.calls, [
            (PERMALINK_B, "review-no", False),
            (PERMALINK_B, "thinkies", True),
        ])
        self.assertEqual(after["posts"][PERMALINK_B]["pending_removals"], [])

    def test_only_task_main_fences_mixed_persisted_pending_and_due_work(self):
        selected_pending = f"https://ylopo.slack.com/archives/{BORIS_CHANNEL}/p1785000000000010"
        selected_due = f"https://ylopo.slack.com/archives/{BORIS_CHANNEL}/p1785000000000011"
        excluded_pending = f"https://ylopo.slack.com/archives/{BORIS_CHANNEL}/p1785000000000012"
        excluded_due = f"https://ylopo.slack.com/archives/{BORIS_CHANNEL}/p1785000000000013"
        excluded_compactable = (
            f"https://ylopo.slack.com/archives/{BORIS_CHANNEL}/p1785000000000014"
        )
        initial = m.migrate_state(None, NOW, backfill=True)[0]
        initial["cursor"]["seen"] = {
            "t_excluded": NOW - m.SEEN_RETENTION_SECONDS - 1,
        }
        initial["cursor"]["attempts"] = {
            "t_excluded": {"count": 2, "last_at": NOW - m.SEEN_RETENTION_SECONDS - 1},
        }
        initial["posts"] = {
            selected_pending: {
                "state": m.STATE_ACTIVE,
                "emoji": "rubbing",
                "applied_at": NOW - 600,
                "pending_removals": ["thinkies"],
                "lanes": {"t_selected": m.STATE_ACTIVE},
            },
            selected_due: {
                "state": m.STATE_BLOCKED,
                "emoji": "face_vomiting",
                "applied_at": NOW - 600,
                "pending_removals": [],
                "lanes": {"t_selected": m.STATE_BLOCKED},
                "reconcile_sweeps": 0,
                "reconcile_due_at": NOW,
            },
            excluded_pending: {
                "state": m.STATE_ACTIVE,
                "emoji": "rubbing",
                "applied_at": NOW - 600,
                "pending_removals": ["thinkies"],
                "lanes": {"t_excluded": m.STATE_ACTIVE},
                "opaque": {"keep": ["pending", "deeply", "equal"]},
            },
            excluded_due: {
                "state": m.STATE_BLOCKED,
                "emoji": "face_vomiting",
                "applied_at": NOW - 600,
                "pending_removals": [],
                "lanes": {"t_excluded": m.STATE_BLOCKED},
                "reconcile_sweeps": 0,
                "reconcile_due_at": NOW,
                "opaque": {"keep": ["due", "deeply", "equal"]},
            },
            excluded_compactable: {
                "state": m.STATE_APPROVED,
                "emoji": "blinkapproved",
                "applied_at": NOW - m.POST_RETENTION_SECONDS - 1,
                "pending_removals": [],
                "lanes": {"t_excluded": m.STATE_APPROVED},
                "opaque": {"keep": ["compaction", "deeply", "equal"]},
            },
        }
        excluded_before = {
            permalink: json.loads(json.dumps(initial["posts"][permalink]))
            for permalink in (excluded_pending, excluded_due, excluded_compactable)
        }
        excluded_seen_before = json.loads(json.dumps(initial["cursor"]["seen"]))
        excluded_attempts_before = json.loads(json.dumps(initial["cursor"]["attempts"]))

        slack, after, _before_bytes, _after_bytes = self.run_persisted_main(
            initial,
            ["--channel", BORIS_CHANNEL, "--only-task", "t_selected"],
        )

        selected_calls = [call for call in slack.calls if call[0] in {selected_pending, selected_due}]
        self.assertEqual(len(selected_calls), 8)
        self.assertEqual({call[0] for call in slack.calls}, {selected_pending, selected_due})
        self.assertEqual(after["posts"][selected_pending]["pending_removals"], [])
        self.assertEqual(after["posts"][selected_due]["reconcile_sweeps"], 1)
        for permalink, record in excluded_before.items():
            self.assertEqual(after["posts"][permalink], record)
        self.assertEqual(after["cursor"]["seen"], excluded_seen_before)
        self.assertEqual(after["cursor"]["attempts"], excluded_attempts_before)

    def test_only_task_excluded_only_repair_is_zero_call_and_byte_identical(self):
        excluded = f"https://ylopo.slack.com/archives/{BORIS_CHANNEL}/p1785000000000020"
        initial = m.migrate_state(None, NOW, backfill=True)[0]
        initial["cursor"]["last_list_at"] = NOW - 1
        initial["cursor"]["seen"] = {
            "t_other": NOW - m.SEEN_RETENTION_SECONDS - 1,
        }
        initial["posts"][excluded] = {
            "state": m.STATE_ACTIVE,
            "emoji": "rubbing",
            "applied_at": NOW - 600,
            "pending_removals": ["thinkies"],
            "lanes": {"t_other": m.STATE_ACTIVE},
            "opaque": {"keep": ["the", "whole", "record"]},
        }

        slack, after, before_bytes, after_bytes = self.run_persisted_main(
            initial, ["--only-task", "t_selected"]
        )

        self.assertEqual(slack.calls, [])
        self.assertEqual(after, initial)
        self.assertEqual(after_bytes, before_bytes)

    def test_only_task_missing_malformed_and_near_match_provenance_fail_closed(self):
        missing = f"https://ylopo.slack.com/archives/{BORIS_CHANNEL}/p1785000000000030"
        malformed = f"https://ylopo.slack.com/archives/{BORIS_CHANNEL}/p1785000000000031"
        near_match = f"https://ylopo.slack.com/archives/{BORIS_CHANNEL}/p1785000000000032"
        initial = m.migrate_state(None, NOW, backfill=True)[0]
        initial["cursor"]["last_list_at"] = NOW - 1
        initial["posts"] = {
            missing: {
                "state": m.STATE_ACTIVE,
                "emoji": "rubbing",
                "applied_at": NOW - 600,
                "pending_removals": ["thinkies"],
                "source": "free text mentioning t_selected is not lane provenance",
            },
            malformed: {
                "state": m.STATE_ACTIVE,
                "emoji": "rubbing",
                "applied_at": NOW - 600,
                "pending_removals": ["thinkies"],
                "lanes": ["t_selected"],
            },
            near_match: {
                "state": m.STATE_ACTIVE,
                "emoji": "rubbing",
                "applied_at": NOW - 600,
                "pending_removals": ["thinkies"],
                "lanes": {"t_selected_extra": m.STATE_ACTIVE},
            },
        }

        slack, after, before_bytes, after_bytes = self.run_persisted_main(
            initial, ["--only-task", "t_selected"]
        )

        self.assertEqual(slack.calls, [])
        self.assertEqual(after, initial)
        self.assertEqual(after_bytes, before_bytes)

    def test_only_task_selects_whole_multi_lane_post_for_precedence(self):
        permalink = f"https://ylopo.slack.com/archives/{BORIS_CHANNEL}/p1785000000000040"
        initial = m.migrate_state(None, NOW, backfill=True)[0]
        initial["posts"][permalink] = {
            "state": m.STATE_APPROVED,
            "emoji": "blinkapproved",
            "applied_at": NOW - 600,
            "pending_removals": ["thinkies"],
            "lanes": {
                "t_selected": m.STATE_APPROVED,
                "t_blocking_sibling": m.STATE_BLOCKED,
            },
        }

        slack, after, _before_bytes, _after_bytes = self.run_persisted_main(
            initial, ["--only-task", "t_selected"]
        )

        self.assertEqual(slack.calls[0], (permalink, "face_vomiting", False))
        self.assertEqual(after["posts"][permalink]["state"], m.STATE_BLOCKED)
        self.assertEqual(after["posts"][permalink]["lanes"], {
            "t_selected": m.STATE_APPROVED,
            "t_blocking_sibling": m.STATE_BLOCKED,
        })

    def test_only_task_does_not_clear_an_excluded_persisted_hold(self):
        excluded = f"https://ylopo.slack.com/archives/{BORIS_CHANNEL}/p1785000000000050"
        initial = m.migrate_state(None, NOW, backfill=True)[0]
        initial["cursor"]["last_list_at"] = NOW - 1
        initial["posts"][excluded] = {
            "state": m.STATE_ACTIVE,
            "emoji": "rubbing",
            "applied_at": NOW - 600,
            "pending_removals": [],
            "lanes": {"t_other": m.STATE_NOTES},
            "want": "doge_notes",
            "deferred_at": NOW - 60,
            "unresolved_lanes": {
                "t_other": m.hold_provenance(m.HOLD_KIND_UNSHOWABLE, "show failed"),
            },
        }

        slack, after, before_bytes, after_bytes = self.run_persisted_main(
            initial,
            ["--only-task", "t_selected", "--clear-unresolved", excluded],
        )

        self.assertEqual(slack.calls, [])
        self.assertEqual(after, initial)
        self.assertEqual(after_bytes, before_bytes)

    def test_scheduled_default_processes_boris_discovery_repair_and_reconciliation(self):
        pending = f"https://ylopo.slack.com/archives/{BORIS_CHANNEL}/p1785000000000060"
        due = f"https://ylopo.slack.com/archives/{BORIS_CHANNEL}/p1785000000000061"
        initial = m.migrate_state(None, NOW, backfill=True)[0]
        initial["posts"] = {
            pending: {
                "state": m.STATE_ACTIVE,
                "emoji": "rubbing",
                "applied_at": NOW - 600,
                "pending_removals": ["thinkies"],
                "lanes": {"t_pending": m.STATE_ACTIVE},
            },
            due: {
                "state": m.STATE_BLOCKED,
                "emoji": "face_vomiting",
                "applied_at": NOW - 600,
                "pending_removals": [],
                "lanes": {"t_due": m.STATE_BLOCKED},
                "reconcile_sweeps": 0,
                "reconcile_due_at": NOW,
            },
        }
        discovered = lane(
            "t_discovered",
            "running",
            permalink=BORIS_PERMALINK,
            created_at=NOW - 10,
        )

        slack, after, _before_bytes, _after_bytes = self.run_persisted_main(
            initial,
            [],
            rows=[discovered],
        )

        self.assertEqual(
            {call[0] for call in slack.calls},
            {BORIS_PERMALINK, pending, due},
        )
        self.assertIn((BORIS_PERMALINK, "rubbing"), slack.adds)
        self.assertEqual(after["posts"][BORIS_PERMALINK]["state"], m.STATE_ACTIVE)
        self.assertEqual(after["posts"][pending]["pending_removals"], [])
        self.assertEqual(after["posts"][due]["reconcile_sweeps"], 1)

    def test_only_task_defers_excluded_legacy_reacted_migration_bookkeeping(self):
        selected_key = f"t_selected:{BORIS_PERMALINK}"
        excluded_permalink = (
            f"https://ylopo.slack.com/archives/{BORIS_CHANNEL}/p1785000000000070"
        )
        excluded_key = f"t_other:{excluded_permalink}"
        initial = {
            "schema_version": m.STATE_SCHEMA_VERSION - 1,
            "baseline_completed_at": NOW - 1200,
            "machine_baseline_at": NOW - 1200,
            "posts": {},
            "reacted": {
                selected_key: {"at": NOW - 600, "task_id": "t_selected",
                               "permalink": BORIS_PERMALINK, "reason": "selected"},
                excluded_key: {"at": NOW - 600, "task_id": "t_other",
                               "permalink": excluded_permalink, "reason": "excluded"},
            },
            "failures": {},
            "cursor": {"seen": {}, "attempts": {}, "last_list_at": NOW - 1},
        }

        slack, after, _before_bytes, _after_bytes = self.run_persisted_main(
            initial, ["--only-task", "t_selected"]
        )

        self.assertEqual(slack.calls, [])
        self.assertIn(BORIS_PERMALINK, after["posts"])
        self.assertNotIn(excluded_permalink, after["posts"])
        self.assertEqual(after["legacy_reacted_completed"], [selected_key])
        self.assertEqual(after["schema_version"], m.STATE_SCHEMA_VERSION - 1)

    def test_only_task_excluded_only_sparse_legacy_state_is_byte_identical(self):
        excluded_permalink = (
            f"https://ylopo.slack.com/archives/{BORIS_CHANNEL}/p1785000000000071"
        )
        excluded_key = f"t_other:{excluded_permalink}"
        initial = {
            "baseline_completed_at": NOW - 1200,
            "reacted": {
                excluded_key: {
                    "at": NOW - 600,
                    "task_id": "t_other",
                    "permalink": excluded_permalink,
                    "reason": "excluded sparse legacy source",
                },
            },
        }

        slack, after, before_bytes, after_bytes = self.run_persisted_main(
            initial, ["--only-task", "t_selected"]
        )

        self.assertEqual(slack.calls, [])
        self.assertEqual(after, initial)
        self.assertEqual(after_bytes, before_bytes)

    def test_only_task_persists_reconciliation_that_compacts_its_last_selected_post(self):
        permalink = f"https://ylopo.slack.com/archives/{BORIS_CHANNEL}/p1785000000000072"
        initial = m.migrate_state(None, NOW, backfill=True)[0]
        initial["posts"][permalink] = {
            "state": m.STATE_BLOCKED,
            "emoji": "face_vomiting",
            "applied_at": NOW - m.POST_RETENTION_SECONDS - 1,
            "pending_removals": [],
            "lanes": {"t_selected": m.STATE_BLOCKED},
            "reconcile_sweeps": 0,
            "reconcile_due_at": NOW,
        }
        invocation = ["--only-task", "t_selected"]

        with tempfile.TemporaryDirectory() as temp_dir:
            state_path = Path(temp_dir) / "watchdog.json"
            state_path.write_text(json.dumps(initial, indent=2, sort_keys=True))
            slack = FakeSlack()
            with (
                mock.patch.object(m, "STATE_PATH", state_path),
                mock.patch.object(m, "kanban_reviewer_lanes", return_value=[]),
                mock.patch.object(m, "slack_reaction", slack),
            ):
                with mock.patch.object(m.time, "time", return_value=NOW):
                    self.assertEqual(m.main(invocation), 0)
                after_first = json.loads(state_path.read_text())
                calls_after_first = list(slack.calls)

                with mock.patch.object(m.time, "time", return_value=NOW + 1):
                    self.assertEqual(m.main(invocation), 0)
                after_second = json.loads(state_path.read_text())

        self.assertEqual(len(calls_after_first), 6)
        self.assertEqual({call[0] for call in calls_after_first}, {permalink})
        self.assertTrue(all(remove for _permalink, _emoji, remove in calls_after_first))
        self.assertNotIn(permalink, after_first["posts"])
        self.assertEqual(after_first["cursor"]["last_list_at"], NOW)
        self.assertEqual(slack.calls, calls_after_first)
        self.assertEqual(after_second, after_first)

    def test_only_task_fresh_discovery_fails_closed_on_malformed_persisted_hold(self):
        permalink = f"https://ylopo.slack.com/archives/{BORIS_CHANNEL}/p1785000000000080"
        initial = m.migrate_state(None, NOW, backfill=True)[0]
        initial["posts"][permalink] = {
            "state": m.STATE_ACTIVE,
            "emoji": "rubbing",
            "applied_at": NOW - 600,
            "pending_removals": ["thinkies"],
            "lanes": {"t_other": m.STATE_BLOCKED},
            "unresolved_lanes": ["t_selected"],
            "opaque": {"keep": ["fresh", "re-admission"]},
        }
        selected_row = lane(
            "t_selected",
            "running",
            permalink=permalink,
            created_at=NOW + 1,
        )

        slack, after, _before_bytes, _after_bytes = self.run_persisted_main(
            initial,
            ["--only-task", "t_selected"],
            rows=[selected_row],
        )

        self.assertEqual(slack.calls, [])
        record = after["posts"][permalink]
        self.assertEqual(m.hold_kind(record["unresolved_lanes"]["unknown"]),
                         m.HOLD_KIND_UNKNOWN)
        self.assertEqual(record["lanes"], {
            "t_other": m.STATE_BLOCKED,
            "t_selected": m.STATE_ACTIVE,
        })


class UnknownPersistedLaneTest(unittest.TestCase):
    def migrated_state(self):
        state = m.migrate_state(None, NOW, backfill=True)[0]
        state["posts"][PERMALINK] = {
            "emoji": m.EMOJI_BLOCK,
            "applied_at": NOW - 60,
            "pending_removals": [],
            "lanes": {
                "t_unknown": "future-review-state",
                "t_approved": m.EMOJI_APPROVED,
            },
        }
        return m.migrate_state(json_roundtrip(state), NOW + 1)[0]

    def test_unknown_persisted_lane_is_retained_and_holds_approved_sibling(self):
        state = self.migrated_state()
        record = state["posts"][PERMALINK]
        self.assertEqual(record["lanes"]["t_unknown"], "future-review-state")
        self.assertEqual(
            m.hold_kind(record["unresolved_lanes"]["t_unknown"]),
            m.HOLD_KIND_UNKNOWN,
        )

        held = tick([], {}, state=state, now=NOW + 2)
        record = state["posts"][PERMALINK]
        self.assertEqual(held["slack"].calls, [])
        self.assertEqual(record["emoji"], m.EMOJI_BLOCK)
        self.assertEqual(record["want"], m.EMOJI_APPROVED)
        self.assertEqual(record["lanes"]["t_unknown"], "future-review-state")

    def test_safe_reclassification_releases_unknown_persisted_lane(self):
        state = self.migrated_state()
        self.assertIn("t_unknown", state["posts"][PERMALINK]["unresolved_lanes"])
        rows = [lane("t_unknown", "done", completed_at=NOW + 100)]
        shows = {"t_unknown": show(
            "t_unknown",
            {"github_review_submitted": True, "github_review_state": "APPROVED", "findings": []},
            ended_at=NOW + 100,
        )}
        healed = tick(rows, shows, state=state, now=NOW + 200)
        self.assertEqual(healed["slack"].adds, [(PERMALINK, m.EMOJI_APPROVED)])
        self.assertEqual(healed["slack"].removes, [(PERMALINK, m.EMOJI_BLOCK)])
        record = state["posts"][PERMALINK]
        self.assertNotIn("unresolved_lanes", record)
        self.assertEqual(record["lanes"]["t_unknown"], m.EMOJI_APPROVED)

    def test_explicit_clear_releases_unknown_hold_and_survives_restart(self):
        state = self.migrated_state()
        released = tick(
            [],
            {},
            state=state,
            now=NOW + 2,
            clear_unresolved=[PERMALINK],
        )
        self.assertEqual(released["slack"].adds, [(PERMALINK, m.EMOJI_APPROVED)])
        self.assertEqual(released["slack"].removes, [(PERMALINK, m.EMOJI_BLOCK)])
        record = state["posts"][PERMALINK]
        self.assertNotIn("unresolved_lanes", record)
        self.assertEqual(record["lanes"]["t_unknown"], "future-review-state")
        self.assertIn("t_unknown", record["cleared_lanes"])

        restarted, _ = m.migrate_state(json_roundtrip(state), NOW + 3)
        self.assertNotIn("unresolved_lanes", restarted["posts"][PERMALINK])


# --- state migration / restart -------------------------------------------


class StateMigrationTest(unittest.TestCase):
    def legacy(self):
        return {
            "baseline_completed_at": 1783578010,
            "failures": {},
            "reacted": {
                f"t_1ba0f918:{PERMALINK}": {
                    "at": 1784133481,
                    "permalink": PERMALINK,
                    "reason": "run=966",
                    "task_id": "t_1ba0f918",
                },
            },
        }

    def test_legacy_state_becomes_a_known_white_check_mark_post(self):
        state, notes = m.migrate_state(self.legacy(), NOW)
        self.assertEqual(state["schema_version"], m.STATE_SCHEMA_VERSION)
        self.assertEqual(state["posts"][PERMALINK]["emoji"], m.EMOJI_APPROVED)
        self.assertEqual(state["posts"][PERMALINK]["lanes"], {"t_1ba0f918": m.EMOJI_APPROVED})
        self.assertEqual(state["baseline_completed_at"], 1783578010)  # legacy key preserved
        self.assertEqual(state["reacted"], self.legacy()["reacted"])  # rollback-compatible
        self.assertEqual(state["machine_baseline_at"], NOW)  # no historical backfill
        self.assertTrue(notes)

    def test_migration_is_idempotent(self):
        once, _ = m.migrate_state(self.legacy(), NOW)
        twice, notes = m.migrate_state(json_roundtrip(once), NOW + 500)
        self.assertEqual(notes, [])
        self.assertEqual(twice["posts"], once["posts"])
        self.assertEqual(twice["machine_baseline_at"], NOW)

    def test_migrated_post_is_not_re_reacted_on_restart(self):
        state, _ = m.migrate_state(self.legacy(), NOW)
        state["machine_baseline_at"] = 0
        rows = [lane("t_1ba0f918", "done")]
        shows = {"t_1ba0f918": show("t_1ba0f918", {
            "github_review_submitted": True, "github_review_state": "APPROVED", "findings": []})}
        result = tick(rows, shows, state=state)
        self.assertEqual(result["slack"].calls, [])
        self.assertEqual(result["metrics"]["posts_converged"], 1)

    def test_migrated_post_transitions_when_a_new_lane_goes_active(self):
        state, _ = m.migrate_state(self.legacy(), NOW)
        state["machine_baseline_at"] = 0
        result = tick([lane("t_new", "running")], {}, state=state)
        self.assertEqual(result["slack"].adds, [(PERMALINK, m.EMOJI_ACTIVE)])
        self.assertEqual(result["slack"].removes, [(PERMALINK, m.EMOJI_APPROVED)])

    def test_corrupt_state_is_rebuilt_not_fatal(self):
        for raw in ("junk", ["junk"], 42):
            state, _ = m.migrate_state(raw, NOW)
            self.assertEqual(state["posts"], {})
            self.assertEqual(state["schema_version"], m.STATE_SCHEMA_VERSION)

    def test_corrupt_post_records_are_dropped(self):
        raw = {
            "schema_version": m.STATE_SCHEMA_VERSION,
            "posts": {PERMALINK: {"emoji": "shrug"}, PERMALINK_B: "nope"},
        }
        state, _ = m.migrate_state(raw, NOW)
        self.assertEqual(state["posts"], {})

    def test_post_record_with_unmet_intent_survives_migration(self):
        raw = {
            "schema_version": m.STATE_SCHEMA_VERSION,
            "posts": {PERMALINK: {"emoji": None, "want": m.EMOJI_BLOCK, "last_error": "slack_down",
                                  "lanes": {"t_a": m.EMOJI_BLOCK}}},
        }
        state, _ = m.migrate_state(raw, NOW)
        self.assertIn(PERMALINK, state["posts"])

    def test_state_loss_converges_to_exactly_one_reaction(self):
        rows = [lane("t_a", "done")]
        shows = {"t_a": show("t_a", {"verdict": "NEEDS_FIX"})}
        fresh = m.migrate_state(None, NOW)[0]
        fresh["machine_baseline_at"] = 0
        result = tick(rows, shows, state=fresh)
        self.assertEqual(result["slack"].adds, [(PERMALINK, m.EMOJI_BLOCK)])
        self.assertEqual(
            [emoji for _, emoji in result["slack"].removes],
            [m.EMOJI_COMMENT, m.EMOJI_ACTIVE, m.EMOJI_APPROVED],
        )

    def test_approved_transition_updates_the_legacy_ledger(self):
        state = m.migrate_state(None, NOW)[0]
        state["machine_baseline_at"] = 0
        rows = [lane("t_a", "done")]
        shows = {"t_a": show("t_a", {
            "github_review_submitted": True, "github_review_state": "APPROVED", "findings": []})}
        tick(rows, shows, state=state)
        self.assertIn(f"t_a:{PERMALINK}", state["reacted"])

    def test_compaction_bounds_cursor_and_posts(self):
        state = m.migrate_state(None, NOW)[0]
        state["cursor"]["seen"] = {"t_old": NOW - m.SEEN_RETENTION_SECONDS - 10, "t_new": NOW - 10}
        state["posts"] = {
            PERMALINK: {"emoji": m.EMOJI_APPROVED, "applied_at": NOW - m.POST_RETENTION_SECONDS - 10,
                        "pending_removals": [], "lanes": {}},
            PERMALINK_B: {"emoji": m.EMOJI_APPROVED, "applied_at": NOW - 10, "pending_removals": [], "lanes": {}},
        }
        m.compact_state(state, NOW)
        self.assertEqual(list(state["cursor"]["seen"]), ["t_new"])
        self.assertEqual(list(state["posts"]), [PERMALINK_B])


# --- Boris REJECT t_397531f0 regressions ----------------------------------


class FailClosedClassifierTest(unittest.TestCase):
    """Blocker 1: a malformed blocking surface must never lose to an approval flag.

    Every metadata shape here was named in the REJECT as classifying (wrongly) as
    :white_check_mark: against the reviewed artifact.
    """

    def status(self, metadata):
        return m.classify_run_status({"metadata": metadata})[:2]

    # -- malformed blocking surfaces beat co-located approval flags --

    def test_string_findings_naming_a_blocker_beat_approved_flags(self):
        self.assertEqual(
            self.status({"findings": "BLOCKER: unsafe", "github_review_submitted": True,
                         "github_review_state": "APPROVED", "approved": True}),
            ("signal", m.STATE_BLOCKED),
        )

    def test_string_review_container_needs_fix_beats_approved_flags(self):
        self.assertEqual(
            self.status({"review": "NEEDS_FIX", "github_review_submitted": True,
                         "github_review_state": "APPROVED", "approved": True}),
            ("signal", m.STATE_BLOCKED),
        )

    def test_dict_shaped_findings_naming_a_blocker_beat_approved_flags(self):
        self.assertEqual(
            self.status({"findings": {"1": "BLOCKER: drops rows"}, "approved_by_chadtao206": True}),
            ("signal", m.STATE_BLOCKED),
        )

    def test_string_blocker_surface_blocks_even_without_blocking_vocabulary(self):
        # `blockers` is a blocker list by name: any non-empty malformed value blocks.
        self.assertEqual(
            self.status({"blockers": "see the thread", "github_review_state": "APPROVED",
                         "github_review_id": 5}),
            ("signal", m.STATE_BLOCKED),
        )

    def test_benign_empty_malformed_surface_is_not_a_block(self):
        # "none"/""/0 say nothing at all and must not manufacture a block.
        for empty in ("none", "", "n/a", "None", 0, False, [], {}):
            self.assertEqual(
                self.status({"findings": empty, "github_review_submitted": True,
                             "github_review_state": "APPROVED"}),
                ("signal", m.STATE_APPROVED), empty,
            )

    # -- approval claims contradicted by a veto --

    def test_nested_posted_false_approval_evidence_quarantines(self):
        self.assertEqual(
            self.status({"approval": {"submitted": True, "review_id": 123, "state": "APPROVED",
                                      "posted": False}, "findings": []}),
            ("quarantine", None),
        )

    def test_approval_status_submitted_with_posted_false_quarantines(self):
        self.assertEqual(
            self.status({"approval_status": "submitted_and_refetched", "posted": False,
                         "findings": []}),
            ("quarantine", None),
        )

    def test_approval_status_submitted_without_a_veto_still_approves(self):
        # The veto is what quarantines, not the submitted claim itself.
        self.assertEqual(
            self.status({"approval_status": "submitted_and_refetched", "findings": []}),
            ("signal", m.STATE_APPROVED),
        )

    def test_every_posted_alias_vetoes(self):
        for key in m.POSTED_FLAG_KEYS:
            self.assertEqual(
                self.status({"github_review_submitted": True, "github_review_state": "APPROVED",
                             "findings": [], key: False}),
                ("quarantine", None), key,
            )

    def test_deeply_nested_posted_false_still_vetoes(self):
        self.assertEqual(
            self.status({"approved": True, "findings": [], "github_review_id": 4797653504,
                         "github_review_state": "APPROVED",
                         "github_review_action": {"posted": False, "review_state": "APPROVED"}}),
            ("quarantine", None),
        )

    def test_the_same_shape_without_the_veto_approves(self):
        self.assertEqual(
            self.status({"approved": True, "findings": [], "github_review_id": 4797653504,
                         "github_review_state": "APPROVED",
                         "github_review_action": {"posted": True, "review_state": "APPROVED"}}),
            ("signal", m.STATE_APPROVED),
        )

    def test_posted_false_outside_review_scope_does_not_veto(self):
        # A `posted: false` on something that is not a review (a Slack post, say)
        # must not quarantine an otherwise clean approval.
        self.assertEqual(
            self.status({"github_review_submitted": True, "github_review_state": "APPROVED",
                         "findings": [], "slack_summary": {"posted": False, "channel": "C1"}}),
            ("signal", m.STATE_APPROVED),
        )

    # -- all aliases validated together, contradictions included --

    def test_contradictory_aliases_resolve_to_the_blocking_one(self):
        self.assertEqual(
            self.status({"review": {"verdict": "NEEDS_FIX"},
                         "github_review": {"state": "APPROVED", "id": 5, "submitted": True}}),
            ("signal", m.STATE_BLOCKED),
        )

    def test_contradictory_aliases_are_not_first_hit_wins(self):
        # Same contradiction, opposite key order: the verdict must still win.
        self.assertEqual(
            self.status({"github_review": {"state": "APPROVED", "id": 5, "submitted": True},
                         "review": {"verdict": "NEEDS_FIX"}}),
            ("signal", m.STATE_BLOCKED),
        )

    def test_commented_alias_beside_an_approved_alias_shows_comments(self):
        # doge_notes outranks white_check_mark, so the readable comment state wins.
        self.assertEqual(
            self.status({"github_review": {"state": "COMMENTED", "id": 5},
                         "approval": {"submitted": True, "review_id": 7, "state": "APPROVED"},
                         "findings": []}),
            ("signal", m.STATE_NOTES),
        )

    def test_changes_requested_alias_beside_an_approved_alias_blocks(self):
        self.assertEqual(
            self.status({"github_review": {"state": "CHANGES_REQUESTED", "id": 5},
                         "approval": {"submitted": True, "review_id": 7, "state": "APPROVED"}}),
            ("signal", m.STATE_BLOCKED),
        )

    def test_nested_blocker_list_in_any_alias_blocks(self):
        for alias in m.ALL_CONTAINER_KEYS:
            self.assertEqual(
                self.status({alias: {"blockers": ["unsafe"]}, "approved_by_chadtao206": True}),
                ("signal", m.STATE_BLOCKED), alias,
            )

    def test_bare_state_done_is_lane_bookkeeping_not_a_review_state(self):
        # `state: "done"` must neither approve nor veto: it is not review vocabulary.
        self.assertEqual(
            self.status({"state": "done", "github_review_submitted": True,
                         "github_review_state": "APPROVED", "findings": []}),
            ("signal", m.STATE_APPROVED),
        )
        self.assertEqual(self.status({"state": "done"}), ("none", None))

    def test_unreadable_surfaces_quarantine_rather_than_approve(self):
        for bad in ({"review": 7}, {"findings": object()}, {"github_review": 3.5}):
            metadata = {"github_review_submitted": True, "github_review_state": "APPROVED", **bad}
            self.assertEqual(self.status(metadata), ("quarantine", None), bad)

    def test_an_unreadable_blocker_surface_blocks_rather_than_quarantines(self):
        # `blockers` is a blocker list by name: anything non-empty in it blocks,
        # whatever its shape. review-no outranks quarantine, so this is stricter.
        self.assertEqual(
            self.status({"blockers": object(), "github_review_submitted": True,
                         "github_review_state": "APPROVED"}),
            ("signal", m.STATE_BLOCKED),
        )

    def test_list_shaped_review_container_naming_a_block_still_blocks(self):
        # Readable content in an unexpected shape is classified, not quarantined.
        self.assertEqual(
            self.status({"review": ["NEEDS_FIX"], "github_review_submitted": True,
                         "github_review_state": "APPROVED"}),
            ("signal", m.STATE_BLOCKED),
        )

    def test_task_approval_does_not_resurrect_an_older_approval(self):
        # The retained lane-level helper must fail closed the same way the reaction
        # path does: the baseline returned the first approval found in ANY run.
        payload = {
            "task": {"id": "t_1", "status": "done", "assignee": "reviewer"},
            "runs": [
                {"id": "old", "ended_at": NOW - 900, "metadata": {
                    "github_review_submitted": True, "github_review_state": "APPROVED",
                    "findings": []}},
                {"id": "new", "ended_at": NOW - 60, "metadata": {"worker_session_id": "abc"}},
            ],
        }
        self.assertFalse(m.task_approval(payload)[0])
        payload["runs"][1]["metadata"] = {"verdict": "NEEDS_FIX"}
        self.assertFalse(m.task_approval(payload)[0])

    def test_task_approval_still_reports_a_clean_newest_approval(self):
        payload = show("t_1", {"github_review_submitted": True,
                               "github_review_state": "APPROVED", "findings": []})
        self.assertTrue(m.task_approval(payload)[0])

    def test_run_indicates_approval_agrees_with_the_status_axis(self):
        # The retained legacy entry point must fail closed identically.
        for metadata in (
            {"findings": "BLOCKER: unsafe", "github_review_submitted": True,
             "github_review_state": "APPROVED"},
            {"approval_status": "submitted_and_refetched", "posted": False, "findings": []},
            {"review": {"verdict": "NEEDS_FIX"}, "github_review": {"state": "APPROVED", "id": 5,
                                                                   "submitted": True}},
            {"review": 7, "github_review_submitted": True, "github_review_state": "APPROVED"},
        ):
            self.assertFalse(m.run_indicates_approval({"metadata": metadata}), metadata)


class NestedAliasFailClosedTest(unittest.TestCase):
    """Re-review blocker: alias validation was only one level deep.

    ``_labelled_containers`` returned the metadata root plus *direct* top-level
    aliases, so a known review/approval alias nested inside another known alias
    (``review.approval``, ``review.github_review``, ``approval.review``) was
    invisible to every classifier surface. Both probes in the REJECT classified as
    :white_check_mark: with ``run_indicates_approval=True``.
    """

    APPROVED_TOP = {"github_review_submitted": True, "github_review_state": "APPROVED"}

    def status(self, metadata):
        return m.classify_run_status({"metadata": metadata})[:2]

    def assert_never_approves(self, metadata, msg=None):
        """All three surfaces must agree that this metadata is not a clean approval."""
        label = msg or repr(metadata)
        status, emoji = self.status(metadata)
        self.assertIn(status, ("signal", "quarantine"), label)
        self.assertNotEqual(emoji, m.STATE_APPROVED, label)
        run_row = {"id": "r", "profile": "reviewer", "ended_at": NOW - 60, "metadata": metadata}
        self.assertFalse(m.run_indicates_approval(run_row), label)
        payload = {"task": {"id": "t_n", "status": "done", "assignee": "reviewer"},
                   "runs": [run_row]}
        self.assertFalse(m.task_approval(payload)[0], label)
        lane_emoji = m.classify_task(payload)[0]
        self.assertNotEqual(lane_emoji, m.STATE_APPROVED, label)
        if status == "quarantine":
            self.assertEqual(lane_emoji, m.QUARANTINE, label)
        else:
            self.assertEqual(lane_emoji, emoji, label)

    def assert_approves(self, metadata, msg=None):
        """The control direction: a clean nested approval still reaches ✅ on all three."""
        label = msg or repr(metadata)
        self.assertEqual(self.status(metadata), ("signal", m.STATE_APPROVED), label)
        run_row = {"id": "r", "profile": "reviewer", "ended_at": NOW - 60, "metadata": metadata}
        self.assertTrue(m.run_indicates_approval(run_row), label)
        payload = {"task": {"id": "t_n", "status": "done", "assignee": "reviewer"},
                   "runs": [run_row]}
        self.assertTrue(m.task_approval(payload)[0], label)
        self.assertEqual(m.classify_task(payload)[0], m.STATE_APPROVED, label)

    # -- the two probes named in the re-review REJECT --

    def test_reject_probe_nested_review_approval_hides_a_blocker_finding(self):
        self.assert_never_approves(
            {"github_review_submitted": True, "github_review_state": "APPROVED",
             "review": {"approval": {"findings": ["BLOCKER: unsafe migration"]}}},
            "probe 1: review.approval.findings blocker",
        )
        self.assertEqual(
            self.status({"github_review_submitted": True, "github_review_state": "APPROVED",
                         "review": {"approval": {"findings": ["BLOCKER: unsafe migration"]}}}),
            ("signal", m.STATE_BLOCKED),
        )

    def test_reject_probe_nested_review_approval_non_approved_state(self):
        self.assert_never_approves(
            {"github_review_submitted": True, "github_review_state": "APPROVED",
             "review": {"approval": {"state": "CHANGES_REQUESTED"}}},
            "probe 2: review.approval.state CHANGES_REQUESTED",
        )

    # -- every alias-inside-alias pair, both orders --

    def test_a_blocker_list_in_any_nested_alias_pair_blocks(self):
        for outer in m.ALL_CONTAINER_KEYS:
            for inner in m.ALL_CONTAINER_KEYS:
                metadata = {**self.APPROVED_TOP, "approved_by_chadtao206": True,
                            outer: {inner: {"blockers": ["unsafe migration"]}}}
                self.assertEqual(self.status(metadata), ("signal", m.STATE_BLOCKED),
                                 f"{outer}.{inner}.blockers")
                self.assert_never_approves(metadata, f"{outer}.{inner}.blockers")

    def test_a_blocking_verdict_in_any_nested_alias_pair_blocks(self):
        for outer in m.ALL_CONTAINER_KEYS:
            for inner in m.ALL_CONTAINER_KEYS:
                metadata = {**self.APPROVED_TOP, "findings": [],
                            outer: {inner: {"verdict": "NEEDS_FIX"}}}
                self.assertEqual(self.status(metadata), ("signal", m.STATE_BLOCKED),
                                 f"{outer}.{inner}.verdict")

    def test_nesting_is_not_insertion_order_dependent(self):
        # Same contradiction, the nested alias declared before/after the approval
        # flags, and the blocking key declared before/after the approving one.
        nested_first = {"review": {"approval": {"blockers": ["unsafe"], "state": "APPROVED",
                                               "review_id": 9, "submitted": True}},
                        **self.APPROVED_TOP}
        nested_last = {**self.APPROVED_TOP,
                       "review": {"approval": {"state": "APPROVED", "review_id": 9,
                                               "submitted": True, "blockers": ["unsafe"]}}}
        for metadata in (nested_first, nested_last):
            self.assertEqual(self.status(metadata), ("signal", m.STATE_BLOCKED), metadata)
            self.assert_never_approves(metadata)

    def test_container_labels_are_independent_of_dict_insertion_order(self):
        # The authoritative set is enumerated breadth-first over ALL_CONTAINER_KEYS,
        # so two dicts with the same aliases yield the same labels in the same order.
        forward = {"review": {"approval": {}}, "github_review": {}, "approval": {}}
        reverse = {"approval": {}, "github_review": {}, "review": {"approval": {}}}
        labels = [label for label, _c in m._labelled_containers(forward)]
        self.assertEqual(labels, [label for label, _c in m._labelled_containers(reverse)])
        self.assertIn("review.approval.", labels)

    # -- nested non-APPROVED states never resolve into an approval --

    def test_every_state_alias_nested_in_review_vetoes_or_blocks(self):
        for key in m.GITHUB_STATE_KEYS:
            for value in ("CHANGES_REQUESTED", "COMMENTED", "DISMISSED", "PENDING", "REJECTED"):
                metadata = {**self.APPROVED_TOP, "findings": [],
                            "review": {"approval": {key: value, "review_id": 11,
                                                    "submitted": True}}}
                self.assert_never_approves(metadata, f"review.approval.{key}={value}")

    def test_a_nested_non_approved_state_is_reported_as_a_veto(self):
        metadata = {**self.APPROVED_TOP, "findings": [],
                    "review": {"github_review": {"review_state": "DISMISSED"}}}
        self.assertIn("review.github_review.review_state=DISMISSED",
                      m.approval_vetoes(metadata))

    def test_a_nested_bare_state_outside_review_vocabulary_neither_approves_nor_vetoes(self):
        # `state: "done"` is lane bookkeeping at any depth: it must not manufacture
        # a veto, and it must not approve on its own either.
        self.assert_approves({**self.APPROVED_TOP, "findings": [],
                              "review": {"approval": {"state": "done"}}})
        self.assertEqual(self.status({"review": {"approval": {"state": "done"}}}), ("none", None))

    def test_nested_posted_false_still_vetoes_a_nested_approval(self):
        self.assert_never_approves(
            {"findings": [], "review": {"approval": {"state": "APPROVED", "review_id": 13,
                                                     "submitted": True, "posted": False}}},
        )

    # -- malformed nested surfaces fail closed rather than approve --

    def test_an_unreadable_nested_alias_quarantines(self):
        for bad in (7, 3.5, object(), (1, 2)):
            metadata = {**self.APPROVED_TOP, "findings": [], "review": {"approval": bad}}
            self.assertEqual(self.status(metadata), ("quarantine", None), bad)
            self.assert_never_approves(metadata, f"review.approval={bad!r}")

    def test_an_unreadable_nested_finding_surface_quarantines(self):
        metadata = {**self.APPROVED_TOP, "review": {"approval": {"findings": object()}}}
        self.assertEqual(self.status(metadata), ("quarantine", None))
        self.assert_never_approves(metadata)

    def test_a_readable_blocking_nested_finding_surface_blocks(self):
        metadata = {**self.APPROVED_TOP,
                    "review": {"approval": {"findings": "BLOCKER: drops rows"}}}
        self.assertEqual(self.status(metadata), ("signal", m.STATE_BLOCKED))

    def test_a_readable_nested_comment_surface_shows_comments(self):
        metadata = {**self.APPROVED_TOP,
                    "review": {"approval": {"review_comments": "two nits in the migration"}}}
        self.assertEqual(self.status(metadata), ("signal", m.STATE_NOTES))

    def test_a_nested_blocker_surface_names_its_full_path(self):
        reasons = [r for _kind, r in m.malformed_surfaces(
            {"review": {"approval": {"blockers": "see the thread"}}})]
        self.assertIn("review.approval.blockers non-list non-empty blocker surface", reasons)

    # -- the depth bound itself fails closed --

    def test_an_alias_chain_at_the_depth_bound_is_still_validated(self):
        chain = {"blockers": ["unsafe"]}
        for key in ("github_review", "approval", "review")[:m.CONTAINER_NEST_DEPTH]:
            chain = {key: chain}
        metadata = {**self.APPROVED_TOP, "approved": True, **chain}
        self.assertEqual(self.status(metadata), ("signal", m.STATE_BLOCKED))
        self.assert_never_approves(metadata)

    def test_an_alias_chain_past_the_depth_bound_quarantines_rather_than_approves(self):
        # A finite bound has to fail closed too, or it is the same defect deeper.
        chain = {"blockers": ["unsafe"]}
        for _ in range(m.CONTAINER_NEST_DEPTH + 1):
            chain = {"review": chain}
        metadata = {**self.APPROVED_TOP, "findings": [], **chain}
        status, emoji = self.status(metadata)
        self.assertEqual((status, emoji), ("quarantine", None))
        self.assertIn("nested deeper than", m.classify_run_status({"metadata": metadata})[2])
        self.assert_never_approves(metadata)

    def test_a_metadata_cycle_terminates_without_a_spurious_depth_defect(self):
        selfref = {**self.APPROVED_TOP, "findings": []}
        selfref["review"] = selfref
        self.assert_approves(selfref, "self-referential review alias")
        outer = {**self.APPROVED_TOP, "findings": []}
        outer["review"] = {"approval": outer}
        self.assert_approves(outer, "two-step alias cycle")

    # -- controls: clean nested approvals are not collateral damage --

    def test_a_clean_nested_approval_container_still_approves(self):
        for outer in m.ALL_CONTAINER_KEYS:
            for inner in m.ALL_CONTAINER_KEYS:
                self.assert_approves(
                    {"findings": [], outer: {inner: {"state": "APPROVED", "review_id": 4797653504,
                                                     "submitted": True}}},
                    f"clean {outer}.{inner} approval",
                )

    def test_a_clean_nested_approval_url_shaped_id_still_approves(self):
        self.assert_approves(
            {"findings": [],
             "review": {"approval": {"github_review_state": "APPROVED", "submitted": True,
                                     "html_url": "https://github.com/Ylopo/x/pull/1"
                                                 "#pullrequestreview-4797653504"}}},
        )

    def test_benign_empty_nested_surfaces_do_not_manufacture_a_block(self):
        for empty in ("none", "", "n/a", 0, False, [], {}):
            metadata = {**self.APPROVED_TOP, "findings": [],
                        "review": {"approval": {"findings": empty, "blockers": empty}}}
            self.assertEqual(self.status(metadata), ("signal", m.STATE_APPROVED), repr(empty))

    def test_unrelated_nested_metadata_is_still_not_broad_scanned(self):
        # Only *known* alias keys are followed. A blocking-looking string parked in
        # an unrelated subtree must not classify the lane.
        self.assert_approves(
            {**self.APPROVED_TOP, "findings": [],
             "worker_notes": {"scratch": {"blockers": ["BLOCKER: unrelated musing"]}}},
        )

    def test_a_nested_alias_verdict_behind_an_older_run_still_fails_closed(self):
        # Lane level: the newest run carries the nested contradiction.
        payload = {
            "task": {"id": "t_n", "status": "done", "assignee": "reviewer"},
            "runs": [
                {"id": "old", "profile": "reviewer", "ended_at": NOW - 900,
                 "metadata": {**self.APPROVED_TOP, "findings": []}},
                {"id": "new", "profile": "reviewer", "ended_at": NOW - 60,
                 "metadata": {**self.APPROVED_TOP,
                              "review": {"approval": {"findings": ["BLOCKER: unsafe"]}}}},
            ],
        }
        self.assertEqual(m.classify_task(payload)[0], m.STATE_BLOCKED)
        self.assertFalse(m.task_approval(payload)[0])


class QuarantineHoldTest(unittest.TestCase):
    """Blocker 1/2: an unsafe lane holds its post instead of letting others stamp it."""

    def quarantined_show(self, task_id):
        return show(task_id, {"approval": {"submitted": True, "review_id": 1, "state": "APPROVED",
                                           "posted": False}, "findings": []})

    def test_quarantined_lane_holds_its_own_post(self):
        result = tick([lane("t_q", "done")], {"t_q": self.quarantined_show("t_q")})
        self.assertEqual(result["slack"].calls, [])
        self.assertIn("t_q", result["state"]["posts"][PERMALINK]["unresolved_lanes"])
        self.assertIn("held", "\n".join(result["outputs"]))

    def test_quarantined_lane_blocks_a_sibling_lane_from_transitioning(self):
        rows = [lane("t_q", "done"), lane("t_ok", "done")]
        shows = {"t_q": self.quarantined_show("t_q"),
                 "t_ok": show("t_ok", {"github_review_state": "COMMENTED"})}
        result = tick(rows, shows)
        self.assertEqual(result["slack"].calls, [])
        self.assertIsNone(result["state"]["posts"][PERMALINK]["emoji"])
        self.assertEqual(result["state"]["posts"][PERMALINK]["want"], m.EMOJI_COMMENT)

    def test_hold_survives_ticks_and_a_restart(self):
        rows = [lane("t_q", "done"), lane("t_ok", "done")]
        shows = {"t_q": self.quarantined_show("t_q"),
                 "t_ok": show("t_ok", {"github_review_state": "COMMENTED"})}
        first = tick(rows, shows)
        # Restart: state round-trips through JSON and back through migration.
        restarted, _ = m.migrate_state(json_roundtrip(first["state"]), NOW + 7200)
        self.assertIn("t_q", restarted["posts"][PERMALINK]["unresolved_lanes"])
        second = tick(rows, shows, state=restarted, now=NOW + 7200)
        self.assertEqual(second["slack"].calls, [])
        self.assertIsNone(restarted["posts"][PERMALINK]["emoji"])

    def test_a_quarantined_lane_never_downgrades_an_applied_block(self):
        # The post is already review-no; a later quarantined run must not clear it.
        first = tick([lane("t_bad", "done")], {"t_bad": show("t_bad", {"verdict": "NEEDS_FIX"})})
        state = first["state"]
        rows = [lane("t_bad", "done", completed_at=NOW + 500)]
        shows = {"t_bad": self.quarantined_show("t_bad")}
        second = tick(rows, shows, state=state, now=NOW + 600)
        self.assertEqual(second["slack"].adds, [])
        self.assertEqual(state["posts"][PERMALINK]["emoji"], m.EMOJI_BLOCK)

    def test_reclassifying_the_lane_releases_the_hold(self):
        rows = [lane("t_q", "done")]
        first = tick(rows, {"t_q": self.quarantined_show("t_q")})
        state = first["state"]
        rows = [lane("t_q", "done", completed_at=NOW + 500)]
        healed = tick(rows, {"t_q": show("t_q", {"verdict": "NEEDS_FIX"}, ended_at=NOW + 500)},
                      state=state, now=NOW + 600)
        self.assertEqual(healed["slack"].adds, [(PERMALINK, m.EMOJI_BLOCK)])
        self.assertNotIn("unresolved_lanes", state["posts"][PERMALINK])

    def test_operator_can_explicitly_clear_a_hold(self):
        rows = [lane("t_q", "done"), lane("t_ok", "done")]
        shows = {"t_q": self.quarantined_show("t_q"),
                 "t_ok": show("t_ok", {"github_review_state": "COMMENTED"})}
        state = tick(rows, shows)["state"]
        released = tick(rows, shows, state=state, now=NOW + 120,
                        clear_unresolved=[PERMALINK])
        self.assertIn("hold_cleared", "\n".join(released["outputs"]))
        self.assertEqual(released["slack"].adds, [(PERMALINK, m.EMOJI_COMMENT)])

    def test_clearing_a_hold_that_does_not_exist_is_a_quiet_noop(self):
        result = tick([], {}, clear_unresolved=[PERMALINK])
        self.assertIn("hold_clear_noop", "\n".join(result["outputs"]))
        self.assertEqual(result["slack"].calls, [])

    def test_only_task_re_shows_an_already_seen_lane(self):
        # The operator's other escape hatch: force a re-classification of a lane the
        # cursor has already marked seen at this revision.
        rows = [lane("t_q", "done")]
        state = tick(rows, {"t_q": self.quarantined_show("t_q")})["state"]
        forced = tick(rows, {"t_q": show("t_q", {"verdict": "NEEDS_FIX"})},
                      state=state, now=NOW + 120, only_task=["t_q"])
        self.assertEqual(forced["shown"], ["t_q"])
        self.assertEqual(forced["slack"].adds, [(PERMALINK, m.EMOJI_BLOCK)])


class UnresolvedHoldPersistenceTest(unittest.TestCase):
    """Re-review blocker (t_6c1d0f29): an *unshowable* hold must outlive rediscovery.

    The rejected artifact recorded provenance only for lanes it had read and
    quarantined. An unshowable completed lane got `deferred_at` and nothing else, so
    the hold lived only in the tick that rediscovered the lane. Once the lane's row
    crossed `--lookback-seconds` (or dropped out of the lane list, or was archived) it
    was no longer queued, `deferred_links` came back empty, and the post — still a
    candidate through `deferred_at` — transitioned from its *sibling* lane's persisted
    verdict: one add plus three removals on a post that was promised an indefinite
    hold.

    Every test here builds the hold with a lane whose `kanban show` always fails.
    """

    BOUNDARY = NOW + m.DEFAULT_LOOKBACK_SECONDS + 1

    def rows(self):
        return [lane("t_ok", "done"), lane("t_missing", "done")]

    def shows(self):
        return {"t_ok": show("t_ok", {"github_review_state": "COMMENTED"})}

    def held_state(self, *, now=NOW):
        """One tick that holds PERMALINK on the unshowable lane t_missing."""
        state = m.migrate_state(None, now, backfill=True)[0]
        first = tick(self.rows(), self.shows(), state=state, now=now)
        self.assertEqual(first["slack"].calls, [])
        return state

    # --- provenance -------------------------------------------------------

    def test_an_unshowable_lane_persists_task_id_and_reason(self):
        state = self.held_state()
        unresolved = state["posts"][PERMALINK]["unresolved_lanes"]
        self.assertEqual(list(unresolved), ["t_missing"])
        self.assertEqual(m.hold_kind(unresolved["t_missing"]), m.HOLD_KIND_UNSHOWABLE)
        self.assertIn("show failed", unresolved["t_missing"])
        self.assertEqual(state["posts"][PERMALINK]["emoji"], None)
        self.assertEqual(state["posts"][PERMALINK]["want"], m.EMOJI_COMMENT)

    def test_a_backoff_deferred_lane_persists_provenance_too(self):
        # Second tick: the lane is inside its retry backoff, so it is not even
        # attempted. The hold still has to be recorded with its reason.
        state = self.held_state()
        state["posts"][PERMALINK].pop("unresolved_lanes")
        result = tick(self.rows(), self.shows(), state=state, now=NOW + 1)
        self.assertEqual(result["metrics"]["shows_deferred"], 1)
        unresolved = state["posts"][PERMALINK]["unresolved_lanes"]
        self.assertEqual(m.hold_kind(unresolved["t_missing"]), m.HOLD_KIND_UNSHOWABLE)
        self.assertIn("backoff", unresolved["t_missing"])

    def test_a_cap_deferred_lane_persists_provenance_too(self):
        rows = [lane("t_a", "done", completed_at=NOW - 10),
                lane("t_b", "done", permalink=PERMALINK_B, completed_at=NOW - 20)]
        shows = {"t_a": show("t_a", {"verdict": "NEEDS_FIX"}),
                 "t_b": show("t_b", {"verdict": "NEEDS_FIX"})}
        result = tick(rows, shows, max_shows=1)
        self.assertEqual(result["shown"], ["t_a"])
        unresolved = result["state"]["posts"][PERMALINK_B]["unresolved_lanes"]
        self.assertEqual(m.hold_kind(unresolved["t_b"]), m.HOLD_KIND_UNSHOWABLE)
        self.assertIn("cap", unresolved["t_b"])

    # --- the reported blocker: the lookback boundary -----------------------

    def test_hold_survives_the_lookback_boundary(self):
        # The exact repro: at NOW + DEFAULT_LOOKBACK_SECONDS + 1 both rows are
        # excluded by lookback, and the rejected artifact added doge_notes plus three
        # removals from t_ok's persisted verdict alone.
        state = self.held_state()
        later = tick(self.rows(), self.shows(), state=state, now=self.BOUNDARY)
        self.assertEqual(later["shown"], [])
        self.assertEqual(later["slack"].calls, [])
        self.assertEqual(later["metrics"]["posts_held"], 1)
        self.assertIsNone(state["posts"][PERMALINK]["emoji"])
        self.assertIn("t_missing", state["posts"][PERMALINK]["unresolved_lanes"])

    def test_hold_survives_far_past_the_lookback_boundary(self):
        state = self.held_state()
        now = self.BOUNDARY
        for _ in range(30):
            now += m.DEFAULT_LOOKBACK_SECONDS
            result = tick(self.rows(), self.shows(), state=state, now=now)
            self.assertEqual(result["slack"].calls, [])
        self.assertIsNone(state["posts"][PERMALINK]["emoji"])
        self.assertIn("t_missing", state["posts"][PERMALINK]["unresolved_lanes"])

    def test_a_custom_lookback_boundary_does_not_release_the_hold_either(self):
        state = self.held_state()
        later = tick(self.rows(), self.shows(), state=state, now=NOW + 4000,
                     lookback_seconds=3600)
        self.assertEqual(later["slack"].calls, [])
        self.assertIn("t_missing", state["posts"][PERMALINK]["unresolved_lanes"])

    def test_the_held_post_is_reported_across_the_boundary(self):
        state = self.held_state()
        later = tick(self.rows(), self.shows(), state=state, now=self.BOUNDARY)
        line = "\n".join(later["outputs"])
        self.assertIn("held source=", line)
        self.assertIn("reason=unclassified_completed_lane", line)
        self.assertIn("unresolved=t_missing", line)

    # --- rediscovery stopping for other reasons ---------------------------

    def test_hold_survives_the_lane_vanishing_from_a_later_successful_list(self):
        # The list call succeeds; t_missing is simply not in it any more (archived,
        # deleted, or filtered out upstream). "We stopped looking" is not evidence.
        state = self.held_state()
        later = tick([lane("t_ok", "done")], self.shows(), state=state, now=NOW + 7200)
        self.assertEqual(later["slack"].calls, [])
        self.assertIn("t_missing", state["posts"][PERMALINK]["unresolved_lanes"])

    def test_hold_survives_a_sibling_reclassifying_while_the_lane_is_absent(self):
        # Teeth: the sibling really does produce fresh signal on this tick, and the
        # held lane is nowhere in the list. Still no Slack call.
        state = self.held_state()
        rows = [lane("t_ok", "done", completed_at=NOW + 500)]
        shows = {"t_ok": show("t_ok", {"github_review_state": "COMMENTED"}, ended_at=NOW + 500)}
        later = tick(rows, shows, state=state, now=NOW + 600)
        self.assertEqual(later["shown"], ["t_ok"])
        self.assertEqual(later["slack"].calls, [])
        self.assertIsNone(state["posts"][PERMALINK]["emoji"])

    def test_hold_survives_the_lane_being_archived(self):
        state = self.held_state()
        rows = [lane("t_ok", "done"), lane("t_missing", "archived")]
        later = tick(rows, self.shows(), state=state, now=NOW + 7200)
        self.assertEqual(later["slack"].calls, [])
        self.assertIn("t_missing", state["posts"][PERMALINK]["unresolved_lanes"])

    def test_hold_survives_an_empty_lane_list(self):
        state = self.held_state()
        later = tick([], {}, state=state, now=self.BOUNDARY)
        self.assertEqual(later["slack"].calls, [])
        self.assertIn("t_missing", state["posts"][PERMALINK]["unresolved_lanes"])

    # --- restart ----------------------------------------------------------

    def test_hold_survives_a_json_restart_before_the_boundary(self):
        state = self.held_state()
        restarted, _ = m.migrate_state(json_roundtrip(state), NOW + 7200)
        self.assertIn("t_missing", restarted["posts"][PERMALINK]["unresolved_lanes"])
        later = tick(self.rows(), self.shows(), state=restarted, now=NOW + 7200)
        self.assertEqual(later["slack"].calls, [])
        self.assertIsNone(restarted["posts"][PERMALINK]["emoji"])

    def test_hold_survives_a_json_restart_after_the_boundary(self):
        state = self.held_state()
        restarted, _ = m.migrate_state(json_roundtrip(state), self.BOUNDARY)
        self.assertIn("t_missing", restarted["posts"][PERMALINK]["unresolved_lanes"])
        later = tick(self.rows(), self.shows(), state=restarted, now=self.BOUNDARY)
        self.assertEqual(later["shown"], [])
        self.assertEqual(later["slack"].calls, [])
        self.assertIsNone(restarted["posts"][PERMALINK]["emoji"])

    def test_repeated_restarts_across_the_boundary_never_release_the_hold(self):
        state = self.held_state()
        now = NOW
        for _ in range(5):
            now += m.DEFAULT_LOOKBACK_SECONDS
            state, _ = m.migrate_state(json_roundtrip(state), now)
            result = tick(self.rows(), self.shows(), state=state, now=now)
            self.assertEqual(result["slack"].calls, [])
        self.assertIn("t_missing", state["posts"][PERMALINK]["unresolved_lanes"])

    def test_a_hold_with_no_persisted_provenance_is_not_released_by_a_restart(self):
        # Pre-provenance (or hand-edited) state: the post was held, the reason is
        # lost. Fail closed — migration names the hold `unknown` rather than letting
        # the next tick transition the post on its remaining lane.
        raw = {
            "schema_version": m.STATE_SCHEMA_VERSION,
            "baseline_completed_at": 0, "machine_baseline_at": 0,
            "posts": {PERMALINK: {"emoji": None, "lanes": {"t_ok": m.EMOJI_COMMENT},
                                  "deferred_at": NOW, "want": m.EMOJI_COMMENT}},
            "reacted": {}, "failures": {},
            "cursor": {"seen": {}, "attempts": {}, "last_list_at": NOW},
        }
        state, _ = m.migrate_state(json_roundtrip(raw), self.BOUNDARY)
        self.assertEqual(list(state["posts"][PERMALINK]["unresolved_lanes"]), ["unknown"])
        later = tick([], {}, state=state, now=self.BOUNDARY)
        self.assertEqual(later["slack"].calls, [])
        self.assertIn("reason=unresolved_lane", "\n".join(later["outputs"]))

    # --- compaction -------------------------------------------------------

    def test_compaction_never_drops_a_held_post(self):
        state = self.held_state()
        record = state["posts"][PERMALINK]
        record["applied_at"] = NOW - m.POST_RETENTION_SECONDS - 10
        m.compact_state(state, NOW)
        self.assertIn(PERMALINK, state["posts"])
        self.assertIn("t_missing", state["posts"][PERMALINK]["unresolved_lanes"])

    def test_compaction_never_drops_a_held_post_that_already_has_a_reaction(self):
        state = m.migrate_state(None, NOW, backfill=True)[0]
        state["posts"][PERMALINK] = {
            "emoji": m.EMOJI_BLOCK, "applied_at": NOW - m.POST_RETENTION_SECONDS - 10,
            "pending_removals": [], "lanes": {"t_bad": m.EMOJI_BLOCK},
            "deferred_at": NOW - 10,
            "unresolved_lanes": {"t_missing": m.hold_provenance(m.HOLD_KIND_UNSHOWABLE, "show failed")},
        }
        m.compact_state(state, NOW)
        self.assertIn(PERMALINK, state["posts"])

    def test_a_held_post_survives_compaction_inside_a_real_tick_past_the_boundary(self):
        state = self.held_state()
        state["posts"][PERMALINK]["applied_at"] = NOW - m.POST_RETENTION_SECONDS - 10
        later = tick(self.rows(), self.shows(), state=state, now=self.BOUNDARY)
        self.assertEqual(later["slack"].calls, [])
        self.assertIn(PERMALINK, state["posts"])
        self.assertIn("t_missing", state["posts"][PERMALINK]["unresolved_lanes"])

    def test_a_released_empty_post_record_is_dropped_not_leaked(self):
        # Recording a hold for every routine deferral must not leave behind records
        # with no emoji and no intent: they have no `applied_at`, so `compact_state`
        # can never age them out.
        rows = [lane("t_a", "done", completed_at=NOW - 10),
                lane("t_b", "done", permalink=PERMALINK_B, completed_at=NOW - 20)]
        silent = {"note": "no verdict recorded"}
        shows = {"t_a": show("t_a", silent), "t_b": show("t_b", silent)}
        state = m.migrate_state(None, NOW, backfill=True)[0]
        tick(rows, shows, state=state, now=NOW, max_shows=1)
        self.assertIn("t_b", state["posts"][PERMALINK_B]["unresolved_lanes"])
        second = tick(rows, shows, state=state, now=NOW + 200, max_shows=1)
        self.assertEqual(second["slack"].calls, [])
        self.assertEqual(state["posts"], {})

    def test_a_released_post_becomes_compactable_again(self):
        state = self.held_state()
        shows = {**self.shows(), "t_missing": show("t_missing", {"verdict": "NEEDS_FIX"})}
        healed = tick(self.rows(), shows, state=state, now=NOW + 200)
        self.assertEqual(healed["slack"].adds, [(PERMALINK, m.EMOJI_BLOCK)])
        record = state["posts"][PERMALINK]
        self.assertNotIn("unresolved_lanes", record)
        self.assertNotIn("deferred_at", record)
        record["applied_at"] = NOW - m.POST_RETENTION_SECONDS - 10
        record.pop("reconcile_due_at", None)
        m.compact_state(state, NOW)
        self.assertNotIn(PERMALINK, state["posts"])

    # --- sibling precedence while held ------------------------------------

    def test_a_lower_precedence_sibling_never_stamps_a_held_post(self):
        state = self.held_state()
        for now in (NOW + 3600, self.BOUNDARY, self.BOUNDARY + m.DEFAULT_LOOKBACK_SECONDS):
            result = tick(self.rows(), self.shows(), state=state, now=now)
            self.assertEqual(result["slack"].adds, [])
            self.assertEqual(result["slack"].removes, [])
        self.assertEqual(state["posts"][PERMALINK]["want"], m.EMOJI_COMMENT)

    def test_a_lower_precedence_sibling_never_downgrades_an_applied_block(self):
        # The post already shows review-no from t_bad. t_ok later reports COMMENTED and
        # t_missing is unshowable: past the boundary the rejected artifact would have
        # replaced review-no with doge_notes.
        first = tick([lane("t_bad", "done")], {"t_bad": show("t_bad", {"verdict": "NEEDS_FIX"})})
        state = first["state"]
        rows = [lane("t_ok", "done", completed_at=NOW + 300), lane("t_missing", "done",
                                                                  completed_at=NOW + 300)]
        held = tick(rows, self.shows(), state=state, now=NOW + 400)
        self.assertEqual(held["slack"].calls, [])
        later = tick(rows, self.shows(), state=state, now=NOW + 400 + m.DEFAULT_LOOKBACK_SECONDS)
        self.assertEqual(later["slack"].calls, [])
        self.assertEqual(state["posts"][PERMALINK]["emoji"], m.EMOJI_BLOCK)

    def test_every_cited_post_of_an_unshowable_lane_is_held(self):
        body = f"Sources: {PERMALINK} and {PERMALINK_B}"
        rows = [lane("t_ok", "done"), lane("t_missing", "done", body=body)]
        state = m.migrate_state(None, NOW, backfill=True)[0]
        tick(rows, self.shows(), state=state, now=NOW)
        for permalink in (PERMALINK, PERMALINK_B):
            self.assertIn("t_missing", state["posts"][permalink]["unresolved_lanes"])
        later = tick(rows, self.shows(), state=state, now=self.BOUNDARY)
        self.assertEqual(later["slack"].calls, [])

    # --- release controls -------------------------------------------------

    def test_the_lane_reclassifying_releases_its_own_hold(self):
        state = self.held_state()
        shows = {**self.shows(), "t_missing": show("t_missing", {"verdict": "NEEDS_FIX"})}
        healed = tick(self.rows(), shows, state=state, now=NOW + 200)
        self.assertEqual(healed["slack"].adds, [(PERMALINK, m.EMOJI_BLOCK)])
        self.assertNotIn("unresolved_lanes", state["posts"][PERMALINK])

    def test_a_lane_that_reads_clean_but_says_nothing_releases_its_hold(self):
        # "Read successfully and silent" is a resolution, not a hold. The rejected
        # artifact only released lanes that produced an *emoji*, so a quarantined lane
        # that later read clean-but-silent stayed held forever with no way out but
        # --clear-unresolved.
        rows = [lane("t_q", "done"), lane("t_ok", "done")]
        quarantined = show("t_q", {"approval": {"submitted": True, "review_id": 1,
                                                "state": "APPROVED", "posted": False}})
        state = tick(rows, {"t_q": quarantined, **self.shows()})["state"]
        self.assertIn("t_q", state["posts"][PERMALINK]["unresolved_lanes"])
        rows = [lane("t_q", "done", completed_at=NOW + 500), lane("t_ok", "done")]
        released = tick(rows, {"t_q": show("t_q", {"note": "no verdict recorded"},
                                          ended_at=NOW + 500), **self.shows()},
                        state=state, now=NOW + 600)
        self.assertEqual(released["slack"].adds, [(PERMALINK, m.EMOJI_COMMENT)])
        self.assertNotIn("unresolved_lanes", state["posts"][PERMALINK])

    def test_an_unshowable_lane_that_reads_clean_but_says_nothing_releases_its_hold(self):
        state = self.held_state()
        shows = {**self.shows(), "t_missing": show("t_missing", {"note": "no verdict recorded"})}
        released = tick(self.rows(), shows, state=state, now=NOW + 200)
        self.assertEqual(released["slack"].adds, [(PERMALINK, m.EMOJI_COMMENT)])
        self.assertNotIn("unresolved_lanes", state["posts"][PERMALINK])

    def test_the_lane_going_active_again_does_not_release_its_hold(self):
        # Deliberate, and a behaviour change from the pre-fix build: a status change is
        # not a classification. The unread completed run may still have been the
        # `review-no`, so a re-activated lane contributes `thinkies` to the aggregate
        # but keeps holding until it is actually read. Cost: while that lane is
        # re-running, its post keeps its old reaction instead of going back to
        # `thinkies`.
        state = self.held_state()
        rows = [lane("t_ok", "done"), lane("t_missing", "running")]
        held = tick(rows, self.shows(), state=state, now=NOW + 200)
        self.assertEqual(held["slack"].calls, [])
        record = state["posts"][PERMALINK]
        self.assertIn("t_missing", record["unresolved_lanes"])
        self.assertEqual(record["lanes"]["t_missing"], m.EMOJI_ACTIVE)
        # ...and it releases as soon as the new run is readable.
        rows = [lane("t_ok", "done"), lane("t_missing", "done", completed_at=NOW + 900)]
        shows = {**self.shows(), "t_missing": show("t_missing", {"verdict": "NEEDS_FIX"},
                                                  ended_at=NOW + 900)}
        healed = tick(rows, shows, state=state, now=NOW + 1000)
        self.assertEqual(healed["slack"].adds, [(PERMALINK, m.EMOJI_BLOCK)])

    def test_only_task_past_the_boundary_releases_the_lanes_own_hold(self):
        # The operator's reclassification path still reaches a lane the lookback
        # window no longer covers.
        state = self.held_state()
        shows = {**self.shows(), "t_missing": show("t_missing", {"verdict": "NEEDS_FIX"})}
        forced = tick(self.rows(), shows, state=state, now=self.BOUNDARY,
                      only_task=["t_missing"])
        self.assertEqual(forced["shown"], ["t_missing"])
        self.assertEqual(forced["slack"].adds, [(PERMALINK, m.EMOJI_BLOCK)])
        self.assertNotIn("unresolved_lanes", state["posts"][PERMALINK])

    def test_only_task_that_quarantines_again_keeps_the_hold(self):
        state = self.held_state()
        shows = {**self.shows(),
                 "t_missing": show("t_missing", {"approval": {"submitted": True, "review_id": 1,
                                                              "state": "APPROVED", "posted": False}})}
        forced = tick(self.rows(), shows, state=state, now=self.BOUNDARY,
                      only_task=["t_missing"])
        self.assertEqual(forced["slack"].calls, [])
        unresolved = state["posts"][PERMALINK]["unresolved_lanes"]
        self.assertEqual(m.hold_kind(unresolved["t_missing"]), m.HOLD_KIND_QUARANTINE)

    def test_operator_clear_releases_a_lane_that_is_still_unshowable(self):
        # The clear runs before discovery, so without a standing release the lane
        # would simply re-hold the post later in the same tick.
        state = self.held_state()
        released = tick(self.rows(), self.shows(), state=state, now=NOW + 200,
                        clear_unresolved=[PERMALINK])
        line = "\n".join(released["outputs"])
        self.assertIn("hold_cleared", line)
        self.assertIn("lanes=t_missing", line)
        self.assertEqual(released["slack"].adds, [(PERMALINK, m.EMOJI_COMMENT)])
        self.assertEqual(state["posts"][PERMALINK]["emoji"], m.EMOJI_COMMENT)

    def test_an_operator_release_is_standing_across_later_ticks_and_restarts(self):
        state = self.held_state()
        tick(self.rows(), self.shows(), state=state, now=NOW + 200,
             clear_unresolved=[PERMALINK])
        now = NOW + 200
        for _ in range(4):
            now += m.FAIL_RETRY_SECONDS
            state, _ = m.migrate_state(json_roundtrip(state), now)
            result = tick(self.rows(), self.shows(), state=state, now=now)
            # The lane keeps failing to show, and keeps not re-holding the post. (Any
            # calls here are the bounded post-convergence sweeps, never a transition.)
            self.assertEqual(result["slack"].adds, [])
            self.assertEqual(result["metrics"]["posts_held"], 0)
            self.assertNotIn("unresolved_lanes", state["posts"][PERMALINK])
        self.assertEqual(state["posts"][PERMALINK]["emoji"], m.EMOJI_COMMENT)

    def test_an_operator_release_is_consumed_by_a_fresh_quarantine(self):
        # Fail closed: the released lane becomes readable and turns out to be unsafe.
        # That is new information the operator never saw, so the post is held again.
        state = self.held_state()
        tick(self.rows(), self.shows(), state=state, now=NOW + 200,
             clear_unresolved=[PERMALINK])
        rows = [lane("t_ok", "done"), lane("t_missing", "done", completed_at=NOW + 1000)]
        shows = {**self.shows(),
                 "t_missing": show("t_missing", {"approval": {"submitted": True, "review_id": 1,
                                                              "state": "APPROVED", "posted": False}},
                                   ended_at=NOW + 1000)}
        requarantined = tick(rows, shows, state=state, now=NOW + 1200)
        self.assertEqual(requarantined["shown"], ["t_missing"])
        self.assertEqual(requarantined["slack"].calls, [])
        self.assertEqual(requarantined["metrics"]["posts_held"], 1)
        record = state["posts"][PERMALINK]
        self.assertIn("t_missing", record["unresolved_lanes"])
        self.assertNotIn("cleared_lanes", record)

    def test_a_release_does_not_release_a_different_lanes_hold(self):
        rows = [lane("t_ok", "done"), lane("t_missing", "done"), lane("t_gone", "done")]
        state = m.migrate_state(None, NOW, backfill=True)[0]
        tick(rows, self.shows(), state=state, now=NOW)
        self.assertEqual(sorted(state["posts"][PERMALINK]["unresolved_lanes"]),
                         ["t_gone", "t_missing"])
        # An operator clears the post, then a brand-new unshowable lane appears.
        tick(rows, self.shows(), state=state, now=NOW + 200, clear_unresolved=[PERMALINK])
        rows.append(lane("t_new", "done", completed_at=NOW + 400))
        later = tick(rows, self.shows(), state=state, now=NOW + 500)
        self.assertEqual(later["slack"].calls, [])
        self.assertEqual(list(state["posts"][PERMALINK]["unresolved_lanes"]), ["t_new"])

    # --- alert volume -----------------------------------------------------

    def test_a_routine_cap_deferral_does_not_alert_on_its_first_tick(self):
        rows = [lane("t_a", "done", completed_at=NOW - 10),
                lane("t_b", "done", permalink=PERMALINK_B, completed_at=NOW - 20)]
        shows = {"t_a": show("t_a", {"verdict": "NEEDS_FIX"}),
                 "t_b": show("t_b", {"verdict": "NEEDS_FIX"})}
        state = m.migrate_state(None, NOW, backfill=True)[0]
        first = tick(rows, shows, state=state, now=NOW, max_shows=1)
        self.assertNotIn("held source=", "\n".join(first["outputs"]))
        # ...and it self-heals on the next tick rather than lingering.
        second = tick(rows, shows, state=state, now=NOW + 200, max_shows=1)
        self.assertEqual(second["shown"], ["t_b"])
        self.assertEqual(second["slack"].adds, [(PERMALINK_B, m.EMOJI_BLOCK)])
        self.assertNotIn("unresolved_lanes", state["posts"][PERMALINK_B])

    def test_a_persistent_unshowable_hold_alerts_after_the_grace_window(self):
        state = self.held_state()
        self.assertNotIn("held source=", "\n".join(
            tick(self.rows(), self.shows(), state=state, now=NOW + 300)["outputs"]))
        late = tick(self.rows(), self.shows(), state=state,
                    now=NOW + m.HOLD_ALERT_GRACE_SECONDS + 1)
        self.assertIn("held source=", "\n".join(late["outputs"]))

    def test_a_quarantine_hold_alerts_immediately(self):
        quarantined = show("t_q", {"approval": {"submitted": True, "review_id": 1,
                                                "state": "APPROVED", "posted": False}})
        result = tick([lane("t_q", "done")], {"t_q": quarantined})
        self.assertIn("reason=quarantined_lane", "\n".join(result["outputs"]))

    def test_a_mixed_hold_is_reported_at_the_strictest_kind(self):
        rows = [lane("t_q", "done"), lane("t_missing", "done")]
        quarantined = show("t_q", {"approval": {"submitted": True, "review_id": 1,
                                               "state": "APPROVED", "posted": False}})
        result = tick(rows, {"t_q": quarantined})
        self.assertIn("reason=quarantined_lane", "\n".join(result["outputs"]))
        self.assertEqual(sorted(result["state"]["posts"][PERMALINK]["unresolved_lanes"]),
                         ["t_missing", "t_q"])

    def test_hold_report_classifies_every_kind(self):
        quarantine = m.hold_provenance(m.HOLD_KIND_QUARANTINE, "unreadable")
        unshowable = m.hold_provenance(m.HOLD_KIND_UNSHOWABLE, "show failed")
        self.assertEqual(m.hold_report({"a": unshowable}),
                         ("unclassified_completed_lane", m.HOLD_ALERT_GRACE_SECONDS))
        self.assertEqual(m.hold_report({"a": quarantine}), ("quarantined_lane", 0))
        self.assertEqual(m.hold_report({"a": unshowable, "b": quarantine}),
                         ("quarantined_lane", 0))
        # Legacy/untagged provenance is treated strictly, never as a routine deferral.
        self.assertEqual(m.hold_report({"a": "run=run-1 malformed: x"}), ("unresolved_lane", 0))
        self.assertEqual(m.hold_report({}), ("unresolved_lane", 0))


class ReconciliationTest(unittest.TestCase):
    """Blocker 3: a late add from the best-effort prompt writer is repaired.

    The channel-watch prompt may add an initial :thinkies: at any moment, including
    just after this watchdog converged the post and removed the stale reactions.
    Local state can never see that add, so each transition arms a bounded number of
    blind sweeps that re-assert the desired end state.
    """

    def converged_block(self):
        state = tick([lane("t_a", "done")], {"t_a": show("t_a", {"verdict": "NEEDS_FIX"})})["state"]
        self.assertEqual(state["posts"][PERMALINK]["emoji"], m.EMOJI_BLOCK)
        return state

    def test_transition_arms_a_bounded_sweep_schedule(self):
        record = self.converged_block()["posts"][PERMALINK]
        self.assertEqual(record["reconcile_sweeps"], 0)
        self.assertEqual(record["reconcile_due_at"], NOW + m.RECONCILE_DELAYS_SECONDS[0])

    def test_no_sweep_before_the_first_delay(self):
        state = self.converged_block()
        quiet = tick([lane("t_a", "done")], {}, state=state,
                     now=NOW + m.RECONCILE_DELAYS_SECONDS[0] - 1)
        self.assertEqual(quiet["slack"].calls, [])

    def test_sweeps_run_then_the_post_goes_quiet_forever(self):
        state = self.converged_block()
        rows = [lane("t_a", "done")]
        now = NOW
        for index, delay in enumerate(m.RECONCILE_DELAYS_SECONDS):
            now += delay
            swept = tick(rows, {}, state=state, now=now)
            self.assertEqual(
                [emoji for _, emoji in swept["slack"].removes],
                [m.EMOJI_COMMENT, m.EMOJI_ACTIVE, m.EMOJI_APPROVED], f"sweep {index}")
            self.assertEqual(swept["slack"].adds, [], f"sweep {index}")
            self.assertEqual(swept["metrics"]["reconcile_sweeps"], 1, f"sweep {index}")
        # Budget spent: a settled post is back to zero Slack calls, forever.
        for extra in (3600, 86400, 864000):
            self.assertEqual(tick(rows, {}, state=state, now=now + extra)["slack"].calls, [])

    def test_late_prompt_side_thinkies_is_repaired_by_the_sweep(self):
        # The adversarial race: the watchdog converges the post to review-no, and
        # only *afterwards* does the prompt-side writer land its best-effort
        # thinkies. Zero-call convergence can never see it; the sweep repairs it.
        state = self.converged_block()

        class RacingSlack(FakeSlack):
            """Live post contents, mutated by a second writer between ticks."""

            def __init__(self, live):
                super().__init__()
                self.live = live

            def __call__(self, permalink, emoji, remove):
                self.calls.append((permalink, emoji, remove))
                if remove:
                    was_there = emoji in self.live
                    self.live.discard(emoji)
                    return {"ok": True, "no_reaction": not was_there}
                already = emoji in self.live
                self.live.add(emoji)
                return {"ok": True, "already_reacted": already}

        live = {m.EMOJI_BLOCK}
        live.add(m.EMOJI_ACTIVE)  # <- the late, unobservable prompt-side add
        slack = RacingSlack(live)
        swept = tick([lane("t_a", "done")], {}, state=state, slack=slack,
                     now=NOW + m.RECONCILE_DELAYS_SECONDS[0])
        self.assertEqual(live, {m.EMOJI_BLOCK})  # contradiction gone
        self.assertEqual(swept["metrics"]["reconcile_repairs"], 1)
        self.assertIn("removed_contradiction=thinkies", "\n".join(swept["outputs"]))

    def test_sweep_schedule_is_neither_lost_nor_repeated_across_a_restart(self):
        state = self.converged_block()
        rows = [lane("t_a", "done")]
        now = NOW + m.RECONCILE_DELAYS_SECONDS[0]
        first = tick(rows, {}, state=state, now=now)
        self.assertEqual(first["metrics"]["reconcile_sweeps"], 1)
        # Crash + restart between the two scheduled sweeps.
        restarted, _ = m.migrate_state(json_roundtrip(state), now + 10)
        self.assertEqual(restarted["posts"][PERMALINK]["reconcile_sweeps"], 1)
        repeat = tick(rows, {}, state=restarted, now=now + 10)
        self.assertEqual(repeat["slack"].calls, [])  # sweep 1 is not replayed
        second = tick(rows, {}, state=restarted, now=now + m.RECONCILE_DELAYS_SECONDS[1])
        self.assertEqual(second["metrics"]["reconcile_sweeps"], 1)  # sweep 2, on time

    def test_a_new_transition_re_arms_the_schedule(self):
        state = self.converged_block()
        now = NOW
        for delay in m.RECONCILE_DELAYS_SECONDS:  # exhaust the first budget
            now += delay
            tick([lane("t_a", "done")], {}, state=state, now=now)
        self.assertNotIn("reconcile_due_at", state["posts"][PERMALINK])
        rows = [lane("t_a", "done", completed_at=now + 10)]
        shows = {"t_a": show("t_a", {"github_review_state": "COMMENTED"}, ended_at=now + 10)}
        moved = tick(rows, shows, state=state, now=now + 20)
        self.assertEqual(moved["slack"].adds, [(PERMALINK, m.EMOJI_COMMENT)])
        record = state["posts"][PERMALINK]
        self.assertEqual(record["reconcile_sweeps"], 0)
        self.assertEqual(record["reconcile_due_at"], now + 20 + m.RECONCILE_DELAYS_SECONDS[0])

    def test_dry_run_never_sweeps(self):
        state = self.converged_block()
        dry = tick([lane("t_a", "done")], {}, state=state,
                   now=NOW + m.RECONCILE_DELAYS_SECONDS[0], dry_run=True)
        self.assertEqual(dry["slack"].calls, [])
        self.assertIn("WOULD_RECONCILE", "\n".join(dry["outputs"]))
        self.assertEqual(state["posts"][PERMALINK]["reconcile_sweeps"], 0)

    def test_migrated_legacy_post_is_never_swept(self):
        # Legacy ✅ posts were written by the approval-only watchdog; this process
        # never transitioned them, so there is no writer race to repair.
        legacy = {
            "baseline_completed_at": 1783578010,
            "reacted": {f"t_legacy:{PERMALINK}": {"at": 1784133481, "permalink": PERMALINK,
                                                  "task_id": "t_legacy", "reason": "run=1"}},
        }
        state, _ = m.migrate_state(legacy, NOW)
        state["machine_baseline_at"] = 0
        self.assertNotIn("reconcile_due_at", state["posts"][PERMALINK])
        for offset in (0, 300, 3600, 86400):
            self.assertEqual(tick([], {}, state=state, now=NOW + offset)["slack"].calls, [])

    def test_failed_sweep_removal_is_retried_as_ordinary_repair_work(self):
        state = self.converged_block()
        rows = [lane("t_a", "done")]
        now = NOW + m.RECONCILE_DELAYS_SECONDS[0]
        failing = tick(rows, {}, state=state, slack=FakeSlack(fail_remove=[m.EMOJI_ACTIVE]),
                       now=now)
        self.assertIn("reconcile_remove_failed", "\n".join(failing["outputs"]))
        self.assertEqual(state["posts"][PERMALINK]["pending_removals"], [m.EMOJI_ACTIVE])
        retried = tick(rows, {}, state=state, now=now + m.FAIL_RETRY_SECONDS)
        self.assertEqual(retried["slack"].adds, [(PERMALINK, m.EMOJI_BLOCK)])
        self.assertEqual(retried["slack"].removes, [(PERMALINK, m.EMOJI_ACTIVE)])
        self.assertEqual(state["posts"][PERMALINK]["pending_removals"], [])

    def test_exit_three_not_reacted_reconciliation_settles_then_is_zero_write(self):
        state = m.migrate_state(None, NOW, backfill=True)[0]
        state["machine_baseline_at"] = 0
        state["posts"][BORIS_PERMALINK] = {
            "state": m.STATE_BLOCKED,
            "emoji": "face_vomiting",
            "applied_at": NOW - 600,
            "pending_removals": [],
            "lanes": {"t_boris": m.STATE_BLOCKED},
            "reconcile_sweeps": 0,
            "reconcile_due_at": NOW,
        }
        commands = []

        def already_absent(cmd, *, timeout=120):
            commands.append(cmd)
            if "--remove" not in cmd:
                return subprocess.CompletedProcess(
                    cmd, 0, '{"ok": true, "already_reacted": true}', ""
                )
            return subprocess.CompletedProcess(
                cmd, 3, '{"ok": false, "error": "not_reacted"}', ""
            )

        with mock.patch.object(m, "run", already_absent):
            reconciled = tick(
                [], {}, state=state, slack=m.slack_reaction, now=NOW,
                channels=[BORIS_CHANNEL],
            )
            writes_after_reconcile = len(commands)
            quiet = tick(
                [], {}, state=state, slack=m.slack_reaction, now=NOW + 1,
                channels=[BORIS_CHANNEL],
            )

        self.assertGreater(writes_after_reconcile, 0)
        self.assertEqual(state["posts"][BORIS_PERMALINK]["pending_removals"], [])
        self.assertNotIn("reconcile_remove_failed", "\n".join(reconciled["outputs"]))
        self.assertEqual(len(commands), writes_after_reconcile)
        self.assertEqual(quiet["metrics"]["reaction_calls"], 0)

    def test_applied_then_http_failure_settles_when_retry_reports_not_reacted(self):
        state = m.migrate_state(None, NOW, backfill=True)[0]
        state["machine_baseline_at"] = 0
        state["posts"][BORIS_PERMALINK] = {
            "state": m.STATE_BLOCKED,
            "emoji": "face_vomiting",
            "applied_at": NOW - 600,
            "pending_removals": [],
            "lanes": {"t_boris": m.STATE_BLOCKED},
            "reconcile_sweeps": 0,
            "reconcile_due_at": NOW,
        }
        live = {"face_vomiting", "doge_notes"}
        commands = []
        doge_remove_attempts = 0

        def ambiguous_then_absent(cmd, *, timeout=120):
            nonlocal doge_remove_attempts
            commands.append(cmd)
            emoji = cmd[cmd.index("--emoji") + 1]
            if "--remove" not in cmd:
                already = emoji in live
                live.add(emoji)
                payload = {"ok": True, "already_reacted": already}
                return subprocess.CompletedProcess(cmd, 0, json.dumps(payload), "")
            was_present = emoji in live
            live.discard(emoji)
            if emoji == "doge_notes":
                doge_remove_attempts += 1
                if doge_remove_attempts == 1:
                    return subprocess.CompletedProcess(
                        cmd, 1, '{"ok": false, "error": "http_500"}', ""
                    )
                self.assertFalse(was_present)
                return subprocess.CompletedProcess(
                    cmd, 0, '{"ok": false, "error": "not_reacted"}', ""
                )
            payload = {"ok": True, "no_reaction": not was_present}
            return subprocess.CompletedProcess(cmd, 0, json.dumps(payload), "")

        with mock.patch.object(m, "run", ambiguous_then_absent):
            failed = tick(
                [], {}, state=state, slack=m.slack_reaction, now=NOW,
                channels=[BORIS_CHANNEL],
            )
            self.assertEqual(
                state["posts"][BORIS_PERMALINK]["pending_removals"], ["doge_notes"]
            )
            self.assertIn("reconcile_remove_failed", "\n".join(failed["outputs"]))

            repaired = tick(
                [], {}, state=state, slack=m.slack_reaction, now=NOW + 1,
                channels=[BORIS_CHANNEL],
            )
            writes_after_repair = len(commands)
            quiet = tick(
                [], {}, state=state, slack=m.slack_reaction, now=NOW + 2,
                channels=[BORIS_CHANNEL],
            )

        self.assertEqual(doge_remove_attempts, 2)
        self.assertEqual(live, {"face_vomiting"})
        self.assertEqual(state["posts"][BORIS_PERMALINK]["pending_removals"], [])
        self.assertEqual(repaired["code"], 0)
        self.assertEqual(len(commands), writes_after_repair)
        self.assertEqual(quiet["metrics"]["reaction_calls"], 0)

    def test_malformed_removal_error_stays_pending_instead_of_crashing(self):
        state = m.migrate_state(None, NOW, backfill=True)[0]
        state["machine_baseline_at"] = 0
        state["posts"][BORIS_PERMALINK] = {
            "state": m.STATE_BLOCKED,
            "emoji": "face_vomiting",
            "applied_at": NOW - 600,
            "pending_removals": [],
            "lanes": {"t_boris": m.STATE_BLOCKED},
            "reconcile_sweeps": 0,
            "reconcile_due_at": NOW,
        }

        def malformed_doge_error(cmd, *, timeout=120):
            emoji = cmd[cmd.index("--emoji") + 1]
            payload = (
                {"ok": False, "error": []}
                if emoji == "doge_notes"
                else {"ok": True, "no_reaction": True}
            )
            return subprocess.CompletedProcess(cmd, 0, json.dumps(payload), "")

        with mock.patch.object(m, "run", malformed_doge_error):
            try:
                result = tick(
                    [], {}, state=state, slack=m.slack_reaction, now=NOW,
                    channels=[BORIS_CHANNEL],
                )
            except TypeError as exc:
                self.fail(f"malformed Slack error escaped fail-closed bookkeeping: {exc}")

        self.assertEqual(
            state["posts"][BORIS_PERMALINK]["pending_removals"], ["doge_notes"]
        )
        self.assertIn("reconcile_remove_failed", "\n".join(result["outputs"]))

    def test_a_held_post_is_never_swept(self):
        # Holding means "touch nothing", including the blind repair sweep.
        rows = [lane("t_a", "done"), lane("t_missing", "done")]
        shows = {"t_a": show("t_a", {"verdict": "NEEDS_FIX"})}
        state = m.migrate_state(None, NOW)[0]
        state["machine_baseline_at"] = 0
        state["posts"][PERMALINK] = {
            "emoji": m.EMOJI_BLOCK, "applied_at": NOW, "lanes": {"t_a": m.EMOJI_BLOCK},
            "pending_removals": [], "reconcile_sweeps": 0,
            "reconcile_due_at": NOW + m.RECONCILE_DELAYS_SECONDS[0],
        }
        held = tick(rows, shows, state=state, now=NOW + m.RECONCILE_DELAYS_SECONDS[0])
        self.assertEqual(held["slack"].calls, [])
        self.assertEqual(held["metrics"]["posts_held"], 1)


class SlackReactionResultTest(unittest.TestCase):
    """Blocker 4: the helper's *result* is validated, not just its exit code."""

    def reaction(self, returncode, stdout, stderr="", *, remove=False):
        import subprocess

        recorded = {}

        def fake_run(cmd, *, timeout=120):
            recorded["cmd"] = cmd
            return subprocess.CompletedProcess(cmd, returncode, stdout, stderr)

        original = m.run
        m.run = fake_run
        try:
            return m.slack_reaction(PERMALINK, m.EMOJI_ACTIVE, remove), recorded
        finally:
            m.run = original

    def test_rc_zero_with_empty_stdout_is_a_failure(self):
        result, _ = self.reaction(0, "")
        self.assertFalse(result["ok"])
        self.assertIn("no valid JSON result", result["error"])

    def test_rc_zero_with_non_json_stdout_is_a_failure(self):
        result, _ = self.reaction(0, "Traceback (most recent call last):\n  boom\n")
        self.assertFalse(result["ok"])

    def test_rc_zero_with_a_json_non_object_is_a_failure(self):
        for payload in ("[]", '"ok"', "true", "null", "7"):
            result, _ = self.reaction(0, payload)
            self.assertFalse(result["ok"], payload)

    def test_rc_zero_with_ok_missing_is_a_failure(self):
        # The reviewed artifact defaulted a missing `ok` to True.
        result, _ = self.reaction(0, '{"channel": "C1", "ts": "1.2"}')
        self.assertFalse(result["ok"])

    def test_rc_zero_with_ok_false_is_a_failure(self):
        result, _ = self.reaction(0, '{"ok": false, "error": "invalid_name"}')
        self.assertFalse(result["ok"])
        self.assertEqual(result["error"], "invalid_name")

    def test_truthy_but_non_true_ok_is_a_failure(self):
        for payload in ('{"ok": 1}', '{"ok": "true"}', '{"ok": "yes"}'):
            result, _ = self.reaction(0, payload)
            self.assertFalse(result["ok"], payload)

    def test_rc_nonzero_with_ok_true_is_still_a_failure(self):
        result, _ = self.reaction(3, '{"ok": true}')
        self.assertFalse(result["ok"])

    def test_rc_zero_with_ok_true_succeeds(self):
        result, _ = self.reaction(0, '{"ok": true, "channel": "C1"}')
        self.assertTrue(result["ok"])
        self.assertIsNone(result["error"])

    def test_already_reacted_and_no_reaction_pass_through(self):
        result, _ = self.reaction(0, '{"ok": true, "already_reacted": true}')
        self.assertTrue(result["ok"] and result["already_reacted"])
        result, _ = self.reaction(0, '{"ok": true, "no_reaction": true}')
        self.assertTrue(result["ok"] and result["no_reaction"])

    def test_remove_folds_not_reacted_into_existing_already_absent_shape(self):
        for error in ("not_reacted", "no_reaction"):
            with self.subTest(error=error):
                result, _ = self.reaction(
                    0, json.dumps({"ok": False, "error": error}), remove=True
                )
                self.assertTrue(result["ok"])
                self.assertIsNone(result["error"])
                self.assertFalse(result["already_reacted"])
                self.assertTrue(result["no_reaction"])
                self.assertEqual(
                    set(result), {"ok", "error", "already_reacted", "no_reaction", "raw"}
                )

    def test_remove_folds_exit_three_not_reacted_into_already_absent_shape(self):
        result, _ = self.reaction(
            3, '{"ok": false, "error": "not_reacted"}', remove=True
        )
        self.assertTrue(result["ok"])
        self.assertIsNone(result["error"])
        self.assertFalse(result["already_reacted"])
        self.assertTrue(result["no_reaction"])

    def test_exit_three_other_results_remain_failures(self):
        payloads = (
            {"ok": False, "error": "invalid_auth"},
            {"ok": False, "error": "missing_scope"},
            {"ok": False, "error": "ratelimited"},
            {"ok": False, "error": "http_500"},
            {"ok": False, "error": "other_error"},
            {"ok": False, "error": "no_reaction"},
            {"ok": True, "error": "not_reacted"},
        )
        for payload in payloads:
            with self.subTest(payload=payload):
                result, _ = self.reaction(3, json.dumps(payload), remove=True)
                self.assertFalse(result["ok"])
                self.assertFalse(result["no_reaction"])

    def test_exit_three_malformed_or_non_object_output_remains_a_failure(self):
        for payload in ("", "not-json", "[]", '"not_reacted"'):
            with self.subTest(payload=payload):
                result, _ = self.reaction(3, payload, remove=True)
                self.assertFalse(result["ok"])
                self.assertFalse(result["no_reaction"])

    def test_other_nonzero_exits_with_not_reacted_remain_failures(self):
        for returncode in (1, 2, 4, 75):
            with self.subTest(returncode=returncode):
                result, _ = self.reaction(
                    returncode,
                    '{"ok": false, "error": "not_reacted"}',
                    remove=True,
                )
                self.assertFalse(result["ok"])
                self.assertFalse(result["no_reaction"])

    def test_remove_keeps_auth_rate_and_http_errors_as_failures(self):
        cases = (
            (0, "invalid_auth"),
            (0, "missing_scope"),
            (0, "ratelimited"),
            (1, "http_500"),
        )
        for returncode, error in cases:
            with self.subTest(error=error):
                result, _ = self.reaction(
                    returncode,
                    json.dumps({"ok": False, "error": error}),
                    remove=True,
                )
                self.assertFalse(result["ok"])
                self.assertEqual(result["error"], error)
                self.assertFalse(result["no_reaction"])

    def test_remove_with_list_or_object_error_remains_a_failure(self):
        for error in ([], {"code": "not_reacted"}):
            with self.subTest(error=error):
                try:
                    result, _ = self.reaction(
                        0,
                        json.dumps({"ok": False, "error": error}),
                        remove=True,
                    )
                except TypeError as exc:
                    self.fail(f"malformed Slack error escaped helper validation: {exc}")
                self.assertFalse(result["ok"])
                self.assertTrue(result["error"])
                self.assertFalse(result["no_reaction"])

    def test_not_reacted_add_failure_preserves_add_before_remove(self):
        for returncode in (0, 3):
            with self.subTest(returncode=returncode):
                commands = []

                def not_reacted(cmd, *, timeout=120):
                    commands.append(cmd)
                    return subprocess.CompletedProcess(
                        cmd, returncode, '{"ok": false, "error": "not_reacted"}', ""
                    )

                with mock.patch.object(m, "run", not_reacted):
                    result = m.apply_transition(
                        PERMALINK,
                        m.EMOJI_BLOCK,
                        {"emoji": m.EMOJI_ACTIVE},
                        m.slack_reaction,
                        m.owned_reactions(PERMALINK),
                    )

                self.assertFalse(result["ok"])
                self.assertEqual(result["prior"], m.EMOJI_ACTIVE)
                self.assertEqual(len(commands), 1)
                self.assertNotIn("--remove", commands[0])

    def test_remove_passes_the_remove_flag_and_exact_permalink(self):
        import subprocess

        recorded = {}

        def fake_run(cmd, *, timeout=120):
            recorded["cmd"] = cmd
            return subprocess.CompletedProcess(cmd, 0, '{"ok": true, "no_reaction": true}', "")

        original = m.run
        m.run = fake_run
        try:
            result = m.slack_reaction(PERMALINK, m.EMOJI_BLOCK, True)
        finally:
            m.run = original
        self.assertTrue(result["ok"])
        self.assertEqual(recorded["cmd"][1:], [PERMALINK, "--emoji", m.EMOJI_BLOCK, "--remove"])

    def test_a_failing_helper_result_never_transitions_the_post(self):
        # End to end: an rc=0 empty-stdout helper must retain the prior reaction.
        rows = [lane("t_a", "done")]
        shows = {"t_a": show("t_a", {"verdict": "NEEDS_FIX"})}
        state = m.migrate_state(None, NOW)[0]
        state["machine_baseline_at"] = 0
        state["posts"][PERMALINK] = {
            "emoji": m.EMOJI_ACTIVE, "applied_at": NOW - 600, "pending_removals": [],
            "lanes": {"t_a": m.EMOJI_ACTIVE},
        }

        def silent_helper(permalink, emoji, remove):
            return {"ok": False, "error": "slack_react rc=0 produced no valid JSON result: ''"}

        result = tick(rows, shows, state=state, slack=silent_helper)
        self.assertEqual(result["code"], 1)
        self.assertIn("react_failed", "\n".join(result["outputs"]))
        self.assertEqual(state["posts"][PERMALINK]["emoji"], m.EMOJI_ACTIVE)
        self.assertEqual(state["posts"][PERMALINK]["want"], m.EMOJI_BLOCK)


def json_roundtrip(value):
    import json

    return json.loads(json.dumps(value))


if __name__ == "__main__":
    unittest.main()
