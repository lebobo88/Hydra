---
description: "Approve a paused HITL gate and resume the supervisor graph."
argument-hint: "<workflow_id> [--note '...']"
model: sonnet
disable-model-invocation: true
---

# /hydra:approve

<authority_boundary>
This command is only the native Claude Code operator interface for an explicit
human decision. Hydra's resume API is the only authority allowed to validate a
gate, append HITL history, patch checkpoint state, and continue the graph.
</authority_boundary>

**Prerequisite:** the attended route reads the operator identity from the
`hydra_control` MCP server's OWN environment, not from the operator's client
shell. Both `HYDRA_OPERATOR_ID` and `HYDRA_OPERATOR_KEY` must be set in the
`hydra_control` entry's `env` block in `~/.hydra/backends.json`, and the
`hydra_control` server must be RESTARTED after changing them — a variable
exported in the operator's own shell never reaches the already-running
server process. Symptom when either is missing: every attended `approve` (and
every other mutating resume action) returns `{ok: false, error:
"operator_identity_required"}`.

Operationally:

1. Query `python -m hydra_core.cli status <workflow_id>` and render the pending
   HITL request exactly enough for the operator to review. At a `plan_gate`,
   `plan_detail` carries the judge's `verdict_outcome` and `judge_vendor` PLUS
   (Hydra#69 part 3, D3) `verdict_critique` (the judge's critique text,
   truncated to 2000 chars) and `verdict_plan_revision` (the plan revision the
   verdict was scored against) — render all four together, not outcome/vendor
   alone, so the operator sees WHY the judge reached its outcome and which
   revision it judged.
2. Obtain the operator's explicit confirmation. Never infer it from prior text.
3. Call `hydra.workflow.resume` with `action: "approve"` (or, from a shell,
   `python -m hydra_core.cli resume <workflow_id> --action approve
   --gate-only`). Render the authoritative response and stop again if it
   returns another gate.

   Do **not** use `python -m hydra_core.cli approve <workflow_id>` for an
   attended workflow: that subcommand has no `--gate-only` and always
   re-enters the graph on `_NullDispatcher` (a stub dispatch, not the real
   attended step/submit loop) — the stub-dispatch gap. It exists only for
   the legacy non-attended path.

Do not directly edit `HydraState`, `hitl_history`, a checkpoint, or a trace.

For rejection or budget mutation, use `/hydra:resume` instead.

Note (RCA path K, EIGHTS-RECORD-OUTCOME-RCA-2026-09-16 §7; RESOLVE-GATE-ONLY
follow-up): `hydra.workflow.resume` picks its transport from
`HYDRA_ALLOW_DETACHED`, not from anything about the workflow itself.

- With the gate set (`HYDRA_ALLOW_DETACHED=1`, automation-only), it launches
  a DETACHED `hydra resume --live` and returns immediately
  (`{ok, launched: true, pid, log}`); progress is only observable via
  `hydra.status` / the trace.
