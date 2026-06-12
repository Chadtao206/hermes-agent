import json
import shutil
import subprocess
import sys

import pytest

pytestmark = [
    pytest.mark.integration,
    pytest.mark.timeout(0),
    pytest.mark.skipif(
        not (shutil.which("tmux") and shutil.which("claude")),
        reason="requires tmux + authed claude",
    ),
]

PROMPT = "Reply with exactly: hello world. Do nothing else."


def test_helper_emits_consumed_fields_with_real_result():
    out = subprocess.run(
        [sys.executable, "-m", "tools.claude_session", "run",
         "--task", PROMPT, "--workdir", ".", "--oneshot", "--timeout", "150"],
        capture_output=True, text=True, timeout=240,
    )
    helper = json.loads(out.stdout)
    for k in ("type", "subtype", "result", "session_id",
              "num_turns", "total_cost_usd", "usage"):
        assert k in helper, f"missing consumed field: {k}"
    assert helper["type"] == "result"
    assert isinstance(helper["num_turns"], int)
    assert isinstance(helper["total_cost_usd"], (int, float))
    assert isinstance(helper["usage"], dict)
    assert "hello world" in helper["result"].lower()
