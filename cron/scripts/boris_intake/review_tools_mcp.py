from __future__ import annotations

import hashlib
import json
import os
import re
import subprocess
import time
from pathlib import Path, PurePosixPath
from typing import Annotated, Literal
from urllib.parse import quote

from mcp.server.fastmcp import FastMCP
from pydantic import WithJsonSchema

from boris_intake.models import LifecycleRequest
from boris_intake.result import (
    ValidationError,
    canonicalize_judgment,
    validate_judgment,
)
from boris_intake.store import Store


class ToolError(RuntimeError):
    pass


class GitHubCommandError(ToolError):
    """A refused `gh` invocation, carrying GitHub's own reason for refusing."""

    def __init__(self, message: str, *, returncode: int, stderr: str) -> None:
        super().__init__(message)
        self.returncode = returncode
        self.stderr = stderr


GHX_RO = "/Users/ctao/.hermes/scripts/pr/ghx_ro.sh"
GITHUB_RUNNER = subprocess.run
_PROFILE = "boris_on_demand"
_TASK_ID = re.compile(r"^t_[A-Za-z0-9_:-]{1,128}$")
_OPERATIONS = frozenset({
    "pr_metadata",
    "pr_diff",
    "pr_checks",
    "pr_comments",
    "file_at_head",
})
_DIFF_PAGE_BUDGET = 60_000
_PATH = "/usr/local/bin:/opt/homebrew/bin:/usr/bin:/bin"
# GitHub refuses to render a pull request diff once it grows past roughly 20k
# changed lines. The changed-file inventory still answers, so it is the only
# changed-code evidence available on the largest pull requests. Ask for 50 files
# a page rather than the 100 maximum: it keeps each response inside the 2 MiB
# ceiling above, and GitHub withholds fewer patches on the smaller page (on a
# 112-file pull request, 1 patch withheld at 50 against 3 at 100).
_DIFF_REFUSED = re.compile(r"HTTP 406|exceeded the maximum number of lines|too_large")
_FILES_PER_PAGE = 50
_MAX_FILE_PAGES = 60
_STDERR_EXCERPT = 300

