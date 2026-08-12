---
description: "Resume a paused workflow with a non-approval action: reject, modify-budget, force-dispatch, change-squads."
argument-hint: "<workflow_id> --reject | --modify-budget <usd> | --force-dispatch | --squads <a,b>"
model: sonnet
---

# /hydra:resume

<authority_boundary>
This is a native Claude Code interface for an explicit human HITL decision.
Only `hydra.workflow.resume` (or the matching Hydra CLI) may validate and
persist the decision, modify a budget, alter squads, or re-enter the graph.
</authority_boundary>

Companion to `/hydra:approve`. Drives non-approve resume paths:

- `--reject`: mark the workflow `surfaced`, write a rejection note.
- `--modify-budget 250`: update `state.budget.budget_usd` and re-enter dispatch.
- `--force-dispatch`: dispatch even though a gate failed (logs a `policy_override` event; operator owns the risk).
- `--squads engineering,garland`: replace `selected_squads` and re-plan.

First render the current pending HITL request from `python -m hydra_core.cli
status <workflow_id>` and obtain an explicit operator decision. Then call
`hydra.workflow.resume` with
the matching action and option (or the matching `hydra resume` CLI form). Do
not patch checkpoint state or any trace directly.

Note (G4): when the resume path would re-detach a background `hydra resume
--live` subprocess (detached workflows), it is gated by
`HYDRA_ALLOW_DETACHED=1` and otherwise returns `error: "detached_disabled"`.
Attended workflows resume in-process and continue via `step`/`submit`.
