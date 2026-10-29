"""Summarise the Jev shadow logs: watchdog review-state agreement and remediation gate decisions.

Run on demand:  ~/.hermes/hermes-agent/venv/bin/python ~/.hermes/scripts/jev_shadow_report.py [--days N]
"""
from __future__ import annotations

import json
import sqlite3
import sys
import time
from collections import Counter
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

HOME = Path("/Users/ctao/.hermes")
WATCHDOG_LOG = HOME / "state" / "jev_shadow_review_reactions.jsonl"
REMEDIATION_DB = HOME / "state" / "pr_remediation.db"
# Backfilled replays are a calibration corpus, never promotion evidence: they are one frozen
# scoring of old runs, so a GO drawn from them would never have survived a live tick.
REPLAY_GLOB = "jev_replay_review_reactions_*.json"


WATCHDOG_RETIRED = ("RETIRED 2026-09-24, hook removed 2026-10-29: the review-lane Jev shadow is gone from "
                    "the watchdog. It read verdicts from task_runs.metadata, which stopped carrying them on "
                    "2026-09-06; boris_intake's derived.presentation now gives deterministically what Jev was "
                    "asked to infer, and Jev could not judge WHY a review blocks (19 of 22 contract-only "
                    "findings called true blockers). The watchdog section below is frozen history.")


def gate_enabled() -> bool:
    try:
        from pr_remediation import jev_gate

        return jev_gate.enabled()
    except Exception:
        return False


def gate_banner(enabled: bool, notes: list, days: float) -> str:
    """What the remediation gate is actually doing, read from its own notes, never assumed."""
    if not enabled:
        return "remediation gate: OFF (JEV_GATE_SHADOW is not 1 or no API key); nothing is being collected."
    if not notes:
        return (f"remediation gate: enabled but wrote NO notes in the last {days:g} days -- either no "
                f"verdict routed to open_lane or the daemon is not reaching the hook.")
    judged = sum(1 for _, n, _ in notes if n.get("would_open") is not None)
    skipped = sum(1 for _, n, _ in notes if is_skipped(n))
    errors = sum(1 for _, n, _ in notes if is_error(n))
    return (f"remediation gate: collecting (shadow only) -- {judged} judged, {skipped} skipped, "
            f"{errors} API errors in the last {days:g} days.")


def watchdog(since: float) -> None:
    rows = []
    if WATCHDOG_LOG.exists():
        for line in WATCHDOG_LOG.read_text().splitlines():
            try:
                rec = json.loads(line)
            except json.JSONDecodeError:
                continue
            if rec.get("ts", 0) >= since:
                rows.append(rec)
    answered = [r for r in rows if "state" in r.get("jev", {})]
    errors = [r for r in rows if "error" in r.get("jev", {})]
    print(f"review-reaction watchdog: {len(rows)} lines, {len(answered)} answered, {len(errors)} errors")
    pairs = Counter((r["wd"]["state"], r["jev"]["state"]) for r in answered)
    for (wd, jev), n in sorted(pairs.items(), key=lambda kv: -kv[1]):
        flag = "" if wd == jev else "  <- disagree"
        print(f"  watchdog={wd:10} jev={jev:9} {n}{flag}")
    for r in answered:
        if r["wd"]["state"] != r["jev"]["state"]:
            print(f"    task={r['task_id']} run={r.get('run_id', '-')} "
                  f"conf={r['jev']['confidence']:.2f} why={r['wd']['why'][:60]}")
    for r in errors[:5]:
        print(f"    error task={r['task_id']}: {r['jev']['error'][:100]}")


    print(f"review-reaction watchdog RECOMMENDATION: {watchdog_recommendation(answered, errors)}")


# Go/no-go rules live here, in code, so the recheck is the same judgment every time.
MIN_WATCHDOG_LINES = 20
MIN_GATE_OUTCOMES = 10
BLOCKED_AGREEMENT_FLOOR = 0.95
GATE_PRECISION_FLOOR = 0.90
FIX_KINDS = {"repost_rereview_sent", "lane_pushed"}
NO_FIX_KINDS = {"lane_abandoned"}
# An escalation hands the loop to a human for reasons that are mostly not about the fix at all
# (an unwritable GitHub, a review that never completes), so it is its own outcome, not a no-fix.
ESCALATED_KINDS = {"escalation_delivered", "escalation_pending"}
# Before 2026-10-29 a verdict with no issues reached the API with zero questions and came back
# as this client-side refusal; it is the same skip the gate now records as `skipped`.
LEGACY_SKIP_ERROR = "At least one question is required."


