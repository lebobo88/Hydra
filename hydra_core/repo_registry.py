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

Base directory computation (``_get_base()``, called at resolution time):

1. ``HYDRA_REPO_BASE`` env var — re-read every call so tests can monkeypatch.
2. Worktree-aware git probe: ``git -C <repo_dir> rev-parse --git-common-dir``
   is run (cached in ``_GIT_PROBE_CACHE`` keyed by ``str(repo_dir)``).  If the
   returned path is not ``<repo_dir>/.git`` the file is running from a linked git
   worktree; the main repo root is ``common_dir.parent`` and the base is
   ``main_root.parent`` (same level as the non-worktree case).
3. Naive fallback (probe fails / nonzero exit): two levels above this file —
   ``hydra_core/`` → ``Hydra/`` → ``AiAppDeployments/``.

Override with the ``HYDRA_REPO_BASE`` environment variable for non-standard
layouts or test fixtures.
"""
from __future__ import annotations

import difflib
import json
import os
import re
import subprocess
import tempfile
import time
from pathlib import Path
from typing import Any, Optional

# ---------------------------------------------------------------------------
# Allow-list: repo_id -> directory NAME (or forward-slash-separated relative
# path) under the shared base dir. Keys are lower-case normalised identifiers.
# Values are EXACT, hardcoded folder names/paths on disk (case-sensitive on
# POSIX) -- never derived from user input. A value MAY contain "/" to reach a
# repo nested one or more levels under base (e.g. "parent/child"); this is
# safe because values are fixed at authoring time (not attacker-controlled
# like the repo_id argument), and `base / value` still passes the
# is_relative_to(base) escape guard plus the git-toplevel verification below.
# Values must never contain ".." or be absolute -- this IS enforced, at import
# time, by _validate_repo_dirnames() below, so a bad entry fails fast and
# names the offending repo_id instead of surfacing later as an opaque
# escape-guard or git-toplevel error.
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
    "mc-test": "mc-test",
    "rlm-creative": "RLM-Creative",
    "rlm-gaming": "RLM-Gaming",
    "candc": "CandC",
    "hydra-galaga": "galaga-game-deepseek/hydra-galaga",
}


def _validate_repo_dirnames(dirnames: dict[str, str]) -> None:
    """Reject any hardcoded _REPO_DIRNAMES entry that isn't a safe relative,
    downward-only path (no absolute values, no '..' traversal segments).

    This is a contributor guard, not an attacker guard: values are authored
    in code, never attacker-controlled. But this dict IS the allow-list's
    injection guard, so a pasted-in absolute path or '..' segment should fail
    fast and name the offending repo_id, rather than surfacing later as an
    opaque base-escape or git-toplevel error at resolution time.
    """
    for rid, value in dirnames.items():
        candidate = Path(value)
        if candidate.is_absolute() or ".." in candidate.parts:
            raise RuntimeError(
                f"invalid _REPO_DIRNAMES entry for {rid!r}: {value!r} "
                f"(must be a relative, downward-only path)"
            )


_validate_repo_dirnames(_REPO_DIRNAMES)

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

# Module-level cache for the git --git-common-dir probe result, keyed by
# str(repo_dir).  Populated lazily by _get_base(); clear in tests to force
# a fresh probe.  The env-override path in _get_base() bypasses this cache.
_GIT_PROBE_CACHE: dict[str, Path] = {}


def _get_base() -> Path:
    """Return the current base directory (parent of the Hydra repo root).

    Resolution order:
    1. ``HYDRA_REPO_BASE`` env var — re-read every call so tests can monkeypatch
       ``os.environ`` and see the effect without a module reload.
    2. Worktree-aware git probe (result cached in ``_GIT_PROBE_CACHE``): if this
       file is running from a linked git worktree, ``git rev-parse
       --git-common-dir`` points at the main checkout's ``.git`` dir.  The base
       is derived as ``common_dir.parent.parent`` (main repo root → base dir).
    3. Naive fallback when the probe fails: ``Path(__file__).resolve().parents[1].parent``.
    """
    # 1. Env override — always wins; not cached so monkeypatching in tests works.
    env_val = os.environ.get("HYDRA_REPO_BASE")
    if env_val:
        return Path(env_val)

    # 2. Compute repo_dir (the directory containing hydra_core/) and naive base.
    repo_dir = Path(__file__).resolve().parents[1]
    naive_base = repo_dir.parent

    # 3. Worktree-aware probe (cached per repo_dir to avoid repeated subprocess calls).
    cache_key = str(repo_dir)
    if cache_key not in _GIT_PROBE_CACHE:
        try:
            proc = subprocess.run(
                ["git", "-C", str(repo_dir), "rev-parse", "--git-common-dir"],
                capture_output=True,
                text=True,
                timeout=5,
                check=False,
            )
            if proc.returncode == 0 and proc.stdout.strip():
                raw = proc.stdout.strip()
                raw_path = Path(raw)
                # --git-common-dir may return a relative path (e.g. ".git") or
                # an absolute path.  Resolve relative paths against repo_dir.
                if raw_path.is_absolute():
                    common_dir: Path = raw_path.resolve()
                else:
                    common_dir = (repo_dir / raw_path).resolve()
                _GIT_PROBE_CACHE[cache_key] = common_dir
            else:
                # Nonzero exit or empty output — treat as non-worktree (fall-soft).
                _GIT_PROBE_CACHE[cache_key] = (repo_dir / ".git").resolve()
        except Exception:  # noqa: BLE001 — git not found or timeout → fail-soft
            _GIT_PROBE_CACHE[cache_key] = (repo_dir / ".git").resolve()

    common_dir = _GIT_PROBE_CACHE[cache_key]

    # 4. If common_dir is NOT repo_dir/.git we are in a linked worktree.
    #    The main repo root is common_dir.parent; the shared base is one level above.
    expected_git = (repo_dir / ".git").resolve()
    if common_dir != expected_git:
        main_root = common_dir.parent  # e.g. C:\AiAppDeployments\Hydra
        return main_root.parent        # e.g. C:\AiAppDeployments

    return naive_base


# Call-time (monkeypatchable) resolvers for the operator repos.json config and
# its short-lived write lock. Both the READ path (_load_extra_repos, below)
# and the WRITE path (register_repo/unregister_repo, further down) call
# through these -- a prior version of this module had _load_extra_repos
# hardcode ``Path.home() / ".hydra" / "repos.json"`` inline while the write
# path referenced a separate module constant, which is exactly the kind of
# two-copies drift WS1 exists to eliminate.
#
# These are FUNCTIONS, not module-level constants, because Path.home() must be
# re-resolved on every call: HOME/USERPROFILE can change after import (e.g. a
# test's monkeypatch, or a long-lived process whose environment is mutated),
# and freezing the path at import time would silently decouple "where the
# operator edits repos.json" from "where Hydra actually reads it" -- the same
# class of silent divergence this registry exists to prevent.
def _repos_json_path() -> Path:
    return Path.home() / ".hydra" / "repos.json"


def _repos_lock_path() -> Path:
    return Path.home() / ".hydra" / "repos.json.lock"


def _load_extra_repos() -> dict[str, Path]:
    """Operator-registered repos beyond the hardcoded allow-list.

    Two trusted sources are merged (env overrides file):
      - ``~/.hydra/repos.json`` -- a JSON object ``{"<id>": "<abs-path>"}``.
      - ``HYDRA_EXTRA_REPOS`` -- the same shape as a JSON string.

    The registration *is* the allow-list (same trust level as
    ``HYDRA_REPO_BASE``), so values may be absolute paths anywhere on disk,
    including a git repo nested under an unrelated parent. Ids are lower-cased.
    Malformed entries are skipped fail-soft so a bad config never breaks
    resolution of the built-in repos.
    """
    extra: dict[str, Path] = {}
    sources: list[str] = []
    try:
        cfg = _repos_json_path()
        if cfg.is_file():
            sources.append(cfg.read_text(encoding="utf-8"))
    except Exception:  # noqa: BLE001 -- a bad config never breaks resolution
        pass
    env_val = os.environ.get("HYDRA_EXTRA_REPOS")
    if env_val and env_val.strip():
        sources.append(env_val)
    for raw in sources:
        try:
            data = json.loads(raw)
        except Exception:  # noqa: BLE001
            continue
        if not isinstance(data, dict):
            continue
        for rid, rpath in data.items():
            key = str(rid).strip().lower()
            if not key or not isinstance(rpath, str) or not rpath.strip():
                continue
            try:
                extra[key] = Path(rpath).expanduser()
            except Exception:  # noqa: BLE001
                continue
    return extra


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

    extra = _load_extra_repos()
    if repo_id in extra:
        # Operator-registered (trusted config, same level as HYDRA_REPO_BASE).
        # May live anywhere on disk -- incl. a git repo nested under an unrelated
        # parent -- so the base-escape guard does NOT apply. The git-root
        # verification below still confirms it is a genuine repo root.
        candidate = extra[repo_id].resolve()
    elif repo_id in _REPO_DIRNAMES:
        base = _get_base().resolve()
        candidate = (base / _REPO_DIRNAMES[repo_id]).resolve()

        # Base-escape guard: the resolved candidate must live under the resolved
        # base. Catches symlink traversal and unusual HYDRA_REPO_BASE values.
        if not candidate.is_relative_to(base):
            raise ValueError(
                f"resolved path {candidate} escapes repo base {base}; "
                f"repo_id={repo_id!r} rejected"
            )
    else:
        raise ValueError(
            f"unknown repo_id {repo_id!r}; "
            f"allow-listed: {sorted(set(_REPO_DIRNAMES) | set(extra))}"
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
        return normalised in _REPO_DIRNAMES or normalised in _load_extra_repos()
    except Exception:  # noqa: BLE001
        return False


def known_repo_ids() -> set[str]:
    """Return the full set of currently known repo ids (allow-list + extras).

    Combines ``_REPO_DIRNAMES`` keys with any operator-registered ids from
    ``_load_extra_repos()``. Fail-soft: extra-repo load errors are silently
    ignored so the built-in ids are always returned.
    """
    ids: set[str] = set(_REPO_DIRNAMES.keys())
    try:
        ids |= set(_load_extra_repos().keys())
    except Exception:  # noqa: BLE001
        pass
    return ids


_REPOS_LOCK_TIMEOUT_S = 10.0
_REPOS_LOCK_POLL_S = 0.05
_REPOS_LOCK_STALE_S = 30.0  # unlink a lock this old regardless of owner (best-effort)


def _acquire_repos_lock(timeout_s: float = _REPOS_LOCK_TIMEOUT_S) -> int:
    """Short-lived O_CREAT|O_EXCL claim on ``~/.hydra/repos.json.lock``.

    Held only for the duration of one read-modify-write cycle (register /
    unregister are quick synchronous calls, unlike cli.py's long-running
    resume claim).

    WS1 retry (finding 4) — narrowed claim: stale reclaim is AGE-ONLY. The
    PID written into the lock file (below) is diagnostic only — nothing
    reads it back or checks liveness before unlinking. A lock still held by
    a live process past ``_REPOS_LOCK_STALE_S`` (30s) CAN be reclaimed and
    replaced by a second process; this module does not implement owner/PID
    validation. In practice register/unregister are fast synchronous calls
    (a handful of filesystem ops), so a 30s stall almost always means the
    holder crashed or was killed mid-critical-section — but that is a
    likelihood argument, not an enforced guarantee. If cross-process races
    under this window become a real problem, add PID-liveness validation
    here before trusting the age check alone.
    """
    lock_path = _repos_lock_path()
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    deadline = time.monotonic() + timeout_s
    while True:
        try:
            fd = os.open(str(lock_path), os.O_CREAT | os.O_EXCL | os.O_WRONLY)
            os.write(fd, str(os.getpid()).encode())
            return fd
        except FileExistsError:
            try:
                age = time.time() - lock_path.stat().st_mtime
            except OSError:
                age = 0.0
            if age >= _REPOS_LOCK_STALE_S:
                try:
                    lock_path.unlink()
                except OSError:
                    pass
                continue
            if time.monotonic() >= deadline:
                raise TimeoutError(
                    f"timed out acquiring {lock_path} after {timeout_s}s "
                    "(a concurrent register/unregister may be stuck)"
                )
            time.sleep(_REPOS_LOCK_POLL_S)


def _release_repos_lock(fd: int) -> None:
    try:
        os.close(fd)
    finally:
        try:
            _repos_lock_path().unlink()
        except OSError:
            pass


def _atomic_write_repos_json(data: dict[str, str]) -> None:
    """temp file + os.replace — the write is atomic even if the process dies
    mid-write; a torn read is what _load_extra_repos degrades on fail-soft,
    which is exactly the silent mis-target this registry exists to prevent."""
    repos_path = _repos_json_path()
    repos_path.parent.mkdir(parents=True, exist_ok=True)
    tmp_fd, tmp_path_str = tempfile.mkstemp(
        dir=str(repos_path.parent), prefix=".repos-", suffix=".json.tmp"
    )
    tmp_path = Path(tmp_path_str)
    try:
        with os.fdopen(tmp_fd, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2, sort_keys=True)
            f.flush()
            os.fsync(f.fileno())
        os.replace(str(tmp_path), str(repos_path))
    except Exception:
        try:
            tmp_path.unlink()
        except OSError:
            pass
        raise


def register_repo(repo_id: str, path: str, *, force: bool = False,
                   init: bool = False) -> dict[str, str]:
    """Self-service registration into ``~/.hydra/repos.json`` (CLI-only — see
    ``hydra repo register`` in ``cli.py``; never expose this over MCP, it
    defines the allow-list). Atomic write under a short lock, verified by
    re-resolving through ``resolve_repo_path`` after the write, with a
    BEST-EFFORT rollback attempt if verification fails: the prior file
    contents are re-parsed with ``json.loads`` and rewritten via the same
    atomic writer, then ``ValueError`` is raised regardless of whether the
    rollback write itself succeeded (a rollback failure is swallowed, not
    propagated — the verification failure is always the error the caller
    sees). Two limits follow from this: (1) the rollback reconstructs the
    prior state from its *parsed* form rather than replaying the original
    bytes, so if the prior file was malformed JSON the "restored" file is an
    empty registry (``{}``), not a faithful byte-for-byte revert; (2) if the
    rollback write itself fails (e.g. disk full, permissions), the file is
    left in the post-failed-verification state, not the pre-write state.
    Callers must not assume ``~/.hydra/repos.json`` is guaranteed to match
    its pre-call contents after a ``ValueError`` here.

    Refuses to shadow a built-in ``_REPO_DIRNAMES`` id unless ``force=True``.
    ``init=True`` creates the directory (if absent) and runs ``git init`` when
    no ``.git`` is present, so a greenfield target (e.g. hydra-galaga) can be
    registered in one step instead of requiring a manual pre-existing repo.
    """
    key = (repo_id or "").strip().lower()
    if not key:
        raise ValueError("repo_id is required")
    if not re.match(r"^[a-z0-9][a-z0-9\-_]{0,63}$", key):
        raise ValueError(
            f"invalid repo_id {repo_id!r}: must match ^[a-z0-9][a-z0-9-_]{{0,63}}$"
        )
    if key in _REPO_DIRNAMES and not force:
        raise ValueError(
            f"{key!r} is a built-in repo id; pass force=True to register an "
            f"operator override (operator-registered entries take precedence "
            f"over the built-in allow-list at resolution time)"
        )

    target = Path(path).expanduser()
    if not target.is_absolute():
        raise ValueError(f"path must be absolute; got {path!r}")

    if init:
        target.mkdir(parents=True, exist_ok=True)
        if not (target / ".git").exists():
            proc = subprocess.run(
                ["git", "init", str(target)],
                capture_output=True, text=True, timeout=30, check=False,
            )
            if proc.returncode != 0:
                raise ValueError(f"git init failed for {target}: {proc.stderr.strip()}")
    else:
        if not target.exists():
            raise ValueError(f"{target} does not exist; pass --init to create it")
        if not (target / ".git").exists():
            raise ValueError(
                f"{target} is not a git repo (no .git found); pass --init to "
                f"run `git init`, or point repo_id at an existing git repo"
            )

    lock_fd = _acquire_repos_lock()
    try:
        repos_path = _repos_json_path()
        prior_raw = (
            repos_path.read_text(encoding="utf-8")
            if repos_path.is_file() else None
        )
        try:
            data = json.loads(prior_raw) if prior_raw and prior_raw.strip() else {}
        except json.JSONDecodeError:
            data = {}
        if not isinstance(data, dict):
            data = {}
        data[key] = str(target)
        _atomic_write_repos_json(data)

        try:
            resolve_repo_path(key)
        except Exception as verify_exc:  # noqa: BLE001 — roll back, then re-raise
            # WS1 retry (finding 4): route rollback through the same
            # temp-file + os.replace atomic writer used for the forward
            # write, instead of a bare write_text/unlink. A crash mid-write
            # here previously could itself leave repos.json torn — which is
            # exactly the failure mode _atomic_write_repos_json exists to
            # prevent on the forward path, so the rollback path must not
            # reintroduce it.
            if prior_raw is not None:
                try:
                    _atomic_write_repos_json(
                        json.loads(prior_raw) if prior_raw.strip() else {}
                    )
                except Exception:  # noqa: BLE001 — best-effort rollback
                    pass
            else:
                try:
                    repos_path.unlink()
                except OSError:
                    pass
            raise ValueError(
                f"registration verification failed for {key!r}: {verify_exc}"
            ) from verify_exc
    finally:
        _release_repos_lock(lock_fd)

    return {"repo_id": key, "path": str(target)}


def unregister_repo(repo_id: str) -> dict[str, Any]:
    """Remove ``repo_id`` from ``~/.hydra/repos.json`` (atomic, CLI-only)."""
    key = (repo_id or "").strip().lower()
    if not key:
        raise ValueError("repo_id is required")
    lock_fd = _acquire_repos_lock()
    try:
        repos_path = _repos_json_path()
        if not repos_path.is_file():
            raise ValueError(f"{key!r} is not registered (no ~/.hydra/repos.json)")
        raw = repos_path.read_text(encoding="utf-8")
        try:
            data = json.loads(raw) if raw.strip() else {}
        except json.JSONDecodeError:
            data = {}
        if not isinstance(data, dict):
            data = {}
        normalised = {str(k).strip().lower(): v for k, v in data.items()}
        if key not in normalised:
            raise ValueError(f"{key!r} is not registered")
        del normalised[key]
        _atomic_write_repos_json(normalised)
    finally:
        _release_repos_lock(lock_fd)
    return {"repo_id": key, "removed": True}


def list_registered_repos() -> dict[str, str]:
    """Return operator-registered repos (id -> absolute path string), sorted."""
    return {k: str(v) for k, v in sorted(_load_extra_repos().items())}


def unknown_repo_hitl_fields(attempted_id: str) -> dict[str, Any]:
    """Actionable fields for an unknown-repo HITL payload (WS1-D): the exact
    registration command to run, the full known-id list, and difflib
    near-miss suggestions — so a typo doesn't require a source-code dig."""
    ids = sorted(known_repo_ids())
    suggestions = difflib.get_close_matches(attempted_id or "", ids, n=3, cutoff=0.6)
    return {
        "remediation": (
            f"hydra repo register {attempted_id} <absolute-path-to-repo> "
            f"[--init] [--force]"
        ),
        "known_ids": ids,
        "suggested_ids": suggestions,
    }


