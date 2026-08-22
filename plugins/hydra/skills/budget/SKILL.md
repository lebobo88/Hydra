---
description: "Show rolling budget consumption across workflows; optionally set a new cap."
argument-hint: "[<workflow_id>] [--set <usd>]"
model: haiku
disable-model-invocation: true
---

# /hydra:budget

Read or set the `HydraState.budget` ledger for a specific workflow, or list all
workflows with their budget summaries.

```
/hydra:budget                          # list all workflows latest-first: budget_usd, spent_usd, phase
/hydra:budget <workflow_id>            # full ledger: remaining, percent_consumed, repo_budgets, repo_spend
/hydra:budget <workflow_id> --set 250  # patch budget_usd to $250 (M3 capability gate; persisted to checkpoint)
```

## Engine surfaces

**CLI:** `python -m hydra_core.cli budget [<workflow_id>] [--set <USD>]`

**MCP tool:** `hydra.workflow.budget` (on `hydra_control` server)
  - `workflow_id` (optional): workflow to inspect or patch
  - `set_budget` (optional number): new budget ceiling — triggers M3 capability verification

The `--set` / `set_budget` path runs through the same `verify_operator_capability`
gate as `hydra resume --action modify-budget`, issuing a degraded-warn token when
`HYDRA_OPERATOR_KEY` is unset (foundation posture). The patch is persisted via
`sup.update_state` and a `budget.set` trace event is emitted.