_STRING_SCHEMA = {"type": "string", "minLength": 1, "maxLength": 2_000}
_ANCHOR_SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "properties": {
        "kind": {
            "type": "string",
            "enum": ["diff", "test", "ci", "ticket", "log", "documentation"],
        },
        "reference": _STRING_SCHEMA,
        "detail": _STRING_SCHEMA,
    },
    "required": ["kind", "reference"],
}
_ANCHORS_SCHEMA = {
    "type": "array",
    "items": _ANCHOR_SCHEMA,
    "minItems": 1,
    "maxItems": 50,
}
_ISSUE_SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "properties": {
        "title": _STRING_SCHEMA,
        "expected_behavior": _STRING_SCHEMA,
        "changed_mechanism": _STRING_SCHEMA,
        "trigger": _STRING_SCHEMA,
        "consequence": _STRING_SCHEMA,
        "impact_scope": _STRING_SCHEMA,
        "evidence_anchors": _ANCHORS_SCHEMA,
        "action": _STRING_SCHEMA,
    },
    "required": [
        "title",
        "expected_behavior",
        "changed_mechanism",
        "trigger",
        "consequence",
        "impact_scope",
        "evidence_anchors",
        "action",
    ],
}
_RISK_SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "properties": {
        "title": _STRING_SCHEMA,
        "observed_facts": _STRING_SCHEMA,
        "hypothesized_link": _STRING_SCHEMA,
        "unresolved_link": _STRING_SCHEMA,
        "evidence_anchors": _ANCHORS_SCHEMA,
        "verification_step": _STRING_SCHEMA,
    },
    "required": [
        "title",
        "observed_facts",
        "hypothesized_link",
        "unresolved_link",
        "evidence_anchors",
        "verification_step",
    ],
}
_GAP_SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "properties": {
        "title": _STRING_SCHEMA,
        "unassessed_claim": _STRING_SCHEMA,
        "evidence_inspected": _ANCHORS_SCHEMA,
        "missing_evidence": _STRING_SCHEMA,
        "prevents_core_assessment": {"type": "boolean"},
        "next_step": _STRING_SCHEMA,
        "reason_code": {"type": "string", "minLength": 1, "maxLength": 128},
    },
    "required": [
        "title",
        "unassessed_claim",
        "evidence_inspected",
        "missing_evidence",
        "prevents_core_assessment",
        "next_step",
    ],
}
_RELEASE_SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "properties": {
        "title": _STRING_SCHEMA,
        "requirement": _STRING_SCHEMA,
        "authoritative_source": _STRING_SCHEMA,
        "phase": {
            "type": "string",
            "enum": [
                "merge",
                "deployment",
                "migration",
                "rollout",
                "production_validation",
            ],
        },
        "completion_criterion": _STRING_SCHEMA,
    },
    "required": [
        "title",
        "requirement",
        "authoritative_source",
        "phase",
        "completion_criterion",
    ],
}
_NIT_SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "properties": {
        "title": _STRING_SCHEMA,
        "location": _STRING_SCHEMA,
        "suggestion": _STRING_SCHEMA,
    },
    "required": ["title", "location"],
}
_JUDGMENT_PAYLOAD_SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "properties": {
        "summary": {"type": "string", "minLength": 1, "maxLength": 4_000},
        "evidence_reviewed": _ANCHORS_SCHEMA,
        "verification_limits": {
            "type": "array",
            "items": _STRING_SCHEMA,
            "maxItems": 50,
        },
        "blocking_issues": {"type": "array", "items": _ISSUE_SCHEMA, "maxItems": 50},
        "non_blocking_issues": {
            "type": "array",
            "items": _ISSUE_SCHEMA,
            "maxItems": 50,
        },
        "risk_hypotheses": {"type": "array", "items": _RISK_SCHEMA, "maxItems": 50},
        "verification_gaps": {"type": "array", "items": _GAP_SCHEMA, "maxItems": 50},
        "release_conditions": {
            "type": "array",
            "items": _RELEASE_SCHEMA,
            "maxItems": 50,
        },
        "nits": {"type": "array", "items": _NIT_SCHEMA, "maxItems": 50},
    },
    "required": [
        "summary",
        "evidence_reviewed",
        "verification_limits",
        "blocking_issues",
        "non_blocking_issues",
        "risk_hypotheses",
        "verification_gaps",
        "release_conditions",
        "nits",
    ],
}
JudgmentPayload = Annotated[
    dict[str, object],
    WithJsonSchema(_JUDGMENT_PAYLOAD_SCHEMA),
]


def _bound_request() -> tuple[Store, LifecycleRequest, str]:
    task_id = os.environ.get("HERMES_KANBAN_TASK", "")
    if not _TASK_ID.fullmatch(task_id):
        raise ToolError("missing or malformed task binding")
    if os.environ.get("HERMES_PROFILE") != _PROFILE:
        raise ToolError("review profile binding is missing or invalid")
    raw_db = os.environ.get("BORIS_REVIEW_DB", "")
    db_path = Path(raw_db) if raw_db else None
    if db_path is None or not db_path.is_absolute() or not db_path.is_file():
        raise ToolError("review database configuration is missing or invalid")
    try:
        store = Store(db_path)
        request = store.request_for_lane(task_id)
    except Exception as error:
        raise ToolError("review database could not resolve the task binding") from error
    if request is None:
        store.close()
        raise ToolError("task binding is not authorized for a review request")
    return store, request, task_id


def _validate_file_path(path: str | None) -> str:
    if not isinstance(path, str) or not path or len(path) > 1_024:
        raise ToolError("file path is required")
    if "\\" in path or "://" in path or path.startswith("/") or "\x00" in path:
        raise ToolError("file path must be repository-relative")
    parts = PurePosixPath(path).parts
    if not parts or any(part in ("", ".", "..") for part in parts):
        raise ToolError("file path traversal is not allowed")
    return path


def _token_path() -> str:
    raw = os.environ.get("BORIS_GH_TOKEN_PATH", "")
    path = Path(raw) if raw else None
    if path is None or not path.is_absolute() or not path.is_file():
        raise ToolError("GitHub credential configuration is missing or invalid")
    return str(path)


