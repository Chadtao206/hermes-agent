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

Both re-review posts (`post_rereview` and `repost_rereview`) must carry
cfg.marker verbatim: boris_intake's gate admits this bot's messages only
when that literal string is present (see
boris_intake/gate.py::_is_loop_repost). Change it in either one and that
path's re-reviews stop being admitted.
"""
from __future__ import annotations

from pr_remediation.boris_source import Verdict
from pr_remediation.config import RemediationConfig

THINKIES = "thinkies"


def pr_url(verdict: Verdict) -> str:
    return f"https://github.com/{verdict.owner}/{verdict.repo}/pull/{verdict.pr_number}"


def _thread_notice(unresolved_threads: list[dict] | None) -> str | None:
    """The line to append to a ready notice about open bot review threads,
    or None when nothing should be added.

    Three cases, and they must stay visibly distinct in the notice itself
    (not just in this function's return type), because a human reading Slack
    sees only the text: `None` in means GitHub's answer cannot be trusted to
    be both correct and complete -- the GraphQL read failed outright, came
    back in a shape this build does not trust, or `GitHubIO.unresolved_bot_threads`
    paginated as far as it is willing to and GitHub still reported more
    pages remaining. All three collapse to the same `None` and the same
    sentence here deliberately: a human deciding whether to go check GitHub
    by hand needs to know the list in front of them is not the whole truth,
    not which of those reasons produced that. That is reported as its own
    sentence -- never silence, which would be indistinguishable from a
    verified-clean PR and would recreate the false all-clear this whole
    change exists to remove. `[]` in means the read succeeded and nothing is
    outstanding, and returns `None` out (no line added) so a clean PR's
    notice reads exactly as it did before this change. A non-empty list
    names the count and the files, because that is what lets a human decide
    whether to act without opening the PR.

    `isOutdated` threads are counted, not dropped. Codex and CodeQL mark a
    thread outdated when the code it points at has since changed, which
    makes it *usually* less actionable than a current one -- but "usually"
    is doing real work in that sentence: outdated does not mean resolved,
    nobody has dismissed it, and an outdated comment can still be pointing at
    a real problem the diff moved rather than fixed. Dropping it silently
    would repeat this exact incident's shape (a real, still-open concern the
    notice doesn't mention) for the sake of tidiness. Surfacing the split
    instead -- the total, and how many of those are outdated -- gives a human
    the same triage signal without discarding anything: current threads are
    presumptively the ones to check first, and a nonzero outdated count is
    itself informative (it says some of this PR's history moved past a
    review comment, which is worth knowing even if that comment no longer
    applies verbatim).
    """
    if unresolved_threads is None:
        return "⚠️ Could not verify all unresolved bot review threads — verify manually."
    if not unresolved_threads:
        return None
    outdated = sum(1 for t in unresolved_threads if t.get("outdated"))
    paths = ", ".join(sorted({str(t.get("path")) for t in unresolved_threads}))
    plural = "" if len(unresolved_threads) == 1 else "s"
    outdated_note = f" ({outdated} outdated)" if outdated else ""
    return (f"⚠️ {len(unresolved_threads)} unresolved bot review thread{plural}"
            f"{outdated_note} on: {paths}")


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

    def _post_thread_reply(self, text: str, thread_ts: str | None) -> str | None:
        """Post `text` as a reply in the review thread, under Son of Chad's identity.

        Shared by post_rereview and repost_rereview: both are re-review posts
        and both must satisfy the same gate, so the guard and the identity
        live here once rather than being kept in sync by hand in two places.
        """
        # The gate (boris_intake/gate.py::_is_loop_repost) admits this bot's
        # message ONLY as a reply in an existing thread. Posting at top level
        # when thread_ts is falsy would land a message the gate silently
        # rejects as NOT_HUMAN — no error anywhere, the loop just stalls
        # waiting for a verdict that will never come. Refuse instead, so the
        # caller can see None and escalate.
        if not thread_ts:
            return None
        # Son of Chad's token: this post's authorization gate keys on that
        # identity's user id, not on which token happens to be able to reach
        # the channel. See the module docstring.
        return self._post(self._review_token, self._cfg.boris_channel, text,
                          thread_ts)

    def post_rereview(self, verdict: Verdict, thread_ts: str | None,
                      round_number: int) -> str | None:
        """Ask Boris to look again after a remediation lane pushed a fix.

        `round_number` is the round that just finished: a lane genuinely ran
        and moved the head, so "remediation pushed" is a true claim here.
        Contrast `repost_rereview`, which posts for a path where neither is
        true -- the two messages must stay distinct, or a human scanning the
        thread cannot tell which of the two actually happened.
        """
        text = (
            f"{self._cfg.marker}\n"
            f"{pr_url(verdict)}\n"
            f"Round {round_number} of {self._cfg.round_cap} — remediation pushed, "
            f"requesting re-review."
        )
        return self._post_thread_reply(text, thread_ts)

    def repost_rereview(self, verdict: Verdict, thread_ts: str | None) -> str | None:
        """Ask Boris to look again after a system-origin incomplete verdict.

        Nothing was remediated and no round was spent here -- the previous
        review simply never reached a verdict on this head (it was
        superseded, or the head could not be read). The text says exactly
        that: no round number (there is none to truthfully report) and no
        claim that anything was pushed. See `post_rereview` for the sibling
        that reports a genuine push.
        """
        text = (
            f"{self._cfg.marker}\n"
            f"{pr_url(verdict)}\n"
            f"Previous review didn't reach a verdict on this head — "
            f"re-requesting review. No remediation round was spent."
        )
        return self._post_thread_reply(text, thread_ts)

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

    def notify_ready(self, verdict: Verdict, rounds: int,
                     unresolved_threads: list[dict] | None) -> str | None:
        """Tell a human the loop closed ready, and what GitHub said about
        open bot review threads while it did.

        `unresolved_threads` carries GitHubIO.unresolved_bot_threads()'s own
        contract verbatim, unmodified by anything in between: None means the
        GraphQL read could not be trusted to be complete (a query failure, a
        shape this build does not recognise, or pagination hitting its page
        bound while GitHub still reported more threads to read), and an
        empty list means the read succeeded and found nothing outstanding. The two must never be
        conflated -- folding None into "nothing to report" is exactly the
        false all-clear this parameter exists to prevent, indistinguishable
        from a genuinely clean PR to anyone reading the notice. So None gets
        its own sentence saying the check did not run, an empty list adds no
        line at all (the notice this replaces said nothing here either, and
        a clean PR should read exactly as it always has), and a non-empty
        list names the count and the files. None of the three ever change
        whether this posts or what channel it posts to.
        """
        text = (
            f"✅ {verdict.slug} is ready for human review.\n"
            f"{pr_url(verdict)}\n"
            f"Cleared after {rounds} remediation round(s)."
        )
        thread_line = _thread_notice(unresolved_threads)
        if thread_line:
            text += f"\n{thread_line}"
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
