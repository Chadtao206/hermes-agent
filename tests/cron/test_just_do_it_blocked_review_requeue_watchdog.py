import json
import argparse
import tempfile
import unittest
import importlib.util
from pathlib import Path
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
PERMALINK = f"https://ylopo.slack.com/archives/{CHANNEL}/p1720000000000000"
PR_URL = "https://github.com/Ylopo/example/pull/42"
PR_696_URL = "https://github.com/Ylopo/npm-ylopo-models-bookshelf/pull/696"


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
    def test_motivating_production_shaped_blocked_review_is_candidate(self):
        show = review_show(
            pr_url="https://github.com/Ylopo/npm-ylopo-models-bookshelf/pull/696",
            review_verdict="NEEDS-FIX",
            github_review_state="CHANGES_REQUESTED",
            reviewed_head_oid="4455c6d245f32b52e752097c49be5f20874a9d59",
            findings=[{
                "severity": "blocking",
                "issue": "Generated declaration output is stale",
            }],
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

    def test_task_must_be_done_and_assigned_reviewer(self):
        for field, value in (("status", "ready"), ("assignee", "engineer")):
            with self.subTest(field=field):
                show = review_show(review_verdict="NEEDS-FIX", reviewed_head_oid="a" * 40)
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

        self.assertEqual(
            m.target_lane_exists([lane], PR_696_URL, head), "t_fc423563"
        )

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
            "Live author: barwin", "Head owner: barwin", "Live state: OPEN",
            "Draft: False", "Old reviewed head:", "New live head:",
            "re-fetch", "exact head", "duplicate", "Chadtao206", "approval",
            "read-only inspect", "tests", "GitHub review submission",
            "branch update", "commit", "push", "PR body", "metadata",
            "engineer", "remediation", "remaining blockers", "author",
            "pr_url", "reviewed head", "verdict/findings", "review state/id/url/commit",
            "validation/CI", "approval submitted/already existed", "reaction result",
        ):
            self.assertIn(text.lower(), third_party.lower())
        self.assertIn(candidate()["source_permalink"], third_party)
        self.assertIn("strict third-party guard", third_party.lower())
        self.assertNotIn("strict third-party guard", chad.lower())
        self.assertIn("no merge", chad.lower())

    @patch.object(m, "run", create=True)
    def test_create_rereview_uses_safe_command_and_verifies_lane(self, run):
        run.side_effect = [
            json.dumps({"id": "t_new"}),
            json.dumps({"task": {"id": "t_new", "status": "ready", "assignee": "reviewer"}, "runs": []}),
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
            json.dumps({"task": {"id": "t_new", "status": "done", "assignee": "reviewer"}, "runs": []}),
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
        self.tasks = [{"id": "t_review42", "status": "done", "assignee": "reviewer"}]
        self.shows = {
            "t_review42": review_show(
                review_verdict="NEEDS-FIX", reviewed_head_oid="a" * 40
            )
        }
        self.live_pr = live()
        self.created = []
        self.fail_create = False
        self.fail_list = False
        self.fail_show = False
        self.fail_github = False
        self.fail_readback = False
        self.reconcile_created_lane = False
        self.reconciled_lane_assignee = "reviewer"
        self.list_calls = 0

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
                    return json.dumps({"task": {
                        "id": task_id, "status": "ready", "assignee": "reviewer",
                    }, "runs": []})
                if self.fail_show:
                    raise RuntimeError("command failed: hermes")
                return json.dumps(self.shows[task_id])
            if command[0] == str(m.GHX):
                if self.fail_github:
                    raise RuntimeError("command failed: ghx.sh")
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

    def test_first_run_baselines_without_historical_dispatch(self):
        self.assertEqual(self.tick(), (0, []))
        self.assertEqual(self.created, [])
        self.assertEqual(
            json.loads(self.state_path.read_text())["baseline_completed_at"], 1784116800
        )

    def test_backfill_bypasses_baseline_and_head_changes_dispatch_new_lane(self):
        self.args.backfill_existing = True
        self.assertEqual(self.tick()[0], 0)
        self.live_pr["headRefOid"] = "c" * 40
        self.assertEqual(self.tick()[0], 0)
        self.assertEqual(len(self.created), 2)

    def test_dispatch_cap_is_two(self):
        self.args.backfill_existing = True
        self.tasks = []
        self.shows = {}
        for index in range(3):
            task_id = f"t_review{index}"
            show = review_show(review_verdict="NEEDS-FIX", reviewed_head_oid="a" * 40)
            show["task"]["id"] = task_id
            show["runs"][0]["metadata"]["pr_url"] = (
                f"https://github.com/Ylopo/example/pull/{index + 1}"
            )
            self.tasks.append({"id": task_id, "status": "done", "assignee": "reviewer"})
            self.shows[task_id] = show

        self.assertEqual(self.tick()[0], 0)
        self.assertEqual(len(self.created), 2)

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
                self.tasks = [{"id": "t_review42", "status": "done", "assignee": "reviewer"}, *extra_tasks]
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
        self.assertEqual(self.list_calls, 2)
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

    def test_merged_696_dry_run_reports_existing_reviewer_lane(self):
        self.configure_merged_696(with_lane=True)
        self.args.dry_run = True

        rc, output = self.tick()

        self.assertEqual(rc, 0)
        self.assertEqual(self.created, [])
        self.assertEqual(
            output,
            [f"WOULD_SKIP pr={PR_696_URL} head={'b' * 40} existing_lane=t_fc423563"],
        )

    def test_merged_696_without_existing_lane_is_silent(self):
        self.configure_merged_696(with_lane=False)

        self.assertEqual(self.tick(), (0, []))
        self.assertEqual(self.created, [])

    def test_existing_lane_is_recorded_in_ledger_without_create(self):
        self.configure_merged_696(with_lane=True)

        rc, output = self.tick()

        self.assertEqual((rc, output), (0, []))
        self.assertEqual(self.created, [])
        state = json.loads(self.state_path.read_text())
        key = f"{PR_696_URL.lower()}|{'b' * 40}"
        self.assertEqual(state["dispatched"][key]["task_id"], "t_fc423563")

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
