"""Toolshed — search-describe-execute meta-tool facade.

Wraps MCP servers (pp_harness, eights, agentsmith, hydra_memory,
executive_suite, rlm_creative, pp_codex, pp_gemini) behind search/
describe/execute meta-tools, reducing the context footprint from ~N tool
schemas to a handful of constant-size meta-tools.

Inspired by Speakeasy's Dynamic Toolsets pattern (avg 96% input reduction).

Usage from squad_node or supervisor::

    shed = ToolShed(dispatcher)
    matches = shed.search("rubric", server="pp_harness", limit=5)
    schema  = shed.describe("pp_harness", "get_rubric")
    result  = shed.execute("pp_harness", "get_rubric", {"rubric_id": "owasp"})
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any, Optional


@dataclass
class ToolEntry:
    """Catalog entry for one tool on one server."""
    server: str
    name: str
    description: str = ""
    category: str = "general"
    input_schema: dict[str, Any] = field(default_factory=dict)
    tags: list[str] = field(default_factory=list)


@dataclass
class SearchResult:
    server: str
    name: str
    description: str
    category: str
    relevance_score: float


class ToolShed:
    """In-memory tool catalog with search, describe, execute."""

    def __init__(self, dispatcher: Any = None) -> None:
        self._dispatcher = dispatcher
        self._catalog: dict[str, list[ToolEntry]] = {}
        self._by_key: dict[str, ToolEntry] = {}

    @property
    def total_tools(self) -> int:
        return len(self._by_key)

    @property
    def servers(self) -> list[str]:
        return sorted(self._catalog.keys())

    def register_server(self, server: str, tools: list[dict[str, Any]]) -> int:
        """Register all tools from a server into the catalog."""
        entries = []
        for t in tools:
            name = t.get("name", "")
            desc = t.get("description", name)
            category = _infer_category(server, name, desc)
            tags = _extract_tags(name, desc)
            entry = ToolEntry(
                server=server,
                name=name,
                description=desc,
                category=category,
                input_schema=t.get("inputSchema", t.get("input_schema", {})),
                tags=tags,
            )
            entries.append(entry)
            self._by_key[f"{server}.{name}"] = entry
        self._catalog[server] = entries
        return len(entries)

    def register_from_dispatcher(self, server: str) -> int:
        """Discover tools from a live MCP server via the dispatcher."""
        if self._dispatcher is None:
            return 0
        try:
            result = self._dispatcher.call_mcp(server, "list_tools", {})
            if not isinstance(result, dict) or result.get("status") == "failed":
                return 0
            tools_data = result.get("result", result)
            if isinstance(tools_data, list):
                return self.register_server(server, tools_data)
            if isinstance(tools_data, dict) and "tools" in tools_data:
                return self.register_server(server, tools_data["tools"])
        except Exception:
            pass
        return 0

    def register_static_catalog(self, server: str,
                                tool_names: list[str],
                                descriptions: dict[str, str] | None = None,
                                schemas: dict[str, dict[str, Any]] | None = None) -> int:
        """Register tools from a static name list (no live discovery needed).

        ``schemas`` optionally maps a tool name to its real JSON input schema.
        Hand-seeded schemas (see ``SCHEMA_OVERRIDES``) give the gateway typed
        params for high-value tools before the offline schema cache is warmed,
        so nested objects and numeric args survive the proxy hop."""
        descs = descriptions or {}
        schemas = schemas or {}
        tools = [
            {"name": n, "description": descs.get(n, n),
             "inputSchema": schemas.get(n, {})}
            for n in tool_names
        ]
        return self.register_server(server, tools)

    def search(self, query: str, *,
               server: str | None = None,
               category: str | None = None,
               limit: int = 10) -> list[SearchResult]:
        """Search the catalog by keyword matching against name, description, tags."""
        query_lower = query.lower()
        query_terms = set(re.split(r"[_\-\s.]+", query_lower))
        query_terms.discard("")

        candidates: list[tuple[float, ToolEntry]] = []
        for key, entry in self._by_key.items():
            if server and entry.server != server:
                continue
            if category and entry.category != category:
                continue
            score = _relevance_score(entry, query_lower, query_terms)
            if score > 0:
                candidates.append((score, entry))

        candidates.sort(key=lambda x: -x[0])
        return [
            SearchResult(
                server=e.server,
                name=e.name,
                description=e.description[:200],
                category=e.category,
                relevance_score=round(s, 3),
            )
            for s, e in candidates[:limit]
        ]

    def describe(self, server: str, tool_name: str) -> dict[str, Any] | None:
        """Return full schema for a specific tool."""
        key = f"{server}.{tool_name}"
        entry = self._by_key.get(key)
        if entry is None:
            for k, e in self._by_key.items():
                if e.name == tool_name and (not server or e.server == server):
                    entry = e
                    break
        if entry is None:
            return None
        return {
            "server": entry.server,
            "name": entry.name,
            "description": entry.description,
            "category": entry.category,
            "input_schema": entry.input_schema,
            "tags": entry.tags,
        }

    def execute(self, server: str, tool_name: str,
                args: dict[str, Any],
                *, squad_id: str | None = None) -> dict[str, Any]:
        """Execute a tool via the dispatcher (proxied call)."""
        if self._dispatcher is None:
            return {"status": "failed", "error": "no dispatcher configured"}
        return self._dispatcher.call_mcp(server, tool_name, args,
                                         squad_id=squad_id)

    def list_categories(self, server: str | None = None) -> dict[str, int]:
        """List categories with tool counts."""
        counts: dict[str, int] = {}
        for entry in self._by_key.values():
            if server and entry.server != server:
                continue
            cat = f"{entry.server}/{entry.category}"
            counts[cat] = counts.get(cat, 0) + 1
        return dict(sorted(counts.items()))

    def list_servers(self) -> list[dict[str, Any]]:
        """List registered servers with tool counts."""
        return [
            {"server": s, "tool_count": len(tools)}
            for s, tools in sorted(self._catalog.items())
        ]

    def stats(self) -> dict[str, Any]:
        """Return catalog statistics."""
        return {
            "total_tools": self.total_tools,
            "servers": len(self._catalog),
            "server_counts": {s: len(t) for s, t in self._catalog.items()},
            "categories": self.list_categories(),
        }


# ---------- scoring ----------

def _relevance_score(entry: ToolEntry, query_lower: str,
                     query_terms: set[str]) -> float:
    """Score a tool entry against a search query. Higher = more relevant."""
    score = 0.0
    name_lower = entry.name.lower()
    desc_lower = entry.description.lower()
    name_parts = set(re.split(r"[_\-\s.]+", name_lower))

    if query_lower == name_lower:
        score += 10.0
    elif query_lower in name_lower:
        score += 5.0
    if query_lower in desc_lower:
        score += 2.0

    term_hits = query_terms & name_parts
    if term_hits:
        score += len(term_hits) * 1.5

    tag_hits = query_terms & set(t.lower() for t in entry.tags)
    if tag_hits:
        score += len(tag_hits) * 1.0

    for term in query_terms:
        if term in desc_lower:
            score += 0.5

    return score


def _infer_category(server: str, name: str, description: str) -> str:
    """Infer a category from the tool name/description."""
    name_lower = name.lower()
    if any(k in name_lower for k in ("list", "get", "status", "query", "search", "read")):
        return "read"
    if any(k in name_lower for k in ("start", "run", "execute", "generate", "create")):
        return "execute"
    if any(k in name_lower for k in ("write", "archive", "record", "update", "patch", "apply")):
        return "write"
    if any(k in name_lower for k in ("critique", "judge", "review", "evaluate", "validate")):
        return "judge"
    if any(k in name_lower for k in ("config", "profile", "setting", "register")):
        return "config"
    if any(k in name_lower for k in ("govern", "policy", "constitution", "audit", "budget")):
        return "governance"
    if any(k in name_lower for k in ("evolve", "propose", "approve", "reject", "commit")):
        return "evolution"
    if any(k in name_lower for k in ("memory", "episodic", "semantic", "cell")):
        return "memory"
    return "general"


def _extract_tags(name: str, description: str) -> list[str]:
    """Extract searchable tags from a tool name and description."""
    parts = re.split(r"[_\-\s.]+", name.lower())
    desc_words = set(re.split(r"[_\-\s.,;:()]+", description.lower()))
    important = {"rubric", "artifact", "run", "stage", "team", "profile",
                 "taxonomy", "budget", "verdict", "critique", "envelope",
                 "evolution", "memory", "cell", "squad", "agent", "policy",
                 "constitution", "audit", "governance", "scaffold"}
    tags = [p for p in parts if len(p) > 2]
    tags.extend(w for w in desc_words & important)
    return list(set(tags))


# ---------- static catalogs for known servers ----------

PP_HARNESS_TOOLS = [
    "analyze_autogenesis", "agents_md_status", "apply_agents_md_patch",
    "apply_master_plan_patch", "archive_artifact", "archive_winner_and_losers",
    "artifact_validate", "audit_status", "borda_count", "browser_validation_finalize",
    "browser_validation_start", "budget_status", "completion_checklist",
    "constitution_status", "detect_profile", "diff_entropy", "doctor",
    "ensure_agents_md", "ensure_constitution", "ensure_master_plan", "ensure_run",
    "finalize_run", "finalize_stage", "force_unlock", "gate_eligible_judges",
    "get_artifact_validation", "get_builtin_profile", "get_claude_tier_models",
    "get_copilot_claude_tier_models", "get_design_template", "get_forum",
    "get_profile", "get_rubric", "get_run", "get_stage_finalize_readiness",
    "get_tdd_check", "hydra_envelope_query", "janitor", "list_design_templates",
    "list_evolution_proposals", "list_forums", "list_missability_checks",
    "list_prior_critiques", "list_profiles", "list_rubrics", "list_runs",
    "list_taxonomy_sections", "loop_ceiling_status", "map_taxonomy",
    "master_plan_status", "record_attempt", "record_smoke_status",
    "record_taxonomy_mapping", "record_verdict", "replay", "report_hydra_completion",
    "request_brand_review", "request_strategic_framing", "request_visual_advisory",
    "retract_verdict", "retry_with_critique", "review_evolution_proposal",
    "run_missability_checks", "start_best_of_stage", "start_run", "start_stage",
    "tdd_post_check", "tdd_pre_check", "team_get", "team_list",
    "teardown_candidates", "triage_request", "visual_regression_capture",
    "visual_regression_diff", "write_profile",
]

EIGHTS_TOOLS = [
    "eights.adapters.exec.register_now", "eights.adapters.exec.start",
    "eights.adapters.exec.stop", "eights.adapters.exec.sync_now",
    "eights.adapters.hydra.register_now",
    "eights.adapters.pp.register_now", "eights.adapters.pp.start",
    "eights.adapters.pp.stop", "eights.adapters.pp.sync_now",
    "eights.adapters.rlm.register_now", "eights.adapters.rlm.start",
    "eights.adapters.rlm.stop", "eights.adapters.rlm.sync_now",
    "eights.audit.bom", "eights.audit.trace", "eights.audit.verify",
    "eights.cells.classify", "eights.cells.distribution", "eights.cells.query",
    "eights.constitution.attest", "eights.constitution.get",
    "eights.constitution.propose_amendment",
    "eights.evolution.approve", "eights.evolution.commit",
    "eights.evolution.detect_drift", "eights.evolution.evaluate",
    "eights.evolution.get_resource", "eights.evolution.list_pending",
    "eights.evolution.list_resources", "eights.evolution.propose",
    "eights.evolution.register", "eights.evolution.reject",
    "eights.evolution.rollback", "eights.evolution.unfreeze",
    "eights.governance.access.check", "eights.governance.breaker.outcome",
    "eights.governance.breaker.reset", "eights.governance.breaker.status",
    "eights.governance.budget.charge", "eights.governance.cap.set",
    "eights.governance.ceiling.tick", "eights.governance.consistency_check",
    "eights.governance.hitl.list", "eights.governance.hitl.request",
    "eights.governance.hitl.resolve",
    "eights.governance.policy.evaluate", "eights.governance.redact",
    "eights.governance.redact_for_squad",
    "eights.hydra.envelope.query", "eights.hydra.envelope.record",
    "eights.hydra.handoff.list",
    "eights.identity.register_actor", "eights.identity.register_project",
    "eights.memory.add", "eights.memory.get", "eights.memory.link",
    "eights.memory.resolve", "eights.memory.resolve_batch", "eights.memory.search",
    "eights.miner.run_now",
    "eights.prompt.diff", "eights.prompt.get", "eights.prompt.list",
    "eights.squad.get", "eights.squad.list",
]

AGENTSMITH_TOOLS = [
    "agentsmith.archivist.audit", "agentsmith.archivist.decisions",
    "agentsmith.archivist.seal", "agentsmith.constitution.attest",
    "agentsmith.constitution.get", "agentsmith.constitution.propose_amendment",
    "agentsmith.eights.evolution_propose", "agentsmith.eights.hitl_request",
    "agentsmith.eights.lookup_envelope_attempt", "agentsmith.eights.memory_add",
    "agentsmith.factory.promote", "agentsmith.factory.scaffold",
    "agentsmith.hydra.squad_list", "agentsmith.hydra.venom_cross_check",
    "agentsmith.inspector.inspect", "agentsmith.inspector.invariants_list",
    "agentsmith.keymaker.gap_report", "agentsmith.keymaker.scan",
    "agentsmith.oracle.evaluate", "agentsmith.pp.best_of_start",
    "agentsmith.pp.borda_count", "agentsmith.quarantine.isolate",
    "agentsmith.quarantine.release", "agentsmith.replicator.list",
    "agentsmith.replicator.spawn", "agentsmith.replicator.teardown",
    "agentsmith.sentinel.classify", "agentsmith.sentinel.events_recent",
    "agentsmith.sentinel.signatures_list",
]


HYDRA_MEMORY_TOOLS = [
    "hydra-mem.write_episodic", "hydra-mem.read_episodic",
    "hydra-mem.list_workflow", "hydra-mem.semantic_search",
    "hydra-mem.query_eights", "hydra-mem.tag_memory",
]

EXECUTIVE_SUITE_TOOLS = [
    "es.roster.list", "es.agent.get", "es.skill.list", "es.skill.get",
    "es.command.list", "es.command.get", "es.output.write", "es.output.read",
    "es.ping",
]

RLM_CREATIVE_TOOLS = [
    "rlm.skill.list", "rlm.skill.get", "rlm.command.list", "rlm.command.get",
    "rlm.agent.list", "rlm.agent.get", "rlm.output.write", "rlm.output.read",
    "rlm.ping",
]

PP_CODEX_TOOLS = ["generate", "critique"]
PP_GEMINI_TOOLS = ["generate", "critique"]


class ProgressiveDisclosureTree:
    """Hierarchical tool navigation: squad → server → category → tool.

    Agents traverse the tree level by level, loading schemas only for
    the tools they actually need. At each level, only category names
    and counts are shown — full schemas are deferred until describe().
    """

    def __init__(self, shed: ToolShed, packs: dict[str, Any] | None = None) -> None:
        self._shed = shed
        self._packs = packs or {}

    def list_squads(self) -> list[dict[str, Any]]:
        """Level 0: list available squads."""
        return [
            {
                "slug": slug,
                "name": getattr(pack, "name", slug),
                "entrypoint": getattr(pack, "entrypoint", "unknown"),
                "declared_tools": len(getattr(pack, "tools", ())),
            }
            for slug, pack in sorted(self._packs.items())
        ]

    def list_servers_for_squad(self, squad_slug: str) -> list[dict[str, Any]]:
        """Level 1: list MCP servers available to a squad."""
        pack = self._packs.get(squad_slug)
        if pack is None:
            return []
        servers: dict[str, int] = {}
        for t in getattr(pack, "tools", ()):
            server = getattr(t, "mcp_server", None) or "local"
            servers[server] = servers.get(server, 0) + 1
        return [
            {"server": s, "tool_count": c}
            for s, c in sorted(servers.items())
        ]

    def list_categories_for_server(self, server: str) -> list[dict[str, Any]]:
        """Level 2: list tool categories on a server."""
        entries = self._shed._catalog.get(server, [])
        cats: dict[str, int] = {}
        for e in entries:
            cats[e.category] = cats.get(e.category, 0) + 1
        return [
            {"category": c, "tool_count": n}
            for c, n in sorted(cats.items())
        ]

    def list_tools_in_category(self, server: str,
                                category: str) -> list[dict[str, Any]]:
        """Level 3: list tools in a category (names + one-line descriptions)."""
        entries = self._shed._catalog.get(server, [])
        return [
            {
                "name": e.name,
                "description": e.description[:120],
                "privilege": "execute",
            }
            for e in entries
            if e.category == category
        ]

    def describe_tool(self, server: str, tool_name: str) -> dict[str, Any] | None:
        """Level 4: full schema for one tool (deferred until explicitly requested)."""
        return self._shed.describe(server, tool_name)

    def navigate(self, path: str) -> dict[str, Any]:
        """Navigate the tree with a slash-delimited path.

        Examples:
          ""                           → list squads
          "engineering"                → list servers for engineering
          "pp_harness"                 → list categories for pp_harness
          "pp_harness/read"            → list tools in pp_harness/read
          "pp_harness/read/get_rubric" → describe pp_harness.get_rubric
        """
        parts = [p for p in path.strip("/").split("/") if p]

        if len(parts) == 0:
            return {"level": "squads", "items": self.list_squads()}

        if len(parts) == 1:
            token = parts[0]
            if token in self._packs:
                return {
                    "level": "servers",
                    "squad": token,
                    "items": self.list_servers_for_squad(token),
                }
            if token in self._shed._catalog:
                return {
                    "level": "categories",
                    "server": token,
                    "items": self.list_categories_for_server(token),
                }
            return {"error": f"unknown squad or server: {token}"}

        if len(parts) == 2:
            server, category = parts
            return {
                "level": "tools",
                "server": server,
                "category": category,
                "items": self.list_tools_in_category(server, category),
            }

        if len(parts) == 3:
            server, _category, tool_name = parts
            entry = self.describe_tool(server, tool_name)
            if entry is None:
                return {"error": f"tool not found: {server}.{tool_name}"}
            return {"level": "tool_detail", **entry}

        return {"error": f"path too deep: {path}"}

    def token_estimate(self) -> dict[str, int]:
        """Estimate token costs at each disclosure level."""
        l0 = len(self.list_squads()) * 4
        servers = self._shed.list_servers()
        l1 = len(servers) * 3
        cats = self._shed.list_categories()
        l2 = len(cats) * 3
        l3_per_cat = 5
        l4_per_tool = 25
        return {
            "level_0_squads": l0,
            "level_1_servers": l1,
            "level_2_categories": l2,
            "level_3_tools_per_category": l3_per_cat,
            "level_4_schema_per_tool": l4_per_tool,
            "full_eager_estimate": self._shed.total_tools * l4_per_tool,
            "progressive_per_query": l0 + l1 + l2 + l3_per_cat + l4_per_tool,
        }


# ---------- hand-seeded schemas (immediate relief before cache warm-up) ----------
#
# The static catalogs above carry only names + descriptions, so the gateway
# advertises `{"type":"object"}` for every tool — which strips type info and
# makes Claude emit nested objects / numeric args as strings (e.g.
# `start_run(n=3)` arrives as `"3"` and the daemon rejects it). These overrides
# declare real schemas for the highest-value tools so calls work even before
# `refresh_schemas` warms ~/.hydra/gateway_schemas.json. Keyed by the catalog
# tool name. The live refresh cache takes precedence over these at runtime.
SCHEMA_OVERRIDES: dict[str, dict[str, dict[str, Any]]] = {
    "pp_harness": {
        "start_run": {
            "type": "object",
            "properties": {
                "request_text": {"type": "string", "minLength": 1},
                "project_path": {"type": "string", "minLength": 1},
                "mode": {"type": "string",
                         "enum": ["single", "best_of", "team", "review"]},
                "team": {"type": "string"},
                "forum": {"type": "string"},
                "n": {"type": "integer", "minimum": 1, "maximum": 8,
                      "description": "Candidate count for best_of mode."},
                "session_id": {"type": "string"},
                "hydra_workflow_id": {"type": "string"},
                "hydra_envelope_id": {"type": "string"},
                "hydra_origin_squad": {"type": "string"},
                "hydra_envelope_type": {"type": "string"},
            },
            "required": ["request_text", "project_path", "mode"],
            "additionalProperties": False,
        },
    },
    "eights": {
        "eights.memory.add": {
            "type": "object",
            "properties": {
                "envelope": {"type": "object",
                             "description": "TheEights envelope (origin, objective, ids)."},
                "content": {"type": "string"},
                "type": {"type": "string",
                         "enum": ["working", "episodic", "semantic",
                                  "procedural", "meta"]},
                "summary": {"type": "string"},
                "scopes": {"type": "array", "items": {"type": "string"}},
                "provenance": {
                    "type": "object",
                    "properties": {
                        "run_id": {"type": "string"},
                        "actor": {"type": "string"},
                        "model": {"type": "string"},
                        "source_uri": {"type": "string"},
                    },
                    "required": ["actor"],
                },
                "embedding": {"type": "array", "items": {"type": "number"}},
                "confidence": {"type": "number", "minimum": 0, "maximum": 1},
                "supersedes": {"type": "array", "items": {"type": "string"}},
                "cell": {"type": "string"},
            },
            "required": ["envelope", "content", "type", "provenance"],
        },
        "eights.memory.search": {
            "type": "object",
            "properties": {
                "envelope": {"type": "object"},
                "query": {"type": "string"},
                "query_embedding": {"type": "array", "items": {"type": "number"}},
                "types": {"type": "array", "items": {"type": "string"}},
                "scopes": {"type": "array", "items": {"type": "string"}},
                "top_k": {"type": "integer", "minimum": 1, "maximum": 100},
                "fusion": {"type": "string",
                           "enum": ["hybrid", "vector", "graph", "episodic"]},
            },
            "required": ["envelope", "query"],
        },
    },
    "hydra_memory": {
        "hydra-mem.write_episodic": {
            "type": "object",
            "properties": {
                "workflow_id": {"type": "string"},
                "kind": {"type": "string"},
                "payload": {"type": "object"},
                "key": {"type": "string"},
                "cells": {"type": "array", "items": {"type": "string"}},
                "origin_squad": {"type": "string"},
            },
            "required": ["workflow_id"],
        },
        "hydra-mem.semantic_search": {
            "type": "object",
            "properties": {
                "query": {"type": "string",
                          "description": "Full-text query over episodic memory."},
                "k": {"type": "integer", "minimum": 1, "maximum": 50},
                "workflow_id": {"type": "string"},
                "cell": {"type": "string"},
                "index": {"type": "string"},
                "embedding": {"type": "array", "items": {"type": "number"}},
            },
        },
    },
}


def build_default_shed(dispatcher: Any = None) -> ToolShed:
    """Build a ToolShed pre-loaded with static catalogs for all 8 backends."""
    shed = ToolShed(dispatcher=dispatcher)
    shed.register_static_catalog("pp_harness", PP_HARNESS_TOOLS,
                                 schemas=SCHEMA_OVERRIDES.get("pp_harness"))
    shed.register_static_catalog("eights", EIGHTS_TOOLS,
                                 schemas=SCHEMA_OVERRIDES.get("eights"))
    shed.register_static_catalog("agentsmith", AGENTSMITH_TOOLS)
    shed.register_static_catalog("hydra_memory", HYDRA_MEMORY_TOOLS,
                                 schemas=SCHEMA_OVERRIDES.get("hydra_memory"))
    shed.register_static_catalog("executive_suite", EXECUTIVE_SUITE_TOOLS)
    shed.register_static_catalog("rlm_creative", RLM_CREATIVE_TOOLS)
    shed.register_static_catalog("pp_codex", PP_CODEX_TOOLS)
    shed.register_static_catalog("pp_gemini", PP_GEMINI_TOOLS)
    return shed
