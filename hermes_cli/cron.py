"""
Cron subcommand for hermes CLI.

Handles standalone cron management commands like list, create, edit,
pause/resume/run/remove, status, and tick.
"""

import json
import sys
from datetime import datetime
from pathlib import Path
from typing import Iterable, List, Optional

PROJECT_ROOT = Path(__file__).parent.parent.resolve()
sys.path.insert(0, str(PROJECT_ROOT))

from hermes_cli.colors import Colors, color


def _normalize_skills(single_skill=None, skills: Optional[Iterable[str]] = None) -> Optional[List[str]]:
    if skills is None:
        if single_skill is None:
            return None
        raw_items = [single_skill]
    else:
        raw_items = list(skills)

    normalized: List[str] = []
    for item in raw_items:
        text = str(item or "").strip()
        if text and text not in normalized:
            normalized.append(text)
    return normalized


def _cron_api(**kwargs):
    from tools.cronjob_tools import cronjob as cronjob_tool

    return json.loads(cronjob_tool(**kwargs))


def _format_health_timestamp(value: Optional[str]) -> str:
    if not value:
        return "never"
    try:
        dt = datetime.fromisoformat(value)
        return dt.strftime("%Y-%m-%d %H:%M")
    except ValueError:
        return str(value)


def _proof_label(health: dict) -> str:
    proof = health.get("proof")
    if proof == "scheduler":
        return color("scheduler", Colors.GREEN)
    if proof == "manual-only":
        return color("manual-only", Colors.YELLOW)
    return color("missing", Colors.RED)


def _severity_label(health: dict) -> str:
    severity = health.get("severity")
    if severity == "healthy":
        return color("healthy", Colors.GREEN)
    if severity == "warning":
        return color("warning", Colors.YELLOW)
    return color("critical", Colors.RED)


def _summarize_flags(flags: list[str]) -> str:
    return ", ".join(flags) if flags else "none"


def cron_health(show_all: bool = False):
    """Show operator-facing cron health with scheduler/manual proof split."""
    from cron.jobs import get_jobs_health

    health_rows = get_jobs_health(include_disabled=show_all)
    if not health_rows:
        print(color("No scheduled jobs.", Colors.DIM))
        return

    scheduler_count = sum(1 for row in health_rows if row.get("proof") == "scheduler")
    manual_only_count = sum(1 for row in health_rows if row.get("proof") == "manual-only")
    missing_count = sum(1 for row in health_rows if row.get("proof") == "missing")
    stale_count = sum(1 for row in health_rows if "stale" in row.get("flags", []))
    receipt_gap_count = sum(1 for row in health_rows if row.get("latest_scheduler_receipt_missing"))

    print()
    print(color("┌─────────────────────────────────────────────────────────────────────────┐", Colors.CYAN))
    print(color("│                           Cron Health                                  │", Colors.CYAN))
    print(color("└─────────────────────────────────────────────────────────────────────────┘", Colors.CYAN))
    print()
    print(
        "  Proof summary: "
        f"scheduler={scheduler_count}  "
        f"manual-only={manual_only_count}  "
        f"missing={missing_count}  "
        f"stale={stale_count}  "
        f"missing-latest-receipt={receipt_gap_count}"
    )
    print("  Manual validation is useful evidence, but it does NOT satisfy scheduler proof.")
    print()

    for row in health_rows:
        receipts = row.get("receipts", {})
        print(
            f"  {color(row['id'], Colors.YELLOW)} "
            f"[{_proof_label(row)} | {_severity_label(row)}]"
        )
        print(f"    Name:                 {row.get('name', '(unnamed)')}")
        print(f"    Schedule:             {row.get('schedule_display', '?')}")
        print(f"    Mode / state:         {row.get('mode', '?')} / {row.get('state', '?')}")
        print(f"    Last scheduler ok:    {_format_health_timestamp(row.get('last_scheduler_success_at'))}")
        print(f"    Last manual receipt:  {_format_health_timestamp(row.get('last_manual_validation_at'))}")
        print(f"    Last scheduler tick:  {_format_health_timestamp(row.get('last_run_at'))}")
        print(f"    Next scheduled run:   {_format_health_timestamp(row.get('next_run_at'))}")
        if row.get("last_status") == "error":
            print(f"    Last scheduler error: {row.get('last_error', 'unknown error')}")
        elif row.get("last_status"):
            print(f"    Last scheduler status:{' ' * 1}{row.get('last_status')}")
        if row.get("last_delivery_error"):
            print(f"    Delivery error:       {row['last_delivery_error']}")
        print(
            f"    Receipts:             scheduler={receipts.get('scheduler', 0)}  "
            f"manual={receipts.get('manual', 0)}  unknown={receipts.get('unknown', 0)}"
        )
        print(f"    Flags:                {_summarize_flags(row.get('flags', []))}")
        print()


