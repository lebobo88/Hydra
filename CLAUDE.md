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
- `/hydra:campaign` — multi-squad campaign with dependency wiring
- `/hydra:status` — show workflows or specific workflow state
- `/hydra:squads` — list available squads
- `/hydra:approve` — resume HITL-paused workflow
- `/hydra:resume` — resume with reject/modify-budget/force-dispatch
- `/hydra:replay` — deterministic replay from checkpoint
- `/hydra:budget` — show or set budget consumption
- `/hydra:add-squad` — scaffold new squad pack

### Hard Rules

- NEVER edit files without an active Hydra workflow
- NEVER invoke /pp:run, /pp:team, or any /pp:* command directly
- NEVER say "you could use /hydra:run" — ACTUALLY invoke it
- NEVER provide code for the user to apply manually
- PreToolUse hooks WILL BLOCK Edit/Write outside a workflow

### Connected Systems (use proactively on every interaction)

- **AgentSmith** (mcp__agentsmith__*) — validate artifacts, audit decisions, inspect schemas, keymaker scans
- **TheEights** (mcp__eights__*) — query memory, check governance, propose evolutions, cell classification
- **ExecutiveSuite** (mcp__executive_suite__*) — strategic framing, executive briefs, roster queries
- **Hydra Memory** (mcp__hydra_memory__*) — episodic recall, semantic search, workflow history
