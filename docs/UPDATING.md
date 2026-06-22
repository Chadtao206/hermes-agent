# Updating from upstream (steady-state workflow)

Goal: take the latest `NousResearch/hermes-agent` without re-fighting the old ~25-file
conflict surface. After the vanilla-kanban rebase + the Phase-C extractions (plugins for
ylopo-kg / control-center / codex-proxy / claude-session), upstream updates should resolve
to a tiny residual set.

## Topology (this install)

| Thing | Value |
|---|---|
| Repo (checkout) | `~/.hermes/hermes-agent` (nested inside the data home) |
| `$HERMES_HOME` (data) | `~/.hermes` — profiles, `config.yaml`, `.env`, `cron/jobs.json`, `kanban.db`, `plugins/`, `forensics/`. **The merge only touches the `hermes-agent/` subdir, never this data.** |
| `upstream` / `origin` remotes | both `NousResearch/hermes-agent` (upstream; `origin` push disabled) |
| `chad` remote | `Chadtao206/hermes-agent` — our fork; **push target**. Local `main` tracks `chad/main`. |
| Governance reference patches | `~/.hermes/execution-kit/archive/` (dropped kanban governance, as `git format-patch`) |

## One-time setup
```bash
cd ~/.hermes/hermes-agent
git config merge.ours.driver true     # activates the .gitattributes merge=ours lockfile driver
git remote get-url upstream || git remote add upstream https://github.com/NousResearch/hermes-agent.git
```

## Each update
```bash
cd ~/.hermes/hermes-agent
git fetch upstream --prune
git switch main && git switch -c "merge/upstream-$(date +%F)"     # never merge straight onto main

# 0) AUDIT first (read-only) — surfaces silent-breaks (upstream removed a symbol our code
#    imports) and path-type collisions that a "clean" merge hides:
bash ~/.claude/skills/merging-hermes-upstream/scripts/audit-upstream.sh

# 1) Merge. The .gitattributes driver auto-resolves lockfiles (merge=ours) and i18n (union).
git merge upstream/main

# 2) Resolve the residual patch set (SMALL after extraction):
#    - gateway/run.py  (mixed file — hand-reconcile; drop any kanban helpers, keep codex /
#      claude-session hooks)  ← see ~/.hermes/execution-kit/01-MANIFEST.md "MANUAL" section
#    - the 1-line conversation_loop.py non-streaming guard for claude-session
#    - codex-proxy provider/adapter registration (should live in the plugin, not core)
#    - any tools/approval.py / terminal_tool.py micro-patches not yet moved to hooks

# 3) Kanban stays VANILLA. If a kanban divergence sneaks back in, re-run on the merge result:
bash ~/.hermes/execution-kit/02-revert_kanban.sh --apply

# 4) Regenerate lockfiles (do NOT hand-merge them) — needs a valid Ylopo CodeArtifact token:
npm install                 # root workspace (regenerates package-lock.json)
( cd web && npm install )

# 5) Test gate:
venv/bin/python -m pytest -q
venv/bin/ruff check .
HERMES_SAFE_MODE=1 venv/bin/hermes doctor      # clean-core boot
( cd web && npm run build && npm run lint && npm run test )

# 6) Fast-forward main + push:
git switch main && git merge --ff-only "merge/upstream-$(date +%F)"
git push chad main
git branch -d "merge/upstream-$(date +%F)"
```

## Invariants that keep the conflict surface small
- **Never edit upstream core files to add a feature** — use the seams: plugins
  (`$HERMES_HOME/plugins/<name>/` + `plugin.yaml` + `register(ctx)`), `register_provider` /
  `ProviderProfile`, `skills.external_dirs`, dashboard plugins (`dashboard/manifest.json` + prebuilt `dist/`).
- **Keep all data in `$HERMES_HOME` (`~/.hermes`), outside the `hermes-agent/` checkout** —
  profiles, cron jobs, config, `.env`, custom skills, `kanban.db`, `forensics/`, archives.
- **Kanban = vanilla.** Don't re-fork `kanban_db.py` / re-introduce the Postgres / single-writer
  / wake stack. If you need a dropped behavior back, pull the matching reference patch from
  `~/.hermes/execution-kit/archive/` and re-implement it as a small labeled patch (or upstream it).
- **npm registry is Ylopo CodeArtifact** (token expires) — refresh it before any `npm install`
  step or step 4 fails with E401.