def cron_list(show_all: bool = False):
    """List all scheduled jobs."""
    from cron.jobs import get_jobs_health, list_jobs

    jobs = list_jobs(include_disabled=show_all)
    health_by_id = {row["id"]: row for row in get_jobs_health(include_disabled=show_all)}

    if not jobs:
        print(color("No scheduled jobs.", Colors.DIM))
        print(color("Create one with 'hermes cron create ...' or the /cron command in chat.", Colors.DIM))
        return

    print()
    print(color("┌─────────────────────────────────────────────────────────────────────────┐", Colors.CYAN))
    print(color("│                         Scheduled Jobs                                  │", Colors.CYAN))
    print(color("└─────────────────────────────────────────────────────────────────────────┘", Colors.CYAN))
    print()

    for job in jobs:
        job_id = job.get("id", "?")
        name = job.get("name", "(unnamed)")
        schedule = job.get("schedule_display", job.get("schedule", {}).get("value", "?"))
        state = job.get("state", "scheduled" if job.get("enabled", True) else "paused")
        next_run = job.get("next_run_at", "?")

        repeat_info = job.get("repeat", {})
        repeat_times = repeat_info.get("times")
        repeat_completed = repeat_info.get("completed", 0)
        repeat_str = f"{repeat_completed}/{repeat_times}" if repeat_times else "∞"

        deliver = job.get("deliver", ["local"])
        if isinstance(deliver, str):
            deliver = [deliver]
        deliver_str = ", ".join(deliver)

        skills = job.get("skills") or ([job["skill"]] if job.get("skill") else [])
        if state == "paused":
            status = color("[paused]", Colors.YELLOW)
        elif state == "completed":
            status = color("[completed]", Colors.BLUE)
        elif job.get("enabled", True):
            status = color("[active]", Colors.GREEN)
        else:
            status = color("[disabled]", Colors.RED)

        print(f"  {color(job_id, Colors.YELLOW)} {status}")
        print(f"    Name:      {name}")
        print(f"    Schedule:  {schedule}")
        print(f"    Repeat:    {repeat_str}")
        print(f"    Next run:  {next_run}")
        print(f"    Deliver:   {deliver_str}")
        if skills:
            print(f"    Skills:    {', '.join(skills)}")
        script = job.get("script")
        if script:
            print(f"    Script:    {script}")
        if job.get("no_agent"):
            print(f"    Mode:      {color('no-agent', Colors.DIM)} (script stdout delivered directly)")
        workdir = job.get("workdir")
        if workdir:
            print(f"    Workdir:   {workdir}")
        profile = job.get("profile")
        if profile:
            print(f"    Profile:   {profile}")

        # Execution history
        last_status = job.get("last_status")
        if last_status:
            last_run = job.get("last_run_at", "?")
            if last_status == "ok":
                status_display = color("ok", Colors.GREEN)
            else:
                status_display = color(f"{last_status}: {job.get('last_error', '?')}", Colors.RED)
            print(f"    Last run:  {last_run}  {status_display}")

        delivery_err = job.get("last_delivery_error")
        if delivery_err:
            print(f"    {color('⚠ Delivery failed:', Colors.YELLOW)} {delivery_err}")

        health = health_by_id.get(job_id)
        if health:
            print(f"    Proof:     {health.get('proof', '?')}")
            print(f"    Sched ok:  {_format_health_timestamp(health.get('last_scheduler_success_at'))}")
            print(f"    Manual:    {_format_health_timestamp(health.get('last_manual_validation_at'))}")
            if health.get("flags"):
                print(f"    Flags:     {_summarize_flags(health['flags'])}")

        print()

    from hermes_cli.gateway import find_gateway_pids
    if not find_gateway_pids():
        print(color("  ⚠  Gateway is not running — jobs won't fire automatically.", Colors.YELLOW))
        print(color("     Start it with: hermes gateway install", Colors.DIM))
        print(color("                    sudo hermes gateway install --system  # Linux servers", Colors.DIM))
        print()


