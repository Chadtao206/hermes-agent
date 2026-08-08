"""Resolve and judge the trustworthiness of a Slack sender.

Trust is attribute-based, not a hand-maintained user list, so new hires work
without a config edit. ``is_restricted`` (multi-channel guest) is deliberately
NOT disqualifying: Avenga engineers are full-time but present as guests. Single-
channel guests (``is_ultra_restricted``) remain excluded.
"""
from __future__ import annotations

import time
from typing import Callable

from boris_intake.config import Config
from boris_intake.models import Identity


def is_trusted(identity: Identity, cfg: Config) -> bool:
    if identity.is_bot or identity.deleted or identity.is_ultra_restricted:
        return False
    if identity.team_id != cfg.team_id:
        return False
    email = (identity.email or "").strip().lower()
    if "@" not in email:
        return False
    domain = email.rsplit("@", 1)[1]
    return domain in {d.lower() for d in cfg.email_domains}


class IdentityResolver:
    """users.info with a positive-only TTL cache.

    Failures are never cached: a transient Slack error must not pin a user into
    a denied state for the whole TTL.
    """

    def __init__(self, slack_call: Callable, token: str, ttl_seconds: int = 3600):
        self._call = slack_call
        self._token = token
        self._ttl = ttl_seconds
        self._cache: dict[str, tuple[float, Identity]] = {}

    def resolve(self, user_id: str) -> Identity | None:
        now = time.time()
        hit = self._cache.get(user_id)
        if hit and now - hit[0] < self._ttl:
            return hit[1]
        try:
            resp = self._call(self._token, "users.info", user=user_id)
        except Exception:
            return None
        if not resp.get("ok"):
            return None
        u = resp.get("user") or {}
        identity = Identity(
            user_id=str(u.get("id") or user_id),
            email=str((u.get("profile") or {}).get("email") or ""),
            team_id=str(u.get("team_id") or ""),
            is_bot=bool(u.get("is_bot")),
            deleted=bool(u.get("deleted")),
            is_restricted=bool(u.get("is_restricted")),
            is_ultra_restricted=bool(u.get("is_ultra_restricted")),
        )
        self._cache[user_id] = (now, identity)
        return identity
