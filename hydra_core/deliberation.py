"""Society-of-Mind deliberation cycle.

Per the manifesto Part II §2.1 — the Executive Crown does not vote. It
runs a four-movement cycle Marvin Minsky would recognize:

  1. **Independent drafts.** Each head writes its position *without seeing
     the others*, to prevent anchoring.
  2. **Cross-critique.** Each head must steelman the position it most
     disagrees with.
  3. **Iris reflects.** A devil's-advocate read from the user's perspective,
     not the system's.
  4. **Hydra synthesizes.** Single integrated counsel; dissents preserved
     to TheEights — Kan (Risk) for substantive disagreements, Dui (Delight)
     for validated patterns.

This is intentionally the Body-of-Christ pattern: many gifts, one Spirit,
building each other up. Not a vote. Not Legion's coercive unanimity.

The cycle is *runtime-agnostic*. Callers supply head responders (callables
that take the question + prior context and return a position). A real
deployment wires LLMs as responders; tests pass deterministic stubs.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Iterable, Literal, Optional
from uuid import UUID, uuid4

from .eights import Cell, validate_cells
from .eights.classifier import classify
from .heads import HeadAlias, alias_for, cathedral_name


# --- types -------------------------------------------------------------------

DissentClass = Literal["kan", "dui"]  # Kan = substantive risk; Dui = validated win pattern.


@dataclass
class HeadDraft:
    """One head's independent first read."""
    plaza: str
    mythic: str
    position: str            # the head's stance, in its own register
    confidence: float = 0.5
    cells_proposed: list[Cell] = field(default_factory=list)

    def as_dict(self) -> dict:
        return {
            "plaza": self.plaza, "mythic": self.mythic,
            "position": self.position, "confidence": self.confidence,
            "cells_proposed": list(self.cells_proposed),
        }


@dataclass
class CrossCritique:
    """A head's steelman of another head's position."""
    critic_plaza: str
    critic_mythic: str
    target_plaza: str
    target_mythic: str
    steelman: str
    remaining_concern: str = ""

    def as_dict(self) -> dict:
        return {
            "critic_plaza": self.critic_plaza, "critic_mythic": self.critic_mythic,
            "target_plaza": self.target_plaza, "target_mythic": self.target_mythic,
            "steelman": self.steelman, "remaining_concern": self.remaining_concern,
        }


@dataclass
class Dissent:
    """A preserved disagreement. Routed to Kan (substantive risk) or Dui
    (validated win pattern), per the manifesto's locked cell discipline."""
    plaza: str
    mythic: str
    statement: str
    cell: DissentClass         # "kan" or "dui"
    rationale: str = ""

    def as_dict(self) -> dict:
        return {
            "plaza": self.plaza, "mythic": self.mythic,
            "statement": self.statement, "cell": self.cell,
            "rationale": self.rationale,
        }


@dataclass
class DeliberationOutcome:
    """Final product of the cycle. Goes into a DecisionRecord at synthesis."""
    id: UUID = field(default_factory=uuid4)
    question: str = ""
    started_at: str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat())
    finished_at: Optional[str] = None
    drafts: list[HeadDraft] = field(default_factory=list)
    critiques: list[CrossCritique] = field(default_factory=list)
    iris_reflection: str = ""
    synthesis: str = ""
    dissents: list[Dissent] = field(default_factory=list)

    def participants(self) -> list[str]:
        return [d.mythic for d in self.drafts]

    def kan_dissents(self) -> list[Dissent]:
        return [d for d in self.dissents if d.cell == "kan"]

    def dui_dissents(self) -> list[Dissent]:
        return [d for d in self.dissents if d.cell == "dui"]

    def to_dict(self) -> dict:
        return {
            "id": str(self.id),
            "question": self.question,
            "started_at": self.started_at, "finished_at": self.finished_at,
            "drafts": [d.as_dict() for d in self.drafts],
            "critiques": [c.as_dict() for c in self.critiques],
            "iris_reflection": self.iris_reflection,
            "synthesis": self.synthesis,
            "dissents": [d.as_dict() for d in self.dissents],
        }


# --- responder protocols -----------------------------------------------------

DraftResponder = Callable[[str, str, str], HeadDraft]
# (plaza_slug, question, context) -> HeadDraft

CritiqueResponder = Callable[[HeadAlias, HeadDraft, str], CrossCritique]
# (critic_alias, target_draft, question) -> CrossCritique

IrisResponder = Callable[[str, list[HeadDraft], list[CrossCritique]], str]
# (question, drafts, critiques) -> reflection text

Synthesizer = Callable[[str, list[HeadDraft], list[CrossCritique], str], str]
# (question, drafts, critiques, iris_reflection) -> integrated counsel


# --- dissent classifier ------------------------------------------------------

