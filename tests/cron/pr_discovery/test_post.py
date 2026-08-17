"""Tests for the discovery cron's Slack surface.

The first and most important property here is a NEGATIVE one: whatever a
pull request's author writes -- its title, its branch name, its body, its
check names -- must never appear in the message this cron posts. The intake
gate's discovery door (`boris_intake/gate.py::_is_discovery_post`) is a plain
substring test on the marker, and `grammar.parse_request` harvests EVERY PR
link in an admitted message. An admitted message is attributed to Chad
(`cfg.loop_attributed_user_id`) and inherits his trust, so a single line of
author-written text repeated into it would let a PR author plant foreign
`Ylopo/*` PR URLs in their own title and have this cron launder them into
Chad-attributed review requests against pull requests nobody chose.

So `test_hostile_author_text_never_reaches_the_message` and
`test_a_planted_url_in_the_repo_name_is_refused_outright` are the two tests
this file exists for; everything else pins the message so it is actually
admitted once it lands.

Every test runs with no network and no Slack token: the transport is always
a recording double in place of `slack_call`.
"""
from __future__ import annotations

from dataclasses import dataclass

import pytest

from boris_intake.config import Config
from boris_intake.gate import _is_discovery_post
from boris_intake.grammar import PRRef, extract_pr_refs
from boris_intake.models import NormalizedEvent
from pr_discovery.discover import Candidate
from pr_discovery.post import (
    DISCOVERY_MARKER,
    INTAKE_CHANNEL,
    Poster,
    _message,
    identity,
    message_for,
    slug,
)

#: A PR the discovery filter never selected, whose URL an attacker wants
#: repeated into an admitted message.
FOREIGN_URL = "https://github.com/Ylopo/victim/pull/999"

#: Son of Chad, the identity this cron posts under -- the same bot user
#: Boris itself posts as, which is exactly why the gate separates the two on
#: the marker rather than on identity.
BOT = "U0B9TK6B41M"

#: The marker and channel as the DEPLOYMENT has to know them, written out
#: rather than imported from the module under test: an assertion that compares
#: the constant against itself moves with a drift instead of catching it.
MARKER = "[swept this one up. v1]"
CHANNEL = "C0BPCKPEFCG"


def _c(number=1, *, owner="Ylopo", repo="x", head_sha="abc1234def"):
    return Candidate(owner=owner, repo=repo, number=number, head_sha=head_sha,
                     merge_state="CLEAN", checks_state="SUCCESS",
                     has_hold=False)


@dataclass(frozen=True)
class _HostileCandidate:
    """Everything a PR author controls, all of it carrying a foreign PR URL.

    Deliberately a wider shape than `Candidate`: it carries the two fields a
    `Candidate` really does hold and that a poster might innocently "report"
    (`merge_state`, `checks_state`), plus the fields a future author of this
    module might be tempted to add (`title`, `body`, `head_ref`). If any of
    them is ever rendered, the assertions below fail.
    """

    owner: str = "Ylopo"
    repo: str = "x"
    number: int = 1
    head_sha: str = "abc1234def"
    merge_state: str = FOREIGN_URL
    checks_state: str = FOREIGN_URL
    has_hold: bool = False
    title: str = f"fix: see also {FOREIGN_URL}"
    body: str = f"supersedes {FOREIGN_URL}"
    head_ref: str = "chad/victim-999"


class _Transport:
    """Records `slack_call(token, method, **params)` and answers canned."""

    def __init__(self, response=None, *, raises=False):
        self.calls: list[tuple[str, str, dict]] = []
        self._response = response if response is not None else {"ok": True, "ts": "1.0"}
        self._raises = raises

    def __call__(self, token, method, **params):
        self.calls.append((token, method, params))
        if self._raises:
            raise RuntimeError("slack is down")
        return self._response


# ---------------------------------------------------------------------------
# The security property
# ---------------------------------------------------------------------------

def test_hostile_author_text_never_reaches_the_message():
    """No field a PR author can write reaches the posted text."""
    text = message_for(_HostileCandidate())
    assert FOREIGN_URL not in text
    assert "victim" not in text
    assert "supersedes" not in text and "see also" not in text
    assert text == f"https://github.com/Ylopo/x/pull/1\n{DISCOVERY_MARKER}"


def test_the_gates_own_harvester_finds_exactly_the_one_pr_we_chose():
    """`parse_request` harvests every link in an admitted message -- so the
    admitted message must contain exactly one, and it must be ours."""
    text = message_for(_HostileCandidate())
    assert extract_pr_refs(text) == [PRRef("Ylopo", "x", 1)]


