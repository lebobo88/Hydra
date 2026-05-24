# Hydra — Enterprise Agent Mesh

**Hydra is the central supervisor / state engine that routes work across heterogeneous AI agent squads** (executive, engineering, garland, legal, healthcare, sales, research, customer-support — and any future pack).

Hydra does *not* re-implement what those squads already do. It sits ABOVE them as:

1. A **LangGraph-based supervisor graph** that classifies a user goal, decomposes it into squad-shaped subtasks, dispatches them, and synthesizes results.
2. A **squad registry** — a plug-in surface where every squad is described by `squads/<name>/squad.yaml` and is auto-discovered.
3. A **cross-squad message bus** of strongly-typed Pydantic schemas (`C_SUITE_DECISION_PACKET`, `PRD`, `ARCH_RFC`, `DEV_TASK`, `CREATIVE_BRIEF`, `SHOT_LIST`, `ASSET_JOB`, `DECISION_RECORD`, `HITL_REQUEST`, `HANDOFF`).
4. An **MCP host** that fans out to each squad's tool surface as an isolated MCP client session.
5. A **memory fabric** with three tiers — ephemeral (in-prompt), episodic (SQLite), semantic (vector store) — exposed as MCP resources.
6. A **governance plane** — HITL interrupts, budget ledger, loop ceilings, redaction at squad boundaries, OTEL trace per workflow.

The architecture is grounded in `Enterprise Master AI Orchestration System Architecture.md` in this directory.

---

## 1. Squad Topology

