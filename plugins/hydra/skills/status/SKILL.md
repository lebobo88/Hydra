---
description: "Show recent Hydra workflows or the structured state of a specific workflow."
argument-hint: "[<workflow_id>]"
model: haiku
disable-model-invocation: true
---

# /hydra:status

<authority_boundary>
This is a read-only Claude Code operator view over Hydra state. Query the
structured status interface; do not reconstruct state by editing checkpoints,
traces, budgets, or HITL records.
</authority_boundary>

```
/hydra:status                 # list workflows latest-first (by trace mtime)
/hydra:status <workflow_id>   # structured tasks table + pending HITL + budget
```

## No-arg output

Returns `{"workflows": [...]}` sorted by trace mtime (LATEST-FIRST). Each entry:
- `workflow_id` — the UUID
- `phase` — current phase (from LangGraph checkpoint; `"?"` if unavailable)
- `pending_hitl` — `{reason, gate_node}` if an HITL gate is open, else `null`
- `root_goal` — first 80 chars of the goal (when available)

## With-id output

Returns a structured JSON object (NOT a raw trace dump):
- `tasks` — table of `[{task_id (8 chars), owner_squad, status}]`
- `pending_hitl` — `{reason, gate_node, summary}` or `null`
- `budget` — `{budget_usd, spent_usd, usd_remaining, percent_consumed}`
- `phase`, `root_goal`

Falls back to the last 30 trace events when the LangGraph checkpoint is unavailable.

## Implementation

Run `python -m hydra_core.cli status [<workflow_id>]` and render the returned
structured result without mutating the workflow. Hydra deliberately exposes
this read-only view through its CLI rather than inventing a duplicate MCP
state surface.
