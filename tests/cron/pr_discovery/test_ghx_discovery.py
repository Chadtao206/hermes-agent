"""Shell-level tests for `ghx_discovery.sh`, the command-allowlisted `gh`
the discovery cron's three GitHub reads go through.

These invoke the script itself -- the one exception to this package's "no
subprocess, no `gh` binary" rule, because a wrapper's refusals cannot be
tested through a Python double: the refusals ARE the shell. `gh` is still
never run: every allowed call is answered by `GHX_DISCOVERY_DRY_RUN=1`, and
the two token tests (which deliberately run past the dry-run gate) use a
`PATH` with no `gh` on it.

What is under test is narrow, and deliberately so. `pr_discovery` is a
`no_agent: true` cron with fixed argv, so this wrapper is not defending
against a model that might be induced to run something -- it is defending
against a future edit to `discover.py` that adds a write call. Each test
below names one refusal clause of the script; delete that clause and this
test is what fails.
"""
import os
import subprocess
from pathlib import Path

import pytest

FIXTURE = Path(__file__).with_name("fixtures") / "ghx_discovery.sh"
TOKEN_FIXTURE = FIXTURE.with_name("gh_token")
DEPLOYED = Path("/Users/ctao/.hermes/scripts/pr/ghx_discovery.sh")

# `gh pr view`/`pr checks`/`search prs` exactly as `discover.py` spells them.
SEARCH = ["search", "prs", "--state=open", "--limit=200", "--json",
          "number,repository,author,isDraft,state,labels", "--owner=Ylopo",
          "--author=Chadtao206"]
VIEW = ["pr", "view", "7", "-R", "Ylopo/million-stars", "--json",
        "headRefOid,mergeStateStatus"]
CHECKS = ["pr", "checks", "7", "-R", "Ylopo/million-stars", "--json",
          "name,state"]


def run(args, *, scope="Ylopo", token=str(TOKEN_FIXTURE), dry_run=True):
    env = {"PATH": "/usr/bin:/bin", "DISCOVERY_GH_TOKEN_PATH": token}
    if scope is not None:
        env["DISCOVERY_ORG_SCOPE"] = scope
    if dry_run:
        env["GHX_DISCOVERY_DRY_RUN"] = "1"
    return subprocess.run([str(FIXTURE), *args], capture_output=True,
                          text=True, env=env, check=False)


# ---------------------------------------------------------------------------
# The org scope, and its absence
# ---------------------------------------------------------------------------

def test_refuses_every_read_when_the_org_scope_is_unset():
    """Unset must refuse, not default: a wrapper that falls back to "any org"
    when nobody sets the variable is a wrapper that enumerates all of GitHub
    the first time a deployment forgets an env line."""
    for args in (SEARCH, VIEW, CHECKS):
        result = run(args, scope=None)
        assert result.returncode == 3, args
        assert "REFUSED DISCOVERY_ORG_SCOPE is unset" in result.stderr, args


def test_refuses_an_empty_org_scope():
    result = run(SEARCH, scope="")
    assert result.returncode == 3
    assert "REFUSED DISCOVERY_ORG_SCOPE is unset" in result.stderr


def test_refuses_a_search_that_names_another_org():
    result = run(["search", "prs", "--owner=SomeOtherOrg"])
    assert result.returncode == 3
    assert ("REFUSED search owner 'SomeOtherOrg' is outside the scoped org "
            "'Ylopo'") in result.stderr


def test_refuses_a_second_owner_smuggled_in_beside_the_scoped_one():
    """Position is not trusted: the scoped owner appearing once does not buy
    a pass for the rest of argv."""
    result = run(["search", "prs", "--owner=Ylopo", "--owner", "SomeOtherOrg"])
    assert result.returncode == 3
    assert "'SomeOtherOrg' is outside the scoped org" in result.stderr


def test_refuses_a_search_with_no_owner_qualifier():
    """`gh search prs --state=open` with no owner searches all of GitHub."""
    result = run(["search", "prs", "--state=open", "--json", "number"])
    assert result.returncode == 3
    assert "REFUSED search without --owner=Ylopo" in result.stderr


def test_refuses_a_pr_read_outside_the_scoped_org():
    for args in (["pr", "view", "7", "-R", "SomeOtherOrg/repo"],
                 ["pr", "checks", "7", "--repo", "SomeOtherOrg/repo"],
                 ["pr", "view", "7", "--repo=SomeOtherOrg/repo"],
                 ["pr", "view", "7", "-R", "Ylopo"]):
        result = run(args)
        assert result.returncode == 3, args
        assert "is outside the scoped org 'Ylopo'" in result.stderr, args


def test_refuses_a_pr_read_that_names_no_repository():
    """Without `-R`, `gh pr view` resolves the repo from the working
    directory's git remote -- a scope this script cannot see and therefore
    cannot enforce."""
    result = run(["pr", "view", "7"])
    assert result.returncode == 3
    assert "REFUSED pr view without -R Ylopo/<repo>" in result.stderr


def test_refuses_a_url_argument_that_would_override_the_repo_flag():
    """`gh pr view <url>` ignores `-R` entirely, so an in-scope `-R` next to
    an out-of-scope URL reads the URL's repository."""
    result = run(["pr", "view", "https://github.com/SomeOtherOrg/repo/pull/5",
                  "-R", "Ylopo/million-stars"])
    assert result.returncode == 3
    assert "would override -R" in result.stderr


