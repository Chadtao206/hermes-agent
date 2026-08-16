"""Andrej remediation lanes, dispatched through the existing kanban machinery.

The argv here mirrors the call `scripts/ylopo_pr_readiness/gather.py:304` already
emits, because that is the shape `kanban_dispatch.sh` actually accepts: a `create`
verb with a positional title, `--assignee` (not `--profile`), and a worktree
anchor (`--workspace worktree:<path>`).

The body is passed as a literal `--body <text>` argument -- the same way
`boris_intake/dispatch.py`'s `create_lane()` already does it -- and NOT as
`--body @<file>`. gather.py's rendered report text uses `@<fix-task-body-file>`
as a template for whichever agent reads the report to fill in and execute by
hand; it is never itself passed to a shell. Neither `kanban_dispatch.sh` nor
`hermes_cli/kanban.py`'s `_cmd_create` (which stores `body=args.body` verbatim
into the task) resolves an `@`-prefixed value, so shelling out with a literal
`--body @/tmp/xyz.md` argv token -- as this file's first draft did -- would
write the literal string "@/tmp/xyz.md" into the lane's body instead of the
remediation brief, and no fake-runner test would catch it.

Note that `kanban_dispatch.sh` independently refuses `create` unless
`PR_REVIEW_AUTOFIX=1` in the live `.env`. That gate is outside this service's
control, which is why selftest checks it before the daemon claims to be live.
"""
from __future__ import annotations

import os
import re
import subprocess
from pathlib import Path

from pr_remediation.boris_source import Verdict

DISPATCH = "/Users/ctao/.hermes/scripts/pr/kanban_dispatch.sh"
ANCHOR_ROOT = os.environ.get(
    "PR_FIX_ANCHOR_ROOT", "/Users/ctao/.hermes/state/pr-review-anchors")
EXPECTED_OWNER = os.environ.get("PR_REVIEW_EXPECTED_OWNER") or "Ylopo"
ASSIGNEE = "engineer"  # SOUL.md: Andrej = engineer
TIMEOUT = 120
# What kanban means by "this lane is finished", taken from kanban's own
# vocabulary rather than guessed at. `hermes_cli/kanban_db.py`'s VALID_STATUSES
# is the whole vocabulary -- kanban *rejects* anything outside it -- and this is
# the terminal subset of it, identical to the set the sibling consumer
# `boris_intake/dispatch.py` (TERMINAL_STATUSES) already polls lanes with.
#
# The first draft here invented `failed` and `cancelled`, which kanban has never
# been able to store (0 rows on the live board), and omitted `archived`, which it
# stores by the hundred. The cost was not cosmetic: a human archiving a stuck
# card -- ordinary board hygiene -- left the lane forever unfinished, so the loop
# sat in `remediating` holding the ownership label and `active_for_pr` blocked
# every future verdict for that pull request. `blocked` belongs here for the
# same reason it does in the sibling: kanban parks a lane a human must look at
# there, and nothing this loop does will move it on.
#
# tests/cron/pr_remediation/test_lanes.py pins this against the real vocabulary
# and against the sibling, so neither can drift away from us unnoticed.
TERMINAL_LANE_STATUSES = frozenset({"done", "archived", "blocked"})
_TASK_ID = re.compile(r"\bt_[0-9a-z]+")
_SAFE_REPO_NAME = re.compile(r"[A-Za-z0-9._-]+")
_DEFAULT_EXCLUDED_REPOS = "Ylopo/ylopo-infra Ylopo/ylopo-github-actions-workflows"
_EXCLUDED_REPO_SUFFIXES = ("-infra", "-infrastructure")


def _excluded_repos() -> frozenset[str]:
    """Space-separated `owner/name` entries. Read live off the environment
    (not cached at import) so a live override -- or a test's monkeypatch.setenv
    -- takes effect without a module reload, matching how ANCHOR_ROOT below is
    read live off the module global."""
    raw = os.environ.get("PR_REVIEW_EXCLUDED_REPOS") or _DEFAULT_EXCLUDED_REPOS
    return frozenset(raw.split())


def _repo_is_excluded(verdict: Verdict) -> bool:
    full = f"{verdict.owner}/{verdict.repo}"
    return full in _excluded_repos() or full.endswith(_EXCLUDED_REPO_SUFFIXES)


