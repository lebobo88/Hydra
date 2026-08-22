---
paths:
  - "hydra_core/**/*.py"
  - "mcp_servers/**/*.py"
  - "tests/**/*.py"
description: Pair-programmer harness requirements for Hydra runtime work.
---

# Engineering execution

Engineering work remains pair-programmer harness work. Start it through Hydra;
the only permitted code generator is the typed agent returned by the attended
cursor inside its isolated stage worktree. Do not replace the harness with a
native Claude Code subagent, Agent Team, worktree, dynamic workflow, or direct
file edit.

The harness records attempts, artifacts, verdicts, smoke status, finalization,
and budget spend. Run the relevant tests and report concrete evidence before
claiming completion. A failed or surfaced stage remains surfaced; never rewrite
that outcome in prose.
