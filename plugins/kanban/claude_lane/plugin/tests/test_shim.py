# tests/test_shim.py
import os
import subprocess
import sys

# The installed host shim by default; CLAUDE_LANE_SHIM (set by conftest) points
# this at the source-controlled copy so the same assertions run against the
# artifact BEFORE it is installed (checkout-pinned) and the host copy after.
SHIM = os.environ.get("CLAUDE_LANE_SHIM") or os.path.expanduser(
    "~/.hermes/scripts/hermes-kanban-task")
VENV = sys.executable


def _run(args, env_extra, fake_hermes_out):
    env = dict(os.environ)
    env.update(env_extra)
    # Point the shim at a fake hermes that just echoes its argv to a file.
    env["HERMES_BIN"] = fake_hermes_out
    return subprocess.run(
        [VENV, SHIM] + args,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        env=env, check=False,
    )


def test_rejects_foreign_task_id(tmp_path):
    fake = tmp_path / "fake-hermes"
    fake.write_text("#!/bin/sh\necho \"$@\" >> %s\n" % (tmp_path / "calls.txt"), encoding="utf-8")
    fake.chmod(0o755)
    r = _run(["complete", "t_OTHER", "--summary", "x"],
             {"HERMES_KANBAN_TASK": "t_MINE"}, str(fake))
    assert r.returncode != 0
    assert not (tmp_path / "calls.txt").exists()  # never called hermes


def test_forces_id_and_redacts(tmp_path):
    calls = tmp_path / "calls.txt"
    fake = tmp_path / "fake-hermes"
    fake.write_text(f"#!/bin/sh\necho \"$@\" >> {calls}\n", encoding="utf-8")
    fake.chmod(0o755)
    r = _run(["complete", "--summary", "token sk-ant-oat01-SECRETSECRETSECRET done"],
             {"HERMES_KANBAN_TASK": "t_MINE"}, str(fake))
    assert r.returncode == 0
    out = calls.read_text(encoding="utf-8")
    assert "kanban complete t_MINE --summary" in out
    assert "sk-ant-oat01-SECRETSECRETSECRET" not in out  # redacted


def test_metadata_is_redacted(tmp_path):
    # MAJOR 5: --metadata must be redacted too (was passed verbatim before).
    calls = tmp_path / "calls.txt"
    fake = tmp_path / "fake-hermes"
    fake.write_text(f"#!/bin/sh\necho \"$@\" >> {calls}\n", encoding="utf-8")
    fake.chmod(0o755)
    r = _run(["complete", "--metadata", '{"k":"sk-ant-oat01-SECRETSECRETSECRET"}'],
             {"HERMES_KANBAN_TASK": "t_MINE"}, str(fake))
    assert r.returncode == 0
    out = calls.read_text(encoding="utf-8")
    assert "kanban complete t_MINE --metadata" in out
    assert "sk-ant-oat01-SECRETSECRETSECRET" not in out  # redacted inside JSON


# -- the supervisor's ownership declaration rides on the shim -----------------
def test_heartbeat_forwards_worker_pid(tmp_path):
    """An out-of-process supervisor is the only thing that can name the card's
    owner (the dispatcher's spawn_fn has no usable pid for a tmux lane). If the
    shim drops --worker-pid the whole chain is back to unowned cards — 23 of 44
    supervisor-lane runs on the live board had no owner recorded."""
    calls = tmp_path / "calls.txt"
    fake = tmp_path / "fake-hermes"
    fake.write_text(f"#!/bin/sh\necho \"$@\" >> {calls}\n", encoding="utf-8")
    fake.chmod(0o755)
    r = _run(["heartbeat", "--note", "supervisor alive", "--worker-pid", "4242"],
             {"HERMES_KANBAN_TASK": "t_MINE"}, str(fake))
    assert r.returncode == 0
    out = calls.read_text(encoding="utf-8")
    assert "kanban heartbeat t_MINE" in out
    assert "--worker-pid 4242" in out


def test_heartbeat_refuses_a_non_integer_worker_pid(tmp_path):
    """Nothing else may ride in on the flag, and a bogus value must fail loudly
    rather than silently heartbeat with no ownership."""
    calls = tmp_path / "calls.txt"
    fake = tmp_path / "fake-hermes"
    fake.write_text(f"#!/bin/sh\necho \"$@\" >> {calls}\n", encoding="utf-8")
    fake.chmod(0o755)
    r = _run(["heartbeat", "--worker-pid", "not-a-pid; rm -rf /"],
             {"HERMES_KANBAN_TASK": "t_MINE"}, str(fake))
    assert r.returncode == 2
    assert not calls.exists()


def test_heartbeat_drops_a_non_positive_worker_pid(tmp_path):
    """A pid that cannot own anything is dropped, not forwarded: core would
    read 0/-1 as 'no owner offered' anyway, and forwarding it only invites a
    bogus ownership record."""
    calls = tmp_path / "calls.txt"
    fake = tmp_path / "fake-hermes"
    fake.write_text(f"#!/bin/sh\necho \"$@\" >> {calls}\n", encoding="utf-8")
    fake.chmod(0o755)
    r = _run(["heartbeat", "--worker-pid", "0"], {"HERMES_KANBAN_TASK": "t_MINE"}, str(fake))
    assert r.returncode == 0
    out = calls.read_text(encoding="utf-8")
    assert "kanban heartbeat t_MINE" in out
    assert "--worker-pid" not in out
