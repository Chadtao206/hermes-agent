"""Slack writes. Reactions only — this pipeline never posts messages.

Rationale: the approval itself is already visible on the PR, and these are busy
engineering channels. A threaded reply per approval would add ~9 messages/day of
pure noise. The reaction on the original message is the whole acknowledgement.

Best-effort by design: a failed reaction must never cause a re-approval attempt,
so failures are swallowed and left to the audit log.
"""
from __future__ import annotations

from typing import Callable


class SlackIO:
    def __init__(self, slack_call: Callable, token: str, dry_run: bool = True):
        self._call = slack_call
        self._token = token
        self.dry_run = dry_run
        self.dry_run_log: list[tuple[str, dict]] = []

    def _post(self, method: str, **kw) -> None:
        if self.dry_run:
            self.dry_run_log.append((method, kw))
            return
        try:
            self._call(self._token, method, **kw)
        except Exception:
            pass  # best effort; the audit log is the durable record

    def add_reaction(self, channel: str, ts: str, name: str) -> None:
        if not name:
            return
        self._post("reactions.add", channel=channel, timestamp=ts, name=name)
