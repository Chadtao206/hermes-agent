"""Shadow purpose evidence; never changes execution or delivery decisions."""

import json


def snapshot_contract(job):
    contract = job.get('purpose_contract')
    if contract is None:
        return 'null'
    if not isinstance(contract, dict):
        return '{"invalid":true}'
    snapshot = {key: contract[key] for key in (
        'version', 'required_task', 'task_id', 'validator', 'status_outcomes'
    ) if key in contract}
    snapshot['no_agent'] = job.get('no_agent') is True
    try:
        return json.dumps(snapshot, allow_nan=False)
    except (TypeError, ValueError, RecursionError):
        return '{"invalid":true}'


def classify_purpose(contract_json, success, output):
    if not success:
        return 'failed', 'execution_failed'
    if not contract_json or contract_json == 'null':
        return 'unknown', 'missing_contract'
    try:
        contract = json.loads(contract_json)
    except (ValueError, TypeError, RecursionError):
        return 'unknown', 'invalid_contract'
    if (not isinstance(contract, dict) or type(contract.get('version')) is not int
            or contract['version'] < 1
            or type(contract.get('required_task', False)) is not bool):
        return 'unknown', 'invalid_contract'
    if contract.get('required_task'):
        task_id = contract.get('task_id')
        if not isinstance(task_id, str) or not task_id.strip():
            return 'failed', 'missing_required_task'
    if contract.get('no_agent') is not True or contract.get('validator') != 'json_status':
        return 'unknown', 'missing_trusted_validator'
    outcomes = contract.get('status_outcomes')
    if (not isinstance(outcomes, dict) or not outcomes
            or any(not isinstance(key, str) or value not in (
                'succeeded', 'no_work', 'blocked', 'failed') for key, value in outcomes.items())):
        return 'unknown', 'invalid_contract'
    if not isinstance(output, str) or len(output) > 100_000:
        return 'unknown', 'missing_terminal_evidence'
    try:
        data = json.loads(output)
    except (ValueError, TypeError, RecursionError):
        return 'unknown', 'invalid_terminal_evidence'
    status = data.get('status') if isinstance(data, dict) else None
    if not isinstance(status, str) or status not in outcomes:
        return 'unknown', 'unrecognized_terminal_status'
    return outcomes[status], 'verified_script_json_status'
