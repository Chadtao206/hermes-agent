# First real routing/workflow experiment

Experiment ID: exp-kanban-specialist-routing
Owner: researcher
Launch date: 2026-05-19 UTC
Status: running

## Decision
Launch the first non-bootstrap routing/workflow experiment now, but treat the initial score as observational only until the window contains enough real routed work.

## Hypothesis
If durable multi-step work is routed through kanban with an explicit specialist as the initial owner instead of Jensen executing directly, then:
- first_owner_routing_accuracy should increase
- user_correction_rate should decrease or remain flat
- without regressing task_success_rate or reopened_task_rate

## Scope
Treatment population:
- durable multi-step tasks
- explicit kanban ownership
- specialist initial owner (researcher, engineer, reviewer, ops, designer) instead of default/Jensen direct execution

Excluded from interpretation:
- bootstrap/demo telemetry tasks
- synthetic probe tasks
- trivial one-turn asks

## Target metrics
- first_owner_routing_accuracy: increase
- user_correction_rate: decrease

## Guardrails
- task_success_rate: must not regress
- user_correction_rate: must not regress materially
- reopened_task_rate: must not regress

## Observation window
- 7 days, scored against the immediately preceding 7-day baseline window
- minimum evidence target before any keep/revert decision: at least 5 real substantial routed tasks

## Rollback rule
Revert or redesign the routing heuristic if either of these becomes true once the evidence floor is met:
- any guardrail regresses materially
- first_owner_routing_accuracy remains at or below 0.5

If the evidence floor is not met, recommendation stays extend_observation rather than keep/revert.

## Current evidence at launch
- bench_metrics_daily currently has only one real calendar day of data: 2026-05-19
- current bench scorecard: task_success_rate=1.0, user_correction_rate=0.6667, reopened_task_rate=0.0, first_owner_routing_accuracy=0.5
- baseline window for a 7-day experiment is empty, so score_experiment.py returns insufficient_data today
- routing telemetry currently includes bootstrap/synthetic history, so today's score is a launch marker, not a decision-quality read

## Next evidence needed
1. accumulate at least 5 real specialist-routed substantial tasks
2. keep syncing kanban data and rebuilding daily metrics
3. re-run score_experiment.py after enough real days exist to populate both baseline and observation windows

## Launch command
HOME=/Users/ctao python3 /Users/ctao/.hermes/scripts/telemetry/score_experiment.py \
  --telemetry-root /Users/ctao/.hermes/telemetry \
  --experiment-id exp-kanban-specialist-routing \
  --as-of 2026-05-19 \
  --write-observations