- Without the gate (the normal interactive session), it runs
  `hydra resume --gate-only` as a real, synchronous CHILD PROCESS spawned by
  `hydra_control` (`_run_cli_json`, `subprocess.run`), WITHOUT `--live` (on
  `_NullDispatcher`), and waits for it in-band, returning the resolved gate
  plus the workflow's resulting `status`/`pending_hitl`. This resolves the gate (lock,
  operator-capability mint+verify, spool prune, per-action state patch) but
  NEVER re-enters the compiled graph — no `sup.invoke`, no `node_dispatch`,
  no squad of any kind runs, not even on the stub. The response says so
  explicitly (`graph_reentered: false`); the attended `step`/`submit` loop
  continues from the cursor afterward. This is NOT "exactly as if the
  workflow had never paused" at a `plan_gate`: approving there materialises
  the plan's steps into real `TaskState` entries DURING the gate-only
  resolve itself (`hydra_core.cli` calls `hydra_core.supervisor.
  materialise_plan_steps` and folds its patch into the SAME checkpoint write
  that clears the gate — Hydra#69 part 1), not on a later graph re-entry, so
  a workflow with a plan is materially different in shape immediately after
  this resolve than it was before the gate was ever hit. A `modify-budget`
  resolution at `plan_gate` behaves differently again: it does not approve
  the plan — it re-files the SAME `plan_gate` (the workflow stays parked
  there with the adjusted budget) rather than advancing past it. This
  response body is ADDITIVE, not byte-for-byte identical to
  the pre-gate-only shape (`gate_only` and, when applicable,
  `eights_resolution` are new fields). Identity is checked TWICE: a
  pure, state-free precheck (operator id known + a signing key present)
  runs BEFORE the resume lock is acquired and before the checkpoint is
  opened — so an unauthenticated call creates no `.hydra/<workflow>/`
  directory and no checkpoint database at all — and the real mint+verify
  (bound to the actual pending gate) runs again right after the checkpoint
  loads. If either fails (unknown operator, no `HYDRA_OPERATOR_ID`, no
  signing key `HYDRA_OPERATOR_KEY`, or a capability that fails
  verification), the approve REFUSES up front (`{ok: false, error:
  "operator_identity_required"}`) rather than proceeding on an unverifiable
  token — this check applies to every mutating resume action reachable
  through `/hydra:resume` too, not only `approve`. Once the gate clears
  locally, TheEights' matching pending ticket is resolved NOW with one
  narrow live call (list + resolve, never a replay or spool drain), bounded
  by its OWN inner deadline (`HYDRA_GATE_ONLY_EIGHTS_TIMEOUT_S`, default
  `8` seconds — see **Timeout and retry** below); the response reports
  `eights_resolution: "resolved"` on success or `eights_resolution:
  "unavailable"` (with a reason, e.g. `"deadline"` or
  `"eights_unreachable"`) when TheEights cannot be reached in time — never a
  hardcoded "deferred". A retry after an interrupted or timed-out gate-only
  resume reconciles the already-resolved gate against TheEights' ledger
  (see below), not just the local spool.

  `recover-stalled-stage` (see `/hydra:resume`) is refused outright on this
  route, before `_run_cli_json` ever spawns the resume child process,
  because it is a LIVE operation, not a gate resolution — only the detached
  CLI (`HYDRA_ALLOW_DETACHED=1`) may run it.

  **Timeout and retry:** `hydra_control` runs `hydra resume --gate-only` as
  a real, synchronous CHILD PROCESS (`_run_cli_json`, `subprocess.run`) and
  waits for it in-band — this is a real subprocess every time, never an
  in-process call. The WHOLE child is bounded by an outer timeout
  (`HYDRA_RESUME_TIMEOUT_S`, default `45` seconds). Inside that child, the
  one live TheEights list+resolve call described above has its own,
  separate, much tighter inner deadline (`HYDRA_GATE_ONLY_EIGHTS_TIMEOUT_S`,
  default `8` seconds) — deliberately far below the outer bound, so the
  inner deadline is what actually governs a slow TheEights daemon, not the
  outer kill. Everything else the child does (mint+verify, a checkpoint
  patch, a spool prune) is sub-second, so the two timeouts serve different
  purposes:
  - If the inner (8s) TheEights deadline fires: the child still finishes
    normally and returns `eights_resolution: "unavailable"` (reason:
    `"deadline"`) — the local gate is ALREADY cleared by this point, so
    nothing is lost; only TheEights' own ledger entry is left unresolved.
  - If the outer (45s) child timeout fires instead, the child itself is
    genuinely stalled (not merely a slow TheEights daemon, which the inner
    deadline already absorbs) and `hydra_control` reports a timeout/failure
    for the whole call.

  Either way, retry the identical `hydra.workflow.resume` call (same
  `workflow_id`/`action`/`option`): the route is idempotent. A retry lands
  on the no-pending-gate path (the checkpoint already shows the gate
  cleared) and reconciles TheEights' ledger for that same gate with the
  same bounded call — reporting `eights_resolution: "resolved"` if a
  pending ticket was found and resolved, or `"none_pending"` if TheEights
  already shows no matching ticket (already resolved by an earlier attempt,
  or nothing was ever pending) — never treating "no ticket" as an error. It
  also reconciles any stale spooled HITL request left by a prior call that
  was interrupted between its checkpoint patch and its spool prune.

  If the outer window is exceeded repeatedly, or you want to confirm state
  directly rather than trusting the transport's own report, inspect the
  workflow with `hydra status <workflow_id>` / `/hydra:status` — it reads
  the checkpoint directly and does not depend on the resume transport at
  all. Do **not** use `hydra.workflow.step` for this: it opens the next
  attended engineering stage and MUTATES the workflow rather than merely
  reporting on it.

  **Known limitation:** on the inner (8s) TheEights deadline, the resume
  child makes a best-effort, time-bounded attempt to close the live MCP
  session that call was using (so a slow-but-not-wedged TheEights daemon it
  spawned doesn't leak). On Windows, the underlying daemon PROCESS can
  still outlive the resume child regardless — Windows does not tie a
  child's lifetime to its parent's, and this fix deliberately does not
  attempt a process-tree sweep to force it down (a prior attempt at that
  elsewhere in this ecosystem had to be withdrawn because it could kill an
  unrelated process that later reused the same pid).
