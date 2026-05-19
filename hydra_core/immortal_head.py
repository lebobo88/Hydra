"""Immortal-head loader and alignment verifier.

The immortal head is `CONSTITUTION.md` at the repo root. It is read by every
agent on every turn and is never written to by any agent.

This module is the gate. Three responsibilities:

  1. **Load** the constitution and compute its SHA-256 hash. The hash is the
     cryptographic identity of the immortal head for the session. A change
     in hash is a change in the law.

  2. **Extract refusal patterns** from Section IV of the constitution. These
     are the absolute refusals that bound every action.

  3. **Verify intent** of any envelope or proposed action against the refusal
     patterns. Returns an `AlignmentVerdict` that downstream governance can
     act on (typically: surface as HITL with reason='constitution_breach').

CLI:
    python -m hydra_core.immortal_head verify

Prints the hash, the refusal-pattern count, and a summary block. Exit code 0
if the constitution loads cleanly, 1 otherwise.
"""
from __future__ import annotations

import hashlib
import re
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterable, Optional


_CONSTITUTION_FILENAME = "CONSTITUTION.md"
_REFUSAL_SECTION_HEADER = "## IV. Refusals"
_NEXT_SECTION_RE = re.compile(r"^## [VX]", re.MULTILINE)


def _find_repo_root(start: Path | None = None) -> Path:
    """Walk up from start (or this file) looking for CONSTITUTION.md."""
    p = (start or Path(__file__)).resolve()
    for parent in [p, *p.parents]:
        if (parent / _CONSTITUTION_FILENAME).is_file():
            return parent
    raise FileNotFoundError(
        f"{_CONSTITUTION_FILENAME} not found from {p}. "
        "The immortal head must exist at the repo root before any workflow runs."
    )


@dataclass(frozen=True)
class ConstitutionSnapshot:
    """A frozen view of CONSTITUTION.md as of a given moment."""

    path: Path
    text: str
    sha256: str
    refusals: tuple[str, ...]

    def summary(self) -> str:
        return (
            f"Constitution: {self.path}\n"
            f"SHA-256:      {self.sha256}\n"
            f"Refusals:     {len(self.refusals)} clause(s)\n"
            f"Bytes:        {len(self.text.encode('utf-8'))}\n"
        )


def _extract_refusals(text: str) -> tuple[str, ...]:
    """Pull the numbered clauses out of Section IV."""
    start = text.find(_REFUSAL_SECTION_HEADER)
    if start < 0:
        return ()
    after = text[start + len(_REFUSAL_SECTION_HEADER):]
    end_match = _NEXT_SECTION_RE.search(after)
    body = after[: end_match.start()] if end_match else after
    clauses = re.findall(r"^\d+\.\s+\*\*(.+?)\*\*", body, re.MULTILINE)
    return tuple(c.strip() for c in clauses)


def load_constitution(repo_root: Path | None = None) -> ConstitutionSnapshot:
    """Load CONSTITUTION.md and return a frozen snapshot with its hash + refusals."""
    root = repo_root or _find_repo_root()
    path = root / _CONSTITUTION_FILENAME
    text = path.read_text(encoding="utf-8")
    sha256 = hashlib.sha256(text.encode("utf-8")).hexdigest()
    refusals = _extract_refusals(text)
    return ConstitutionSnapshot(path=path, text=text, sha256=sha256, refusals=refusals)


@dataclass
class AlignmentVerdict:
    """Result of running a proposed action through the immortal-head gate."""

    aligned: bool
    breached_refusals: tuple[str, ...] = field(default_factory=tuple)
    rationale: str = ""

    @property
    def reason_code(self) -> str:
        return "constitution_aligned" if self.aligned else "constitution_breach"


# --- Heuristic refusal matchers ------------------------------------------------
# Each refusal clause in CONSTITUTION.md gets a coarse pattern. These are
# intentionally permissive (better to surface a borderline case for HITL than
# to silently approve a Legion-shaped move). They are not the last word; they
# are the *first* word — the user is the second, via HITL.

_BREACH_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = (
    # 3 — must not rewrite the constitution
    ("rewrite_constitution",
     re.compile(r"(edit|rewrite|patch|modify|overwrite|delete).{0,30}constitution\.md", re.IGNORECASE)),
    # 4 — must not deceive
    ("deception",
     re.compile(r"(fabricate|forge|spoof|hide|conceal).{0,40}(citation|source|tool[_ ]call|model)", re.IGNORECASE)),
    # 5 — must not bypass HITL
    ("bypass_hitl",
     re.compile(r"(skip|bypass|auto[- ]?approve|silently approve).{0,30}(hitl|approval|human[- ]in[- ]the[- ]loop)", re.IGNORECASE)),
    # 7 — venom without Cerberus
    ("unguarded_venom",
     re.compile(
         r"(rm\s+-rf"
         r"|force[- ]push|push\s+--force"
         r"|prod(uction)?[- ]deploy"
         r"|wire[- ]transfer|charge[- ]card"
         r"|drop[- ]table)",
         re.IGNORECASE,
     )),
    # 8 — procedural update against the constitution
    ("procedural_overwrite",
     re.compile(r"(rewrite|override|disable).{0,20}(refusal|constitution|rule of faith)", re.IGNORECASE)),
)


def verify_intent(
    payload: str | dict | object,
    snapshot: Optional[ConstitutionSnapshot] = None,
) -> AlignmentVerdict:
    """Check a proposed action (envelope dict, free text, or anything with str())
    against the constitution's refusal patterns.

    This is the gate site. Call before:
      - committing a procedural-memory update,
      - executing a capability marked `venom: true`,
      - completing a workflow's `postcheck` phase,
      - any cross-squad handoff that grants a destructive tool.

    Returns an AlignmentVerdict. `aligned=False` → surface as HITL with
    reason='constitution_breach'.
    """
    snap = snapshot or load_constitution()
    text = _stringify(payload)

    breached: list[str] = []
    for name, pat in _BREACH_PATTERNS:
        if pat.search(text):
            breached.append(name)

    if breached:
        rationale = (
            "Proposed action matched refusal pattern(s): "
            + ", ".join(breached)
            + f". Constitution {snap.sha256[:12]} requires HITL review."
        )
        return AlignmentVerdict(
            aligned=False,
            breached_refusals=tuple(breached),
            rationale=rationale,
        )
    return AlignmentVerdict(
        aligned=True,
        breached_refusals=(),
        rationale=f"No refusal pattern matched. Constitution {snap.sha256[:12]} satisfied.",
    )


def _stringify(payload: str | dict | object) -> str:
    if isinstance(payload, str):
        return payload
    if isinstance(payload, dict):
        # Concatenate values; keys are usually field names, not content.
        return " ".join(_stringify(v) for v in payload.values())
    if isinstance(payload, (list, tuple)):
        return " ".join(_stringify(v) for v in payload)
    return str(payload)


# --- CLI -----------------------------------------------------------------------

def _main(argv: Iterable[str]) -> int:
    args = list(argv)
    cmd = args[0] if args else "verify"
    if cmd == "verify":
        try:
            snap = load_constitution()
        except FileNotFoundError as e:
            sys.stderr.write(str(e) + "\n")
            return 1
        sys.stdout.write(snap.summary())
        if not snap.refusals:
            sys.stderr.write(
                "WARNING: no refusal clauses parsed from Section IV. "
                "The immortal head is loaded but unarmed.\n"
            )
            return 1
        return 0
    sys.stderr.write(f"Unknown subcommand: {cmd!r}. Try `verify`.\n")
    return 2


if __name__ == "__main__":
    raise SystemExit(_main(sys.argv[1:]))
