import json
import argparse
import tempfile
import unittest
import importlib.util
from pathlib import Path
from typing import Any
from unittest.mock import patch

try:
    from cron.scripts import just_do_it_blocked_review_requeue_watchdog as m
except ImportError:
    module_path = Path(__file__).with_name(
        "just_do_it_blocked_review_requeue_watchdog.py"
    )
    spec = importlib.util.spec_from_file_location(
        "just_do_it_blocked_review_requeue_watchdog", module_path
    )
    if spec is None or spec.loader is None:
        raise ImportError(f"cannot load watchdog from {module_path}")
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)


CHANNEL = "C08K8FQKX9S"
WATCHED_CHANNELS = (
    "C08K8FQKX9S",
    "C07BU8AAPB3",
    "C09339EGZ9U",
)
EXPECTED_ALLOWED_CHANNELS = set(WATCHED_CHANNELS)
PERMALINK = f"https://ylopo.slack.com/archives/{CHANNEL}/p1720000000000000"
PR_URL = "https://github.com/Ylopo/example/pull/42"
PR_696_URL = "https://github.com/Ylopo/npm-ylopo-models-bookshelf/pull/696"
HEIMDALL_PR_URL = "https://github.com/Ylopo/heimdall-tls-service/pull/10"


def direct_review_url(review_id):
    return f"{HEIMDALL_PR_URL}#pullrequestreview-{review_id}"


def review_show(**metadata):
    return {
        "task": {"id": "t_review42", "status": "done", "assignee": "reviewer"},
        "runs": [
            {
                "id": "run-42",
                "completed_at": 1784030400,
                "profile": "reviewer",
                "metadata": {
                    "pr_url": "https://github.com/Ylopo/example/pull/42/files#diff-1",
                    "source_slack_permalink": PERMALINK,
                    **metadata,
                },
            }
        ],
    }


def candidate():
    return {
        "task_id": "t_review42",
        "pr_url": PR_URL,
        "reviewed_head": "a" * 40,
        "source_permalink": PERMALINK,
        "findings": [{"severity": "blocking", "issue": "wrong docs"}],
        "completed_at": 1784030400,
        "run_id": "run-42",
    }


def live(**overrides):
    return {
        "state": "OPEN",
        "isDraft": False,
        "headRefOid": "b" * 40,
        "author": {"login": "barwin"},
        "headRepositoryOwner": {"login": "barwin"},
        "headRefName": "fix/docs",
        **overrides,
    }


def existing_lane(head):
    return {
        "id": "t_existing",
        "assignee": "reviewer",
        "body": f"PR: {PR_URL}\nNew live head: {head}",
    }


