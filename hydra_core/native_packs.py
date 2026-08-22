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
    "executive": NativePack("executive-suite", "executivesuite", "boardroom", "output"),
    "garland": NativePack("rlm-creative", "rlm-creative", "brand-strategist", "RLM/output"),
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
