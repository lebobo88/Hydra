---
name: squad-registry-discovery
description: "How Hydra discovers, resolves, and routes to squad packs. Read this before adding/removing/modifying a squad."
---

# Squad Registry Discovery

Hydra's modularity hinges on one rule: **a squad is any directory under `squads/` (project-local) or `~/.hydra/squads/` (user-global) that contains a `squad.yaml`**. Discovery runs at supervisor construction time via `hydra_core.squad_loader.discover_squads`.

## Resolution Order

1. `<project-root>/squads/<slug>/squad.yaml` (project wins — used for project-specific overrides)
2. `~/.hydra/squads/<slug>/squad.yaml` (user global)
3. (built-in — currently empty; reserved)

A project-level slug shadows a user-level slug of the same name.

## Required `squad.yaml` Keys

```yaml
name: <human readable>
description: <multi-line>
source_pack: <abs path or null>                  # references the underlying repo if any
entrypoint: mcp|subprocess|agent-impersonation|claude-skill|stub
industries: [tag1, tag2]                          # router signal
agents:    [{slug, role, authority, model_hint?}]
tools:     [{name, mcp_server?, privilege}]
accepts:   [<envelope_type>, ...]                 # or ["*"]
emits:     [<envelope_type>, ...]
gates:     [{rubric_id, hitl_required?, when?}]
invoke:    {entrypoint-specific config}
```

## Adding A Squad — Minimum Steps

1. `mkdir squads/<slug>` and write `squad.yaml`.
2. (Optional) add keyword fingerprints in `hydra_core/router.py::_KEYWORDS[<slug>]`.
3. `/hydra:squads` to verify discovery.
4. `/hydra:run "<test goal>" --squad <slug>` to smoke-test.
5. Flip `entrypoint` from `stub` to the real adapter once tools/MCP are wired.

See `CONTRIBUTING-SQUADS.md` for the full activation checklist.