def normalize_repo_subpath(subpath: str) -> str:
    """Validate and normalize a repo-relative target subpath.

    The result always uses forward slashes for stable transport in state/envelopes.
    Absolute paths, drive-qualified paths, and parent traversal are rejected.
    """
    raw = (subpath or "").strip()
    if not raw:
        raise ValueError("repo subpath requires a value")
    if raw.startswith(("/", "\\")) or re.match(r"^[A-Za-z]:", raw):
        raise ValueError(f"repo subpath must be relative, got {subpath!r}")
    if ":" in raw:
        raise ValueError(f"repo subpath must not contain ':'; got {subpath!r}")

    parts = [p for p in re.split(r"[\\/]+", raw) if p and p != "."]
    if not parts:
        raise ValueError("repo subpath requires at least one path segment")
    if any(p == ".." for p in parts):
        raise ValueError(f"repo subpath must not escape via '..': {subpath!r}")
    return "/".join(parts)


def resolve_repo_project_path(repo_id: str, subpath: str | None = None) -> Path:
    """Resolve an allow-listed repo root, optionally narrowed to a safe subpath."""
    root = resolve_repo_path(repo_id)
    if subpath is None:
        return root
    normalized = normalize_repo_subpath(subpath)
    candidate = (root / Path(normalized)).resolve(strict=False)
    if not candidate.is_relative_to(root):
        raise ValueError(
            f"resolved project path {candidate} escapes repo root {root}; "
            f"repo_id={repo_id!r}, subpath={subpath!r} rejected"
        )
    return candidate


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


