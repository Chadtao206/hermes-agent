"""Purpose evidence stays bound to the owned attempt, independently of execution success."""

import json

import pytest


def test_contract_snapshot_survives_edits_and_rejects_foreign_owners(tmp_path, monkeypatch):
    from cron import executions

    monkeypatch.setattr(executions, 'EXECUTIONS_FILE', tmp_path / 'executions.db')
    run = executions.create_execution('job', source='test')
    executions.mark_execution_running(run['id'])
    job = {'id': 'job', 'no_agent': True, 'purpose_contract': {
        'version': 1, 'required_task': True, 'task_id': 'task-original',
        'validator': 'json_status', 'status_outcomes': {'observed': 'succeeded'}}}
    assert executions.capture_purpose_contract(run['id'], job)
    job['purpose_contract']['task_id'] = 'task-changed'
    assert not executions.capture_purpose_contract(run['id'], job)
    snapshot = json.loads(executions.get_execution(run['id'])['purpose_contract'])
    assert snapshot['task_id'] == 'task-original'
    monkeypatch.setattr(executions, '_PROCESS_ID', 'different-owner')
    assert executions.finish_execution(run['id'], success=True, purpose_output='{"status":"observed"}') is None
    assert executions.get_execution(run['id'])['purpose_outcome'] == 'unknown'


@pytest.mark.parametrize('no_agent,contract,output,success,expected', [
    (True, {'version': 1, 'validator': 'json_status', 'status_outcomes': {'observed': 'succeeded'}}, '{"status":"observed"}', True, 'succeeded'),
    (True, {'version': 1, 'validator': 'json_status', 'status_outcomes': {'idle': 'no_work'}}, '{"status":"idle"}', True, 'no_work'),
    (True, {'version': 1, 'validator': 'json_status', 'status_outcomes': {'observed': 'succeeded'}}, '[SILENT]', True, 'unknown'),
    (False, {'version': 1, 'validator': 'json_status', 'status_outcomes': {'observed': 'succeeded'}}, '{"status":"observed"}', True, 'unknown'),
    (True, {'version': 1, 'required_task': True}, '[SILENT]', True, 'failed'),
    (True, None, '[SILENT]', True, 'unknown'),
    pytest.param(True, {'version': 1, 'validator': 'json_status', 'status_outcomes': {'ok': 'succeeded'}}, '{"status":"ok","extra":' + '[' * 49_000 + '0' + ']' * 49_000 + '\n}', True, 'unknown', id='deep-json-stays-unknown'),
    (True, {'version': 1, 'validator': 'json_status', 'status_outcomes': {'observed': 'succeeded'}}, '{"status":"observed"}', False, 'failed'),
    (True, {'version': 1, 'validator': 'json_status', 'status_outcomes': {'observed': 'succeeded'}}, '{"status":[]}', True, 'unknown'),
])
def test_real_script_run_records_shadow_purpose_without_repeating_side_effects(
    tmp_path, monkeypatch, no_agent, contract, output, success, expected
):
    from cron import executions, jobs, scheduler

    home = tmp_path / 'profile'
    scripts = home / 'scripts'
    scripts.mkdir(parents=True)
    marker = home / 'ran'
    evidence = home / 'evidence.txt'
    evidence.write_text(output)
    (scripts / 'probe.py').write_text(
        'from pathlib import Path\n'
        f'with Path({str(marker)!r}).open("a") as stream: stream.write("once\\n")\n'
        f'print(Path({str(evidence)!r}).read_text())\n'
        f'raise SystemExit({0 if success else 1})\n'
    )
    monkeypatch.setenv('HERMES_HOME', str(home))
    monkeypatch.setattr(executions, 'EXECUTIONS_FILE', home / 'cron' / 'executions.db')
    monkeypatch.setattr(scheduler, '_launch_external_cron_worker', lambda _job: False)
    with jobs.use_cron_store(home):
        job = jobs.create_job(prompt=None, schedule='every 1h', script='probe.py', no_agent=True, deliver='local')
        job = jobs.update_job(job['id'], {'purpose_contract': contract})
        # The agent case exercises the same ledger contract without calling a provider.
        if not no_agent:
            job['no_agent'] = False
            monkeypatch.setattr(scheduler, 'run_job', lambda *_a, **_k: (success, output, output, None))
        assert scheduler.run_one_job(job)
        run = executions.latest_execution(job['id'])
        assert run['purpose_outcome'] == expected
        assert run['status'] == ('completed' if success else 'failed')
        if no_agent:
            assert marker.read_text() == 'once\n'
        assert executions.finish_execution(run['id'], success=True, purpose_output='{"status":"observed"}') is None
        assert executions.get_execution(run['id']) == run


def test_legacy_ledger_and_interrupted_runs_never_gain_purpose_success(tmp_path, monkeypatch):
    import sqlite3
    from cron import executions

    database = tmp_path / 'executions.db'
    monkeypatch.setattr(executions, 'EXECUTIONS_FILE', database)
    with sqlite3.connect(database) as conn:
        conn.execute('''CREATE TABLE executions (
            id TEXT PRIMARY KEY, job_id TEXT, source TEXT, process_id TEXT,
            pid INTEGER, process_started_at INTEGER, status TEXT,
            claimed_at TEXT, started_at TEXT, finished_at TEXT, error TEXT)''')
        conn.execute("INSERT INTO executions (id,job_id,status) VALUES ('old','job','completed')")
    assert executions.get_execution('old')['purpose_outcome'] == 'unknown'
    run = executions.create_execution('job', source='test')
    executions.mark_execution_running(run['id'])
    executions.capture_purpose_contract(run['id'], {
        'id': 'job', 'no_agent': True, 'purpose_contract': {
            'version': 1, 'validator': 'json_status', 'status_outcomes': {'ok': 'succeeded'}}})
    monkeypatch.setattr(executions, '_PROCESS_ID', 'replacement')
    monkeypatch.setattr(executions, '_owner_is_live', lambda *_args: False)
    assert executions.recover_interrupted_executions() == 1
    recovered = executions.get_execution(run['id'])
    assert recovered['status'] == 'unknown'
    assert recovered['purpose_outcome'] == 'unknown'


def test_deep_contract_cannot_turn_shadow_capture_into_execution_failure(tmp_path, monkeypatch):
    from cron import executions

    monkeypatch.setattr(executions, 'EXECUTIONS_FILE', tmp_path / 'executions.db')
    nested = []
    for _ in range(1100):
        nested = [nested]
    run = executions.create_execution('job', source='test')
    executions.mark_execution_running(run['id'])
    assert executions.capture_purpose_contract(run['id'], {
        'id': 'job', 'purpose_contract': {'version': 1, 'task_id': nested}})
    finished = executions.finish_execution(run['id'], success=True)
    assert finished['status'] == 'completed'
    assert finished['purpose_outcome'] == 'unknown'
