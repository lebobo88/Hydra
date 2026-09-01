---
name: hitl-protocol
description: "When and how Hydra pauses for human input. Defines HITL gate placement, render format, resume contracts, expiry behavior, and override semantics."
---

# HITL Protocol

Hydra is HITL-first. Three rules every agent honors:

1. **You do not override a gate.** Only `/hydra:approve` or `/hydra:resume` resumes a paused workflow.
2. **You do not paraphrase the request.** Render the `HITL_REQUEST` envelope verbatim — its `reason`, `summary`, `options`, and `default_option`.
3. **You wait.** If the operator does not respond, mark the workflow `surfaced` after `expires_at` (default 24h) and emit a postmortem note.

## Expiry and ledger reconciliation

Every gate Hydra files with TheEights (`eights.governance.hitl.request`,
kind `hydra_gate`) carries `expires_at` = now + `HYDRA_HITL_EXPIRY_HOURS`
(default 24h, the rule-3 window). Filing without an expiry is what produced
858 immortal pending rows (finding E2-17).

Terminal transitions close the ledger row as well as the local gate:
`hydra resume` (any resolution, scoped to that gate's `gate_node`), the
`abort` option, and `hydra reap --apply` all issue
`eights.governance.hitl.resolve`. The call is fail-soft and spooled, so an
offline daemon replays it on the next `hydra eights-drain`.

Operator surfaces:

- `hydra doctor` WARNs when pending `hydra_gate` rows exceed
  `HYDRA_HITL_PENDING_THRESHOLD` (default 25).
- `hydra eights-hitl-reconcile [--apply] [--limit N]` sweeps the pending
  queue against Hydra's checkpoint store and resolves rows whose workflow is
  terminal or unknown. Rows for an active workflow are never touched.

## Gate Placement in the Supervisor Graph

LangGraph builds with `interrupt_before=["approval", "synthesis"]`. Additional ad-hoc gates fire from inside `dispatch` when a squad emits a `HITL_REQUEST`.

| Gate | Reason codes |
|---|---|
| approval (planning → dispatch) | `budget_approval`, `high_risk`, `policy_breach`, `campaign_signoff` |
| synthesis (dispatch → postcheck) | `schema_conflict`, `dissent_unresolved` |
| postcheck (postcheck → done) | `loop_ceiling`, `budget_approval`, `prod_deploy` |

## Render Format

```
============================================
HYDRA HITL REQUEST — workflow_id=<UUID>
reason   : high_risk
summary  : Dispatch creative+engineering to launch campaign for $8,000
options  : approve | reject | modify-budget
default  : reject
expires  : 2026-05-19T18:00:00Z
============================================
```

## Resume Contracts

- `/hydra:approve <wf>` → `hitl_history += [{decision:"approve", ts, operator}]`, `pending_hitl=None`, resume.
- `/hydra:resume <wf> --reject` → mark `phase="surfaced"`, log rejection.
- `/hydra:resume <wf> --modify-budget <usd>` → patch `state.budget.budget_usd`, resume.
- `/hydra:resume <wf> --force-dispatch` → emit `policy_override` event, resume. Operator owns risk.

## What NOT To Do

- Do NOT swallow `pending_hitl` to "keep things moving." That defeats the audit trail.
- Do NOT route the HITL into an LLM "approver agent." Humans only.
- Do NOT clear `hitl_history`. It is append-only.