def build_brief(verdict: Verdict, round_number: int) -> str:
    issues = "\n".join(f"- {title}" for title in verdict.blocking_issues) or "- (none listed)"
    return (
        f"Remediate {verdict.slug} at exact head {verdict.head_sha}.\n"
        f"This is remediation round {round_number}, driven by the Boris review loop.\n\n"
        f"Boris raised these blocking issues:\n{issues}\n\n"
        f"Fix them at this head and push. Do not open a new PR, do not force-push, "
        f"and do not change the PR's base branch.\n"
    )


def idempotency_key(verdict: Verdict, round_number: int) -> str:
    return f"borisloop:{verdict.slug}:{verdict.head_sha[:7]}:r{round_number}"


class LaneIO:
    def __init__(self, kanban_db: Path, runner=subprocess.run):
        self._kanban_db = Path(kanban_db)
        self._runner = runner

    def create(self, verdict: Verdict, round_number: int) -> str | None:
        # --- repo-name path safety ---------------------------------------
        # verdict.repo is the only field used to build a filesystem path
        # below (ANCHOR_ROOT/<repo>), so validate it before any path is
        # touched. Mirrors kanban_dispatch.sh's owner + character-class
        # checks (that script's `prfix:*:fix` block, ~lines 181-187): the PR
        # must be under the expected owner, and the repo name must contain
        # nothing outside A-Za-z0-9._- (rules out embedded path separators
        # and URL-shaped values). A bare "." or ".." is composed entirely of
        # otherwise-allowed characters, so the character class alone would
        # not catch a literal traversal segment -- reject those explicitly.
        if verdict.owner != EXPECTED_OWNER:
            return None
        if verdict.repo in (".", "..") or not _SAFE_REPO_NAME.fullmatch(verdict.repo):
            return None

        # --- infra-repo ban -----------------------------------------------
        # Infra repos are OFF-LIMITS for automated fixes: devops (jbankes)
        # rejected pr-bot backend/CI workarounds on ylopo-infra three times
        # (#252/#255/#249). The SAME policy also lives in
        # kanban_dispatch.sh's `prfix:*:fix` gate (~lines 188-206) and in
        # pr_review_gather.py's `repo_excluded` -- keep all three in sync if
        # you change this policy. infra PRs stay minimal + human-driven.
        if _repo_is_excluded(verdict):
            return None

        # kanban_dispatch.sh's clone-on-miss is gated on `prfix:*:fix`
        # idempotency keys (see the `case` around line 168 of that script);
        # this loop deliberately uses `borisloop:` keys instead, because
        # `prfix:*:fix` keys are not round-aware and a second remediation
        # round would dedupe into round 1's task and silently never run.
        # That means the clone-on-miss path never fires for us, so a
        # missing anchor would otherwise dispatch a lane whose worker fails
        # later, far from the cause, when it can't materialise a worktree.
        # Refuse up front instead. We deliberately do not create/clone the
        # anchor ourselves -- that path is security-hardened inside
        # kanban_dispatch.sh (confines writes to a canonical root, refuses
        # traversal, only removes a clone it created) and duplicating it
        # here would fork a security-sensitive control.
        if not (Path(ANCHOR_ROOT) / verdict.repo / ".git").exists():
            return None
        title = f"fix(boris-loop): {verdict.slug} round {round_number}"
        argv = [
            DISPATCH, "create", title,
            "--assignee", ASSIGNEE,
            "--workspace", f"worktree:{ANCHOR_ROOT}/{verdict.repo}",
            "--idempotency-key", idempotency_key(verdict, round_number),
            "--created-by", "boris-remediation-loop",
            "--max-retries", "1",
            "--body", build_brief(verdict, round_number),
        ]
        try:
            proc = self._runner(
                argv, capture_output=True, text=True, timeout=TIMEOUT,
                env={"HERMES_KANBAN_DB": str(self._kanban_db),
                     "HOME": "/Users/ctao", "HERMES_HOME": "/Users/ctao/.hermes",
                     "PATH": "/Users/ctao/.hermes/hermes-agent/venv/bin:"
                             "/usr/local/bin:/opt/homebrew/bin:/usr/bin:/bin"},
            )
        except Exception:
            return None
        if proc.returncode != 0:
            return None
        match = _TASK_ID.search(proc.stdout or "")
        return match.group(0) if match else None

    def status(self, lane_id: str) -> str | None:
        from boris_intake.dispatch import KanbanIO

        return KanbanIO(self._kanban_db).lane_status(lane_id)
