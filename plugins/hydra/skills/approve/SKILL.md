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

Note (RCA path K, EIGHTS-RECORD-OUTCOME-RCA-2026-09-16 §7): `hydra.workflow.resume`
picks its transport from `HYDRA_ALLOW_DETACHED`, not from anything about the
workflow itself. With the gate set (`HYDRA_ALLOW_DETACHED=1`, automation-only),
it launches a DETACHED `hydra resume --live` and returns immediately
(`{ok, launched: true, pid, log}`); progress is only observable via
`hydra.status` / the trace. Without the gate (the normal interactive session),
it instead runs `hydra resume` SYNCHRONOUSLY, in-process, WITHOUT `--live`
(on `_NullDispatcher`) and returns the resolved gate plus the workflow's
resulting `status`/`pending_hitl` in-band. That in-process resume clears the
gate (lock, capability check, spool prune, TheEights resolve) but genuinely
stops at the attended hand-off — engineering (and any other host-bridged)
tasks are deferred back to the host rather than dispatched on the stub — so
the attended `step`/`submit` loop continues from the cursor afterward exactly
as if the workflow had never paused.
