---
description: "Resume a paused workflow with a non-approval action: reject, modify-budget, force-dispatch, change-squads."
argument-hint: "<workflow_id> --reject | --modify-budget <usd> | --force-dispatch | --squads <a,b>"
model: sonnet
---

# /hydra:resume

Companion to `/hydra:approve`. Drives non-approve resume paths:

- `--reject`: mark the workflow `surfaced`, write a rejection note.
- `--modify-budget 250`: update `state.budget.budget_usd` and re-enter dispatch.
- `--force-dispatch`: dispatch even though a gate failed (logs a `policy_override` event; operator owns the risk).
- `--squads engineering,creative`: replace `selected_squads` and re-plan.

Adopt `hydra-hitl-gate` to capture the operator decision, then resume the LangGraph checkpoint with the patched state.
