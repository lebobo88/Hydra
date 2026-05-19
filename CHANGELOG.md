# Hydra Changelog

All notable changes to Hydra's runtime (`hydra_core/`), squad registry, and
Claude Code plugin surface land here. Format loosely follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/) and the project follows
[Semantic Versioning](https://semver.org/spec/v2.0.0.html) at the `hydra_core`
public-API boundary.

## [Unreleased]

### Added — Cross-vendor judge plane

Hydra now incorporates the pair-programmer "rubber-duck across vendors" pattern
as an orchestrator-wide judging layer. PP keeps owning engineering verdicts;
Hydra now judges every other squad's output (executive, creative, healthcare,
legal-compliance, sales-gtm, research-ds, customer-support) plus the
post-synthesis Cathedral artifact, reusing PP's `pp_codex` / `pp_gemini` MCP
critique servers as the vendor abstraction.

- **`hydra_core/judge/` package** with versioned rubrics (`@1` IDs, immutable
  bodies for replay determinism), tier router (`cross_vendor | same_vendor |
  skip` with content-aware regex escalation), dispatcher with PP's pragmatic-
  pass guard ported, Borda-count best-of-N, Reflexion ×1 retry, and YAML-backed
  policy loader.
- **`JudgeVerdict` envelope** registered in `SCHEMA_REGISTRY`; verdict's
  `rubric_id@N` pinned so past verdicts replay against the exact rubric body.
- **Supervisor lifecycle extended**: two new nodes — `judge_per_squad` after
  dispatch and `judge_synthesis` after synthesis. `interrupt_before` now
  includes `judge_synthesis` so LangGraph pauses for HITL when synthesis fails
  on a high-severity rubric.
- **Best-of-N at squad level** (`squads/<slug>/squad.yaml: best_of_n: N`):
  executive + creative opt in with `N=3`. Produces N candidates, judges each,
  Borda-ranks, archives losers under `kind: bon_losers` (TheEights Kan-cell
  convention).
- **Reflexion ×1 bridge**: a `revise` verdict triggers exactly one re-dispatch
  to the source squad with the critique embedded; capped by the existing
  governance loop ceiling.
- **HITL escalation**: a `fail` verdict on a HITL-severity rubric
  (`constitution-alignment@1`, `phi-redaction-completeness@1`,
  `financial-hardcoding@1`) surfaces the workflow with
  `pending_hitl.reason="policy_breach"`.
- **`MCPCritiqueClient`** calls `pp-codex` and `pp-gemini` MCP critique tools
  through the existing `MCPStdioDispatcher`. Response normalizer unwraps PP's
  `result.parsed` envelope and tolerates flat / aliased shapes.
- **10 versioned rubrics** in `hydra_core/judge/registry.py`:
  `constitution-alignment@1`, `synthesis-coherence@1`,
  `board-decision-quality@1`, `mna-due-diligence@1`, `scenario-rigor@1`,
  `financial-hardcoding@1`, `brand-consistency@1`, `audience-fit@1`,
  `phi-redaction-completeness@1`, `compliance-coverage@1`,
  `sales-gtm-rigor@1`, `research-rigor@1`, `support-deflection-quality@1`.
- **Telemetry**: `judge.invoked`, `judge.verdict`, `judge.skipped`,
  `judge.borda`, `judge.reflexion`, `judge.hitl_escalation`,
  `judge.bon_fallback`, `judge.bon_all_pending` events on the per-workflow
  trace.
- **`.mcp.json`**: `pp-codex` + `pp-gemini` server entries pointing at PP's
  daemon CLI subcommands `mcp-codex` / `mcp-gemini`.

### Added — TheEights attestation adapter

Hydra now calls the eights-daemon's audit-ledger MCP tools at the right
lifecycle points so a future eights-daemon deployment lights up Hydra's
attestation surface with zero further code changes. Until the daemon registers,
all calls no-op cleanly.

