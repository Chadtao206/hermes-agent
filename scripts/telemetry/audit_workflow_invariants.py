#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import os
import sqlite3
import stat
from pathlib import Path
from typing import Any

try:
    import yaml
except Exception:  # pragma: no cover - operator environment guard
    yaml = None

HOME = Path('/Users/ctao')
HERMES_HOME = HOME / '.hermes'
PROFILES = ('default', 'engineer', 'researcher', 'reviewer', 'ops', 'designer')
SPECIALIST_ATLASSIAN_PROFILES = ('engineer', 'researcher', 'reviewer', 'ops')
ACTIVE_GOVERNANCE_JOBS = {
    'daily-ops-digest',
    'weekly-ops-digest',
}
SUPERSEDED_GOVERNANCE_JOBS = {
    'daily-telemetry-kanban-sync',
    'daily-telemetry-metric-aggregation',
    'daily-routing-workflow-audit',
    'weekly-orchestration-workflow-report',
    'weekly-morning-signal-quality-review',
}
PAUSED_REDUNDANT_JOBS = {
    'weekly-cross-profile-memory-review',
    'daily-cross-profile-memory-audit',
    'weekly-bench-activation-audit',
    'weekly-experiment-scorecard',
}


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description='Audit Hermes orchestration workflow invariants without brittle exact toolset/profile baselines.'
    )
    parser.add_argument('--hermes-home', default=str(HERMES_HOME))
    parser.add_argument('--json', action='store_true', help='Emit machine-readable JSON')
    return parser.parse_args()


def cfg_path(root: Path, profile: str) -> Path:
    return root / 'config.yaml' if profile == 'default' else root / 'profiles' / profile / 'config.yaml'


def load_yaml(path: Path) -> dict[str, Any]:
    if yaml is None:
        raise RuntimeError('PyYAML unavailable; cannot parse config.yaml')
    if not path.exists():
        return {}
    return yaml.safe_load(path.read_text(encoding='utf-8')) or {}


def disabled_skills(root: Path, profile: str) -> set[str]:
    cfg = load_yaml(cfg_path(root, profile))
    return set(((cfg.get('skills') or {}).get('disabled') or []))


def profile_metrics_latest(root: Path) -> tuple[set[str], str | None]:
    db = root / 'telemetry' / 'events.db'
    if not db.exists():
        return set(), None
    conn = sqlite3.connect(db)
    try:
        day = conn.execute('SELECT MAX(date) FROM profile_metrics_daily').fetchone()[0]
        if not day:
            return set(), None
        rows = conn.execute('SELECT profile FROM profile_metrics_daily WHERE date = ?', (day,)).fetchall()
        return {str(row[0]) for row in rows}, str(day)
    finally:
        conn.close()


def cron_jobs(root: Path) -> dict[str, dict[str, Any]]:
    path = root / 'cron' / 'jobs.json'
    if not path.exists():
        return {}
    payload = json.loads(path.read_text(encoding='utf-8'))
    return {str(job.get('name')): job for job in payload.get('jobs', []) if job.get('name')}


def add_check(checks: list[dict[str, Any]], name: str, passed: bool, detail: str, *, evidence: Any = None) -> None:
    checks.append({'name': name, 'pass': bool(passed), 'detail': detail, 'evidence': evidence})


