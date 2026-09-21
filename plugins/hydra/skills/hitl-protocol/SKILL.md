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

LangGraph builds with `interrupt_before=["approval", "hitl_gate_dispatch", "hitl_gate_judge", "synthesis", "judge_synthesis", "plan_gate"]` (`build_supervisor` in `hydra_core/supervisor.py`; `plan_only` additionally interrupts before `"dispatch"`). `hitl_gate_dispatch`/`hitl_gate_judge` are F9's re-entry gates: dispatch/judge surface a resumable HITL there instead of falling straight through to postcheck/halt, and resuming clears the gate and re-enters the originating node. `judge_synthesis` pauses so a synthesis-stage judge verdict is reviewable before postcheck. Additional ad-hoc gates fire from inside `dispatch` when a squad emits a `HITL_REQUEST`.

| Gate | Reason codes |
|---|---|
| approval (planning → dispatch) | `budget_approval`, `high_risk`, `acceptance_criteria`, `over_budget`, `policy_breach`, `campaign_signoff` — `high_risk`/`acceptance_criteria` fire here only when the plan gate is NOT active (rigor `trivial`, a checkpoint predating the plan phase, or `HYDRA_PLAN_PHASE=0`); `over_budget` always fires here regardless (§6 stand-down deliberately excludes budget exhaustion) |
| plan_gate (dispatch → dispatch) | `plan_approval` — the DEFAULT for a high-risk/AC-qualifying workflow now that `HYDRA_PLAN_PHASE` ships on: fires whenever rigor is not `trivial` and the flag is not explicitly disabled with `HYDRA_PLAN_PHASE=0` |
| hitl_gate_dispatch (dispatch → dispatch) | any `HITL_REQUEST` a squad emits mid-dispatch (`hitl_return_node="dispatch"`); resuming clears the gate and re-enters dispatch |
| hitl_gate_judge (judge → judge) | any `HITL_REQUEST` a per-squad judge emits (`hitl_return_node="judge_per_squad"`); resuming clears the gate and re-enters judging |
| synthesis (dispatch → postcheck) | `schema_conflict`, `dissent_unresolved` |
| judge_synthesis (synthesis → postcheck) | a synthesis-stage cross-vendor judge verdict pending review before postcheck |
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
  This event is now genuinely emitted for every force-dispatch. It was promised
  here and in `resume/SKILL.md` long before the engine produced it, so a
  force-dispatch past any gate used to leave no trace at all.
- `/hydra:resume <wf> --modify-plan --critique-ref <path-or-memoryref>` →
  bump `plan_revision`, set `plan_status="authoring"`, seed one planning task
  carrying the critique and the prior plan's id as `supersedes`, re-enter the
  graph. Valid **only** at `plan_gate`. Bounded by `HYDRA_PLAN_MAX_REVISIONS`
  (default 2); at the ceiling the gate re-renders offering only `approve` and
  `abort`.
  The critique travels as a **reference**, never as `--option`: that field is
  capped at 200 characters of a restricted class because it guards a string
  bound for a subprocess argument list, so real prose would be truncated or
  refused. The referenced file must resolve inside the project root.

### At `plan_gate` specifically

- `approve` → `plan_status="approved"`, the barrier lifts, and the plan's steps
  become tasks. Steps are materialised **here, on approval only** — never at
  draft time, because the task list is append-only and a rejected or superseded
  revision's steps would otherwise stay selectable forever.
- `reject` → `plan_status="rejected"`, workflow parks at `surfaced`. There is
  deliberately **no automatic re-plan**: an engine that authors a new plan the
  moment one is rejected is a loop the operator cannot stop. The rejected plan
  stays on disk marked rejected — it is evidence, not waste.
- `force-dispatch` → dispatch proceeds without plan approval, and that becomes
  evidence rather than a gap: a `policy_override` event, `plan_status="bypassed"`,
  and a governance note appended to the plan artifact. The operator owns the
  risk, as with any gate; the point is that the bypass is recorded.

## What NOT To Do

- Do NOT swallow `pending_hitl` to "keep things moving." That defeats the audit trail.
- Do NOT route the HITL into an LLM "approver agent." Humans only.
- Do NOT clear `hitl_history`. It is append-only.
