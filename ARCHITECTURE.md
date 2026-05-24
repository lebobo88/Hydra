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
|     squad_node.py  -> 4 entrypoint adapters                  |
|     governance.py  -> budget, loops, circuit break, redact   |
|     memory.py      -> episodic SQLite + semantic Chroma      |
|     telemetry.py   -> OTEL spans + JSONL trace               |
+----------------------------+---------------------------------+
                             |
        +--------------------+--------------------+
        |                    |                    |
+-------v------+    +--------v-------+   +--------v-------+
| Squad packs  |    | MCP host       |   | Memory fabric  |
| (8 today)    |    | per-squad      |   | episodic + vec |
|              |    | client session |   |                |
| executive    |    | pp-daemon      |   | ~/.hydra/      |
| engineering  |    | rlm-skills     |   |   episodic.db  |
| creative     |    | hydra-mem      |   |   vectors/     |
| 5 stubs      |    | hydra-redact   |   |   checkpoints  |
+--------------+    +----------------+   +----------------+
```

A Claude Code session loads the plugin. The plugin's hooks initialize the
registry and start the supervisor on `/hydra:run`. The supervisor compiles
a LangGraph state machine (or a pure-python fallback runner when LangGraph
is absent), dispatches each squad through one of four adapters, persists
state to LangGraph's `SqliteSaver` at `~/.hydra/checkpoints.db`, and emits
OTEL spans plus a JSONL trace at `<project>/.hydra/<workflow_id>/trace.jsonl`.

## 2. LangGraph state machine

The supervisor graph has seven explicit phases, mirroring `HYDRA.md` §2:

| Phase | Node | Purpose |
|---|---|---|
| `intake` | `hydra-router` | Classify goal -> 1+ squad slugs (deterministic keywords first, LLM fallback). Establish `Constraints` (budget, deadline, risk_tolerance, industries). |
| `planning` | `hydra-planner` | Decompose into typed `TaskState` entries, build a small DAG, populate `selected_squads`, draft a `CSuiteDecisionPacket`. |
| `approval` | `hydra-hitl-gate` | `interrupt_before` checkpoint. Sets `pending_hitl`; supervisor halts until `/hydra:approve` resumes the thread. |
| `dispatch` | `dispatcher` | Fan out to squad subgraphs in parallel, materialize one envelope per task. |
| `executing` | per-squad node | `squad_node.execute_squad()` routes through one of four adapters (mcp / subprocess / agent-impersonation / claude-skill / stub) and produces a `SquadResult`. |
| `synthesis` | `hydra-synthesizer` | Merge all `SquadResult`s into a single `DECISION_RECORD`, post artifacts to episodic memory, append a master-plan patch. |
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
                   "executing", "synthesis", "postcheck",
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
- `CreativeBrief` — produced by `executive` or `sales-gtm`; consumed by `creative`.
- `ShotList`, `AssetJob` — internal to and emitted by `creative`.
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

Hydra is an MCP host. It opens one isolated client session per
`(squad, server)` pair declared in `.mcp.json` and each squad's
`squad.yaml#tools`. Tool names are namespaced:

- `hydra-eng.pp.*` — pair-programmer harness daemon (engineering)
- `hydra-exec.es.*` — executive suite (impersonation pseudo-tools)
- `hydra-creative.rlm.*` — RLM skills
- `hydra-mem.*` — memory fabric (episodic + semantic)
- `hydra-redact.*` — redaction service at squad boundaries

RBAC is enforced at the host: each squad's allowed tools come from its
`tools:` block, and an executive agent cannot call an engineering deploy
tool unless the planner emits an explicit `Handoff` envelope that grants
the privilege.

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

- **HITL**: `interrupt_before=["approval_gate", "high_risk_dispatch", "synthesizer"]`.
- **Budget**: enforced per workflow. At 80% consumption Hydra signals the
  current squad to downgrade model tier (mirrors PP's cost router); at
  100% it blocks dispatch and raises an HITL.
- **Loop ceiling**: `iteration_count >= loop_ceiling` (default 25) or
  `depth >= depth_ceiling` (default 5) terminates with `surfaced`.
- **Circuit breaker**: three consecutive failures on the same node
  disables that node for the rest of the workflow.
- **Redaction**: every cross-squad message passes through `redactor.py`,
  which strips PII / financial / SoT fields according to the squad
  permission matrix.
- **Telemetry**: OTEL spans per node plus JSONL trace at
  `<project>/.hydra/<workflow_id>/trace.jsonl` in PP-compatible format.

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
