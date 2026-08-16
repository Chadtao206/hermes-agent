"""Slack surface for the remediation loop -- two identities, two tokens.

The two channels this service posts to sit in two different Slack
workspaces, and cross-workspace channel membership does not exist: a token
cannot be made a member of a channel that belongs to another team, and no
scope or invite changes that.

* **Review-thread work** -- the re-review post and the `:thinkies:`
  reactions -- uses `review_token` (`TICKET_HUB_SLACK_BOT_TOKEN`, "Son of
  Chad", team `Ylopo`). That identity must own those specifically: Boris's
  own gate (`boris_intake/gate.py::_is_loop_repost`) authorizes a re-review
  by this bot's Slack user id, so posting review-thread work under any other
  identity would make every re-review invisible to the gate, not merely
  differently attributed.
* **Notifications** -- ready and escalation -- use `notify_token`
  (`SLACK_BOT_TOKEN`, the main Hermes bot, team `hermes-gateway-yl`), which
  is the identity already a member of `cfg.notify_channel`
  (`#pr-curator`). Son of Chad is not a member there and cannot become one:
  `conversations.join` from its token against that channel returns
  `channel_not_found`, every time, regardless of scope -- the channel is not
  merely un-joined, it is not visible to that team's token at all. Using
  Son of Chad's token for these would make `chat.postMessage` fail with
  `not_in_channel` on every ready notice and every escalation, and this
  layer's own failure handling turns that into total silence: `_post`
  catches it and returns `None`, so nothing anywhere records that the
  notice never went out. That is the outcome this split exists to prevent.

The re-review post must carry cfg.marker verbatim: boris_intake's gate
admits this bot's messages only when that literal string is present (see
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
    def __init__(self, cfg: RemediationConfig, review_token: str,
                notify_token: str, caller):
        self._cfg = cfg
        self._review_token = review_token
        self._notify_token = notify_token
        self._call = caller

    def _post(self, token: str, channel: str, text: str,
              thread_ts: str | None = None) -> str | None:
        params = {"channel": channel, "text": text}
        if thread_ts:
            params["thread_ts"] = thread_ts
        try:
            response = self._call(token, "chat.postMessage", **params)
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
        # Son of Chad's token: this post's authorization gate keys on that
        # identity's user id, not on which token happens to be able to reach
        # the channel. See the module docstring.
        return self._post(self._review_token, self._cfg.boris_channel, text,
                          thread_ts)

    def add_thinkies(self, channel: str, ts: str) -> None:
        try:
            self._call(self._review_token, "reactions.add", channel=channel,
                       timestamp=ts, name=THINKIES)
        except Exception:
            pass

    def remove_thinkies(self, channel: str, ts: str) -> None:
        try:
            self._call(self._review_token, "reactions.remove", channel=channel,
                       timestamp=ts, name=THINKIES)
        except Exception:
            pass

    def notify_ready(self, verdict: Verdict, rounds: int) -> str | None:
        text = (
            f"✅ {verdict.slug} is ready for human review.\n"
            f"{pr_url(verdict)}\n"
            f"Cleared after {rounds} remediation round(s)."
        )
        # The main Hermes bot's token: it is the identity already a member of
        # notify_channel. Son of Chad cannot reach that channel at all — see
        # the module docstring.
        return self._post(self._notify_token, self._cfg.notify_channel, text)

    def notify_escalation(self, verdict: Verdict, rounds: int, reason: str) -> str | None:
        text = (
            f"⚠️ {verdict.slug} needs you — remediation stopped ({reason}).\n"
            f"{pr_url(verdict)}\n"
            f"Stopped at round {rounds} of {self._cfg.round_cap}."
        )
        return self._post(self._notify_token, self._cfg.notify_channel, text)
