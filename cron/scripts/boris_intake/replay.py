#!/usr/bin/env python3
"""Replay real channel history through the gate. Read-only; creates nothing.

Preflight for going live without a shadow period: it exercises the real gate
against real messages, real identities, and real GitHub state, but never
dispatches a lane or writes to Slack.
"""
from __future__ import annotations

import argparse
import collections
import sys
import time
from datetime import datetime, timezone, UTC
from pathlib import Path

# APPENDED, never prepended -- `slack_read` lives only under this directory
# and nowhere in the repo, so appending is enough to find it, while
# prepending put the whole DEPLOYED tree ahead of the repo checkout for the
# rest of the process and rebound every sibling package not yet imported.
# See `reconciler.py`'s copy of this note for the run that proved it, and
# `pr_remediation/daemon.py::_bootstrap_sys_path` for the first time this
# exact mistake was made and fixed.
sys.path.append("/Users/ctao/.hermes/scripts")

from slack_read import ENV_PATH, parse_env, slack_call

from boris_intake import audit
from boris_intake.config import load_config
from boris_intake.daemon import normalize
from boris_intake.gate import GateDeps, evaluate
from boris_intake.github import (
    GitHubRetryBudget,
    fetch_gate_snapshot,
    fetch_pr,
)
from boris_intake.identity import IdentityResolver


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--days", type=int, default=30)
    ap.add_argument("--channel", action="append", dest="channels",
                    help="Read history from these channels (repeatable).")
    ap.add_argument("--as-channel", default="",
                    help="Evaluate every message as if posted in this channel. "
                         "Preflight only: it neutralises the channel check so "
                         "identity, org, grammar and PR validation can be "
                         "exercised against real traffic from elsewhere.")
    ap.add_argument("--audit-path", type=Path, default=audit.DEFAULT_PATH)
    args = ap.parse_args()

    cfg = load_config()
    channels = args.channels or list(cfg.channels)
    token = parse_env(ENV_PATH)["TICKET_HUB_SLACK_BOT_TOKEN"]
    resolver = IdentityResolver(slack_call, token)
    deps = GateDeps(
        identity=resolver.resolve,
        pr_info=lambda owner, repo, number, budget: fetch_pr(
            owner, repo, number, token_path=cfg.github_token_path, budget=budget
        ),
        rate_verdict=lambda uid: "ok",    # rate limits are time-dependent; skip
        now=time.time,
    )

    print(f"mode={cfg.mode} orgs={cfg.orgs} domains={cfg.email_domains}")
    print(f"replaying {args.days}d over {channels}\n")

    oldest = time.time() - args.days * 86400
    tally: collections.Counter = collections.Counter()
    requesters: collections.Counter = collections.Counter()
    accepted = 0
    for channel in channels:
        cursor = None
        while True:
            kw = dict(channel=channel, oldest=f"{oldest:.6f}", limit=200)
            if cursor:
                kw["cursor"] = cursor
            resp = slack_call(token, "conversations.history", **kw)
            if not resp.get("ok"):
                print(f"{channel}: ERROR {resp.get('error')}")
                break
            for raw in resp.get("messages", []):
                raw["channel"] = args.as_channel or channel
                ev = normalize(raw)
                if ev is None:
                    continue
                # One retry window per replayed message, exactly as the live
                # pipeline does it -- otherwise a preflight run against a bad
                # GitHub window would report a different rejection mix than the
                # daemon would produce from the same history.
                budget = GitHubRetryBudget()
                result = evaluate(ev, cfg, deps, budget)
                proposals = []
                snapshot_rejections = []
                if result.accepted:
                    captured_at = datetime.fromtimestamp(
                        deps.now(), UTC
                    ).isoformat().replace("+00:00", "Z")
                    for c in result.commands:
                        capture = fetch_gate_snapshot(
                            c.owner,
                            c.repo,
                            c.pr_number,
                            c.head_sha,
                            captured_at,
                            token_path=cfg.github_token_path,
                            budget=budget,
                        )
                        if not capture.captured:
                            reason = (
                                "gate_snapshot_unavailable" if capture.head_moved
                                else "github_unavailable"
                            )
                            tally[reason] += 1
                            snapshot_rejections.append((
                                reason,
                                f"{c.slug} — {capture.detail}" if capture.detail
                                else f"{c.slug} — exact-head Gate Snapshot unavailable",
                            ))
                            print(
                                f"REJECT {c.slug} @ {c.head_sha[:7]} "
                                f"{reason}: {capture.detail}"
                            )
                            continue
                        snapshot = capture.snapshot
                        accepted += 1
                        requesters[c.requester_email] += 1
                        proposals.append({
                            "pr": c.slug,
                            "head_sha": c.head_sha,
                            "gate_fingerprint": snapshot.fingerprint,
                        })
                        print(
                            f"PROPOSE {c.slug} @ {c.head_sha[:7]} "
                            f"gate={snapshot.fingerprint} by {c.requester_email}"
                        )
                for r in result.rejections:
                    tally[r.reason.value] += 1
                audit.write(args.audit_path, {
                    "source": "shadow_replay",
                    "mode": cfg.mode,
                    "channel": ev.channel_id,
                    "ts": ev.ts,
                    "user": ev.user_id,
                    "decision": "accept" if proposals else "reject",
                    "proposed": bool(proposals),
                    "requests": proposals,
                    "reasons": [
                        *[rejection.reason.value for rejection in result.rejections],
                        *[reason for reason, _ in snapshot_rejections],
                    ],
                    "details": [
                        *[rejection.detail for rejection in result.rejections],
                        *[detail for _, detail in snapshot_rejections],
                    ],
                })
            cursor = (resp.get("response_metadata") or {}).get("next_cursor")
            if not cursor:
                break

    print(f"\nproposed commands: {accepted}")
    print("requesters:")
    for email, n in requesters.most_common():
        print(f"  {email}: {n}")
    print("rejections:")
    for reason, n in tally.most_common():
        print(f"  {reason}: {n}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