_SUBDIR_FLAG = r"--(?:subdir|repo-subpath)"
_SUBDIR_ARG_RE = re.compile(
    rf"(?:^|\s)({_SUBDIR_FLAG}(?:=(\S+)|\s+(\S+))?)(?=\s|$)",
    re.IGNORECASE,
)
_SUBDIR_BARE_RE = re.compile(
    rf"(?:^|\s){_SUBDIR_FLAG}(?:\s+--|\s*$)",
    re.IGNORECASE,
)
_SUBDIR_COUNT_RE = re.compile(
    rf"(?:^|\s){_SUBDIR_FLAG}(?:=|\s)",
    re.IGNORECASE,
)


# ---------------------------------------------------------------------------
# Multi-repo CLI argument parser (--repos / --fleet)
# ---------------------------------------------------------------------------

# VALUE PATTERN for --repos / --fleet:
#   Matches a comma-separated list of repo tokens that tolerates optional whitespace
#   around commas but STOPS at a space that is NOT followed by a comma-separated
#   continuation.  Pattern: first token, then zero-or-more ( \s*,\s* next-token ).
#
#   Per-token class: [^\s,]+ — any run of non-whitespace, non-comma characters.
#   This is INTENTIONALLY broad: it captures foreign/malformed tokens (e.g.
#   "foreign/repo", "foo:bar") as a WHOLE token so they reach allow-list validation
#   and raise ValueError cleanly.  Nothing can bleed into the goal text because the
#   only terminators are whitespace (not preceded by a comma) and end-of-string.
#
#   Examples accepted:
#     "a,b,c"          "a, b, c"       "a ,b , c"      "a , b , c"
#   Malformed — captured whole, then rejected by allow-list:
#     "pair-programmer,foreign/repo"  -> ["pair-programmer","foreign/repo"] -> ValueError
#   Stopping correctly:
#     "--repos a, b Fix the bug"  -> value "a, b",  residual "Fix the bug"
#     "--repos a,b Fix"           -> value "a,b",   residual "Fix"
#
# The EQUALS form captures the value in group 2; the SPACE form in group 3.
# Group 1 is the full flag+value token (used for span removal).
_REPOS_VALUE_PAT = r"[^\s,]+(?:\s*,\s*[^\s,]+)*"
_REPOS_ARG_RE = re.compile(
    r"(?:^|(?<=\s))"                                   # start or preceded by whitespace
    r"(--(?:repos|fleet)"                              # flag (group 1 opens)
    r"(?:=\s*(" + _REPOS_VALUE_PAT + r")"             # equals-form: group 2
    r"|\s+(" + _REPOS_VALUE_PAT + r")"                # space-form:  group 3
    r"))",                                             # group 1 closes
    re.IGNORECASE,
)

