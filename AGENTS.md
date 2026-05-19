# AGENTS.md — Hydra Cross-Tool Behavioral Contract

This file is the single source of truth for any AI agent (Claude Code, Codex, Gemini, Copilot, etc.) working inside the Hydra repo. Tool-specific shims (`CLAUDE.md`, etc.) import from this file.

## What Hydra Is

A central LangGraph supervisor + Claude Code plugin that routes work across heterogeneous AI agent squads (executive, engineering, creative, plus stubs). Hydra does NOT do the squads' work; it routes, governs, and synthesizes.

See `HYDRA.md` for the master spec and `ARCHITECTURE.md` for the layered design.

## Squads

| Slug | Source pack | Entrypoint |
|---|---|---|
| executive | `C:\AiAppDeployments\ExecutiveSuite` | agent-impersonation |
| engineering | `C:\AiAppDeployments\pair-programmer` | mcp |
| creative | `C:\AiAppDeployments\RLM-CLI-Starter` | claude-skill |
| legal-compliance | (stub) | stub |
| healthcare | (stub) | stub |
| sales-gtm | (stub) | stub |
| research-ds | (stub) | stub |
| customer-support | (stub) | stub |

## Hard Rules

1. **Never bypass HITL.** A paused workflow resumes only via `/hydra:approve` or `/hydra:resume`.
2. **Never cross a squad boundary with a raw blob.** Use `MemoryRef` handles.
3. **Never modify the squad registry inline.** Use `/hydra:add-squad` or edit `squads/<slug>/squad.yaml`.
4. **Always validate envelopes** with `hydra_core.schemas.validate_envelope` at squad boundaries.
5. **Always log** to the per-workflow trace via `hydra_core.telemetry.emit`.

## Engineering Standards

- Python 3.11+, Pydantic 2.x, LangGraph optional but recommended.
- Type-annotate everything in `hydra_core/`.
- Keep `hydra_core/` runtime-agnostic — dispatchers are injected, no provider SDK imports.
- Tests under `tests/` use `pytest`. Avoid network or LLM calls in unit tests.

## Security

- PII / PHI / financial data is redacted at squad boundaries (`governance.redact_for_squad_boundary`) unless the receiving squad's `squad.yaml` has explicit allow-list.
- The healthcare squad's `phi-redactor` agent runs FIRST on every inbound envelope.
- MCP tools are namespaced and whitelisted per-squad. The host enforces RBAC; agents do not self-check.

## Where To Read More

- `HYDRA.md` — top-level architecture
- `ARCHITECTURE.md` — engineer-facing detailed design
- `CONTRIBUTING-SQUADS.md` — how to add a squad pack
- `Enterprise Master AI Orchestration System Architecture.md` — upstream research doc grounding the design