- **`hydra_core/eights/attestation.py`** — `EightsAttestor` wrapping
  `eights.constitution.attest`, `eights.hydra.envelope_record`,
  `eights.governance.ceiling_tick`, `eights.governance.budget_charge`,
  `eights.governance.hitl_request`, `eights.redaction.redact_for_squad`,
  `eights.prompt.get`. Best-effort: dispatcher failure / missing tool / missing
  daemon all return `None` without raising.
- **Supervisor wiring**: `constitution_attest` at intake (stamps
  `state.constitution_hash` + version + receipt); `envelope_record` after
  every envelope emit in dispatch and judge_synthesis; `ceiling_tick` once
  per intake; `hitl_request` on both per-squad and synthesis HITL escalation.

### Added — Caveat fixes from the live smoke

- **Host-pickup envelopes skip the judge plane.** When the dispatcher returns
  `host_pickup_required` (Claude Code subagent will fulfil out-of-band),
  the produced envelope is tagged `_host_pickup_pending` and
  `node_judge_per_squad` skips it. Prevents the judge from scoring
  placeholder DecisionRecords. Best-of-N also short-circuits when every
  candidate is pending — no point ranking placeholders.
- **`hydra run --no-checkpoint` flag.** Forces the pure-Python supervisor
  runner, bypassing LangGraph's HITL interrupts. For smoke tests and dev
  loops; production runs still use the checkpointed graph and pause at HITL.

### Changed

- **AGENTS.md Hard Rule 8**: "Never modify a rubric's `@<version>` body in
  `hydra_core/judge/registry.py`. Past verdicts pin `rubric_id@N` for replay
  determinism. To change a rubric, register `@N+1` and update consumers to
  opt in."
- **`hydra_core/state.py`**: `HydraState.phase` literal extended with
  `judge_per_squad` / `judge_synthesis`; new append-only `verdicts: list[dict]`
  field.
- **`SquadResult.host_pickup_pending: bool`** field on the squad-executor
  result; impersonation and claude-skill paths set it when the dispatcher
  returned `host_pickup_required`.
- **`SquadPack.best_of_n: int`** field on the squad registry; executive +
  creative `squad.yaml` opt in with `best_of_n: 3`. Stub-squad `squad.yaml`
  files declare their cross-model rubric in `gates:` alongside existing
  domain-specific gates.
- **`build_supervisor`**: new kwargs `critique_client` (injectable), `profile`,
  `force_pure_python`.

### Fixed

- `.mcp.json` `pp-codex` / `pp-gemini` entries — initial Phase-2 attempt
  pointed at bare server modules which only *export* the runner. Correct
  invocation is `node dist/index.js mcp-codex` / `mcp-gemini`.
- `_PurePythonRunner` skipped `approval` unconditionally, which would
  short-circuit the lifecycle when `requires_human_approval=False`. Now the
  approval step is conditional, matching the compiled-graph behavior.

### Tests

- **70 new tests** across 8 new test files: `test_judge_router.py` (15),
  `test_judge_borda.py` (5), `test_judge_reflexion.py` (5),
  `test_judge_dispatcher_mock.py` (7), `test_judge_policy.py` (16),
  `test_judge_supervisor_integration.py` (3), `test_judge_best_of_n.py` (4),
  `test_judge_supervisor_phase34.py` (4), `test_judge_host_pickup_skip.py` (1),
  `test_eights_attestation.py` (11). Full suite: **216 passing**, 0
  regressions vs. the 146-test baseline.

### Live validation

End-to-end live cross-vendor critique confirmed:
`pp-gemini.critique` and `pp-codex.critique` both reachable; substantive
disagreement captured (e.g., Gemini scored `single_voice: 0.2 → revise` on
a synthesis output where Codex scored `0.96 → pass`); `judge.borda`,
`judge.verdict`, and `judge.invoked` events landed in
`.hydra/<workflow_id>/trace.jsonl`.
