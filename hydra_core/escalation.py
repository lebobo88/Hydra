"""Escalation-keyword classifier. Single home for `_ESCALATION_PATTERNS`.

Deliberately pure and dependency-free (imports nothing but `re`) so it can be
imported by both `hydra_core.judge.router` (content-aware judge-tier
escalation) and `hydra_core.plan_triage` (plan-rigor classification) without
either one dragging the other's import closure along with it.

`hydra_core.plan_triage` in particular MUST stay free of the `judge` package's
eager imports (dispatcher, MCP critique client) because `plan_rigor` is
recomputed on `/hydra:replay` and any MCP/env/filesystem touch in its import
chain breaks replay determinism and the `HYDRA_TEST_NO_DAEMONS=1` hermeticity
the test suite depends on. See `tests/test_plan_triage.py`.

Do NOT fork a second copy of these patterns elsewhere — import from here.
"""
from __future__ import annotations

import re

# Regex escalation: any match upgrades same_vendor -> cross_vendor (judge
# router) or trivial/standard -> major (plan triage).
_ESCALATION_PATTERNS = [
    re.compile(p, re.IGNORECASE) for p in [
        r"\bcapital[_ ]allocation\b",
        r"\bmerger\b",
        r"\bacquisition\b",
        r"\bcrisis\b",
        r"\bblack[_ ]swan\b",
        r"\bphi\b",
        r"\bhipaa\b",
        r"\bgdpr\b",
        r"\bconstitution\b",
        r"\bproduction\s+deploy",
        r"\b(?:auth|cred|secret|token)\b",
    ]
]


def goal_escalates(text: str) -> bool:
    """True if free text matches an escalation keyword pattern.

    Single home for the escalation keyword list (`_ESCALATION_PATTERNS`) so
    callers — `hydra_core.judge.router` and `hydra_core.plan_triage` — never
    fork their own copy. Pure: no state, no I/O.
    """
    return any(pat.search(text) for pat in _ESCALATION_PATTERNS)