# Bare "--repos" or "--fleet" with no value following (space-form: end of string
# or immediately followed by another flag/whitespace-only).
_REPOS_BARE_RE = re.compile(
    r"(?:^|\s)--(?:repos|fleet)(?:\s+--|\s*$)",
    re.IGNORECASE,
)

# Count total "--repos" / "--fleet" occurrences (both forms) for duplicate detection.
_REPOS_COUNT_RE = re.compile(
    r"(?:^|\s)--(?:repos|fleet)(?:=|\s)",
    re.IGNORECASE,
)


def parse_repos_arg(text: str) -> tuple[list[str], str]:
    """Extract an optional ``--repos <id,id,...>`` / ``--fleet <id,id,...>`` token.

    Parses a MULTI-repo token from *text*, validates each id against the allow-list,
    deduplicates (first-occurrence order), removes the token from the string, and
    returns ``(list_of_repo_ids, cleaned_text)``.

    Whitespace around commas is tolerated: ``--repos a, b, c`` is equivalent to
    ``--repos a,b,c``.  The value terminates at the first space-separated word that
    is NOT preceded by a comma, so ``--repos a, b Fix the bug`` correctly produces
    ``["a", "b"]`` with ``"Fix the bug"`` left in the cleaned text.

    Supported forms::

        --repos agentsmith,theeights,xenia   Fix fleet
        --repos agentsmith, theeights, xenia Fix fleet   (spaces after commas)
        --repos=agentsmith,theeights          (equals-form)
        --repos=agentsmith, theeights         (equals-form with spaces)
        --fleet agentsmith,theeights          (synonym)
        --fleet=agentsmith,theeights          (synonym equals-form)

    Returns:
        ``([], text)`` unchanged when no ``--repos`` / ``--fleet`` token is present.

        ``(list_of_repo_ids, cleaned_text)`` on success (list has >=1 element).

    Raises:
        ValueError: if the token is present with no value, if any id is unknown,
                    or if ``--repos`` / ``--fleet`` appears more than once.
    """
    # Equals-form with empty value: "--repos=" or "--fleet=" at end / whitespace only.
    if re.search(r"(?:^|\s)--(?:repos|fleet)=\s*(?:\s|$)", text, re.IGNORECASE):
        raise ValueError("--repos/--fleet requires a value")

    # Bare --repos/--fleet with no value (space-form: at end or followed by flag).
    if _REPOS_BARE_RE.search(text):
        raise ValueError("--repos/--fleet requires a value")

    # Also catch bare --repos/--fleet at end of string (trailing whitespace variants).
    stripped = text.strip()
    if re.search(r"(?:^|\s)--(?:repos|fleet)$", stripped, re.IGNORECASE):
        raise ValueError("--repos/--fleet requires a value")

    # Count occurrences for duplicate detection.
    occurrences = len(_REPOS_COUNT_RE.findall(text))
    if occurrences > 1:
        raise ValueError(
            f"--repos/--fleet specified more than once ({occurrences} times); "
            "only a single multi-repo token is supported per invocation"
        )

    m = _REPOS_ARG_RE.search(text)
    if m is None:
        return [], text

    # Group 2 = equals-form value; group 3 = space-form value.
    raw_value = ((m.group(2) or "") + (m.group(3) or "")).strip()
    if not raw_value:
        raise ValueError("--repos/--fleet requires a value")

    # Comma-split on optional surrounding whitespace; trim each part; drop empties;
    # dedup preserving first-occurrence order.
    parts = [p.strip() for p in re.split(r"\s*,\s*", raw_value)]
    parts = [p for p in parts if p]  # drop empties
    if not parts:
        raise ValueError("--repos/--fleet requires at least one non-empty repo id")

    # P3: prose-safe tail check (same rule as parse_repo_arg).
    # Short strings: max(0, len-120)=0 → m.start(1)>=0 always → tail → ValueError.
    # Long strings: only matches in the final 120 chars are explicit; mid-prose
    # --repos with an unknown id raises RepoFlagIgnored instead.
    _repos_text_len = len(text)
    _repos_is_tail = m.start(1) >= max(0, _repos_text_len - 120)

    # Validate each id; collect deduplicated list.
    seen: dict[str, bool] = {}
    deduped: list[str] = []
    for raw_id in parts:
        normalised = raw_id.lower()
        if normalised in seen:
            continue  # dedup: keep first occurrence
        seen[normalised] = True
        if not is_known_repo(raw_id):
            if not _repos_is_tail:
                raise RepoFlagIgnored(raw_id)
            raise ValueError(
                f"--repos/--fleet: {raw_id!r} is not an allow-listed repo_id; "
                f"known: {sorted(_REPO_DIRNAMES)}"
            )
        deduped.append(normalised)

    # Remove the entire matched token (group 1) from text.  m.start(1)/m.end(1)
    # covers only the flag+value, not any leading whitespace consumed by the
    # lookbehind — so we slice on the group-1 span and clean surrounding space.
    token_start = m.start(1)
    token_end = m.end(1)
    cleaned = text[:token_start].rstrip() + " " + text[token_end:].lstrip()
    cleaned = cleaned.strip()
    cleaned = re.sub(r"  +", " ", cleaned)
    return deduped, cleaned


