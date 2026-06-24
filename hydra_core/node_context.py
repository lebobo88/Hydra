"""Node-scoped context trimming for the supervisor graph.

Each LangGraph node (intake, planner, dispatch, synthesis, postcheck)
needs different tools and instructions. This module builds a trimmed
context per node type, stripping irrelevant instructions to reduce
token usage per turn.

Per "Dynamic System Instructions and Tool Exposure" (arXiv:2602.17046):
agents run 2-20x more loops within context limits by dynamically
adjusting system instructions and exposed tools per step.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from .squad_loader import SquadPack
from .tool_scope import build_node_tool_scope
from .toolshed import ToolShed


@dataclass
class NodeContext:
    """Trimmed context for a supervisor node."""
    node_name: str
    instructions: str
    tool_categories: list[str]
    relevant_squads: list[str]
    tool_scope_directive: str


_NODE_INSTRUCTIONS: dict[str, str] = {
    "intake": (
        "You are the Hydra Router. Classify the goal into 1+ squad slugs "
        "using deterministic keyword matching first, LLM fallback second. "
        "Establish constraints (budget, deadline, risk_tolerance, industries). "
        "Do NOT plan, decompose, or execute — only route."
    ),
    "planner": (
        "You are the Hydra Planner. Decompose the goal into typed TaskState "
        "entries. Build a small DAG. Populate selected_squads. Draft a "
        "CSuiteDecisionPacket if executive is in play. Decide whether "
        "HITL approval is needed based on risk gates."
    ),
    "approval": (
        "You are the HITL Gate. Render the approval request for the operator. "
        "Do NOT proceed until /hydra:approve or /hydra:resume is received."
    ),
    "dispatch": (
        "You are the Dispatcher. Fan out to squad subgraphs. For each "
        "pending task, invoke the appropriate squad adapter (mcp / "
        "agent-impersonation / claude-skill / subprocess / stub). "
        "Validate and redact envelopes at squad boundaries."
    ),
    "judge_per_squad": (
        "You are the Per-Squad Judge. Score each squad-produced envelope "
        "against its rubrics. Handle Reflexion retry on 'revise' verdicts. "
        "Escalate HITL on fail verdicts with high-severity rubrics. "
        "Respect the Reflexion x1 ceiling."
    ),
    "synthesis": (
        "You are the Synthesizer. Merge all SquadResults into a single "
        "DECISION_RECORD. Group envelopes by squad. Preserve dissenting "
        "opinions verbatim. List every artifact. Report budget burn. "
        "Set sealed=False for mutually-exclusive verdicts."
    ),
    "judge_synthesis": (
        "You are the Synthesis Judge. Score the final DecisionRecord against "
        "cross-vendor + synthesis-coherence rubrics. Escalate HITL on "
        "high-severity failures."
    ),
    "postcheck": (
        "You are the Postcheck Gate. Run enforce_governance: constitution, "
        "loop ceiling, envelope ceiling, MCP failures, budget, failed tasks, "
        "surfaced tasks. Release any orphaned pp-harness locks on surface."
    ),
}

_NODE_IRRELEVANT_TOPICS: dict[str, set[str]] = {
    "intake": {"judge", "synthesis", "reflexion", "best_of_n", "postcheck", "budget_enforcement"},
    "planner": {"judge", "synthesis", "reflexion", "best_of_n", "mcp_dispatch"},
    "approval": {"judge", "synthesis", "reflexion", "dispatch", "intake", "planner"},
    "dispatch": {"intake", "planner", "approval", "synthesis", "postcheck"},
    "judge_per_squad": {"intake", "planner", "approval", "dispatch", "postcheck", "synthesis"},
    "synthesis": {"intake", "planner", "approval", "dispatch", "judge"},
    "judge_synthesis": {"intake", "planner", "approval", "dispatch", "reflexion"},
    "postcheck": {"intake", "planner", "approval", "dispatch", "judge", "synthesis"},
}


# ---------------------------------------------------------------------------
# Per-squad dispatch priming (RC5).
#
# A claude-skill orchestrator receives this directive (alongside the envelope +
# tool scope) when it is dispatched, so the host-run skill knows the cross-squad
# DELEGATION CONTRACT: what to emit, with which fields, to hand work to its
# implementing squads. Without it the skill returned prose and its DEV_TASKs
# never carried a pp team or game context — engineering then ran the generic
# single-engineer template. Keyed by squad slug; absent = no extra priming.
# ---------------------------------------------------------------------------
_SQUAD_DISPATCH_PRIMING: dict[str, str] = {
    "rlm-gaming": (
        "You are the RLM-Gaming Arcade crown — an ORCHESTRATOR. Do the design, "
        "then DELEGATE implementation; never write engine source or media binaries "
        "yourself.\n"
        "DELEGATION CONTRACT — return delegated work as typed envelopes under the "
        "`emitted_envelopes` key of your result (a JSON list), in addition to your "
        "DECISION_RECORD:\n"
        "  • Engine/gameplay code -> a DEV_TASK per scoped unit, target_squad="
        "\"engineering\". REQUIRED fields: owner — which is one of the ROLE values "
        "frontend|backend|fullstack|devops|data (NOT a squad name; the squad is "
        "already named by target_squad=\"engineering\"); repo (the allow-listed game "
        "repo, e.g. \"candc\" or \"mc-test\"), branch, instructions. When the operator "
        "names a repo subfolder, include target_repo_subpath (or the alias subdir / "
        "repo_subpath) as a REPO-RELATIVE path, e.g. repo=\"mc-test\" + "
        "target_repo_subpath=\"test-3d\". SET pp_team to the right pair-programmer "
        "game team — game-feature-team (default), game-netcode-team (online), "
        "game-cert-team (submission/ratings), game-live-ops-team (seasons/events), "
        "game-bug-fix-team, game-refactor-team, game-accessibility-team, or "
        "game-art-pipeline-team. PACK the game context INTO instructions: design "
        "pillars, perf budget, engine/runtime target, and any determinism/"
        "server-authority constraints (engineering does not see your design docs "
        "otherwise).\n"
        "  • Art/audio/3D binaries -> a CREATIVE_BRIEF / SHOT_LIST / ASSET_JOB, "
        "target_squad=\"garland\".\n"
        "Hydra forwards each emitted envelope to its target squad automatically; an "
        "envelope missing target_squad routes by type (DEV_TASK->engineering, "
        "CREATIVE_BRIEF/SHOT_LIST/ASSET_JOB->garland)."
    ),
}


def get_squad_dispatch_priming(squad_slug: str) -> str:
    """Return the cross-squad delegation priming for a squad (``""`` if none)."""
    return _SQUAD_DISPATCH_PRIMING.get(squad_slug, "")


def build_node_context(
    node_name: str,
    *,
    selected_squads: list[str] | None = None,
    packs: dict[str, SquadPack] | None = None,
    toolshed: ToolShed | None = None,
) -> NodeContext:
    """Build a trimmed context for a specific supervisor node."""
    instructions = _NODE_INSTRUCTIONS.get(node_name, "")
    irrelevant = _NODE_IRRELEVANT_TOPICS.get(node_name, set())

    squads = selected_squads or []
    ps = packs or {}

    tool_scope = build_node_tool_scope(node_name, squads, ps)

    tool_cats = _node_tool_categories(node_name)

    return NodeContext(
        node_name=node_name,
        instructions=instructions,
        tool_categories=tool_cats,
        relevant_squads=squads,
        tool_scope_directive=tool_scope,
    )


def trim_system_prompt(
    full_prompt: str,
    node_name: str,
) -> str:
    """Trim a system prompt by removing sections irrelevant to the current node.

    Looks for markdown headers (## or ###) and removes sections whose titles
    match irrelevant topics for this node. This is a heuristic — it won't
    catch every irrelevant section, but it significantly reduces noise.
    """
    irrelevant = _NODE_IRRELEVANT_TOPICS.get(node_name, set())
    if not irrelevant:
        return full_prompt

    lines = full_prompt.split("\n")
    output_lines: list[str] = []
    skip_until_next_header = False
    skip_level = 0

    for line in lines:
        stripped = line.strip()
        if stripped.startswith("##"):
            level = len(stripped) - len(stripped.lstrip("#"))
            title_lower = stripped.lstrip("# ").lower()
            if any(topic in title_lower for topic in irrelevant):
                skip_until_next_header = True
                skip_level = level
                continue
            elif skip_until_next_header and level <= skip_level:
                skip_until_next_header = False

        if not skip_until_next_header:
            output_lines.append(line)

    return "\n".join(output_lines)


def _node_tool_categories(node_name: str) -> list[str]:
    """Return the tool categories relevant to a node."""
    mapping = {
        "intake": ["read", "config"],
        "planner": ["read", "config"],
        "approval": ["governance"],
        "dispatch": ["execute", "write", "read"],
        "judge_per_squad": ["judge", "read"],
        "synthesis": ["memory", "read", "write"],
        "judge_synthesis": ["judge", "read"],
        "postcheck": ["governance", "read"],
    }
    return mapping.get(node_name, [])


def get_node_instructions(node_name: str) -> str:
    """Get the focused instruction set for a supervisor node."""
    return _NODE_INSTRUCTIONS.get(node_name, "")


def get_irrelevant_topics(node_name: str) -> set[str]:
    """Get the set of topics irrelevant to a supervisor node."""
    return _NODE_IRRELEVANT_TOPICS.get(node_name, set())
