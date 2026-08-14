"""Resolve authoritative PR facts. Head SHA never comes from Slack."""

from __future__ import annotations

import json
import re
import subprocess
from pathlib import Path

from boris_intake.models import GateSnapshot, LifecycleRequest, PRInfo, PublishedReview
from boris_intake.result import ValidationError, build_gate_snapshot, load_gate_registry

GHX_RO = "/Users/ctao/.hermes/scripts/pr/ghx_ro.sh"
_SHA = re.compile(r"^[0-9a-f]{40}$")
_GATE_REGISTRY = Path(__file__).with_name("gate_registry.json")
_RUN_ENV = {
    "PATH": "/usr/local/bin:/opt/homebrew/bin:/usr/bin:/bin",
}
GH = "/opt/homebrew/bin/gh"


def fetch_pr(
    owner: str,
    repo: str,
    number: int,
    runner=subprocess.run,
    token_path: Path | None = None,
) -> PRInfo | None:
    """Return PRInfo, or None on any failure. Never raises — callers fail closed."""
    try:
        proc = runner(
            [
                GHX_RO,
                "pr",
                "view",
                str(number),
                "-R",
                f"{owner}/{repo}",
                "--json",
                "headRefOid,author,state",
            ],
            capture_output=True,
            text=True,
            timeout=30,
            env={
                "BORIS_PR_SCOPE": f"{owner}/{repo}#{number}",
                **({"BORIS_GH_TOKEN_PATH": str(token_path)} if token_path else {}),
                **_RUN_ENV,
            },
        )
    except Exception:
        return None
    if proc.returncode != 0:
        return None
    try:
        data = json.loads(proc.stdout or "{}")
    except ValueError:
        return None
    sha = str(data.get("headRefOid") or "").lower()
    if not _SHA.match(sha):
        return None
    return PRInfo(
        head_sha=sha,
        author=str((data.get("author") or {}).get("login") or ""),
        state=str(data.get("state") or "").upper(),
    )


def _run_json(
    command: list[str],
    scope: str,
    runner,
    allowed_codes=(0,),
    token_path: Path | None = None,
    empty_stderr_prefix: str | None = None,
):
    try:
        proc = runner(
            command,
            capture_output=True,
            text=True,
            timeout=30,
            env={
                "BORIS_PR_SCOPE": scope,
                **({"BORIS_GH_TOKEN_PATH": str(token_path)} if token_path else {}),
                **_RUN_ENV,
            },
        )
    except Exception:
        return None
    if proc.returncode not in allowed_codes:
        return None
    if (
        empty_stderr_prefix
        and not (proc.stdout or "").strip()
        and str(proc.stderr or "").strip().startswith(empty_stderr_prefix)
    ):
        return []
    try:
        return json.loads(proc.stdout)
    except (TypeError, ValueError):
        return None


def fetch_gate_snapshot(
    owner: str,
    repo: str,
    number: int,
    expected_head_sha: str,
    captured_at: str,
    runner=subprocess.run,
    token_path: Path | None = None,
) -> GateSnapshot | None:
    scope = f"{owner}/{repo}#{number}"
    repository = f"{owner}/{repo}"
    head = _run_json(
        [GHX_RO, "pr", "view", str(number), "-R", repository, "--json", "headRefOid"],
        scope,
        runner,
        token_path=token_path,
    )
    if not isinstance(head, dict):
        return None
    current_head = str(head.get("headRefOid") or "").lower()
    if not _SHA.fullmatch(current_head) or current_head != expected_head_sha.lower():
        return None

    checks = _run_json(
        [
            GHX_RO,
            "pr",
            "checks",
            str(number),
            "-R",
            repository,
            "--json",
            "name,state,link",
        ],
        scope,
        runner,
        (0, 1),
        token_path,
    )
    required = _run_json(
        [
            GHX_RO,
            "pr",
            "checks",
            str(number),
            "-R",
            repository,
            "--required",
            "--json",
            "name,state,link",
        ],
        scope,
        runner,
        (0, 1),
        token_path,
        "no required checks reported on the ",
    )
    if not isinstance(checks, list) or not isinstance(required, list):
        return None
    if not all(isinstance(item, dict) for item in checks + required):
        return None
    required_names = {str(item.get("name") or "") for item in required}
    all_names = {str(item.get("name") or "") for item in checks}
    if not required_names <= all_names:
        return None
    rows = [
        dict(item, required=str(item.get("name") or "") in required_names)
        for item in checks
    ]
    try:
        return build_gate_snapshot(
            rows, captured_at, load_gate_registry(_GATE_REGISTRY)
        )
    except ValidationError:
        return None