class BlockedCandidateTest(unittest.TestCase):
    def test_pr_number_spelling_is_canonicalized_to_decimal_identity(self):
        self.assertEqual(
            m.canonical_pr_url("https://github.com/Ylopo/example/pull/00042/files"),
            PR_URL,
        )
        self.assertIsNone(
            m.canonical_pr_url("https://github.com/Ylopo/example/pull/" + "9" * 5000)
        )

    def test_default_allowlist_matches_all_watched_channels(self):
        self.assertEqual(m.DEFAULT_ALLOWED_CHANNELS, EXPECTED_ALLOWED_CHANNELS)

    def test_each_watched_channel_is_accepted_by_default(self):
        for channel in WATCHED_CHANNELS:
            with self.subTest(channel=channel):
                show = review_show(
                    review_verdict="NEEDS-FIX",
                    reviewed_head_oid="a" * 40,
                    source_slack_permalink=(
                        f"https://ylopo.slack.com/archives/{channel}/p1720000000000000"
                    ),
                )
                self.assertIsNotNone(
                    m.blocked_candidate(show, m.DEFAULT_ALLOWED_CHANNELS)
                )

    def test_motivating_production_shaped_blocked_review_is_candidate(self):
        show = review_show(
            pr_url="https://github.com/Ylopo/npm-ylopo-models-bookshelf/pull/696",
            review_verdict="NEEDS-FIX",
            github_review_state="CHANGES_REQUESTED",
            reviewed_head_oid="4455c6d245f32b52e752097c49be5f20874a9d59",
            findings=[
                {
                    "severity": "blocking",
                    "issue": "Generated declaration output is stale",
                }
            ],
        )
        show["task"]["id"] = "t_b2eb8405"
        show["runs"][0]["ended_at"] = 1784034629
        del show["runs"][0]["completed_at"]

        item = m.blocked_candidate(show, {CHANNEL})

        self.assertEqual(item["task_id"], "t_b2eb8405")
        self.assertEqual(
            item["pr_url"],
            "https://github.com/Ylopo/npm-ylopo-models-bookshelf/pull/696",
        )
        self.assertEqual(
            item["reviewed_head"], "4455c6d245f32b52e752097c49be5f20874a9d59"
        )
        self.assertEqual(item["completed_at"], 1784034629)
        self.assertEqual(item["run_id"], "run-42")

    def test_exact_production_direct_hermes_review_shapes_are_candidates(self):
        shapes = (
            (
                "t_115edcba",
                {
                    "approved": False,
                    "pr": HEIMDALL_PR_URL,
                    "review": {
                        "id": 4789083303,
                        "state": "CHANGES_REQUESTED",
                        "url": direct_review_url(4789083303),
                    },
                    "reviewed_head": "a4565552ded7fed5e1831f712517521b6c2696e3",
                },
                "a4565552ded7fed5e1831f712517521b6c2696e3",
            ),
            (
                "t_544c2060",
                {
                    "accepted_for_human_review": False,
                    "exact_head": "e0c7f94f40d36cfb00e25d4e515a0a013900e8a8",
                    "pr": HEIMDALL_PR_URL,
                    "review": {
                        "id": 4790375147,
                        "state": "CHANGES_REQUESTED",
                        "url": direct_review_url(4790375147),
                    },
                },
                "e0c7f94f40d36cfb00e25d4e515a0a013900e8a8",
            ),
            (
                "t_238b7ef1",
                {
                    "accepted_for_human_review": False,
                    "exact_head": "e70bb3c3da062264924418e6aeb94084a44a4e0b",
                    "pr": HEIMDALL_PR_URL,
                    "review": {
                        "id": 4790752761,
                        "state": "CHANGES_REQUESTED",
                        "url": direct_review_url(4790752761),
                    },
                    "verdict": "NEEDS_FIX",
                },
                "e70bb3c3da062264924418e6aeb94084a44a4e0b",
            ),
        )
        for task_id, metadata, expected_head in shapes:
            with self.subTest(task_id=task_id):
                show = review_show()
                show["task"]["id"] = task_id
                show["runs"][0]["metadata"] = metadata

                item = m.blocked_candidate(show, m.DEFAULT_ALLOWED_CHANNELS)
                assert item is not None

                self.assertEqual(item["task_id"], task_id)
                self.assertEqual(item["pr_url"], HEIMDALL_PR_URL)
                self.assertEqual(item["reviewed_head"], expected_head)
                self.assertIsNone(item["source_permalink"])
                self.assertEqual(item["review_url"], metadata["review"]["url"])

    def test_no_permalink_with_structured_pr_and_exact_head_is_candidate(self):
        show = review_show()
        show["runs"][0]["metadata"] = {
            "pr": HEIMDALL_PR_URL,
            "reviewed_head": "a" * 40,
            "verdict": "NEEDS_FIX",
        }

        item = m.blocked_candidate(show, m.DEFAULT_ALLOWED_CHANNELS)

        assert item is not None
        self.assertIsNone(item["source_permalink"])
        self.assertIsNone(item["review_url"])

    def test_optional_review_metadata_must_not_contradict_pr_or_exact_head(self):
        base = {
            "pr": HEIMDALL_PR_URL,
            "reviewed_head": "a" * 40,
            "review": {
                "id": 4790752761,
                "state": "CHANGES_REQUESTED",
                "url": direct_review_url(4790752761),
            },
        }
        invalid_reviews = (
            {
                **base["review"],
                "url": "https://github.com/Ylopo/other/pull/10#pullrequestreview-4790752761",
            },
            {**base["review"], "commit_id": "b" * 40},
        )
        for review in invalid_reviews:
            with self.subTest(review=review):
                show = review_show()
                show["runs"][0]["metadata"] = {**base, "review": review}
                self.assertIsNone(m.blocked_candidate(show, m.DEFAULT_ALLOWED_CHANNELS))

    def test_all_nested_review_containers_and_aliases_must_agree(self):
        base = {
            "pr": HEIMDALL_PR_URL,
            "reviewed_head": "a" * 40,
            "review": {
                "id": 4790752761,
                "state": "CHANGES_REQUESTED",
                "commit": "a" * 40,
                "url": direct_review_url(4790752761),
            },
        }
        conflicting_github_reviews = (
            {**base["review"], "commit": "b" * 40},
            {
                **base["review"],
                "url": "https://github.com/Ylopo/other/pull/10#pullrequestreview-4790752761",
            },
            {**base["review"], "id": 4790752762},
            {**base["review"], "state": "APPROVED"},
            "not-a-container",
            17,
        )
        for github_review in conflicting_github_reviews:
            with self.subTest(github_review=github_review):
                show = review_show()
                show["runs"][0]["metadata"] = {
                    **base,
                    "github_review": github_review,
                }
                self.assertIsNone(m.blocked_candidate(show, m.DEFAULT_ALLOWED_CHANNELS))

        huge_id = "9" * 5000
        show = review_show()
        show["runs"][0]["metadata"] = {
            "pr": HEIMDALL_PR_URL,
            "reviewed_head": "a" * 40,
            "review_url": f"{HEIMDALL_PR_URL}#pullrequestreview-{huge_id}",
        }
        self.assertIsNone(m.blocked_candidate(show, m.DEFAULT_ALLOWED_CHANNELS))

    def test_task_body_pr_can_bind_structured_review_url_when_metadata_pr_is_absent(
        self,
    ):
        show = review_show()
        show["task"]["body"] = f"Re-review the exact head of {HEIMDALL_PR_URL}."
        show["runs"][0]["metadata"] = {
            "head_sha": "e9518f8d0d107501be3a72aed08bdcc38190e289",
            "review_state": "CHANGES_REQUESTED",
            "review_url": direct_review_url(4796154282),
        }

        item = m.blocked_candidate(show, m.DEFAULT_ALLOWED_CHANNELS)

        assert item is not None
        self.assertEqual(item["pr_url"], HEIMDALL_PR_URL)
        self.assertEqual(item["run_id"], "run-42")

        show["task"]["body"] = "Re-review https://github.com/Ylopo/other/pull/10"
        self.assertIsNone(m.blocked_candidate(show, m.DEFAULT_ALLOWED_CHANNELS))

    def test_all_prior_verdicts_and_trigger_sources_are_eligible(self):
        shapes = (
            {
                "pr_url": PR_URL,
                "reviewed_head_sha": "a" * 40,
                "review_verdict": "APPROVED",
                "source_slack_permalink": PERMALINK,
            },
            {
                "pr": PR_URL,
                "exact_head": "b" * 40,
                "verdict": "NEEDS_FIX",
                "jira_issue": "OPS-42",
            },
            {
                "pr": PR_URL,
                "target_head": "c" * 40,
                "review": {"state": "COMMENTED", "commit_id": "c" * 40},
                "created_by": "manual-kanban",
            },
            {
                "pr": PR_URL,
                "review": {"state": "CHANGES_REQUESTED", "commit": "d" * 40},
                "created_by": "another-cron",
            },
        )
        for metadata in shapes:
            with self.subTest(metadata=metadata):
                show = review_show()
                show["runs"][0]["metadata"] = metadata
                self.assertIsNotNone(
                    m.blocked_candidate(show, m.DEFAULT_ALLOWED_CHANNELS)
                )

    def test_task_must_be_done_and_assigned_reviewer(self):
        for field, value in (("status", "ready"), ("assignee", "engineer")):
            with self.subTest(field=field):
                show = review_show(
                    review_verdict="NEEDS-FIX", reviewed_head_oid="a" * 40
                )
                show["task"][field] = value
                self.assertIsNone(m.blocked_candidate(show, {CHANNEL}))

    def test_explicit_non_reviewer_run_is_rejected(self):
        show = review_show(review_verdict="NEEDS-FIX", reviewed_head_oid="a" * 40)
        show["runs"][0]["profile"] = "engineer"
        self.assertIsNone(m.blocked_candidate(show, {CHANNEL}))

    def test_needs_fix_run_becomes_candidate(self):
        show = review_show(
            review_verdict="NEEDS-FIX",
            reviewed_head_oid="a" * 40,
            findings=[
                {
                    "severity": "blocking",
                    "issue": "wrong docs",
                    "recommended_fix": "correct docs",
                }
            ],
        )

        candidate = m.blocked_candidate(show, {CHANNEL})

        self.assertEqual(candidate["task_id"], "t_review42")
        self.assertEqual(candidate["run_id"], "run-42")
        self.assertEqual(
            candidate["pr_url"], "https://github.com/Ylopo/example/pull/42"
        )
        self.assertEqual(candidate["reviewed_head"], "a" * 40)
        self.assertEqual(candidate["source_permalink"], PERMALINK)

    def test_underscore_needs_fix_alias_is_normalized(self):
        show = review_show(verdict="NEEDS_FIX", reviewed_head_oid="a" * 40)

        self.assertIsNotNone(m.blocked_candidate(show, {CHANNEL}))

    def test_approved_run_is_candidate(self):
        show = review_show(
            review_verdict="APPROVED",
            github_review={"state": "APPROVED"},
            reviewed_head_sha="b" * 40,
        )

        item = m.blocked_candidate(show, {CHANNEL})

        assert item is not None
        self.assertEqual(item["review_outcomes"], ["APPROVED"])

    def test_comment_only_no_permalink_run_is_candidate(self):
        show = review_show()
        show["runs"][0]["metadata"] = {
            "pr": PR_URL,
            "reviewed_head": "c" * 40,
            "verdict": "COMMENT",
            "review": {"state": "COMMENTED"},
            "source": "direct-hermes-slack",
        }

        item = m.blocked_candidate(show, m.DEFAULT_ALLOWED_CHANNELS)

        assert item is not None
        self.assertEqual(item["reviewed_head"], "c" * 40)
        self.assertIsNone(item["source_permalink"])
        self.assertEqual(item["review_outcomes"], ["COMMENT", "COMMENTED"])

    def test_conflicting_structured_pr_head_or_review_id_fails_closed(self):
        invalid_metadata = (
            {
                "pr": PR_URL,
                "pr_url": "https://github.com/Ylopo/other/pull/42",
                "reviewed_head": "a" * 40,
            },
            {
                "pr": PR_URL,
                "reviewed_head": "a" * 40,
                "exact_head": "b" * 40,
            },
            {
                "pr": PR_URL,
                "reviewed_head": "a" * 40,
                "review": {
                    "id": 999,
                    "url": f"{PR_URL}#pullrequestreview-1000",
                },
            },
        )
        for metadata in invalid_metadata:
            with self.subTest(metadata=metadata):
                show = review_show()
                show["runs"][0]["metadata"] = metadata
                self.assertIsNone(m.blocked_candidate(show, m.DEFAULT_ALLOWED_CHANNELS))

    def test_missing_exact_head_is_rejected_and_unwatched_source_is_optional(self):
        without_sha = review_show(review_verdict="NEEDS-FIX")
        other_channel = review_show(
            review_verdict="NEEDS-FIX",
            reviewed_commit="c" * 40,
            source_slack_permalink=(
                "https://ylopo.slack.com/archives/C0000000000/p1720000000000000"
            ),
        )

        self.assertIsNone(m.blocked_candidate(without_sha, m.DEFAULT_ALLOWED_CHANNELS))
        item = m.blocked_candidate(other_channel, m.DEFAULT_ALLOWED_CHANNELS)
        assert item is not None
        self.assertIsNone(item["source_permalink"])

    def test_aliases_and_findings_are_normalized(self):
        show = review_show(
            github_review_state="CHANGES_REQUESTED",
            reviewed_commit="d" * 40,
            source_permalink=PERMALINK,
            source_slack_permalink=None,
            blockers=[{"issue": "unsafe"}, "missing test", 17, None],
        )

        candidate = m.blocked_candidate(show, {CHANNEL})

        self.assertEqual(candidate["reviewed_head"], "d" * 40)
        self.assertEqual(candidate["findings"], [{"issue": "unsafe"}, "missing test"])
        json.dumps(candidate)

    def test_nested_github_state_and_reviewed_head_sha_are_accepted(self):
        show = review_show(
            github_review={"state": "CHANGES_REQUESTED"},
            reviewed_head_sha="e" * 40,
            findings="requested changes in review",
        )

        candidate = m.blocked_candidate(show, {CHANNEL})

        self.assertEqual(candidate["findings"], ["requested changes in review"])

    def test_invalid_url_and_sha_are_rejected_but_invalid_source_is_ignored(self):
        invalid_url = review_show(
            pr_url="https://example.com/Ylopo/example/pull/42",
            review_verdict="NEEDS-FIX",
            reviewed_head_oid="a" * 40,
        )
        invalid_sha = review_show(
            review_verdict="NEEDS-FIX", reviewed_head_oid="abc123"
        )
        invalid_permalink = review_show(
            review_verdict="NEEDS-FIX",
            reviewed_head_oid="a" * 40,
            source_slack_permalink="https://evil.example/archives/C08K8FQKX9S/p1720000000000000",
        )

        self.assertIsNone(m.blocked_candidate(invalid_url, {CHANNEL}))
        self.assertIsNone(m.blocked_candidate(invalid_sha, {CHANNEL}))
        item = m.blocked_candidate(invalid_permalink, {CHANNEL})
        assert item is not None
        self.assertIsNone(item["source_permalink"])

    def test_run_without_completion_timestamp_is_rejected(self):
        show = review_show(review_verdict="NEEDS-FIX", reviewed_head_oid="a" * 40)
        show["runs"][0]["completed_at"] = None

        self.assertIsNone(m.blocked_candidate(show, {CHANNEL}))

    def test_non_completed_status_or_outcome_is_rejected(self):
        for field, value in (
            ("status", "running"),
            ("status", "failed"),
            ("outcome", "reclaimed"),
        ):
            with self.subTest(field=field, value=value):
                show = review_show(
                    review_verdict="NEEDS-FIX", reviewed_head_oid="a" * 40
                )
                show["runs"][0][field] = value

                self.assertIsNone(m.blocked_candidate(show, {CHANNEL}))

    def test_newest_qualifying_run_is_selected(self):
        show = review_show(review_verdict="NEEDS-FIX", reviewed_head_oid="a" * 40)
        show["runs"].append({
            "id": "run-new",
            "completed_at": 1784116800,
            "profile": "reviewer",
            "metadata": {
                "pr_url": "https://github.com/Ylopo/example/pull/42",
                "review_verdict": "NEEDS-FIX",
                "reviewed_head_oid": "f" * 40,
                "source_slack_permalink": PERMALINK,
                "findings": [],
            },
        })

        candidate = m.blocked_candidate(show, {CHANNEL})

        self.assertEqual(candidate["run_id"], "run-new")
        self.assertEqual(candidate["reviewed_head"], "f" * 40)


