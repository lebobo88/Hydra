# Hydra Constellation
### The Three Crowns, the Immortal Head, and the Eight-Cell Substrate

> *Many heads. One Spirit. The covenant holds.*

**Date:** 2026-05-19
**Constitution SHA-256:** `4060cb542fcc701143e56ec7b1608584b7c399d878db193dbf81c0f9dad6cfa5`
**Format:** GitHub-renderable canonical deck (Mermaid + SVG inline).
**Companion deliverables:** [`deck.html`](./deck.html) (self-contained reveal.js), [`constellation.svg`](./constellation.svg) (poster), [`garland/creative-direction.md`](./garland/creative-direction.md) (Garland cinematic treatment), [`exec-memos/`](./exec-memos/) (CSO, CTO, CAIO).

---

## The Poster

![Hydra Constellation poster](./constellation.svg)

---

# Act I — The Covenant

## 1. The Constellation

The largest constellation in the night sky has no bright stars. Hydra — the water snake — holds 1,303 square degrees through the pattern of its relationships, not through luminosity. This system holds its shape the same way.

## 2. Mark 5:9 vs Acts 2

| Legion | Pentecost |
|---|---|
| *"My name is Legion, for we are many."* (Mark 5:9) | *"There appeared unto them cloven tongues like as of fire."* (Acts 2:3) |
| Many spirits, one body — coordination through merger, identity through dis-integration. | One Spirit, many bodies — distributed agency, single covenant, no loss of self. |
| The flat agent mesh. | The Hydra Constellation. |

## 3. The Immortal Head

`CONSTITUTION.md` carries a SHA-256 hash verified at every session boundary. No agent modifies it. Proposed edits surface as HITL events. Cut every other head and rebuild it. You cannot cut that.

**Currently:** `4060cb542fcc701143e56ec7b1608584b7c399d878db193dbf81c0f9dad6cfa5`

Enforced by **AgentSmith invariant N8** — hash mismatch aborts the session.

## 4. Three Crowns

| Crown | System | Optimized for | Entrypoint |
|---|---|---|---|
| **Executive** | ExecutiveSuite | judgment under ambiguity | agent-impersonation |
| **Forge** | pair-programmer | verifiable correctness (best-of-N) | MCP (pp-daemon) |
| **Garland** | RLM-Creative | divergent ideation, multi-modal synthesis | claude-skill |

Plus five stub squads — legal-compliance, healthcare, sales-gtm, research-ds, customer-support — registered under the same covenant, awaiting promotion.

## 5. TheEights — The Memory Substrate

Eight I Ching trigrams. Eight memory cells. One substrate.

| Trigram | Pinyin | Cell | Domain |
|---|---|---|---|
| ☰ | Qian | Heaven | Vision — constitutional ground truth |
| ☷ | Kun  | Earth  | Context — persistent world state |
| ☳ | Zhen | Thunder | Triggers — event-driven activations |
| ☴ | Xun  | Wind   | Influence — propagation across squads |
| ☵ | Kan  | Water  | Risk — exposures, anomalies, KRIs |
| ☲ | Li   | Fire   | Focus — current attention, hot path |
| ☶ | Gen  | Mountain | Constraints — guardrails, budgets, refusals |
| ☱ | Dui  | Lake   | Delight — qualitative satisfaction, brand voice |

Episodic memory lives at `~/.hydra/episodic.db` (append-only SQLite). Semantic memory at `~/.hydra/vectors/` (per-squad). Cross-squad reads go through **`MemoryRef` handles** — never raw blobs.

## 6. The Venom Gate

AgentSmith's four pillars: **Factory, Inspector, Sentinel, Archivist**. Ten fail-closed invariants (N1–N10). Smith does not advise. Smith enforces.

