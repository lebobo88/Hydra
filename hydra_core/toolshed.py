"""Toolshed — search-describe-execute meta-tool facade.

Wraps large MCP servers (pp_harness, eights, agentsmith) behind three
meta-tools per server, reducing the context footprint from ~N tool schemas
to 3 constant-size meta-tools regardless of catalog size.

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
                                descriptions: dict[str, str] | None = None) -> int:
        """Register tools from a static name list (no live discovery needed)."""
        descs = descriptions or {}
        tools = [
            {"name": n, "description": descs.get(n, n)}
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
    "adapters_exec_register_now", "adapters_exec_start", "adapters_exec_stop",
    "adapters_exec_sync_now", "adapters_hydra_register_now",
    "adapters_pp_register_now", "adapters_pp_start", "adapters_pp_stop",
    "adapters_pp_sync_now", "adapters_rlm_register_now", "adapters_rlm_start",
    "adapters_rlm_stop", "adapters_rlm_sync_now", "audit_bom", "audit_trace",
    "audit_verify", "cells_classify", "cells_distribution", "cells_query",
    "constitution_attest", "constitution_get", "constitution_propose_amendment",
    "evolution_approve", "evolution_commit", "evolution_detect_drift",
    "evolution_evaluate", "evolution_get_resource", "evolution_list_pending",
    "evolution_list_resources", "evolution_propose", "evolution_register",
    "evolution_reject", "evolution_rollback", "evolution_unfreeze",
    "governance_access_check", "governance_breaker_outcome",
    "governance_breaker_reset", "governance_breaker_status",
    "governance_budget_charge", "governance_cap_set", "governance_ceiling_tick",
    "governance_consistency_check", "governance_hitl_list",
    "governance_hitl_request", "governance_hitl_resolve",
    "governance_policy_evaluate", "governance_redact", "governance_redact_for_squad",
    "hydra_envelope_query", "hydra_envelope_record", "hydra_handoff_list",
    "identity_register_actor", "identity_register_project", "memory_add",
    "memory_get", "memory_link", "memory_resolve", "memory_resolve_batch",
    "memory_search", "miner_run_now", "prompt_diff", "prompt_get", "prompt_list",
    "squad_get", "squad_list",
]

AGENTSMITH_TOOLS = [
    "agentsmith_archivist_audit", "agentsmith_archivist_decisions",
    "agentsmith_archivist_seal", "agentsmith_constitution_attest",
    "agentsmith_constitution_get", "agentsmith_constitution_propose_amendment",
    "agentsmith_eights_evolution_propose", "agentsmith_eights_hitl_request",
    "agentsmith_eights_lookup_envelope_attempt", "agentsmith_eights_memory_add",
    "agentsmith_factory_promote", "agentsmith_factory_scaffold",
    "agentsmith_hydra_squad_list", "agentsmith_hydra_venom_cross_check",
    "agentsmith_inspector_inspect", "agentsmith_inspector_invariants_list",
    "agentsmith_keymaker_gap_report", "agentsmith_keymaker_scan",
    "agentsmith_oracle_evaluate", "agentsmith_pp_best_of_start",
    "agentsmith_pp_borda_count", "agentsmith_quarantine_isolate",
    "agentsmith_quarantine_release", "agentsmith_replicator_list",
    "agentsmith_replicator_spawn", "agentsmith_replicator_teardown",
    "agentsmith_sentinel_classify", "agentsmith_sentinel_events_recent",
    "agentsmith_sentinel_signatures_list",
]


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


def build_default_shed(dispatcher: Any = None) -> ToolShed:
    """Build a ToolShed pre-loaded with static catalogs for the big servers."""
    shed = ToolShed(dispatcher=dispatcher)
    shed.register_static_catalog("pp_harness", PP_HARNESS_TOOLS)
    shed.register_static_catalog("eights", EIGHTS_TOOLS)
    shed.register_static_catalog("agentsmith", AGENTSMITH_TOOLS)
    return shed