class GitHubIO:
    def __init__(self, token_path: Path, runner=subprocess.run):
        self._token_path = Path(token_path)
        self._runner = runner

    def _token(self) -> str:
        try:
            token = self._token_path.read_text(encoding="utf-8").strip()
        except OSError as error:
            raise RuntimeError("GitHub credential is unavailable") from error
        if not token or any(character.isspace() for character in token):
            raise RuntimeError("GitHub credential is malformed")
        return token

    def _api(self, args: list[str], *, payload: dict | None = None) -> object:
        kwargs = {
            "capture_output": True,
            "text": True,
            "timeout": 30,
            "env": {**_RUN_ENV, "GH_TOKEN": self._token()},
        }
        if payload is not None:
            kwargs["input"] = json.dumps(payload, separators=(",", ":"))
        try:
            process = self._runner([GH, "api", *args], **kwargs)
        except Exception as error:
            raise RuntimeError("GitHub lifecycle call failed") from error
        if process.returncode != 0:
            detail = str(process.stderr or "").strip()[:300]
            raise RuntimeError(f"GitHub lifecycle call failed: {detail}")
        try:
            return json.loads(process.stdout or "null")
        except ValueError as error:
            raise RuntimeError(
                "GitHub lifecycle call returned malformed JSON"
            ) from error

    def current_pr(self, request: LifecycleRequest) -> PRInfo | None:
        return fetch_pr(
            request.owner,
            request.repo,
            request.pr_number,
            runner=self._runner,
            token_path=self._token_path,
        )

    def capture_gate_snapshot(
        self, request: LifecycleRequest, captured_at: str
    ) -> GateSnapshot | None:
        return fetch_gate_snapshot(
            request.owner,
            request.repo,
            request.pr_number,
            request.head_sha,
            captured_at,
            runner=self._runner,
            token_path=self._token_path,
        )

    def find_review(self, request: LifecycleRequest) -> PublishedReview | None:
        raw = self._api([
            "--paginate",
            "--slurp",
            f"repos/{request.owner}/{request.repo}/pulls/{request.pr_number}/reviews?per_page=100",
        ])
        if not isinstance(raw, list):
            raise RuntimeError("GitHub review lookup returned a non-list")
        rows = [
            item
            for page in raw
            for item in (page if isinstance(page, list) else [page])
        ]
        marker = re.compile(
            rf"<!-- boris-review request_id={re.escape(request.request_id)} "
            r"result_hash=sha256:[0-9a-f]{64} -->"
        )
        matches = [
            item
            for item in rows
            if isinstance(item, dict) and marker.search(str(item.get("body") or ""))
        ]
        if len(matches) > 1:
            raise RuntimeError("multiple marker-matched GitHub reviews found")
        if not matches:
            return None
        match = matches[0]
        review_id = str(match.get("id") or "")
        url = str(match.get("html_url") or "")
        if not review_id or not url:
            raise RuntimeError("marker-matched GitHub review is missing identity")
        return PublishedReview(review_id, url, str(match.get("body") or ""))

    def publish_review(self, request: LifecycleRequest, body: str) -> PublishedReview:
        raw = self._api(
            [
                "--method",
                "POST",
                f"repos/{request.owner}/{request.repo}/pulls/{request.pr_number}/reviews",
                "--input",
                "-",
            ],
            payload={"event": "COMMENT", "body": body},
        )
        if not isinstance(raw, dict):
            raise RuntimeError("GitHub review publication returned a non-object")
        review_id = str(raw.get("id") or "")
        url = str(raw.get("html_url") or "")
        if not review_id or not url:
            raise RuntimeError("GitHub review publication returned no identity")
        return PublishedReview(review_id, url, str(raw.get("body") or body))
