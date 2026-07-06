# ROUTE-AUDIT-2026-07-05 — Hydra Squad-Route & Connected-System Verification Campaign

Branch `fable-audit-2` @ `d6af7e6`. Execution window: 2026-07-05 → 2026-07-06.
Operator mandate: trace and test all routes and all usage of every squad pack and
connected system end-to-end through Hydra; fix deviations from design/spec;
**every fix requires operator approval first**. Plan: `~/.claude/plans/bright-dancing-bengio.md`.

## Phase 0 — Baseline

| Check | Result |
|---|---|
| Branch / tree | `fable-audit-2`, clean @ `d6af7e6` |
| Offline suite | **1366 passed, 1 skipped** (expected: rlm_creative user-scope) in 312.5s |
| `hydra doctor` (full) | ALL OK — constitution sha `4060cb542fcc`, 14 squads, 63 cathedral aliases, eights vocab, episodic db (2645 rows), 11 venoms, langgraph+pydantic, 5/5 MCP probes reachable |
| `gateway_health` | 16/16 backends registered; 15 connected first-try; `agentsmith` failed first-try (→ RA-2), recovered on retry |
| Ping matrix | es/mb/senate/xenia/xenia-kb (10 docs)/xenia-tickets (5 open)/rlm/rlmgaming/hydra_control — all ok |
| Squad registry | Exactly 14 (11 active, 3 stubs inactive); AgentSmith's `hydra.squad_list` view matches Hydra's |
| pp_harness | reachable; `list_runs` ok |
| TheEights | `squad.list` ok after boot-verify window ("audit verification in progress" during incremental audit verify — by design) |

### MU backlog re-verification (operator: "double check")

| Item | Status | Evidence |
|---|---|---|
| MU4 stale-workflow reap | LANDED | `hydra reap` + `_is_reapable` (`hydra_core/cli.py:1948`) |
| MU5/MU5b repo-from-prose | LANDED | `supervisor.py:584` conservative goal-prose repo inference |
| MU6 smoke slowness (P1 scoped smoke) | LANDED | `HYDRA_SMOKE_TIMEOUT_S` (`squad_node.py:432`) + `.harness/smoke_cmd.json` override (`squad_node.py:354`) |
| MU11 tag silent no-op | LANDED | error dict on unknown key (`mcp_servers/hydra_memory/server.py:215`) — functional re-check in C4 |
| MU13 pycache merge fragility | LANDED | `__pycache__/` exclude (`host_bridge.py:116`) |
| MU14 $0 cost on timeout | LANDED | no-cost trace emission (`squad_node.py:1057,1528`) |
| MU16 fleet mid-leg budget | LANDED | pre-candidate budget gates (`squad_node.py:890,1390,1452`), merge `79c1e15` |

Note: the pp "surfaced runs waiting" session banners refer to attended runs already
preserved-and-merged (e.g. `run_6nka9GNm4QHc` → `6d3b6bf`, `run_ocHptcKSLdGE` → `79c1e15`)
— stale rows, cosmetic (→ RA-4/S4).

## Phase 1 — Static route matrix

| Squad | Entrypoint | Backend (shim) | Headless terminal | Attended terminal |
|---|---|---|---|---|
| engineering | mcp | pp_harness (+pp_codex judge) | complete (drive loop) | complete |
| executive | agent-impersonation | executive_suite | deferred_to_host | complete |
| garland | claude-skill | rlm_creative (`rlm.*`) | deferred_to_host | complete |
| legal-compliance | claude-skill | senate (`senate.*`) | deferred_to_host | complete |
| customer-support | claude-skill | xenia (`xenia.*`) + xenia_kb/xenia_tickets | deferred_to_host | complete |
| rlm-gaming | claude-skill | rlm_gaming (`rlmgaming.*`) | deferred_to_host | complete (+delegation envelopes) |
| marketing-strategy/creative/research/production/ops | claude-skill | marketbliss (`mb.*`) | deferred_to_host | complete |
| healthcare / sales-gtm / research-ds | stub | — | surfaced `[STUB]` (explicit `--squad` only) | n/a |