def cron_tick():
    """Run due jobs once and exit."""
    from cron.scheduler import tick
    tick(verbose=True)


def cron_status():
    """Show cron execution status."""
    from cron.jobs import get_jobs_health, list_jobs
    from hermes_cli.gateway import find_gateway_pids

    print()

    pids = find_gateway_pids()
    if pids:
        print(color("✓ Gateway is running — cron jobs will fire automatically", Colors.GREEN))
        print(f"  PID: {', '.join(map(str, pids))}")
    else:
        print(color("✗ Gateway is not running — cron jobs will NOT fire", Colors.RED))
        print()
        print("  To enable automatic execution:")
        print("    hermes gateway install    # Install as a user service")
        print("    sudo hermes gateway install --system  # Linux servers: boot-time system service")
        print("    hermes gateway            # Or run in foreground")

    print()

    jobs = list_jobs(include_disabled=False)
    health_rows = get_jobs_health(include_disabled=False)
    if jobs:
        next_runs = [j.get("next_run_at") for j in jobs if j.get("next_run_at")]
        print(f"  {len(jobs)} active job(s)")
        if next_runs:
            print(f"  Next run: {min(next_runs)}")
        scheduler_count = sum(1 for row in health_rows if row.get("proof") == "scheduler")
        manual_only_count = sum(1 for row in health_rows if row.get("proof") == "manual-only")
        missing_count = sum(1 for row in health_rows if row.get("proof") == "missing")
        stale_count = sum(1 for row in health_rows if "stale" in row.get("flags", []))
        print(
            "  Health: "
            f"scheduler={scheduler_count}, "
            f"manual-only={manual_only_count}, "
            f"missing={missing_count}, "
            f"stale={stale_count}"
        )
        print("  Detail: hermes cron health")
    else:
        print("  No active jobs")

    print()


def cron_create(args):
    result = _cron_api(
        action="create",
        schedule=args.schedule,
        prompt=args.prompt,
        name=getattr(args, "name", None),
        deliver=getattr(args, "deliver", None),
        repeat=getattr(args, "repeat", None),
        skill=getattr(args, "skill", None),
        skills=_normalize_skills(getattr(args, "skill", None), getattr(args, "skills", None)),
        script=getattr(args, "script", None),
        workdir=getattr(args, "workdir", None),
        profile=getattr(args, "profile", None),
        no_agent=getattr(args, "no_agent", False) or None,
    )
    if not result.get("success"):
        print(color(f"Failed to create job: {result.get('error', 'unknown error')}", Colors.RED))
        return 1
    print(color(f"Created job: {result['job_id']}", Colors.GREEN))
    print(f"  Name: {result['name']}")
    print(f"  Schedule: {result['schedule']}")
    if result.get("skills"):
        print(f"  Skills: {', '.join(result['skills'])}")
    job_data = result.get("job", {})
    if job_data.get("script"):
        print(f"  Script: {job_data['script']}")
    if job_data.get("no_agent"):
        print("  Mode: no-agent (script stdout delivered directly)")
    if job_data.get("workdir"):
        print(f"  Workdir: {job_data['workdir']}")
    if job_data.get("profile"):
        print(f"  Profile: {job_data['profile']}")
    print(f"  Next run: {result['next_run_at']}")
    return 0