class LivePrAndDispatchTest(unittest.TestCase):
    @patch.object(m, "run", create=True)
    def test_fetch_live_pr_requests_required_fields(self, run):
        run.return_value = json.dumps(live())

        self.assertEqual(m.fetch_live_pr(PR_URL), live())

        self.assertEqual(
            run.call_args.args[0],
            [
                "/Users/ctao/.hermes/scripts/pr/ghx.sh",
                "pr",
                "view",
                PR_URL,
                "--json",
                "state,isDraft,headRefOid,author,headRepositoryOwner,headRefName",
            ],
        )

    @patch.object(m, "run", create=True)
    def test_fetch_live_pr_rejects_malformed_or_failed_output(self, run):
        for output in ("not json", '{"error":"authentication failed"}'):
            with self.subTest(output=output):
                run.return_value = output
                with self.assertRaisesRegex(RuntimeError, "live PR lookup"):
                    m.fetch_live_pr(PR_URL)

    @patch.object(m, "run", create=True)
    def test_fetch_live_pr_rejects_invalid_field_values(self, run):
        invalid_values = {
            "headRefOid": "not-a-sha",
            "state": "UNKNOWN",
            "isDraft": "false",
            "author": {"login": ""},
            "headRepositoryOwner": None,
            "headRefName": "",
        }
        for field, value in invalid_values.items():
            with self.subTest(field=field, value=value):
                run.return_value = json.dumps(live(**{field: value}))
                with self.assertRaisesRegex(RuntimeError, "live PR lookup"):
                    m.fetch_live_pr(PR_URL)

    def test_live_head_change_builds_third_party_read_only_lane(self):
        body = m.build_rereview_body(candidate(), live())

        self.assertIn("Old reviewed head: " + "a" * 40, body)
        self.assertIn("New live head: " + "b" * 40, body)
        self.assertIn("STRICTLY READ-ONLY", body)
        self.assertIn("Do NOT edit code", body)
        self.assertIn(candidate()["source_permalink"], body)
        self.assertIn("wrong docs", body)

    def test_existing_lane_for_target_head_is_detected(self):
        self.assertEqual(
            m.target_lane_exists([existing_lane("b" * 40)], PR_URL, "b" * 40),
            "t_existing",
        )

    def test_unbound_active_latest_head_lane_does_not_suppress_exact_head(self):
        lane = {
            "id": "t_7141c3c7",
            "title": "Rereview Ylopo/heimdall-tls-service PR #10",
            "status": "running",
            "assignee": "reviewer",
            "body": (
                f"Perform an independent, read-only rereview of {HEIMDALL_PR_URL} "
                "for Chad Tao. Review the latest head and determine whether prior "
                "requested changes are addressed."
            ),
        }

        self.assertIsNone(
            m.target_lane_exists(
                [lane],
                HEIMDALL_PR_URL,
                "e9518f8d0d107501be3a72aed08bdcc38190e289",
            )
        )

    def test_completed_or_non_rereview_generic_latest_head_lane_does_not_dedupe(self):
        base = {
            "id": "t_generic",
            "title": "Review Ylopo/heimdall-tls-service PR #10",
            "status": "running",
            "assignee": "reviewer",
            "body": f"Review the latest head of {HEIMDALL_PR_URL}",
        }
        for lane in ({**base, "status": "done"}, base):
            with self.subTest(lane=lane):
                self.assertIsNone(
                    m.target_lane_exists(
                        [lane],
                        HEIMDALL_PR_URL,
                        "e9518f8d0d107501be3a72aed08bdcc38190e289",
                    )
                )

    @patch.object(m, "run", create=True)
    def test_completed_manual_latest_head_lane_is_bound_by_run_metadata(self, run):
        head = "e9518f8d0d107501be3a72aed08bdcc38190e289"
        lane = {
            "id": "t_7141c3c7",
            "title": "Rereview Ylopo/heimdall-tls-service PR #10",
            "status": "done",
            "assignee": "reviewer",
            "body": f"Review the latest head of {HEIMDALL_PR_URL}",
        }
        run.return_value = json.dumps({
            "task": lane,
            "runs": [
                {
                    "id": 1697,
                    "profile": "reviewer",
                    "status": "done",
                    "outcome": "completed",
                    "ended_at": 1785233587,
                    "metadata": {
                        "head_sha": head,
                        "review_state": "CHANGES_REQUESTED",
                        "review_url": (
                            f"{HEIMDALL_PR_URL}#pullrequestreview-4796154282"
                        ),
                    },
                }
            ],
        })

        self.assertEqual(
            m.find_target_lane([lane], HEIMDALL_PR_URL, head),
            "t_7141c3c7",
        )
        self.assertEqual(
            run.call_args.args[0],
            ["hermes", "kanban", "show", "t_7141c3c7", "--json"],
        )

    def test_engineer_lane_for_target_head_does_not_dedupe(self):
        lane = existing_lane("b" * 40)
        lane["assignee"] = "engineer"

        self.assertIsNone(m.target_lane_exists([lane], PR_URL, "b" * 40))

    def test_actual_exact_head_sha_reviewer_lane_is_detected(self):
        head = "c40fb1db61c6c02625904017d67647e512576598"
        lane = {
            "id": "t_fc423563",
            "assignee": "reviewer",
            "body": f"PR: {PR_696_URL}\nExact head SHA: {head}",
        }

        self.assertEqual(m.target_lane_exists([lane], PR_696_URL, head), "t_fc423563")

    def test_body_match_requires_same_pr_and_full_sha(self):
        head = "c40fb1db61c6c02625904017d67647e512576598"
        for lane in (
            {
                "id": "t_other_head",
                "assignee": "reviewer",
                "body": f"PR: {PR_696_URL}\nExact head SHA: {'d' * 40}",
            },
            {
                "id": "t_other_pr",
                "assignee": "reviewer",
                "body": f"PR: https://github.com/Ylopo/other/pull/696\nExact head SHA: {head}",
            },
            {
                "id": "t_engineer",
                "assignee": "engineer",
                "body": f"PR: {PR_696_URL}\nExact head SHA: {head}",
            },
        ):
            with self.subTest(task=lane["id"]):
                self.assertIsNone(m.target_lane_exists([lane], PR_696_URL, head))

    def test_existing_lane_pr_comparison_is_case_insensitive(self):
        lane = existing_lane("b" * 40)
        lane["body"] = lane["body"].replace("Ylopo/example", "YLOPO/EXAMPLE")

        self.assertEqual(m.target_lane_exists([lane], PR_URL, "b" * 40), "t_existing")

    def test_closed_unchanged_or_malformed_pr_is_not_dispatchable(self):
        self.assertFalse(m.should_dispatch(candidate(), live(state="CLOSED")))
        self.assertFalse(m.should_dispatch(candidate(), live(headRefOid="a" * 40)))
        self.assertFalse(m.should_dispatch(candidate(), live(headRefOid="not-a-sha")))

    def test_open_draft_from_third_party_author_is_dispatchable(self):
        self.assertTrue(m.should_dispatch(candidate(), live(isDraft=True)))

    def test_ylopo_head_repository_from_third_party_author_is_dispatchable(self):
        self.assertTrue(
            m.should_dispatch(candidate(), live(headRepositoryOwner={"login": "Ylopo"}))
        )

    def test_chad_authored_pr_is_dispatchable(self):
        self.assertTrue(
            m.should_dispatch(candidate(), live(author={"login": "Chadtao206"}))
        )

    def test_body_has_full_contract_and_conditional_third_party_safety(self):
        third_party = m.build_rereview_body(candidate(), live())
        chad = m.build_rereview_body(candidate(), live(author={"login": "Chadtao206"}))
        for text in (
            "Live author: barwin",
            "Head owner: barwin",
            "Live state: OPEN",
            "Draft: False",
            "Old reviewed head:",
            "New live head:",
            "re-fetch",
            "exact head",
            "duplicate",
            "Chadtao206",
            "approval",
            "read-only inspect",
            "tests",
            "GitHub review submission",
            "branch update",
            "commit",
            "push",
            "PR body",
            "metadata",
            "engineer",
            "remediation",
            "remaining blockers",
            "author",
            "pr_url",
            "reviewed head",
            "verdict/findings",
            "review state/id/url/commit",
            "validation/CI",
            "approval submitted/already existed",
            "reaction result",
        ):
            self.assertIn(text.lower(), third_party.lower())
        self.assertIn(candidate()["source_permalink"], third_party)
        self.assertIn("strict third-party guard", third_party.lower())
        self.assertNotIn("strict third-party guard", chad.lower())
        self.assertIn("no merge", chad.lower())

    def test_direct_review_body_has_valid_non_slack_source_contract(self):
        item = candidate()
        item["source_permalink"] = None
        item["review_url"] = direct_review_url(4790752761)

        body = m.build_rereview_body(item, live())

        self.assertIn(direct_review_url(4790752761), body)
        self.assertIn("structured completed reviewer run", body)
        self.assertNotIn("Source review: None", body)
        self.assertNotIn("reaction result", body)

    @patch.object(m, "run", create=True)
    def test_create_rereview_uses_safe_command_and_verifies_lane(self, run):
        run.side_effect = [
            json.dumps({"id": "t_new"}),
            json.dumps({
                "task": {"id": "t_new", "status": "ready", "assignee": "reviewer"},
                "runs": [],
            }),
        ]

        self.assertEqual(m.create_rereview(candidate(), live()), "t_new")

        create_command = run.call_args_list[0].args[0]
        self.assertEqual(create_command[:3], ["hermes", "kanban", "create"])
        self.assertIn("--body", create_command)
        self.assertIn(
            "STRICTLY READ-ONLY", create_command[create_command.index("--body") + 1]
        )
        self.assertIn(
            "just-do-it-rereview:ylopo/example#42:" + "b" * 40,
            create_command,
        )
        self.assertEqual(
            run.call_args_list[1].args[0],
            ["hermes", "kanban", "show", "t_new", "--json"],
        )

    @patch.object(m, "run", create=True)
    def test_create_rereview_rejects_unverified_lane(self, run):
        run.side_effect = [
            json.dumps({"id": "t_new"}),
            json.dumps({
                "task": {"id": "t_new", "status": "done", "assignee": "reviewer"},
                "runs": [],
            }),
        ]

        with self.assertRaisesRegex(RuntimeError, "created re-review lane"):
            m.create_rereview(candidate(), live())


