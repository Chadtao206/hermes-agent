import json
import unittest
from unittest.mock import patch

from cron.scripts import just_do_it_blocked_review_requeue_watchdog as m


CHANNEL = "C08K8FQKX9S"
PERMALINK = f"https://ylopo.slack.com/archives/{CHANNEL}/p1720000000000000"
PR_URL = "https://github.com/Ylopo/example/pull/42"


def review_show(**metadata):
    return {
        "id": "t_review42",
        "runs": [
            {
                "id": "run-42",
                "completed_at": "2026-07-14T12:00:00Z",
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
        "completed_at": "2026-07-14T12:00:00Z",
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
        "body": f"PR: {PR_URL}\nNew live head: {head}",
    }


class BlockedCandidateTest(unittest.TestCase):
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
        self.assertEqual(candidate["pr_url"], "https://github.com/Ylopo/example/pull/42")
        self.assertEqual(candidate["reviewed_head"], "a" * 40)
        self.assertEqual(candidate["source_permalink"], PERMALINK)

    def test_approved_run_is_not_candidate(self):
        show = review_show(
            review_verdict="APPROVED",
            github_review={"state": "APPROVED"},
            reviewed_head_sha="b" * 40,
        )

        self.assertIsNone(m.blocked_candidate(show, {CHANNEL}))

    def test_unstructured_or_non_slack_run_is_not_candidate(self):
        without_sha = review_show(review_verdict="NEEDS-FIX")
        other_channel = review_show(
            review_verdict="NEEDS-FIX",
            reviewed_commit="c" * 40,
            source_slack_permalink=(
                "https://ylopo.slack.com/archives/C0000000000/p1720000000000000"
            ),
        )

        self.assertIsNone(m.blocked_candidate(without_sha, {CHANNEL}))
        self.assertIsNone(m.blocked_candidate(other_channel, {CHANNEL}))

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
        self.assertEqual(
            candidate["findings"], [{"issue": "unsafe"}, "missing test"]
        )
        json.dumps(candidate)

    def test_nested_github_state_and_reviewed_head_sha_are_accepted(self):
        show = review_show(
            github_review={"state": "CHANGES_REQUESTED"},
            reviewed_head_sha="e" * 40,
            findings="requested changes in review",
        )

        candidate = m.blocked_candidate(show, {CHANNEL})

        self.assertEqual(candidate["findings"], ["requested changes in review"])

    def test_invalid_url_sha_and_permalink_are_rejected(self):
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
        self.assertIsNone(m.blocked_candidate(invalid_permalink, {CHANNEL}))

    def test_run_without_completion_timestamp_is_rejected(self):
        show = review_show(
            review_verdict="NEEDS-FIX", reviewed_head_oid="a" * 40
        )
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
        show = review_show(
            review_verdict="NEEDS-FIX", reviewed_head_oid="a" * 40
        )
        show["runs"].append(
            {
                "id": "run-new",
                "completed_at": "2026-07-15T12:00:00Z",
                "metadata": {
                    "pr_url": "https://github.com/Ylopo/example/pull/42",
                    "review_verdict": "NEEDS-FIX",
                    "reviewed_head_oid": "f" * 40,
                    "source_slack_permalink": PERMALINK,
                    "findings": [],
                },
            }
        )

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
                str(m.GHX),
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

    def test_closed_unchanged_or_draft_pr_is_not_dispatchable(self):
        self.assertFalse(m.should_dispatch(candidate(), live(state="CLOSED")))
        self.assertFalse(m.should_dispatch(candidate(), live(headRefOid="a" * 40)))
        self.assertFalse(m.should_dispatch(candidate(), live(isDraft=True)))
        self.assertFalse(
            m.should_dispatch(
                candidate(), live(headRepositoryOwner={"login": "Ylopo"})
            )
        )

    @patch.object(m, "run", create=True)
    def test_create_rereview_uses_safe_command_and_verifies_lane(self, run):
        run.side_effect = [
            json.dumps({"id": "t_new"}),
            json.dumps({"id": "t_new", "status": "ready", "assignee": "reviewer"}),
        ]

        self.assertEqual(m.create_rereview(candidate(), live()), "t_new")

        create_command = run.call_args_list[0].args[0]
        self.assertEqual(create_command[:3], ["hermes", "kanban", "create"])
        self.assertIn("--body", create_command)
        self.assertIn("STRICTLY READ-ONLY", create_command[create_command.index("--body") + 1])
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
            json.dumps({"id": "t_new", "status": "done", "assignee": "reviewer"}),
        ]

        with self.assertRaisesRegex(RuntimeError, "created re-review lane"):
            m.create_rereview(candidate(), live())


if __name__ == "__main__":
    unittest.main()
