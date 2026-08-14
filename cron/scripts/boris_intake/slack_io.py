"""Slack writes. Best-effort by design — a failed reaction must never block a review.

Reaction ownership: this module adds the initial :rubbing: and nothing else.
just_do_it_review_reaction_watchdog.py is the single authoritative writer for
every later state (doge_notes / review-no / white_check_mark).
"""
from __future__ import annotations

from typing import Callable

from boris_intake.models import Rejection, ReviewCommand

REASON_TEXT = {
    "identity_not_trusted": (
        "I only take review requests from ylopo.com, avenga.com, or "
        "maverickre.com accounts. Ask Chad if you think this is wrong."),
    "identity_lookup_failed": "I couldn't verify your Slack account just now — try again.",
    "org_not_allowed": "I only review PRs in the Ylopo GitHub org.",
    "pr_not_found": "I couldn't find that PR (or I can't see it).",
    "pr_not_open": "That PR isn't open.",
    "duplicate": "Already reviewed or in progress at that head.",
    "rate_limited": "Rate limited.",
}


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
            pass  # best effort; the watchdogs reconcile reaction state

    def add_reviewing_reaction(self, channel: str, ts: str) -> None:
        self._post("reactions.add", channel=channel, timestamp=ts, name="rubbing")

    def reply_ack(self, cmd: ReviewCommand) -> None:
        """Confirm which PR and which commit was picked up — nothing more.

        Deliberately minimal. The lane id is kanban-internal and means nothing to
        the requester (it stays in the audit log for debugging). The read-only /
        non-blocking policy is already stated in the channel description, the
        pinned instructions, and the footer of every GitHub comment; repeating it
        on every review is noise. The :rubbing: reaction already signals state,
        so this line exists only to disambiguate WHICH PR at WHICH commit — which
        matters when several links are posted at once or the head has moved.
        """
        self._post(
            "chat.postMessage", channel=cmd.channel_id, thread_ts=cmd.message_ts,
            text=f"Boris is reviewing {cmd.slug} @ {cmd.head_sha[:7]}")

    def reply_rejection(self, channel: str, ts: str, rejection: Rejection) -> None:
        if rejection.reason.value == "unknown_flag" and rejection.detail:
            self._post("chat.postMessage", channel=channel, thread_ts=ts,
                       text=rejection.detail)
            return
        base = REASON_TEXT.get(rejection.reason.value, "I can't take that request.")
        detail = f" ({rejection.detail})" if rejection.detail else ""
        self._post("chat.postMessage", channel=channel, thread_ts=ts,
                   text=f"{base}{detail}")
