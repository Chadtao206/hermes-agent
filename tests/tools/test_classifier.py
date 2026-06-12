from control_center_store import _classify_system_process


def test_realistic_worker_argv_is_worker():
    cmd = ("/Users/ctao/.hermes/hermes-agent/venv/bin/python3 "
           "-m tools.claude_session run --task x --workdir /w")
    assert _classify_system_process(cmd) == "worker"
