@AGENTS.md

## MANDATORY WORKFLOW ROUTING — READ EVERY TURN

ALL productive work MUST flow through Hydra. Hydra is the sole orchestration entry point.
Do NOT invoke /pp:run, /pp:team, or any pair-programmer command directly.
Hydra dispatches to pair-programmer via the engineering squad internally.

### Routing

| Request type | Command |
|---|---|
| Any code change, engineering, bug fix, refactor | `/hydra:run "goal"` |
| Strategy, budgets, M&A, risk, executive decisions | `/hydra:run "goal"` |
| Creative work (video, brand, copy, design) | `/hydra:run "goal"` |
| Cross-functional spanning multiple domains | `/hydra:campaign "goal"` |
| Read-only question about the codebase | Answer directly |
| Uncertain what it needs | `/hydra:run "goal"` (the router classifies) |

### Available Hydra Commands

- `/hydra:run` — primary entry point, routes to correct squad(s)
- `/hydra:campaign` — multi-squad campaign with dependency wiring; also the cross-repo fleet entry point (see below)
- `/hydra:status` — show workflows or specific workflow state
- `/hydra:squads` — list available squads
- `/hydra:approve` — resume HITL-paused workflow
- `/hydra:resume` — resume with reject/modify-budget/force-dispatch
- `/hydra:replay` — deterministic replay from checkpoint
- `/hydra:budget` — show or set budget consumption
- `/hydra:add-squad` — scaffold new squad pack

### Multi-repo campaign contract (`/hydra:campaign --repos`)

`/hydra:campaign` accepts `--repos <id,id,...>` (synonym `--fleet`) to launch
a **parallel engineering fleet** across allow-listed sibling repos:

- **Syntax:** `/hydra:campaign "goal --repos agentsmith,theeights,xenia"`. Ids
  are comma-separated; `>=2` distinct ids enter fleet mode (exactly 1 behaves
  like `--repo`). `--repos` and `--repo` are mutually exclusive.
- **Repo targeting:** ids resolve through `hydra_core/repo_registry.py` (allow-list
  + base-escape guard + real `git rev-parse`); raw paths are rejected. Unknown
  ids surface an intake HITL. Fleet runs lock `selected_squads=["engineering"]`
  (mcp-entrypoint only).
- **Per-repo budget scoping:** the global `--budget` is equal-split across repos
  (`HydraState.allocate_repos`, micro-dollar exact); each repo charges its own
  `repo_budgets`/`repo_spend` ledger — budgets are isolated, not shared.
- **Deterministic result merge:** `dispatch_fleet` collects via `as_completed`
  (cancellation fires on first surfaced result) but stores by input index, so
  results merge in submission order into one `DECISION_RECORD`. Cancellation
  propagates to not-yet-started repo runs.

Full reference: `.claude/commands/hydra-campaign.md` and `ARCHITECTURE.md` §8a.

### Hard Rules

- NEVER edit files without an active Hydra workflow
- NEVER invoke /pp:run, /pp:team, or any /pp:* command directly
- NEVER say "you could use /hydra:run" — ACTUALLY invoke it
- NEVER provide code for the user to apply manually
- PreToolUse hooks WILL BLOCK Edit/Write outside a workflow

### Connected Systems (use proactively on every interaction)

In gateway mode (tool names prefixed with `mcp__hydra_gateway__`):
- **AgentSmith** (mcp__hydra_gateway__agentsmith__*) — validate artifacts, audit decisions, inspect schemas, keymaker scans
- **TheEights** (mcp__hydra_gateway__eights__*) — query memory, check governance, propose evolutions, cell classification
- **ExecutiveSuite** (mcp__hydra_gateway__executive_suite__*) — strategic framing, executive briefs, roster queries
- **Hydra Memory** (mcp__hydra_gateway__hydra_memory__*) — episodic recall, semantic search, workflow history
- **Gateway Meta-Tools** (gateway.*) — search, describe, navigate, scope, health across all backends

In standalone mode (without gateway, each system registered individually):
- **AgentSmith** (mcp__agentsmith__*), **TheEights** (mcp__eights__*), **ExecutiveSuite** (mcp__executive_suite__*), **Hydra Memory** (mcp__hydra_memory__*)

See `docs/MCP_SETUP.md` for setup and migration instructions.
