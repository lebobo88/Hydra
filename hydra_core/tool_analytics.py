"""Per-workflow tool usage analytics.

Tracks which tools each workflow/squad/node actually calls, surfaces
unused declared tools and frequently-needed-but-undeclared tools, and
feeds recommendations back for squad.yaml curation.

Implements the Stripe Toolshed pattern's feedback loop: observed usage
drives tool-set curation over time.
"""
from __future__ import annotations

import json
from collections import Counter
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

from .squad_loader import SquadPack


@dataclass
class ToolCall:
    """One observed tool invocation."""
    timestamp: str
    workflow_id: str
    squad_id: str
    node_name: str
    server: str
    tool: str
    status: str
    duration_ms: float = 0.0


@dataclass
class ToolUsageReport:
    """Aggregated usage report for a workflow or time period."""
    total_calls: int
    unique_tools: int
    calls_by_server: dict[str, int]
    calls_by_squad: dict[str, int]
    calls_by_node: dict[str, int]
    top_tools: list[tuple[str, int]]
    declared_but_unused: list[str]
    used_but_undeclared: list[str]
    recommendations: list[str]


class ToolUsageTracker:
    """In-memory tracker for tool usage within a supervisor session."""

    def __init__(self, packs: dict[str, SquadPack] | None = None) -> None:
        self._calls: list[ToolCall] = []
        self._packs = packs or {}

    def record(self, *,
               workflow_id: str,
               squad_id: str,
               node_name: str,
               server: str,
               tool: str,
               status: str,
               duration_ms: float = 0.0) -> None:
        """Record a tool invocation."""
        self._calls.append(ToolCall(
            timestamp=datetime.now(timezone.utc).isoformat(),
            workflow_id=workflow_id,
            squad_id=squad_id,
            node_name=node_name,
            server=server,
            tool=tool,
            status=status,
            duration_ms=duration_ms,
        ))

    def report(self, workflow_id: str | None = None) -> ToolUsageReport:
        """Generate a usage report, optionally filtered by workflow."""
        calls = self._calls
        if workflow_id:
            calls = [c for c in calls if c.workflow_id == workflow_id]

        if not calls:
            return ToolUsageReport(
                total_calls=0, unique_tools=0,
                calls_by_server={}, calls_by_squad={}, calls_by_node={},
                top_tools=[], declared_but_unused=[], used_but_undeclared=[],
                recommendations=[],
            )

        tool_counter: Counter[str] = Counter()
        server_counter: Counter[str] = Counter()
        squad_counter: Counter[str] = Counter()
        node_counter: Counter[str] = Counter()

        for c in calls:
            key = f"{c.server}.{c.tool}"
            tool_counter[key] += 1
            server_counter[c.server] += 1
            squad_counter[c.squad_id] += 1
            node_counter[c.node_name] += 1

        used_tools = set(tool_counter.keys())

        declared_tools: set[str] = set()
        for pack in self._packs.values():
            for t in pack.tools:
                server = t.mcp_server or pack.slug
                declared_tools.add(f"{server}.{t.name}")

        declared_but_unused = sorted(declared_tools - used_tools)
        used_but_undeclared = sorted(used_tools - declared_tools)

        recommendations = _generate_recommendations(
            tool_counter, declared_but_unused, used_but_undeclared,
        )

        return ToolUsageReport(
            total_calls=len(calls),
            unique_tools=len(tool_counter),
            calls_by_server=dict(server_counter.most_common()),
            calls_by_squad=dict(squad_counter.most_common()),
            calls_by_node=dict(node_counter.most_common()),
            top_tools=tool_counter.most_common(20),
            declared_but_unused=declared_but_unused,
            used_but_undeclared=used_but_undeclared,
            recommendations=recommendations,
        )

    def flush_to_file(self, path: Path) -> int:
        """Append all recorded calls to a JSONL file and clear the buffer."""
        if not self._calls:
            return 0
        path.parent.mkdir(parents=True, exist_ok=True)
        count = 0
        with path.open("a", encoding="utf-8") as f:
            for call in self._calls:
                f.write(json.dumps({
                    "ts": call.timestamp,
                    "workflow_id": call.workflow_id,
                    "squad_id": call.squad_id,
                    "node_name": call.node_name,
                    "server": call.server,
                    "tool": call.tool,
                    "status": call.status,
                    "duration_ms": call.duration_ms,
                }, default=str) + "\n")
                count += 1
        self._calls.clear()
        return count

    def load_from_file(self, path: Path) -> int:
        """Load historical calls from a JSONL file."""
        if not path.exists():
            return 0
        count = 0
        with path.open("r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    data = json.loads(line)
                    self._calls.append(ToolCall(
                        timestamp=data.get("ts", ""),
                        workflow_id=data.get("workflow_id", ""),
                        squad_id=data.get("squad_id", ""),
                        node_name=data.get("node_name", ""),
                        server=data.get("server", ""),
                        tool=data.get("tool", ""),
                        status=data.get("status", ""),
                        duration_ms=data.get("duration_ms", 0.0),
                    ))
                    count += 1
                except (json.JSONDecodeError, KeyError):
                    continue
        return count


def _generate_recommendations(
    tool_counter: Counter[str],
    declared_but_unused: list[str],
    used_but_undeclared: list[str],
) -> list[str]:
    """Generate curation recommendations from usage data."""
    recs: list[str] = []

    if declared_but_unused:
        recs.append(
            f"Consider removing {len(declared_but_unused)} declared-but-unused "
            f"tool(s) from squad.yaml: {', '.join(declared_but_unused[:5])}"
            + ("..." if len(declared_but_unused) > 5 else "")
        )

    if used_but_undeclared:
        recs.append(
            f"Consider adding {len(used_but_undeclared)} used-but-undeclared "
            f"tool(s) to squad.yaml: {', '.join(used_but_undeclared[:5])}"
            + ("..." if len(used_but_undeclared) > 5 else "")
        )

    top_tools = tool_counter.most_common(5)
    if top_tools:
        hottest = top_tools[0]
        if hottest[1] > 10:
            recs.append(
                f"Hot tool: {hottest[0]} called {hottest[1]} times. "
                "Consider caching or batching if latency is a concern."
            )

    return recs


def analytics_path(project_root: Path) -> Path:
    """Standard path for the tool usage analytics JSONL file."""
    return project_root / ".hydra" / "tool_usage.jsonl"