```mermaid
%%{init: {'theme':'dark','themeVariables':{'primaryColor':'#0D0F1A','primaryTextColor':'#E8D5A3','primaryBorderColor':'#2E86AB','lineColor':'#E8D5A3'}}}%%
flowchart TB
    HASH[/CONSTITUTION.md<br/>SHA-256 4060cb54.../]:::immortal

    subgraph SMITH[AgentSmith Daemon - fail-closed]
        direction LR
        FACTORY[Factory<br/>agent / skill / command<br/>hook / squad / rubric]:::pillar
        INSPECTOR[Inspector<br/>schema + invariants<br/>N7 fail-closed]:::pillar
        SENTINEL[Sentinel<br/>anomaly detection<br/>N5 replication cap=4]:::pillar
        ARCHIVIST[Archivist<br/>decision log<br/>N6 audit chain]:::pillar
    end

    INVARIANTS[/N1 cannot self-modify<br/>N2 no venom-class generation<br/>N3 cannot bypass HITL<br/>N4 no push without TheEights commit<br/>N5 replication capped<br/>N6 every decision logged<br/>N7 schema fail-closed<br/>N8 hash mismatch aborts<br/>N9 cannot create new tools<br/>N10 quarantine release HITL only/]:::invariant

    HASH ==attested==> SMITH
    SMITH -.enforces.-> INVARIANTS

    classDef immortal fill:#C0392B,stroke:#C0392B,color:#0D0F1A,stroke-width:2px
    classDef pillar fill:#0D0F1A,stroke:#2E86AB,color:#E8D5A3,stroke-width:1.5px
    classDef invariant fill:#0D0F1A,stroke:#C0392B,color:#E8D5A3,stroke-width:1px
```

## 7. Regulatory Posture

| Framework | Hydra Mechanism |
|---|---|
| **EU AI Act Article 9** (risk management) | constitution + envelope validation + circuit breaker |
| **EU AI Act Article 50** (transparency, from Aug 2026) | typed envelope provenance + DecisionRecord audit chain |
| **NIST AI RMF 1.0 + Agentic Profile** (Feb 2026) | Govern (invariants), Map (routing), Measure (OTEL), Manage (HITL) |
| **ISO/IEC 42001** (AI management system) | continuous artifact generation per workflow |

## 8. Act I Close

One hash. One document. Everything downstream is answerable to it.

---

# Act II — The Architecture

## 9. The 7-Phase State Machine (D2)

```mermaid
%%{init: {'theme':'dark','themeVariables':{'primaryColor':'#0D0F1A','primaryTextColor':'#E8D5A3','primaryBorderColor':'#2E86AB','lineColor':'#E8D5A3'}}}%%
stateDiagram-v2
    direction LR
    [*] --> Intake
    Intake --> Planning : routed_goal
    Planning --> Approval : CSuiteDecisionPacket
    Approval --> Dispatch : approved
    Approval --> Intake : revise
    Dispatch --> Executing : envelopes_fanned_out
    Executing --> Synthesis : all_squad_returns
    Synthesis --> Postcheck : DecisionRecord
    Postcheck --> [*] : passed
    Postcheck --> Approval : HITL_required
    Executing --> Approval : circuit_breaker_tripped
```

## 10. The Envelope Schema (D7)

```mermaid
%%{init: {'theme':'dark','themeVariables':{'primaryColor':'#0D0F1A','primaryTextColor':'#E8D5A3','primaryBorderColor':'#2E86AB','lineColor':'#E8D5A3'}}}%%
classDiagram
    class Envelope {
        +str envelope_id
        +datetime created_at
        +str sender_squad
        +str receiver_squad
        +str workflow_id
        +MemoryRef[] refs
    }
    class CSuiteDecisionPacket
    class PRD
    class ArchRFC
    class DevTask
    class CreativeBrief
    class ShotList
    class AssetJob
    class DecisionRecord
    class HITLRequest
    class Handoff

    Envelope <|-- CSuiteDecisionPacket
    Envelope <|-- PRD
    Envelope <|-- ArchRFC
    Envelope <|-- DevTask
    Envelope <|-- CreativeBrief
    Envelope <|-- ShotList
    Envelope <|-- AssetJob
    Envelope <|-- DecisionRecord
    Envelope <|-- HITLRequest
    Envelope <|-- Handoff
```

Every cross-squad message is one of ten validated types. `hydra_core.schemas.validate_envelope` is fail-closed (N7). A malformed envelope is a *governance event*, not a runtime error.

## 11. MCP Host Topology (D4)