Static confirmations: router keyword coverage per non-stub squad (`router.py:25-140`);
stub exclusion (`router.py:182-198,223`); `_SKILL_PACK_SHIMS` complete for all 8
claude-skill squads (`squad_node.py:2351-2393`); RBAC keying `f"{server}.{tool}"` with
form-(b) `name` + `mcp_server` (`dispatcher.py:232-265`).

## Phase 2 — Live route tests

### 2a Plan-only routing (15/15 PASS)
R1→engineering, R2→executive, R3→garland, R4→legal-compliance, R5→customer-support,
R6→rlm-gaming, R7→marketing-research, R8→marketing-strategy, R9→marketing-creative,
R10→marketing-ops, R11→marketing-production — all exact single-squad selections.
R12 `--squad executive` beat marketing keywords ✓. R13 no-signal → executive default ✓.
R14 overlap goal → `[executive, garland, marketing-strategy]` (multi-squad by design;
right squad included — observation, not deviation). R15 healthcare keywords → executive,
stub NOT auto-selected (MU9a holds) ✓. Workflow ids in `.hydra/` (1d41136f, 6ca816d6,
9217fc05, e62aa0fe, 93758beb, 02729578, 96516fe6, 0412b420, 68ffdc7b, b9e9b58f,
0e4c1641, 46e77b47, 6ceaf0ce, ff8c5567, b9613021).

### 2b Stub via explicit --squad (wf 1bd1754a)
`supervisor.stub_squad_explicitly_selected` fired ✓; BUT live dispatch deferred the stub
to host (stranded at synthesis, task `deferred_to_host`) instead of surfacing the
`[STUB]` DecisionRecord → **RA-5**. Same code path covers sales-gtm/research-ds.

### 2c Headless defer seam (5/5 PASS)
executive 6ca816d6, garland 9217fc05, marketing-research 96516fe6, marketing-creative
68ffdc7b, marketing-ops b9e9b58f — all `dispatch.deferred_to_host` with correct squad +
entrypoint; no wrong-pack writes.

### 2d Attended executions
| Run | Squad → backend | Result |
|---|---|---|
| E1 e62aa0fe | legal-compliance → senate | **phase done**; general-counsel host_action → Curia deliverable (4-1 majority, Cicero dissent) → $0.20 charged; no judge branch (best_of_n:0 by design) ✓ |
| E2 93758beb | customer-support → xenia | **phase done**; support-supervisor ran 18 live tool calls: ticket 000008 created, KB citations by doc_id (csv-export-troubleshooting, dashboard-sharing-howto, refund-policy), recommend-only on refund (pending), NO send/execute ✓; $0.25 charged |
| E3 02729578 | rlm-gaming → rlm_gaming | stage **complete** (design-only GDD + delegation); `submit_envelopes` ingest: malformed envelopes rejected 3× fail-closed (validation ✓), valid DEV_TASK **dispatched to engineering as real pp run** `run_Zh0duin6_2H5` (surfaced), `ingest.over_budget` honest stop at $1.10/$1.00 ✓; workflow honestly **surfaced** |
| E4 0412b420 | marketing-strategy → marketbliss (symlink cwd resolved ✓) | **phase done**; campaign-strategist deliverable + CREATIVE_BRIEF handoff stub; $0.18 charged |
| E5 engineering | — | folded into first approved fix workflow (Phase 4); delegation-spawned run `run_Zh0duin6_2H5` already live-proved envelope→pp dispatch |

## Phase 3 — Connected-systems checks

