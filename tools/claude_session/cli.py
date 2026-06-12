from __future__ import annotations

import argparse
from typing import Any, Dict, Optional

from .models import SUBTYPE_BUDGET, TurnResult


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="claude_session")
    sub = p.add_subparsers(dest="verb", required=True)

    def caps(sp):
        sp.add_argument("--max-turns", type=int, default=None)
        sp.add_argument("--max-budget-usd", type=float, default=None)
        sp.add_argument("--timeout", type=float, default=600.0)

    s = sub.add_parser("start"); s.add_argument("--name", required=True)
    s.add_argument("--workdir", required=True); s.add_argument("--model", default="opus")
    caps(s)
    sd = sub.add_parser("send"); sd.add_argument("--name", required=True)
    sd.add_argument("--prompt", required=True); sd.add_argument("--wait", action="store_true")
    caps(sd)
    for verb in ("status", "stop"):
        sp = sub.add_parser(verb); sp.add_argument("--name", required=True)
    cap = sub.add_parser("capture"); cap.add_argument("--name", required=True)
    cap.add_argument("--lines", type=int, default=60)
    stp = sub.add_parser("steer"); stp.add_argument("--name", required=True)
    stp.add_argument("--text", required=True)
    sl = sub.add_parser("slash"); sl.add_argument("--name", required=True)
    sl.add_argument("--cmd", required=True)
    sub.add_parser("list")
    sub.add_parser("gc")
    r = sub.add_parser("run"); r.add_argument("--task", required=True)
    r.add_argument("--workdir", required=True); r.add_argument("--model", default="opus")
    r.add_argument("--oneshot", action="store_true")
    r.add_argument("--no-tmux", action="store_true")
    caps(r)
    return p


def cmd_send_result_json(tr: TurnResult, *,
                         max_budget_usd: Optional[float]) -> Dict[str, Any]:
    """Apply the external budget cap (interactive ignores --max-budget-usd).
    Note: cost is an estimate, so this cap is approximate."""
    if max_budget_usd is not None and tr.total_cost_usd > max_budget_usd:
        tr.subtype = SUBTYPE_BUDGET
    return tr.to_dict()


def main(argv=None) -> int:
    args = build_parser().parse_args(argv)
    from . import dispatch   # lazy: keeps unit tests free of tmux deps
    return dispatch.run(args)
