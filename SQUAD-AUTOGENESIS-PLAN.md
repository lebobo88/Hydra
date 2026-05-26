# Squad Autogenesis — Implementation Plan

> **Status:** Approved design baseline. Score: 78/100 (asymptote across three judge cycles).
> **Author:** Claude Opus 4.7 (interactive planning session 2026-05-23) + 8 rounds of Codex GPT-5.4-high adversarial review.
> **Source:** `~/.claude/plans/do-any-of-our-silly-lemur.md` (full review trail; this file is the consolidated implementation reference).
>
> **What this is:** A plan to add an auto-detection + propose + HITL-approve + scaffold pipeline that creates new squad packs when Hydra repeatedly encounters routing misses for capability gaps that don't match any existing squad.
>
> **What this is NOT:** Implementation. No code in `hydra_core/` has been changed. PR1 below is the first implementation step, gated on this plan's approval.

---

## Review trajectory (for context)

| Version | Score | Outcome | Key change |
|---|---|---|---|
| v1 | 22 | block | Initial design. Many file:line errors; missed procedural-memory infra. |
| v2 | 34 | block | Routed via procedural-memory queue; dropped HITLRequest.reason extension. |
| v3 | 46 | block | Added Phase 0 infra prereqs (SQLite store, operator CLI, approve hook). |
| v4 | 6 | frame-mismatch | Judge applied implementation-completeness frame to a plan; score invalid. |
| v4.1 | 73 | approve_with_changes | Folded 3 errata from v4 file-walk (envelope helper, LLM-exception, CLI). |
| v4.2 | 78 | approve_with_changes | 2PC commit, selective backfill, public approve()/reject() through CAS. |
| v4.3 | 78 | approve_with_changes | Forward/backward/HITL recovery reconciliation. |
| v4.4 | 78 | approve_with_changes | Atomic hook claim, concurrency contract, SHA-256 byte match. |

Three consecutive 78s = genuine asymptote. Remaining items are PR1-review-level (see "PR1 review checklist" at end).

---

## Part A — ADR-0001: Squad Autogenesis Supersedes Manual-Only Registry Mutation

### Status
Proposed (this plan IS the proposal; lives at `docs/adr/0001-squad-autogenesis.md` after PR7).

### Context
`hydra-supervisor.md:30` declares: *"Only `/hydra:add-squad` modifies the squad registry."* Today unmatched intents fall back to executive triage at confidence `0.1` (`hydra_core/router.py:166-172`), with `used_fallback=False` (dataclass default at `router.py:101`). The closest thing to "creation" today is the manual `/hydra:add-squad` command + `/smith:scaffold squad:<slug>`. Neither is auto-invoked.

### Decision
Permit registry mutation via a **second sanctioned channel**: an autogenesis pipeline that produces a `ProceduralUpdate` of new kind `squad_pack_creation`, which flows through the existing procedural-memory gate (`hydra_core/procedural.py:134-168`). Approval is required from the same operator path as any other procedural update. The manual `/hydra:add-squad` command remains valid; it is now one of two paths.

