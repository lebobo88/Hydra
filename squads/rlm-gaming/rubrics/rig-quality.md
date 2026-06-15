---
id: rig-quality@1
bare_id: rig-quality
kind: spec
version: 1
title: "Rigs deform correctly, normalize cleanly, and export to spec"
owner_head: animation-director
---
# Rig quality rubric

The Choreographer gates every returned rig/animation on this. Rig quality is
**measured, not eyeballed** — explicit weight/hierarchy/gimbal/export metrics, not
"looks right". Garland's `blender-rig` self-checks this before delivery;
pair-programmer's `dcc-asset-validation@1` is the engineering-side mirror.

Score 0..1 per cluster:

- **hierarchy**: single root at origin; unique bone names; no cycles; deform vs
  control separation; twist bones on long segments where needed.
- **naming_roll**: `.L/.R` symmetric naming; consistent roll (X-across / Y-along /
  Z-up) so bends stay planar and IK poles behave.
- **weights**: per-vertex Σw = 1 (±1e-5); ≤4 influences; most vertices 1–3
  influences; no distant-bone weights; pruned + normalized.
- **ik_constraints**: IK chains have pole targets + biomechanical limit
  constraints; FK/IK switch drives constraint influence 0..1.
- **no_gimbal**: no Euler discontinuity (> 120°/frame) on keyed channels;
  gimbal-risk bones in quaternion mode.
- **transform**: scale identity applied; no animated / non-uniform bone scale.
- **export**: single root at origin, leaf bones off, baked animation, engine axis
  preset correct (UE Z-up/X-forward; Unity Humanoid; UsdSkel); imports clean.
- **provenance**: gen-AI motion/rig carries a valid C2PA signature/sidecar
  (cross-ref `ai-content-provenance`).

Outcome:
- pass: every applicable cluster ≥ 0.7 AND export imports clean.
- revise: any cluster in [0.5, 0.7) OR a localized weight/gimbal fix.
- fail: any cluster < 0.5 OR multiple roots / dup names OR animated bone scale OR export fails engine import.
