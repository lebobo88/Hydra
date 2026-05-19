---
name: cross-squad-message
description: "How to construct, validate, and route Hydra's typed cross-squad message envelopes. Use whenever an agent emits or consumes a CSuiteDecisionPacket, PRD, ArchRFC, DevTask, CreativeBrief, ShotList, AssetJob, HITLRequest, DecisionRecord, or Handoff."
---

# Cross-Squad Message Protocol

Every artifact that crosses a squad boundary is a Pydantic envelope defined in `hydra_core/schemas.py`. They share a base (`HydraEnvelope`) with: `id`, `type`, `origin_squad`, `target_squad`, `workflow_id`, `parent_id`, `context_refs`, `constraints`, `created_at`.

## The Ten Envelope Types

| Type | Producer → Consumer | Carries |
|---|---|---|
| `C_SUITE_DECISION_PACKET` | executive → any | objective, proposed_tasks, approvals_required |
| `PRD` | engineering planner | user stories, acceptance criteria, NFRs |
| `ARCH_RFC` | architect | proposed_changes, risk_assessment, rollout_plan |
| `DEV_TASK` | engineering | repo/branch/instructions/test_plan |
| `CREATIVE_BRIEF` | exec/marketing → creative | objective, audience, channels, assets_required |
| `SHOT_LIST` | creative cinematographer | shots[] (angle, focal, duration, lighting) |
| `ASSET_JOB` | creative asset agent | model_type, resolution, fps, max_render_cost_usd |
| `HITL_REQUEST` | governance → operator | reason, summary, options, default_option |
| `DECISION_RECORD` | synthesizer | decision, rationale, dissenting_opinions, artifacts |
| `HANDOFF` | supervisor → squad | granted_tools, granted_memory_scopes, payload_envelope_id |

## Rules

1. NEVER serialize a raw blob across a boundary. Use `MemoryRef` handles (`tier`, `key`, `summary`) and let the receiver resolve via the memory MCP server.
2. ALWAYS validate inbound envelopes with `hydra_core.schemas.validate_envelope`. The `schema-validate` hook runs this automatically on tool-output write-back, but agent code should not rely on the hook alone.
3. Preserve `parent_id` chains. They are the only way `/hydra:replay` can reconstruct causality.
4. Redact PII at boundaries via `hydra_core.governance.redact_for_squad_boundary` unless `allow_pii=True` is set on the squad (e.g. healthcare squad keeps PHI behind its phi-redactor agent).

## Construction Example (Python)

```python
from hydra_core.schemas import PRD, UserStory, Constraints, MemoryRef
from uuid import uuid4

prd = PRD(
    workflow_id=workflow_id,
    origin_squad="hydra",
    target_squad="engineering",
    source_goal_id=goal_id,
    summary="Add idempotency-key support to /payments POST",
    user_personas=["merchant-integration-engineer"],
    user_stories=[UserStory(id="s1", as_a="integrator", i_want="idempotency-key headers honored", so_that="retries don't double-charge")],
    acceptance_criteria=["GET /payments with same key returns prior result"],
    non_functional_requirements=["p99 < 200ms"],
    constraints=Constraints(budget_usd=80, deadline_ts=None, priority="P1"),
    context_refs=[MemoryRef(tier="semantic", key="code_repos:payments")],
)
```
