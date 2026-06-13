# Hydra Architecture

This document is the engineer-facing companion to `HYDRA.md`. It describes
the runtime layers, the supervisor state machine, the cross-squad message
contract, the memory fabric, the MCP host topology, the governance plane,
how Hydra delegates to its three production squad packs, and the failure
modes a contributor should expect.

## 1. Layered topology

```
+--------------------------------------------------------------+
|  User / Claude Code CLI session                              |
|     - slash commands (/hydra:run, /hydra:approve, ...)       |
|     - SessionStart, UserPromptSubmit, Stop hooks             |
+----------------------------+---------------------------------+
                             |
+----------------------------v---------------------------------+
|  Hydra Claude Code plugin (.claude-plugin/)                  |
|     - agents: hydra-supervisor, hydra-router, hydra-planner, |
|       hydra-synthesizer, hydra-hitl-gate, hydra-cfo-gate     |
|     - skills: cross-squad-message, hitl-protocol,            |
|       budget-control, squad-registry-discovery,              |
|       memory-handles                                         |
+----------------------------+---------------------------------+
                             |  (subprocess + JSON-RPC)
+----------------------------v---------------------------------+
|  hydra_core supervisor (Python, LangGraph)                   |
|     state.py       -> HydraState (typed Pydantic)            |
|     supervisor.py  -> build_supervisor() compiles the graph  |
|     router.py      -> deterministic + LLM-fallback intent    |
|     squad_loader.py-> auto-discovers squads/<slug>/squad.yaml|
|     squad_node.py  -> 5 entrypoint adapters                  |
|     governance.py  -> budget, loops, circuit break, redact   |
|     memory.py      -> episodic SQLite + semantic Chroma      |
|     telemetry.py   -> OTEL spans + JSONL trace               |
+----------------------------+---------------------------------+
                             |
        +--------------------+--------------------+
        |                    |                    |
+-------v------+    +--------v-------+   +--------v-------+
| Squad packs  |    | MCP host       |   | Memory fabric  |
| (13 today)   |    | per-squad      |   | episodic + vec |
|              |    | client session |   |                |
| executive    |    | hydra_memory   |   | ~/.hydra/      |
| engineering  |    | executive_suite|   |   episodic.db  |
| garland      |    | rlm_creative   |   |   vectors/     |
| legal/Curia  |    | hydra_control  |   |   checkpoints  |
| customer-sup |    | xenia_tickets  |   |                |
| 5 marketing  |    | hydra_toolshed |   |                |
| 3 stubs      |    | hydra_gateway  |   |                |
+--------------+    +----------------+   +----------------+
```

Squad census: **13 packs, 10 active** (executive, engineering, garland,
legal-compliance/Curia, customer-support/Xenia Hearth, and the 5 `marketing-*`
packs — the latter are filesystem symlinks into the MarketBliss checkout) **and
3 stubs** (healthcare, sales-gtm, research-ds).

A Claude Code session loads the plugin. The plugin's hooks initialize the
registry and start the supervisor on `/hydra:run`. The supervisor compiles
a LangGraph state machine (or a pure-python fallback runner when LangGraph
is absent), dispatches each squad through one of five adapters, persists
state to LangGraph's `SqliteSaver` at `~/.hydra/checkpoints.db`, and emits
OTEL spans plus a JSONL trace at `<project>/.hydra/<workflow_id>/trace.jsonl`.

## 2. LangGraph state machine

The supervisor graph has eight explicit nodes, mirroring `HYDRA.md` §2:

| Phase | Node | Purpose |
|---|---|---|
| `intake` | `hydra-router` | Classify goal -> 1+ squad slugs (deterministic keywords first, LLM fallback). Establish `Constraints` (budget, deadline, risk_tolerance, industries). |
| `planner` | `hydra-planner` | Decompose into typed `TaskState` entries, build a small DAG, populate `selected_squads`, draft a `CSuiteDecisionPacket`. |
| `approval` | `hydra-hitl-gate` | `interrupt_before` checkpoint. Sets `pending_hitl`; supervisor halts until `/hydra:approve` resumes the thread. |
| `dispatch` | `dispatcher` | Fan out to squad subgraphs in parallel via one of five adapters (mcp / subprocess / agent-impersonation / claude-skill / stub), materialize one envelope per task, produce a `SquadResult` per squad. |
| `judge_per_squad` | cross-vendor judge | Per-squad rubric evaluation via `pp_codex` / `pp_gemini`. Reflexion ×1 retry on `revise`. Skips host-pickup envelopes. |
| `synthesis` | `hydra-synthesizer` | Merge all `SquadResult`s into a single `DECISION_RECORD`, post artifacts to episodic memory, append a master-plan patch. |
| `judge_synthesis` | cross-vendor judge | Judge the merged `DecisionRecord` against synthesis-level rubrics. HITL escalation on policy breach. |
| `postcheck` | `hydra-cfo-gate` | Re-evaluate budget, loop ceiling, residual risk. May surface back to HITL or to `done`. |

Conditional edges from `postcheck` route to `done`, back to `dispatch`
(if a missed dependency was discovered), or to `surfaced` (a terminal
state meaning "human must intervene").

Checkpointing uses `SqliteSaver(thread_id=workflow_id)` so long-running
campaigns survive a restart. `/hydra:resume` re-enters at the last
persisted node; `/hydra:replay` reconstructs deterministically from the
JSONL trace.

## 3. Typed state object

The state lives in `hydra_core/state.py`. Two reducers matter for
LangGraph merging: `_append` for list fields and `_merge_dict` for the
error-counter map. Everything else is replace-by-default.

```python
class HydraState(BaseModel):
    workflow_id: UUID
    tenant_id: str = "default"
    root_goal: str = ""
    phase: Literal["intake", "planning", "approval", "dispatch",
                   "executing", "judge_per_squad", "synthesis",
                   "judge_synthesis", "postcheck",
                   "done", "surfaced"] = "intake"

    selected_squads: list[str]
    current_node: Optional[str]

    tasks: list[TaskState]                 # append-reduced
    envelopes: list[dict[str, Any]]        # append-reduced
    artifacts: list[dict[str, Any]]        # append-reduced

    episodic_refs: list[str]
    semantic_queries: list[dict[str, Any]]

    budget: BudgetLedger                   # budget_usd, spent_usd, tokens
    iteration_count: int = 0
    depth: int = 0
    loop_ceiling: int = 25
    depth_ceiling: int = 5
    error_counters: dict[str, int]         # merge-reduced

    requires_human_approval: bool = False
    pending_hitl: Optional[dict[str, Any]]
    hitl_history: list[dict[str, Any]]
```

`TaskState` carries `task_id`, `owner_squad`, `description`, `status`
(pending/running/blocked/done/failed/surfaced), the envelope id that
triggered it, and a priority tag (P0..P3). Tasks are the unit the
synthesizer reasons over.

## 4. Cross-squad message schemas

Every cross-squad message inherits `HydraEnvelope` (`hydra_core/schemas.py`):

```python
class HydraEnvelope(BaseModel):
    id: UUID
    type: Literal[...]                    # discriminator
    origin_squad: str
    target_squad: Optional[str]
    workflow_id: UUID
    parent_id: Optional[UUID]
    context_refs: list[MemoryRef]         # handles, not blobs
    constraints: Constraints              # budget, deadline, risk_tolerance
    created_at: datetime
```

The discriminator types currently defined:

- `CSuiteDecisionPacket` — produced by `executive`, consumed by anyone.
- `PRD` — product requirement doc; produced by `engineering` or `executive`.
- `ArchRFC` — architecture RFC; produced by `engineering`.
- `DevTask` — granular implementation unit; consumed by `engineering`.
- `CreativeBrief` — produced by `executive` or `sales-gtm`; consumed by `garland`.
- `ShotList`, `AssetJob` — internal to and emitted by `garland`.
- `DecisionRecord` — terminal artifact, emitted by every squad and merged
  by the synthesizer.