def _github(
    request: LifecycleRequest,
    args: list[str],
    *,
    expect_json: bool,
    allowed_codes: tuple[int, ...] = (0,),
):
    runner = GITHUB_RUNNER
    if runner is None:
        raise ToolError("GitHub adapter is not configured")
    try:
        proc = runner(
            [GHX_RO, *args],
            capture_output=True,
            text=True,
            timeout=30,
            env={
                "PATH": _PATH,
                "BORIS_PR_SCOPE": request.slug,
                "BORIS_PR_HEAD_SHA": request.head_sha,
                "BORIS_GH_TOKEN_PATH": _token_path(),
            },
        )
    except ToolError:
        raise
    except Exception as error:
        raise ToolError("GitHub evidence read failed") from error
    if proc.returncode not in allowed_codes:
        detail = " ".join((proc.stderr or "").split())[:_STDERR_EXCERPT]
        raise GitHubCommandError(
            f"GitHub evidence read failed with status {proc.returncode}"
            + (f": {detail}" if detail else ""),
            returncode=proc.returncode,
            stderr=proc.stderr or "",
        )
    output = proc.stdout or ""
    if len(output.encode()) > 2 * 1024 * 1024:
        raise ToolError("GitHub evidence response exceeds 2 MiB")
    if not expect_json:
        return output
    try:
        return json.loads(output)
    except ValueError as error:
        raise ToolError("GitHub evidence returned malformed JSON") from error


def _metadata(request: LifecycleRequest) -> dict:
    data = _github(
        request,
        [
            "pr",
            "view",
            str(request.pr_number),
            "-R",
            f"{request.owner}/{request.repo}",
            "--json",
            "headRefOid,title,body,author,state,baseRefName,headRefName,comments,reviews",
        ],
        expect_json=True,
    )
    if not isinstance(data, dict):
        raise ToolError("GitHub metadata response is not an object")
    head = str(data.get("headRefOid") or "").lower()
    if head != request.head_sha.lower():
        raise ToolError("pull request head no longer matches the bound review")
    return data


def _ensure_current_head(request: LifecycleRequest) -> None:
    _metadata(request)


def _diff_sections(diff: str) -> list[tuple[str, str]]:
    if not diff:
        return []
    sections: list[tuple[str, str]] = []
    current_path = ""
    current: list[str] = []
    for line in diff.splitlines(keepends=True):
        if line.startswith("diff --git "):
            if current:
                sections.append((current_path, "".join(current)))
            current_path = line.rstrip("\n").split(" b/")[-1]
            current = [line]
        else:
            current.append(line)
    if current:
        sections.append((current_path, "".join(current)))
    return sections


def _split_oversized(path: str, text: str, budget: int) -> list[tuple[str, str]]:
    """Break one file's diff at hunk boundaries, marking any hunk still too large."""
    pieces: list[tuple[str, str]] = []
    header: list[str] = []
    hunks: list[list[str]] = []
    for line in text.splitlines(keepends=True):
        if line.startswith("@@"):
            hunks.append([line])
        elif hunks:
            hunks[-1].append(line)
        else:
            header.append(line)
    if not hunks:
        return [(path, text[:budget])]
    prefix = "".join(header)
    for hunk in hunks:
        body = "".join(hunk)
        room = budget - len(prefix)
        if len(body) > room:
            marker = (
                f"[boris: {len(body)} lines of this hunk omitted — "
                f"read {path} with file_at_head for full content]\n"
            )
            kept = body[: max(room - len(marker) - 1, 0)].rsplit("\n", 1)[0]
            omitted = body[len(kept) :].count("\n")
            body = (
                f"{kept}\n[boris: {omitted} lines of this hunk omitted — "
                f"read {path} with file_at_head for full content]\n"
            )
        pieces.append((path, prefix + body))
        prefix = ""
    return pieces


