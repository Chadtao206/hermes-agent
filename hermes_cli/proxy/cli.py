"""CLI handlers for the ``hermes proxy`` subcommand."""

from __future__ import annotations

import asyncio
import logging
import os
import secrets
import subprocess
import sys
from pathlib import Path
from typing import Any

from hermes_cli.proxy.adapters import ADAPTERS, get_adapter
from hermes_cli.proxy.server import (
    AIOHTTP_AVAILABLE,
    DEFAULT_HOST,
    DEFAULT_PORT,
    run_server,
)

logger = logging.getLogger(__name__)


def _print_aiohttp_missing() -> None:
    print(
        "hermes proxy requires aiohttp. Install one of:\n"
        "  pip install 'hermes-agent[messaging]'\n"
        "  pip install aiohttp",
        file=sys.stderr,
    )


def cmd_proxy_start(args: Any) -> int:
    """Run the proxy server in the foreground.

    Returns process exit code (0 on clean shutdown).
    """
    if not AIOHTTP_AVAILABLE:
        _print_aiohttp_missing()
        return 1

    provider = getattr(args, "provider", None) or "nous"
    try:
        adapter = get_adapter(provider)
    except ValueError as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 2

    if not adapter.is_authenticated():
        auth_hint = getattr(adapter, "auth_hint", f"hermes auth add {adapter.name}")
        print(
            f"Not logged into {adapter.display_name}. "
            f"Run `{auth_hint}` first.",
            file=sys.stderr,
        )
        return 2

    host = getattr(args, "host", None) or DEFAULT_HOST
    port = getattr(args, "port", None) or DEFAULT_PORT

    print(
        f"Starting Hermes proxy for {adapter.display_name}\n"
        f"  Listening on:  http://{host}:{port}/v1\n"
        f"  Forwarding to: (resolved per-request from your subscription)\n"
        f"  Use any bearer token in the client — the proxy attaches your real credential.\n"
        f"\n"
        f"Press Ctrl+C to stop.",
        file=sys.stderr,
    )

    try:
        asyncio.run(run_server(adapter, host=host, port=port))
    except KeyboardInterrupt:
        print("\nproxy: stopped", file=sys.stderr)
    except OSError as exc:
        print(f"proxy: failed to bind {host}:{port}: {exc}", file=sys.stderr)
        return 1
    return 0


def cmd_proxy_status(args: Any) -> int:
    """Print the status of each configured upstream adapter."""
    print("Hermes proxy upstream adapters\n")
    for name in sorted(ADAPTERS):
        adapter = get_adapter(name)
        if not adapter.is_authenticated():
            print(f"  [{name:8s}] {adapter.display_name} — not logged in")
            continue
        try:
            cred = adapter.get_credential()
        except Exception as exc:
            print(
                f"  [{name:8s}] {adapter.display_name} — credentials need attention "
                f"({exc})"
            )
            continue
        expires = f" (bearer expires {cred.expires_at})" if cred.expires_at else ""
        print(f"  [{name:8s}] {adapter.display_name} — ready{expires}")
    print(
        "\nStart the proxy with: hermes proxy start [--provider <name>]"
    )
    return 0


def cmd_proxy_list_providers(args: Any) -> int:
    """List available proxy upstream providers."""
    print("Available proxy upstream providers:")
    for name in sorted(ADAPTERS):
        adapter = get_adapter(name)
        print(f"  {name}  — {adapter.display_name}")
    return 0


_PLIST_LABEL = "ai.hermes.codex-proxy"


def build_proxy_plist(*, python_path: str, hermes_home: str, port: int,
                      proxy_token: str) -> str:
    """Generate a crash-safe launchd plist for the codex proxy.

    KeepAlive={SuccessfulExit: false} restarts only on failure (avoids a
    crash-loop if the service ever exits cleanly). HERMES_HOME is pinned to the
    global root so the proxy reads/writes the global openai-codex pool.
    """
    log_dir = Path(hermes_home) / "logs"
    return f"""<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key><string>{_PLIST_LABEL}</string>
  <key>ProgramArguments</key>
  <array>
    <string>{python_path}</string>
    <string>-m</string><string>hermes_cli.main</string>
    <string>proxy</string><string>start</string>
    <string>--provider</string><string>codex</string>
    <string>--port</string><string>{port}</string>
  </array>
  <key>WorkingDirectory</key><string>{hermes_home}</string>
  <key>EnvironmentVariables</key>
  <dict>
    <key>HERMES_HOME</key><string>{hermes_home}</string>
    <key>HERMES_PROXY_TOKEN</key><string>{proxy_token}</string>
    <key>PATH</key><string>/usr/local/bin:/usr/bin:/bin:/opt/homebrew/bin</string>
  </dict>
  <key>LimitLoadToSessionType</key>
  <array>
    <string>Aqua</string>
    <string>Background</string>
  </array>
  <key>RunAtLoad</key><true/>
  <key>KeepAlive</key>
  <dict><key>SuccessfulExit</key><false/></dict>
  <key>StandardOutPath</key><string>{log_dir}/codex-proxy.log</string>
  <key>StandardErrorPath</key><string>{log_dir}/codex-proxy.error.log</string>
</dict>
</plist>
"""


