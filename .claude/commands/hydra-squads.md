---
description: "List discovered squad packs (registry resolution: project → user → built-in)."
model: haiku
---

# /hydra:squads

Run `python -m hydra_core.cli squads` and render the JSON registry: slug, entrypoint, accepted/emitted envelope types, agent count, industry tags.

Use this to verify a new squad pack is discovered before invoking `/hydra:run` against it.