def audit(root: Path) -> dict[str, Any]:
    checks: list[dict[str, Any]] = []

    launcher = root / 'scripts' / 'jensen-neutral'
    add_check(
        checks,
        'neutral_jensen_launcher',
        launcher.exists() and os.access(launcher, os.X_OK),
        'Jensen neutral launcher exists and is executable.',
        evidence=str(launcher),
    )
    default_cfg = load_yaml(cfg_path(root, 'default'))
    add_check(
        checks,
        'terminal_cwd_not_globally_forced',
        (default_cfg.get('terminal') or {}).get('cwd') == '.',
        'Default terminal.cwd remains repo/session-relative; neutral launch is opt-in via launcher.',
        evidence=(default_cfg.get('terminal') or {}).get('cwd'),
    )

    default_atl = ((default_cfg.get('mcp_servers') or {}).get('atlassian') or {})
    default_include = (((default_atl.get('tools') or {}).get('include')) or [])
    for profile in SPECIALIST_ATLASSIAN_PROFILES:
        cfg = load_yaml(cfg_path(root, profile))
        tools = ((((cfg.get('mcp_servers') or {}).get('atlassian') or {}).get('tools') or {}))
        include = tools.get('include') or []
        add_check(
            checks,
            f'atlassian_allowlist_{profile}',
            bool(default_include) and include == default_include and tools.get('resources') is False and tools.get('prompts') is False,
            f'{profile} Atlassian MCP mirrors Jensen allowlist and disables resources/prompts.',
            evidence={'include_count': len(include), 'resources': tools.get('resources'), 'prompts': tools.get('prompts')},
        )

    expected_skill_state = {
        'default': {'baoyu-article-illustrator': True, 'kanban-codex-lane': True},
        'engineer': {'baoyu-article-illustrator': True, 'kanban-codex-lane': False},
        'researcher': {'baoyu-article-illustrator': True, 'kanban-codex-lane': True},
        'reviewer': {'baoyu-article-illustrator': True, 'kanban-codex-lane': True},
        'ops': {'baoyu-article-illustrator': True, 'kanban-codex-lane': True},
        'designer': {'baoyu-article-illustrator': False, 'kanban-codex-lane': True},
    }
    for profile, skills in expected_skill_state.items():
        disabled = disabled_skills(root, profile)
        for skill, should_be_disabled in skills.items():
            add_check(
                checks,
                f'skill_scope_{profile}_{skill}',
                (skill in disabled) == should_be_disabled,
                f'{skill} disabled={should_be_disabled} for {profile}.',
                evidence={'disabled': skill in disabled},
            )

    jobs = cron_jobs(root)
    for name in ACTIVE_GOVERNANCE_JOBS:
        job = jobs.get(name)
        add_check(
            checks,
            f'cron_consolidated_enabled_{name}',
            bool(job and job.get('enabled') is True),
            f'{name} remains enabled as part of the consolidated governance pipeline.',
            evidence={'enabled': job.get('enabled') if job else None, 'toolsets': job.get('enabled_toolsets') if job else None},
        )
    for name in SUPERSEDED_GOVERNANCE_JOBS:
        job = jobs.get(name)
        add_check(
            checks,
            f'cron_superseded_removed_or_disabled_{name}',
            job is None or job.get('enabled') is False,
            f'{name} is absent or disabled because daily/weekly ops digests replaced it.',
            evidence={'present': job is not None, 'enabled': job.get('enabled') if job else None},
        )
    for name in PAUSED_REDUNDANT_JOBS:
        job = jobs.get(name)
        # Superseded governance jobs may be either paused during a proving
        # window or fully removed after cleanup. Treat absence as healthy;
        # otherwise the daily routing audit keeps re-reporting successful
        # cleanup as a governance regression.
        add_check(
            checks,
            f'cron_redundant_removed_or_disabled_{name}',
            job is None or job.get('enabled') is False,
            f'{name} is absent or disabled so governance reporting remains consolidated.',
            evidence={'present': job is not None, 'enabled': job.get('enabled') if job else None},
        )
    morning = jobs.get('weekly-morning-signal-quality-review') or {}
    morning_ok = (
        not morning
        or morning.get('enabled') is False
        or morning.get('enabled_toolsets') == ['terminal', 'file']
    )
    add_check(
        checks,
        'morning_signal_review_removed_or_scoped',
        morning_ok,
        'Morning signal quality review is removed/disabled or scoped to terminal,file.',
        evidence={'present': bool(morning), 'enabled': morning.get('enabled') if morning else None, 'toolsets': morning.get('enabled_toolsets') if morning else None},
    )

    latest_profiles, latest_day = profile_metrics_latest(root)
    missing_profiles = set(PROFILES) - latest_profiles if latest_profiles else set(PROFILES)
    add_check(
        checks,
        'profile_metrics_include_current_bench',
        not missing_profiles,
        'Latest profile metrics include all six active profiles, including designer/Iris.',
        evidence={'latest_day': latest_day, 'profiles': sorted(latest_profiles), 'missing': sorted(missing_profiles)},
    )

    failures = [item for item in checks if not item['pass']]
    return {
        'status': 'pass' if not failures else 'fail',
        'checked_at_note': 'local filesystem/db snapshot',
        'failure_count': len(failures),
        'checks': checks,
    }


def print_text(payload: dict[str, Any]) -> None:
    print(f"workflow_invariants={payload['status']} failures={payload['failure_count']}")
    for check in payload['checks']:
        prefix = 'PASS' if check['pass'] else 'FAIL'
        print(f"{prefix} {check['name']}: {check['detail']}")
        if not check['pass']:
            print(f"  evidence: {check['evidence']}")


def main() -> int:
    args = parse_args()
    payload = audit(Path(args.hermes_home).expanduser().resolve())
    if args.json:
        print(json.dumps(payload, indent=2, sort_keys=True))
    else:
        print_text(payload)
    return 0 if payload['status'] == 'pass' else 2


if __name__ == '__main__':
    raise SystemExit(main())
