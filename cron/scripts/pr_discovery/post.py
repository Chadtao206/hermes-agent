"""The discovery cron's Slack surface: one pull request per message, and
nothing in that message this cron did not compose itself.

WHY THE MESSAGE IS SO SHORT -- this is a security boundary, not a style
choice. `boris_intake`'s discovery door (`gate.py::_is_discovery_post`)
admits a message from this bot when the marker appears anywhere in its text
-- a plain `in` test -- and `grammar.parse_request` then harvests EVERY PR
link the admitted text contains, up to `Limits.max_prs_per_message`. An
admitted message is attributed to `cfg.loop_attributed_user_id` (Chad) and
inherits his trust for every downstream predicate, quota and audit row.

So a single line of text written by someone else -- a PR title, a branch
name, a body excerpt, a commit subject, a check name -- would be enough for
the author of one pull request to plant extra `Ylopo/*` PR URLs of their own
choosing and have this cron launder them into Chad-attributed review requests
against pull requests the readiness filter never selected. The message is
therefore exactly two things: the URL this module builds from the candidate's
own `(owner, repo, number)`, and the marker literal below. Nothing else may
ever be added to it, and `merge_state`/`checks_state` -- the two GitHub-
authored strings a `Candidate` does carry -- are deliberately not reported
here even though they would be useful, because "useful" is how the first
piece of foreign text gets in.

`identity()` is the last gate on that promise. The three identity fields are
GitHub-authored too, and `discover._evaluate` only checks that owner and repo
are non-empty strings -- so a repo name containing a space and a second URL
would compose a message carrying two PR links. Rather than escape or trim
such a value, this module refuses to post it at all, and refuses to render it
into the operator summary (`slug`). The accepted shapes are exactly
`boris_intake.grammar._PR_URL`'s own owner/repo/number bounds, restated here
as literals: this package also runs from its deployed copy under
`~/.hermes/scripts/`, where `boris_intake` is importable but is a separately
deployed artifact, so the two are kept in agreement by tests that import the
real grammar rather than by a runtime import -- the same arrangement
`discover.BANNED_REPO_SUFFIXES` uses against `lanes._EXCLUDED_REPO_SUFFIXES`.
Those tests are `test_identity_agrees_with_the_real_grammar_on_every_bound`
(both sides of every bound) and
`test_an_accepted_identity_survives_the_gates_own_harvester` (the round trip:
whatever this module accepts, the real harvester takes back out of the real
message, and takes back nothing else), in
`tests/cron/pr_discovery/test_post.py`.
"""
from __future__ import annotations

import re

#: The literal the intake gate keys on. `boris_intake` holds its own copy in
#: YAML (`boris_intake.discovery_marker`), and nothing but
#: `main.run_selftest`'s drift check keeps the two equal -- a drift makes
#: every post land in the channel and be silently dropped as NOT_HUMAN.
DISCOVERY_MARKER = "[swept this one up. v1]"

#: The intake channel. A *passive* channel, so a bare PR link is itself the
#: request: no bot mention is needed, and adding one would only be more text.
INTAKE_CHANNEL = "C0BPCKPEFCG"

# Restated from `boris_intake.grammar` (see the module docstring for why they
# are literals rather than an import). `_PR_URL` is anchored on the literal
# `https://github.com/` prefix, so an identity this module accepts is one the
# gate can parse back out of the message it composes.
_OWNER = re.compile(r"\A[A-Za-z0-9][A-Za-z0-9_.-]{0,38}\Z")
_REPO = re.compile(r"\A[A-Za-z0-9_.-]{1,100}\Z")
MAX_PR_NUMBER = 10_000_000

_HEX = re.compile(r"[^0-9a-fA-F]")


def identity(candidate) -> tuple[str, str, int] | None:
    """The candidate's `(owner, repo, number)`, or `None` if any of the three
    is a shape this module refuses to put in a message.

    Fails closed and silently at this layer: the caller reports the refusal
    (`main` counts it as a failed post), because a refusal here is either a
    `gh` response shaped like nothing GitHub produces or an attempt to smuggle
    a second URL, and neither should be repeated verbatim into a channel to
    explain itself.
    """
    owner = getattr(candidate, "owner", None)
    repo = getattr(candidate, "repo", None)
    number = getattr(candidate, "number", None)
    if not isinstance(owner, str) or not _OWNER.match(owner):
        return None
    if not isinstance(repo, str) or not _REPO.match(repo):
        return None
    if not isinstance(number, int) or isinstance(number, bool):
        return None
    if not 0 < number < MAX_PR_NUMBER:
        return None
    return owner, repo, number


def _message(owner: str, repo: str, number: int) -> str:
    """The whole message: the link, then the marker.

    Takes three scalars rather than the candidate on purpose -- composition
    cannot repeat a field it was never handed.

    Private, and deliberately so: it validates nothing, so handing it an
    unchecked `repo` is exactly the laundering `identity()` exists to stop
    (`_message('Ylopo', 'x ' + a_foreign_url, 1)` composes a message carrying
    two PR links). `message_for` is the only supported way in, and the only
    caller. Tests reach it directly to probe composition against the real
    grammar -- which is the whole point of it staying a pure composer.
    """
    return f"https://github.com/{owner}/{repo}/pull/{number}\n{DISCOVERY_MARKER}"


def message_for(candidate) -> str | None:
    """The message for a validated candidate, or `None` if it is unpostable."""
    parts = identity(candidate)
    return None if parts is None else _message(*parts)


def slug(candidate) -> str:
    """`owner/repo#number@head` for the operator-facing run summary.

    The one place a candidate's identity is rendered outside the message
    itself, so it carries the same refusal: an identity `identity()` rejects
    is named as unpostable rather than formatted, and the head SHA is reduced
    to its first seven hex characters, so no value on a candidate can put
    arbitrary text (a URL, a mention, a marker) into a Slack summary either.
    """
    parts = identity(candidate)
    if parts is None:
        return "<unpostable identity>"
    owner, repo, number = parts
    head = getattr(candidate, "head_sha", "")
    head = _HEX.sub("", head)[:7] if isinstance(head, str) else ""
    return f"{owner}/{repo}#{number}@{head}" if head else f"{owner}/{repo}#{number}"


class Poster:
    """Posts one candidate as one top-level message in the intake channel.

    `caller` is `slack_call(token, method, **params)` -- the same convention
    `pr_remediation/daemon.py::slack_caller` wires in. Never raises: a
    transport that explodes, a Slack error, or a response this layer cannot
    read all answer `None`, and `main` treats `None` as "not posted", leaving
    the ledger unwritten so the pull request comes back on the next tick.

    There is deliberately no `thread_ts` parameter. `_is_discovery_post`
    admits only a top-level message; a threaded one would be rejected as
    NOT_HUMAN with no error anywhere, so the ability to post one does not
    exist here rather than being guarded against.
    """

    def __init__(self, token: str, caller, *, channel: str = INTAKE_CHANNEL):
        self._token = token
        self._call = caller
        self._channel = channel

    def post(self, candidate) -> str | None:
        """The message's `ts` if Slack accepted it, `None` otherwise."""
        text = message_for(candidate)
        if text is None:
            return None
        try:
            response = self._call(self._token, "chat.postMessage",
                                  channel=self._channel, text=text)
        except Exception:
            return None
        if not isinstance(response, dict) or not response.get("ok"):
            return None
        ts = response.get("ts")
        # No `ts` means we cannot claim this landed, and the caller records
        # the ledger row only on a claimed success.
        return ts if isinstance(ts, str) and ts else None