```mermaid
%%{init: {'theme':'dark','themeVariables':{'primaryColor':'#0D0F1A','primaryTextColor':'#E8D5A3','primaryBorderColor':'#2E86AB','lineColor':'#E8D5A3'}}}%%
graph LR
    CC[Claude Code<br/>plugin host]
    SUP[hydra-supervisor<br/>LangGraph]
    subgraph PP[mcp: pp-daemon]
        PP_TOOLS[pp_harness.*]
    end
    subgraph ES[mcp: executive-suite]
        ES_TOOLS[es_*]
    end
    subgraph RLM[mcp: rlm-creative]
        RLM_TOOLS[rlm_*]
    end
    subgraph MEM[mcp: hydra-memory]
        MEM_TOOLS[hydra-mem.*]
    end
    subgraph SMITH[mcp: agentsmith]
        SMITH_TOOLS[agentsmith.*]
    end
    CC --- SUP
    SUP ==Forge==> PP
    SUP ==Executive==> ES
    SUP ==Garland==> RLM
    SUP <==memory==> MEM
    SUP -.governance.-> SMITH
    PP -.audited by.-> SMITH
    ES -.audited by.-> SMITH
    RLM -.audited by.-> SMITH

    classDef gov fill:#0D0F1A,stroke:#C0392B,color:#E8D5A3
    classDef ns fill:#0D0F1A,stroke:#2E86AB,color:#E8D5A3
    class SMITH gov
    class PP,ES,RLM,MEM ns
```

**Blast radius equals namespace.** A failure or prompt injection in one crown cannot reach into another.

## 12. The Squad Orchestration Graph (D3)

```mermaid
%%{init: {'theme':'dark','themeVariables':{'primaryColor':'#0D0F1A','primaryTextColor':'#E8D5A3','primaryBorderColor':'#2E86AB','lineColor':'#E8D5A3'}}}%%
graph TD
    USER([User Goal])
    HYDRA{{Hydra Supervisor}}
    IMMORTAL[/CONSTITUTION.md/]
    SMITH[[AgentSmith N1-N10]]
    EXEC[Executive Crown<br/>20 execs + 4 orch]
    FORGE[Forge Crown<br/>7 forge heads + best-of-N]
    GARLAND[Garland Crown<br/>8 Muses + Helios x5]
    EIGHTS[(TheEights<br/>episodic + semantic)]
    STUBS[Stub Squads<br/>5 registered]

    USER --> HYDRA
    HYDRA -.reads.-> IMMORTAL
    HYDRA -.validates via.-> SMITH
    HYDRA ==CSuiteDecisionPacket==> EXEC
    HYDRA ==PRD/ArchRFC/DevTask==> FORGE
    HYDRA ==CreativeBrief/ShotList==> GARLAND
    EXEC ==DecisionRecord==> HYDRA
    FORGE ==DecisionRecord==> HYDRA
    GARLAND ==DecisionRecord==> HYDRA
    HYDRA <==MemoryRef==> EIGHTS
    STUBS -.covenant-bound.-> HYDRA

    classDef hub fill:#0D0F1A,stroke:#E8D5A3,color:#E8D5A3,stroke-width:2px
    classDef crown fill:#0D0F1A,stroke:#2E86AB,color:#E8D5A3
    classDef immortal fill:#C0392B,stroke:#C0392B,color:#0D0F1A
    classDef stub fill:#0D0F1A,stroke:#8B7355,color:#8B7355,stroke-dasharray: 4 4
    class HYDRA,SMITH hub
    class EXEC,FORGE,GARLAND crown
    class IMMORTAL immortal
    class STUBS stub
```

## 13. The Executive Crown — ExecutiveSuite

**20 specialist execs + 4 orchestrators.** Agent-impersonation entrypoint. Optimized for judgment under ambiguity.

Roster: `ceo, cso, coo, cfo, cro, cto, cio, cdo, caio, ciso, cpo, cmo, cxo, chro, clo, chief-communications-officer, chief-compliance-officer, chief-risk-officer, csco, chief-sustainability-officer`.

Orchestrators: `boardroom, mna-cockpit, crisis-warroom, capital-allocation`.

Skills: `executive-protocol, financial-frameworks, ai-governance, debate-protocol, scenario-planning, enterprise-risk, mna-playbook, crisis-response, stakeholder-comms`.