# ---------------------------------------------------------------------------
# The verb allowlist
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("args", [
    ["pr", "merge", "7", "-R", "Ylopo/million-stars"],
    ["pr", "review", "7", "-R", "Ylopo/million-stars", "--body", "x"],
    ["pr", "comment", "7", "-R", "Ylopo/million-stars", "--body", "x"],
    ["pr", "edit", "7", "-R", "Ylopo/million-stars", "--add-label", "x"],
    ["pr", "close", "7", "-R", "Ylopo/million-stars"],
    ["pr", "reopen", "7", "-R", "Ylopo/million-stars"],
    ["pr", "ready", "7", "-R", "Ylopo/million-stars"],
    ["pr", "create", "-R", "Ylopo/million-stars"],
    ["pr", "update-branch", "7", "-R", "Ylopo/million-stars"],
    ["pr", "diff", "7", "-R", "Ylopo/million-stars"],
    ["pr", "list", "-R", "Ylopo/million-stars"],
])
def test_refuses_every_pr_subcommand_but_view_and_checks(args):
    """Deny by default inside `pr` too: `diff` and `list` are reads and are
    still refused, because discovery does not make them and an allowlist that
    grows to cover what a caller *might* want is not an allowlist."""
    result = run(args)
    assert result.returncode == 3
    assert f"REFUSED pr {args[1]}" in result.stderr


@pytest.mark.parametrize("args", [
    ["api", "graphql"],
    ["api", "/repos/Ylopo/million-stars/pulls/7"],
    ["workflow", "run", "deploy.yml"],
    ["workflow", "list"],
    ["gist", "create", "secrets.txt"],
    ["repo", "clone", "Ylopo/million-stars"],
    ["auth", "token"],
    ["release", "create", "v1"],
    ["secret", "set", "TOKEN"],
    ["issue", "create", "-R", "Ylopo/million-stars"],
    [],
])
def test_refuses_every_verb_outside_the_allowlist(args):
    """`api graphql` is the one to look at twice: it is how a mutation
    reaches GitHub without a write-looking verb anywhere in argv. The
    machine credential also carries the `workflow` and `gist` scopes, so
    both of those would really work if they were let through."""
    result = run(args)
    assert result.returncode == 3
    assert f"REFUSED verb '{args[0] if args else ''}'" in result.stderr


@pytest.mark.parametrize("sub", ["repos", "code", "issues", "commits"])
def test_refuses_search_subcommands_other_than_prs(sub):
    result = run(["search", sub, "--owner=Ylopo"])
    assert result.returncode == 3
    assert f"REFUSED search {sub}" in result.stderr


# ---------------------------------------------------------------------------
# Flags, wherever they appear in argv
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("flag", [
    "--approve", "--request-changes", "--admin", "--merge", "--squash",
    "--rebase", "--delete-branch",
])
def test_refuses_a_write_flag_appended_to_an_allowed_read(flag):
    result = run([*VIEW, flag])
    assert result.returncode == 3
    assert f"REFUSED flag {flag}" in result.stderr


def test_refuses_a_hostname_flag_that_would_leave_this_github():
    """Not a write -- a scope escape. `--hostname` retargets an
    identically-named org on another GitHub host, which the org check above
    would happily approve."""
    result = run([*VIEW, "--hostname", "evil.test"])
    assert result.returncode == 3
    assert "REFUSED flag --hostname" in result.stderr


@pytest.mark.parametrize("flag", [
    "-X", "-XPOST", "--method=POST", "-f", "--field", "--field=a=b", "-F",
    "--raw-field", "--raw-field=a=b", "--input", "--input=body.json",
])
def test_refuses_request_writing_flags_appended_to_an_allowed_read(flag):
    result = run([*VIEW, flag])
    assert result.returncode == 3
    assert "REFUSED request-writing flag" in result.stderr


# ---------------------------------------------------------------------------
# The credential
# ---------------------------------------------------------------------------

def test_refuses_to_run_gh_when_the_token_file_is_missing(tmp_path):
    """Runs past the dry-run gate deliberately (that is where the token is
    read). `PATH` carries no `gh`, so a deleted guard shows up as a 127, not
    as a passing test."""
    result = run(VIEW, token=str(tmp_path / "nope"), dry_run=False)
    assert result.returncode == 1
    assert "cannot read token file" in result.stderr


def test_refuses_to_run_gh_when_the_token_file_is_empty(tmp_path):
    """An empty credential does not make `gh` fail -- it makes it run
    unauthenticated, and an unauthenticated search answers with an empty
    result set that reads exactly like "this org has no open PRs"."""
    empty = tmp_path / "gh_token"
    empty.write_text("")
    result = run(VIEW, token=str(empty), dry_run=False)
    assert result.returncode == 1
    assert "is empty" in result.stderr


# ---------------------------------------------------------------------------
# The reads discovery actually makes, and the deployed copy
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("args", [SEARCH, VIEW, CHECKS], ids=["search", "view",
                                                             "checks"])
def test_allows_the_three_reads_discovery_makes(args):
    """The argv here are the ones `discover.py` builds. If this fails, the
    cron is inert -- an allowlist that refuses its own caller is worse than
    no wrapper."""
    result = run(args)
    assert result.returncode == 0, result.stderr
    assert result.stdout.startswith("ghx_discovery: would run gh ")


@pytest.mark.allow_real_home_io
@pytest.mark.skipif(not DEPLOYED.exists(),
                    reason="wrapper is not deployed on this machine")
def test_the_deployed_wrapper_matches_the_copy_under_test():
    """`~/.hermes` is not a git repo, so the live wrapper is a copy, and a
    copy is a thing that drifts. Every test above exercises the repo copy;
    production runs the deployed one. This is the only line that says they
    are the same file."""
    assert DEPLOYED.read_bytes() == FIXTURE.read_bytes()
    assert os.access(DEPLOYED, os.X_OK)
