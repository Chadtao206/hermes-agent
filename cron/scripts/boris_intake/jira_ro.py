#!/usr/bin/env python
"""Read one Jira issue as acceptance evidence for a Boris review.

Deliberately narrow, in the spirit of ``scripts/pr/ghx_ro.sh``: one issue key
in, one JSON evidence object out, read-only tools only, no search and no write
path. Runs as its own process so a slow or broken Atlassian never blocks the
intake daemon.

Exit 0 with JSON on stdout, or non-zero with nothing. Callers treat any
non-zero exit as "no acceptance evidence" and continue.
"""
from __future__ import annotations

import asyncio
import json
import os
import re
import sys

HERMES_HOME = "/Users/ctao/.hermes"
MCP_URL = "https://mcp.atlassian.com/v1/mcp"
SERVER = "atlassian_ro"
CLOUD_ID = "1125b1a5-b167-4a1a-b41e-024971a9dcad"  # ylopollc.atlassian.net
TIMEOUT_SECONDS = 35
MAX_CONTENT = 20_000

KEY = re.compile(r"^[A-Z][A-Z0-9]{1,9}-\d{1,7}$")


async def _fetch(key: str) -> dict | None:
    sys.path.insert(0, f"{HERMES_HOME}/hermes-agent")
    os.environ.setdefault("HERMES_HOME", HERMES_HOME)
    from mcp import ClientSession
    from tools.mcp_oauth_manager import get_manager

    try:  # mcp >= 2.0: streams API renamed; auth rides on a caller-owned httpx2 client
        from mcp.client.streamable_http import create_mcp_http_client, streamable_http_client
        mcp2 = True
    except ImportError:  # mcp 1.x
        from mcp.client.streamable_http import streamablehttp_client
        mcp2 = False

    async def _call(session) -> dict | None:
        await session.initialize()
        result = await session.call_tool(
            "getJiraIssue",
            {"cloudId": CLOUD_ID, "issueIdOrKey": key},
        )
        # mcp 2.0 renamed CallToolResult.isError -> is_error; accept both.
        is_error = getattr(result, "is_error", None)
        if is_error is None:
            is_error = getattr(result, "isError", False)
        if is_error or not result.content:
            return None
        return json.loads(result.content[0].text)

    auth = get_manager().get_or_build_provider(SERVER, MCP_URL, None)
    if mcp2:
        # The SDK skips cleanup when http_client is provided, so own its lifetime here.
        async with create_mcp_http_client(auth=auth) as http_client:
            async with streamable_http_client(MCP_URL, http_client=http_client) as (read, write, _):
                async with ClientSession(read, write) as session:
                    return await _call(session)
    async with streamablehttp_client(MCP_URL, auth=auth) as (read, write, _):
        async with ClientSession(read, write) as session:
            return await _call(session)


def _evidence(key: str, issue: dict) -> dict | None:
    fields = issue.get("fields") or {}
    summary = str(fields.get("summary") or "").strip()
    description = fields.get("description")
    # A ticket with no written requirement is not acceptance evidence; saying
    # so honestly beats authorizing an empty contract the reviewer would cite.
    if not isinstance(description, str) or not description.strip():
        return None
    status = ((fields.get("status") or {}).get("name")) or "unknown"
    body = f"{summary}\n\nStatus: {status}\n\n{description.strip()}"
    return {
        "kind": "ticket",
        "reference": key,
        "content": body[:MAX_CONTENT],
        "source": f"jira:{key}",
    }


def main(argv: list[str]) -> int:
    if len(argv) != 2 or not KEY.match(argv[1]):
        return 2
    key = argv[1]
    try:
        issue = asyncio.run(asyncio.wait_for(_fetch(key), timeout=TIMEOUT_SECONDS))
    except Exception:
        return 1
    if not isinstance(issue, dict) or issue.get("key", key).upper() != key:
        return 1
    evidence = _evidence(key, issue)
    if evidence is None:
        return 1
    json.dump(evidence, sys.stdout)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
