import subprocess
from pathlib import Path

GHX_RO = Path(__file__).with_name("fixtures") / "ghx_ro.sh"
TOKEN_FIXTURE = GHX_RO.with_name("gh_token")


def run(args, scope="Ylopo/repo#1", head="a" * 40):
    env = {
        "PATH": "/usr/bin:/bin",
        "BORIS_PR_SCOPE": scope,
        "BORIS_GH_TOKEN_PATH": str(TOKEN_FIXTURE),
        "GHX_RO_DRY_RUN": "1",
    }
    if head is not None:
        env["BORIS_PR_HEAD_SHA"] = head
    return subprocess.run(
        [str(GHX_RO), *args], capture_output=True, text=True,
        env=env, check=False,
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


def test_allows_pr_head_discovery_before_a_head_is_pinned():
    assert run(
        ["pr", "view", "1", "-R", "Ylopo/repo"],
        head=None,
    ).returncode == 0


def test_allows_pr_diff():
    assert run(["pr", "diff", "1", "-R", "Ylopo/repo"]).returncode == 0


def test_refuses_comment_review_even_in_scope():
    r = run(["pr", "review", "1", "-R", "Ylopo/repo", "--comment",
             "--body-file", "/tmp/b.md"])
    assert r.returncode == 3 and "REFUSED" in r.stderr


def test_refuses_comment_review_out_of_scope():
    r = run(["pr", "review", "9", "-R", "Ylopo/other", "--comment",
             "--body-file", "/tmp/b.md"], scope="Ylopo/repo#1")
    assert r.returncode == 3 and "REFUSED" in r.stderr


def test_refuses_pr_comment_even_in_scope():
    assert run(["pr", "comment", "1", "-R", "Ylopo/repo", "--body", "x"]).returncode == 3


def test_refuses_out_of_scope_pr_reads():
    assert run(["pr", "view", "9", "-R", "Ylopo/other"]).returncode == 3


def test_allows_only_file_content_api_at_the_pinned_head():
    good = "/repos/Ylopo/repo/contents/src/app.py?ref=" + "a" * 40
    bad_ref = "/repos/Ylopo/repo/contents/src/app.py?ref=" + "b" * 40
    assert run(["api", good]).returncode == 0
    assert run(["api", bad_ref]).returncode == 3
    assert run(["api", "/repos/Ylopo/repo/issues/1"]).returncode == 3


def test_file_content_api_rejects_traversal_alternate_hosts_and_body_flags():
    route = "/repos/Ylopo/repo/contents/src/app.py?ref=" + "a" * 40
    traversal = "/repos/Ylopo/repo/contents/src/../secret?ref=" + "a" * 40
    assert run(["api", traversal]).returncode == 3
    assert run(["api", "--hostname", "evil.test", route]).returncode == 3
    assert run(["api", "-F", "x=y", route]).returncode == 3
    assert run(["api", "--method=POST", route]).returncode == 3


def test_allows_the_changed_file_inventory_for_the_pinned_pull_request():
    assert run(["api", "/repos/Ylopo/repo/pulls/1/files"]).returncode == 0
    assert run(["api", "/repos/Ylopo/repo/pulls/1/files?per_page=50"]).returncode == 0
    assert run(["api", "/repos/Ylopo/repo/pulls/1/files?per_page=50&page=3"]).returncode == 0


def test_changed_file_inventory_stays_inside_the_dispatched_scope():
    # Another pull request, another repository, and neighbouring pull sub-resources
    # are all outside the scope this lane was dispatched for.
    assert run(["api", "/repos/Ylopo/repo/pulls/9/files"]).returncode == 3
    assert run(["api", "/repos/Other/repo/pulls/1/files"]).returncode == 3
    assert run(["api", "/repos/Ylopo/repo/pulls/1/reviews"]).returncode == 3
    assert run(["api", "/repos/Ylopo/repo/pulls/1/files/../../secrets"]).returncode == 3


def test_changed_file_inventory_accepts_only_pagination_query_parameters():
    for query in ("ref=deadbeef", "per_page=50&evil=1", "page=1;rm", "per_page=abc", ""):
        result = run(["api", f"/repos/Ylopo/repo/pulls/1/files?{query}"])
        assert result.returncode == 3, query
