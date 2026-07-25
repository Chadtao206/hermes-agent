# claude-kanban-lane host artifacts (versioned)

The `claude-kanban-lane` plugin runs each claude-CLI worker in a tmux pane with
an out-of-process **supervisor** babysitting it. The supervisor owns the kanban
card on behalf of a session Python never forked, so it depends on the core
ownership contract in [`hermes_cli/kanban_supervisor.py`](../../../hermes_cli/kanban_supervisor.py):
an exact run to fence every lifecycle call to, its own pid as the recorded
owner, and a run-fenced compare-and-set for adopting a live session.

Those artifacts live **outside any checkout** — `~/.hermes/scripts`,
`~/.hermes/plugins/claude-kanban-lane` — while the API they call lives inside
one. This directory is the source of truth for the files on both sides of that
line, and `install.py` is the only supported way to move them onto a host.

## Why this exists

The artifacts were once hand-edited on the host to import
`hermes_cli.kanban_supervisor` while the *installed* core still predated that
module. Result: every lane spawn died with `ModuleNotFoundError`, the plugin's
own test suite stopped collecting, and the repo's tests passed anyway because
they ran against a checkout the lane never imports. Three properties prevent a
repeat:

1. **Versioned artifacts.** `install.py --check` reports drift, and reports a
   *partial* install as an error — that mixed state is what broke the lane.
2. **An ordering gate.** `install.py --install` probes the target runtime and
   refuses unless the core contract is actually importable there. The probe runs
   with `PYTHONPATH` scrubbed and a cwd outside every checkout, so it answers
   for the runtime the lane imports, not the tree you are sitting in.
3. **Fail-closed artifacts.** If the core contract is missing anyway (a core
   rollback under installed artifacts), the supervisor stands down with exit 6
   and the launcher keeps the lane off entirely — the card runs as a native
   worker instead. No traceback per spawn, no orphaned panes.

## Contents

| Versioned path | Installs to |
| --- | --- |
| `scripts/claude_kanban_supervisor.py` | `~/.hermes/scripts/claude_kanban_supervisor.py` |
| `scripts/hermes-kanban-task` | `~/.hermes/scripts/hermes-kanban-task` |
| `plugin/launcher.py` | `~/.hermes/plugins/claude-kanban-lane/launcher.py` |
| `plugin/wrapper.py` | `~/.hermes/plugins/claude-kanban-lane/wrapper.py` |
| `plugin/tests/*.py` | `~/.hermes/plugins/claude-kanban-lane/tests/` |

The rest of the plugin (`brief.py`, `cfgdir.py`, `config.py`, `envbuild.py`,
`modelmap.py`, `router.py`, `state.py`) is unversioned host state that this
contract does not touch. The plugin tests are versioned because they pin the
artifacts' behaviour, and an install that updated the code but not its tests
would leave the host suite failing on purpose-built assertions.

## Install ordering

**Core first, artifacts second.** Ordering matters in one direction only:

* the supervisor script is re-read from disk on every lane spawn;
* `launcher.py` / `wrapper.py` are imported once, at gateway start.

So:

| Order | Result |
| --- | --- |
| new supervisor + old launcher | **safe** — the old launcher passes no `--run-id`, and the new supervisor pins the card's current run itself, once, at startup |
| new launcher + old supervisor | **unsafe** — the launcher passes `--run-id` / `--claim-lock`, the old supervisor's argparse rejects them, it exits 2, and a live claude pane is orphaned |

`install.py` therefore installs in manifest order (scripts before plugin
modules) and rolls back in reverse.

```sh
# 1. land the core change (merge to main / deploy the checkout the gateway imports)
# 2. verify the runtime actually has it, then install:
python plugins/kanban/claude_lane/install.py --check
python plugins/kanban/claude_lane/install.py --install
# 3. plugin suite against the installed files:
cd ~/.hermes/plugins/claude-kanban-lane && pytest -q tests
```

A gateway restart is **not** required by the install: `launcher.py` /
`wrapper.py` only take effect at the next gateway start, and until then the
running gateway keeps using the in-memory previous versions, which the new
supervisor tolerates (the safe row above).

## Rollback ordering

Artifacts first, core second — the mirror image of install:

```sh
python plugins/kanban/claude_lane/install.py --rollback   # restores newest .pre-<stamp> backups
# then, if needed, revert the core commit
```

Rolling back only the core is also survivable (that is what property 3 above
buys): the installed artifacts detect the missing contract and keep the lane
native until the core returns.

## Verification

Checkout-pinned (runs in CI, no host required):

```sh
pytest -q tests/hermes_cli/test_kanban_supervisor_contract.py \
          tests/plugins/test_kanban_claude_lane_rollout.py
```

Installed-runtime (host only, and deliberately without `PYTHONPATH` help —
`PYTHONPATH` pointing at a dev checkout is exactly what masked the original
breakage):

```sh
cd /tmp
env -u PYTHONPATH ~/.hermes/scripts/claude_kanban_supervisor.py --help   # must exit 0
cd ~/.hermes/plugins/claude-kanban-lane && env -u PYTHONPATH pytest -q tests
python plugins/kanban/claude_lane/install.py --check                     # 0 installed / 3 staged / 1 mixed
```

`--check` exit codes: `0` fully installed and coherent, `3` staged but not
installed (the coherent pre-deploy state), `1` drift, partial install, or
artifacts installed against a core that lacks the contract.