def watchdog_recommendation(answered: list, errors: list) -> str:
    if len(answered) < MIN_WATCHDOG_LINES:
        return f"STAY IN SHADOW: only {len(answered)} judged lanes, need {MIN_WATCHDOG_LINES}"
    if errors:
        return f"STAY IN SHADOW: {len(errors)} API errors"
    blocked = [r for r in answered if r["wd"]["state"] == "blocked"]
    agree = sum(1 for r in blocked if r["jev"]["blocker"] >= 0.5)
    invented = [r for r in answered if r["wd"]["state"] == "approved" and r["jev"]["state"] == "blocked"]
    if blocked and agree / len(blocked) < BLOCKED_AGREEMENT_FLOOR:
        return f"STAY IN SHADOW: Jev agrees on only {agree}/{len(blocked)} watchdog-blocked lanes"
    if invented:
        return f"STAY IN SHADOW: Jev called {len(invented)} approved lane(s) blocked"
    return (f"GO: {agree}/{len(blocked)} blocked agreement, no invented blocks; replace the verdict-text "
            f"markers with Jev and keep the GitHub-review-id evidence gate in code")


CRITICAL_STATES = {"blocked"}
# Neither is a verdict Jev is offered or should be asked for: `none` is "no reviewer signal"
# and `quarantine` is a malformed run container the watchdog already resolved in code.
UNSCORABLE_STATES = {"none", "quarantine"}


def load_replays() -> list:
    rows = []
    for path in sorted((HOME / "state").glob(REPLAY_GLOB)):
        try:
            blob = json.loads(path.read_text())
        except (OSError, json.JSONDecodeError):
            continue
        rows += [r for r in blob.values() if "state" in r.get("jev", {})]
    return rows


def calibration(rows: list) -> None:
    """What a confidence threshold would actually buy, per bucket, against the recorded outcome.

    Scored only against runs the watchdog could classify. Its `none` is "no reviewer signal" --
    no opinion, not a verdict -- so counting those as misses understates Jev by 20 points and
    drags the top confidence bucket down with rows that have no ground truth to be right about.
    """
    if not rows:
        print("calibration corpus: none")
        return
    scored = [r for r in rows if r["wd"]["state"] not in UNSCORABLE_STATES]
    unsignalled = [r for r in rows if r["wd"]["state"] in UNSCORABLE_STATES]
    if not scored:
        print(f"calibration corpus: {len(rows)} runs, none with a watchdog signal to score against")
        return
    agree = sum(1 for r in scored if r["wd"]["state"] == r["jev"]["state"])
    print(f"calibration corpus (replays, not promotion evidence): {len(scored)} scorable runs, "
          f"{agree / len(scored):.1%} agreement; {len(unsignalled)} more unscorable "
          f"(no reviewer signal, or a malformed container Jev is no longer asked about)")

    buckets: dict[float, list] = {}
    for r in scored:
        conf = r["jev"].get("confidence", 0.0)
        buckets.setdefault(min(int(conf * 10) / 10, 0.9), []).append(
            (conf, r["wd"]["state"] == r["jev"]["state"]))
    print(f"  {'confidence':>12} {'n':>6} {'accuracy':>9} {'claimed':>8} {'gap':>8}")
    for lo in sorted(buckets):
        vals = buckets[lo]
        acc = sum(hit for _, hit in vals) / len(vals)
        claimed = sum(c for c, _ in vals) / len(vals)
        print(f"  {lo:.1f}-{lo + 0.1:.1f}   {len(vals):>6} {acc:>8.1%} {claimed:>8.2f} {acc - claimed:>+8.1%}")

    by: dict[str, list] = {}
    for r in scored:
        by.setdefault(r["wd"]["state"], []).append(r["wd"]["state"] == r["jev"]["state"])
    print("  per watchdog class: " + ", ".join(
        f"{k}={sum(v)}/{len(v)}" for k, v in sorted(by.items(), key=lambda kv: -len(kv[1]))))

    crit = [r for r in scored if r["wd"]["state"] in CRITICAL_STATES]
    if crit:
        caught = sum(1 for r in crit if r["jev"]["blocker"] >= 0.5)
        print(f"  critical recall (blocked, blocker>=0.5): {caught}/{len(crit)} "
              f"= {caught / len(crit):.1%}")
    if unsignalled:
        found = sum(1 for r in unsignalled if r["jev"]["blocker"] >= 0.5)
        print(f"  unsignalled runs: Jev reads {found}/{len(unsignalled)} as blocking -- coverage the "
              f"verdict-text markers have no answer for, and the only part no replay can score")
    print(f"calibration RECOMMENDATION: {calibration_recommendation(scored)}")


