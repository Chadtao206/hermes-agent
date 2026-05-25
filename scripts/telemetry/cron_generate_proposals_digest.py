#!/usr/bin/env python3
from __future__ import annotations

import hashlib
import json
import os
import sqlite3
import subprocess
import sys
from pathlib import Path

HOME = "/Users/ctao"
HERMES_HOME = "/Users/ctao/.hermes"
TELEMETRY_ROOT = Path(f"{HERMES_HOME}/telemetry")
OUTPUT_DIR = TELEMETRY_ROOT / "proposals"
KANBAN_DB = Path(f"{HERMES_HOME}/kanban.db")
CRON_STATE = Path(f"{HERMES_HOME}/cron/jobs.json")
GENERATOR = Path(f"{HERMES_HOME}/scripts/telemetry/generate_proposals.py")
STATE = Path(f"{HERMES_HOME}/state/self_improvement_proposal_digest.json")


def load_state() -> dict:
    if not STATE.exists():
        return {}
    try:
        return json.loads(STATE.read_text(encoding="utf-8"))
    except Exception:
        return {}


def save_state(payload: dict) -> None:
    STATE.parent.mkdir(parents=True, exist_ok=True)
    STATE.write_text(json.dumps(payload, indent=2, sort_keys=True), encoding="utf-8")


def proposal_table_counts() -> dict[str, int]:
    conn = sqlite3.connect(TELEMETRY_ROOT / "experiments.db")
    try:
        cur = conn.cursor()
        return {
            "proposals": int(cur.execute("SELECT COUNT(*) FROM proposals").fetchone()[0]),
            "proposal_evidence_links": int(cur.execute("SELECT COUNT(*) FROM proposal_evidence_links").fetchone()[0]),
        }
    finally:
        conn.close()


def run_generator() -> dict:
    env = os.environ.copy()
    env["HOME"] = HOME
    env["HERMES_HOME"] = HERMES_HOME
    cmd = [
        sys.executable,
        str(GENERATOR),
        "--telemetry-root",
        str(TELEMETRY_ROOT),
        "--output-dir",
        str(OUTPUT_DIR),
        "--kanban-db",
        str(KANBAN_DB),
        "--cron-state",
        str(CRON_STATE),
        "--dry-run",
    ]
    proc = subprocess.run(cmd, text=True, capture_output=True, env=env, timeout=180)
    if proc.returncode != 0:
        if proc.stdout:
            print(proc.stdout.strip())
        if proc.stderr:
            print(proc.stderr.strip(), file=sys.stderr)
        raise RuntimeError(f"generate_proposals exited {proc.returncode}")
    try:
        return json.loads(proc.stdout)
    except json.JSONDecodeError as exc:
        raise RuntimeError(f"generate_proposals returned non-JSON output: {exc}") from exc


def fingerprint(payload: dict, counts: dict[str, int]) -> str:
    normalized = json.dumps(
        {
            "overall_verdict": payload.get("overall_verdict"),
            "proposal_count": payload.get("proposal_count"),
            "suppressed_count": payload.get("suppressed_count"),
            "proposals": payload.get("proposals"),
            "suppressed": payload.get("suppressed"),
            "db_counts": counts,
        },
        sort_keys=True,
    )
    return hashlib.sha256(normalized.encode("utf-8")).hexdigest()


def entries(
    rows: list[dict],
    *,
    proposal_type: str | None = None,
    owner: str | None = None,
    decision: str | None = None,
) -> list[str]:
    values: list[str] = []
    for row in rows:
        if proposal_type and row.get("proposal_type") != proposal_type:
            continue
        if owner and row.get("owner_profile") != owner:
            continue
        if decision and row.get("decision_requested") != decision:
            continue
        proposal_id = str(row.get("proposal_id") or "").strip()
        title = str(row.get("title") or "").strip()
        if proposal_id and title:
            values.append(f"{proposal_id} ({title})")
        elif proposal_id:
            values.append(proposal_id)
        elif title:
            values.append(title)
    return values


def format_list(items: list[str]) -> str:
    return ", ".join(items) if items else "none"


def build_digest(payload: dict, counts: dict[str, int]) -> str:
    proposals = payload.get("proposals") or []
    suppressed = payload.get("suppressed") or []
    overall_verdict = payload.get("overall_verdict") or "unknown"
    readiness_fix = entries(proposals, proposal_type="readiness_gate_fix")
    deterministic_gap = entries(proposals, proposal_type="telemetry_gap_repair", owner="ops", decision="approve")
    human_gap = entries(proposals, proposal_type="telemetry_gap_repair", decision="discuss")
    experiment_fix = entries(proposals, proposal_type="experiment_not_scoreable_fix")
    suppressed_titles = entries(suppressed)

    lines = [
        "Self-improvement proposal dry-run digest",
        f"Readiness: {overall_verdict} | proposals: {len(proposals)} | suppressed strategic: {len(suppressed)}",
        "Mode: dry-run only; proposal ledger remains unchanged in experiments.db.",
        f"DB counts after run: proposals={counts['proposals']}, proposal_evidence_links={counts['proposal_evidence_links']}",
    ]

    if overall_verdict == "NOT_COMPLETE":
        lines.append("Repair-only mode confirmed: readiness fixes and gap repairs emitted; strategic metric recommendations suppressed.")

    if readiness_fix:
        lines.append(f"Readiness fixes ({len(readiness_fix)}): {format_list(readiness_fix)}")
    if deterministic_gap:
        lines.append(f"Deterministic telemetry repairs ({len(deterministic_gap)}): {format_list(deterministic_gap)}")
    if human_gap:
        lines.append(f"Human/evidence telemetry repairs ({len(human_gap)}): {format_list(human_gap)}")
    if experiment_fix:
        lines.append(f"Experiment scoreability fixes ({len(experiment_fix)}): {format_list(experiment_fix)}")
    if suppressed_titles:
        lines.append(f"Suppressed while NOT_COMPLETE ({len(suppressed_titles)}): {format_list(suppressed_titles)}")

    lines.append(f"Artifacts: {OUTPUT_DIR}")
    return "\n".join(lines)


def main() -> int:
    before = proposal_table_counts()
    payload = run_generator()
    after = proposal_table_counts()
    if after != before:
        print(
            "Dry-run mutated proposal ledger unexpectedly: "
            f"before={json.dumps(before, sort_keys=True)} after={json.dumps(after, sort_keys=True)}",
            file=sys.stderr,
        )
        return 1

    current_fingerprint = fingerprint(payload, after)
    previous = load_state()
    save_state(
        {
            "fingerprint": current_fingerprint,
            "updated_at": payload.get("evaluated_at"),
            "overall_verdict": payload.get("overall_verdict"),
            "proposal_count": payload.get("proposal_count"),
            "suppressed_count": payload.get("suppressed_count"),
        }
    )
    if previous.get("fingerprint") == current_fingerprint:
        return 0

    print(build_digest(payload, after))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
