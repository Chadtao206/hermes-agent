from __future__ import annotations

import base64
import binascii
import fnmatch
import hashlib
import inspect
import json
import logging
import os
import re
import subprocess
import time
from pathlib import Path, PurePosixPath
from typing import Annotated, Any, Literal, cast
from urllib.parse import quote

try:  # mcp >= 2.0 renamed FastMCP -> MCPServer and removed mcp.server.fastmcp
    from mcp.server import MCPServer as FastMCP
except ImportError:  # mcp 1.x
    from mcp.server.fastmcp import FastMCP
from pydantic import Field, WithJsonSchema

from boris_intake.models import LifecycleRequest, PriorReview
from boris_intake.result import (
    ValidationError,
    canonicalize_judgment,
    validate_judgment,
)
from boris_intake.store import Store


logger = logging.getLogger("boris_intake.review_tools_mcp")


class ToolError(RuntimeError):
    pass


class GitHubResponseTooLarge(ToolError):
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
# Hermes persists any mcp_ tool result whose rendered handler output exceeds
# tools.budget_config.DEFAULT_MCP_RESULT_SIZE_CHARS (50k), and this lane has no
# file tool to read a persisted result back, so a spilled page is lost to the
# review. "Rendered" is what Hermes measures: the MCP server's indent=2 JSON of the
# return value, JSON-encoded once more by Hermes, so quotes, backslashes and control
# characters in code cost up to four chars each. Every page is budgeted in rendered
# chars against this figure, envelope included, with headroom under the threshold.
_RESULT_BUDGET = 44_000
# Keeping every page under the spill threshold means no page ever spills, so evidence accumulates
# in context instead. A PR needing many pages therefore grows the context ~44k at a time until
# pre-API compression fires, and compression discards the evidence -- the reviewer restarts at page
# one and thrashes until the turn budget dies, ending the lane with no verdict at all. Measured
# across 957 lane sessions: every review that produced a verdict read under 472k chars of
# unspilled evidence, every review that died read over 1.43M, and that ceiling is not a
# coincidence: measured against the context trajectory, ~400k chars of evidence costs ~94k tokens,
# so evidence crosses the compression threshold at roughly 470k -- exactly where the surviving
# reviews stop. Past it a review cannot hold its own evidence, whatever the turn budget says. The
# paginator itself goes further (its tests walk a 2,000-file diff to 1.22M) because paging is a
# separate concern from what one context can carry. Cutting off here reports a gap instead of a
# verdict, which is what an exhausted turn budget already produces -- the same outcome, named,
# after ~12 pages instead of 60.
_EVIDENCE_BUDGET = 500_000
# Keyed by request id because a server may bind more than one review over its life. Cleared
# wholesale when it grows past the cap: a dropped counter only restarts the tally for a review
# already in flight, which is cheaper than tracking liveness for a number this cheap to rebuild.
_EVIDENCE_TRACKED = 64
_evidence_served: dict[str, int] = {}
# Bounded lists that ride on every page or listing; the totals are always reported.
# Counts cap the work, rendered size caps what reaches the model: deep monorepo
# paths made a 300-entry all_files list alone outgrow the result budget.
_ALL_FILES_LISTED = 300
_DIR_ENTRIES_LISTED = 200
_LIST_BUDGET = 12_000
# GitHub's contents API withholds `content` above 1 MiB (encoding "none").
_CONTENTS_LIMIT = 1_048_576
_NOT_FOUND = re.compile(r"HTTP 404|Not Found")
_METADATA_FIELDS = "headRefOid,title,body,author,state,baseRefName,headRefName"
_PATH = "/usr/local/bin:/opt/homebrew/bin:/usr/bin:/bin"
# GitHub refuses to render a pull request diff once it grows past roughly 20k
# changed lines. The changed-file inventory still answers, so it is the only
# changed-code evidence available on the largest pull requests. Ask for 50 files
# a page rather than the 100 maximum; oversized batches shrink further.
# GitHub withholds fewer patches on the smaller page (on a
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
        "reason_code": {
            "type": "string",
            "minLength": 1,
            "maxLength": 128,
            "description": (
                "Stable code for why core assessment was prevented. Meaningful only "
                "when prevents_core_assessment is true; ignored otherwise."
            ),
        },
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
    "description": (
        "The Boris-owned judgment: exactly these nine fields. Request identity, "
        "policy, gate, outcome, and destination are owned by workflow code and "
        "are rejected here."
    ),
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
    task_id = os.environ.get("BORIS_KANBAN_TASK", "")
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
        raise GitHubResponseTooLarge("GitHub evidence response exceeds 2 MiB")
    if not expect_json:
        return output
    try:
        return json.loads(output)
    except ValueError as error:
        raise ToolError("GitHub evidence returned malformed JSON") from error


