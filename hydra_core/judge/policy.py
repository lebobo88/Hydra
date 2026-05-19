"""Policy loader for the judge plane.

Reads `hydra_core/judge/policy.yaml` (built-in defaults) and optionally overlays
`<project_root>/.hydra/judge_policy.yaml` if present. Returns a typed snapshot
the supervisor consults to decide:
  - which squads have real judging enabled (others get the NoOp client),
  - which rubrics escalate to HITL on `fail`,
  - the per-workflow budget tripwire.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any


_DEFAULT_POLICY_PATH = Path(__file__).with_name("policy.yaml")


@dataclass
class JudgePolicy:
    enabled_squads: set[str] = field(default_factory=set)
    hitl_on_fail: set[str] = field(default_factory=set)
    budget_cap_per_workflow_usd: float = 2.50
    escalation_keywords: tuple[str, ...] = ()

    def squad_enabled(self, squad: str | None) -> bool:
        """True when the squad opts in to real cross-vendor judging.

        Empty enabled_squads = all squads enabled (development default).
        None squad → treat as enabled (post-synthesis path).
        """
        if not self.enabled_squads:
            return True
        if squad is None:
            return True
        return squad in self.enabled_squads

    def is_hitl_severity(self, rubric_id: str) -> bool:
        return rubric_id in self.hitl_on_fail


def _load_yaml(path: Path) -> dict[str, Any]:
    try:
        import yaml  # type: ignore
    except ImportError:
        return {}
    if not path.exists():
        return {}
    try:
        data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    except Exception:
        return {}
    return data if isinstance(data, dict) else {}


def load_policy(project_root: Path | None = None) -> JudgePolicy:
    """Load defaults from packaged policy.yaml; overlay project override."""
    base = _load_yaml(_DEFAULT_POLICY_PATH)
    override: dict[str, Any] = {}
    if project_root is not None:
        override = _load_yaml(Path(project_root) / ".hydra" / "judge_policy.yaml")

    merged: dict[str, Any] = {**base, **override}
    return JudgePolicy(
        enabled_squads=set(merged.get("enabled_squads") or []),
        hitl_on_fail=set(merged.get("hitl_on_fail") or []),
        budget_cap_per_workflow_usd=float(merged.get("budget_cap_per_workflow_usd", 2.50)),
        escalation_keywords=tuple(merged.get("escalation_keywords") or []),
    )
