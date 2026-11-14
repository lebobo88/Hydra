# Hydra — Enterprise Agent Mesh

**Hydra is the central supervisor / state engine that routes work across heterogeneous AI agent squads** (executive, engineering, creative, legal, marketing, game development, customer support — and any future pack).

Hydra does *not* re-implement what those squads already do. It sits ABOVE them as:

1. A **supervisor graph** (LangGraph when installed, a pure-Python runner otherwise) that classifies a user goal, plans it, dispatches squad work, judges it, and synthesizes one `DECISION_RECORD`.
2. A **squad registry** — a plug-in surface where every squad is described by `squads/<slug>/squad.yaml` and is auto-discovered.
3. A **cross-squad message bus** of strongly-typed Pydantic schemas (fourteen registered types; see §3).
4. An **MCP gateway** that puts every backend server behind one MCP registration.
5. A **memory fabric** — ephemeral (in-prompt) and episodic (SQLite), with a pluggable semantic seam — served by the `hydra_memory` MCP server.
6. A **governance plane** — the constitution, HITL interrupts, budget ledger, loop ceilings, circuit breaker, redaction at squad boundaries, venom-class capability gate, and a JSONL trace per workflow.

This file describes the system as it is in the repository. The approved redesign toward one orchestrator and one engine policy is in [`docs/plans/hydra-unified-orchestration.html`](docs/plans/hydra-unified-orchestration.html) (epic Hydra#78) and is not yet implemented. The upstream research is `Enterprise Master AI Orchestration System Architecture.md`.

---

## 1. Squad Topology

| Squad | Slug | Source pack | Entrypoint | Status |
|---|---|---|---|---|
| Executive (C-Suite) | `executive` | [`ExecutiveSuite`](https://github.com/lebobo88/ExecutiveSuite) | `claude-native` | active |
| Engineering & Product | `engineering` | [`pair-programmer`](https://github.com/lebobo88/pair-programmer) | `mcp` | active |
| Garland (Creative & Production) | `garland` | [`RLM-Creative`](https://github.com/lebobo88/RLM-Creative) | `claude-native` | active |
| Senate — Legal & Compliance (Curia) | `legal-compliance` | [`Senate`](https://github.com/lebobo88/Senate) | `claude-native` | active |
| RLM-Gaming (Arcade Crown) | `rlm-gaming` | [`RLM-Gaming`](https://github.com/lebobo88/RLM-Gaming) | `claude-native` | active |
| Marketing — Strategy | `marketing-strategy` | [`MarketBliss`](https://github.com/lebobo88/MarketBliss) | `claude-native` | active |
| Marketing — Creative | `marketing-creative` | [`MarketBliss`](https://github.com/lebobo88/MarketBliss) | `claude-native` | active |
| Marketing — Research | `marketing-research` | [`MarketBliss`](https://github.com/lebobo88/MarketBliss) | `claude-native` | active |
| Marketing — Production | `marketing-production` | [`MarketBliss`](https://github.com/lebobo88/MarketBliss) | `claude-native` | active |
| Marketing — Ops | `marketing-ops` | [`MarketBliss`](https://github.com/lebobo88/MarketBliss) | `claude-native` | active |
| Planning (plan gate) | `planning` | this repo (`covenant/plan-*.cov`) | `claude-native` | active |
| Customer Support (Xenia Hearth) | `customer-support` | [`Xenia`](https://github.com/lebobo88/Xenia-Support) | `claude-skill` | active |
| Healthcare / Clinical | `healthcare` | (stub) | `stub` | scaffold |
| Sales & GTM | `sales-gtm` | (stub) | `stub` | scaffold |
| Research & Data-Science | `research-ds` | (stub) | `stub` | scaffold |

**15 squads: 12 active and 3 stubs.** The five `marketing-*` packs are filesystem symlinks into the MarketBliss checkout (`squads/marketing-* → MarketBliss/squads/marketing-*`), so they share that pack's source of truth.

The `planning` squad is a meta-squad: it authors the typed `PLAN` before any other squad is dispatched. The router and `--squad` cannot select it. Its three agents (`plan-author`, `plan-critic`, `plan-scribe`) are generated into `plugins/hydra/.claude/agents/` from `covenant/plan-*.cov`; see [`docs/covenant-pilot.md`](docs/covenant-pilot.md).

Every squad declares — in its `squad.yaml`:
- `agents:` the roster (slugs + role description + authority bounds)
- `tools:` MCP server endpoints + privilege scope
- `accepts:` message types it consumes (e.g. `CREATIVE_BRIEF`)
- `emits:` message types it produces (e.g. `SHOT_LIST`, `ASSET_JOB`)
- `gates:` rubrics + HITL checkpoints
- `entrypoint:` how Hydra invokes it (`mcp` | `subprocess` | `agent-impersonation` | `claude-skill` | `claude-native` | `stub`)
- `industries:` keyword tags used by the router

How the entrypoints behave today:

- `claude-native` — the squad runs as a plugin-scoped Claude Code subagent. `node_dispatch` always defers it to the attended host cursor; it has no in-graph path. Only the squad's lead agent is mapped (`hydra_core/native_packs.py`).
- `mcp` — engineering. Hydra drives pair-programmer's stage protocol.
- `claude-skill` — the squad is driven through its pack's skill, deferred to the host when live dispatch is on.
- `agent-impersonation` and `subprocess` — supported by the loader; no squad uses them today.
- `stub` — returns a placeholder; does no work.

Adding a new squad = dropping a folder into `squads/`. No code change to Hydra core.

---

## 2. State Graph

The graph in `hydra_core/supervisor.py` has twelve nodes:

```
USER GOAL ──► intake            classify into 1+ squads; budget, risk, repo targeting
                 │
                 ▼
              planner           triage plan rigor; seed tasks (or a planning task)
                 │
                 ▼
              approval  ◄─────  HITL gate (high risk, acceptance criteria, over budget)
                 │
                 ▼
              dispatch  ──────► squad work
                 │                 • claude-native / claude-skill: deferred to the attended host cursor
                 │                 • mcp (engineering): pair-programmer stage protocol
                 │
                 ├──► plan_judge ──► plan_gate    plan phase loop: judge the PLAN, then HITL plan approval;
                 │         ▲             │        tasks are materialised from PlanSteps on approval
                 │         └─────────────┘
                 ├──► hitl_gate_dispatch          HITL pause raised during dispatch
                 ▼
              judge_per_squad ──► hitl_gate_judge (HITL pause raised by a verdict)
                 │
                 ▼
              synthesis         merge squad results into one DECISION_RECORD
                 │
                 ▼
              judge_synthesis   judge the merged record
                 │
                 ▼
              postcheck         budget, loop ceiling, residual risk ──► done | surfaced | HITL
```

State is a typed Pydantic object (`HydraState` in `hydra_core/state.py`). Checkpointing uses LangGraph's `SqliteSaver` at `~/.hydra/checkpoints.db`, keyed by `workflow_id` (= LangGraph `thread_id`). Long-running workflows survive restarts.

The plan phase is on by default (`HYDRA_PLAN_PHASE`; only the literal `"0"` disables it). While a plan is being authored, judged or rejected, no non-planning task dispatches. Details: `ARCHITECTURE.md` §2a.

---

## 3. Cross-Squad Message Schemas

All envelopes share the base:

```python
class HydraEnvelope(BaseModel):
    id: UUID
    type: Literal[...]                # discriminator
    origin_squad: str
    target_squad: Optional[str]
    workflow_id: UUID
    parent_id: Optional[UUID]
    context_refs: list[MemoryRef]
    constraints: Constraints           # budget, deadline, risk_tolerance
    created_at: datetime
```

`SCHEMA_REGISTRY` in `hydra_core/schemas.py` registers fourteen types: `C_SUITE_DECISION_PACKET`, `PRD`, `ARCH_RFC`, `DEV_TASK`, `CREATIVE_BRIEF`, `SHOT_LIST`, `ASSET_JOB`, `HITL_REQUEST`, `DECISION_RECORD`, `HANDOFF`, `PLAN`, `SUPPORT_TICKET`, `PORTABLE_CONTEXT`, `VOC_REPORT`.

`validate_envelope` is a registry lookup plus `model_validate`. A `PLAN` with duplicate, self-referential, dangling or cyclic step dependencies is rejected at construction.

---

## 4. MCP Host Configuration

In **gateway mode**, Claude Code registers one server, `hydra_gateway`, which proxies the backends declared in `~/.hydra/backends.json`. Tool names are `mcp__hydra_gateway__<server>__<tool>`. In **standalone mode**, each server is registered directly in `~/.claude.json`.

RBAC: tools are whitelisted per squad in `squad.yaml#tools`, and `MCPStdioDispatcher` enforces the allow-list on squad-originated calls made through the engine. The gateway itself is a flat passthrough for calls made from a Claude Code session; it does not arbitrate per-squad privilege (Hydra#94 and #95 change this).

---

## 5. Memory Fabric

- **Ephemeral**: the prompt window of one node or subagent.
- **Episodic**: append-only SQLite (`~/.hydra/episodic.db`) — envelopes, verdicts, approvals, decision records. Served by the `hydra_memory` MCP server (`hydra-mem.*`).
- **Semantic**: `hydra-mem.semantic_search` is a full-text `LIKE` search over the episodic log. No vector database ships with Hydra; `hydra_core/memory.py` has an in-memory cosine index as a fallback and a seam for plugging one in. With TheEights connected and `HYDRA_MEM_FEDERATE_EIGHTS` set, search results are enriched with TheEights hybrid-search hits.
- **Cells**: writes can be tagged with the eight-cell vocabulary (`hydra_core/eights/`), and recall can filter by cell.

Agents receive **handles** (`MemoryRef`), never raw blobs, across squad boundaries.

---

## 6. Governance Plane

- **Constitution**: `CONSTITUTION.md` is read-only for agents and hash-checked; a proposed edit surfaces as HITL with reason `constitution_breach`.
- **HITL**: the graph interrupts before `approval`, `synthesis`, `judge_synthesis`, `plan_gate` and the two `hitl_gate_*` nodes. Only `/hydra:approve` or `/hydra:resume` continues a paused workflow.
- **Budget**: per-workflow ledger; the model tier is downgraded at 80% consumed and dispatch blocks for HITL at 100%.
- **Loop ceiling**: max `iterations` (default 25) and `depth` (default 5); the circuit breaker trips after 3 consecutive failures at a node.
- **Redaction**: cross-squad messages pass through `governance.redact_for_squad_boundary`.
- **Venom-class capabilities**: gated through `governance.enforce_constitution` (`docs/VENOM.md`).
- **Telemetry**: OTEL-style spans in a JSONL trace at `<project>/.hydra/<workflow_id>/trace.jsonl`.

---

## 7. Claude Code Plugin Surface

The plugin lives in `plugins/hydra/` and is published through the repo's local marketplace (`.claude-plugin/marketplace.json`).

| Surface | Items |
|---|---|
| Main agent | `hydra:hydra` (set by `plugins/hydra/settings.json`); `hydra-plaza` is the terse register of the same contract |
| Engine-spawned agents | `engineer`, `judge-cross-vendor`, `judge-same-vendor`, `plan-author` |
| Agents present but not spawned by the engine | `hydra-supervisor`, `hydra-router`, `hydra-planner`, `hydra-synthesizer`, `hydra-hitl-gate`, `hydra-cfo-gate`, `plan-critic`, `plan-scribe` (Python does this work; see Hydra#93) |
| Commands | `/hydra:run`, `/hydra:drive`, `/hydra:campaign`, `/hydra:status`, `/hydra:squads`, `/hydra:approve`, `/hydra:resume`, `/hydra:budget`, `/hydra:replay`, `/hydra:add-squad` |
| Reference skills | `plan`, `cross-squad-message`, `hitl-protocol`, `budget-control`, `squad-registry-discovery`, `memory-handles`, `workflow-recovery`, `claude-native-review`, `evidence-citation-review`, `legal-summary-intake` |
| Hooks | `SessionStart`, `UserPromptSubmit`, `PreToolUse`, `PostToolUse`, `Stop` (`plugins/hydra/hooks/hooks.json`): session contract and doctor, routing directive, blocks on direct engine-source writes and direct `/pp:*` use, Iolaus check and log |

---

## 8. Lifecycle of a Cross-Squad Workflow

User: *"Launch a Q3 campaign for the new billing microservice — needs a press kit, in-app announcement, and pricing-page update."*

Interactive sessions are **attended**: the host session drives the lifecycle in-context and the Python engine stays authoritative.

1. **`/hydra:run`** calls `hydra.workflow.plan`. `intake` classifies the goal (here `executive`, `engineering`, `garland`) and `planner` triages plan rigor. For a non-trivial goal a planning task is seeded.
2. **`hydra.workflow.step`** returns a `host_action` naming the agent to spawn. First `plan-author` writes the `PLAN`; `plan_judge` scores it and `plan_gate` pauses for the operator.
3. **`/hydra:approve`** approves the plan; one task per `PlanStep` is materialised.
4. **step / submit loop.** For each task the engine returns a `host_action`: a squad's lead agent for a `claude-native` squad, or `engineer` followed by a judge for engineering (in an isolated worktree). The host spawns exactly that agent and returns the result with `hydra.workflow.submit_host_result`. Budget is charged on stage completion.
5. **`hydra.workflow.finalize`** materialises the results and resumes the graph through `synthesis`, `judge_synthesis` and `postcheck`, returning one `DECISION_RECORD` with dissents kept.

**Detached** execution (`hydra.workflow.launch` → `hydra run --live`) is for automation and the cross-repo fleet (`/hydra:campaign "goal --repos a,b"`), gated by `HYDRA_ALLOW_DETACHED=1`.

A workflow can be replayed from its checkpoint with `/hydra:replay <workflow_id>`; replay refuses a workflow that raised a plan (`ARCHITECTURE.md` §2a).

---

## 9. What Hydra Is NOT

- Not a competitor to pair-programmer, ExecutiveSuite, RLM-Creative or the other packs. Hydra **uses** them. They remain independently shippable.
- Not a code generator. Hydra delegates code work to pair-programmer.
- Not a model gateway. Hydra delegates per-task model choice to the squad.
- Not a vendor lock-in. Any squad can swap out an LLM provider behind its MCP server without Hydra noticing.
