---
description: "Scaffold a new squad pack: creates squads/<slug>/squad.yaml from a template + registers keyword fingerprints."
argument-hint: "<slug> --industries <a,b,c> [--entrypoint mcp|subprocess|agent-impersonation|claude-skill|stub]"
model: sonnet
---

# /hydra:add-squad

Adds a new modular squad to Hydra's registry. After running, the squad is auto-discovered on the next `/hydra:squads`.

## Steps

1. Validate `<slug>` is kebab-case and not already taken.
2. Create `squads/<slug>/squad.yaml` from `templates/squad.yaml.tpl` with the supplied industries + entrypoint default = `stub`.
3. Optionally append keyword fingerprints to `hydra_core/router.py:_KEYWORDS` (use `--keywords "word1,word2"`).
4. Print a CONTRIBUTING-SQUADS.md-style activation checklist: add agents, declare tools, wire MCP/skill, write rubrics, flip entrypoint from `stub` to live.

## Example

```
/hydra:add-squad procurement --industries procurement,supply-chain --keywords "rfp,vendor,po,purchase order"
```

See `CONTRIBUTING-SQUADS.md` for the full activation checklist.