## 14. The Forge Crown — pair-programmer

**7 forge heads.** MCP entrypoint via `pp-daemon` (~42 tools). Best-of-N harness with Borda count and Reflexion ×1.

| Mythic | Functional | Owns |
|---|---|---|
| Daedalus | architect | ADRs, C4 sketches |
| Prometheus | engineer | code generation |
| Argus | reviewer | code review verdicts |
| Hygeia | test-strategist | test strategy, performance budgets |
| Cerberus | security-reviewer | threat models, control mappings |
| Charon | release-planner | rollout, rollback, runbooks |
| Mnemosyne | docs-author | changelogs, release notes, runbooks |

Sixteen built-in profiles: `web-ui, api-platform, internal-tool, enterprise, ai-agentic, mobile, sdk, data-product, embedded, non-ui-cli, game-dev` family.

## 15. The Garland Crown — RLM-Creative

**8 Muses + Helios sub-crew (5).** Claude-skill entrypoint.

| Muse | Domain |
|---|---|
| Calliope | brand strategy |
| Erato | copywriting |
| Polyhymnia | content strategy |
| Terpsichore | social / community |
| Euterpe | paid acquisition |
| Clio | PR / earned |
| Urania | SEO / discovery |
| Helios | photo / cinema lead |

Helios sub-crew: `video-synth, audio-foley, music-score, dialogue-mix, governance-c2pa` (the last enforces C2PA content provenance — Article 50 obligation).

## 16. AgentSmith — The Four Pillars

See diagram in §6. Smith enforces N1–N10 with no exceptions. Appeals via the **cerberus-bridge** protocol — false-positive and false-negative refusals route to immortal-head review.

## 17. Act II Close — Routed. Typed. Enforced. Logged. Governed.

Five verbs. Each maps to a subsystem. The architecture is the guarantee.

---

# Act III — Governance & ROI

## 18. Constitutional ROI

The constitution is not overhead. It is the product.

- Every typed envelope → compliance artifact.
- Every HITL event → audit log entry.
- Every hash verification → governance attestation.

Hydra **generates** compliance as a byproduct of operation. It does not bolt it on.

## 19. EU AI Act Article 9 — Lived

| Article 9 obligation | Hydra mechanism |
|---|---|
| Identification of risks | squad-boundary redaction; venom-class detection |
| Estimation / evaluation | envelope schema validation (N7) |
| Risk mitigation | circuit breaker, loop ceiling, HITL gate |
| Testing | best-of-N + cross-vendor judge plane |
| Continuous monitoring | OTEL emit at every phase |
| Documentation | archivist seal (N6); replay-deterministic trace |

## 20. The HITL Gate (D9)

```mermaid
%%{init: {'theme':'dark','themeVariables':{'primaryColor':'#0D0F1A','primaryTextColor':'#E8D5A3','primaryBorderColor':'#2E86AB','lineColor':'#E8D5A3'}}}%%
flowchart LR
    IN([Envelope in]) --> SCHEMA{validate_envelope<br/>fail-closed N7}
    SCHEMA -- pass --> REDACT[Boundary redaction]
    SCHEMA -- fail --> ABORT[Governance event<br/>archivist seal]:::stop
    REDACT --> BUDGET{Budget gate<br/>80%% downgrade<br/>100%% HITL}
    BUDGET -- under --> LOOP{Loop ceiling<br/>25 iter / depth 5}
    BUDGET -- 80%% --> DOWNGRADE[Model tier downgrade]:::warn
    BUDGET -- 100%% --> HITL[/HITL Request/]:::hitl
    LOOP -- under --> CIRCUIT{Circuit breaker<br/>3 strikes}
    LOOP -- ceiling --> HITL
    CIRCUIT -- under --> EXEC[Dispatch to squad]
    CIRCUIT -- tripped --> HITL
    EXEC --> OTEL[OTEL emit<br/>archivist seal N6]
    OTEL --> OUT([DecisionRecord out])
    HITL -. /hydra:approve .-> EXEC
    HITL -. /hydra:resume .-> EXEC

    classDef stop fill:#C0392B,stroke:#C0392B,color:#0D0F1A
    classDef warn fill:#8B7355,stroke:#8B7355,color:#0D0F1A
    classDef hitl fill:#0D0F1A,stroke:#C0392B,color:#E8D5A3,stroke-width:2px
```