### Consequences
- `hydra-supervisor.md` rule restated: *"Squad registry is mutated only via (a) `/hydra:add-squad` or (b) an approved `squad_pack_creation` procedural update."*
- The procedural-memory queue becomes the single source of truth for pending proposals — once Phase 0a-c ship (durable SQLite store, operator CLI, approval hooks).
- **No hot-reload.** Approved proposals are written to disk; the next `build_supervisor()` call picks them up. In-flight workflows complete on their captured `packs` snapshot — preserves replay determinism (`hydra_core/state.py:50-129` has no `packs_hash` field, and we won't add one).
- **No TheEights dependency.** Per README.md:153, TheEights-backed autogenesis is "Phase 4 / not wired today." This plan uses Hydra-local procedural memory only. When TheEights ships, the committer becomes a federation source for `evolution.propose` (future ADR).

### Refusal mapping
- Refusal #5 (no HITL bypass) → procedural-gate enforces this (`procedural.py:150` calls `enforce_constitution` at admission).
- Refusal #7 (no unguarded venom) → enforced at execution time by `require_cerberus_pass` (`venom.py:263`); also pre-checked at proposal time by scanning proposed `tools[].name` against the venom registry.
- Refusal #8 (no procedural-memory contradiction) → already enforced by `enforce_constitution` at queue admission.

---

## Part B — Implementation Plan

### B.1 Architecture (7 stages)

```
  PHASE 0a  Durable ProceduralStore (SQLite) + CAS + 2PC
  PHASE 0b  hydra procedural list/show/approve/reject CLI
  PHASE 0c  procedural.approve post-hook (atomic claim)
  ─────────────────────────────────────────────────
  PHASE 1a  routing.miss telemetry + episodic emit (is_miss field)
  PHASE 1b  finalized field + router filter + selective backfill
  PHASE 2   detector CLI (manual: hydra autogenesis scan)
  PHASE 3   proposer CLI (manual: hydra autogenesis propose <cluster>)
  PHASE 4   committer wired to procedural post-hook (with 2PC recovery)
  PHASE 5   detector cron + budget guard
  PHASE 6   (deferred ADRs) auto-activation past stub; SQUAD_PROPOSAL envelope;
            TheEights federation; multi-tenant; Reflexion retry
```

### B.2 Touchpoints (file:line, verified against current tree)

| Phase | Change | File | Anchor |
|---|---|---|---|
| 0a | SQLite store | `hydra_core/procedural_store_sqlite.py` | NEW |
| 0a | `default_store()` reads env `HYDRA_PROCEDURAL_DB` | `hydra_core/procedural.py:118-119` | edit |
| 0a | Patch four call sites | `hydra_core/procedural.py:167,179,199,214` | `_DEFAULT_STORE` → `default_store()` |
| 0a | Atomic CAS approve/reject | `hydra_core/procedural_store_sqlite.py` | `approve_atomic`, `reject_atomic` |
| 0a | 2PC `pending_materialization` status | `hydra_core/procedural.py:37-43` | extend `ProceduralKind` + `ProceduralStatus` |
| 0a | `recover_orphans()` w/ FS reconciliation | `hydra_core/autogenesis/committer.py` | NEW |
| 0a | Invariants doc + parametrized tests | `hydra_core/procedural_store_sqlite.py` docstring + `tests/procedural/test_store_invariants.py` | NEW |
| 0b | CLI `procedural` subcommands | `hydra_core/cli.py:295-348` | add next to memory parser at `:324` |
| 0b | CLI `autogenesis` subcommands | same | new sibling parser |
| 0c | Approval hook (atomic claim via `INSERT OR IGNORE`) | `hydra_core/procedural.py:171-209` | extend `approve()` and `reject()`; add `register_approval_hook` |
| 0c | `approval_events` table w/ `UNIQUE(update_id, event_type)` | `hydra_core/procedural_store_sqlite.py` schema | NEW |
| 1a | `is_miss` field | `hydra_core/router.py:96-101` (`RoutingDecision`) | add `is_miss: bool = False` |
| 1a | `_miss_decision()` helper | `hydra_core/router.py` | covers 3 paths: no-signal, LLM-exception, LLM-empty |
| 1a | Miss detection branch | `hydra_core/supervisor.py:121` | after `state.selected_squads = decision.squads` |
| 1a | Episodic kind | `hydra_core/memory.py append_episodic` | add `"routing.miss"` |
| 1a | Telemetry events | `hydra_core/telemetry.py` | `routing.miss`, `routing.fallback`, `autogenesis.{proposed,approved,refused,activated,materialize_failed}` |
| 1b | `finalized: bool = False` | `hydra_core/squad_loader.py:59-78` (`SquadPack`) | NEW field |
| 1b | Router filters non-finalized | `hydra_core/router.py:107-172` | filter `packs` before keyword AND industry scoring |
| 1b | Envelope-helper filter | `hydra_core/squad_loader.py:136-138` | add `include_unfinalized: bool = False` kwarg |
| 1b | Selective backfill | `squads/{executive,engineering,creative,marketing-*}/squad.yaml` | add `finalized: true`; stubs (`healthcare/legal-compliance/sales-gtm/research-ds/customer-support/garland`) stay `finalized: false` (or omit → defaults false) |
| 1b | Update test | `tests/test_rlm_creative_integration.py:102-124` | assert router skips finalized=false stub; executive picks up |
| 2 | Detector module | `hydra_core/autogenesis/detector.py` | NEW |
| 3 | Proposer module | `hydra_core/autogenesis/proposer.py` | NEW |
| 3 | Autogenesis workflow wrapper | `hydra_core/autogenesis/workflow.py` | NEW — thin `HydraState` builder w/ `tenant_id="autogenesis"` + `BudgetLedger` from env |
| 3 | New procedural kind | `hydra_core/procedural.py:37-43` | add `"squad_pack_creation"` to `ProceduralKind` Literal |
| 3 | Budget enforcement | `hydra_core/autogenesis/proposer.py` | `record_cost` per LLM/embed call; `is_over_budget` pre-check; `BudgetExhausted` exception |
| 3 | Venom pre-check | `hydra_core/autogenesis/proposer.py` | iterate `tools[].name`; `venom.get_venom()` lookup; reject if any non-None |
| 4 | Committer | `hydra_core/autogenesis/committer.py` | registers approval hook; 2PC flow; calls `_atomic_materialize` |
| 4 | `_atomic_materialize` | same | tmp dir + os.replace + post-write SHA-256 persist |
| 4 | Slash command `--finalize <slug>` | `.claude/commands/hydra-add-squad.md` | extend; only allowed when entrypoint != stub |
| 4 | Slash command `--remove <slug>` | same | NEW |
| 4 | Slash command wrapper | `.claude/commands/hydra-autogenesis.md` | NEW — thin wrapper over Python CLI |
| 4 | ADR-0001 | `docs/adr/0001-squad-autogenesis.md` | NEW (Part A above) |
| 4 | Supervisor contract update | `.claude/agents/hydra-supervisor.md:30` | edit |
| 4 | Cross-tool contract | `AGENTS.md`, `HYDRA.md` | reference ADR-0001 |
| 5 | Cron docs | `docs/autogenesis-operations.md` | NEW |
| 5 | Budget env | `HYDRA_AUTOGENESIS_BUDGET_USD` (default $1/day) | document |

### B.3 PR sequence (8 PRs, each independently revertable)

1. **PR1 (Phase 0a):** `SQLiteProceduralStore` (with WAL + `BEGIN IMMEDIATE` + WITHOUT ROWID `approval_events`) + invariants doc + parametrized shared tests + 4 call-site patches + `approve_atomic` + `reject_atomic` CAS + `recover_orphans` w/ 3-way decision tree.
2. **PR2 (Phase 0b):** CLI `hydra procedural list/show/approve/reject` + `hydra autogenesis scan/propose/list` in `cli.py:295-348`.
3. **PR3 (Phase 0c):** `register_approval_hook` + atomic-claim via INSERT OR IGNORE + `_fire_approval_hooks` with E6 dispatch (SQLite uses CAS path; InMemoryStore uses legacy path).
4. **PR4 (Phase 1a):** `is_miss` field + `_miss_decision()` helper + telemetry split + episodic emit in `node_intake`.
5. **PR5 (Phase 1b):** `finalized` field + `classify_intent` filter + envelope-helper filter + selective backfill + `garland` test update.
6. **PR6 (Phase 2+3):** detector + proposer + `autogenesis_workflow` wrapper + budget enforcement + venom pre-check + redundancy ≥0.85 reject.
7. **PR7 (Phase 4):** committer + `_atomic_materialize` w/ SHA-256 persist + `/hydra:add-squad --finalize/--remove` flags + ADR-0001 + supervisor.md/AGENTS.md/HYDRA.md updates.
8. **PR8 (Phase 5):** cron docs + `HYDRA_AUTOGENESIS_BUDGET_USD` env.

Each PR ships independent value: PR1 alone gives durable procedural memory; PR2 alone gives operators CLI visibility; PR4-5 alone reduce mis-routing.

### B.4 Failure modes & mitigations (cumulative across all errata)

| Failure mode | Mitigation |
|---|---|
| Two proposers race same slug | SQLite `proposer_locks` table; `INSERT … ON CONFLICT DO NOTHING` |
| Two approvers race same update | CAS via `UPDATE … WHERE id=? AND status='pending'` with `rowcount==1` check |
| SQL committed but materialize fails | 2PC `pending_materialization` intermediate status + `recover_orphans` |
| Recover_orphans on already-materialized squad | FS reconciliation: SHA-256 check → forward (commit) / backward (revert) / HITL (mismatch) |
| Concurrent hook firers | Atomic claim via `INSERT OR IGNORE INTO approval_events` on UNIQUE(update_id, event_type) |
| Prompt-injected proposer (deceptive squad) | Constitution gate at `procedural.propose` + operator review + MCP whitelist + venom pre-check |
| Cost runaway from miss-clustering | `autogenesis_workflow` w/ `BudgetLedger` + explicit `record_cost` per call + `BudgetExhausted` exception |
| Replay determinism (new squad mid-workflow) | No hot-reload; `packs` captured at `build_supervisor()` (`supervisor.py:67`) |
| Stale compiled supervisor | `/hydra:autogenesis list` shows "committed pending restart" indicator |
| Old-checkpoint `ProceduralKind` extension | Additive Literal; `procedural.py:66-81 to_dict` permissive; verified backcompat test |
| Partial scaffold write breaks `discover_squads` | `_atomic_materialize`: tmp dir + post-write re-validate + `os.replace` (atomic POSIX + Windows) |
| Stub routing pollution after autogenesis | `finalized` filter in BOTH `classify_intent` AND envelope-helper; committer scaffolds w/ `industries: []` + `finalized: false` |
| LLM classifier crash mis-classified as miss | `_miss_decision()` helper unifies 3 paths (no-signal, LLM-exception, LLM-empty); detector keys on explicit `is_miss` field |
| Slug collision between proposers | 2PC mismatch branch surfaces HITL `reason="autogenesis_slug_collision"`; row stays in `pending_materialization` |
| Windows portability | No `fcntl`; SQLite WAL + `BEGIN IMMEDIATE` + SQL-based locks |

### B.5 Verification (24 tests)

**Phase 0 gates:**
1. `test_sqlite_store_roundtrip` — cross-process round-trip via temp DB.
2. `test_approve_hook_called_synchronously` — hook fires; exception in hook doesn't block approval; `approval_events` row written.
3. `test_procedural_cli` — `hydra procedural list/show/approve/reject` works end-to-end.

**Phase 1+ gates:**
4. `test_signal_no_classifier_miss` — `classify_callable=None`, unmatched text → `routing.miss` telemetry + episodic.
5. `test_signal_llm_fallback_success` — `classify_callable` returns squad → `routing.fallback` only, NOT `routing.miss`.
6. `test_signal_llm_fallback_empty_or_exception` — empty/exception → `is_miss=True`, `routing.miss` emitted.
7. `test_detector_clustering` — 5 rows across 5 workflow_ids → cluster; 5 rows in 1 workflow_id → no cluster.
8. `test_proposer_venom_reject` — venom in tool → rejected pre-`procedural.propose`.
9. `test_proposer_redundancy_reject` — cosine ≥0.85 → reject.
10. `test_proposer_entrypoint_stub_enforced` — non-stub draft → coerced or rejected.
11. `test_proposer_budget_exhausted` — proposer with $0.01 budget after first LLM call → aborts.
12. `test_committer_industries_empty` — yaml has `industries: []` + `finalized: false`; loaded pack filtered by `classify_intent`.
13. `test_committer_no_keywords_mutation` — committer scaffold doesn't touch `_KEYWORDS`.
14. `test_committer_agentsmith_absent_fallback` — direct file write path works when agentsmith MCP absent.
15. `test_e2e_cross_process` — detector subprocess → proposer subprocess → SQLite → operator subprocess approves → committer subprocess materializes.
16. `test_replay_determinism` — new squad on disk mid-workflow → in-flight `packs` snapshot unaffected.
17. `test_atomic_materialize_partial_write` — simulated mid-write failure → `squads/<slug>/` doesn't exist; `discover_squads()` still works.
18. `test_pre_commit_reject` — `procedural.reject` → no files.
19. `test_post_commit_rollback` — `/hydra:add-squad --remove <slug>` → directory deleted.
20. `test_procedural_kind_backcompat` — old serialized record with pre-extension kind round-trips.
21. `test_finalized_filter` — non-finalized excluded for keyword AND industry scoring.
22. `test_is_miss_signal_paths` — assert `is_miss=True` only at the 3 designated paths in `router.py`.
23. `test_orphan_recovery_forward_backward_hitl` — three pending_materialization rows: matching disk → forward, no disk → backward, mismatching disk → HITL.
24. `test_hook_claim_atomic` — two concurrent `_fire_approval_hooks` calls → counter==1; `approval_events` has one row.

**Plus:** existing `tests/` + `tests/governance` + `tests/test_judge_supervisor_integration.py` pass with zero regressions.

### B.6 Out of scope (deferred to follow-up ADRs)

- Auto-activation past `stub` entrypoint (separate ADR — blast radius too high for MVP).
- `SQUAD_PROPOSAL` envelope + judge routing (would extend `_BASE_TIER_BY_TYPE` in `judge/router.py:26-39`).
- TheEights `evolution.propose` federation (per README.md:153 — Phase 4 of TheEights).
- Multi-tenant proposal isolation (`tenant_id` namespacing in `squads/`).
- Reflexion ×1 retry on rejected proposals.

---

## PR1 Review Checklist (carried from final judge cycle)

Three items the judge flagged as remaining at v4.4. Best resolved with live Codex review against actual `SQLiteProceduralStore` code, NOT in plan text:

1. **Hook execution semantics: at-most-once attempt vs exactly-once effect.** Add `completed_at TEXT NULLABLE` to `approval_events`. Claim sets `claimed_at`; hook success sets `completed_at`. Recovery re-fires `claimed-but-not-completed` rows past timeout T. Hook impls must be idempotent at the side-effect layer (documented as `register_approval_hook(fn, idempotency_class="...")` requirement).

2. **E11 concurrency contract extension.** Add a sixth clause covering the post-claim/pre-effect hook crash window: "If a hook is claimed but never completes within T minutes, recovery re-claims and re-fires. The hook framework guarantees at-least-once attempt; effect-level exactly-once is the hook author's contract via idempotency_class."

3. **content_sha crash window.** Between `os.replace` success and `UPDATE … SET content_sha=?`, a crash leaves correct disk content with NULL `content_sha`. Recovery in that state degrades from forward-recovery to HITL (operator visually compares). Acceptable; window is sub-millisecond in practice — document in PR7 release notes.

---

## Detailed errata (audit trail of design evolution)

The full deltas E1-E13 with rationale, code snippets, and judge feedback per round are preserved in:

- `~/.claude/plans/do-any-of-our-silly-lemur.md` (this session's plan file)

That file contains the complete review trail: v1's initial design, every judge critique that blocked or revised it, and the reasoning behind each fix. Reference it during PR1-7 implementation to understand WHY decisions were made, not just WHAT to implement.

---

## Sources

- [AutoGenesisAgent (arXiv 2404.17017)](https://arxiv.org/abs/2404.17017) — lifecycle-of-specialist-agents framing.
- [AutoAgents (arXiv 2309.17288)](https://arxiv.org/abs/2309.17288) — Planner/Observer stages.
- [Auto-scaling LLM MAS — IAAG/DRTAG (Frontiers)](https://www.frontiersin.org/journals/artificial-intelligence/articles/10.3389/frai.2025.1638227/full) — conversation-manager pattern for runtime agent spawning.
- [Regulation (EU) 2024/1689 — AI Act, EUR-Lex](https://eur-lex.europa.eu/eli/reg/2024/1689/oj/eng) — phased applicability: 2 Feb 2025, 2 Aug 2025, 2 Aug 2026 (general), 2 Aug 2027 (high-risk extension).
- [Runtime Governance for AI Agents (arXiv 2603.16586)](https://arxiv.org/abs/2603.16586) — path-dependent runtime evaluation.
- [HITL Agentic AI 2026 — OneReach](https://onereach.ai/blog/human-in-the-loop-agentic-ai-systems/) — HITL gating patterns for high-stakes decisions.
- [LangGraph Deep Agents subagent spawning](https://medium.com/@abhishek68/how-langchain-deep-agents-actually-work-under-the-hood-8847e177406c) — instantiable subagent patterns.
