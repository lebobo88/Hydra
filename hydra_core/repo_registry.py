"""Allow-listed sibling-repo registry.

A workflow can direct pair-programmer at a sibling repository by setting
``target_repo_id`` on ``HydraState`` (via ``--repo <id>`` on the CLI).
This module resolves that id to a real on-disk path without ever accepting
a raw path string — the allow-list is the injection guard.

Typical project layout assumed:

    AiAppDeployments/
        Hydra/          ← this repo   (repo_id "hydra")
        pair-programmer/
        AgentSmith/
        ...

``_BASE`` defaults to ``AiAppDeployments/`` (two levels above this file:
``hydra_core/`` → ``Hydra/`` → ``AiAppDeployments/``).  Override with
the ``HYDRA_REPO_BASE`` environment variable for non-standard layouts or
test fixtures.
"""
from __future__ import annotations

import os
import re
import subprocess
from pathlib import Path
from typing import Optional

# ---------------------------------------------------------------------------
# Allow-list: repo_id -> directory NAME under the shared base dir.
# Keys are lower-case normalised identifiers.  Values are the EXACT folder
# names on disk (case-sensitive on POSIX).
# ---------------------------------------------------------------------------
_REPO_DIRNAMES: dict[str, str] = {
    "hydra": "Hydra",
    "pair-programmer": "pair-programmer",
    "agentsmith": "AgentSmith",
    "theeights": "TheEights",
    "xenia": "Xenia",
    "executivesuite": "ExecutiveSuite",
    "senate": "Senate",
    "marketbliss": "MarketBliss",
    "rlm-creative": "RLM-Creative",
}

# ---------------------------------------------------------------------------
# Base directory computation.
# hydra_core/ → Hydra/ → AiAppDeployments/
# Module-level constant for reference; resolve_repo_path calls _get_base()
# at invocation time so HYDRA_REPO_BASE monkeypatching in tests works
# without a module reload.
# ---------------------------------------------------------------------------
_BASE: Path = (
    Path(os.environ["HYDRA_REPO_BASE"])
    if "HYDRA_REPO_BASE" in os.environ
    else Path(__file__).resolve().parents[1].parent
)


def _get_base() -> Path:
    """Return the current base directory.

    Re-evaluates ``HYDRA_REPO_BASE`` at call time so tests can monkeypatch
    ``os.environ`` and see the effect without a module reload.
    """
    env_val = os.environ.get("HYDRA_REPO_BASE")
    if env_val:
        return Path(env_val)
    return Path(__file__).resolve().parents[1].parent


def resolve_repo_path(repo_id: str) -> Path:
    """Return the absolute path for *repo_id*, validated as an existing git repo.

    Security properties:
    - Allow-list lookup is the sole path-construction step; raw path strings
      are explicitly rejected before and by the lookup.
    - After resolution, ``candidate.is_relative_to(base)`` is checked to
      prevent symlink or override escapes from leaving the shared base dir.
    - A real ``git -C <path> rev-parse --show-toplevel`` call (local only,
      no network, 10-second timeout) confirms the directory is genuinely a
      git repo root — not merely a directory that happens to contain a
      ``.git`` file or symlink planted by an attacker.

    Args:
        repo_id: One of the allow-listed identifiers (case-insensitive).

    Returns:
        Resolved, existing ``Path`` that is a git repo root.

    Raises:
        ValueError: if *repo_id* contains path separators; if it is not in the
                    allow-list; if the resolved path escapes the base dir; or if
                    the directory is not a git repo root.
    """
    repo_id = (repo_id or "").strip().lower()

    # Explicit guard: raw path strings are not accepted.
    if any(c in repo_id for c in ("/", "\\", ":", "..")):
        raise ValueError(
            f"raw paths are not accepted; pass an allow-listed repo_id. "
            f"Got: {repo_id!r}"
        )

    if repo_id not in _REPO_DIRNAMES:
        raise ValueError(
            f"unknown repo_id {repo_id!r}; "
            f"allow-listed: {sorted(_REPO_DIRNAMES)}"
        )

    base = _get_base().resolve()
    candidate = (base / _REPO_DIRNAMES[repo_id]).resolve()

    # Base-escape guard: the resolved candidate must live under the resolved base.
    # Catches symlink traversal and unusual HYDRA_REPO_BASE values.
    if not candidate.is_relative_to(base):
        raise ValueError(
            f"resolved path {candidate} escapes repo base {base}; "
            f"repo_id={repo_id!r} rejected"
        )

    # Real git verification: run `git -C <candidate> rev-parse --show-toplevel`
    # and require its resolved output to equal the candidate.  This is a local
    # git invocation only (no network), with a 10-second timeout.
    try:
        proc = subprocess.run(
            ["git", "-C", str(candidate), "rev-parse", "--show-toplevel"],
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
        )
    except (FileNotFoundError, subprocess.TimeoutExpired) as exc:
        raise ValueError(
            f"{repo_id!r} resolved to {candidate}: git verification failed: {exc}"
        ) from exc

    if proc.returncode != 0:
        raise ValueError(
            f"{repo_id!r} resolved to {candidate} which is not a git repo "
            f"(git exit {proc.returncode}: {proc.stderr.strip()!r})"
        )

    toplevel = Path(proc.stdout.strip()).resolve()
    if toplevel != candidate:
        raise ValueError(
            f"{repo_id!r}: git toplevel {toplevel} != resolved candidate {candidate}; "
            f"rejected (possible nested-repo or symlink mismatch)"
        )

    return candidate