- `HITLRequest` — any node can raise one; surfaces to the approval gate.
- `Handoff` — explicit cross-squad delegation with privilege scope.

A `schema-validate` hook runs at every squad-boundary edge. Invalid
envelopes fail closed (the task moves to `failed` and the error counter
increments).

## 5. Memory tiers

Three tiers, all exposed as MCP resources so any squad can read them by
handle:

| Tier | Storage | Lifetime | Handle form |
|---|---|---|---|
| Ephemeral | rolling prompt window | one node invocation | n/a (inline) |
| Episodic | SQLite, `~/.hydra/episodic.db` | append-only, full retention | `ep://wf/<workflow_id>/<task_id>/<seq>` |
| Semantic | Chroma, `~/.hydra/vectors/<index>/` | indefinite, indexed | `sem://<index>/<doc_id>` |

`hydra_core/memory.py` provides `append_episodic`, `resolve_episodic`,
`list_episodic`, and `SemanticIndex` with per-squad read scoping. Agents
receive **handles** — never raw payloads — to keep prompt cost predictable.
The synthesizer is the only node allowed to dereference handles in bulk.

## 6. MCP host topology

Hydra consumes MCP servers registered at operator-managed user scope
(`~/.claude.json` mcpServers). It does **not** register them itself.
The servers fall into three categories:

### 6a. In-repo MCP servers (shipped with Hydra)

| Server | Location | Purpose |
|--------|----------|---------|
| `hydra_memory` | `mcp_servers/hydra_memory/server.py` | Thin shim over SQLite episodic store + TheEights cells |
| `executive_suite` | `mcp_servers/executive_suite/server.py` | Read-only introspection of ExecutiveSuite's `.claude/` directory (roster, skills, commands, output) |
| `rlm_creative` | `mcp_servers/rlm_creative/server.py` | Read-only introspection of RLM-Creative's `.claude/` directory |
| `hydra_toolshed` | `mcp_servers/hydra_toolshed/server.py` | Search-describe-execute meta-tools over large tool catalogs (Speakeasy Dynamic Toolsets pattern) |
| `hydra_gateway` | `mcp_servers/hydra_gateway/server.py` | Unified proxy — consolidates all backend servers behind a single MCP registration with static tool catalog and on-demand backend connections |
| `hydra_control` | `mcp_servers/hydra_control/server.py` | Workflow-resume gate + cockpit audit filing. 3 tools: `hydra.control.ping`, `hydra.workflow.resume` (launches the CLI resume path for a paused HITL gate — approve/reject/modify-budget/force-dispatch), `hydra.cockpit.audit` (files a `cockpit_write` audit envelope through the attestor for actions taken from the mesh console) |
| `xenia_tickets` | `mcp_servers/xenia_tickets/server.py` | Customer-support ticket filing for the Xenia Hearth squad. 9 tools: `xenia-tickets.create`, `.get`, `.list`, `.comment`, `.update_fields`, `.recommend`, `.send_response`, `.execute_approved`, `.ping`. Mutating tools (`send_response`, `execute_approved`) verify a server-side WS-AUTH operator/agent capability token (`mint_for_tool.py` + `clearance.py`) before acting — actor identity is taken from the dispatcher binding, never self-reported |

(Other shipped pack-shim servers — `xenia`, `xenia_kb`, `senate`,
`marketbliss` — follow the same read-only-introspection pattern as
`executive_suite`/`rlm_creative`.)

These are pack-shim servers that read filesystem state from sibling
projects. They are **not** the primary execution path for their
respective squads — `executive_suite` and `rlm_creative` provide
enrichment data (live roster, skill catalogue) while the actual work
flows through `agent-impersonation` and `claude-skill` entrypoints.
`hydra_control` and `xenia_tickets` are the exception: they perform
real side-effecting writes (workflow resume, audit filing, ticket
mutation) and therefore enforce HITL-capability / WS-AUTH gating.