def test_hostile_author_text_never_reaches_slack():
    """The same property one layer out, through the real posting path."""
    transport = _Transport()
    Poster("tok", transport).post(_HostileCandidate())
    (_token, _method, params), = transport.calls
    assert FOREIGN_URL not in params["text"]
    assert params["text"] == f"https://github.com/Ylopo/x/pull/1\n{DISCOVERY_MARKER}"


def test_a_planted_url_in_the_repo_name_is_refused_outright():
    """The last gate: an identity that could smuggle a second URL is never
    composed and never sent, rather than being sent HTML-escaped or trimmed."""
    planted = _c(repo=f"x {FOREIGN_URL}")
    assert message_for(planted) is None
    transport = _Transport()
    assert Poster("tok", transport).post(planted) is None
    assert transport.calls == []


def test_a_planted_url_in_the_owner_or_number_is_refused_outright():
    transport = _Transport()
    poster = Poster("tok", transport)
    assert poster.post(_c(owner=f"Ylopo {FOREIGN_URL}")) is None
    assert poster.post(_c(number="1 " + FOREIGN_URL)) is None
    assert poster.post(_c(number=True)) is None
    assert transport.calls == []


def test_a_slash_in_the_owner_or_repo_is_refused():
    """The single character that decides the whole property.

    The refusal tests above feed values containing a space or a colon, which
    every plausible regression still rejects. `/` is the one that does not:
    add it to `_REPO`'s character class -- one character -- and
    `repo="victim/pull/999"` is accepted, composing

        https://github.com/Ylopo/victim/pull/999/pull/1

    out of which the gate's harvester takes `Ylopo/victim#999`, a pull request
    the readiness filter never selected, and never sees ours at all. The same
    character in `_OWNER` does the same thing one path segment earlier. No
    other assertion in this suite moves when either class gains it.
    """
    assert identity(_c(repo="victim/pull/999")) is None
    assert identity(_c(owner="Ylopo/victim/pull/999")) is None
    assert identity(_c(repo="a/b")) is None
    assert identity(_c(owner="a/b")) is None


def test_a_trailing_newline_is_refused_rather_than_anchored_away():
    """`\\Z`, not `$`: `$` also matches just before a trailing newline.

    A repo name ending in one would compose a URL split across two lines. The
    gate still ADMITS that message -- the marker is in it -- and harvests no
    pull request at all from it, which is one `NO_PR_URL` reject reply per
    tick, forever, for a candidate that is never recorded and so returns on
    every tick.
    """
    assert identity(_c(repo="x\n")) is None
    assert identity(_c(owner="Ylopo\n")) is None


def test_a_non_string_identity_field_is_refused_rather_than_coerced():
    """The `isinstance` guards, pinned apart from the patterns they front.

    Dropping one in favour of `str(owner)` reads harmless and is not: `gh`
    answering `null` for a field would then compose a message about the
    perfectly pattern-valid owner `None`, i.e. a request for a review of a
    pull request that does not exist.
    """
    assert identity(_c(owner=None)) is None
    assert identity(_c(repo=None)) is None
    assert identity(_c(owner=123)) is None
    assert identity(_c(repo=1.5)) is None
    assert identity(_c(number=None)) is None
    assert identity(_c(number=1.0)) is None


def test_slug_never_repeats_author_text_either():
    """The operator-facing summary renders identities through this one
    function, so it inherits the same refusal rather than formatting whatever
    it was handed."""
    assert slug(_c()) == "Ylopo/x#1@abc1234"
    assert FOREIGN_URL not in slug(_c(repo=f"x {FOREIGN_URL}"))
    assert FOREIGN_URL not in slug(_c(head_sha=FOREIGN_URL))


# ---------------------------------------------------------------------------
# The message, and the gate it has to satisfy
# ---------------------------------------------------------------------------

def test_the_message_is_the_link_and_the_marker_and_nothing_else():
    assert message_for(_c(42, repo="million-stars")) == (
        "https://github.com/Ylopo/million-stars/pull/42\n"
        "[swept this one up. v1]"
    )


def test_the_composed_message_satisfies_the_intake_gates_discovery_door():
    """Drift guard: the marker literal this module ships is the one the gate
    keys on, and the message shape (top-level, no subtype, this bot) is what
    `_is_discovery_post` admits. Imported from boris_intake here in the test
    rather than at runtime -- this package also runs from its deployed copy,
    where boris_intake's marker lives in YAML, not in an importable constant
    (main.run_selftest guards that half)."""
    event = NormalizedEvent(
        channel_id=INTAKE_CHANNEL, channel_type="channel", user_id=BOT,
        ts="1.0", thread_ts=None, text=message_for(_c()), is_bot=True,
        subtype=None)
    cfg = Config(discovery_marker=MARKER, discovery_bot_user_id=BOT,
                 loop_attributed_user_id="U07E3AHV686")
    assert _is_discovery_post(event, cfg) is True


