"""Slack writes for legacy intake replies and durable lifecycle status messages.

Reaction ownership: this module adds the initial :rubbing: and nothing else.
just_do_it_review_reaction_watchdog.py is the single authoritative writer for
every later state (doge_notes / review-no / white_check_mark).
"""

from __future__ import annotations

from typing import Callable

from boris_intake.models import LifecycleRequest, Rejection, ReviewCommand, SlackStatus

_STATUS_EVENT_TYPE = "boris_review_status_v2"


class SlackLifecycleError(RuntimeError):
    pass


class SlackRootUnavailable(SlackLifecycleError):
    pass


class SlackStatusMissing(SlackLifecycleError):
    pass


REASON_TEXT = {
    "identity_not_trusted": (
        "I only take review requests from ylopo.com, avenga.com, or "
        "maverickre.com accounts. Ask Chad if you think this is wrong."
    ),
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

    def _lifecycle_call(self, method: str, **kw) -> dict:
        if self.dry_run:
            self.dry_run_log.append((method, kw))
            raise SlackLifecycleError("lifecycle writes are disabled in dry-run mode")
        try:
            response = self._call(self._token, method, **kw)
        except Exception as error:
            raise SlackLifecycleError(f"{method} failed") from error
        if not isinstance(response, dict) or not response.get("ok"):
            code = str(
                response.get("error")
                if isinstance(response, dict)
                else "invalid_response"
            )
            if code in {"thread_not_found", "channel_not_found"}:
                raise SlackRootUnavailable(code)
            if method == "chat.update" and code in {
                "message_not_found",
                "cant_update_message",
                "edit_window_closed",
            }:
                raise SlackStatusMissing(code)
            if code == "message_not_found":
                raise SlackRootUnavailable(code)
            raise SlackLifecycleError(code)
        return response

    @staticmethod
    def _metadata(request: LifecycleRequest) -> dict:
        return {
            "event_type": _STATUS_EVENT_TYPE,
            "event_payload": {"request_id": request.request_id},
        }

    def find_status(self, request: LifecycleRequest) -> SlackStatus | None:
        cursor = None
        seen_cursors = set()
        saw_message = False
        while True:
            args = {
                "channel": request.channel_id,
                "ts": request.root_thread_ts,
                "limit": 200,
                "include_all_metadata": True,
            }
            if cursor is not None:
                args["cursor"] = cursor
            response = self._lifecycle_call("conversations.replies", **args)
            messages = response.get("messages")
            if not isinstance(messages, list):
                raise SlackLifecycleError(
                    "conversations.replies returned no message list"
                )
            saw_message = saw_message or bool(messages)
            for message in messages:
                if not isinstance(message, dict):
                    continue
                metadata = message.get("metadata")
                payload = (
                    metadata.get("event_payload")
                    if isinstance(metadata, dict)
                    else None
                )
                if (
                    isinstance(metadata, dict)
                    and metadata.get("event_type") == _STATUS_EVENT_TYPE
                    and isinstance(payload, dict)
                    and payload.get("request_id") == request.request_id
                ):
                    message_ts = str(message.get("ts") or "")
                    if not message_ts:
                        raise SlackLifecycleError(
                            "matched lifecycle status has no timestamp"
                        )
                    return SlackStatus(message_ts, str(message.get("text") or ""))
            response_metadata = response.get("response_metadata")
            next_cursor = (
                str(response_metadata.get("next_cursor") or "")
                if isinstance(response_metadata, dict)
                else ""
            )
            if not next_cursor:
                break
            if next_cursor in seen_cursors:
                raise SlackLifecycleError("conversations.replies repeated a cursor")
            seen_cursors.add(next_cursor)
            cursor = next_cursor
        if not saw_message:
            raise SlackRootUnavailable("thread_not_found")
        return None

    def post_status(self, request: LifecycleRequest, text: str) -> str:
        response = self._lifecycle_call(
            "chat.postMessage",
            channel=request.channel_id,
            thread_ts=request.root_thread_ts,
            text=text,
            metadata=self._metadata(request),
        )
        message_ts = str(response.get("ts") or "")
        if not message_ts:
            raise SlackLifecycleError("chat.postMessage returned no timestamp")
        return message_ts

    def update_status(
        self, request: LifecycleRequest, message_ts: str, text: str
    ) -> None:
        self._lifecycle_call(
            "chat.update",
            channel=request.channel_id,
            ts=message_ts,
            text=text,
            metadata=self._metadata(request),
        )

    def replace_status(self, request: LifecycleRequest, text: str) -> str:
        return self.post_status(request, text)

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
            "chat.postMessage",
            channel=cmd.channel_id,
            thread_ts=cmd.message_ts,
            text=f"Boris is reviewing {cmd.slug} @ {cmd.head_sha[:7]}",
        )

    def reply_rejection(self, channel: str, ts: str, rejection: Rejection) -> None:
        if rejection.reason.value == "unknown_flag" and rejection.detail:
            self._post(
                "chat.postMessage", channel=channel, thread_ts=ts, text=rejection.detail
            )
            return
        base = REASON_TEXT.get(rejection.reason.value, "I can't take that request.")
        detail = f" ({rejection.detail})" if rejection.detail else ""
        self._post(
            "chat.postMessage", channel=channel, thread_ts=ts, text=f"{base}{detail}"
        )