def _metadata(request: LifecycleRequest) -> dict:
    """Identity and description of the bound pull request. Comments and reviews are
    unbounded and belong to pr_comments; carrying them here made the first call of
    every lane spill on busy pull requests and took review_context down with it."""
    data = _github(
        request,
        [
            "pr",
            "view",
            str(request.pr_number),
            "-R",
            f"{request.owner}/{request.repo}",
            "--json",
            _METADATA_FIELDS,
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


def _rendered_len(text: str) -> int:
    """Chars *text* occupies in the model-facing result: the MCP server JSON-encodes
    the return value and Hermes JSON-encodes that string once more. Measured by
    double-encoding, minus the two pairs of quotes."""
    return len(json.dumps(json.dumps(text, ensure_ascii=False), ensure_ascii=False)) - 6


def _fit(text: str, budget: int) -> str:
    """Longest prefix of *text* whose rendered length fits *budget*."""
    if budget <= 0:
        return ""
    if _rendered_len(text) <= budget:
        return text
    low, high = 0, len(text)
    while low < high:
        mid = (low + high + 1) // 2
        if _rendered_len(text[:mid]) <= budget:
            low = mid
        else:
            high = mid - 1
    return text[:low]


def _rendered_items_len(items: list, depth: int = 2) -> int:
    """Rendered size of a JSON list as the model receives it (indent=2, encoded twice)
    when the list sits *depth* levels deep in the result: every line inside it carries
    2*depth more indentation than a top-level render. Lists under `data` are at 2;
    with real gh comment shapes (a dozen lines each) the difference is what tipped a
    600-comment thread over the threshold."""
    inner = json.dumps(items, indent=2, ensure_ascii=False)
    return len(json.dumps(inner, ensure_ascii=False)) + 2 * depth * inner.count("\n")


def _fit_items(items: list, budget: int, depth: int = 2) -> list:
    """Longest prefix of *items* whose rendered JSON fits *budget* at *depth*."""
    if budget <= 0:
        return []
    if _rendered_items_len(items, depth) <= budget:
        return items
    low, high = 0, len(items)
    while low < high:
        mid = (low + high + 1) // 2
        if _rendered_items_len(items[:mid], depth) <= budget:
            low = mid
        else:
            high = mid - 1
    return items[:low]


def _content_budget(envelope: dict) -> int:
    """Rendered chars left for content once *envelope* (the result with its content
    blank and its counters at their widest) is paid for. Every list in an envelope is
    itself size-bounded, so the small floor only guards against a negative budget."""
    used = len(json.dumps(json.dumps(envelope, indent=2, ensure_ascii=False), ensure_ascii=False))
    return max(_RESULT_BUDGET - used, 2_000)


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
        return [(path, _fit(text, budget))]
    prefix = "".join(header)
    for hunk in hunks:
        body = "".join(hunk)
        room = budget - _rendered_len(prefix)
        if _rendered_len(body) > room:
            marker = (
                f"\n[boris: {body.count(chr(10))} lines of this hunk omitted — "
                f"read {path} with file_at_head for full content]\n"
            )
            kept = _fit(body, room - _rendered_len(marker)).rsplit("\n", 1)[0]
            omitted = body[len(kept) :].count("\n")
            body = (
                f"{kept}\n[boris: {omitted} lines of this hunk omitted — "
                f"read {path} with file_at_head for full content]\n"
            )
        pieces.append((path, prefix + body))
        prefix = ""
    return pieces


def paginate_diff(diff: str, page: int, budget: int | None = None) -> dict:
    """Page a unified diff at file boundaries so nothing is silently truncated.
    *budget* is rendered chars for the diff text plus the per-page file list; by
    default it is what remains of _RESULT_BUDGET after the envelope."""
    sections = _diff_sections(diff)
    all_files = sorted({path for path, _ in sections if path})
    listed = _fit_items(all_files[:_ALL_FILES_LISTED], _LIST_BUDGET)
    if budget is None:
        envelope = {
            "operation": "pr_diff",
            "head_sha": "0" * 40,
            "data": {
                "diff": "",
                "page": 9_999,
                "total_pages": 9_999,
                "files_on_page": [],
                "has_more": False,
                "all_files": listed,
                "total_files": len(all_files),
                "all_files_truncated": False,
                "source": "changed_files_api",
            },
        }
        budget = _content_budget(envelope)

    units: list[tuple[str, str]] = []
    for path, text in sections:
        if _rendered_len(text) > budget:
            units.extend(_split_oversized(path, text, budget))
        else:
            units.append((path, text))

    def cost(unit: tuple[str, str]) -> int:
        # The diff text plus this file's entry in files_on_page (indent, quotes,
        # comma and newline, rendered twice).
        return _rendered_len(unit[1]) + _rendered_len(unit[0]) + 13

    pages: list[list[tuple[str, str]]] = []
    current: list[tuple[str, str]] = []
    size = 0
    for unit in units:
        if current and size + cost(unit) > budget:
            pages.append(current)
            current, size = [], 0
        current.append(unit)
        size += cost(unit)
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
        "all_files": listed,
        "total_files": len(all_files),
        "all_files_truncated": len(listed) < len(all_files),
    }


# A repo that marks a path `-diff` or `linguist-generated=true` in .gitattributes is stating that
# the file is machine-written and its line diff is not the reviewable record -- forge-content-service
# and million-stars both say so of their emitted OpenAPI spec and generated client, and pin them with
# a CI drift guard instead. GitHub honours that in its own UI but the pulls/files API returns the
# patch regardless, so without this the reviewer reads tens of thousands of generated lines to reach
# a few hundred hand-written ones, and on a large PR never reaches them at all. The file stays in the
# inventory and stays readable with file_at_head; only its patch is withheld.
_GENERATED_ATTRS = ("-diff", "linguist-generated=true", "linguist-generated")


def _generated_globs(request: LifecycleRequest) -> tuple[str, ...]:
    """Paths the repo itself declares machine-written, read from .gitattributes at the head."""
    owner = quote(request.owner, safe="")
    repo = quote(request.repo, safe="")
    try:
        raw = _github(
            request,
            ["api", f"/repos/{owner}/{repo}/contents/.gitattributes"
                    f"?ref={quote(request.head_sha, safe='')}", "--jq", ".content // empty"],
            expect_json=False,
            allowed_codes=(0, 1),
        )
    except ToolError:
        return ()
    if not isinstance(raw, str) or not raw.strip():
        return ()
    try:
        text = base64.b64decode(raw.strip(), validate=False).decode("utf-8", "replace")
    except (binascii.Error, ValueError):
        return ()
    globs: list[str] = []
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        fields = line.split()
        if len(fields) < 2:
            continue
        if any(attr in fields[1:] for attr in _GENERATED_ATTRS):
            globs.append(fields[0])
    return tuple(globs)


def _is_generated(filename: str, globs: tuple[str, ...]) -> bool:
    """gitattributes matching, narrowed to what these patterns use: a pattern with no slash
    matches the basename at any depth, one with a slash matches the whole path."""
    if not globs:
        return False
    for raw in globs:
        pattern = raw.lstrip("/")
        if fnmatch.fnmatch(filename, pattern):
            return True
        if "/" not in pattern and fnmatch.fnmatch(PurePosixPath(filename).name, pattern):
            return True
        if pattern.endswith("/**") and filename.startswith(pattern[:-3] + "/"):
            return True
    return False


def _changed_file_section(entry: object, generated: tuple[str, ...] = ()) -> str:
    """Rebuild one file's diff section from its changed-file inventory entry."""
    if not isinstance(entry, dict):
        raise ToolError("GitHub changed-file entry is not an object")
    filename = str(entry.get("filename") or "")
    if not filename:
        raise ToolError("GitHub changed-file entry has no filename")
    previous = entry.get("previous_filename")
    header = f"diff --git a/{previous or filename} b/{filename}\n"
    patch = entry.get("patch")
    if _is_generated(filename, generated):
        changes = entry.get("changes", 0)
        return header + (
            f"[boris: {filename} is declared machine-written in .gitattributes ({changes} changed "
            f"lines); its patch is not shown because the generator's inputs, not this file, are the "
            f"reviewable record. Read it with file_at_head if a specific emitted value matters]\n"
        )
    if isinstance(patch, str) and patch:
        return header + (patch if patch.endswith("\n") else patch + "\n")
    status = str(entry.get("status") or "modified")
    changes = entry.get("changes", 0)
    if status == "removed":
        # Deleted files are gone at head, so file_at_head cannot recover them.
        note = (
            f"[boris: Patch unavailable from GitHub or the evidence size limit for this deleted file "
            f"({changes} lines removed); it is not readable at head]\n"
        )
    else:
        note = (
            f"[boris: Patch unavailable from GitHub or the evidence size limit for this file (status {status}, "
            f"{changes} changed lines) — read {filename} with file_at_head "
            f"for full content]\n"
        )
    return header + note


def _changed_files_diff(request: LifecycleRequest) -> str:
    """A unified diff rebuilt from the changed-file inventory."""
    owner = quote(request.owner, safe="")
    repo = quote(request.repo, safe="")
    generated = _generated_globs(request)
    sections: list[str] = []
    exhausted = True
    per_page = _FILES_PER_PAGE
    page = 1
    limit = _MAX_FILE_PAGES * _FILES_PER_PAGE
    while (page - 1) * per_page < limit:
        args = [
            "api",
            f"/repos/{owner}/{repo}/pulls/{request.pr_number}"
            f"/files?per_page={per_page}&page={page}",
        ]
        try:
            batch = _github(request, args, expect_json=True)
        except GitHubResponseTooLarge:
            if per_page > 1:
                # Restart because changing per_page changes every page boundary.
                per_page = max(1, per_page // 2)
                page = 1
                sections.clear()
                continue
            batch = _github(
                request, [*args, "--jq", "map(del(.patch))"], expect_json=True
            )
        if not isinstance(batch, list):
            raise ToolError("GitHub changed-file response is not a list")
        sections.extend(_changed_file_section(entry, generated) for entry in batch)
        if len(batch) < per_page:
            exhausted = False
            break
        page += 1
    if not sections:
        raise ToolError("GitHub reported no changed files for this pull request")
    if exhausted:
        # Never let a bounded walk read as complete coverage.
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
    except GitHubResponseTooLarge:
        return _changed_files_diff(request), "changed_files_api"
    except GitHubCommandError as error:
        if not _DIFF_REFUSED.search(error.stderr):
            raise
        return _changed_files_diff(request), "changed_files_api"
    return raw, "pr_diff"


def _split_lines(text: str) -> list[str]:
    """Lines by "\\n" only, newline kept. Editors, blame and diff hunks number lines
    this way; str.splitlines would also break on form feeds and U+2028 and put the
    reviewer's line anchors off by one."""
    if not text:
        return []
    pieces = text.split("\n")
    lines = [piece + "\n" for piece in pieces[:-1]]
    if pieces[-1]:
        lines.append(pieces[-1])
    return lines


def paginate_text(text: str, page: int, budget: int) -> dict:
    """Page a decoded file by whole lines so a large file is read in full across
    pages instead of being cut by the transport. *budget* is rendered chars. A single
    line longer than a page (minified bundles, fixtures) is shortened with a marker
    and still counts as one line, so line numbers stay true to the file."""
    lines = _split_lines(text)
    total_lines = len(lines)
    pages: list[tuple[int, int, str]] = []
    current: list[str] = []
    first = 1
    size = 0
    for index, line in enumerate(lines, start=1):
        cost = _rendered_len(line)
        if cost > budget:
            newline = "\n" if line.endswith("\n") else ""
            body = line[: len(line) - len(newline)]
            marker = f" [boris: {len(body)} chars of this line omitted]"
            kept = _fit(body, budget - _rendered_len(marker + newline))
            line = f"{kept} [boris: {len(body) - len(kept)} chars of this line omitted]{newline}"
            cost = _rendered_len(line)
        if current and size + cost > budget:
            pages.append((first, index - 1, "".join(current)))
            current, size, first = [], 0, index
        current.append(line)
        size += cost
    if current or not pages:
        pages.append((first if total_lines else 0, total_lines, "".join(current)))

    total = len(pages)
    if page < 1 or page > total:
        raise ToolError(f"file page {page} is out of range (1..{total})")
    first_line, last_line, content = pages[page - 1]
    return {
        "content": content,
        "page": page,
        "total_pages": total,
        "has_more": page < total,
        "line_range": [first_line, last_line],
        "total_lines": total_lines,
    }


def _file_evidence(request: LifecycleRequest, file_path: str, page: int) -> dict:
    """One repository path at the pinned head: decoded text pages for a file, a
    bounded listing for a directory. The raw contents-API object (base64 blob,
    API links) never reaches the model."""
    route = (
        f"/repos/{quote(request.owner, safe='')}/{quote(request.repo, safe='')}"
        f"/contents/{quote(file_path, safe='/')}?ref={request.head_sha}"
    )
    try:
        data = _github(request, ["api", route], expect_json=True)
    except GitHubCommandError as error:
        if _NOT_FOUND.search(error.stderr):
            reason = " ".join(error.stderr.split())[:_STDERR_EXCERPT]
            raise ToolError(
                f"{file_path} does not exist at the pinned head; take paths from "
                "pr_diff all_files (minus files the diff deletes) or list the "
                f"directory with file_at_head ({reason})"
            ) from error
        raise
    if isinstance(data, list):
        if page != 1:
            raise ToolError("a directory listing has a single page")
        # Entry paths are the listing path plus the name; repeating them doubled the
        # size of deep listings.
        entries = [
            {"name": entry.get("name"), "type": entry.get("type"), "size": entry.get("size")}
            for entry in data[:_DIR_ENTRIES_LISTED]
            if isinstance(entry, dict)
        ]
        envelope = {
            "operation": "file_at_head",
            "head_sha": request.head_sha,
            "data": {
                "path": file_path,
                "type": "dir",
                "entries": [],
                "total_entries": len(data),
                "truncated": False,
            },
        }
        entries = _fit_items(entries, _content_budget(envelope))
        return {
            "path": file_path,
            "type": "dir",
            "entries": entries,
            "total_entries": len(data),
            "truncated": len(entries) < len(data),
        }
    if not isinstance(data, dict):
        raise ToolError("GitHub contents response is not an object")
    kind = data.get("type")
    if kind not in (None, "file"):
        raise ToolError(f"{file_path} is a {kind}, not a readable file")
    size = data.get("size") if isinstance(data.get("size"), int) else 0
    encoding = data.get("encoding")
    raw_content = data.get("content")
    if encoding == "none" or (not raw_content and size > _CONTENTS_LIMIT):
        raise ToolError(
            f"{file_path} is {size} bytes; GitHub withholds file content above "
            "1 MiB, so it cannot be read here"
        )
    if encoding != "base64" or not isinstance(raw_content, str):
        raise ToolError("GitHub contents response carries no readable content")
    try:
        raw = base64.b64decode(raw_content)
    except (ValueError, binascii.Error) as error:
        raise ToolError("GitHub contents response is not valid base64") from error
    head = {"path": file_path, "sha": data.get("sha"), "size": size}
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError:
        text = None
    if text is None or "\x00" in text:
        if page != 1:
            raise ToolError("a binary file has no pages")
        return {
            **head,
            "binary": True,
            "content": None,
            "note": "binary or non-UTF-8 content is not shown",
        }
    envelope = {
        "operation": "file_at_head",
        "head_sha": request.head_sha,
        "data": {
            **head,
            "encoding": "utf-8",
            "content": "",
            "page": 9_999,
            "total_pages": 9_999,
            "has_more": False,
            "line_range": [9_999_999, 9_999_999],
            "total_lines": 9_999_999,
        },
    }
    return {**head, "encoding": "utf-8", **paginate_text(text, page, _content_budget(envelope))}


def _prior_review_context(prior: PriorReview | None, request: LifecycleRequest) -> dict | None:
    if prior is None:
        return None
    return {
        "head_sha": prior.head_sha,
        "same_head": prior.head_sha == request.head_sha,
        "outcome": prior.outcome,
        "gate_only": prior.gate_only,
        "assembled_at": prior.assembled_at,
        "blocking_titles": list(prior.blocking_titles),
        "non_blocking_titles": list(prior.non_blocking_titles),
    }


def _review_context(store: Store, request: LifecycleRequest) -> dict:
    """Workflow-owned facts the lane needs and cannot get from GitHub. The kanban
    card body never reaches the model (the worker's only user message is the bare
    task id and the kanban toolset is off), so this rides on pr_metadata, the one
    call every lane makes first. Only validated store fields are rendered. A store
    hiccup here must not fail that first call: GitHub has already answered, and the
    review can proceed without context."""
    try:
        return {
            "policy": {
                "rubric_version": request.policy.rubric_version,
                "ignore_ci": request.policy.ignore_ci,
            },
            "acceptance_references": list(store.acceptance_references(request.request_id)),
            "prior_review": _prior_review_context(store.prior_review(request), request),
        }
    except Exception as error:
        logger.warning("review context unavailable for %s: %s", request.request_id, error)
        return {"unavailable": "review context could not be loaded"}


def _bounded_metadata(operation: str, request: LifecycleRequest, context: dict, data: dict) -> dict:
    """pr_metadata with the description fitted to what remains of the budget after
    review_context. Dependabot-style bodies run to 65k chars and would spill the one
    result every lane depends on."""
    body = data.get("body")
    if isinstance(body, str):
        envelope = {
            "operation": operation,
            "head_sha": request.head_sha,
            "review_context": context,
            "data": {**data, "body": "", "body_truncated": False},
        }
        room = _content_budget(envelope)
        if _rendered_len(body) > room:
            marker = f"\n[boris: {len(body)} chars of the description omitted]"
            kept = _fit(body, room - _rendered_len(marker))
            data = {
                **data,
                "body": f"{kept}\n[boris: {len(body) - len(kept)} chars of the description omitted]",
                "body_truncated": True,
            }
    return {
        "operation": operation,
        "head_sha": request.head_sha,
        "review_context": context,
        "data": data,
    }


def _bounded_entry(entry: object, cap: int, noun: str) -> object:
    """One comment or review with its body fitted to *cap* rendered chars. Bot
    comments (coverage tables, dependency changelogs) are routinely the newest and
    tens of thousands of chars; unbounded, one of them would consume the thread's
    whole room and the newest-first fit would keep nothing at all."""
    if not isinstance(entry, dict):
        return entry
    record = cast("dict[str, Any]", entry)
    body = record.get("body")
    if not isinstance(body, str) or _rendered_len(body) <= cap:
        return entry
    marker = f"\n[boris: {len(body)} chars of this {noun} omitted]"
    kept = _fit(body, cap - _rendered_len(marker))
    return {
        **record,
        "body": f"{kept}\n[boris: {len(body) - len(kept)} chars of this {noun} omitted]",
        "body_truncated": True,
    }


def _bounded_thread(request: LifecycleRequest, data: dict) -> dict:
    """Issue comments and review summaries, newest kept when the thread outgrows the
    budget. GitHub lists both oldest first; a re-review needs the latest exchange, and
    the counts say what was dropped."""
    raw_comments = data.get("comments")
    raw_reviews = data.get("reviews")
    comments: list = raw_comments if isinstance(raw_comments, list) else []
    reviews: list = raw_reviews if isinstance(raw_reviews, list) else []
    envelope = {
        "operation": "pr_comments",
        "head_sha": request.head_sha,
        "data": {
            "comments": [],
            "reviews": [],
            "total_comments": len(comments),
            "total_reviews": len(reviews),
            "comments_omitted": len(comments),
            "reviews_omitted": len(reviews),
            "note": "omitted entries are the oldest",
        },
    }
    room = _content_budget(envelope)
    per_entry = room // 4
    comments = [_bounded_entry(entry, per_entry, "comment") for entry in comments]
    reviews = [_bounded_entry(entry, per_entry, "review") for entry in reviews]
    kept_reviews = list(reversed(_fit_items(list(reversed(reviews)), room // 3)))
    kept_comments = list(
        reversed(_fit_items(list(reversed(comments)), room - _rendered_items_len(kept_reviews)))
    )
    bounded: dict[str, object] = {
        "comments": kept_comments,
        "reviews": kept_reviews,
        "total_comments": len(comments),
        "total_reviews": len(reviews),
        "comments_omitted": len(comments) - len(kept_comments),
        "reviews_omitted": len(reviews) - len(kept_reviews),
    }
    if bounded["comments_omitted"] or bounded["reviews_omitted"]:
        bounded["note"] = "omitted entries are the oldest"
    return bounded


_GITHUB_TOOL_DESCRIPTION = (
    "Read-only evidence about the one pull request bound to this review, always at "
    "its pinned head. Call pr_metadata first: it returns title, body, author, state "
    "and branches, plus review_context, which is workflow-owned and carries the "
    "review policy, the authorized acceptance references, and any prior Boris review "
    "of this pull request. pr_diff returns the unified diff paged at file "
    "boundaries: read every page while has_more is true, and read a hunk the diff "
    "marks as shortened in full with file_at_head. pr_checks returns check runs "
    "(name, state, link); the merge gate is decided by workflow code, not by the "
    "reviewer. pr_comments returns the issue comments and review summaries. "
    "file_at_head returns one repository file at the pinned head as decoded UTF-8 "
    "text, paged by whole lines with line_range, or a bounded directory listing "
    "when the path is a directory; the path must exist at head, so take it from "
    "pr_diff all_files (minus files the diff deletes), from a directory listing, or "
    "from an import you have read."
)
_ACCEPTANCE_TOOL_DESCRIPTION = (
    "Return the pre-fetched ticket this pull request claims to implement, as "
    "authorized for this review. The reference must be exactly one of "
    "review_context.acceptance_references from pr_metadata, a bare Jira key such as "
    "PLAT-123. URLs, other ticket keys, and repository document paths are never "
    "authorized; read repository documents with file_at_head. When "
    "acceptance_references is empty there is no acceptance evidence for this "
    "review: record that as a verification limit and assess the changed code on "
    "its own terms."
)
_SUBMIT_TOOL_DESCRIPTION = (
    "Submit the single typed Boris judgment for this review. The payload holds "
    "exactly the nine judgment fields defined by the schema and nothing about "
    "request identity, policy, gate, outcome, or destination; workflow code owns "
    "those. A validation_error result names the schema mechanic to fix: correct "
    "only that and resubmit the same judgment. An accepted result ends the review; "
    "do not submit again."
)

server = FastMCP("boris_review")


def _record_evidence(request_id: str, result: dict) -> None:
    if len(_evidence_served) >= _EVIDENCE_TRACKED and request_id not in _evidence_served:
        _evidence_served.clear()
    _evidence_served[request_id] = _evidence_served.get(request_id, 0) + len(
        json.dumps(result, ensure_ascii=False))


@server.tool(description=_GITHUB_TOOL_DESCRIPTION)
def read_github_evidence(
    operation: Annotated[
        Literal[
            "pr_metadata",
            "pr_diff",
            "pr_checks",
            "pr_comments",
            "file_at_head",
        ],
        Field(
            description=(
                "pr_metadata (call first; includes review_context), pr_diff (paged), "
                "pr_checks, pr_comments (issue comments and review summaries), or "
                "file_at_head (decoded file text or a directory listing, needs path)."
            )
        ),
    ],
    path: Annotated[
        str | None,
        Field(
            description=(
                "Repository-relative path for file_at_head: a file to read or a "
                "directory to list. Not accepted by other operations."
            )
        ),
    ] = None,
    page: Annotated[
        int,
        Field(
            description=(
                "1-based page for pr_diff and file_at_head; keep reading while "
                "has_more is true. Other operations have a single page."
            )
        ),
    ] = 1,
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
    if page != 1 and operation not in ("pr_diff", "file_at_head"):
        raise ToolError("page is valid only for pr_diff and file_at_head")

    store, request, _ = _bound_request()
    try:
        served = _evidence_served.get(request.request_id, 0)
        if served >= _EVIDENCE_BUDGET:
            # Refused before the fetch, so a reviewer that retries pays nothing and cannot thrash.
            raise ToolError(
                f"Evidence budget exhausted: {served:,} chars already read for this review, past "
                f"the {_EVIDENCE_BUDGET:,} that fit in one context. Further pages would push the "
                "context into compression and discard the evidence already gathered, so this PR "
                "cannot be assessed by reading more. Submit the judgment now with a verification "
                "gap whose prevents_core_assessment is true and whose reason_code is "
                "DIFF_EVIDENCE_RESPONSE_TOO_LARGE, citing the evidence actually inspected."
            )
        if operation == "pr_metadata":
            # review_context precedes GitHub's payload on purpose: if the result ever
            # spills, Hermes keeps a head preview, and the workflow-owned facts must
            # be in it.
            data = _metadata(request)
            result = _bounded_metadata(operation, request, _review_context(store, request), data)
            _record_evidence(request.request_id, result)
            return result
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
                thread = _github(
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
                if not isinstance(thread, dict):
                    raise ToolError("GitHub comments response is not an object")
                data = _bounded_thread(request, thread)
            else:
                data = _file_evidence(request, file_path, page)
        result = {"operation": operation, "head_sha": request.head_sha, "data": data}
        _record_evidence(request.request_id, result)
        return result
    finally:
        store.close()


@server.tool(description=_ACCEPTANCE_TOOL_DESCRIPTION)
def read_acceptance_evidence(
    reference: Annotated[
        str,
        Field(
            description=(
                "One of review_context.acceptance_references from pr_metadata, "
                "for example PLAT-123."
            )
        ),
    ],
) -> dict:
    if not isinstance(reference, str) or not reference or len(reference) > 512:
        raise ToolError("acceptance evidence reference is invalid")
    store, request, _ = _bound_request()
    try:
        evidence = store.acceptance_evidence(request.request_id, reference)
        if evidence is None:
            authorized = store.acceptance_references(request.request_id)
            hint = (
                f"authorized references: {', '.join(authorized)}"
                if authorized
                else (
                    "no acceptance evidence is authorized for this request; record "
                    "the limit and assess the changed code on its own terms"
                )
            )
            raise ToolError(f"reference is not authorized for this review request; {hint}")
        return evidence
    finally:
        store.close()


def _complete_bound_task(task_id: str, request_id: str, judgment_hash: str) -> bool:
    raw_db = os.environ.get("HERMES_KANBAN_DB", "")
    db_path = Path(raw_db) if raw_db else None
    if db_path is None or not db_path.is_absolute() or not db_path.is_file():
        raise ToolError("Kanban database configuration is missing or invalid")
    run_id_raw = os.environ.get("BORIS_KANBAN_RUN_ID")
    try:
        run_id = int(run_id_raw) if run_id_raw else None
    except ValueError as error:
        raise ToolError("Kanban run binding is malformed") from error
    from hermes_cli import kanban_db

    from hermes_cli import kanban_db_connect as kbc
    # Upstream v0.21.0 (cut over 2026-09-06) dropped the fork-only ``operation_id``
    # idempotency kwarg from complete_task. Passing it raised TypeError inside the
    # MCP tool, the judgment was accepted but the lane never completed, and the
    # dispatcher re-ran every review 3 more times as a protocol violation.
    completion_kwargs: dict = {}
    if "operation_id" in inspect.signature(kanban_db.complete_task).parameters:
        completion_kwargs["operation_id"] = f"boris-review-submit:{request_id}:{judgment_hash}"
    with kbc.connect_closing(db_path) as connection:
        return kanban_db.complete_task(
            connection,
            task_id,
            summary="Structured Boris review judgment accepted.",
            expected_run_id=run_id,
            **completion_kwargs,
        )


@server.tool(description=_SUBMIT_TOOL_DESCRIPTION)
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




def _unfence_kanban() -> None:
    """Kanban workers hand every child process Hermes' delegate fence, which opens the board
    read-only. This server's only board write is completing the lane it is bound to."""
    os.environ.pop("HERMES_DELEGATED_CHILD_CONTEXT", None)

def main() -> None:
    _unfence_kanban()
    server.run(transport="stdio")


if __name__ == "__main__":
    main()