def test_the_post_is_top_level_in_the_intake_channel():
    """`_is_discovery_post` admits only a top-level message: a thread_ts the
    gate does not recognise makes the post a NOT_HUMAN reject, silently."""
    transport = _Transport()
    Poster("tok", transport).post(_c())
    (token, method, params), = transport.calls
    assert (token, method) == ("tok", "chat.postMessage")
    assert params["channel"] == CHANNEL
    assert "thread_ts" not in params


def test_one_pr_per_message():
    """Never a batch: one Slack message must never be the failure unit for
    several reviews."""
    transport = _Transport()
    poster = Poster("tok", transport)
    poster.post(_c(1))
    poster.post(_c(2))
    assert [params["text"] for _t, _m, params in transport.calls] == [
        f"https://github.com/Ylopo/x/pull/1\n{DISCOVERY_MARKER}",
        f"https://github.com/Ylopo/x/pull/2\n{DISCOVERY_MARKER}",
    ]


# ---------------------------------------------------------------------------
# Failure handling
# ---------------------------------------------------------------------------

def test_a_successful_post_returns_its_timestamp():
    assert Poster("tok", _Transport({"ok": True, "ts": "17.5"})).post(_c()) == "17.5"


def test_a_refused_post_returns_none_rather_than_raising():
    """`ok` is what decides, not the presence of a `ts`: a message Slack
    refused must never be recorded in the ledger as delivered."""
    poster = Poster("tok", _Transport({"ok": False, "error": "not_in_channel",
                                       "ts": "9.9"}))
    assert poster.post(_c()) is None


def test_a_transport_that_raises_returns_none():
    assert Poster("tok", _Transport(raises=True)).post(_c()) is None


def test_an_unrecognised_response_shape_is_a_failure_not_a_success():
    """A `ts` this layer cannot find means we cannot claim the post landed --
    and the caller records the ledger row only on a claimed success."""
    assert Poster("tok", _Transport("sure thing")).post(_c()) is None
    assert Poster("tok", _Transport({"ok": True})).post(_c()) is None
    assert Poster("tok", _Transport({"ok": True, "ts": 17.5})).post(_c()) is None


def test_identity_accepts_what_the_gates_url_grammar_accepts():
    """Parity with `boris_intake.grammar`'s own owner/repo/number bounds: an
    identity this module is willing to post must be one the gate can parse
    back out, or the message is admitted and then finds no PR at all."""
    assert identity(_c()) == ("Ylopo", "x", 1)
    assert identity(_c(number=0)) is None
    assert identity(_c(number=10_000_000)) is None
    assert identity(_c(repo="")) is None
    assert identity(_c(owner="-leading-dash")) is None
    assert identity(_c(owner="a" * 40)) is None


# ---------------------------------------------------------------------------
# The round trip, stated as a property against the real grammar
# ---------------------------------------------------------------------------

#: Identities to state the round-trip property over. Every hostile shape this
#: file already covers, plus -- the point of the list -- the shapes a
#: ONE-CHARACTER widening of `_OWNER` or `_REPO` would newly admit. The
#: property is only interesting on identities `identity()` accepts, so a
#: regression can only be caught if the corpus contains what it would accept.
_ROUND_TRIP_CORPUS = [
    ("Ylopo", "x", 1),
    ("Ylopo", "million-stars", 42),
    ("Ylopo", "a.b_c-d", 9_999_999),
    ("A", "1", 1),
    ("a" * 39, "r" * 100, 1234),
    # `/` in either class: the composed URL then carries a DIFFERENT
    # owner/repo/number than the one this cron chose.
    ("Ylopo", "victim/pull/999", 1),
    ("Ylopo/victim/pull/999", "x", 1),
    ("Ylopo", "x/pull/999", 1),
    ("Ylopo", "victim/pull/999/pull", 1),
    # A trailing newline: what `$` would admit where `\Z` does not.
    ("Ylopo", "x\n", 1),
    ("Ylopo\n", "x", 1),
    # Whitespace and a second URL: already refused, kept so the property is
    # stated over the whole hostile set rather than a subset of it.
    ("Ylopo", f"x {FOREIGN_URL}", 1),
    (f"Ylopo {FOREIGN_URL}", "x", 1),
    ("Ylopo", f"x\n{FOREIGN_URL}", 1),
]


@pytest.mark.parametrize("owner,repo,number", _ROUND_TRIP_CORPUS)
def test_an_accepted_identity_survives_the_gates_own_harvester(owner, repo,
                                                               number):
    """The security property as one statement, against the REAL grammar.

    If this module is willing to post an identity, then the harvester that
    actually runs on the admitted message takes back exactly that identity --
    no MORE (a second, foreign pull request laundered into a Chad-attributed
    review request) and no FEWER (none at all, i.e. a reject reply per tick).

    This is what makes a widened character class impossible to land quietly:
    `repo="victim/pull/999"` accepted here harvests `Ylopo/victim#999`, and
    both halves of the inequality fail at once.
    """
    accepted = identity(_c(number, owner=owner, repo=repo))
    if accepted is None:
        return
    assert accepted == (owner, repo, number)
    assert extract_pr_refs(_message(*accepted)) == [PRRef(owner, repo, number)]


