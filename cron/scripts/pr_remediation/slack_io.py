"""Slack surface for the remediation loop, posting as Son of Chad.

The re-review post must carry cfg.marker verbatim: boris_intake's gate admits
this bot's messages only when that literal string is present (see
boris_intake/gate.py::_is_loop_repost). Change it here and re-reviews stop
being admitted.
"""
from __future__ import annotations

from pr_remediation.boris_source import Verdict
from pr_remediation.config import RemediationConfig

THINKIES = "thinkies"


def pr_url(verdict: Verdict) -> str:
    return f"https://github.com/{verdict.owner}/{verdict.repo}/pull/{verdict.pr_number}"


class SlackIO:
    def __init__(self, cfg: RemediationConfig, token: str, caller):
        self._cfg = cfg
        self._token = token
        self._call = caller

    def _post(self, channel: str, text: str, thread_ts: str | None = None) -> str | None:
        params = {"channel": channel, "text": text}
        if thread_ts:
            params["thread_ts"] = thread_ts
        try:
            response = self._call(self._token, "chat.postMessage", **params)
        except Exception:
            return None
        if not isinstance(response, dict):
            return None
        return response.get("ts") if response.get("ok") else None

    def post_rereview(self, verdict: Verdict, thread_ts: str | None,
                      round_number: int) -> str | None:
        # The gate (boris_intake/gate.py::_is_loop_repost) admits this bot's
        # message ONLY as a reply in an existing thread. Posting at top level
        # when thread_ts is falsy would land a message the gate silently
        # rejects as NOT_HUMAN — no error anywhere, the loop just stalls
        # waiting for a verdict that will never come. Refuse instead, so the
        # caller can see None and escalate.
        if not thread_ts:
            return None
        text = (
            f"{self._cfg.marker}\n"
            f"{pr_url(verdict)}\n"
            f"Round {round_number} of {self._cfg.round_cap} — remediation pushed, "
            f"requesting re-review."
        )
        return self._post(self._cfg.boris_channel, text, thread_ts)

    def add_thinkies(self, channel: str, ts: str) -> None:
        try:
            self._call(self._token, "reactions.add", channel=channel,
                       timestamp=ts, name=THINKIES)
        except Exception:
            pass

    def remove_thinkies(self, channel: str, ts: str) -> None:
        try:
            self._call(self._token, "reactions.remove", channel=channel,
                       timestamp=ts, name=THINKIES)
        except Exception:
            pass

    def notify_ready(self, verdict: Verdict, rounds: int) -> str | None:
        text = (
            f"✅ {verdict.slug} is ready for human review.\n"
            f"{pr_url(verdict)}\n"
            f"Cleared after {rounds} remediation round(s)."
        )
        return self._post(self._cfg.notify_channel, text)

    def notify_escalation(self, verdict: Verdict, rounds: int, reason: str) -> str | None:
        text = (
            f"⚠️ {verdict.slug} needs you — remediation stopped ({reason}).\n"
            f"{pr_url(verdict)}\n"
            f"Stopped at round {rounds} of {self._cfg.round_cap}."
        )
        return self._post(self._cfg.notify_channel, text)
