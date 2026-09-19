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
3. Call `hydra.workflow.resume` with `action: "approve"` (or
   `python -m hydra_core.cli approve <workflow_id>`). Render the authoritative
   response and stop again if it returns another gate.

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
  `eights_resolution` are new fields). If the operator identity is unknown
  or the minted capability is degraded (missing `HYDRA_OPERATOR_ID` /
  `HYDRA_OPERATOR_KEY`), the approve REFUSES up front (`{ok: false, error:
  "operator_identity_required"}`) rather than proceeding on an unverifiable
  token — this check applies to every mutating resume action reachable
  through `/hydra:resume` too, not only `approve`. TheEights resolution is
  reported honestly as `eights_resolution: "deferred"` — it is swept on the
  next live call, not resolved from the stub. A retry after an interrupted
  gate-only resume reconciles any stale spooled HITL request for the
  already-resolved gate.

  `recover-stalled-stage` (see `/hydra:resume`) is refused outright on this
  route, before any subprocess runs, because it is a LIVE operation, not a
  gate resolution — only the detached CLI (`HYDRA_ALLOW_DETACHED=1`) may
  run it.
