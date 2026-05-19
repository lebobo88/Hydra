---
description: "Show recent Hydra workflows or the full state tree of a specific workflow."
argument-hint: "[<workflow_id>]"
model: haiku
---

# /hydra:status

```
/hydra:status                 # list recent workflows (latest first)
/hydra:status <workflow_id>   # print full trace + tasks + HITL state
```

Implementation: call `python -m hydra_core.cli status [<workflow_id>]` and render the output.
