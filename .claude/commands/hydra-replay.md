---
description: "Replay a past workflow from checkpoint (LangGraph checkpoint → optionally pinned to original model/CLI versions)."
argument-hint: "<workflow_id> [--from-phase intake|planning|dispatch|...] [--swap-model <id>]"
model: sonnet
---

# /hydra:replay

Reconstruct the full prompt set + envelope chain + model/CLI versions for a past workflow so it can be replayed reproducibly.

Implementation: load the LangGraph checkpoint at `~/.hydra/checkpoints.db` keyed by `<workflow_id>`. Reapply from `--from-phase` (default: intake). Use `--swap-model` to test a different model on the same prompts (regression / cost study).

Mirrors `/pp:replay` from the pair-programmer harness; the trace file remains JSONL-compatible.
