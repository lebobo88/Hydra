---
description: "Approve a paused HITL gate and resume the supervisor graph."
argument-hint: "<workflow_id> [--note '...']"
model: sonnet
---

# /hydra:approve

Adopt `hydra-hitl-gate`. Look up `HydraState.pending_hitl` for `<workflow_id>`. Append an approval entry to `hitl_history`, clear `pending_hitl`, and resume the LangGraph checkpoint.

Operationally:

1. Verify the workflow exists and is in `phase ∈ {approval, synthesis, surfaced}`.
2. Print the original HITL_REQUEST so the operator can re-verify what they're approving.
3. On confirmation, write the approval and resume.

For rejection or budget mutation, use `/hydra:resume` instead.