A paused workflow resumes only by human hand. No bypass. No timeout resumption.

## 21. The Squad Expansion Model

Five squads are stubs. *That is the design.* Legal-compliance, healthcare, sales-gtm, research-ds, customer-support — all registered under `squads/<slug>/squad.yaml`, all governed by the covenant, none yet operational.

Adding a squad means **one `squad.yaml` + one typed entrypoint**. The covenant scales without renegotiation.

## 22. Failure Modes & Mitigations

| Failure mode | Invariant / Control | Mitigation |
|---|---|---|
| MAS coordination collapse | typed envelopes | `validate_envelope` fail-closed (N7) |
| Runaway agent loop | loop ceiling | 25 iter / depth 5; HITL on breach |
| Constitution drift | hash pin | N8 aborts session on mismatch |
| Replication explosion | replication cap | N5 caps at 4 clones per scope |
| Venom-class capability generation | refusal registry | N2 fail-closed; cerberus-bridge for appeal |
| Push without verdict | TheEights gate | N4 requires evolution.commit before push |
| Schema malformity | envelope validator | N7 fail-closed |
| Tool inflation | factory ban | N9 — Smith cannot create new tools |
| Quarantine bypass | release gate | N10 requires TheEights HITL |
| Audit silence | decision log | N6 — every Smith decision logged |

## 23. The Pentecost Frame — Final Statement

Hydra is not Legion — it does not seek merger or dominance. Six squads, three crowns, one constitution. The distributed agency is not a bug in the governance model. It *is* the governance model.

## 24. For the Builder

```yaml
# squads/<your-slug>/squad.yaml
slug: your-squad
version: 1.0.0
entrypoint: mcp           # or: claude-skill | agent-impersonation
namespace: your-namespace
agents:
  - name: your-agent
    role: your-role
risk_class: low           # low | medium | venom
allow_list:
  - tool: external.api.read
constitution_attest: required
```

Read [`CONTRIBUTING-SQUADS.md`](../CONTRIBUTING-SQUADS.md). Register your slug. Define your envelope schema. Wire your entrypoint. Submit for constitution review. **The immortal head decides.**

You don't have to rebuild the governance layer — it's already there.

---

## Coda — The Largest Constellation

Hydra. 1,303 square degrees. Holding.

The largest constellation has no bright stars. It holds its shape not by luminosity but by the pattern of its relationships. So does this system. The covenant is the shape.

---

## Appendix A — Three Crowns Roster (D8)

```mermaid
mindmap
  root((Hydra<br/>Constellation))
    Executive Crown
      ceo / cso / coo / cfo / cro
      cto / cio / cdo / caio / ciso
      cpo / cmo / cxo / chro / clo
      csco / chief-sustainability-officer
      Orchestrators
        boardroom
        mna-cockpit
        crisis-warroom
        capital-allocation
    Forge Crown
      Daedalus · architect
      Prometheus · engineer
      Argus · reviewer
      Hygeia · test-strategist
      Cerberus · security-reviewer
      Charon · release-planner
      Mnemosyne · docs-author
    Garland Crown
      Calliope · brand
      Erato · copy
      Polyhymnia · content
      Terpsichore · social
      Euterpe · paid
      Clio · PR
      Urania · SEO
      Helios · photo-cinema
      Helios Sub-Crew
        video-synth
        audio-foley
        music-score
        dialogue-mix
        governance-c2pa
```

## Appendix B — Executive Memos

- [CSO — Strategic Case for Constellation Architecture](./exec-memos/cso.md)
- [CTO — Architectural Reference](./exec-memos/cto.md)
- [CAIO — AI Governance Posture](./exec-memos/caio.md)

## Appendix C — Garland Cinematic Treatment

- [Garland Creative Direction (brand voice, art direction, slide outline, narration, asset list, accessibility)](./garland/creative-direction.md)

## Appendix D — Research Notes (Phase 4)

- [Web research — LangGraph 2026, EU AI Act, NIST RMF, reveal.js](./assets/research-notes.md)

---

*Filed under the covenant. SHA-256 attested. Pentecost, not Legion.*