def paginate_diff(diff: str, page: int, budget: int = _DIFF_PAGE_BUDGET) -> dict:
    """Page a unified diff at file boundaries so nothing is silently truncated."""
    units: list[tuple[str, str]] = []
    for path, text in _diff_sections(diff):
        if len(text) > budget:
            units.extend(_split_oversized(path, text, budget))
        else:
            units.append((path, text))

    pages: list[list[tuple[str, str]]] = []
    current: list[tuple[str, str]] = []
    size = 0
    for unit in units:
        if current and size + len(unit[1]) > budget:
            pages.append(current)
            current, size = [], 0
        current.append(unit)
        size += len(unit[1])
    if current:
        pages.append(current)
    if not pages:
        pages = [[]]

    total = len(pages)
    if page < 1 or page > total:
        raise ToolError(f"diff page {page} is out of range (1..{total})")
    selected = pages[page - 1]
    return {
        "diff": "".join(text for _, text in selected),
        "page": page,
        "total_pages": total,
        "files_on_page": sorted({path for path, _ in selected if path}),
        "has_more": page < total,
        "all_files": sorted({path for path, _ in units if path}),
    }


def _changed_file_section(entry: object) -> str:
    """Rebuild one file's diff section from its changed-file inventory entry."""
    if not isinstance(entry, dict):
        raise ToolError("GitHub changed-file entry is not an object")
    filename = str(entry.get("filename") or "")
    if not filename:
        raise ToolError("GitHub changed-file entry has no filename")
    previous = entry.get("previous_filename")
    header = f"diff --git a/{previous or filename} b/{filename}\n"
    patch = entry.get("patch")
    if isinstance(patch, str) and patch:
        return header + (patch if patch.endswith("\n") else patch + "\n")
    status = str(entry.get("status") or "modified")
    changes = entry.get("changes", 0)
    if status == "removed":
        # Deleted files are gone at head, so file_at_head cannot recover them.
        note = (
            f"[boris: GitHub withheld the patch for this deleted file "
            f"({changes} lines removed); it is not readable at head]\n"
        )
    else:
        note = (
            f"[boris: GitHub withheld the patch for this file (status {status}, "
            f"{changes} changed lines) — read {filename} with file_at_head "
            f"for full content]\n"
        )
    return header + note


def _changed_files_diff(request: LifecycleRequest) -> str:
    """A unified diff rebuilt from the changed-file inventory."""
    owner = quote(request.owner, safe="")
    repo = quote(request.repo, safe="")
    sections: list[str] = []
    exhausted = True
    for page in range(1, _MAX_FILE_PAGES + 1):
        batch = _github(
            request,
            [
                "api",
                f"/repos/{owner}/{repo}/pulls/{request.pr_number}"
                f"/files?per_page={_FILES_PER_PAGE}&page={page}",
            ],
            expect_json=True,
        )
        if not isinstance(batch, list):
            raise ToolError("GitHub changed-file response is not a list")
        sections.extend(_changed_file_section(entry) for entry in batch)
        if len(batch) < _FILES_PER_PAGE:
            exhausted = False
            break
    if not sections:
        raise ToolError("GitHub reported no changed files for this pull request")
    if exhausted:
        # Never let a bounded walk read as complete coverage.
        limit = _MAX_FILE_PAGES * _FILES_PER_PAGE
        sections.append(
            f"diff --git a/x b/[boris]-changed-file-inventory-truncated\n"
            f"[boris: the changed-file inventory stopped at {limit} files; "
            f"any file beyond that is absent from this diff]\n"
        )
    return "".join(sections)


def _pr_diff(request: LifecycleRequest) -> tuple[str, str]:
    """The exact-head diff, plus which evidence route produced it."""
    try:
        raw = _github(
            request,
            [
                "pr",
                "diff",
                str(request.pr_number),
                "-R",
                f"{request.owner}/{request.repo}",
            ],
            expect_json=False,
        )
    except GitHubCommandError as error:
        if not _DIFF_REFUSED.search(error.stderr):
            raise
        return _changed_files_diff(request), "changed_files_api"
    return raw, "pr_diff"


server = FastMCP("boris_review")


