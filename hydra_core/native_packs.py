"""Claude Code-native squad metadata.

This module deliberately contains no provider SDK dependency.  It maps Hydra's
registered packs to the sibling repositories that own their Claude plugins.
"""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from .repo_registry import resolve_repo_path


@dataclass(frozen=True)
class NativePack:
    plugin: str
    repo_id: str
    lead_agent: str
    output_root: str

    @property
    def qualified_lead_agent(self) -> str:
        """Claude Code plugin agents are addressed in their plugin namespace."""
        return f"{self.plugin}:{self.lead_agent}"


NATIVE_PACKS: dict[str, NativePack] = {
    # "planning" is a claude-native pack whose plugin (agents/skill) lives in
    # HYDRA's OWN plugin, not a sibling repo -- repo_id="hydra" resolves to
    # this repo via repo_registry.py.
    #
    # output_root is DELIBERATELY ".hydra/plan" and NOT "docs/plans":
    # write_native_artifact resolves to
    # `native_pack_root(slug) / output_root / relative`, and with
    # repo_id="hydra" that is HYDRA'S OWN checkout -- so an output_root of
    # "docs/plans" would land every plan inside Hydra's tree instead of the
    # target repo the plan is actually for, dirtying Hydra's working copy on
    # every run. It would also be the wrong writer for the job regardless:
    # write_native_artifact's suffix allow-list is frozen at
    # {.md, .json, .txt} (no .html), so it could never write the rendered
    # plan page anyway. The tracked `docs/plans/<id>.html` artifact is
    # written by `write_repo_artifact` (hydra_core/artifact_store.py, landed
    # in P2) against the TARGET repo's root -- write_native_artifact /
    # NATIVE_PACKS.output_root plays no part in that path and must not be
    # made to look like it does.
    "planning": NativePack("hydra", "hydra", "plan-author", ".hydra/plan"),
    "executive": NativePack("executive-suite", "executivesuite", "boardroom", "output"),
    "garland": NativePack("rlm-creative", "rlm-creative", "calliope", "RLM/output"),
    "legal-compliance": NativePack("senate", "senate", "general-counsel", "output"),
    "rlm-gaming": NativePack("rlm-gaming", "rlm-gaming", "the-director", "RLM/output"),
    "marketing-strategy": NativePack("marketbliss", "marketbliss", "marketing-supervisor", "output"),
    "marketing-creative": NativePack("marketbliss", "marketbliss", "marketing-supervisor", "output"),
    "marketing-research": NativePack("marketbliss", "marketbliss", "marketing-supervisor", "output"),
    "marketing-production": NativePack("marketbliss", "marketbliss", "marketing-supervisor", "output"),
    "marketing-ops": NativePack("marketbliss", "marketbliss", "marketing-supervisor", "output"),
}


def native_pack(slug: str) -> NativePack:
    try:
        return NATIVE_PACKS[slug]
    except KeyError as exc:
        raise ValueError(f"no native Claude Code pack registered for {slug!r}") from exc


def native_pack_root(slug: str) -> Path:
    return resolve_repo_path(native_pack(slug).repo_id)
