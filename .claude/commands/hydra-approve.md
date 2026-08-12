---
description: "Approve a paused HITL gate and resume the supervisor graph."
argument-hint: "<workflow_id> [--note '...']"
model: sonnet
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

Note (G4): resuming a **detached** workflow re-detaches `hydra resume --live`
in the background, which is gated by `HYDRA_ALLOW_DETACHED=1` — without the
gate the server returns `error: "detached_disabled"`. Attended workflows are
unaffected: approval continues in-process and the attended `step`/`submit`
loop picks up from the cursor.