| # | System | Result |
|---|---|---|
| C1 | AgentSmith | venom cross-check benign→ok ✓; hostile→**"venom unreachable — failing CLOSED (N2)", reason hydra-mcp-unavailable** → RA-6; constitution attest receipt ✓; keymaker_scan enumerates all hydra artifacts incl. symlinked marketing squads ✓; N8 refusal→attest→lift verified live ✓ |
| C2 | TheEights | constitution attest ✓ (sha matches doctor 4060cb542fcc); squad.list ✓; envelope_query returns engineering-path records but NOTHING for today's workflows; **spool ~/.hydra/eights-pending/ has 6,217 undrained files since 2026-07-03, no replay events in any trace** → RA-7 |
| C3 | ExecutiveSuite | roster 24 agents matches squad.yaml ✓; output write→read round-trip byte-identical ✓ (read takes `path`, not `relative`) |
| C4 | hydra_memory | write→read→tag round-trip ✓; MU11 unknown-key tag → explicit error ✓; BUT `list_workflow` for completed E1 = 0 rows; semantic_search for E1 content = 0; **only venom + external callers write episodic; supervisor lifecycle never persists; synthesis MemoryRefs dangle** → RA-8 |
| C5 | Xenia WS-AUTH | send without token → CALLER_CAPABILITY_INVALID ✓; forged token → rejected ✓; degraded (no-key) token → rejected fail-closed ✓; execute_approved without scope/approval_id → rejected ✓; **HYDRA_OPERATOR_KEY unprovisioned in every environment → positive path inert everywhere** → RA-9 (fail-closed, no exposure) |
| C6 | hydra_control | telemetry_tail stitches E1 full lifecycle (plan→intake→tool_scope→hitl_resumed→dispatch→attended.step/submit) ✓ |
| C7 | unknown-slug fallback | statically confirmed: `_resolve_skill_shim` falls back to garland shim with warning only (`squad_node.py:2396-2408`) — folded into RA-3 fix scope |

Non-findings (asserted, by design): envelope validation fail-closed (3× rejection of malformed
delegation envelopes); ingest over-budget honest stop; xenia-kb search corpus-limitation
(on-corpus queries score correctly); legal best_of_n:0; pp_gemini retired; R14 multi-squad
selection; per-squad approval-gate variance (squad gates config).

## Deviation ledger