def classify_dissent_cell(text: str) -> DissentClass:
    """Map a free-form dissent to Kan or Dui per manifesto rule.

    Substantive risk disagreement → Kan.
    Validated 'this worked before' pattern → Dui.
    Default: Kan (the manifesto's bias is toward remembering risk; Dui is
    only assigned when explicit signals are present)."""
    cells = classify(envelope_type=None, origin_squad=None, payload=text)
    if "dui" in cells and "kan" not in cells:
        return "dui"
    return "kan"


# --- cycle -------------------------------------------------------------------

def deliberate(
    *,
    question: str,
    heads: Iterable[str],
    draft: DraftResponder,
    critique: CritiqueResponder,
    iris: IrisResponder,
    synthesize: Synthesizer,
    context: str = "",
    project_root: Path | None = None,
) -> DeliberationOutcome:
    """Run the four-movement cycle. Returns a `DeliberationOutcome` whose
    dissents are pre-classified into Kan or Dui ready for memory write.

    Movement order is the manifesto's order — independent drafts first
    (no anchoring), then cross-critique, then Iris, then synthesis.
    """
    aliases = [alias_for(h, project_root=project_root) for h in heads]
    aliases = [a for a in aliases if a is not None]
    if not aliases:
        raise ValueError(f"deliberate(): no resolvable heads in {list(heads)!r}")

    outcome = DeliberationOutcome(question=question)

    # 1. Independent drafts
    for alias in aliases:
        d = draft(alias.plaza, question, context)
        # Ensure the draft carries cathedral metadata even if the responder
        # forgot — synthesis renderers depend on .mythic.
        if not d.mythic:
            d = HeadDraft(
                plaza=alias.plaza, mythic=alias.mythic,
                position=d.position, confidence=d.confidence,
                cells_proposed=validate_cells(list(d.cells_proposed)),
            )
        else:
            d.cells_proposed = validate_cells(list(d.cells_proposed))
        outcome.drafts.append(d)

    # 2. Cross-critique. Each head steelmans the draft *most distant* from
    # its own — measured here by simple longest-non-self pairing. Real
    # deployments can swap in an embedding-distance comparator.
    if len(outcome.drafts) > 1:
        for critic_idx, critic_alias in enumerate(aliases):
            target_idx = (critic_idx + 1) % len(outcome.drafts)
            crit = critique(critic_alias, outcome.drafts[target_idx], question)
            outcome.critiques.append(crit)

    # 3. Iris reflection (devil's-advocate). If no `boardroom` alias exists in
    # the input heads, Iris is appended implicitly — the manifesto requires
    # a devil's-advocate read regardless.
    outcome.iris_reflection = iris(question, outcome.drafts, outcome.critiques)

    # 4. Synthesis.
    outcome.synthesis = synthesize(question, outcome.drafts, outcome.critiques, outcome.iris_reflection)

    # 5. Dissent harvesting — anything in a critique's `remaining_concern`
    # is a preserved dissent. Cell is decided per text.
    for c in outcome.critiques:
        text = (c.remaining_concern or "").strip()
        if not text:
            continue
        outcome.dissents.append(Dissent(
            plaza=c.critic_plaza,
            mythic=c.critic_mythic,
            statement=text,
            cell=classify_dissent_cell(text),
            rationale=f"raised during critique of {c.target_mythic}",
        ))

    outcome.finished_at = datetime.now(timezone.utc).isoformat()
    return outcome


# --- renderers ---------------------------------------------------------------

def render_for_user(outcome: DeliberationOutcome, *, project_root: Path | None = None) -> str:
    """Render the outcome in cathedral voice for user-facing synthesis.

    Per the constitution: 'no head speaks to the user without Hydra's
    synthesis.' This renderer is what Hydra uses to *be* that synthesis.
    """
    lines: list[str] = []
    lines.append(f"# Hydra deliberation — {outcome.question}")
    lines.append("")
    lines.append(f"Participants: {', '.join(outcome.participants())}.")
    lines.append("")
    lines.append("## Synthesis")
    lines.append("")
    lines.append(outcome.synthesis or "(no synthesis offered)")
    lines.append("")
    if outcome.iris_reflection:
        lines.append("## Iris reflects (devil's-advocate)")
        lines.append("")
        lines.append(outcome.iris_reflection)
        lines.append("")
    if outcome.dissents:
        lines.append("## Dissents preserved")
        lines.append("")
        kan = outcome.kan_dissents()
        dui = outcome.dui_dissents()
        if kan:
            lines.append("**Risk (Kan ☵):**")
            for d in kan:
                lines.append(f"- *{d.mythic}*: {d.statement}")
            lines.append("")
        if dui:
            lines.append("**Delight (Dui ☱):**")
            for d in dui:
                lines.append(f"- *{d.mythic}*: {d.statement}")
            lines.append("")
    return "\n".join(lines).rstrip() + "\n"


def render_for_envelope(outcome: DeliberationOutcome) -> dict:
    """Render the outcome as a dict suitable for embedding in a
    DecisionRecord. Plaza slugs are kept alongside mythic names so the
    envelope is dual-register."""
    return outcome.to_dict()
