"""W2.4 (Hydra#92) runbook contract: the host-facing SKILL.md files document
the parallel head-leg behaviour, and every test they cite exists."""
from __future__ import annotations

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SKILLS = {
    "drive": ROOT / "plugins" / "hydra" / "skills" / "drive" / "SKILL.md",
    "run": ROOT / "plugins" / "hydra" / "skills" / "run" / "SKILL.md",
}

REQUIRED_TOKENS = ("host_actions", "budget_blocked_heads", "resume_in_progress")
OPEN_SENTENCE = (
    "A step opens every unrecorded head that is not yet open unless the budget "
    "gate blocks it; it opens nothing new when every unrecorded head is already "
    "open or budget-blocked."
)
SINGLE_STREAM_SENTENCE = "Engineering stays single-stream"


def _read(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def _w2_4_test_names() -> set[str]:
    names: set[str] = set()
    for f in (ROOT / "tests").glob("test_w2_4_*.py"):
        names.update(re.findall(r"^def (test_\w+)\(", _read(f), flags=re.M))
    return names


def test_drive_and_run_skill_document_host_actions() -> None:
    known = _w2_4_test_names()
    for label, path in SKILLS.items():
        text = _read(path)
        for token in REQUIRED_TOKENS:
            assert token in text, f"{label} SKILL.md missing {token!r}"
        assert OPEN_SENTENCE in text, f"{label} SKILL.md missing the open-heads sentence"
        assert SINGLE_STREAM_SENTENCE in text, (
            f"{label} SKILL.md missing the single-stream sentence")
        cited = re.findall(r"<!--\s*(test_\w+)\s*-->", text)
        assert cited, f"{label} SKILL.md cites no backing tests"
        for name in cited:
            assert name in known, (
                f"{label} SKILL.md cites {name}, not found in tests/test_w2_4_*.py")