def parse_repo_subpath_arg(text: str) -> tuple[Optional[str], str]:
    """Extract ``--subdir <path>`` / ``--repo-subpath <path>`` from *text*."""
    if re.search(rf"(?:^|\s){_SUBDIR_FLAG}=\s*(?:\s|$)", text, re.IGNORECASE):
        raise ValueError("--subdir/--repo-subpath requires a value")
    if _SUBDIR_BARE_RE.search(text):
        raise ValueError("--subdir/--repo-subpath requires a value")
    stripped = text.strip()
    if re.search(rf"(?:^|\s){_SUBDIR_FLAG}$", stripped, re.IGNORECASE):
        raise ValueError("--subdir/--repo-subpath requires a value")

    occurrences = len(_SUBDIR_COUNT_RE.findall(text))
    if occurrences > 1:
        raise ValueError(
            f"--subdir/--repo-subpath specified more than once ({occurrences} times); "
            "only a single repo subpath is supported per invocation"
        )

    m = _SUBDIR_ARG_RE.search(text)
    if m is None:
        return None, text

    raw = ((m.group(2) or "") + (m.group(3) or "")).strip()
    normalized = normalize_repo_subpath(raw)

    token_start = m.start(1)
    token_end = m.end(1)
    cleaned = text[:token_start].rstrip() + " " + text[token_end:].lstrip()
    cleaned = re.sub(r"  +", " ", cleaned.strip())
    return normalized, cleaned


