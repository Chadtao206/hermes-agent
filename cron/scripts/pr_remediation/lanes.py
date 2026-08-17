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

The brief also carries an inventory of unresolved Codex/CodeQL review
threads (see `_bot_threads_section`), but deliberately NOT their comment
bodies. Boris's findings are inlined in full because they live only in
`boris_intake.db` -- nowhere else for the lane to go get them. Codex/CodeQL
threads live on the PR itself, and the lane runs in a worktree with `gh`
already in it, so the brief hands over a count, as many of the distinct
file paths involved as `MAX_BOT_THREADS_SECTION_CHARS` allows, and a fetch
instruction, and lets the agent read the threads itself. Inlining thread
bodies too would also fight the cap `MAX_BRIEF_CHARS` already sits close to
for an ordinary multi-issue verdict.
"""
from __future__ import annotations

import os
import re
import subprocess
from pathlib import Path

from pr_remediation.boris_source import EvidenceAnchor, Issue, Verdict

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


# The brief is passed as a literal `--body <text>` argv token (see the module
# docstring above) and stored verbatim as the kanban task's `body`, so its
# size is bounded by more than good taste. `Issue`'s own fields can each run
# to 2,000 characters and an issue can carry up to 50 anchors
# (boris_intake/review_tools_mcp.py's own schema caps), so an unbounded render
# of `verdict.blocking_issues` could in principle reach the megabytes.
#
# The limit that binds is KANBAN'S, not the OS's and not our taste. The engineer
# agent never reads this text directly -- it reads what
# `hermes_cli/kanban_db.py`'s `build_worker_context` renders, and that applies
# `_cap(task.body, _CTX_MAX_BODY_BYTES)` with `_CTX_MAX_BODY_BYTES = 8 * 1024`.
# `_cap` truncates from the HEAD:
#
#     return s[:limit] + f"… [truncated, {len(s) - limit} chars omitted]"
#
# which eats the END of the brief -- exactly where the acceptance criteria, the
# three prohibitions and the push target live. The first draft's 20,000 was 2.4x
# that, so kanban's silent head-truncation, not ours, was the one that would
# fire: the live three-issue brief for Ylopo/million-stars#199 rendered at 6,811
# characters, 83% of the way to losing its own stopping condition, and a
# four-issue verdict crosses it. The OS argv limit (~1 MB) never enters into it.
#
# Set equal to kanban's cap so OUR truncation always goes first: `_bounded`
# protects the header and footer by construction and says out loud what it
# dropped, which is precisely what `_cap` cannot do.
#
# Deliberately a literal rather than an import of `_CTX_MAX_BODY_BYTES`: this
# module also runs from its deployed copy under `~/.hermes/scripts/`, where
# `hermes_cli` is not importable. tests/cron/pr_remediation/test_lanes.py
# imports kanban's real constant and pins `MAX_BRIEF_CHARS <=` it, so neither
# side can drift away from the other unnoticed.
MAX_BRIEF_CHARS = 8 * 1024

# The character budget for the WHOLE rendered "## Codex / CodeQL threads"
# section -- heading, count sentence, path list and fetch instruction
# together (see `_bot_threads_section`).
#
# Boris's blocking issues are this brief's payload; the bot-thread inventory
# is supporting context, and supporting context must never evict the payload.
# Before this budget existed it could, in two stages. `_bounded` computes the
# issue budget as `MAX_BRIEF_CHARS - len(header) - len(footer)` and never
# trims the footer -- and this section is part of that footer -- so one line
# per distinct path with no cap first ate the issue budget (issues silently
# dropped to make room for a path list), and then, once the footer alone
# outgrew MAX_BRIEF_CHARS, made `_bounded` hand back a brief LONGER than the
# cap. At which point kanban's `_cap` head-truncation -- the thing this
# module's entire size discipline exists to get in front of -- took the
# acceptance criteria and the push target off the end.
#
# The unit is CHARACTERS, not a path count, because the cost of one path is
# its length -- and that length varies by nearly a factor of three across the
# repos this loop serves. Measured with `git ls-files`: thousand-stars runs 61
# characters at the median, 140 at the 90th percentile, 246 at its longest;
# million-stars runs 52 / 86 / 119. A cap of "15 paths" would therefore be 780
# characters of brief in one and 2,100 in the other. Thread COUNT is not the
# lever at all -- the render dedups paths, so 2,000 threads across eight files
# are eight lines.
#
# Sized against the live three-issue verdict this module's cap was set from,
# br_732853620fd14dfb8f6c5db13d8fb1d2 (Ylopo/million-stars#199): its three
# issues render as 6,252 characters of issue blocks, leaving 1,367 characters
# for this section before the brief loses either an issue or part of its
# footer -- 1,248 when neither branch name could be read and the header
# carries the longer placeholders instead. 1,024 sits under the tighter of
# those with 224 characters to spare, and is one eighth of MAX_BRIEF_CHARS,
# so seven eighths of every brief stay reserved for what Boris actually
# found. It buys a preview, not a full listing, and is meant to: about nine
# median-length Ylopo paths or four at the 90th percentile, over a heading
# carrying the true total and an omission line carrying the rest.
#
# tests/cron/pr_remediation/test_lanes.py pins both halves of that: that a
# hostile inventory cannot cost a live-sized verdict an issue, its acceptance
# criteria or its push target, and that the section itself never exceeds this
# number.
MAX_BOT_THREADS_SECTION_CHARS = 1024


def _omission_notice(omitted: int) -> str:
    """The notice that stands in for the issues that did not fit.

    Says how MANY are missing, because the footer's acceptance criteria count
    every blocking issue in the verdict. A brief that silently rendered issues
    1-4 of 20 under a footer reading "All 20 listed blocking issue(s) are
    cleared" handed the agent a stopping condition it could not even
    enumerate, with nothing anywhere to say that anything was absent. This
    count and the footer's count are the two halves of one sentence.
    """
    return (
        f"\n[BRIEF TRUNCATED: {omitted} further blocking issue(s) are NOT "
        f"shown above -- the rendered issues reached this brief's "
        f"{MAX_BRIEF_CHARS}-character cap, which is kanban's own limit on a "
        "task body. Every issue shown above is shown in full; the omitted "
        "ones are missing entirely, not shortened. The acceptance criteria "
        "below still count all of them, so read the omitted issues off the "
        "PR's Boris review thread before treating the review as cleared.]\n"
    )


def _anchor_line(anchor: EvidenceAnchor) -> str:
    """One evidence anchor. `diff` anchors are surfaced as the file paths
    they are -- real repo-relative paths -- which is what lets the agent go
    straight to the code instead of rediscovering where a finding lives."""
    label = "file" if anchor.kind == "diff" else anchor.kind
    line = f"     - {label}: {anchor.reference}"
    return line + (f" ({anchor.detail})" if anchor.detail else "")


def _issue_block(number: int, issue: Issue) -> str:
    lines = [f"{number}. {issue.title or '(untitled)'}"]
    if issue.expected_behavior:
        lines.append(f"   - Expected behavior: {issue.expected_behavior}")
    if issue.changed_mechanism:
        lines.append(f"   - Changed mechanism: {issue.changed_mechanism}")
    if issue.trigger:
        lines.append(f"   - Trigger: {issue.trigger}")
    if issue.consequence:
        lines.append(f"   - Consequence: {issue.consequence}")
    if issue.impact_scope:
        lines.append(f"   - Impact scope: {issue.impact_scope}")
    if issue.action:
        lines.append(f"   - Suggested action: {issue.action}")
    if issue.evidence_anchors:
        lines.append("   - Evidence:")
        lines.extend(_anchor_line(a) for a in issue.evidence_anchors)
    return "\n".join(lines)


def _issue_blocks(issues: tuple[Issue, ...]) -> list[str]:
    """One rendered block per issue, kept separate rather than pre-joined so
    `_bounded` can drop them a whole issue at a time."""
    return [_issue_block(n, issue) for n, issue in enumerate(issues, 1)]


def _bounded(header: str, blocks: list[str], footer: str) -> str:
    """Assemble the brief, dropping WHOLE issues when they do not all fit.

    Never truncates `header` or `footer` -- that is where the branch names,
    the acceptance criteria, the three prohibitions and the push target live,
    and they are the first thing kanban's own head-truncation would take (see
    MAX_BRIEF_CHARS).

    Within the issue detail, the unit of truncation is one issue, not one
    character. Slicing the joined text mid-render produced lines like
    `- file: src/path_number_118.p` -- a broken repo path that reads exactly
    like a complete one -- and dropped issues 5..20 of a 20-issue verdict
    without the notice ever admitting a whole finding was gone. An issue is
    either rendered in full or counted as omitted; there is no third,
    half-true state.

    Enough budget is reserved as we go for the omission notice we would owe
    if the next issue were the one that did not fit, so keeping an issue can
    never push that notice out of the cap.
    """
    budget = max(MAX_BRIEF_CHARS - len(header) - len(footer), 0)
    if not blocks:
        return header + "(none listed)\n" + footer
    body = ""
    for index, block in enumerate(blocks):
        candidate = body + block + "\n"
        remaining_after_this = len(blocks) - index - 1
        reserve = (len(_omission_notice(remaining_after_this))
                   if remaining_after_this else 0)
        if len(candidate) + reserve <= budget:
            body = candidate
            continue
        return header + body + _omission_notice(len(blocks) - index) + footer
    return header + body + footer


def _paths_omission_line(omitted: int) -> str:
    """The line that stands in for the paths that did not fit the section's
    budget. Shaped like `_omission_notice`, and for the same reason: an
    inventory that quietly lists some of the files and none of the fact that
    it did so is worse than a short one that admits it.

    It says the paths are missing, never the threads: the count in the
    heading above stays the true total, so this line is what stops the
    heading and the list under it from contradicting each other.
    """
    return (f"[... {omitted} further file(s) not listed -- the path list hit "
            f"this section's {MAX_BOT_THREADS_SECTION_CHARS}-character budget. "
            "The thread count above is still the true total; get the rest off "
            "the PR with the command below.]\n")


def _bot_threads_section(bot_threads: list[dict] | None) -> str:
    """Inventory only -- see the module docstring for why bodies are not inlined.

    None is the "could not be determined" answer `unresolved_bot_threads`
    returns for a failed or truncated read. It must render as its own
    sentence: collapsing it into "none found" is exactly the false
    all-clear that method exists to prevent.

    Bounded by `MAX_BOT_THREADS_SECTION_CHARS`, whole paths at a time. Both
    halves of that matter. Bounded, because `build_brief` folds this section
    into the footer `_bounded` refuses to trim, so an unbounded section
    crowds out Boris's issues and then the brief's own stopping condition.
    Whole paths, because this file has already shipped the other kind of cut
    once: slicing a rendered brief mid-token left the line
    `- file: src/path_number_118.p`, a broken repo path that reads exactly
    like a complete one and that the agent would go looking for. A path is
    either listed in full or counted in `_paths_omission_line`; there is no
    half-true third state. Enough budget is held back as we go for the
    omission line we would owe if the next path were the one that did not
    fit, so listing a path can never push that line out of the budget.

    `outdated` is surfaced as a count in the heading, matching the precedent
    `slack_io.py`'s `_thread_notice` sets and for the reason its docstring
    argues: an outdated thread is not a resolved one, so it is counted, and
    the split is shown because it is real triage signal. Deliberately NOT a
    per-path marker -- that would grow the list, which is the thing this
    section is being kept small about.
    """
    if bot_threads is None:
        return ("\n## Codex / CodeQL threads\n\n"
                "The unresolved-thread count could not be determined. Check the PR\n"
                "yourself before you finish, and resolve anything still open.\n")
    if not bot_threads:
        return ("\n## Codex / CodeQL threads\n\n"
                "No unresolved Codex/CodeQL threads at the time this lane opened.\n"
                "Re-check before you finish -- bots can post after a push.\n")
    paths = sorted({t["path"] for t in bot_threads if t.get("path")})
    outdated = sum(1 for t in bot_threads if t.get("outdated"))
    heading = ("\n## Codex / CodeQL threads\n\n"
               f"{len(bot_threads)} unresolved Codex/CodeQL review thread(s)"
               f"{f' ({outdated} outdated)' if outdated else ''}, "
               "across these files:\n\n")
    fetch = ("\nRead them with "
             "`gh api graphql` (reviewThreads on this PR) or `gh pr view --comments`, "
             "fix what they raise, and resolve each thread you addressed.\n")
    budget = max(MAX_BOT_THREADS_SECTION_CHARS - len(heading) - len(fetch), 0)
    listed = ""
    for index, path in enumerate(paths):
        candidate = listed + f"- {path}\n"
        remaining_after_this = len(paths) - index - 1
        reserve = (len(_paths_omission_line(remaining_after_this))
                   if remaining_after_this else 0)
        if len(candidate) + reserve <= budget:
            listed = candidate
            continue
        listed += _paths_omission_line(len(paths) - index)
        break
    return heading + listed + fetch


def build_brief(verdict: Verdict, round_number: int, *,
                base_branch: str | None = None,
                head_branch: str | None = None,
                bot_threads: list[dict] | None = None) -> str:
    """The remediation brief, dispatched as the lane's `--body`.

    Renders the full issue objects Boris produced -- title, expected
    behavior, changed mechanism, trigger, consequence, impact scope,
    suggested action, and evidence anchors, with `diff` anchors surfaced as
    the file paths they are -- rather than the bare titles the old brief
    carried, which left the engineer agent to rediscover where each finding
    lives before it could fix anything. `Verdict.blocking_issues` (see
    boris_source.py) is what makes this possible: the whole issue survives
    from the reviewing service through to here now, not only its title.

    Adds two things the old brief never had. An "Acceptance criteria"
    section, shaped like the comparable fast-lane briefs this service is
    benchmarked against (see ylopo_pr_readiness's generated fix briefs),
    gives the agent an actual stopping condition -- the old brief said what
    to fix and what not to do, but never what "done" means. And the base and
    head branch name, read once via GitHubIO.branches() and threaded in from
    `_open_lane`, remove a real ambiguity: the old brief said "push" without
    saying where, and the worktree a lane runs in is checked out on a
    kanban-generated branch that is not the PR's -- pushing that branch
    instead would look like progress and fix nothing.

    `base_branch`/`head_branch` default to None (unreadable, or not asked
    for) and degrade to a visible placeholder rather than a guess: never
    silently omitted, and never a name invented for a branch nobody
    confirmed.

    `bot_threads` is what `GitHubIO.unresolved_bot_threads()` found on the
    PR (or None when that could not be determined), read on the same pass
    as branches() and threaded in from `_open_lane`. This is coverage
    pr-review-hourly (the cron this loop retires) still tracked and this
    loop, until now, did not. It renders as an inventory -- a count and the
    distinct file paths involved, via `_bot_threads_section` -- rather than
    inlined finding text: unlike Boris's issues, a bot thread carries no
    comment body in what this method's data shape hands back
    (`{"path": str, "outdated": bool}`), and the lane has `gh` in its
    worktree to fetch the rest itself.

    That section is folded into the footer, not the truncatable issue body,
    so `_bounded` protects it the same way it protects the acceptance
    criteria and the push target. Protected from OUR truncation is the whole
    of that claim, and it cuts both ways: an unbounded section in the footer
    is precisely how the acceptance criteria and push target stop surviving
    KANBAN's truncation, because `_bounded` subtracts the footer from the
    issue budget without ever trimming it, and a footer bigger than
    MAX_BRIEF_CHARS makes `_bounded` return a brief over the cap for kanban's
    `_cap` to head-truncate. `MAX_BOT_THREADS_SECTION_CHARS` is what keeps
    that from happening; see its comment.
    """
    unknown_head = ("(unknown -- confirm with `gh pr view`; this is NOT the "
                    "worktree's own branch)")
    header = (
        f"Remediate {verdict.slug} at exact head {verdict.head_sha}.\n"
        f"Base branch: {base_branch or '(unknown -- confirm with `gh pr view`)'}\n"
        f"Head branch: {head_branch or unknown_head}\n"
        f"This is remediation round {round_number}, driven by the Boris review loop.\n\n"
        f"Boris raised these blocking issues:\n"
    )
    push_target = (f"the PR's head branch, `{head_branch}`" if head_branch else
                   "the PR's head branch named above (NOT the worktree's own branch)")
    # A non-empty inventory or an undetermined read (None) both leave
    # something the agent still owes: an unresolved thread to address, or a
    # check it still has to make itself. Only a verified-clean read owes
    # nothing, so only that case skips the bullet.
    #
    # `is not None and not bot_threads`, not `== []`: this predicate and
    # `_bot_threads_section`'s `not bot_threads` decide the same question
    # about the same value and have to agree on the answer. Under `== []` an
    # empty TUPLE rendered "No unresolved Codex/CodeQL threads at the time
    # this lane opened" and then, three lines later, an acceptance criterion
    # telling the agent to resolve the threads that sentence had just said
    # did not exist. `GitHubIO.unresolved_bot_threads` only ever returns
    # `list | None`, so nothing in production could reach it -- but
    # test_loop.py's fake defaulted to `()`, so every "clean PR" loop test
    # ran on a value production never produces, which is exactly how a
    # contradiction like this goes unnoticed.
    bot_threads_bullet = (
        "" if bot_threads is not None and not bot_threads else
        "- Resolve every unresolved Codex/CodeQL thread you addressed.\n"
    )
    footer = (
        _bot_threads_section(bot_threads) +
        "\n## Acceptance criteria\n\n"
        f"- All {len(verdict.blocking_issues)} listed blocking issue(s) are "
        "cleared at a pushed head, with tests covering them.\n"
        + bot_threads_bullet +
        "- Do not open a new PR.\n"
        "- Do not force-push.\n"
        "- Do not change the PR's base branch.\n\n"
        f"Fix them at this head and push to {push_target}.\n"
    )
    return _bounded(header, _issue_blocks(verdict.blocking_issues), footer)


def idempotency_key(verdict: Verdict, round_number: int) -> str:
    return f"borisloop:{verdict.slug}:{verdict.head_sha[:7]}:r{round_number}"


class LaneIO:
    def __init__(self, kanban_db: Path, runner=subprocess.run):
        self._kanban_db = Path(kanban_db)
        self._runner = runner

    def create(self, verdict: Verdict, round_number: int, *,
               base_branch: str | None = None,
               head_branch: str | None = None,
               bot_threads: list[dict] | None = None) -> str | None:
        # base_branch/head_branch: read by the caller (loop.py's _open_lane,
        # via GitHubIO.branches()) and threaded straight into the brief.
        # Optional and default None so a caller that cannot read them --  or
        # a test that does not care -- still dispatches a valid, if less
        # specific, brief; see build_brief for how a missing value degrades.
        # bot_threads: read the same way, off the same _open_lane pass, via
        # GitHubIO.unresolved_bot_threads() -- see build_brief for how it
        # renders and why None (undetermined) is never treated as "clean".
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
            "--body", build_brief(verdict, round_number,
                                  base_branch=base_branch, head_branch=head_branch,
                                  bot_threads=bot_threads),
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