@server.tool()
def read_github_evidence(
    operation: Literal[
        "pr_metadata",
        "pr_diff",
        "pr_checks",
        "pr_comments",
        "file_at_head",
    ],
    path: str | None = None,
    page: int = 1,
) -> dict:
    if operation not in _OPERATIONS:
        raise ToolError("unknown GitHub evidence operation")
    if operation == "file_at_head":
        file_path = _validate_file_path(path)
    elif path is not None:
        raise ToolError("path is valid only for file_at_head")
    else:
        file_path = ""
    if not isinstance(page, int) or isinstance(page, bool) or page < 1:
        raise ToolError("page must be a positive integer")
    if page != 1 and operation != "pr_diff":
        raise ToolError("page is valid only for pr_diff")

    store, request, _ = _bound_request()
    try:
        if operation == "pr_metadata":
            data = _metadata(request)
        else:
            _ensure_current_head(request)
            repository = f"{request.owner}/{request.repo}"
            if operation == "pr_diff":
                raw, source = _pr_diff(request)
                data = paginate_diff(raw, page)
                data["source"] = source
            elif operation == "pr_checks":
                data = _github(
                    request,
                    [
                        "pr",
                        "checks",
                        str(request.pr_number),
                        "-R",
                        repository,
                        "--json",
                        "name,state,link",
                    ],
                    expect_json=True,
                    allowed_codes=(0, 1),
                )
            elif operation == "pr_comments":
                data = _github(
                    request,
                    [
                        "pr",
                        "view",
                        str(request.pr_number),
                        "-R",
                        repository,
                        "--json",
                        "comments,reviews",
                    ],
                    expect_json=True,
                )
            else:
                route = (
                    f"/repos/{quote(request.owner, safe='')}/{quote(request.repo, safe='')}"
                    f"/contents/{quote(file_path, safe='/')}?ref={request.head_sha}"
                )
                data = _github(request, ["api", route], expect_json=True)
        return {"operation": operation, "head_sha": request.head_sha, "data": data}
    finally:
        store.close()


@server.tool()
def read_acceptance_evidence(reference: str) -> dict:
    if not isinstance(reference, str) or not reference or len(reference) > 512:
        raise ToolError("acceptance evidence reference is invalid")
    store, request, _ = _bound_request()
    try:
        evidence = store.acceptance_evidence(request.request_id, reference)
        if evidence is None:
            raise ToolError("reference is not authorized for this review request")
        return evidence
    finally:
        store.close()


def _complete_bound_task(task_id: str, request_id: str, judgment_hash: str) -> bool:
    raw_db = os.environ.get("HERMES_KANBAN_DB", "")
    db_path = Path(raw_db) if raw_db else None
    if db_path is None or not db_path.is_absolute() or not db_path.is_file():
        raise ToolError("Kanban database configuration is missing or invalid")
    run_id_raw = os.environ.get("HERMES_KANBAN_RUN_ID")
    try:
        run_id = int(run_id_raw) if run_id_raw else None
    except ValueError as error:
        raise ToolError("Kanban run binding is malformed") from error
    from hermes_cli import kanban_db

    operation_id = f"boris-review-submit:{request_id}:{judgment_hash}"
    with kanban_db.connect_closing(db_path) as connection:
        return kanban_db.complete_task(
            connection,
            task_id,
            summary="Structured Boris review judgment accepted.",
            expected_run_id=run_id,
            operation_id=operation_id,
        )


@server.tool()
def submit_review_result(payload: JudgmentPayload) -> dict:
    try:
        judgment = validate_judgment(payload)
    except ValidationError as error:
        return {
            "status": "validation_error",
            "error": str(error),
            "retry_same_judgment": True,
        }
    canonical = canonicalize_judgment(judgment)
    digest = f"sha256:{hashlib.sha256(canonical.encode()).hexdigest()}"
    store, request, task_id = _bound_request()
    try:
        try:
            status = store.submit_judgment(request.request_id, canonical, time.time())
        except ValueError as error:
            raise ToolError(str(error)) from error
        if status == "conflict":
            raise ToolError("conflicting judgment rejected; the first valid judgment is authoritative")
        if store.judgment_task_completed(request.request_id):
            return {"status": status, "task_completed": True}
        try:
            completed = _complete_bound_task(task_id, request.request_id, digest)
        except Exception as error:
            raise ToolError(
                "judgment accepted but task completion failed; retry the identical payload"
            ) from error
        if not completed:
            raise ToolError(
                "judgment accepted but task completion failed; retry the identical payload"
            )
        store.mark_judgment_task_completed(request.request_id, time.time())
        return {"status": status, "task_completed": True}
    finally:
        store.close()


def main() -> None:
    server.run(transport="stdio")


if __name__ == "__main__":
    main()