def is_known_repo(repo_id: str) -> bool:
    """Return ``True`` if *repo_id* is in the allow-list, without raising."""
    try:
        normalised = (repo_id or "").strip().lower()
        return normalised in _REPO_DIRNAMES
    except Exception:  # noqa: BLE001
        return False


# ---------------------------------------------------------------------------
# CLI argument parser
# ---------------------------------------------------------------------------

# Matches both "--repo <id>" (space-separated) and "--repo=<id>" (equals-form).
# Group 1 captures the value after the separator, or is empty/absent for bare
# "--repo" with nothing following (caught by the bare-token check below).
# The leading (?:^|\s) prevents matching inside a word.
_REPO_ARG_RE = re.compile(
    r"(?:^|\s)(--repo(?:=(\S+)|\s+(\S+))?)(?=\s|$)",
    re.IGNORECASE,
)

# Bare "--repo" with no value following (space-form: "--repo" at end of string
# or followed only by another flag).  Used to detect missing-value errors.
_REPO_BARE_RE = re.compile(
    r"(?:^|\s)--repo(?:\s+--|\s*$)",
    re.IGNORECASE,
)

# Count total "--repo" occurrences (both forms) for duplicate detection.
_REPO_COUNT_RE = re.compile(
    r"(?:^|\s)--repo(?:=|\s)",
    re.IGNORECASE,
)


def parse_repo_arg(text: str) -> tuple[Optional[str], str]:
    """Extract an optional ``--repo <id>`` or ``--repo=<id>`` token from *text*.

    Searches *text* for the ``--repo`` argument, validates the id against the
    allow-list, removes the token from the string, and returns
    ``(repo_id, cleaned_text)``.  When no ``--repo`` is present, returns
    ``(None, text)`` unchanged.

    Supported forms:
        --repo agentsmith Fix X          (space-separated)
        --repo=agentsmith Fix X          (equals-form)

    Args:
        text: The raw argument string (e.g. ``"--repo agentsmith Fix AS-GV-2"``).

    Returns:
        A ``(repo_id_or_None, text_with_--repo_removed)`` tuple.

    Raises:
        ValueError: if ``--repo`` is present with no value following it.
        ValueError: if ``--repo`` appears more than once.
        ValueError: if the id is not in the allow-list.
    """
    # Equals-form with empty or whitespace-only value: "--repo=" or "--repo=  ".
    # Must be caught before the count/match logic because _REPO_COUNT_RE only
    # matches "--repo=" when a non-space char follows, so "--repo=" at end of
    # string slips through the duplicate counter and _REPO_ARG_RE returns no
    # group-2 value — resulting in a silent no-op instead of a clear error.
    if re.search(r"(?:^|\s)--repo=\s*(?:\s|$)", text, re.IGNORECASE):
        raise ValueError("--repo requires a value")

    # Bare --repo with no value (space-form end-of-string or followed by flag).
    if _REPO_BARE_RE.search(text):
        raise ValueError("--repo requires a value")

    # Also catch bare --repo at end of string (regex above may miss trailing
    # whitespace variants); check directly.
    stripped = text.strip()
    if re.search(r"(?:^|\s)--repo$", stripped, re.IGNORECASE):
        raise ValueError("--repo requires a value")

    # Count occurrences of --repo (both forms).
    occurrences = len(_REPO_COUNT_RE.findall(text))
    if occurrences > 1:
        raise ValueError(
            f"--repo specified more than once ({occurrences} times); "
            "only a single repo target is supported per invocation"
        )

    m = _REPO_ARG_RE.search(text)
    if m is None:
        return None, text

    # Group 2 = equals-form value; group 3 = space-form value.
    # Strip both; raise if the result is empty (defensive catch-all for any
    # equals-form edge case not caught by the early guards above).
    raw_id = ((m.group(2) or "") + (m.group(3) or "")).strip()
    if not raw_id:
        raise ValueError("--repo requires a value")

    if not is_known_repo(raw_id):
        raise ValueError(
            f"--repo {raw_id!r} is not an allow-listed repo_id; "
            f"known: {sorted(_REPO_DIRNAMES)}"
        )

    # Remove the entire matched token (full match in group 0).  The leading
    # whitespace is inside the match so re.sub removes it cleanly; strip() tidies
    # any resulting double-spaces or leading/trailing whitespace.
    cleaned = text[: m.start()].rstrip() + " " + text[m.end():].lstrip()
    cleaned = cleaned.strip()
    # Collapse any double-spaces left after removal.
    cleaned = re.sub(r"  +", " ", cleaned)
    return raw_id.lower(), cleaned
