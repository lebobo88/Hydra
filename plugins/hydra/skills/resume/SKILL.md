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

Note (RCA path K, EIGHTS-RECORD-OUTCOME-RCA-2026-09-16 §7; RESOLVE-GATE-ONLY
follow-up): the transport `hydra.workflow.resume` picks is governed by
`HYDRA_ALLOW_DETACHED`, not by whether the workflow itself is attended or
detached.

- **Detached-allowed** (automation, `HYDRA_ALLOW_DETACHED=1`): launches a
  DETACHED `hydra resume --live` subprocess and returns immediately
  (`{ok, launched: true, pid, log}`). Unchanged.
- **Attended (the normal interactive session, gate not set)**: runs
  `hydra resume --gate-only` SYNCHRONOUSLY in-process on `_NullDispatcher`.
  This resolves the gate (lock, operator-capability mint+verify, spool
  prune, per-action state patch clearing `pending_hitl`) and returns
  WITHOUT EVER re-entering the compiled graph — no `sup.invoke`, no
  `node_dispatch`, no squad of any kind runs, not even a stub result. The
  result JSON says so explicitly (`graph_reentered: false`, a `note` naming
  `hydra.workflow.step`); the host's existing step/submit loop continues the
  workflow from its own cursor afterward, exactly as if it had never
  paused. If the operator identity is unknown (no `HYDRA_OPERATOR_ID`) or
  the minted capability is degraded (no `HYDRA_OPERATOR_KEY`), the resume
  REFUSES with `{ok: false, error: "operator_identity_required"}` before
  touching any state — it does not silently proceed with a degraded token.
  TheEights resolution is honestly reported as `eights_resolution:
  "deferred"`: `_NullDispatcher` cannot reach the shared ledger, so the
  matching row is left for the next live sweep
  (`hydra eights-hitl-reconcile` / `hydra reap --apply`) rather than being
  silently skipped or falsely claimed resolved.
