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

Operationally:

1. Query `python -m hydra_core.cli status <workflow_id>` and render the pending
   HITL request exactly enough for the operator to review.
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
  `hydra resume --gate-only` SYNCHRONOUSLY, in-process, WITHOUT `--live` (on
  `_NullDispatcher`), and returns the resolved gate plus the workflow's
  resulting `status`/`pending_hitl` in-band. This resolves the gate (lock,
  operator-capability mint+verify, spool prune, per-action state patch) but
  NEVER re-enters the compiled graph — no `sup.invoke`, no `node_dispatch`,
  no squad of any kind runs, not even on the stub. The response says so
  explicitly (`graph_reentered: false`); the attended `step`/`submit` loop
  continues from the cursor afterward, exactly as if the workflow had never
  paused. This response body is ADDITIVE, not byte-for-byte identical to
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
  narrow live call (list + resolve, never a replay or spool drain); the
  response reports `eights_resolution: "resolved"` on success or
  `eights_resolution: "unavailable"` (with a reason) when TheEights cannot
  be reached — never a hardcoded "deferred". A retry after an interrupted
  gate-only resume reconciles any stale spooled HITL request for the
  already-resolved gate.

  `recover-stalled-stage` (see `/hydra:resume`) is refused outright on this
  route, before `_run_cli_json` ever spawns the resume child process,
  because it is a LIVE operation, not a gate resolution — only the detached
  CLI (`HYDRA_ALLOW_DETACHED=1`) may run it.

  **Timeout and retry:** the MCP transport runs `hydra resume --gate-only`
  as a real, synchronous CHILD PROCESS (`_run_cli_json`, `subprocess.run`)
  bounded by a 30-second timeout (`HYDRA_RESUME_TIMEOUT_S`, default `30`).
  The child itself never dispatches — it does mint+verify, a checkpoint
  patch, a spool prune, and one narrow live TheEights list+resolve call, all
  on `_NullDispatcher` for graph re-entry purposes — so 30s is generous
  headroom for cold start, not an expected duration; a timeout usually means
  the child process itself is stalled, not that the gate is slow. On a
  timeout or any other failure, simply retry the same
  `hydra.workflow.resume` call (or the equivalent `cli resume --gate-only`
  invocation) with the same `workflow_id`/`action`/`option`: the route is
  idempotent — a retry after a resolution that already landed on the
  checkpoint reconciles any stale spooled HITL request rather than
  double-applying the gate or erroring.