| ID | Sev | Area | Summary | Proposed fix | Status |
|---|---|---|---|---|---|
| RA-1 | S2 | enforcement | `HYDRA_PP_STAGE_ACTIVE=1` leaked into host session process env (parent shell; not registry/settings). All 3 enforcement hooks bypass unconditionally while set → routing enforcement OFF for sessions from that shell. MU2-class at process scope. | Operator clears it from the launching shell. Hardening (code): hooks only honor the bypass when an active-stage marker file exists (`.harness/stage-active`), else warn loudly; SessionStart hook flags a bare env leak. | AWAITING APPROVAL |
| RA-2 | S3 | agentsmith availability | Time-to-MCP-ready 16s idle / 27-30s+ loaded vs gateway `_CONNECT_TIMEOUT=20s` → backend flaps failed under load; venom cross-check silently skipped meanwhile (Hydra side fail-open). Boot blocks on `[eights-daemon]` child before MCP-ready, contradicting smith's own transport-first design comment. | Hydra side: make `_CONNECT_TIMEOUT` env-tunable (`HYDRA_GATEWAY_CONNECT_TIMEOUT_S`) with per-backend override. Root fix (AgentSmith repo): start MCP transport before EightsBridge child spawn/first-connect. | AWAITING APPROVAL |
| RA-3 | S2 | RBAC / claude-skill seam | No claude-skill squad declares its shim tools → live RBAC denies 17/18 shim calls (command.list + output.write) → catalogue silently empty, pack-store artifact writes silently dropped while runs report success. Unknown-slug fallback writes to WRONG pack (garland) with only a log warning. | Single-point fix: `_check_tool_rbac` (or loader) auto-authorizes each claude-skill squad's own shim pair from `_SKILL_PACK_SHIMS`; emit `dispatch.rbac_denied` trace on any denial; unknown slug fails closed (surfaced) instead of garland fallback; extend `test_squad_rbac.py` to all squads. | AWAITING APPROVAL |
| RA-4 | S4 | pp hygiene | Session banners list surfaced pp runs whose attended work already merged (run_6nka9GNm4QHc → 6d3b6bf, run_ocHptcKSLdGE → 79c1e15, run_qtksS1xg5KxQ → 6ebef77). | Mark those pp run rows closed (pp harness ack) or teach the banner to suppress runs with a merged attended branch. | AWAITING APPROVAL |
| RA-5 | S3 | stub dispatch | Live dispatcher defers explicitly-selected STUB squads to host (`supervisor.py:1801` matches any non-mcp entrypoint) → task stranded `deferred_to_host` (host can't run a stub); design says surface `[STUB]` DecisionRecord (`squad_node.py:159-179`). Repro wf 1bd1754a. | Exclude `entrypoint == "stub"` from the live-defer pre-filter so `_stub` runs in-graph and surfaces honestly; regression test. | AWAITING APPROVAL |
| RA-6 | S3 | agentsmith↔hydra venom | Smith's venom cross-check reports `hydra-mcp-unavailable` (fails CLOSED smith-side); Hydra's cross-check consumer is fail-OPEN → the F35 cross-check adds nothing in current topology. | Wire smith's HydraBridge to hydra_control (backend spec/env in smith config), or explicitly document cross-check as inactive; add doctor probe for the smith→hydra back-channel. | AWAITING APPROVAL |
| RA-7 | S2 | eights governance mirror | `~/.hydra/eights-pending/` spool: 6,217 undrained files since 2026-07-03 (+2 stale .partial). No `replay` events in any of today's traces — attest/HITL/envelope/budget events for plan-intakes (stub dispatcher spools by design) and resume paths never reach TheEights; governance mirror dark for 3 days. | Drain spool from every live-dispatcher entry (resume + launch, not just full-run intake); add `hydra eights-drain` CLI + doctor spool-depth tripwire; cap spool growth with age-out + alert. | AWAITING APPROVAL |
| RA-8 | S3 | workflow memory | Completed workflows leave NO episodic rows (`hydra-mem.list_workflow` empty for done wf e62aa0fe); synthesis builds `MemoryRef(tier="episodic")` handles it never persists (only venom.py + external tools call `append_episodic`). Episodic recall / workflow history contract unfulfilled. | Persist synthesis DECISION_RECORD (and attended squad artifacts) via `append_episodic` keyed by MemoryRef key at synthesis/postcheck; regression: list_workflow non-empty after a completed workflow. | AWAITING APPROVAL |
| RA-10 | S4 | goal-text flag parsing | Observation from Phase 4: `--squad engineering` embedded in goal TEXT is not parsed by `hydra.workflow.plan` (only the tool's `squad` param / CLI flag channel is honored), and the router then keyword-matched the word "garland" inside a fix description → selected `[engineering, garland]` for a pure engineering goal (wf d5aec52a, rejected). Known limitation class (cf. MU5 for --repo, since fixed for repo flags). | Either parse a tail-position `--squad` in goal text at intake (mirroring the MU5 prose-safe repo-flag rule) or document that MCP callers must use the `squad` param. | RECORDED (operator may batch with future work) |
| RA-11 | S3 | scoped-smoke override ineffective | Discovered during FIX-A finalize: `_detect_smoke_command` resolves `.harness/smoke_cmd.json` against `project_path`, which for attended finalizes is the WORKTREE — and `.harness/` is gitignored, so worktrees never contain the override. Net effect: the P1 scoped-smoke operator override never applies to attended finalizes (its primary use case); full-suite smoke ran and timed out at 600s (run_I1CLcQgpdkKX, judge PASS discarded to preserved branch). | Resolve the override (and HYDRA_SMOKE_TIMEOUT_S default) from the repo root / `git rev-parse --git-common-dir` when project_path is a worktree; regression test. | APPROVED — folded into FIX-B (same subsystem) |
| RA-9 | S3 | xenia WS-AUTH provisioning | `HYDRA_OPERATOR_KEY` unprovisioned (host env, xenia_tickets backend env) → all capability mints degraded → server rejects everything fail-closed. No security exposure, but send_response/execute_approved permanently inert; ties to tracked WS-AUTH Phase 2. | Provision key for the xenia_tickets backend (backends.json env or key file) + dispatcher mint seam per Phase 2 plan; add doctor probe "WS-AUTH key present". | AWAITING APPROVAL |

| RA-12 | S2 | resume ghost re-dispatch / budget blindness | Discovered in Phase 4: resuming a workflow whose ENGINEERING task already completed+merged via the attended path re-dispatches the task through the HEADLESS drive loop. FIX-B's closing resume ran an unrequested best-of-3 (`run_gK8bfZ_MKMgq`, 3× opus ≈ **$10.02 vs the $5 workflow budget**); FIX-C's resume started the same (killed mid-flight, `run_sQni_Nj4HK-G`, finalized aborted). Root causes: (a) node_dispatch does not skip mcp tasks whose attended cursor is complete (attended_done_task_ids not consulted on the dispatch path); (b) resume-dispatched pp runs carry `hydra_workflow_id: null` (MU5b provenance gap) so their cost never lands in the workflow ledger and the MU16 budget gates are blind to it. No duplicate code merged (candidates found work already landed). | (a) node_dispatch skips tasks in attended_done_task_ids (emit trace `dispatch.attended_already_complete`); (b) thread workflow_id through the resume→pp start_run path so charges land and gates see them; regression tests for both. | **OPEN — needs operator approval** (found after the approval round) |

## Phase 4 — Fixes (all operator-approved 2026-07-06)

| Bundle | Workflow | Findings | Outcome |
|---|---|---|---|
| FIX-A | 3e46afdc / run_I1CLcQgpdkKX | RA-3, RA-5, RA-8 | Engineer → codex judge REVISE (per-artifact test gap) → Reflexion×1 → codex judge **PASS** (10/10/9.6). Finalize smoke 600s timeout (RA-11 class) → preserved-branch pickup merge `726e2ee`. Suite **1380 green**. |
| FIX-B | 50a3d5b2 / run_XYR4OsQmp8fO | RA-7, RA-2, RA-11, RA-6 probe | Engineer → same-vendor judge **PASS** (.90/.95/.07). Smoke timeout again (fix not yet active) → pickup merge `168a474`. Suite **1404 green**. |
| FIX-C | 7591b49f / run_YPuPaxVxrsDl | RA-1, RA-9 probe | Engineer → same-vendor judge **PASS** (8/9/8). Finalize smoke **PASS via scoped override — RA-11 fix live-verified** → harness auto-merge `a7aa846`. Suite **1419 green**. |

### Post-fix live re-verification
- RA-3: RBAC probe now ALLOWs each claude-skill squad's own shim pair (`xenia.output.write` → ALLOW; cross-squad still denied). Residual (S4): squad.yaml declared-tool names for host-side tools (e.g. `ticket-system-bridge` @ `xenia-tickets`) remain abstract — unused by dispatcher paths, docs follow-up.
- RA-5: fresh healthcare `--squad` probe (wf f0255dc5): task `surfaced`, zero `deferred_to_host` events, workflow terminal `surfaced`. PASS.
- RA-7: `supervisor.eights_replay` fires on resume; `hydra eights-drain` drained the backlog **6,575 → 52** (persistent failures kept for retry, 2 stale .partial removed).
- RA-11: FIX-C finalize used the repo-root scoped smoke and **passed + auto-merged** — end-to-end proof.
- RA-9 (ops half): `HYDRA_OPERATOR_KEY` generated and provisioned into `~/.hydra/backends.json` (`xenia_tickets` + `hydra_control` env; backup `backends.json.bak-route-audit`); active on next gateway restart. Doctor probe reports provisioning state.
- RA-1 residual: FIX-C judge noted the attended-* worktree marker is trivially satisfied while stale worktrees exist → pruned all 8 stale merged attended worktrees (0 remain). Operator must still clear `HYDRA_PP_STAGE_ACTIVE` from the launching shell.
- RA-4: pp `finalize_run` integrity gate correctly refuses to upgrade surfaced runs to complete (can't forge). Banner suppression needs a pair-programmer-side change — residual, operator decision.

Suite progression: 1366 → **1419 passed** (+53 regression tests), 1 expected skip, across three verified merges.

## Phase 5 — Documentation & asset refresh (via Hydra, wf 0bf37ad6)

Attended engineering docs workflow: engineer → cross-vendor judge REVISE (2 factual
errors: 10s-vs-20s timeout default; RA-3 "failed"-vs-"surfaced" — judge round also
exposed a pp_codex sandbox worktree-access failure, worked around via inline evidence)
→ Reflexion×1 → judge **PASS** (1.0/1.0/1.0) → scoped smoke pass → **auto-merged `35a0da1`**.
Updated: README (alt text 5-stubs→3-stubs+2-active, eights-drain CLI, 3 new doctor probes,
Verification section), constellation.svg aria-label, D3 (active non-crown subgraph vs
3-stub subgraph), D4 (16 backends + shim map + attended/detached), ARCHITECTURE.md
(8 reconciled claims), MCP_SETUP.md (gateway timeout table). Mermaid balance-checked
post-merge; docs-only diff.

Follow-up micro-fix RA-6b (wf 246c8e8b): the shipped doctor probe used a nonexistent
tool name — corrected to `agentsmith.hydra.venom_cross_check` (verified against
AgentSmith's tool registry), judge PASS, scoped smoke pass, **auto-merged `352724a`**.

## Phase 7 — Residual round (operator-approved 2026-07-06, second session)

| Fix | Repo / workflow | Outcome |
|---|---|---|
| FIX-D: RA-12a dispatch skip + RA-12b provenance threading + RA-10 goal-text --squad (+ prose-safe repo multiplicity) + RA-3 docs | Hydra / 2418f135 (run_rWSg8bFhNcK4) | Engineer (stalled verify subprocess → continuation engineer reconciled 3 test mismatches) → judge REVISE (d-1 doc named nonexistent shim pair) → Reflexion → **PASS** (.95/.95/.95) → scoped smoke pass → **auto-merged `2688f0f`**. Suite **1435 green** (+16). **RA-12a live-verified**: resume of the merged workflow fired 2× `dispatch.attended_already_complete`, zero ghost generations (the identical scenario previously cost $10). |
| FIX-E: RA-6 smith hydra-bridge connect timeout (env AGENTSMITH_HYDRA_CONNECT_TIMEOUT_MS, default 15s vs hardcoded 2s < hydra_control's ~3s initialize) | AgentSmith / a8dc16f1 (run_swdXhQuG3nVn) | Judge **PASS** (1.0/1.0/.05); smoke skipped (no root auto-detect) → pickup merge `095848d`; vitest 99/99, tsc clean, dist rebuilt in main checkout. Back-channel becomes functional on next smith daemon restart. |
| FIX-F: RA-4 ack_run tool + banner acked_at filters; codex --add-dir for linked worktrees GATED to read-only sandbox | pair-programmer / 0938262a (run_BfP8_Q4YqaSi) | Judge REVISE (--add-dir on workspace-write would breach candidate isolation — host-analysis confirmed by codex) → Reflexion (read-only gate + guard tests) → **PASS** (1.0/1.0/.97); pickup merge `f8aabbb` (critique_failures byproducts excluded); ack-run 5/5 + codex-worktree 8/8 in main checkout, tsc clean, dist rebuilt. ack_run + sandbox fix live on next pp daemon restart. |

New residuals recorded (S4): `pp_harness.get_rubric("rfc-2119-normative")` resolves null though verdicts pin that rubric id (registry lookup gap — judges inlined the rubric); intake repo-flag multiplicity counted mid-prose tokens before the prose-safe filter (fixed in FIX-D alongside RA-10).

## Phase 6 — Final status

- Branch `fable-audit-2` @ `352724a`; five campaign merges (726e2ee, 168a474, a7aa846, 35a0da1, 352724a).
- Final suite: **1419 passed, 1 expected skip** (baseline 1366).
- Doctor (post-fix): WS-AUTH key **configured (source=backends.json)**; spool tripwire live; venom back-channel probe live (reports `hydra-mcp-unavailable` deployment state pending RA-6 back-channel wiring).
- Eights spool: 6,575 → ~60 residual persistent-failure entries (kept for retry by design).
- OPEN items for operator decision: **RA-12** (resume ghost re-dispatch of attended-completed engineering tasks, unbudgeted — proposed fix in ledger row; ~$13-16 unplanned spend observed), RA-6 back-channel wiring (AgentSmith-side config), RA-4 banner suppression (pair-programmer repo), RA-10 goal-text --squad parsing (S4), RA-3 residual squad.yaml naming docs (S4), pp_codex sandbox worktree access for judges (S4).
- Operator manual action: clear `HYDRA_PP_STAGE_ACTIVE` from the shell that launches Claude Code sessions (RA-1's leak source); restart the gateway to activate the provisioned WS-AUTH key and per-backend `connect_timeout_s` support.
