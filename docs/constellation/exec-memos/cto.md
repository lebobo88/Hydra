# Architectural Reference — Hydra Constellation — 2026-05-19

**To:** Hydra Constellation Presentation Audience (Engineering, Executive, Mythopoetic Readers)
**From:** Chief Technology Officer
**Date:** 2026-05-19
**Subject:** Architectural Reference — Hydra Constellation (Act II Opening — The Three Crowns)

## Executive Summary
Hydra is a **typed-envelope supervisor over three specialist crowns**, not a flat agent swarm. Confidence: **High**. The architecture is a deliberate inversion of the "more agents" reflex: one LangGraph state machine routes contracts, three MCP-isolated crowns execute them, and a memory fabric makes the system learn across runs. This is what makes it implementable today and fundable for the next three years.

## Situation Assessment
The default failure mode of multi-agent systems is **emergent chaos**: N agents gossiping on a shared bus, with non-deterministic handoffs, unbounded loops, and no audit chain. Hydra rejects that topology. We chose a **contract-net supervisor** (LangGraph, 8 nodes: intake → planner → approval → dispatch → judge_per_squad → synthesis → judge_synthesis → postcheck) that brokers typed work packets to specialist crowns. Squad boundaries are contracts, not conversations.

## Detail

**1. Supervisor + typed envelopes, not a mesh.** Ten Pydantic-validated schemas (`CSuiteDecisionPacket`, `PRD`, `ArchRFC`, `DevTask`, `CreativeBrief`, `ShotList`, `AssetJob`, `DecisionRecord`, `HITLRequest`, `Handoff`) are the only legal currency at squad boundaries. `hydra_core.schemas.validate_envelope` is fail-closed. This is the **blackboard pattern with a contract layer** — every cross-squad message is a signed, typed work order, replayable from the episodic log. Flat meshes cannot give you this.

**2. Three crowns, three optimization targets, three MCP namespaces.** Executive (ExecutiveSuite, agent-impersonation, 20 execs + 4 orchestrators) is optimized for **judgment under ambiguity**. Forge (pair-programmer, MCP, best-of-N harness with 7 forge heads and 16 profiles) is optimized for **verifiable code production**. Garland (RLM-Creative, claude-skill, 8 Muses + Helios sub-crew) is optimized for **multi-modal creative synthesis**. Each lives behind its own MCP namespace (`executive-suite`, `pp-daemon`, `rlm-creative`), so a failure, prompt injection, or runaway loop in one crown cannot reach into another. **Blast radius equals namespace.**

**3. Memory continuity is the real innovation.** Agent count is a vanity metric. The architectural moat is TheEights' memory fabric: episodic SQLite at `~/.hydra/episodic.db` plus a semantic vector store, accessed only through `MemoryRef` handles. Squads never exchange raw blobs — they exchange references to durable, auditable memory. This is what turns ad-hoc runs into a learning institution.

**4. AgentSmith makes it safe to scale.** Four pillars (Factory, Inspector, Sentinel, Archivist) enforce invariants N1–N10 fail-closed, with loop ceilings (25 iterations / depth 5), circuit breakers (3 consecutive failures), HITL gates, budget tripwires, and boundary redaction. The `CONSTITUTION.md` immortal head is SHA-256 pinned per session and never edited inline. Safety is structural, not aspirational.

## Recommendation
Adopt the Constellation reference architecture as the **paved road** for all multi-agent workloads. Build new capability *as a squad pack*, not as a parallel system. Reversibility is preserved: the supervisor, envelopes, and MCP namespacing keep optionality cheap.

## Next Steps
| # | Action | Owner | Deadline | Success Criterion |
|---|---|---|---|---|
| 1 | Publish ADR-001 "Supervisor + Typed Envelopes" | cto | 2026-05-26 | Merged to `docs/adr/` |
| 2 | Lock the 10 envelope schemas at v1.0 | cto + engineering | 2026-06-09 | Semver pin; consumers migrated |
| 3 | Stand up OTEL dashboards per crown | engineering | 2026-06-16 | DORA + per-namespace MTTR visible |
| 4 | Promote one stub squad (legal-compliance) to active | cpo + cto | 2026-Q3 | Envelope conformance + HITL traces |

## Risk Factors & Mitigations
| Risk | Probability | Impact | Mitigation | Trigger |
|---|---|---|---|---|
| Envelope schema sprawl | Med | High | Schema review board; semver discipline | >12 schemas |
| MCP namespace coupling regressions | Med | High | Contract tests at namespace boundary | Cross-namespace import detected |
| Memory fabric becomes hot path | Med | Med | Read-replicate semantic store; cache `MemoryRef` | p95 read >150ms |
| Constitution drift across sessions | Low | Critical | SHA-256 attestation, fail-closed | Hash mismatch event |

## Assumptions That Could Be Wrong
- LangGraph remains the right supervisor runtime through 2028 (alternative: home-grown state machine if upstream stalls).
- MCP becomes the durable cross-vendor agent protocol (kill criterion: a credible competing standard achieves hyperscaler adoption).
- Episodic+semantic memory is sufficient; we will not need a third store (procedural) at our current scale.
- Three crowns is the right cardinality for the next 24 months — additional crowns are squads, not crowns.

## HITL / Approvals Required
- CEO + Board: endorsement of Constellation as the firm-wide reference architecture.
- CISO: sign-off on namespace isolation as a security boundary equivalent to a service mesh.
- CFO: TCO envelope for the memory fabric scaling line in FY27 plan.

---
Filed by: cto | Date: 2026-05-19
