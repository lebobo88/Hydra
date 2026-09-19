---
description: "Resume a paused workflow with a non-approval action: reject, modify-budget, force-dispatch, change-squads."
argument-hint: "<workflow_id> --reject | --modify-budget <usd> | --force-dispatch | --squads <a,b>"
model: sonnet
disable-model-invocation: true
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
- `--force-dispatch`: dispatch even though a gate failed (logs a `policy_override` event; operator owns the risk). At `plan_gate` this additionally stamps `plan_status="bypassed"` and appends a governance note to the plan artifact, so proceeding without an approved plan is evidence rather than a gap.
- `--squads engineering,garland`: replace `selected_squads` and re-plan.
- `--modify-plan --critique-ref <path-or-memoryref>`: request a plan revision.
  Valid **only** at `plan_gate`. Bumps `plan_revision`, sets
  `plan_status="authoring"`, and seeds one planning task carrying the critique
  and the prior plan's id as `supersedes`. Bounded by
  `HYDRA_PLAN_MAX_REVISIONS` (default 2); at the ceiling the gate offers only
  `approve` and `abort`.
  **The critique travels as a reference, never as `--option`.** That field is
  capped at 200 characters of a restricted character class because it guards a
  string bound for a subprocess argument list — real prose would be truncated
  or refused outright. The referenced file must resolve inside the project
  root; an escaping path is refused rather than read.

Rejecting at `plan_gate` parks the workflow and deliberately does **not**
trigger a re-plan. An engine that authors a fresh plan the instant one is
rejected is a loop the operator cannot stop. The rejected plan stays on disk,
marked rejected — it is evidence of a decision, not waste.

First render the current pending HITL request from `python -m hydra_core.cli
status <workflow_id>` and obtain an explicit operator decision. Then call
`hydra.workflow.resume` with
the matching action and option (or the matching `hydra resume` CLI form). Do
not patch checkpoint state or any trace directly.

Note (RCA path K, EIGHTS-RECORD-OUTCOME-RCA-2026-09-16 §7): the transport
`hydra.workflow.resume` picks is governed by `HYDRA_ALLOW_DETACHED`, not by
whether the workflow itself is attended or detached. With the gate set, it
launches a DETACHED `hydra resume --live` subprocess and returns immediately
(`{ok, launched: true, pid, log}`). Without the gate — the normal interactive
session — it runs `hydra resume` SYNCHRONOUSLY in-process WITHOUT `--live`
(on `_NullDispatcher`, which never re-dispatches a host-bridged squad like
engineering on the stub) and returns the resolved gate and the workflow's
resulting `status`/`pending_hitl` in the same call. Either way, an attended
workflow's engineering (and any other host-bridged) tasks are deferred back
to the host rather than executed, so `step`/`submit` continues from the
cursor afterward.
