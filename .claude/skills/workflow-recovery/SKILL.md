---
name: workflow-recovery
description: "Read-only Hydra workflow status and recovery diagnosis. Use when a workflow paused, timed out, surfaced, or needs operator-facing next steps."
context: fork
agent: Explore
background: false
allowed-tools: Read Glob Grep Bash(python -m hydra_core.cli status *)
---

# Hydra workflow recovery

Diagnose workflow `$ARGUMENTS` without changing files, state, budget, or HITL.

<authority>
Hydra's checkpoint, trace, pending HITL envelope, and attended cursor are the
only authoritative workflow state. This skill provides evidence and recovery
guidance; it never approves, resumes, clears, or edits state.
</authority>

<method>
1. Run `python -m hydra_core.cli status <workflow_id>` when an id is supplied.
2. Inspect the corresponding `.hydra/<workflow_id>/trace.jsonl` and attended
   cursor only when present.
3. Report phase, pending HITL, task states, budget, last terminal/error event,
   and the exact supported next operator command.
4. If evidence is incomplete, state that instead of inferring a recovery.
</method>

<output_contract>
Return `Evidence`, `Current state`, `Safe next action`, and `Do not do`.
Never recommend bypassing `/hydra:approve`, `/hydra:resume`, envelope
validation, the pair-programmer stage protocol, or a surfaced outcome.
</output_contract>
