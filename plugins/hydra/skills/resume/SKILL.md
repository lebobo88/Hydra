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

Companion to `/hydra:approve`. Drives non-approve resume paths. The
descriptions below are the LEGACY (non-attended, `--live`) CLI behavior —
re-entering the graph/dispatch. On the attended default (gate-only, see the
Note below), every one of these actions instead applies its state patch and
STOPS without ever calling `sup.invoke`; nothing here "dispatches" or
"re-enters" on that route:

- `--reject`: mark the workflow `surfaced`, write a rejection note.
- `--modify-budget 250`: update `state.budget.budget_usd`; legacy path re-enters dispatch, attended gate-only path only patches the budget and stops.
- `--force-dispatch`: dispatch even though a gate failed (logs a `policy_override` event; operator owns the risk) on the legacy path; the attended gate-only path records the same `policy_override` event and per-action patch but never actually re-enters dispatch. At `plan_gate` this additionally stamps `plan_status="bypassed"` and appends a governance note to the plan artifact, so proceeding without an approved plan is evidence rather than a gap.
- `--squads engineering,garland`: replace `selected_squads`; legacy path re-plans, attended gate-only path only patches `selected_squads` and stops.
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
  paused. This JSON body is ADDITIVE relative to the pre-gate-only shape —
  `gate_only` and (when applicable) `eights_resolution` are new fields, not
  a byte-for-byte-identical document.

  The operator-identity check covers EVERY action that can mutate
  checkpoint state or the spool, including `--reject` (not only
  `approve`/`force-dispatch`/`modify-budget`/`change-squads`/`modify-plan`).
  Identity is verified TWICE, at two different points: a pure, state-free
  precheck (operator id known + a signing key present) runs BEFORE the
  resume lock is even acquired and before the checkpoint is opened, so an
  unauthenticated call creates nothing on disk at all (no `.hydra/<workflow>/`
  lock directory, no checkpoint database); the real mint+verify (bound to
  the actual pending gate) runs again immediately after the checkpoint
  loads, before any state mutation. If either check fails — unknown
  operator (no `HYDRA_OPERATOR_ID`), no signing key (no
  `HYDRA_OPERATOR_KEY`), or a capability that fails verification — the
  resume REFUSES with `{ok: false, error: "operator_identity_required"}`
  before touching any state — it does not silently proceed with a degraded
  token. Once the gate clears locally, TheEights' matching pending ticket is
  resolved NOW with one narrow live call (list + resolve, never a replay or
  spool drain); the result reports `eights_resolution: "resolved"` on
  success or `eights_resolution: "unavailable"` (with a reason) when
  TheEights cannot be reached — never a hardcoded "deferred", and never a
  spooled retry on this route. A retry after an interrupted gate-only resume
  (killed between its checkpoint patch and its spool prune) reconciles the
  stale spooled HITL request for the already-resolved gate rather than
  leaving it orphaned.

  `recover-stalled-stage` is the ONE resume action this route REFUSES
  outright, before any subprocess runs (`{ok: false, error:
  "recovery_is_live_operation"}`): it is a LIVE operation — a real
  `MCPStdioDispatcher` that can replay a pp verdict, run
  smoke/finalization, and merge code — not a gate resolution. Only the
  DETACHED route (`hydra resume --live --action recover-stalled-stage`,
  requires `HYDRA_ALLOW_DETACHED=1`) may run it.

  `--gate-only` and `--live` are mutually exclusive on the `hydra resume`
  CLI — never pass both. `--live` builds a real `MCPStdioDispatcher` and
  starts a background eights spool drain, exactly the live side effects
  the gate-only route promises never to trigger; the CLI refuses the
  combination before doing any work (argparse-level and, defensively, a
  matching runtime check).

  **Timeout and retry:** the MCP transport (`_run_cli_json` in
  `mcp_servers/hydra_control/server.py`) runs `hydra resume --gate-only` as
  a synchronous CHILD PROCESS (`subprocess.run([sys.executable, "-m",
  "hydra_core.cli", ...])`) and waits for it in-band — this is a real
  subprocess, not an in-process call; "no subprocess" describes only the
  work the child itself does once running (mint+verify, a checkpoint patch,
  a spool prune, and now one narrow live TheEights list+resolve call — no
  dispatch, no live squad/engineering work, no replay). That whole call is
  bounded by a 30-second synchronous timeout
  (`HYDRA_RESUME_TIMEOUT_S`, default `30`); a timeout signals a stalled
  child process, not a slow gate. Retry the identical `hydra.workflow.resume`
  call (same
  `workflow_id`/`action`/`option`) on any failure; the route is
  idempotent, including reconciling a stale spooled HITL request left by
  a prior call that was interrupted between its checkpoint patch and its
  spool prune.
