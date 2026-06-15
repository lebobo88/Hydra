---
id: mesh-topology-budget@1
bare_id: mesh-topology-budget
kind: spec
version: 1
title: "3D meshes are built to budget and engine-ready by construction"
owner_head: 3d-modeling-director
---
# Mesh topology & budget rubric

The Sculptor gates every returned 3D mesh on this. A mesh is **engine-ready by
construction** — built to the DCC contract's topology, budget, UV, LOD, and axis
rules — not "looks good in the viewport". Garland's `blender-model` self-checks
this before delivery; pair-programmer's `dcc-asset-validation@1` is the
engineering-side mirror.

Score 0..1 per cluster:

- **poly_budget**: within the declared tri budget; LOD ladder present and
  monotonic with transition distances (or Nanite justified on UE5).
- **topology**: quad-dominant; no n-gons on deforming/subdivided meshes; poles
  kept off deformation lines.
- **deformation_ready**: (rig-bound assets) even quad edge loops at every
  deforming joint; watertight / manifold so Blender bone-heat auto-weights solve.
- **uv_layout**: UVs packed, no unintended overlap, consistent texel density
  (±10%); lightmap UVs where required.
- **pbr_set**: material channels match the contract (albedo / ORM / normal /
  emissive); no engine-incompatible (Cycles-only) nodes on export.
- **transform_axis_scale**: scale=1 / rotation=0 applied; 1 unit = 1 m; pivot at
  the contract origin; correct up/forward axis preset for the target engine.
- **export**: exports to the contract format (FBX / glTF 2.0 / USD) and imports
  clean in the target engine.
- **provenance**: gen-AI meshes carry a valid C2PA signature/sidecar (cross-ref
  `ai-content-provenance`).

Outcome:
- pass: every applicable cluster ≥ 0.7 AND export imports clean.
- revise: any cluster in [0.5, 0.7) OR a missing LOD/UV/axis fix.
- fail: any cluster < 0.5 OR n-gons on a deforming mesh OR export fails engine import.