def cron_edit(args):
    from cron.jobs import AmbiguousJobReference, resolve_job_ref

    try:
        job = resolve_job_ref(args.job_id)
    except AmbiguousJobReference as exc:
        print(color(str(exc), Colors.RED))
        for m in exc.matches:
            print(f"  {m['id']}  (name: {m.get('name')!r})")
        return 1
    if not job:
        print(color(f"Job not found: {args.job_id}", Colors.RED))
        return 1

    existing_skills = list(job.get("skills") or ([] if not job.get("skill") else [job.get("skill")]))
    replacement_skills = _normalize_skills(getattr(args, "skill", None), getattr(args, "skills", None))
    add_skills = _normalize_skills(None, getattr(args, "add_skills", None)) or []
    remove_skills = set(_normalize_skills(None, getattr(args, "remove_skills", None)) or [])

    final_skills = None
    if getattr(args, "clear_skills", False):
        final_skills = []
    elif replacement_skills is not None:
        final_skills = replacement_skills
    elif add_skills or remove_skills:
        final_skills = [skill for skill in existing_skills if skill not in remove_skills]
        for skill in add_skills:
            if skill not in final_skills:
                final_skills.append(skill)

    result = _cron_api(
        action="update",
        job_id=args.job_id,
        schedule=getattr(args, "schedule", None),
        prompt=getattr(args, "prompt", None),
        name=getattr(args, "name", None),
        deliver=getattr(args, "deliver", None),
        repeat=getattr(args, "repeat", None),
        skills=final_skills,
        script=getattr(args, "script", None),
        workdir=getattr(args, "workdir", None),
        profile=getattr(args, "profile", None),
        no_agent=getattr(args, "no_agent", None),
    )
    if not result.get("success"):
        print(color(f"Failed to update job: {result.get('error', 'unknown error')}", Colors.RED))
        return 1

    updated = result["job"]
    print(color(f"Updated job: {updated['job_id']}", Colors.GREEN))
    print(f"  Name: {updated['name']}")
    print(f"  Schedule: {updated['schedule']}")
    if updated.get("skills"):
        print(f"  Skills: {', '.join(updated['skills'])}")
    else:
        print("  Skills: none")
    if updated.get("script"):
        print(f"  Script: {updated['script']}")
    if updated.get("no_agent"):
        print("  Mode: no-agent (script stdout delivered directly)")
    if updated.get("workdir"):
        print(f"  Workdir: {updated['workdir']}")
    if updated.get("profile"):
        print(f"  Profile: {updated['profile']}")
    return 0


def _job_action(action: str, job_id: str, success_verb: str) -> int:
    result = _cron_api(action=action, job_id=job_id)
    if not result.get("success"):
        print(color(f"Failed to {action} job: {result.get('error', 'unknown error')}", Colors.RED))
        return 1
    job = result.get("job") or result.get("removed_job") or {}
    print(color(f"{success_verb} job: {job.get('name', job_id)} ({job_id})", Colors.GREEN))
    if action in {"resume", "run"} and result.get("job", {}).get("next_run_at"):
        print(f"  Next run: {result['job']['next_run_at']}")
    if action == "run":
        print("  It will run on the next scheduler tick.")
    return 0


def cron_command(args):
    """Handle cron subcommands."""
    subcmd = getattr(args, 'cron_command', None)

    if subcmd is None or subcmd == "list":
        show_all = getattr(args, 'all', False)
        cron_list(show_all)
        return 0

    if subcmd == "status":
        cron_status()
        return 0

    if subcmd == "health":
        show_all = getattr(args, 'all', False)
        cron_health(show_all)
        return 0

    if subcmd == "tick":
        cron_tick()
        return 0

    if subcmd in {"create", "add"}:
        return cron_create(args)

    if subcmd == "edit":
        return cron_edit(args)

    if subcmd == "pause":
        return _job_action("pause", args.job_id, "Paused")

    if subcmd == "resume":
        return _job_action("resume", args.job_id, "Resumed")

    if subcmd == "run":
        return _job_action("run", args.job_id, "Triggered")

    if subcmd in {"remove", "rm", "delete"}:
        return _job_action("remove", args.job_id, "Removed")

    print(f"Unknown cron command: {subcmd}")
    print("Usage: hermes cron [list|create|edit|pause|resume|run|remove|status|health|tick]")
    sys.exit(1)