def test_the_round_trip_corpus_is_not_vacuous():
    """The property above is worth nothing if nothing in the corpus is
    accepted -- an `identity()` that refused everything would satisfy it. It
    is stated here rather than as an assertion inside the parametrised test so
    that "how many are admitted" cannot quietly fall to zero."""
    accepted = [(owner, repo, number)
                for owner, repo, number in _ROUND_TRIP_CORPUS
                if identity(_c(number, owner=owner, repo=repo)) is not None]
    assert accepted == [
        ("Ylopo", "x", 1),
        ("Ylopo", "million-stars", 42),
        ("Ylopo", "a.b_c-d", 9_999_999),
        ("A", "1", 1),
        ("a" * 39, "r" * 100, 1234),
    ]


# ---------------------------------------------------------------------------
# Parity with the real grammar's bounds (the test post.py's docstring cites)
# ---------------------------------------------------------------------------

#: Identities straddling every bound the two modules restate: the last value
#: each side accepts, the first it refuses, and one in the middle of the range
#: that a NARROWING of either side would move across the line. `post.py`
#: restates these bounds as literals because it also runs from its deployed
#: copy, where `boris_intake` is a separately deployed artifact -- so a drift
#: on either side is caught here and nowhere else.
_BOUND_CORPUS = [
    ("Ylopo", "x", 1),
    ("a", "r", 1),
    ("a" * 39, "x", 1),                     # longest owner
    ("a" * 40, "x", 1),                     # one over
    ("a" * 20, "x", 1),                     # mid-range: a narrowed owner splits here
    ("Ylopo", "r" * 100, 1),                # longest repo
    ("Ylopo", "r" * 101, 1),                # one over
    ("Ylopo", "r" * 50, 1),                 # mid-range: a narrowed repo splits here
    ("Ylopo", "x", 9_999_999),              # largest number
    ("Ylopo", "x", 10_000_000),             # one over
    ("Ylopo", "x", 5_000),                  # mid-range: a narrowed cap splits here
    ("Ylopo", "x", 0),
    ("-leading-dash", "x", 1),
    ("Ylopo", "-leading-dash-is-fine-for-a-repo", 1),
    ("Ylopo", "", 1),
    ("", "x", 1),
]


def _the_gate_harvests_exactly(owner, repo, number) -> bool:
    """Does the REAL `boris_intake` grammar take exactly this identity back
    out of the REAL message this module composes for it?"""
    return extract_pr_refs(_message(owner, repo, number)) == [
        PRRef(owner, repo, number)]


@pytest.mark.parametrize("owner,repo,number", _BOUND_CORPUS)
def test_identity_agrees_with_the_real_grammar_on_every_bound(owner, repo,
                                                              number):
    """The parity `post.py`'s docstring promises, against the imported
    grammar rather than a restatement of it -- the same arrangement
    `test_suffix_constant_has_not_drifted_from_lanes` uses in
    `test_discover.py`.

    Both directions matter and neither is the security property. If the gate
    narrows and this module does not, every post about a value in the gap is
    admitted and harvests nothing: a reject reply per tick. If this module
    narrows and the gate does not, a legitimate pull request is silently never
    reviewed. Bounds compared through BEHAVIOUR, so a rewritten pattern that
    still admits the same set is not a false alarm.
    """
    assert (identity(_c(number, owner=owner, repo=repo)) is not None) == \
        _the_gate_harvests_exactly(owner, repo, number)


def test_the_bound_corpus_straddles_every_bound():
    """A corpus that landed entirely on one side of the bounds would agree
    with any pair of patterns at all."""
    admitted = [ident for ident in _BOUND_CORPUS
                if _the_gate_harvests_exactly(*ident)]
    refused = [ident for ident in _BOUND_CORPUS
               if not _the_gate_harvests_exactly(*ident)]
    assert admitted == [
        ("Ylopo", "x", 1),
        ("a", "r", 1),
        ("a" * 39, "x", 1),
        ("a" * 20, "x", 1),
        ("Ylopo", "r" * 100, 1),
        ("Ylopo", "r" * 50, 1),
        ("Ylopo", "x", 9_999_999),
        ("Ylopo", "x", 5_000),
        ("Ylopo", "-leading-dash-is-fine-for-a-repo", 1),
    ]
    assert len(refused) == len(_BOUND_CORPUS) - len(admitted)
