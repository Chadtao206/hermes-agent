import json
from types import SimpleNamespace

from boris_intake.config import Config
from boris_intake.daemon import Pipeline, normalize
from boris_intake.gate import GateDeps
from boris_intake.models import GateSnapshot, Identity, PRInfo
from boris_intake.slack_io import SlackIO
from boris_intake.store import Store


def pipeline(tmp_path, *, mode="live"):
    config = Config(
        mode=mode,
        channels=("C1",),
        passive_channels=("C1",),
    )
    store = Store(tmp_path / "intake.db")
    slack_calls = []
    deps = GateDeps(
        identity=lambda user_id: Identity(
            user_id, "reviewer@ylopo.com", "T02PND103", False, False, False, False
        ),
        pr_info=lambda owner, repo, number: PRInfo(
            f"{number:040x}", "author", "OPEN"
        ),
        rate_verdict=lambda user_id: "ok",
        now=lambda: 1_000_000.0,
    )

    def slack_call(token, method, **kwargs):
        slack_calls.append((method, kwargs))
        return {"ok": True}

    def capture(command, captured_at):
        return GateSnapshot(
            f"sha256:{command.pr_number:064x}", captured_at, ()
        )

    return (
        Pipeline(
            config,
            deps,
            store,
            SlackIO(slack_call, "token", dry_run=mode != "live"),
            tmp_path / "audit.jsonl",
            capture_gate_snapshot=capture,
        ),
        store,
        slack_calls,
    )


def event(text):
    return normalize({
        "channel": "C1",
        "channel_type": "channel",
        "user": "U1",
        "ts": "1.0",
        "text": text,
    })


def test_message_policy_is_shared_while_pr_requests_are_independent(tmp_path):
    intake, store, slack_calls = pipeline(tmp_path)
    intake.handle(event(
        "https://github.com/Ylopo/one/pull/1 --ignore-ci "
        "https://github.com/Ylopo/two/pull/2"
    ))

    requests = store.requests_for_event("C1", "1.0")
    assert len(requests) == 2
    assert len({request.request_id for request in requests}) == 2
    assert all(request.policy.ignore_ci for request in requests)
    assert slack_calls == []


def test_default_batch_limit_admits_a_real_world_24_pr_message(tmp_path):
    intake, store, slack_calls = pipeline(tmp_path)
    text = " ".join(
        f"https://github.com/Ylopo/repo-{number}/pull/{number}"
        for number in range(1, 25)
    )

    intake.handle(event(text))

    assert len(store.requests_for_event("C1", "1.0")) == 24
    assert slack_calls == []


def test_shadow_intake_does_not_mutate_workflow_or_external_systems(tmp_path):
    intake, store, slack_calls = pipeline(tmp_path, mode="shadow")

    intake.handle(event("https://github.com/Ylopo/one/pull/1"))

    assert store.requests_for_event("C1", "1.0") == ()
    assert store.seen_event("C1", "1.0") is False
    assert slack_calls == []


def test_shadow_replay_audits_gate_snapshot_proposals_without_external_mutation(
    monkeypatch, capsys, tmp_path
):
    from boris_intake import replay

    calls = []
    captures = []
    github_calls = []
    token_path = tmp_path / "replay-token"
    audit_path = tmp_path / "replay-audit.jsonl"

    def slack_call(token, method, **kwargs):
        calls.append((method, kwargs))
        return {
            "ok": True,
            "messages": [{
                "channel": "C1",
                "channel_type": "channel",
                "user": "U1",
                "ts": "1.0",
                "text": "https://github.com/Ylopo/one/pull/1",
            }],
            "response_metadata": {"next_cursor": ""},
        }

    monkeypatch.setattr(replay, "load_config", lambda: Config(
        mode="shadow",
        channels=("C1",),
        passive_channels=("C1",),
        github_token_path=token_path,
    ))
    monkeypatch.setattr(
        replay, "parse_env", lambda path: {"TICKET_HUB_SLACK_BOT_TOKEN": "token"}
    )
    monkeypatch.setattr(replay, "slack_call", slack_call)
    monkeypatch.setattr(
        replay,
        "IdentityResolver",
        lambda call, token: SimpleNamespace(resolve=lambda user_id: Identity(
            user_id, "reviewer@ylopo.com", "T02PND103", False, False, False, False
        )),
    )
    def fetch_pr_info(owner, repo, number, *, token_path):
        github_calls.append((owner, repo, number, token_path))
        return PRInfo("1" * 40, "author", "OPEN")

    monkeypatch.setattr(replay, "fetch_pr", fetch_pr_info)

    def capture(owner, repo, number, head_sha, captured_at, *, token_path):
        captures.append((owner, repo, number, head_sha, captured_at, token_path))
        return GateSnapshot("sha256:" + "a" * 64, captured_at, ())

    monkeypatch.setattr(replay, "fetch_gate_snapshot", capture, raising=False)
    monkeypatch.setattr(replay.sys, "argv", [
        "replay.py",
        "--days", "1",
        "--channel", "C1",
        "--audit-path", str(audit_path),
    ])

    assert replay.main() == 0

    output = capsys.readouterr().out
    assert "PROPOSE Ylopo/one#1" in output
    assert "gate=sha256:" in output
    assert len(captures) == 1
    assert github_calls == [("Ylopo", "one", 1, token_path)]
    assert captures[0][-1] == token_path
    assert [method for method, _ in calls] == ["conversations.history"]
    record = json.loads(audit_path.read_text().splitlines()[0])
    assert record["source"] == "shadow_replay"
    assert record["decision"] == "accept"
    assert record["proposed"] is True
    assert record["requests"] == [{
        "pr": "Ylopo/one#1",
        "head_sha": "1" * 40,
        "gate_fingerprint": "sha256:" + "a" * 64,
    }]