def _global_hermes_root() -> Path:
    from hermes_constants import get_default_hermes_root
    return get_default_hermes_root().resolve()


def _launchd_domain() -> str:
    """Return the launchd user domain for the current user (e.g. ``user/501``)."""
    return f"user/{os.getuid()}"


def cmd_proxy_install(args: Any) -> int:
    """Install the codex proxy as a launchd service (global root only)."""
    from hermes_cli.auth import get_hermes_home
    current = get_hermes_home().resolve()
    global_root = _global_hermes_root()
    if current != global_root:
        print(
            f"Refusing to install: HERMES_HOME is a profile ({current}). "
            f"Run from the global root ({global_root}) so the proxy reads the "
            f"global openai-codex pool.",
            file=sys.stderr,
        )
        return 2
    port = getattr(args, "port", None) or 8645
    # I5: explicit --token argument; env var; generated fallback
    token = getattr(args, "token", None) or os.environ.get("HERMES_PROXY_TOKEN") or secrets.token_urlsafe(24)
    plist = build_proxy_plist(
        python_path=sys.executable, hermes_home=str(global_root),
        port=port, proxy_token=token,
    )
    plist_path = Path.home() / "Library" / "LaunchAgents" / f"{_PLIST_LABEL}.plist"
    plist_path.parent.mkdir(parents=True, exist_ok=True)
    # I1: create the log directory before writing the plist
    (global_root / "logs").mkdir(parents=True, exist_ok=True)
    plist_path.write_text(plist)
    domain = _launchd_domain()
    label = _PLIST_LABEL
    # C1: use bootout/bootstrap (not deprecated load/unload)
    # bootout first is idempotent — ignore failure if not loaded
    subprocess.run(
        ["launchctl", "bootout", f"{domain}/{label}"],
        capture_output=True,
    )
    rc = subprocess.run(
        ["launchctl", "bootstrap", domain, str(plist_path)],
        capture_output=True,
    )
    if rc.returncode != 0:
        # C2: clean up plist on bootstrap failure
        plist_path.unlink(missing_ok=True)
        print(f"launchctl bootstrap failed: {rc.stderr.decode()}", file=sys.stderr)
        return 1
    print(f"Installed {_PLIST_LABEL} on port {port}.")
    print(f"Proxy bearer token (use as the dummy key in clients):\n  {token}")
    # I4: token warning
    print("Save this token — it cannot be retrieved later; reinstalling rotates it.")
    return 0


def cmd_proxy_uninstall(args: Any) -> int:
    plist_path = Path.home() / "Library" / "LaunchAgents" / f"{_PLIST_LABEL}.plist"
    # C1: use bootout (not deprecated unload); ignore "not loaded" errors
    subprocess.run(
        ["launchctl", "bootout", f"{_launchd_domain()}/{_PLIST_LABEL}"],
        capture_output=True,
    )
    if plist_path.exists():
        plist_path.unlink()
    print(f"Uninstalled {_PLIST_LABEL}.")
    return 0


def cmd_proxy(args: Any) -> int:
    """Dispatch ``hermes proxy <subcommand>``."""
    sub = getattr(args, "proxy_command", None)
    if sub == "start":
        return cmd_proxy_start(args)
    if sub == "status":
        return cmd_proxy_status(args)
    if sub in {"providers", "list"}:
        return cmd_proxy_list_providers(args)
    if sub == "install":
        return cmd_proxy_install(args)
    if sub == "uninstall":
        return cmd_proxy_uninstall(args)
    # No subcommand → print short help.
    print(
        "hermes proxy — local OpenAI-compatible proxy that attaches your\n"
        "OAuth-authenticated provider credentials to outbound requests.\n"
        "\n"
        "Subcommands:\n"
        "  hermes proxy start [--provider nous|xai] [--host 127.0.0.1] [--port 8645]\n"
        "      Run the proxy in the foreground.\n"
        "  hermes proxy status\n"
        "      Show which upstream adapters are ready.\n"
        "  hermes proxy providers\n"
        "      List available upstream providers.\n"
        "  hermes proxy install [--port PORT]\n"
        "      Install the codex proxy as a crash-safe launchd service.\n"
        "  hermes proxy uninstall\n"
        "      Uninstall the codex proxy launchd service.\n",
        file=sys.stderr,
    )
    return 0


__all__ = [
    "cmd_proxy",
    "cmd_proxy_start",
    "cmd_proxy_status",
    "cmd_proxy_list_providers",
    "cmd_proxy_install",
    "cmd_proxy_uninstall",
    "build_proxy_plist",
]
