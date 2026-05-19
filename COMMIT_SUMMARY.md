# Commit Summary

This is a paste-ready commit message body for the judge plane + eights wiring
landing in `hydra_core/`. Title kept under 70 chars; details in the body.

---

## Suggested commit title

```
feat(judge): orchestrator-wide cross-vendor judge plane + eights wiring
```

## Suggested commit body

```
Port the pair-programmer rubber-duck-across-vendors pattern from a single-
purpose code-review tool into a generic orchestrator judge plane Hydra
applies to every non-engineering squad (executive, creative, healthcare,
legal-compliance, sales-gtm, research-ds, customer-support) plus the
post-synthesis Cathedral artifact. PP retains ownership of engineering
verdicts; envelopes carrying a `pp_verdict` are skipped (no double-judging).

New surface (hydra_core/judge/):
  - schemas.py        JudgeVerdict envelope, RubricRef
  - registry.py       13 versioned rubrics (@1, immutable bodies)
  - router.py         tier policy: cross_vendor | same_vendor | skip
                      + content-aware regex escalation
  - dispatcher.py     CritiqueClient protocol; NoOp + pragmatic-pass guard
  - mcp_client.py     MCPCritiqueClient → pp-codex / pp-gemini critique
  - borda.py          rank-aggregation for best-of-N
  - reflexion.py      ×1 retry packet
  - best_of_n.py      judge_and_rank + best_of_n_run
  - policy.py +.yaml  enabled_squads allowlist + hitl_on_fail severities

Supervisor lifecycle gains two nodes:
  intake → planner → (approval) → dispatch → judge_per_squad
                                            → synthesis → judge_synthesis
                                            → postcheck → done

judge_per_squad does three things on each unjudged envelope:
  1. score against router-bound rubrics (constitution-alignment always
     included)
  2. revise → Reflexion ×1 re-dispatch with critique embedded
  3. fail on HITL-severity rubric → surface pending_hitl(policy_breach)

judge_synthesis is always cross_vendor + synthesis-coherence@1.

Best-of-N (squad.yaml: best_of_n: N≥2): executive + creative produce N
candidates, Borda-rank them, archive losers as `kind: bon_losers`.

TheEights attestation adapter (hydra_core/eights/attestation.py):
  Best-effort calls into the eights-daemon at intake (constitution_attest +
  ceiling_tick), each envelope emit (envelope_record), and each HITL
  escalation (hitl_request). Calls no-op cleanly when the daemon is not
  registered in .mcp.json — wires the call sites today so they light up the
  moment the daemon is deployed.

Caveat fixes from the live smoke:
  - host_pickup_pending envelopes (Claude Code subagent fulfils out-of-band)
    are tagged and skipped by the judge plane instead of getting zero-score
    placeholder verdicts. Best-of-N short-circuits when all candidates are
    pending.
  - hydra run --no-checkpoint flag forces pure-Python supervisor for smoke
    tests and dev loops (production runs still use the checkpointed graph).

.mcp.json:
  + pp-codex / pp-gemini server entries
    (node dist/index.js mcp-codex / mcp-gemini)

AGENTS.md:
  + Hard Rule 8 — never modify a rubric's @N body; create @N+1.

State / schema changes:
  + HydraState.verdicts: list[dict] (append-only)
  + HydraState.phase literal extended with judge_per_squad / judge_synthesis
  + SquadResult.host_pickup_pending: bool
  + SquadPack.best_of_n: int (executive + creative set to 3)
  + JUDGE_VERDICT registered in SCHEMA_REGISTRY

Tests: 216 passing (146 baseline + 70 new). Live smoke confirmed substantive
cross-vendor disagreement (Gemini single_voice: 0.2 → revise vs Codex 0.96
→ pass on the same synthesis output) and clean trace events for
judge.invoked / judge.verdict / judge.borda / judge.skipped /
judge.bon_all_pending.
```

---

## File-by-file delta

**New files** (`hydra_core/judge/`):
- `__init__.py` — public surface
- `schemas.py` — `JudgeVerdict`, `RubricRef`
- `registry.py` — 13 versioned rubrics
- `router.py` — tier policy + content escalation
- `dispatcher.py` — `CritiqueClient`, `NoOpCritiqueClient`, pragmatic-pass guard, `_wrap_untrusted`
- `mcp_client.py` — `MCPCritiqueClient` + PP response normalizer
- `borda.py` — rank aggregation
- `reflexion.py` — `package_retry` + `MAX_RETRY_INDEX=1`
- `best_of_n.py` — `judge_and_rank`, `best_of_n_run`, `BestOfNOutcome`
- `policy.py` + `policy.yaml` — loader + defaults

**New file** (`hydra_core/eights/`):
- `attestation.py` — `EightsAttestor` best-effort daemon caller

**Modified**:
- `hydra_core/supervisor.py` — two new nodes, best-of-N branch in dispatch, eights attestation hooks, force_pure_python kwarg
- `hydra_core/state.py` — phase literals + `verdicts` field
- `hydra_core/schemas.py` — `JUDGE_VERDICT` registration (cycle-safe via judge/__init__.py)
- `hydra_core/squad_loader.py` — `best_of_n` pack field
- `hydra_core/squad_node.py` — `SquadResult.host_pickup_pending`; impersonation + claude-skill paths set it
- `hydra_core/cli.py` — `--no-checkpoint` flag, MCPCritiqueClient wired in live mode
- `.mcp.json` — pp-codex / pp-gemini entries
- `AGENTS.md` — Hard Rule 8
- `squads/executive/squad.yaml` — `best_of_n: 3`
- `squads/creative/squad.yaml` — `best_of_n: 3`
- `squads/healthcare/squad.yaml` — `phi-redaction-completeness@1` gate
- `squads/legal-compliance/squad.yaml` — `compliance-coverage@1` gate
- `squads/sales-gtm/squad.yaml` — `sales-gtm-rigor@1` gate
- `squads/research-ds/squad.yaml` — `research-rigor@1` gate
- `squads/customer-support/squad.yaml` — `support-deflection-quality@1` gate

**New tests** (`tests/`):
- `test_judge_router.py`
- `test_judge_borda.py`
- `test_judge_reflexion.py`
- `test_judge_dispatcher_mock.py`
- `test_judge_policy.py`
- `test_judge_supervisor_integration.py`
- `test_judge_best_of_n.py`
- `test_judge_supervisor_phase34.py`
- `test_judge_host_pickup_skip.py`
- `test_eights_attestation.py`

---

## What this does NOT include

- **eights-daemon** itself (separate consumer repo per the Phase-6 note).
  Hydra's call sites are wired and will start landing receipts the moment
  the daemon registers under `EIGHTS_MCP_SERVER` ("eights-daemon") in
  `.mcp.json`.
- **`budget_charge` per-model-call hooks** — the cost-attestation surface
  is wired in the adapter but not yet called from squad executors. Belongs
  in a follow-up that touches `squad_node.py`'s `_via_mcp` /
  `_via_impersonation` / `_via_claude_skill` paths.
- **`prompt_get` for system-prompt loading** — adapter wired, call sites
  deferred until the prompt registry contract is settled.
- **A `/hydra:approve` flow that resumes the LangGraph compiled graph** —
  `--no-checkpoint` is the dev-loop escape hatch; the production resume
  path is unchanged from before this work.