| Squad | Slug | Source pack | Status |
|---|---|---|---|
| Executive (C-Suite) | `executive` | [`ExecutiveSuite`](https://github.com/lebobo88/ExecutiveSuite) | active |
| Engineering & Product | `engineering` | [`pair-programmer`](https://github.com/lebobo88/pair-programmer) | active |
| Garland (Creative & Production) | `garland` | [`RLM-Creative`](https://github.com/lebobo88/RLM-Creative) | active |
| Legal & Compliance | `legal-compliance` | (stub) | scaffold |
| Healthcare / Clinical | `healthcare` | (stub) | scaffold |
| Sales & GTM | `sales-gtm` | (stub) | scaffold |
| Research & Data-Science | `research-ds` | (stub) | scaffold |
| Customer Support | `customer-support` | (stub) | scaffold |

Every squad declares — in its `squad.yaml`:
- `agents:` the roster (slugs + role description + authority bounds)
- `tools:` MCP server endpoints + privilege scope
- `accepts:` message types it consumes (e.g. `CREATIVE_BRIEF`)
- `emits:` message types it produces (e.g. `SHOT_LIST`, `ASSET_JOB`)
- `gates:` rubrics + HITL checkpoints
- `entrypoint:` how Hydra invokes it (`mcp` | `subprocess` | `agent-impersonation` | `claude-skill`)
- `industries:` keyword tags used by the router

Adding a new squad = dropping a folder into `squads/`. No code change to Hydra core.

---

## 2. State Graph

```
              ┌──────────────────┐
USER GOAL ──► │  intake / triage │  classifies into 1+ squads, budget, SLA, risk
              └────────┬─────────┘
                       ▼
              ┌──────────────────┐
              │  planner (CEO-A) │  decomposes into typed tasks, builds DAG
              └────────┬─────────┘
                       ▼
              ┌──────────────────┐    HITL interrupt
              │  approval gate?  │ ◄──────────────┐
              └────────┬─────────┘                │
                       ▼                          │
              ┌──────────────────┐                │
              │   dispatcher     │ ──► squad subgraphs (parallel)
              └────────┬─────────┘                │
                       ▼                          │
   ┌──────────┐  ┌──────────┐  ┌──────────┐       │
   │ exec     │  │ eng      │  │ creative │  ...  │
   │ subgraph │  │ subgraph │  │ subgraph │       │
   └────┬─────┘  └────┬─────┘  └────┬─────┘       │
        └─────────────┴─────────────┘             │
                       ▼                          │
              ┌──────────────────┐                │
              │   synthesizer    │ ──► DECISION_RECORD, artifact links
              └────────┬─────────┘                │
                       ▼                          │
              ┌──────────────────┐ ─ budget/loop/risk breach ─► HITL ──┘
              │  postcheck       │
              └────────┬─────────┘
                       ▼
                    RESPONSE
```

State is a typed Pydantic object (`HydraState` in `hydra_core/state.py`), reduced by pure functions over events. Checkpointing uses LangGraph's `SqliteSaver` at `~/.hydra/checkpoints.db`, keyed by `workflow_id` (= LangGraph `thread_id`). Long-running campaigns survive restarts.

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

See `hydra_core/schemas.py` for: `CSuiteDecisionPacket`, `PRD`, `ArchRFC`, `DevTask`, `CreativeBrief`, `ShotList`, `AssetJob`, `DecisionRecord`, `HITLRequest`, `Handoff`.

Schema validation runs at every squad-boundary edge in the graph (`schema-validate` hook).

---

## 4. MCP Host Configuration

Hydra is an MCP host. It opens one isolated client session per squad-server pair declared in `.mcp.json` and in each squad's `squad.yaml`. Tool names are namespaced (`hydra-eng.pp.*`, `hydra-exec.es.*`, `hydra-mem.*`). RBAC at the host: tools are *whitelisted per squad* — an executive agent cannot call an engineering deploy tool unless the planner explicitly grants a `Handoff`.

---

## 5. Memory Fabric

- **Ephemeral**: rolling prompt window per node; aggressively summarized when over threshold.
- **Episodic**: append-only SQLite (`~/.hydra/episodic.db`) — every message, tool call, verdict, approval. Keyed by `(workflow_id, task_id)`. Surfaced as MCP resources `ep://wf/<id>/...`.
- **Semantic**: pluggable vector store (default: Chroma at `~/.hydra/vectors/`). Per-squad indexes: `code_repos`, `strategy_docs`, `brand_guidelines`, `shot_library`, `regulatory_corpus`, etc. Read access controlled by squad scope.

Agents receive **handles**, never raw blobs, to keep prompt cost predictable.

---

## 6. Governance Plane

- **HITL**: `interrupt_before=["approval_gate", "high_risk_dispatch", "synthesizer"]` on the supervisor graph. `/hydra:approve <workflow_id>` resumes.
- **Budget**: per-workflow `Constraints.budget_usd` enforced by `budget_tripwire` hook; auto-downgrades model tier when 80% consumed (mirrors PP cost router).
- **Loop ceiling**: max `iterations` (default 25) and `depth` (default 5) per supervisor; circuit-breaker disables a node after 3 consecutive failures.
- **Redaction**: cross-squad messages pass through `redactor.py` — strips PII / financial / source-of-truth fields per squad permission matrix.
- **Telemetry**: OTEL spans per node + JSONL trace at `<project>/.hydra/<workflow_id>/trace.jsonl` (PP-compatible format).

---

## 7. Claude Code Plugin Surface

| Surface | Items |
|---|---|
| Agents | `hydra-supervisor`, `hydra-router`, `hydra-planner`, `hydra-synthesizer`, `hydra-hitl-gate`, `hydra-cfo-gate` |
| Commands | `/hydra:run`, `/hydra:status`, `/hydra:squads`, `/hydra:approve`, `/hydra:resume`, `/hydra:budget`, `/hydra:campaign`, `/hydra:replay`, `/hydra:add-squad` |
| Skills | `cross-squad-message`, `hitl-protocol`, `budget-control`, `squad-registry-discovery`, `memory-handles` |
| Hooks | `SessionStart`: load registry, surface waiting HITLs; `UserPromptSubmit`: route hint; `PreToolUse`: privilege-check, schema-validate; `PostToolUse`: cost tally, trace append; `Stop`: workflow checkpoint flush |

---

## 8. Lifecycle of a Cross-Squad Workflow

User: *"Launch a Q3 campaign for the new billing-microservice — needs a press kit, in-app announcement, and pricing-page update."*

1. **`/hydra:run`** → `intake` classifies: `executive` (pricing strategy) + `engineering` (pricing page) + `creative` (press kit + in-app).
2. **planner** emits a `CSuiteDecisionPacket` (CEO+CFO+CMO impersonation via ExecutiveSuite's `boardroom` pattern) → `DECISION_RECORD` with budget split.
3. **approval gate** interrupts; user `/hydra:approve` confirms budget.
4. **dispatcher** fans out:
   - `creative` ← `CreativeBrief` → invokes RLM `rlmboard`+`copywriter`+`gemini-image` → returns `AssetJob` results.
   - `engineering` ← `PRD` → invokes pair-programmer `/pp:team feature-team` → returns PR url + smoke-pass.
   - `executive` parks dependency on `cfo` to validate pricing after both finish.
5. **synthesizer** consolidates into `DECISION_RECORD` v2 with go-live runbook, posts to episodic memory, files master-plan patches in `PROJECT_MASTER.md`.

The full trace is replayable via `/hydra:replay <workflow_id>`.

---

## 9. What Hydra Is NOT

- Not a competitor to PP, ES, or RLM. Hydra **uses** them. They remain independently shippable.
- Not a code generator. Hydra delegates code work to PP.
- Not a model gateway. Hydra delegates per-task model choice to the squad (e.g. PP's tier-aware router).
- Not a vendor lock-in. Any squad can swap out an LLM provider behind its MCP server without Hydra noticing.