### 6b. Externally registered sibling servers (operator must install)

| Server | Source project | Purpose |
|--------|---------------|---------|
| `pp_harness` | [pair-programmer](https://github.com/lebobo88/pair-programmer) | Engineering work: `start_run`, `start_stage`, `archive_artifact` |
| `pp_codex` | pair-programmer daemon | Cross-vendor critique (OpenAI Codex) |
| `pp_gemini` | pair-programmer daemon | Cross-vendor critique (Google Gemini) |
| `eights` | [TheEights](https://github.com/lebobo88/TheEights) | Evolution, governance, memory, cells |
| `agentsmith` | [AgentSmith](https://github.com/lebobo88/AgentSmith) | Artifact validation, audit, constitution attestation |

In **standalone mode** these are registered directly in `~/.claude.json`.
In **gateway mode** their specs live in `~/.hydra/backends.json` and the
gateway proxies them — see §6e.

### 6c. Non-MCP execution paths

| Squad | Entrypoint | Mechanism |
|-------|-----------|-----------|
| Executive | `agent-impersonation` | `dispatcher.emit_claude_prompt()` → host-pickup envelope; Claude Code impersonates the persona in-process |
| Garland | `claude-skill` | `dispatcher.invoke_claude_skill()` → host-pickup envelope; Claude Code invokes the skill |
| Stubs | `stub` | Returns a placeholder `DecisionRecord` |

Only the Engineering squad uses MCP as its primary execution path.

### 6d. RBAC enforcement

Each squad's `squad.yaml#tools` block declares which tools and MCP
servers the squad is authorized to use, with privilege levels
(`read | write | execute | destructive`). `MCPStdioDispatcher.call_mcp()`
validates every call against the calling squad's tool allowlist and
rejects unauthorized access with a telemetry event. Cross-squad tool
delegation is supported via `Handoff` envelopes carrying explicit
`granted_tools` lists with expiration timestamps.

### 6e. Gateway consolidation (two-layer registry)

In gateway mode, Claude Code registers only `hydra_gateway`. Backend
specs live in a separate Hydra-owned file:

| Layer | File | Reader |
|-------|------|--------|
| Claude-visible | `~/.claude.json` (only `hydra_gateway`) | Claude Code |
| Backend registry | `~/.hydra/backends.json` | Gateway + internal dispatcher |

The gateway reads `backends.json`, connects to each backend via stdio,
and re-exposes their tools under namespaced names:
`{server}__{tool_name}` → Claude sees `mcp__hydra_gateway__{server}__{tool_name}`.

Hydra's internal dispatcher (`MCPStdioDispatcher`) also reads
`backends.json` as a fallback, so supervisor/judge/squad_node calls
still work when backends are absent from `~/.claude.json`.

See `docs/MCP_SETUP.md` for migration and setup instructions.

## 7. Governance plane

`hydra_core/governance.py` is intentionally small and pure. The key
functions:

```python
record_cost(state, usd, tokens)            # increments BudgetLedger
should_downgrade_model(state, threshold=0.8)
should_block_for_budget(state)
should_circuit_break(state, node, max_consecutive=3)
redact_for_squad_boundary(text, *, allow_pii=False)
enforce_governance(state, ...) -> GovernanceVerdict
```

Behaviour:

- **HITL**: `interrupt_before=["approval", "synthesis", "judge_synthesis"]`.
  The planner builds and files the `pending_hitl` request *before* the
  interrupt (so a paused workflow already carries the gate for
  `/hydra:status` and TheEights' HITL queue). Three triggers raise the
  approval gate:
  - **high_risk** — any P0/P1 task, or any task whose squad has an explicit
    `hitl_required` gate. `reason="high_risk"` is a frozen-contract value the
    mesh console keys off of.
  - **acceptance_criteria** (WS9) — a major (P0/P1) task on a squad *without*
    its own `hitl_required` gate is dispatching with no structured
    `TaskState.acceptance_criteria`. The gate pauses with
    `reason="acceptance_criteria"` and options
    `[approve_with_criteria, reject, modify-budget]`, asking the operator to
    confirm or supply criteria before dispatch. When a squad gate already
    fired (`reason="high_risk"` wins), missing-criteria detail is still
    appended to the summary so the operator is never blind to it.
  - **over_budget** — see Budget below.
- **Model tier propagation** (WS9): `TaskState.model_tier` (`haiku | sonnet |
  opus | fable | deep`, normalised by `hydra_core/tiers.py#normalize_tier`,
  fail-closed on unknown tokens) is threaded from the dispatch envelope /
  operator flag onto every per-squad dispatch packet and onto best-of-N and
  Reflexion-retry packets (`supervisor.py`). `fable`/`deep` route to
  pair-programmer's deep-reasoning team and are operator/flag-driven only —
  there is no automatic escalation path.
- **Budget**: enforced per workflow. At 80% consumption Hydra signals the
  current squad to downgrade model tier (mirrors PP's cost router); at
  100% it blocks dispatch and raises an HITL.
- **Loop ceiling**: `iteration_count >= loop_ceiling` (default 25) or
  `depth >= depth_ceiling` (default 5) terminates with `surfaced`.
- **Circuit breaker**: three consecutive failures on the same node
  disables that node for the rest of the workflow.
- **Redaction**: every cross-squad message passes through
  `governance.redact_for_squad_boundary()`, which strips PII (SSN,
  credit card, email, phone) and neutralizes MCP-attack patterns
  (prompt injection, cross-tool exfiltration, base64 obfuscation).
  Applied at squad dispatch and synthesis boundaries in `supervisor.py`.
- **Envelope validation**: `schemas.validate_envelope()` runs at every
  squad-boundary edge. Invalid envelopes fail closed (task → `failed`,
  error counter increments).
- **Telemetry**: OTEL spans per node plus JSONL trace at
  `<project>/.hydra/<workflow_id>/trace.jsonl` in PP-compatible format.
  Boundary crossings (dispatch, synthesis, redaction, RBAC violations)
  emit structured trace events.

## 8. Delegation to PP, ES, and RLM

**Pair-programmer (engineering).** Entrypoint is `mcp`. Hydra opens an
MCP client session against the local `pp-daemon` and invokes
`pp.harness.start_run`, `pp.harness.start_stage`, `pp.codex.generate`,
`pp.gemini.critique`, and `pp.harness.archive_artifact`. The squad
yaml's `invoke.mode` selects between `pp_run`, `pp_team`, `pp_best_of`,
and `pp_review`; `default_team` is `feature-team`, `forum_for_review`
is `change-advisory-board`. Engineering produces `PRD`, `ARCH_RFC`,
`DEV_TASK`, and `DECISION_RECORD` envelopes. See
`squads/engineering/squad.yaml` for the full surface.

**ExecutiveSuite (executive).** Entrypoint is `agent-impersonation` —
ES is a Claude Code sub-agent pack, not a subprocess. Hydra emits a
Claude prompt naming the desired persona (`/board-meeting`,
`/capital-decision`, `/crisis-mode`, `/mna-review`, etc.) and ES's
sub-agents take over the conversation turn within the same Claude
session. The 20+ C-level personas plus 4 orchestrators (`boardroom`,
`capital-allocation`, `crisis-warroom`, `mna-cockpit`) emit a
`CSuiteDecisionPacket` which the planner converts to per-squad tasks.

**RLM-Creative (garland).** Entrypoint is `claude-skill`. Hydra
invokes `/rlm-team` (or fallbacks `/creative-campaign`,
`/photo-direction`, `/brand-refresh`)
through the Claude Code skill mechanism. The garland squad emits
`SHOT_LIST`, `ASSET_JOB`, and `DECISION_RECORD`. Outputs land under
`RLM/output/{phase}/{topic}-{date}.md` per the squad yaml.

## 8a. Cross-repo fleet dispatch (WS8)

A single workflow can fan out engineering work across *distinct sibling
repos* in parallel. Three `hydra_core` modules cooperate.

### Sibling-repo registry (`hydra_core/repo_registry.py`)

`target_repo_id` on `HydraState` directs pair-programmer at a sibling
repository. Resolution is **allow-list-only** — raw path strings are
rejected (the allow-list is the injection guard). The allow-list maps
nine `repo_id`s (`hydra`, `pair-programmer`, `agentsmith`, `theeights`,
`xenia`, `executivesuite`, `senate`, `marketbliss`, `rlm-creative`) to
exact on-disk folder names under a shared base dir
(`…/AiAppDeployments/`, overridable via `HYDRA_REPO_BASE`).
`resolve_repo_path()` layers three guards: allow-list lookup → base-escape
check (`candidate.is_relative_to(base)` defeats symlink traversal) → a real
local `git -C <path> rev-parse --show-toplevel` (10 s timeout, no network)
that must round-trip to the same path. CLI parsing helpers:
`parse_repo_arg` (single `--repo <id>` / `--repo=<id>`) and
`parse_repos_arg` (`--repos`/`--fleet <id,id,…>`, comma-list with
whitespace tolerance, dedup preserving first-occurrence order).

### Parallel fan-out (`hydra_core/fleet.py`)

`dispatch_fleet()` is a bounded-concurrency `ThreadPoolExecutor` fan-out
over `execute_squad`. Invariants:

- **Distinct-repo guard** — two tasks sharing a `target_repo_id` (including
  two both `None`) would collide on the per-project `.harness/.lock`;
  duplicates are rejected *before* fan-out.
- **mcp-only eligibility** — only `entrypoint="mcp"` packs are fleet-eligible.
  Non-mcp packs race on `state.error_counters` or ignore `target_repo_id`,
  so they stay on the sequential path.
- **No shared-state mutation in workers** — each worker gets a *fresh*
  `Dispatcher` from `dispatcher_factory()` (own asyncio loop, no shared
  `run_until_complete()` races) and a per-call collector list; the main
  thread merges `open_pp_runs` and the per-task `ToolUsageTracker` slots
  after the join.
- **Per-repo budget isolation** — each repo's run is scoped to its own
  `.harness` budget; one repo overrunning does not charge another.
- **Deterministic ordering** — results are collected via `as_completed`
  (so cancellation fires on the first surfaced result regardless of submit
  order) but stored by **input index**, so `results[i]` always corresponds
  to `tasks[i]`.
- **Cancellation tokens** — an optional `cancel_event` (read-only to
  workers; set only by the main-thread collection loop) plus a
  `should_cancel(result)` predicate. On trigger, `cancel_event` is set and
  `future.cancel()` is called on every not-yet-started future, whose slot is
  filled with `status="cancelled"`. In-flight workers past their entry-check
  complete naturally — threads cannot be force-killed.
- **Failure isolation** — one worker's exception yields a
  `SquadResult(status="failed")` for that slot only; it never crashes the
  fleet or cancels siblings.

Concurrency is clamped to `[1, FLEET_MAX_CAP=8]` (default 4 via
`state.fleet_max_concurrency`). Campaign wiring chains fleet dispatches
across phases with deterministic multi-repo synthesis.

### Operator-capability tokens (`hydra_core/auth/capability.py`)

WS-AUTH mints HMAC-SHA256 operator-capability tokens that gate HITL
resume and cross-system writes. Format is byte-identical to Xenia's signer
so one token verifies in both systems on a shared key:

```
canonical = json.dumps({all fields except "sig"}, sort_keys=True,
                        separators=(",",":"), ensure_ascii=True)
sig.value = base64url-nopad( HMAC-SHA256(HYDRA_OPERATOR_KEY, canonical) )
```

- **Mint** — `mint_capability(payload)` requires `v, actor_id, actor_kind,
  capability`; auto-generates a `jti` nonce and enforces strict expiry (`exp`
  must be an exact `int`; default TTL 900 s). `mint_for_approval()` /
  `apply_approval(state, operator)` derive the capability + `resource_id`
  from the pending HITL gate and stash the token on
  `state.operator_capability`. The seam is invoked by `cli.py`
  `_cmd_resume_locked` on approve and by the `hydra-hitl-gate` agent.
- **Inject** — the minted token rides on the resume / dispatch path to the
  consuming system.
- **Verify** — `verify_capability` (lower-level) and
  `verify_operator_capability` (strict: `v==1`, `actor_kind=="human"`,
  non-sentinel `actor_id`, required `workflow_id`/`resource_id` binding for
  replay-prevention). Both **fail closed** on every error path including
  degraded tokens, expiry (`now >= exp`), and signature mismatch
  (constant-time compare), and never raise for any input shape.
- **Degraded mode** — when `HYDRA_OPERATOR_KEY` is unset the token is still
  produced with `sig.value=None, degraded=True` (instrument-first posture);
  the approval is not blocked on Hydra's side, but consumers that call the
  verifiers (TheEights and Xenia enforce them server-side on governance /
  ticket writes) reject degraded tokens. The CLI logs a warning.

## 9. Failure modes and recovery

| Failure | Detection | Recovery |
|---|---|---|
| Squad subprocess / MCP server crashes | `squad_node.execute_squad` catches, increments `error_counters[node]` | Retry up to 2x; on 3rd consecutive failure the circuit breaker disables the node and surfaces the workflow. |
| Schema validation fails at squad boundary | `schema-validate` hook on the dispatch edge | Task -> `failed`. Synthesizer reports it; user can patch the envelope and `/hydra:resume`. |
| Budget exceeded mid-workflow | `should_block_for_budget` | Pending tasks pause, HITL request raised. `/hydra:approve` may grant a budget bump. |
| Loop ceiling tripped | `state.is_looping()` checked at `postcheck` | Workflow -> `surfaced`. Trace tells the human where it spun. |
| LangGraph absent | Importerror during `build_supervisor` | Falls back to `_PurePythonRunner` (same state contract, no checkpointing). `hydra doctor` warns. |
| Claude Code session killed mid-run | LangGraph checkpoint exists at `~/.hydra/checkpoints.db` | `/hydra:resume <workflow_id>` re-enters at the last persisted node. |
| Deterministic replay needed (audit) | JSONL trace contains every envelope + tool call hash | `/hydra:replay <workflow_id>` reconstructs prompts, model ids, artifact hashes. |

`/hydra:status` surfaces the count of `surfaced` workflows at
SessionStart so they don't get lost.

## 10. Instrumenting a new squad's hot path

When you add or activate a squad, instrument its execution as follows:

1. **Trace.** In your squad node adapter, call
   `telemetry.emit(project, workflow_id, event, payload)` at entry, on
   each tool call, and on result. Use the existing event names
   (`node_start`, `tool_call`, `node_end`, `node_error`) so the JSONL
   trace stays uniform.
2. **Cost.** After every billable model call, call
   `governance.record_cost(state, usd, tokens)`. This drives downgrade
   and HITL.
3. **Episodic memory.** Persist every outbound envelope and every
   non-trivial tool result with `memory.append_episodic(workflow_id,
   task_id, payload)` and return the resulting handle in
   `state.episodic_refs`.
4. **Redaction.** Wrap any text crossing a squad boundary in
   `governance.redact_for_squad_boundary(text, allow_pii=...)`. Default
   is `allow_pii=False`.
5. **Circuit breaker.** When you catch and swallow an exception from a
   downstream tool, increment `state.error_counters[node]` before
   continuing — otherwise the breaker can't see consecutive failures.
6. **Gates.** Honor the `gates:` block from `squad.yaml`. If a gate has
   `hitl_required: true` and its `when:` condition evaluates true, set
   `state.requires_human_approval = True` and populate `pending_hitl`.

Following these six steps means the squad participates correctly in
budget, retry, audit, and replay without any change to `hydra_core`.