def calibration_recommendation(scored: list) -> str:
    """Reported apart from the promotion gate: the gate asks only whether a blocker is caught,
    this says how much of the rest of the answer to trust."""
    top = [r for r in scored if r["jev"].get("confidence", 0.0) >= 0.9]
    if not top:
        return "no high-confidence answers to score"
    acc = sum(1 for r in top if r["wd"]["state"] == r["jev"]["state"]) / len(top)
    crit = [r for r in scored if r["wd"]["state"] in CRITICAL_STATES]
    recall = sum(1 for r in crit if r["jev"]["blocker"] >= 0.5) / len(crit) if crit else 0.0
    verdict = (f"confidence>=0.9 is right {acc:.0%} of the time over {len(top)} answers; "
               f"critical recall {recall:.0%}")
    if acc < BLOCKED_AGREEMENT_FLOOR:
        return f"{verdict}. Use Jev for the blocker question only, never the four-way state."
    return (f"{verdict}. The four-way state carries a >=0.9 gate too; below it, escalate rather "
            f"than act -- the middle band is still overconfident on thin counts.")


def standing_findings() -> None:
    """The two fixed-corpus results that constrain any GO printed above.

    Both read their own cache, so the weekly digest costs no API calls; neither corpus grows on
    its own, so a rerun would only restate them. Refresh deliberately:
      jev_retired/jev_label_calibration.py --refresh   |   jev_gate_backfill.py
    """
    try:
        cache_path = HOME / "state" / "jev_label_calibration.json"
        baseline = HOME / "state" / "boris_calibration" / "baseline"
        cached = json.loads(cache_path.read_text()) if cache_path.exists() else {}
        cases = [json.loads(f.read_text()) for f in baseline.glob("*.json")]
        pairs = [(c["classification"].replace("baseline:", ""),
                  cached.get(c["case_id"], {}).get("kind")) for c in cases]
        pairs = [(t, j) for t, j in pairs if j]
        if not pairs:
            print("human-label corpus: not scored yet -- run cron/scripts/jev_retired/jev_label_calibration.py")
        else:
            co = [(t, j) for t, j in pairs if t == "contract_only"]
            wrong = sum(1 for t, j in co if j == "true_blocker")
            print(f"human-label corpus ({len(pairs)} scored): Jev calls {wrong}/{len(co)} contract-only "
                  f"findings true blockers.")
            print("  It catches that a report blocks; it cannot judge why. Do not let a GO above be "
                  "read as licence to triage.")
    except Exception as exc:
        print(f"human-label corpus: unavailable ({exc!r:.60})")

    try:
        import jev_gate_backfill as gb
        cached = json.loads(gb.CACHE.read_text()) if gb.CACHE.exists() else {}
        if not cached:
            print("gate backfill: not run -- run jev_gate_backfill.py")
            return
        outcomes = {lid: o for lid, _, o in gb.settled_loops()}
        judged = [(outcomes.get(lid), d) for lid, d in cached.items()
                  if d.get("would_open") is not None and lid in outcomes]
        positives = sum(1 for o, _ in judged if o == "fix")
        print(f"gate backfill: {len(judged)} replayed loops, {positives} of them a fix.")
        if not positives:
            print("  A refuse-everything gate scores 100% on that, so it cannot clear the gate: a fixed "
                  "lane's stored verdict no longer carries the issues it was fixed for.")
    except Exception as exc:
        print(f"gate backfill: unavailable ({exc!r:.60})")


def loop_outcome(con, loop_id: str, after_id: int) -> str | None:
    kinds = [k for (k,) in con.execute(
        "select kind from loop_events where loop_id=? and id>? order by id", (loop_id, after_id))]
    if any(k in FIX_KINDS for k in kinds):
        return "fix"
    if any(k in NO_FIX_KINDS for k in kinds):
        return "no_fix"
    if any(k in ESCALATED_KINDS for k in kinds):
        return "escalated"
    return None


def is_skipped(note: dict) -> bool:
    return bool(note.get("skipped")) or LEGACY_SKIP_ERROR in note.get("error", "")


def is_error(note: dict) -> bool:
    return note.get("would_open") is None and not is_skipped(note)