class OrchestrationTest(unittest.TestCase):
    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory()
        self.state_path = Path(self.tempdir.name) / "state.json"
        self.args = argparse.Namespace(
            dry_run=False,
            backfill_existing=False,
            lookback_seconds=7 * 24 * 3600,
            only_task=None,
            channel=[CHANNEL],
        )
        self.tasks: list[dict[str, Any]] = [
            {"id": "t_review42", "status": "done", "assignee": "reviewer"}
        ]
        self.shows = {
            "t_review42": review_show(
                review_verdict="NEEDS-FIX", reviewed_head_oid="a" * 40
            )
        }
        self.live_pr = live()
        self.live_review = {
            "id": 4790752761,
            "state": "CHANGES_REQUESTED",
            "html_url": direct_review_url(4790752761),
            "commit_id": "a" * 40,
            "user": {"login": "Chadtao206"},
        }
        self.created = []
        self.fail_create = False
        self.fail_list = False
        self.fail_show = False
        self.fail_github = False
        self.fail_readback = False
        self.reconcile_created_lane = False
        self.reconciled_lane_assignee = "reviewer"
        self.list_calls = 0
        self.show_calls = 0
        self.github_calls = 0

    def tearDown(self):
        self.tempdir.cleanup()

    def write_state(self, state):
        self.state_path.write_text(json.dumps(state))

    def tick(self, now=1784116800):
        def fake_run(command):
            if command[:3] == ["hermes", "kanban", "list"]:
                self.list_calls += 1
                if self.fail_list:
                    raise RuntimeError("command failed: hermes")
                return json.dumps(self.tasks)
            if command[:3] == ["hermes", "kanban", "show"]:
                self.show_calls += 1
                task_id = command[3]
                if task_id.startswith("t_new"):
                    if self.fail_readback:
                        if self.reconcile_created_lane:
                            self.tasks.append({
                                "id": "t_fc423563",
                                "status": "ready",
                                "assignee": self.reconciled_lane_assignee,
                                "body": f"PR: {PR_URL}\nNew live head: {'b' * 40}",
                            })
                        raise RuntimeError("command failed: hermes")
                    return json.dumps({
                        "task": {
                            "id": task_id,
                            "status": "ready",
                            "assignee": "reviewer",
                        },
                        "runs": [],
                    })
                if self.fail_show:
                    raise RuntimeError("command failed: hermes")
                return json.dumps(self.shows[task_id])
            if command[0] == str(m.GHX):
                self.github_calls += 1
                if self.fail_github:
                    raise RuntimeError("command failed: ghx.sh")
                if command[1] == "api":
                    return json.dumps(self.live_review)
                return json.dumps(self.live_pr)
            if command[:3] == ["hermes", "kanban", "create"]:
                if self.fail_create:
                    raise RuntimeError("command failed: hermes")
                task_id = f"t_new{len(self.created) + 1}"
                self.created.append(command)
                return json.dumps({"id": task_id})
            raise AssertionError(command)

        with (
            patch.object(m, "STATE_PATH", self.state_path),
            patch.object(m, "run", side_effect=fake_run),
        ):
            return m.process_tick(self.args, now)

    def test_new_head_dispatches_exactly_once(self):
        self.write_state({
            "baseline_completed_at": 1784000000,
            "dispatched": {},
            "failures": {},
        })

        rc, output = self.tick()
        self.assertEqual(
            (rc, output),
            (
                0,
                [
                    f"JUST_DO_IT_BLOCKED_REREVIEW dispatched pr={PR_URL} head={'b' * 7} task=t_new1"
                ],
            ),
        )
        self.assertEqual(len(self.created), 1)

        self.assertEqual(self.tick(), (0, []))
        self.assertEqual(len(self.created), 1)

    def test_first_run_bounded_backfill_enrolls_and_dispatches_historical_open_pr(self):
        rc, output = self.tick()

        self.assertEqual(rc, 0)
        self.assertEqual(len(self.created), 1)
        self.assertIn("dispatched", output[0])
        state = json.loads(self.state_path.read_text())
        self.assertEqual(state["state_schema_version"], m.STATE_SCHEMA_VERSION)
        self.assertIn(PR_URL.lower(), state["enrolled"])

    def test_same_tick_decimal_equivalent_candidates_create_one_canonical_lane(self):
        now = 1784116800
        canonical = "https://github.com/Ylopo/example/pull/10"
        alias = "https://github.com/Ylopo/example/pull/010"
        target_head = "b" * 40
        self.tasks = []
        self.shows = {}
        for index, pr_url in enumerate((canonical, alias), start=1):
            task_id = f"t_decimal_{index}"
            completed_at = now - 10 + index
            show = review_show(
                pr_url=pr_url,
                review_verdict="NEEDS-FIX",
                reviewed_head_oid="a" * 40,
            )
            show["task"]["id"] = task_id
            show["runs"][0]["id"] = f"run-decimal-{index}"
            show["runs"][0]["completed_at"] = completed_at
            self.tasks.append({
                "id": task_id,
                "status": "done",
                "assignee": "reviewer",
                "completed_at": completed_at,
            })
            self.shows[task_id] = show
        self.live_pr = live(headRefOid=target_head)

        rc, output = self.tick(now=now)

        self.assertEqual(rc, 0)
        self.assertEqual(
            output,
            [
                "JUST_DO_IT_BLOCKED_REREVIEW "
                f"dispatched pr={canonical} head={target_head[:7]} task=t_new1"
            ],
        )
        self.assertEqual(len(self.created), 1)
        create_command = self.created[0]
        idempotency_key = create_command[create_command.index("--idempotency-key") + 1]
        self.assertEqual(
            idempotency_key,
            f"just-do-it-rereview:ylopo/example#10:{target_head}",
        )

        state = json.loads(self.state_path.read_text())
        canonical_key = canonical.lower()
        ledger_key = f"{canonical_key}|{target_head}"
        self.assertEqual(list(state["enrolled"]), [canonical_key])
        self.assertEqual(state["enrolled"][canonical_key]["pr_url"], canonical)
        self.assertEqual(list(state["dispatched"]), [ledger_key])
        self.assertEqual(state["dispatched"][ledger_key]["task_id"], "t_new1")
        self.assertNotIn(alias.lower(), state["enrolled"])
        self.assertFalse(any("/pull/010" in key for key in state["dispatched"]))

    def test_backfill_bypasses_baseline_and_head_changes_dispatch_new_lane(self):
        self.args.backfill_existing = True
        self.assertEqual(self.tick()[0], 0)
        self.live_pr["headRefOid"] = "c" * 40
        self.assertEqual(self.tick()[0], 0)
        self.assertEqual(len(self.created), 2)

    def test_direct_hermes_candidate_is_live_verified_before_dispatch(self):
        self.args.backfill_existing = True
        self.shows["t_review42"]["runs"][0]["metadata"] = {
            "pr": HEIMDALL_PR_URL,
            "reviewed_head": "a" * 40,
            "review": {
                "id": 4790752761,
                "state": "CHANGES_REQUESTED",
                "url": direct_review_url(4790752761),
            },
            "verdict": "NEEDS_FIX",
        }

        rc, output = self.tick()

        self.assertEqual(rc, 0)
        self.assertEqual(len(self.created), 1)
        self.assertIn(HEIMDALL_PR_URL, output[0])

    def test_dispatch_cap_is_three_with_backlog_on_later_tick(self):
        self.args.backfill_existing = True
        self.tasks = []
        self.shows = {}
        for index in range(4):
            task_id = f"t_review{index}"
            show = review_show(review_verdict="NEEDS-FIX", reviewed_head_oid="a" * 40)
            show["task"]["id"] = task_id
            show["runs"][0]["metadata"]["pr_url"] = (
                f"https://github.com/Ylopo/example/pull/{index + 1}"
            )
            self.tasks.append({"id": task_id, "status": "done", "assignee": "reviewer"})
            self.shows[task_id] = show

        self.assertEqual(self.tick()[0], 0)
        self.assertEqual(len(self.created), 3)

        self.assertEqual(self.tick(now=1784116801)[0], 0)
        self.assertEqual(len(self.created), 4)

    def test_failure_is_reported_then_backed_off_silently(self):
        self.args.backfill_existing = True
        self.fail_create = True
        rc, output = self.tick()
        self.assertEqual(rc, 1)
        self.assertIn("re-review lane creation", output[0])

        self.assertEqual(self.tick(now=1784116801), (0, []))

    def test_unchanged_closed_and_existing_lane_are_quiet(self):
        self.args.backfill_existing = True
        for changed_live, extra_tasks in (
            (live(headRefOid="a" * 40), []),
            (live(state="CLOSED"), []),
            (live(), [existing_lane("b" * 40)]),
        ):
            with self.subTest(live=changed_live["state"], existing=bool(extra_tasks)):
                self.live_pr = changed_live
                self.tasks = [
                    {"id": "t_review42", "status": "done", "assignee": "reviewer"},
                    *extra_tasks,
                ]
                self.shows.update({
                    task["id"]: {**task, "runs": []} for task in extra_tasks
                })
                self.assertEqual(self.tick(), (0, []))

    def test_list_failure_is_explicit(self):
        self.write_state({
            "baseline_completed_at": 1784000000,
            "dispatched": {},
            "failures": {},
        })
        self.fail_list = True
        rc, output = self.tick()
        self.assertEqual(rc, 1)
        self.assertIn("Kanban list", output[0])
        self.assertEqual(self.tick(now=1784116801), (0, []))

    def test_live_top_level_array_list_contract_and_malformed_outputs(self):
        with patch.object(m, "run", return_value='[{"id":"t_live"}]'):
            self.assertEqual(m._task_list(), [{"id": "t_live"}])

        for output in ('{"tasks":[]}', "not json", '[{"id":"ok"}, 7]'):
            with (
                self.subTest(output=output),
                patch.object(m, "run", return_value=output),
            ):
                with self.assertRaisesRegex(RuntimeError, "Kanban list"):
                    m._task_list()

    def test_show_failure_is_explicit_and_backed_off(self):
        self.write_state({
            "baseline_completed_at": 1784000000,
            "dispatched": {},
            "failures": {},
        })
        self.fail_show = True

        rc, output = self.tick()
        self.assertEqual(rc, 1)
        self.assertIn("Kanban show task=t_review42", output[0])
        self.assertEqual(self.tick(now=1784116801), (0, []))

    def test_live_github_failure_is_explicit_and_backed_off(self):
        self.args.backfill_existing = True
        self.fail_github = True

        rc, output = self.tick()
        self.assertEqual(rc, 1)
        self.assertIn("live PR lookup", output[0])
        self.assertEqual(self.tick(now=1784116801), (0, []))

    def test_readback_failure_refreshes_tasks_and_never_duplicates(self):
        self.args.backfill_existing = True
        self.fail_readback = True

        rc, output = self.tick()
        self.assertEqual(rc, 1)
        self.assertIn("created re-review lane verification", output[0])
        self.assertEqual(self.list_calls, 3)
        self.assertEqual(len(self.created), 1)

        self.fail_readback = False
        self.tasks.append(existing_lane("b" * 40))
        self.shows["t_existing"] = {"id": "t_existing", "runs": []}
        self.assertEqual(self.tick(now=1784120401), (0, []))
        self.assertEqual(len(self.created), 1)

    def test_readback_failure_immediately_reconciles_exactly_one_created_lane(self):
        self.args.backfill_existing = True
        self.fail_readback = True
        self.reconcile_created_lane = True

        rc, output = self.tick()

        self.assertEqual(rc, 0)
        self.assertIn("task=t_fc423563 reconciled=true", output[0])
        state = json.loads(self.state_path.read_text())
        key = f"{PR_URL.lower()}|{'b' * 40}"
        self.assertEqual(state["dispatched"][key]["task_id"], "t_fc423563")
        self.assertNotIn(key, state["failures"])

    def test_readback_failure_does_not_reconcile_engineer_lane(self):
        self.args.backfill_existing = True
        self.fail_readback = True
        self.reconcile_created_lane = True
        self.reconciled_lane_assignee = "engineer"

        rc, output = self.tick()

        self.assertEqual(rc, 1)
        self.assertIn("created re-review lane verification", output[0])
        state = json.loads(self.state_path.read_text())
        key = f"{PR_URL.lower()}|{'b' * 40}"
        self.assertNotIn(key, state["dispatched"])
        self.assertEqual(state["failures"][key], 1784116800)

    def test_dry_run_reports_existing_lane_for_production_shaped_candidate(self):
        self.args.backfill_existing = True
        self.args.dry_run = True
        lane = existing_lane("b" * 40)
        lane["id"] = "t_fc423563"
        lane.update(status="ready", assignee="reviewer")
        self.tasks.append(lane)

        rc, output = self.tick()

        self.assertEqual(rc, 0)
        self.assertEqual(len(self.created), 0)
        self.assertIn("WOULD_SKIP", output[0])
        self.assertIn("existing_lane=t_fc423563", output[0])

    def configure_merged_696(self, with_lane):
        self.args.backfill_existing = True
        self.live_pr = live(state="MERGED")
        self.shows["t_review42"]["runs"][0]["metadata"]["pr_url"] = PR_696_URL
        if with_lane:
            self.tasks.append({
                "id": "t_fc423563",
                "status": "ready",
                "assignee": "reviewer",
                "body": f"PR: {PR_696_URL}\nExact head SHA: {'b' * 40}",
            })

    def test_merged_696_dry_run_is_silent_before_lane_reconciliation(self):
        self.configure_merged_696(with_lane=True)
        self.args.dry_run = True

        rc, output = self.tick()

        self.assertEqual(rc, 0)
        self.assertEqual(self.created, [])
        self.assertEqual(output, [])

    def test_merged_696_without_existing_lane_is_silent(self):
        self.configure_merged_696(with_lane=False)

        self.assertEqual(self.tick(), (0, []))
        self.assertEqual(self.created, [])

    def test_merged_pr_is_pruned_and_tombstoned_without_create(self):
        self.configure_merged_696(with_lane=True)

        rc, output = self.tick()

        self.assertEqual((rc, output), (0, []))
        self.assertEqual(self.created, [])
        state = json.loads(self.state_path.read_text())
        self.assertNotIn(PR_696_URL.lower(), state["enrolled"])
        self.assertEqual(state["tombstones"][PR_696_URL.lower()]["state"], "MERGED")

    def test_persistent_registry_survives_restart_and_seven_day_review_age(self):
        first_now = 1784116800
        self.live_pr = live(headRefOid="a" * 40)

        self.assertEqual(self.tick(now=first_now), (0, []))
        persisted = json.loads(self.state_path.read_text())
        self.assertIn(PR_URL.lower(), persisted["enrolled"])

        self.tasks = []
        self.live_pr = live(headRefOid="c" * 40)
        restart_now = first_now + 8 * 24 * 3600
        rc, output = self.tick(now=restart_now)

        self.assertEqual(rc, 0)
        self.assertEqual(len(self.created), 1)
        self.assertIn("head=ccccccc", output[0])
        state = json.loads(self.state_path.read_text())
        self.assertIn(PR_URL.lower(), state["enrolled"])
        self.assertGreater(
            restart_now - state["enrolled"][PR_URL.lower()]["reviewed_at"],
            self.args.lookback_seconds,
        )

    def test_later_completed_boris_lane_advances_reviewed_head_for_successive_heads(
        self,
    ):
        first_now = 1784116800
        self.assertEqual(self.tick(now=first_now)[0], 0)
        self.assertEqual(len(self.created), 1)

        next_task = "t_review_new_head"
        next_show = review_show(
            pr_url=PR_URL,
            reviewed_head="b" * 40,
            verdict="APPROVED",
        )
        next_show["task"]["id"] = next_task
        next_show["runs"][0]["completed_at"] = first_now + 100
        self.shows[next_task] = next_show
        self.tasks.append({
            "id": next_task,
            "status": "done",
            "assignee": "reviewer",
            "completed_at": first_now + 100,
        })
        self.live_pr = live(headRefOid="c" * 40)

        self.assertEqual(
            self.tick(now=first_now + m.DISCOVERY_INTERVAL_SECONDS + 1)[0], 0
        )
        self.assertEqual(len(self.created), 2)
        state = json.loads(self.state_path.read_text())
        self.assertEqual(state["enrolled"][PR_URL.lower()]["reviewed_head"], "b" * 40)
        self.assertIn(f"{PR_URL.lower()}|{'b' * 40}", state["dispatched"])
        self.assertIn(f"{PR_URL.lower()}|{'c' * 40}", state["dispatched"])

    def test_same_task_later_completed_run_advances_across_state_reloads(self):
        first_now = 1784116800
        self.live_pr = live(headRefOid="a" * 40)
        self.assertEqual(self.tick(now=first_now), (0, []))

        task = self.tasks[0]
        task["completed_at"] = 1784030400
        task["updated_at"] = first_now + 100
        newer = review_show(pr_url=PR_URL, reviewed_head="b" * 40, verdict="APPROVED")[
            "runs"
        ][0]
        newer["id"] = "run-43"
        newer["completed_at"] = first_now + 100
        self.shows["t_review42"]["runs"].append(newer)
        self.live_pr = live(headRefOid="b" * 40)

        self.assertEqual(
            self.tick(now=first_now + m.DISCOVERY_INTERVAL_SECONDS + 1), (0, [])
        )
        state = json.loads(self.state_path.read_text())
        self.assertEqual(state["enrolled"][PR_URL.lower()]["reviewed_head"], "b" * 40)
        self.assertEqual(state["enrolled"][PR_URL.lower()]["source_run_id"], "run-43")

        task["updated_at"] = first_now + 700
        newest = review_show(pr_url=PR_URL, reviewed_head="c" * 40, verdict="COMMENT")[
            "runs"
        ][0]
        newest["id"] = "run-44"
        newest["completed_at"] = first_now + 700
        self.shows["t_review42"]["runs"].append(newest)
        self.live_pr = live(headRefOid="c" * 40)

        self.assertEqual(self.tick(now=first_now + 701), (0, []))
        state = json.loads(self.state_path.read_text())
        self.assertEqual(state["enrolled"][PR_URL.lower()]["reviewed_head"], "c" * 40)
        self.assertEqual(state["enrolled"][PR_URL.lower()]["source_run_id"], "run-44")

    def test_only_task_does_not_consume_other_schema_migration_pending_rows(self):
        now = 1784116800
        other_task = "t_other"
        other_show = review_show(
            pr_url="https://github.com/Ylopo/example/pull/43",
            reviewed_head="a" * 40,
        )
        other_show["task"]["id"] = other_task
        self.tasks.append({"id": other_task, "status": "done", "assignee": "reviewer"})
        self.shows[other_task] = other_show
        self.args.only_task = "t_review42"
        self.live_pr = live(headRefOid="a" * 40)
        self.write_state({
            "state_schema_version": 2,
            "dispatched": {
                f"{PR_URL.lower()}|{'a' * 40}": {
                    "task_id": "t_review42",
                    "dispatched_at": now - 10,
                },
                f"https://github.com/ylopo/example/pull/43|{'a' * 40}": {
                    "task_id": other_task,
                    "dispatched_at": now - 10,
                },
            },
            "failures": {},
            "enrolled": {},
            "tombstones": {},
            "discovery": {
                "last_run_at": now,
                "watermark_completed_at": now,
                "backfill_complete": True,
                "seen": {"t_review42": now - 100, other_task: now - 100},
                "pending": {},
            },
        })

        self.assertEqual(self.tick(now=now), (0, []))
        self.assertEqual(self.show_calls, 1)
        state = json.loads(self.state_path.read_text())
        self.assertIn(other_task, state["discovery"]["pending"])

    def test_malformed_discovery_scalars_are_quarantined_without_crashing_tick(self):
        self.write_state({
            "dispatched": {},
            "failures": {},
            "enrolled": {},
            "tombstones": {},
            "discovery": {
                "last_run_at": "bad",
                "watermark_completed_at": [1784116800],
                "backfill_complete": "yes",
                "seen": {"t_bad": "not-an-epoch"},
                "pending": {"t_bad": 17},
            },
        })
        self.live_pr = live(headRefOid="a" * 40)

        rc, _ = self.tick()

        self.assertEqual(rc, 0)
        state = json.loads(self.state_path.read_text())
        self.assertIsInstance(state["discovery"]["last_run_at"], int)
        self.assertIsInstance(state["discovery"]["watermark_completed_at"], int)
        self.assertIsInstance(state["discovery"]["backfill_complete"], bool)
        self.assertNotIn("t_bad", state["discovery"]["seen"])
        self.assertNotIn("t_bad", state["discovery"]["pending"])
        self.assertIn("discovery.last_run_at", state["quarantine"])

    def test_equivalent_pr_number_state_keys_are_coalesced_during_migration(self):
        now = 1784116800
        canonical = PR_URL.lower()
        padded = canonical.replace("/pull/42", "/pull/00042")
        old = {
            "pr_url": padded,
            "reviewed_head": "a" * 40,
            "reviewed_at": now - 200,
            "enrolled_at": now - 300,
            "last_seen_at": now - 200,
            "last_checked_at": 0,
            "next_check_at": now + 1000,
            "source_task_id": "t_old",
        }
        new = {
            **old,
            "pr_url": PR_URL,
            "reviewed_head": "b" * 40,
            "reviewed_at": now - 100,
            "source_task_id": "t_new",
        }
        self.write_state({
            "state_schema_version": 2,
            "dispatched": {
                f"{padded}|{'c' * 40}": {"task_id": "t_lane", "dispatched_at": now - 50}
            },
            "failures": {},
            "enrolled": {padded: old, canonical: new},
            "tombstones": {},
            "discovery": {
                "last_run_at": now,
                "watermark_completed_at": now,
                "backfill_complete": True,
                "seen": {},
                "pending": {},
            },
        })
        self.tasks = []
        self.live_pr = live(headRefOid="b" * 40)

        self.assertEqual(self.tick(now=now), (0, []))

        state = json.loads(self.state_path.read_text())
        self.assertEqual(list(state["enrolled"]), [canonical])
        self.assertEqual(state["enrolled"][canonical]["source_task_id"], "t_new")
        self.assertIn(f"{canonical}|{'c' * 40}", state["dispatched"])
        self.assertNotIn(f"{padded}|{'c' * 40}", state["dispatched"])

    def test_global_completed_lane_show_budget_defers_and_continues_next_tick(self):
        now = 1784116800
        enrolled = {}
        self.tasks = []
        self.shows = {}
        for pr_number in (1, 2):
            url = f"https://github.com/Ylopo/example/pull/{pr_number}"
            enrolled[url.lower()] = {
                "pr_url": url,
                "reviewed_head": "a" * 40,
                "reviewed_at": now - 100,
                "enrolled_at": now - 100,
                "last_seen_at": now - 100,
                "last_checked_at": 0,
                "next_check_at": 0,
                "source_task_id": f"t_source{pr_number}",
            }
            for candidate_number in (1, 2):
                task_id = f"t_done_{pr_number}_{candidate_number}"
                row = {
                    "id": task_id,
                    "status": "done",
                    "assignee": "reviewer",
                    "body": f"PR: {url}",
                    "completed_at": now - candidate_number,
                }
                self.tasks.append(row)
                shown = review_show(
                    pr_url=url,
                    reviewed_head="d" * 40,
                    verdict="APPROVED",
                )
                shown["task"] = row
                self.shows[task_id] = shown
        self.write_state({
            "state_schema_version": m.STATE_SCHEMA_VERSION,
            "dispatched": {},
            "failures": {},
            "enrolled": enrolled,
            "tombstones": {},
            "discovery": {
                "last_run_at": now,
                "watermark_completed_at": now,
                "backfill_complete": True,
                "seen": {},
                "pending": {},
            },
        })
        self.args.metrics = True

        with patch.object(m, "MAX_COMPLETED_LANE_SHOWS_PER_TICK", 3):
            rc, output = self.tick(now=now)
            self.assertEqual(rc, 0)
            first_metrics = json.loads(output[-1].removeprefix("METRICS "))
            self.assertEqual(first_metrics["completed_lane_show_calls"], 3)
            self.assertEqual(first_metrics["reconciliation_deferred"], 1)
            self.assertEqual(len(self.created), 1)

            rc, output = self.tick(now=now + 1)
            self.assertEqual(rc, 0)
            second_metrics = json.loads(output[-1].removeprefix("METRICS "))
            self.assertLessEqual(second_metrics["completed_lane_show_calls"], 3)
            self.assertEqual(len(self.created), 2)

    def test_historical_discovery_is_show_bounded_and_watermarked(self):
        base = 1784116800
        self.tasks = []
        self.shows = {}
        self.live_pr = live(headRefOid="b" * 40)
        for index in range(m.MAX_DISCOVERY_SHOWS_PER_TICK + 5):
            task_id = f"t_history_{index:02d}"
            show = review_show(
                pr_url=f"https://github.com/Ylopo/example/pull/{index + 1}",
                reviewed_head="b" * 40,
                verdict="COMMENT",
            )
            show["task"]["id"] = task_id
            show["runs"][0]["completed_at"] = base + index
            self.shows[task_id] = show
            self.tasks.append({
                "id": task_id,
                "status": "done",
                "assignee": "reviewer",
                "completed_at": base + index,
            })

        self.assertEqual(self.tick(now=base + 100)[0], 0)
        self.assertEqual(self.show_calls, m.MAX_DISCOVERY_SHOWS_PER_TICK)
        self.assertEqual(self.list_calls, 1)

        self.assertEqual(self.tick(now=base + 101)[0], 0)
        self.assertEqual(self.show_calls, m.MAX_DISCOVERY_SHOWS_PER_TICK)
        self.assertEqual(self.list_calls, 1)

        second_now = base + 100 + m.DISCOVERY_INTERVAL_SECONDS
        self.assertEqual(self.tick(now=second_now)[0], 0)
        self.assertEqual(self.show_calls, m.MAX_DISCOVERY_SHOWS_PER_TICK + 5)
        self.assertEqual(self.list_calls, 2)
        state = json.loads(self.state_path.read_text())
        self.assertTrue(state["discovery"]["backfill_complete"])
        self.assertEqual(state["discovery"]["pending"], {})

        self.assertEqual(self.tick(now=second_now + 1)[0], 0)
        self.assertEqual(self.show_calls, m.MAX_DISCOVERY_SHOWS_PER_TICK + 5)
        self.assertEqual(self.list_calls, 2)

    def test_registry_github_refresh_is_bounded_per_tick(self):
        now = 1784116800
        enrolled = {}
        for index in range(m.MAX_PR_REFRESHES_PER_TICK + 2):
            url = f"https://github.com/Ylopo/example/pull/{index + 1}"
            enrolled[url.lower()] = {
                "pr_url": url,
                "reviewed_head": "b" * 40,
                "reviewed_at": now - 100,
                "enrolled_at": now - 100,
                "last_seen_at": now - 100,
                "last_checked_at": 0,
                "next_check_at": 0,
                "source_task_id": f"t_{index}",
            }
        self.write_state({
            "state_schema_version": m.STATE_SCHEMA_VERSION,
            "dispatched": {},
            "failures": {},
            "enrolled": enrolled,
            "tombstones": {},
            "discovery": {
                "last_run_at": now,
                "watermark_completed_at": now,
                "backfill_complete": True,
                "seen": {},
                "pending": {},
            },
        })
        self.tasks = []
        self.live_pr = live(headRefOid="b" * 40)

        self.assertEqual(self.tick(now=now), (0, []))
        self.assertEqual(self.github_calls, m.MAX_PR_REFRESHES_PER_TICK)
        self.assertEqual(self.list_calls, 0)

    def test_completed_manual_incident_lane_dedupes_exact_head(self):
        head = "e9518f8d0d107501be3a72aed08bdcc38190e289"
        self.args.backfill_existing = True
        self.shows["t_review42"]["runs"][0]["metadata"] = {
            "pr": HEIMDALL_PR_URL,
            "reviewed_head": "e70bb3c3da062264924418e6aeb94084a44a4e0b",
            "verdict": "NEEDS_FIX",
        }
        lane = {
            "id": "t_7141c3c7",
            "status": "done",
            "assignee": "reviewer",
            "body": f"Rereview the latest head of {HEIMDALL_PR_URL}",
        }
        self.tasks.append(lane)
        self.shows["t_7141c3c7"] = {
            "task": lane,
            "runs": [
                {
                    "id": 1697,
                    "profile": "reviewer",
                    "status": "done",
                    "outcome": "completed",
                    "ended_at": 1785233587,
                    "metadata": {
                        "head_sha": head,
                        "review_state": "CHANGES_REQUESTED",
                        "review_url": f"{HEIMDALL_PR_URL}#pullrequestreview-4796154282",
                    },
                }
            ],
        }
        self.live_pr = live(headRefOid=head)

        rc, output = self.tick(now=1785234325)

        self.assertEqual(rc, 0)
        self.assertEqual(self.created, [])
        self.assertEqual(output, [])
        state = json.loads(self.state_path.read_text())
        enrollment = state["enrolled"][HEIMDALL_PR_URL.lower()]
        self.assertEqual(enrollment["reviewed_head"], head)
        self.assertEqual(enrollment["source_task_id"], "t_7141c3c7")
        self.assertEqual(enrollment["source_run_id"], 1697)

    def test_closed_pr_is_pruned_and_tombstoned(self):
        self.args.backfill_existing = True
        self.live_pr = live(state="CLOSED")

        self.assertEqual(self.tick(), (0, []))
        state = json.loads(self.state_path.read_text())
        self.assertNotIn(PR_URL.lower(), state["enrolled"])
        self.assertEqual(state["tombstones"][PR_URL.lower()]["state"], "CLOSED")

    def test_legacy_state_migrates_without_losing_ledger(self):
        self.write_state({
            "baseline_completed_at": 1784000000,
            "dispatched": {"legacy|head": {"task_id": "t_old", "dispatched_at": 1}},
            "failures": {},
        })
        self.live_pr = live(headRefOid="a" * 40)

        self.assertEqual(self.tick(), (0, []))
        state = json.loads(self.state_path.read_text())
        self.assertEqual(state["state_schema_version"], m.STATE_SCHEMA_VERSION)
        self.assertIn("legacy|head", state["dispatched"])
        self.assertIn(PR_URL.lower(), state["enrolled"])

    def test_expired_failure_backoff_markers_are_compacted(self):
        now = 1784116800
        self.write_state({
            "dispatched": {},
            "failures": {"show|legacy": now - m.FAIL_RETRY_SECONDS},
            "enrolled": {},
            "tombstones": {},
            "discovery": {
                "last_run_at": now,
                "watermark_completed_at": now,
                "backfill_complete": True,
                "seen": {},
                "pending": {},
            },
        })
        self.tasks = []

        self.assertEqual(self.tick(now=now), (0, []))
        state = json.loads(self.state_path.read_text())
        self.assertEqual(state["failures"], {})

    def test_dry_run_does_not_mutate_persistent_state(self):
        original = {
            "dispatched": {},
            "failures": {},
            "enrolled": {},
            "tombstones": {},
            "discovery": {
                "last_run_at": 0,
                "watermark_completed_at": 0,
                "backfill_complete": False,
                "seen": {},
                "pending": {},
            },
        }
        self.write_state(original)
        before = self.state_path.read_text()
        self.args.dry_run = True

        self.assertEqual(self.tick()[0], 0)
        self.assertEqual(self.state_path.read_text(), before)

    def test_metrics_report_measured_board_show_and_github_calls(self):
        self.args.metrics = True
        self.live_pr = live(headRefOid="a" * 40)

        rc, output = self.tick()

        self.assertEqual(rc, 0)
        metrics = json.loads(output[-1].removeprefix("METRICS "))
        self.assertEqual(metrics["task_list_calls"], 1)
        self.assertEqual(metrics["task_show_calls"], 1)
        self.assertEqual(metrics["github_calls"], 1)
        self.assertEqual(metrics["registry_size"], 1)

    def test_list_prefilter_avoids_show_for_non_done_or_non_reviewer_rows(self):
        self.args.backfill_existing = True
        self.tasks = [
            {"id": "t_ready", "status": "ready", "assignee": "reviewer"},
            {"id": "t_engineer", "status": "done", "assignee": "engineer"},
        ]
        self.shows = {}

        self.assertEqual(self.tick(), (0, []))


if __name__ == "__main__":
    unittest.main()
