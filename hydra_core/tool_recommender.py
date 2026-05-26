"""Adaptive tool subsetting — usage-driven squad.yaml curation.

Uses D1's tool usage analytics data to build tool-to-squad affinity
scores and recommend additions/removals for squad.yaml tool declarations.

Inspired by:
- Tool-to-Agent Retrieval (arXiv:2511.01854): shared embedding space
- Dynamic Tool Dependency Retrieval (arXiv:2512.17052): sequential deps
- Stripe Toolshed: feedback-driven curation
"""
from __future__ import annotations

import math
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .squad_loader import SquadPack
from .tool_analytics import ToolCall, ToolUsageTracker


@dataclass
class ToolAffinityScore:
    """How strongly a tool is associated with a squad."""
    tool_key: str
    squad_id: str
    call_count: int
    success_rate: float
    avg_duration_ms: float
    affinity: float


@dataclass
class ToolDependency:
    """Observed sequential dependency: tool_a is frequently called before tool_b."""
    tool_a: str
    tool_b: str
    co_occurrence_count: int
    strength: float


@dataclass
class CurationRecommendation:
    """A specific recommendation for changing a squad's tool declarations."""
    squad_id: str
    action: str  # "add" | "remove" | "upgrade_privilege" | "add_dependency"
    tool_key: str
    reason: str
    confidence: float
    evidence: dict[str, Any] = field(default_factory=dict)


