from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Dict, List

from .models import SUBTYPE_NO_TRANSCRIPT, SUBTYPE_SUCCESS, TurnResult

# USD per 1M (input, output) tokens. Prefix-matched so dated model ids resolve.
# Estimate only — the transcript carries no cost field.
_PRICES = {
    "claude-opus-4": (15.0, 75.0),
    "claude-sonnet-4": (3.0, 15.0),
    "claude-haiku-4": (1.0, 5.0),
}


def _price_for(model: str) -> tuple[float, float]:
    for key, price in _PRICES.items():
        if model and model.startswith(key):
            return price
    return (0.0, 0.0)


def _read_rows(path: Path) -> List[Dict[str, Any]]:
    rows: List[Dict[str, Any]] = []
    with path.open("r", encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            try:
                rows.append(json.loads(line))
            except json.JSONDecodeError:
                continue
    return rows


def _text_of(message: Dict[str, Any]) -> str:
    parts = []
    for block in message.get("content", []) or []:
        if isinstance(block, dict) and block.get("type") == "text":
            parts.append(block.get("text", ""))
    return "".join(parts)


def parse_transcript(path: Path, *, session_id: str) -> TurnResult:
    path = Path(path)
    if not path.exists():
        return TurnResult("", session_id, 0, 0.0, {}, SUBTYPE_NO_TRANSCRIPT)

    rows = _read_rows(path)
    assistant = [r for r in rows if r.get("type") == "assistant"]

    last_by_req: Dict[str, Dict[str, Any]] = {}
    order: List[str] = []
    last_text = ""
    for i, row in enumerate(assistant):
        rid = row.get("requestId") or f"_norid_{i}"
        if rid not in last_by_req:
            order.append(rid)
        last_by_req[rid] = row
        txt = _text_of(row.get("message", {}) or {})
        if txt:
            last_text = txt

    in_tok = out_tok = 0
    cost = 0.0
    for rid in order:
        msg = last_by_req[rid].get("message", {}) or {}
        usage = msg.get("usage", {}) or {}
        ti = int(usage.get("input_tokens", 0) or 0)
        to = int(usage.get("output_tokens", 0) or 0)
        in_tok += ti
        out_tok += to
        pin, pout = _price_for(msg.get("model", ""))
        cost += (ti * pin + to * pout) / 1_000_000.0

    return TurnResult(
        result=last_text,
        session_id=session_id,
        num_turns=len(order),
        total_cost_usd=round(cost, 6),
        usage={"input_tokens": in_tok, "output_tokens": out_tok},
        subtype=SUBTYPE_SUCCESS,
    )