class RepoFlagIgnored(ValueError):
    """Raised when ``--repo <token>`` looks like prose rather than an explicit CLI flag.

    The intake node catches this (before the generic ``ValueError`` handler) to emit
    an ``intake.repo_flag_like_token_ignored`` trace and continue without HITL —
    the user described the flag conceptually rather than intending to target a repo.

    Attributes:
        token: The unrecognised id token that triggered the ignore decision.
    """

    def __init__(self, token: str) -> None:
        self.token = token
        super().__init__(
            f"--repo {token!r} looks like prose (not an explicit flag); ignored"
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

    # RA-10: count only "explicit" --repo occurrences (those that survive the
    # prose-safe rule) toward the duplicate check. A match where the id is NOT
    # known AND the match is mid-prose (not in the final 120 chars) raises
    # RepoFlagIgnored below — counting it here fires "specified more than once"
    # before the prose-safe filter runs, aborting goals like
    # "improve --repo flags and documentation --repo hydra".
    # Explicit = tail-position (m.start >= max(0, len-120)) OR known repo id.
    _ra10_all_ms = list(_REPO_ARG_RE.finditer(text))
    _ra10_tail_thresh = max(0, len(text) - 120)
    _ra10_explicit = sum(
        1
        for _rm in _ra10_all_ms
        if _rm.start() >= _ra10_tail_thresh
        or is_known_repo(((_rm.group(2) or "") + (_rm.group(3) or "")).strip())
    )
    if _ra10_explicit > 1:
        raise ValueError(
            f"--repo specified more than once ({_ra10_explicit} times); "
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
        # P3: prose-safe flag detection.
        # A match is EXPLICIT (raises ValueError / surfaces HITL) when the match
        # starts within the last 120 chars of the goal: max(0, len-120).
        # Short strings (≤120 chars): max(0, len-120)=0, so m.start()>=0 is always
        # True → all short-string unknown ids raise ValueError (typo protection).
        # Long strings: only matches in the FINAL 120 chars are explicit; a match
        # deep in the middle (>120 chars from the end) raises RepoFlagIgnored so
        # prose descriptions of --repo never surface unexpected HITL.
        # Known ids remain explicit regardless of position (checked above).
        _text_len = len(text)
        _is_tail = m.start() >= max(0, _text_len - 120)
        if not _is_tail:
            raise RepoFlagIgnored(raw_id)
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
