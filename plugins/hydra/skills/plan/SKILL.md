---
name: plan
description: "The planning squad's own runbook: Pyrrha (plan-author) → Momus (plan-critic) → Theseus (plan-scribe) gate a PLAN artifact before any squad is dispatched. Read this before touching squads/planning/ or before wiring the engine to seed a planning task (P5)."
---

# Plan

## What this is (and is not) yet

This skill documents the `planning` squad pack's contract. As of P4 of the
planning-phase build, the pack is **discovered but never selected**: nothing
in `hydra_core/router.py` or `hydra_core/supervisor.py` seeds a planning
task, routes to `planning`, or authors a plan. That wiring is P5. Until then,
this skill's runbook describes intent, not a live path — do not treat it as
license to hand-author a plan artifact inline from this skill, from
`/hydra:run`, or from any other command. The whole point of a dedicated
`plan-scribe` gate (below) is that nothing else writes the plan file.

## Why `claude-native`

`squads/planning/squad.yaml` declares `entrypoint: claude-native`, the ONLY
entrypoint `node_dispatch` (`hydra_core/squad_node.py`) defers
UNCONDITIONALLY to the attended host cursor. `agent-impersonation` and
`claude-skill` packs are deferred to the host only when live dispatch is
true; under a test harness or a null dispatcher they fall through to the
legacy in-graph path, where a supervisor LLM turn would have to improvise the
squad's output — i.e. fabricate a plan. For an artifact that frames every
downstream task and HITL gate, a fabricated one is the worst available
failure. `claude-native` cannot take that path: with no attended host it
returns `status="deferred_to_host"` honestly instead.

## The three-head pipeline

1. **Pyrrha (`plan-author`, gatekeeper)** — decomposes the routed goal into a
   draft PLAN: steps, each with an owner (squad) and a success statement
   phrased as a test. Refuses to decompose a goal whose success cannot be
   stated as a test.
2. **Momus (`plan-critic`, gatekeeper)** — reads the draft for the seam that
   will not hold: a step with no owner, no acceptance criterion, or
   insufficient dependency accounting. Blesses forward or returns the plan
   to plan-author with the fault named. Refuses to bless a plan whose steps
   have no owner or no acceptance criterion.
3. **Theseus (`plan-scribe`, execute)** — renders the approved PLAN through
   `hydra_core.artifact_store.write_repo_artifact` (P2) against the TARGET
   repo, not Hydra's own tree. Diffs the render against the approved
   envelope before writing; refuses to write a plan file that disagrees with
   the envelope it came from.

No head in this pipeline blesses its own work: plan-author does not
self-approve, plan-critic does not rewrite in place, plan-scribe does not
add or soften content the critic did not see.

## The two landmines this pack must never trip

- **`hitl_required`** on any gate in `squads/planning/squad.yaml` must stay
  `false`, and every agent's `hitl_trigger` must stay `false`.
  `_task_is_high_risk` (`hydra_core/supervisor.py`) checks
  `any(g.hitl_required for g in pack.gates)` across the **full task set**,
  not just selected squads — one `true` here would silently convert every
  planned Hydra workflow to `requires_human_approval=True,
  reason="high_risk"`. See
  `tests/test_planning_squad_containment.py::test_no_planning_gate_declares_hitl_required`.
- **`priority`**: whenever P5 wires a task seeded to this squad, that task
  must be `P2`, never `P0`/`P1` — either priority alone trips
  `_task_is_high_risk` regardless of the pack's gates.

## Containment (why `planning` cannot be routed to today)

`hydra_core/router.py` denies the slug in both the deterministic
keyword/industry pass and the LLM-fallback pass (`RESERVED_META_SQUADS`).
`hydra_core/supervisor.py` denies it at both `--squad`/goal-text
force-selection sites. `node_synthesis` filters a `planning`-origin envelope
out of the squad-voice grouping (the plan is the frame of a decision record,
not one of its voices), covering both emission points: an in-graph dispatch
envelope and `_materialize_attended_results`'s own DECISION_RECORD. See
`tests/test_planning_squad_containment.py` for the discriminating evidence
per guard.

## Reference

- `squads/planning/squad.yaml`, `squads/planning/heads.yaml`
- `plugins/hydra/agents/plan-author.md`, `plan-critic.md`, `plan-scribe.md`
- `hydra_core/native_packs.py` (`NATIVE_PACKS["planning"]`)
- `hydra_core/plan_artifact.py`, `hydra_core/artifact_store.py` (P2 — the
  writer this pack's plan-scribe will call once P5 wires dispatch)
