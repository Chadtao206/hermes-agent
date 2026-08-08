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

sys.path.insert(0, "/Users/ctao/.hermes/scripts")

from slack_read import ENV_PATH, parse_env, slack_call  # noqa: E402

from boris_intake.config import load_config  # noqa: E402
from boris_intake.daemon import normalize  # noqa: E402
from boris_intake.gate import GateDeps, evaluate  # noqa: E402
from boris_intake.github import fetch_pr  # noqa: E402
from boris_intake.identity import IdentityResolver  # noqa: E402


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
    args = ap.parse_args()

    cfg = load_config()
    channels = args.channels or list(cfg.channels)
    token = parse_env(ENV_PATH)["TICKET_HUB_SLACK_BOT_TOKEN"]
    resolver = IdentityResolver(slack_call, token)
    deps = GateDeps(
        identity=resolver.resolve,
        pr_info=fetch_pr,
        lane_for=lambda k: None,          # replay never dedups against live lanes
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
                result = evaluate(ev, cfg, deps)
                if result.accepted:
                    accepted += len(result.commands)
                    for c in result.commands:
                        requesters[c.requester_email] += 1
                        print(f"ACCEPT {c.slug} @ {c.head_sha[:7]} "
                              f"by {c.requester_email}")
                for r in result.rejections:
                    tally[r.reason.value] += 1
            cursor = (resp.get("response_metadata") or {}).get("next_cursor")
            if not cursor:
                break

    print(f"\naccepted commands: {accepted}")
    print("requesters:")
    for email, n in requesters.most_common():
        print(f"  {email}: {n}")
    print("rejections:")
    for reason, n in tally.most_common():
        print(f"  {reason}: {n}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
