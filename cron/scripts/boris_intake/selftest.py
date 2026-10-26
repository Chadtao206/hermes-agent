"""Fail-closed preconditions for entering live intake mode."""
from __future__ import annotations

import os
import sqlite3
import subprocess
from pathlib import Path

try:
    import yaml
    from yaml import YAMLError
except ModuleNotFoundError:  # PyYAML left upstream's dependency set; hermes_yaml wraps ruamel
    import hermes_yaml as yaml
    from ruamel.yaml.error import YAMLError

from boris_intake.config import Config
from boris_intake.result import load_gate_registry
from boris_intake.store import Store

GHX_RO = Path("/Users/ctao/.hermes/scripts/pr/ghx_ro.sh")
DB_PATH = Path("/Users/ctao/.hermes/state/boris_intake.db")
GATE_REGISTRY = Path(__file__).with_name("gate_registry.json")
PROFILE_CONFIG = Path("/Users/ctao/.hermes/profiles/boris_on_demand/config.yaml")

ALLOWED_GH_VERBS = (
    ["pr", "view", "1", "-R", "Ylopo/repo", "--json", "headRefOid"],
    ["pr", "diff", "1", "-R", "Ylopo/repo"],
    ["pr", "checks", "1", "-R", "Ylopo/repo"],
    ["api", "/repos/Ylopo/repo/contents/README.md?ref=" + "a" * 40],
    ["api", "/repos/Ylopo/repo/pulls/1/files?per_page=50&page=1"],
)

REFUSED_GH_VERBS = (
    ["api", "/repos/Ylopo/repo/pulls/9/files"],
    ["workflow", "list"],
    ["gist", "create", "x"],
    ["pr", "merge", "1", "-R", "Ylopo/repo"],
    ["pr", "review", "1", "-R", "Ylopo/repo", "--comment"],
    ["pr", "comment", "1", "-R", "Ylopo/repo", "--body", "x"],
)

_EXPECTED_TOOLS = {
    "read_github_evidence",
    "read_acceptance_evidence",
    "submit_review_result",
}


def _check_profile() -> list[str]:
    try:
        profile = yaml.safe_load(PROFILE_CONFIG.read_text(encoding="utf-8")) or {}
    except (OSError, YAMLError) as error:
        return [f"boris profile unavailable: {error}"]
    if profile.get("toolsets") != ["mcp-boris_review"]:
        return ["boris profile does not select only mcp-boris_review"]
    servers = profile.get("mcp_servers")
    if not isinstance(servers, dict) or set(servers) != {"boris_review"}:
        return ["boris profile does not define only the boris_review MCP server"]
    server = servers["boris_review"]
    tools = server.get("tools") if isinstance(server, dict) else None
    included = tools.get("include") if isinstance(tools, dict) else None
    if (
        not isinstance(server, dict)
        or server.get("enabled") is not True
        or not isinstance(included, list)
        or set(included) != _EXPECTED_TOOLS
        or len(included) != len(_EXPECTED_TOOLS)
    ):
        return ["boris profile MCP tool allowlist is not sealed"]
    env = server.get("env")
    required_binding = {
        "HERMES_PROFILE": "boris_on_demand",
        "BORIS_KANBAN_TASK": "${HERMES_KANBAN_TASK}",
        "BORIS_KANBAN_RUN_ID": "${HERMES_KANBAN_RUN_ID}",
    }
    if not isinstance(env, dict) or any(
        env.get(name) != value for name, value in required_binding.items()
    ):
        return ["boris profile does not preserve the worker task binding"]
    command = Path(str(server.get("command") or ""))
    args = server.get("args")
    script = Path(str(args[0])) if isinstance(args, list) and args else Path("")
    failures = []
    if not command.is_file() or not os.access(command, os.X_OK):
        failures.append(f"boris profile MCP command is unavailable: {command}")
    if not script.is_file():
        failures.append(f"boris review MCP server is unavailable: {script}")
    return failures


def run_selftest(cfg: Config) -> list[str]:
    failures: list[str] = []

    try:
        store = Store(DB_PATH)
        store.close()
        connection = sqlite3.connect(DB_PATH)
        try:
            if connection.execute("PRAGMA user_version").fetchone()[0] != 5:
                failures.append("workflow DB migration did not reach schema version 5")
        finally:
            connection.close()
    except Exception as error:
        failures.append(f"workflow DB migration failed: {error}")

    try:
        load_gate_registry(GATE_REGISTRY)
    except Exception as error:
        failures.append(f"gate registry invalid: {error}")

    failures.extend(_check_profile())

    token_path = Path(cfg.github_token_path)
    if not token_path.is_file():
        failures.append(f"github token missing at {token_path}")
    else:
        mode = oct(token_path.stat().st_mode & 0o777)
        if mode != "0o600":
            failures.append(f"github token at {token_path} has mode {mode}, want 0o600")

    if not GHX_RO.is_file() or not os.access(GHX_RO, os.X_OK):
        failures.append(f"read-only GitHub wrapper is unavailable: {GHX_RO}")

    env = {
        "PATH": "/usr/bin:/bin",
        "BORIS_PR_SCOPE": "Ylopo/repo#1",
        "BORIS_PR_HEAD_SHA": "a" * 40,
        "GHX_RO_DRY_RUN": "1",
    }
    for argv in ALLOWED_GH_VERBS:
        try:
            process = subprocess.run(
                [str(GHX_RO), *argv],
                capture_output=True,
                text=True,
                timeout=15,
                env=env, check=False,
            )
        except Exception as error:
            failures.append(f"ghx_ro read probe {argv} errored: {error}")
            continue
        if process.returncode != 0:
            failures.append(f"ghx_ro did not allow read {argv} (rc={process.returncode})")

    for argv in REFUSED_GH_VERBS:
        try:
            process = subprocess.run(
                [str(GHX_RO), *argv],
                capture_output=True,
                text=True,
                timeout=15,
                env=env, check=False,
            )
        except Exception as error:
            failures.append(f"ghx_ro refusal probe {argv} errored: {error}")
            continue
        if process.returncode != 3:
            failures.append(f"ghx_ro did not refuse {argv} (rc={process.returncode})")

    return failures
