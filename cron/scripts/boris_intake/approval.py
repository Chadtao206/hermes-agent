"""Which ready reviews are published as a GitHub approval rather than a comment.

Every function returns the reason an approval is withheld, or None to approve.
Withholding never loses the review: it is published as a comment instead.
"""
from __future__ import annotations

import re

from boris_intake.result import StructuredResult

_READY = frozenset({"READY_CLEAN", "READY_WITH_COMMENTS"})
# Ylopo services cut production releases as `releases/<version>` into the production
# branch of a repository whose default branch is `development`.
_RELEASE_HEAD = re.compile(r"^releases?/", re.IGNORECASE)
_RELEASE_TITLE = re.compile(r"^chore\(release\)", re.IGNORECASE)
_PRODUCTION_BRANCHES = frozenset({"main", "master", "production", "prod"})
_SENSITIVE_PATHS = (
    # million-stars has a product feature called migrations under its Next.js routes.
    ("database migration or SQL", re.compile(
        r"^(?!(?:.*/)?src/app/)(?:.*/)?migrations?[^/]*/|(^|/)(drizzle|flyway|liquibase)/|\.sql$",
        re.IGNORECASE)),
    ("infrastructure definition", re.compile(
        r"\.tf(vars)?$|(^|/)(terraform|helm|charts|k8s|kubernetes|cloudformation)/"
        r"|(^|/)serverless\.ya?ml$", re.IGNORECASE)),
    ("CI or deploy workflow", re.compile(
        r"^\.github/workflows/|(^|/)Jenkinsfile$|^\.circleci/", re.IGNORECASE)),
    ("container build", re.compile(
        r"(^|/)Dockerfile[^/]*$|(^|/)docker-compose[^/]*\.ya?ml$", re.IGNORECASE)),
    ("environment, secret or ownership file", re.compile(
        r"(^|/)\.env(\.(?!example$|sample$|template$)[^/]*)?$|(^|/)secrets?\.(ya?ml|json)$"
        r"|(^|/)CODEOWNERS$", re.IGNORECASE)),
)


def verdict_block(result: StructuredResult) -> str | None:
    if result.derived.presentation not in _READY:
        return f"verdict is {result.derived.presentation}"
    if result.release_conditions:
        return "the review names release conditions"
    if result.policy.ignore_ci or result.waived_ci_conditions:
        return "required CI was waived"
    return None


def pull_request_block(pull: dict, files: list[str], viewer: str) -> str | None:
    base = pull.get("base") if isinstance(pull.get("base"), dict) else {}
    head = pull.get("head") if isinstance(pull.get("head"), dict) else {}
    author = pull.get("user") if isinstance(pull.get("user"), dict) else {}
    repo = base.get("repo") if isinstance(base.get("repo"), dict) else {}
    base_ref, default = str(base.get("ref") or ""), str(repo.get("default_branch") or "")
    if not base_ref or not default or not author.get("login"):
        return "pull request facts are incomplete"
    # GitHub refuses an approval of the account's own pull request with HTTP 422.
    if str(author["login"]).lower() == viewer.lower():
        return "the reviewing account authored the pull request"
    if _RELEASE_HEAD.search(str(head.get("ref") or "")) or _RELEASE_TITLE.search(
        str(pull.get("title") or "")
    ):
        return "production release"
    if base_ref != default and base_ref.lower() in _PRODUCTION_BRANCHES:
        return f"targets production branch {base_ref} directly"
    if pull.get("changed_files") != len(files):
        return "changed file list is incomplete"
    for label, pattern in _SENSITIVE_PATHS:
        for path in files:
            if pattern.search(path):
                return f"changes {label}: {path}"
    return None
