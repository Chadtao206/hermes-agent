import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from boris_intake.config import Config
from boris_intake.identity import IdentityResolver, is_trusted
from boris_intake.models import Identity

CFG = Config(mode="live")


def ident(**kw):
    base = dict(user_id="U1", email="a@ylopo.com", team_id="T02PND103",
                is_bot=False, deleted=False, is_restricted=False,
                is_ultra_restricted=False)
    base.update(kw)
    return Identity(**base)


def test_full_member_is_trusted():
    assert is_trusted(ident(), CFG) is True


def test_avenga_multi_channel_guest_is_trusted():
    # Verified 2026-08-05: Avenga engineers are is_restricted=True.
    assert is_trusted(ident(email="o@avenga.com", is_restricted=True), CFG) is True


def test_maverickre_is_trusted():
    assert is_trusted(ident(email="r@maverickre.com"), CFG) is True


def test_single_channel_guest_is_rejected():
    assert is_trusted(ident(is_ultra_restricted=True), CFG) is False


def test_untrusted_domain_rejected():
    assert is_trusted(ident(email="x@gmail.com"), CFG) is False


def test_subdomain_of_trusted_domain_rejected():
    assert is_trusted(ident(email="x@evil.ylopo.com.attacker.net"), CFG) is False


def test_domain_match_is_case_insensitive():
    assert is_trusted(ident(email="X@YLOPO.COM"), CFG) is True


def test_foreign_team_rejected():
    assert is_trusted(ident(team_id="T99"), CFG) is False


def test_bot_rejected():
    assert is_trusted(ident(is_bot=True), CFG) is False


def test_deleted_rejected():
    assert is_trusted(ident(deleted=True), CFG) is False


def test_missing_email_rejected():
    assert is_trusted(ident(email=""), CFG) is False


def test_resolver_caches_and_returns_none_on_failure():
    calls = []

    def fake_call(token, method, **kw):
        calls.append(kw["user"])
        if kw["user"] == "UBAD":
            return {"ok": False, "error": "user_not_found"}
        return {"ok": True, "user": {
            "id": kw["user"], "team_id": "T02PND103", "is_bot": False,
            "deleted": False, "is_restricted": False,
            "is_ultra_restricted": False, "profile": {"email": "a@ylopo.com"},
        }}

    r = IdentityResolver(slack_call=fake_call, token="x")
    assert r.resolve("U1").email == "a@ylopo.com"
    assert r.resolve("U1").email == "a@ylopo.com"
    assert calls == ["U1"]            # second call served from cache
    assert r.resolve("UBAD") is None  # failure is not cached as success
