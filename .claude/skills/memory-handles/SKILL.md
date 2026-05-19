---
name: memory-handles
description: "Memory fabric usage — ephemeral / episodic / semantic. Read this before reading from or writing to Hydra memory."
---

# Memory Handles

Hydra never crosses a squad boundary with a raw blob. Agents receive `MemoryRef` handles (`tier`, `key`, `summary`) and resolve them through the `hydra-memory` MCP server (or `hydra_core.memory.resolve` directly).

## Three Tiers

| Tier | Storage | Lifetime | When to write |
|---|---|---|---|
| `ephemeral` | in-prompt window | one turn | step-local scratch |
| `episodic` | SQLite append-only at `~/.hydra/episodic.db` | workflow lifetime + audit | every envelope, tool call, verdict, approval |
| `semantic` | vector store (Chroma default) at `~/.hydra/vectors/` | persistent | long-term knowledge: code RAG, brand bibles, regulatory corpus |

## Writing

```python
from hydra_core.memory import append_episodic
ref = append_episodic(workflow_id, kind="prd", payload=prd.model_dump(mode="json"))
# returns MemoryRef(tier="episodic", key="ep:<wf>:prd:<ts>", summary="prd@<wf>")
```

## Reading

```python
from hydra_core.memory import resolve, list_episodic
content = resolve(ref)
all_for_wf = list_episodic(workflow_id)
```

## Access Control

Each squad's `squad.yaml` declares `granted_memory_scopes` implicitly via its `accepts`. Cross-squad reads MUST go through a `HANDOFF` envelope which carries `granted_memory_scopes`. The redaction layer (`governance.redact_for_squad_boundary`) strips PII at the boundary unless the squad is in the explicit allow-list (e.g. healthcare with PHI under its phi-redactor agent).

## Vector Index Names

Conventional names by squad:
- engineering: `code_repos`, `runbooks`, `incident_history`
- executive: `strategy_docs`, `financials`, `market_intel`
- creative: `brand_guidelines`, `shot_library`, `prior_campaigns`
- legal-compliance: `regulatory_corpus`, `contract_clauses`
- healthcare: `snomed_ct`, `clinical_guidelines`
- research-ds: `paper_corpus`, `experiment_logs`
