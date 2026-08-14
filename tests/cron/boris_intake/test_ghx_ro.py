import subprocess
from pathlib import Path

GHX_RO = Path(__file__).with_name("fixtures") / "ghx_ro.sh"
TOKEN_FIXTURE = GHX_RO.with_name("gh_token")


def run(args, scope="Ylopo/repo#1"):
    return subprocess.run(
        [str(GHX_RO), *args], capture_output=True, text=True,
        env={"PATH": "/usr/bin:/bin", "BORIS_PR_SCOPE": scope,
             "BORIS_GH_TOKEN_PATH": str(TOKEN_FIXTURE), "GHX_RO_DRY_RUN": "1"},
    )


def test_refuses_approve():
    r = run(["pr", "review", "1", "-R", "Ylopo/repo", "--approve"])
    assert r.returncode == 3 and "REFUSED" in r.stderr


def test_refuses_request_changes():
    r = run(["pr", "review", "1", "-R", "Ylopo/repo", "--request-changes"])
    assert r.returncode == 3 and "REFUSED" in r.stderr


def test_refuses_merge():
    assert run(["pr", "merge", "1"]).returncode == 3


def test_refuses_workflow():
    assert run(["workflow", "list"]).returncode == 3


def test_refuses_gist():
    assert run(["gist", "create", "x"]).returncode == 3


def test_refuses_clone():
    assert run(["repo", "clone", "Ylopo/repo"]).returncode == 3


def test_refuses_pr_edit():
    assert run(["pr", "edit", "1", "--body", "x"]).returncode == 3


def test_refuses_update_branch():
    assert run(["pr", "update-branch", "1"]).returncode == 3


def test_refuses_auth():
    assert run(["auth", "token"]).returncode == 3


def test_refuses_non_get_api():
    assert run(["api", "-X", "POST", "/repos/Ylopo/repo/merges"]).returncode == 3


def test_refuses_api_outside_org():
    assert run(["api", "/repos/Other/repo/pulls/1"]).returncode == 3


def test_allows_pr_view():
    assert run(["pr", "view", "1", "-R", "Ylopo/repo"]).returncode == 0


def test_allows_pr_diff():
    assert run(["pr", "diff", "1", "-R", "Ylopo/repo"]).returncode == 0


def test_allows_comment_review_in_scope():
    r = run(["pr", "review", "1", "-R", "Ylopo/repo", "--comment",
             "--body-file", "/tmp/b.md"])
    assert r.returncode == 0


def test_refuses_comment_review_out_of_scope():
    r = run(["pr", "review", "9", "-R", "Ylopo/other", "--comment",
             "--body-file", "/tmp/b.md"], scope="Ylopo/repo#1")
    assert r.returncode == 3 and "scope" in r.stderr.lower()
