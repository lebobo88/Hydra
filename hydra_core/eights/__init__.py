"""TheEights — the eight-cell tag vocabulary for Hydra's semantic memory.

This module is the *vocabulary*, not the storage. Per the manifesto's
Stage-3 locked decision (see ROADMAP-MANIFESTO.md), the cells are a tag /
facet vocabulary over the existing episodic + semantic stores rather than
a hard storage partition. This keeps the symbolic charter (resurrection,
trigrams of transformation, wisdom-from-the-many-headed) without locking
the schema.

The eight cells follow the I Ching trigrams (Wilhelm/Baynes ordering):

  ☰ Qian  — Heaven / The Creative   → Vision
  ☷ Kun   — Earth / The Receptive   → Context
  ☳ Zhen  — Thunder / The Arousing  → Triggers
  ☴ Xun   — Wind / The Gentle       → Influence
  ☵ Kan   — Water / The Abysmal     → Risk
  ☲ Li    — Fire / The Clinging     → Focus
  ☶ Gen   — Mountain / Keeping Still → Constraints
  ☱ Dui   — Lake / The Joyous       → Delight

Dui is first-class. Most agent systems forget what worked; Hydra
remembers victories so future routing is hope-shaped, not just risk-shaped.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Literal, get_args


Cell = Literal["qian", "kun", "zhen", "xun", "kan", "li", "gen", "dui"]

# Tuple form — useful for iteration, validation, schema generation.
ALL_CELLS: tuple[Cell, ...] = get_args(Cell)


@dataclass(frozen=True)
class CellSpec:
    cell: Cell
    trigram: str       # the unicode trigram glyph
    name: str          # English aspect (Vision / Context / …)
    chinese: str       # Qian / Kun / …
    aspect: str        # Heaven / Earth / Thunder / Wind / Water / Fire / Mountain / Lake
    quality: str       # Creative / Receptive / Arousing / Gentle / Abysmal / Clinging / Keeping Still / Joyous
    holds: str         # one-line description of what this cell stores
    example_queries: tuple[str, ...]


CELL_SPECS: dict[Cell, CellSpec] = {
    "qian": CellSpec(
        cell="qian",
        trigram="☰",
        name="Vision",
        chinese="Qian",
        aspect="Heaven",
        quality="Creative",
        holds=(
            "Mission, immortal-head intent, long-horizon goals, the user's "
            "covenantal aims."
        ),
        example_queries=(
            "What does the user actually want, ultimately?",
            "Which goals predate this session?",
        ),
    ),
    "kun": CellSpec(
        cell="kun",
        trigram="☷",
        name="Context",
        chinese="Kun",
        aspect="Earth",
        quality="Receptive",
        holds="The world as it is — customers, market, environment, received givens.",
        example_queries=(
            "Who are we serving and where do they live?",
            "What is the state of the field around us?",
        ),
    ),
    "zhen": CellSpec(
        cell="zhen",
        trigram="☳",
        name="Triggers",
        chinese="Zhen",
        aspect="Thunder",
        quality="Arousing",
        holds="Events, signals, alerts, the inbound moves that wake the system.",
        example_queries=(
            "What event prompted this workflow?",
            "Which alerts have fired in the last week?",
        ),
    ),
    "xun": CellSpec(
        cell="xun",
        trigram="☴",
        name="Influence",
        chinese="Xun",
        aspect="Wind",
        quality="Gentle",
        holds=(
            "Soft signals — brand, reputation, relationships, what penetrates "
            "without force."
        ),
        example_queries=(
            "How is the brand being received this quarter?",
            "Which relationships are warming or cooling?",
        ),
    ),
    "kan": CellSpec(
        cell="kan",
        trigram="☵",
        name="Risk",
        chinese="Kan",
        aspect="Water",
        quality="Abysmal",
        holds=(
            "Threats, dangers, failures, post-mortems, Cerberus' findings, "
            "substantive dissent worth remembering."
        ),
        example_queries=(
            "What did Themis flag the last time we considered EU expansion?",
            "Which workflows surfaced a constitution_breach in the past 90 days?",
        ),
    ),
    "li": CellSpec(
        cell="li",
        trigram="☲",
        name="Focus",
        chinese="Li",
        aspect="Fire",
        quality="Clinging",
        holds="Active goals, in-flight projects, what currently has the system's attention.",
        example_queries=(
            "What is in flight right now and who owns it?",
            "Which heads are loaded for this week?",
        ),
    ),
    "gen": CellSpec(
        cell="gen",
        trigram="☶",
        name="Constraints",
        chinese="Gen",
        aspect="Mountain",
        quality="Keeping Still",
        holds=(
            "Immovable facts — regulatory, contractual, technical, theological. "
            "The mountain you do not move."
        ),
        example_queries=(
            "What compliance regimes bind us in this domain?",
            "Which technical decisions are sealed?",
        ),
    ),
    "dui": CellSpec(
        cell="dui",
        trigram="☱",
        name="Delight",
        chinese="Dui",
        aspect="Lake",
        quality="Joyous",
        holds=(
            "Wins, gratitudes, joy, what works, what users love. Hope-shaped "
            "memory. The cell most agent systems forget."
        ),
        example_queries=(
            "What worked last time we shipped a creative campaign?",
            "Which patterns have produced delight for this client?",
        ),
    ),
}


def cell_of(name_or_aspect: str) -> Cell | None:
    """Resolve a free-form string to a cell. Accepts the slug, the trigram,
    the English aspect, or the Chinese name. Case-insensitive."""
    if not name_or_aspect:
        return None
    needle = name_or_aspect.strip().lower()
    for spec in CELL_SPECS.values():
        if needle in {
            spec.cell, spec.trigram, spec.name.lower(),
            spec.chinese.lower(), spec.aspect.lower(), spec.quality.lower(),
        }:
            return spec.cell
    return None


def validate_cells(cells: list[str]) -> list[Cell]:
    """Normalize and validate a list of cell-ish strings. Drops anything that
    can't resolve. Used by the classifier and by MCP boundary tagging."""
    out: list[Cell] = []
    seen: set[Cell] = set()
    for c in cells or []:
        resolved = cell_of(c)
        if resolved and resolved not in seen:
            seen.add(resolved)
            out.append(resolved)
    return out