class ToolRecommender:
    """Recommends squad.yaml changes based on observed tool usage patterns."""

    def __init__(self, tracker: ToolUsageTracker,
                 packs: dict[str, SquadPack]) -> None:
        self._tracker = tracker
        self._packs = packs

    def compute_affinities(self) -> list[ToolAffinityScore]:
        """Compute tool-to-squad affinity scores from usage data."""
        calls = self._tracker._calls
        if not calls:
            return []

        squad_tool_calls: dict[str, Counter[str]] = defaultdict(Counter)
        squad_tool_success: dict[str, Counter[str]] = defaultdict(Counter)
        squad_tool_duration: dict[str, dict[str, list[float]]] = defaultdict(
            lambda: defaultdict(list)
        )

        for c in calls:
            key = f"{c.server}.{c.tool}"
            squad_tool_calls[c.squad_id][key] += 1
            if c.status in ("done", "complete"):
                squad_tool_success[c.squad_id][key] += 1
            if c.duration_ms > 0:
                squad_tool_duration[c.squad_id][key].append(c.duration_ms)

        scores: list[ToolAffinityScore] = []
        for squad_id, tool_counts in squad_tool_calls.items():
            total_squad_calls = sum(tool_counts.values())
            for tool_key, count in tool_counts.items():
                success = squad_tool_success[squad_id].get(tool_key, 0)
                success_rate = success / count if count > 0 else 0.0
                durations = squad_tool_duration[squad_id].get(tool_key, [])
                avg_dur = sum(durations) / len(durations) if durations else 0.0

                frequency = count / total_squad_calls if total_squad_calls > 0 else 0.0
                affinity = frequency * success_rate * _log_boost(count)

                scores.append(ToolAffinityScore(
                    tool_key=tool_key,
                    squad_id=squad_id,
                    call_count=count,
                    success_rate=round(success_rate, 3),
                    avg_duration_ms=round(avg_dur, 1),
                    affinity=round(affinity, 4),
                ))

        scores.sort(key=lambda s: -s.affinity)
        return scores

    def detect_dependencies(self, window_size: int = 3) -> list[ToolDependency]:
        """Detect sequential tool dependencies from call ordering.

        Looks for tools that are frequently called within `window_size`
        calls of each other within the same workflow.
        """
        calls = self._tracker._calls
        if len(calls) < 2:
            return []

        workflow_calls: dict[str, list[str]] = defaultdict(list)
        for c in calls:
            key = f"{c.server}.{c.tool}"
            workflow_calls[c.workflow_id].append(key)

        pair_counter: Counter[tuple[str, str]] = Counter()
        for wf_calls in workflow_calls.values():
            for i, tool_a in enumerate(wf_calls):
                for j in range(i + 1, min(i + window_size + 1, len(wf_calls))):
                    tool_b = wf_calls[j]
                    if tool_a != tool_b:
                        pair_counter[(tool_a, tool_b)] += 1

        total_windows = sum(
            max(0, len(wf) - 1) for wf in workflow_calls.values()
        ) or 1

        deps: list[ToolDependency] = []
        for (a, b), count in pair_counter.most_common(50):
            strength = count / total_windows
            if count >= 2:
                deps.append(ToolDependency(
                    tool_a=a, tool_b=b,
                    co_occurrence_count=count,
                    strength=round(strength, 4),
                ))

        return deps

    def recommend(self, min_confidence: float = 0.3) -> list[CurationRecommendation]:
        """Generate curation recommendations for all squads."""
        affinities = self.compute_affinities()
        dependencies = self.detect_dependencies()
        recs: list[CurationRecommendation] = []

        declared_by_squad: dict[str, set[str]] = {}
        for slug, pack in self._packs.items():
            declared_by_squad[slug] = {
                f"{t.mcp_server or slug}.{t.name}" for t in pack.tools
            }

        affinity_by_squad: dict[str, dict[str, ToolAffinityScore]] = defaultdict(dict)
        for a in affinities:
            affinity_by_squad[a.squad_id][a.tool_key] = a

        for squad_id, declared in declared_by_squad.items():
            used = set(affinity_by_squad.get(squad_id, {}).keys())

            for tool in declared - used:
                recs.append(CurationRecommendation(
                    squad_id=squad_id,
                    action="remove",
                    tool_key=tool,
                    reason="Declared but never used in observed workflows.",
                    confidence=0.4,
                    evidence={"call_count": 0},
                ))

            for tool_key, score in affinity_by_squad.get(squad_id, {}).items():
                if tool_key not in declared and score.call_count >= 3:
                    confidence = min(0.9, score.affinity * 2)
                    if confidence >= min_confidence:
                        recs.append(CurationRecommendation(
                            squad_id=squad_id,
                            action="add",
                            tool_key=tool_key,
                            reason=(
                                f"Used {score.call_count} times with "
                                f"{score.success_rate:.0%} success rate but not declared."
                            ),
                            confidence=round(confidence, 3),
                            evidence={
                                "call_count": score.call_count,
                                "success_rate": score.success_rate,
                                "affinity": score.affinity,
                            },
                        ))

        for dep in dependencies:
            for squad_id, declared in declared_by_squad.items():
                if dep.tool_a in declared and dep.tool_b not in declared:
                    if dep.co_occurrence_count >= 3:
                        recs.append(CurationRecommendation(
                            squad_id=squad_id,
                            action="add_dependency",
                            tool_key=dep.tool_b,
                            reason=(
                                f"Frequently called after {dep.tool_a} "
                                f"({dep.co_occurrence_count} times) but not declared."
                            ),
                            confidence=round(min(0.8, dep.strength * 5), 3),
                            evidence={
                                "depends_on": dep.tool_a,
                                "co_occurrence": dep.co_occurrence_count,
                                "strength": dep.strength,
                            },
                        ))

        recs = [r for r in recs if r.confidence >= min_confidence]
        recs.sort(key=lambda r: -r.confidence)
        return recs

    def summary(self) -> dict[str, Any]:
        """Generate a summary of tool usage patterns and recommendations."""
        affinities = self.compute_affinities()
        deps = self.detect_dependencies()
        recs = self.recommend()

        return {
            "total_affinities": len(affinities),
            "top_affinities": [
                {"tool": a.tool_key, "squad": a.squad_id,
                 "calls": a.call_count, "affinity": a.affinity}
                for a in affinities[:10]
            ],
            "dependencies_detected": len(deps),
            "top_dependencies": [
                {"a": d.tool_a, "b": d.tool_b, "count": d.co_occurrence_count}
                for d in deps[:10]
            ],
            "recommendations": [
                {"squad": r.squad_id, "action": r.action,
                 "tool": r.tool_key, "confidence": r.confidence,
                 "reason": r.reason}
                for r in recs[:20]
            ],
        }


def _log_boost(count: int) -> float:
    """Logarithmic boost for call frequency — diminishing returns past ~10 calls."""
    return math.log2(count + 1) / math.log2(11)
