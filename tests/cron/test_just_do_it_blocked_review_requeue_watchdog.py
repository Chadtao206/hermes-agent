import json
import unittest

from cron.scripts import just_do_it_blocked_review_requeue_watchdog as m


CHANNEL = "C08K8FQKX9S"
PERMALINK = f"https://ylopo.slack.com/archives/{CHANNEL}/p1720000000000000"


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


if __name__ == "__main__":
    unittest.main()
