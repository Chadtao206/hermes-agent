"""Fail-closed preconditions for entering `live`.

The gate is the only boundary between "a teammate pasted a link" and "a public
approval landed on a PR", so these probes do not merely check that config
parsed — they drive synthetic events through the real gate and assert it says
no. Any probe the gate fails to block keeps the daemon in `shadow`.
"""
from __future__ import annotations

from pathlib import Path

from boris_intake.models import Identity, NormalizedEvent

from jdi_autoapprove.config import Config
from jdi_autoapprove.gate import GateDeps, evaluate
from jdi_autoapprove.models import PRFacts

_GOOD_SHA = "a" * 40
_LINK = "https://github.com/Ylopo/newlead-processor/pull/1"


def _identity(email: str, team_id: str, **kw) -> Identity:
    return Identity(user_id="U1", email=email, team_id=team_id, is_bot=False,
                    deleted=False, is_restricted=False,
                    is_ultra_restricted=kw.get("ultra", False))


def _event(**kw) -> NormalizedEvent:
    base = dict(channel_id="C_ALLOWED", channel_type="channel", user_id="U1",
                ts="1.0", thread_ts=None, text=_LINK, is_bot=False, subtype=None)
    base.update(kw)
    return NormalizedEvent(**base)


def _deps(cfg: Config, identity: Identity | None, facts: PRFacts | None,
          viewer: str = "somebot") -> GateDeps:
    return GateDeps(
        identity=lambda uid: identity,
        pr_facts=lambda o, r, n: facts,
        approved_at=lambda k: None,
        rate_verdict=lambda uid: "ok",
        viewer_login=viewer,
        now=lambda: 0.0,
    )


def run_selftest(cfg: Config, slack_call=None, token: str = "",
                 viewer_login: str = "") -> list[str]:
    failures: list[str] = []

    # 1. Credential present, readable, and correctly permissioned.
    token_path = Path(cfg.github_token_path)
    if not token_path.is_file():
        failures.append(f"github token missing at {token_path}")
    else:
        mode = oct(token_path.stat().st_mode & 0o777)
        if mode != "0o600":
            failures.append(f"github token at {token_path} has mode {mode}, want 0o600")

    # 2. The credential must resolve to an account, or self-approval detection
    #    silently degrades and every one of Chad's own PRs collects a 422.
    if not viewer_login:
        failures.append("could not resolve the GitHub login for this token")

    # 3. Channels must be explicitly configured.
    if not cfg.channels:
        failures.append("no channels configured")

    # 4. The gate must provably refuse each of these.
    probe_cfg = Config(
        mode="live", team_id="T_OK", channels=("C_ALLOWED",),
        email_domains=("ylopo.com",), orgs=("Ylopo",),
    )
    ok_identity = _identity("dev@ylopo.com", "T_OK")
    open_pr = PRFacts(head_sha=_GOOD_SHA, author="someone", state="OPEN",
                      review_decision="REVIEW_REQUIRED")

    probes = [
        ("bot message", _event(is_bot=True), ok_identity, open_pr, "somebot"),
        ("message subtype", _event(subtype="channel_join"), ok_identity, open_pr, "somebot"),
        ("unwatched channel", _event(channel_id="C_OTHER"), ok_identity, open_pr, "somebot"),
        ("no PR link", _event(text="ship it"), ok_identity, open_pr, "somebot"),
        ("untrusted domain", _event(), _identity("x@gmail.com", "T_OK"), open_pr, "somebot"),
        ("foreign workspace", _event(), _identity("dev@ylopo.com", "T_EVIL"), open_pr, "somebot"),
        ("single-channel guest", _event(),
         _identity("dev@ylopo.com", "T_OK", ultra=True), open_pr, "somebot"),
        ("identity lookup failed", _event(), None, open_pr, "somebot"),
        ("lookalike host", _event(text="https://github.com.evil.com/Ylopo/x/pull/1"),
         ok_identity, open_pr, "somebot"),
        ("userinfo smuggling", _event(text="https://github.com@evil.com/Ylopo/x/pull/1"),
         ok_identity, open_pr, "somebot"),
        ("non-Ylopo org", _event(text="https://github.com/Evil/repo/pull/1"),
         ok_identity, open_pr, "somebot"),
        ("PR not found", _event(), ok_identity, None, "somebot"),
        ("PR already merged", _event(), ok_identity,
         PRFacts(_GOOD_SHA, "someone", "MERGED", ""), "somebot"),
        ("self-authored PR", _event(), ok_identity,
         PRFacts(_GOOD_SHA, "Chadtao206", "OPEN", "REVIEW_REQUIRED"), "chadtao206"),
    ]
    for name, event, identity, facts, viewer in probes:
        result = evaluate(event, probe_cfg, _deps(probe_cfg, identity, facts, viewer))
        if result.accepted:
            failures.append(f"gate did NOT block: {name}")

    # 5. And it must still accept the ordinary case — a gate that blocks
    #    everything would pass every probe above while doing nothing useful.
    happy = evaluate(_event(), probe_cfg, _deps(probe_cfg, ok_identity, open_pr))
    if not happy.accepted:
        failures.append("gate rejected the normal accept case: "
                        + ",".join(r.reason.value for r in happy.rejections))

    # 6. Slack preconditions: bot is in every channel, and the emoji exists.
    if slack_call and token:
        for channel in cfg.channels:
            try:
                resp = slack_call(token, "conversations.info", channel=channel)
            except Exception as exc:
                failures.append(f"channel {channel} probe errored: {exc}")
                continue
            if not resp.get("ok"):
                failures.append(f"channel {channel}: {resp.get('error')}")
            elif not (resp.get("channel") or {}).get("is_member"):
                failures.append(f"bot is not a member of {channel}")
        try:
            emoji = slack_call(token, "emoji.list")
            if emoji.get("ok") and cfg.reaction not in (emoji.get("emoji") or {}):
                # Not fatal on its own, but a silently missing reaction means no
                # visible signal that anything happened.
                failures.append(f"reaction :{cfg.reaction}: does not exist in this workspace")
        except Exception as exc:
            failures.append(f"emoji probe errored: {exc}")

    return failures
