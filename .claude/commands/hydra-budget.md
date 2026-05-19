---
description: "Show rolling budget consumption across workflows; optionally set a new cap."
argument-hint: "[<workflow_id>] [--set <usd>]"
model: haiku
---

# /hydra:budget

Read `HydraState.budget` for a specific workflow, or aggregate across all active workflows.

```
/hydra:budget                          # rolling totals: workflows count, $spent, tokens
/hydra:budget <workflow_id>            # just that one
/hydra:budget <workflow_id> --set 250  # patch budget_usd; useful mid-flight
```
