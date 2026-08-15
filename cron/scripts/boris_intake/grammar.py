"""Reduce arbitrary Slack text to a list of PR references and nothing else.

Anchoring on the literal ``https://github.com/`` prefix is what defeats lookalike
hosts (github.com.evil.com), scheme downgrades, and userinfo smuggling
(github.com@evil.com). Anything the pattern does not match is discarded — there
is no fallback parse.
"""
from __future__ import annotations

import re
from dataclasses import dataclass

MAX_PR_NUMBER = 10_000_000

#: Characters that may legitimately precede a URL. Anything else means the
#: "https://" we matched sits inside a longer token (e.g. after "@" userinfo),
#: so the match is discarded.
_SAFE_PRECEDING = set(" \t\n\r([<|\"'`*")

_PR_URL = re.compile(
    r"https://github\.com/"
    r"(?P<owner>[A-Za-z0-9][A-Za-z0-9_.-]{0,38})/"
    r"(?P<repo>[A-Za-z0-9_.-]{1,100})/"
    r"pull/"
    r"(?P<number>\d{1,9})"
    r"(?![0-9])"
)

_FLAG_TOKEN = re.compile(r"(?<!\S)--[A-Za-z][A-Za-z0-9-]*(?=$|\s)")
_KNOWN_FLAGS = frozenset({"--ignore-ci"})


@dataclass(frozen=True)
class PRRef:
    owner: str
    repo: str
    number: int


@dataclass(frozen=True)
class ParsedRequest:
    refs: tuple[PRRef, ...]
    flags: tuple[str, ...]
    unknown_flags: tuple[str, ...]


def extract_pr_refs(text: str) -> list[PRRef]:
    """Distinct PR refs in first-seen order. Never raises."""
    text = text or ""
    refs: list[PRRef] = []
    seen: set[tuple[str, str, int]] = set()
    for m in _PR_URL.finditer(text):
        start = m.start()
        if start > 0 and text[start - 1] not in _SAFE_PRECEDING:
            continue
        number = int(m.group("number"))
        if number <= 0 or number >= MAX_PR_NUMBER:
            continue
        key = (m.group("owner").lower(), m.group("repo").lower(), number)
        if key in seen:
            continue
        seen.add(key)
        refs.append(PRRef(m.group("owner"), m.group("repo"), number))
    return refs


def parse_request(text: str) -> ParsedRequest:
    text = text or ""
    flags: list[str] = []
    unknown: list[str] = []
    seen: set[str] = set()
    for match in _FLAG_TOKEN.finditer(text):
        flag = match.group(0)
        if flag in seen:
            continue
        seen.add(flag)
        if flag in _KNOWN_FLAGS:
            flags.append(flag)
        else:
            unknown.append(flag)
    return ParsedRequest(tuple(extract_pr_refs(text)), tuple(flags), tuple(unknown))


def mentions_bot(text: str, bot_user_id: str) -> bool:
    """True iff the text contains Slack's canonical mention token for the bot."""
    if not bot_user_id:
        return False
    return f"<@{bot_user_id}>" in (text or "")
