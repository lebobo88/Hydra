"""Hydra cross-model judge plane.

Ports the principles of pair-programmer's rubber-duck-across-vendors judging into
Hydra's supervisor lifecycle. Reuses PP's pp_codex / pp_gemini MCP servers as the
vendor abstraction layer; adds Hydra-native rubrics for executive, garland, and
cross-domain governance.

Public surface:
    JudgeVerdict   — the verdict envelope (schemas.py)
    route_judge    — tier policy (router.py)
    dispatch_judge — invoke the judge via MCP (dispatcher.py)
    borda_winner   — rank-aggregate N candidates (borda.py)
    package_retry  — Reflexion ×1 bridge (reflexion.py)

Skeleton phase: dispatcher is a no-op that always returns outcome="pass".
Real verdicts wired in Phase 2.
"""
from .schemas import JudgeVerdict, RubricRef
from .registry import get_rubric, list_rubrics
from .router import route_judge, JudgeRoute
from .dispatcher import dispatch_judge, JudgeDispatchError
from .borda import borda_winner
from .reflexion import package_retry, ReflexionPacket
from .mcp_client import MCPCritiqueClient
from .policy import JudgePolicy, load_policy
from .best_of_n import BestOfNOutcome, best_of_n_run, judge_and_rank

# Register JudgeVerdict in the parent schema registry. Done here (not in
# schemas.py) to avoid a schemas ↔ judge import cycle.
from .. import schemas as _parent_schemas
_parent_schemas._register_judge_verdict()

__all__ = [
    "JudgeVerdict",
    "RubricRef",
    "get_rubric",
    "list_rubrics",
    "route_judge",
    "JudgeRoute",
    "dispatch_judge",
    "JudgeDispatchError",
    "borda_winner",
    "package_retry",
    "ReflexionPacket",
    "MCPCritiqueClient",
    "JudgePolicy",
    "load_policy",
    "BestOfNOutcome",
    "best_of_n_run",
    "judge_and_rank",
]