def load_gate_notes(since: float) -> list:
    if not REMEDIATION_DB.exists():
        return []
    con = sqlite3.connect(f"file:{REMEDIATION_DB}?mode=ro", uri=True)
    try:
        rows = con.execute(
            "select id, loop_id, detail_json from loop_events where kind='jev_gate_shadow' and at >= ? "
            "order by id", (since,)).fetchall()
        return [(lid, json.loads(d), loop_outcome(con, lid, eid)) for eid, lid, d in rows]
    finally:
        con.close()


def top_mechanical(note: dict) -> float | None:
    """The score the mechanical threshold is compared against: the best in-diff issue's."""
    scores = [i["mechanical"] for i in note.get("issues") or [] if i.get("in_diff")]
    return max(scores) if scores else None


def gate_calibration(notes: list) -> list[str]:
    """Outcome by Jev mechanical-probability band, so MECHANICAL_YES can be set from outcomes."""
    outcomes = ("fix", "no_fix", "escalated", "open")
    bands: dict[float, Counter] = {}
    for _, n, outcome in notes:
        score = top_mechanical(n)
        if score is None:
            continue
        lo = min(int(score * 10) / 10, 0.9)
        bands.setdefault(lo, Counter())[outcome or "open"] += 1
    if not bands:
        return ["  calibration: no judged notes with an in-diff issue yet"]
    lines = ["  calibration (best in-diff mechanical score vs outcome; 'open' = not settled yet):",
             f"  {'band':>9} {'n':>4} " + " ".join(f"{o:>9}" for o in outcomes) + f" {'fix rate':>9}"]
    for lo in sorted(bands):
        c = bands[lo]
        settled = c["fix"] + c["no_fix"]
        rate = f"{c['fix'] / settled:.0%}" if settled else "-"
        lines.append(f"  {lo:.1f}-{lo + 0.1:.1f} {sum(c.values()):>4} "
                     + " ".join(f"{c[o]:>9}" for o in outcomes) + f" {rate:>9}")
    return lines


def gate(notes: list) -> None:
    if not REMEDIATION_DB.exists():
        print("remediation gate: no store")
        return
    would = Counter("skipped" if is_skipped(n) else str(n["would_open"]) for _, n, _ in notes)
    print(f"remediation autofix gate: {len(notes)} notes, would_open={dict(would)}")
    for lid, n, outcome in notes:
        if n.get("would_open") is False:
            worst = max(n["issues"], key=lambda i: i["author_decision"] + i["instruction"], default=None)
            print(f"    {lid} would NOT open (lane {outcome or 'still open'}): {len(n['issues'])} issue(s); "
                  f"e.g. {worst and worst['title'][:70]}")
        elif is_error(n):
            print(f"    {lid} error: {n.get('error', '')[:100]}")
    for line in gate_calibration(notes):
        print(line)
    print(f"remediation autofix gate RECOMMENDATION: {gate_recommendation(notes)}")


def gate_recommendation(notes: list) -> str:
    errors = [n for _, n, _ in notes if is_error(n)]
    if errors:
        return f"STAY IN SHADOW: {len(errors)} API errors"
    settled = [(n, o) for _, n, o in notes if o in ("fix", "no_fix") and n.get("would_open") is not None]
    if len(settled) < MIN_GATE_OUTCOMES:
        return f"STAY IN SHADOW: only {len(settled)} lanes with a known outcome, need {MIN_GATE_OUTCOMES}"
    refused = [(n, o) for n, o in settled if n["would_open"] is False]
    right = sum(1 for _, o in refused if o == "no_fix")
    if refused and right / len(refused) < GATE_PRECISION_FLOOR:
        return (f"STAY IN SHADOW: {len(refused) - right}/{len(refused)} lanes the gate would have refused "
                f"went on to push a fix")
    return (f"GO: {right}/{len(refused)} refused lanes produced no fix; route to escalate instead of "
            f"open_lane when nothing is lane-worthy")


def main() -> int:
    days = float(sys.argv[sys.argv.index("--days") + 1]) if "--days" in sys.argv else 7.0
    since = time.time() - days * 86400
    notes = load_gate_notes(since)
    print(WATCHDOG_RETIRED)
    print(gate_banner(gate_enabled(), notes, days) + "\n")
    watchdog(since)
    print()
    calibration(load_replays())
    print()
    gate(notes)
    print()
    standing_findings()
    return 0


if __name__ == "__main__":
    sys.exit(main())
