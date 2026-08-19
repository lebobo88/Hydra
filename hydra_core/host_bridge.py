"""Attended (host-bridged) engineering execution.

This is the core of the *attended* execution mode: instead of the headless
``_drive_pp_stage_loop`` driving generate -> judge -> finalize in a detached
subprocess the operator cannot watch, the Claude Code host session drives the
SAME pair-programmer stage protocol IN-CONTEXT, surfacing the generate and judge
steps as visible ``Agent`` subagents it can follow along with.

Design note (supersedes the ``run_host_agent`` trampoline framing in the plan):
``_drive_pp_stage_loop`` is a straight-line function with a broad fail-soft
``except`` that would swallow any mid-loop pause exception (and finalize the run
``aborted``). So we do NOT reuse it. Instead this module is an **explicit
step-state-machine** that persists its progress (the "cursor") to disk between
host round-trips — exactly the resumable plumbing codex's review called for, and
replay-free: each ``step``/``submit`` advances the cursor by exactly one
transition, so every pp ledger call (``record_attempt``/``record_verdict``/
``finalize_*``) happens exactly once.

It calls ONLY tools the engineering squad declares (RBAC-safe via
``squad_id="engineering"``) and reuses the headless loop's governance helpers
(``_build_engineer_prompt``, ``_run_smoke``, ``gate_eligible_judges`` routing,
the finalize-readiness gate, the real-diff judge text) so the attended path and
the headless path enforce the same gates. Budget is NOT charged here — the
caller (the ``hydra step`` / ``submit-host-result`` CLI operating on the
checkpointed ``HydraState``) charges the returned ``cost_usd`` via
``charge_and_gate`` so the 80%/100% tripwires stay authoritative.

Runtime-agnostic: no provider SDK imports. The visible ``engineer`` / judge
subagents are spawned by the host (Claude Code), not here; this module only
sequences the deterministic pp tool calls around them.
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
from pathlib import Path
from typing import Any, Optional

import re as _re

from . import telemetry as _telemetry
from .squad_node import (
    Dispatcher,
    _augment_with_critique,
    _build_engineer_prompt,
    _generate_failure_reason,
    _judge_artifact_text,
    _pp_gate_type,
    _pp_inner,
    _pp_ok,
    _rubric_md,
    _run_smoke,
    _worktree_dirty_set,
)

# Cursor schema version — bump on any incompatible shape change so a stale
# on-disk cursor from an older build is rejected loudly rather than misread.
CURSOR_SCHEMA = 1

# Terminal cursor states.
_TERMINAL = {"complete", "surfaced", "aborted"}

_SQ = "engineering"

# --------------------------------------------------------------------------- #
# LV-1: error-payload detection                                               #
# --------------------------------------------------------------------------- #

# Known-good statuses from MCPStdioDispatcher.call_mcp; anything else with an
# "error" key is treated as a failure too.
_CALL_MCP_SUCCESS_STATUSES: frozenset[str] = frozenset(
    {"done", "ok", "complete", "stub", "skipped"}
)


class PPLedgerError(RuntimeError):
    """A pp ledger call failed with a structured error payload.

    ``payload`` carries the original ``call_mcp`` response dict so
    ``_classify_infra_failure`` can key off the STRUCTURE of the rejection
    (``status``, ``gate_error``, ``hitl_required``, ``venom_refused``)
    rather than substring-matching the rendered message. A fail-CLOSED
    rejection whose ``{exc}`` text happens to mention a transport-sounding
    phrase (e.g. "database is locked") must still classify as deterministic
    -- see the venom gate's fail-closed branch in dispatcher.py.
    """

    def __init__(self, message: str, payload: dict[str, Any]):
        super().__init__(message)
        self.payload = payload


def _raise_on_error_payload(resp: Any, tool: str) -> Any:
    """Raise PPLedgerError when a call_mcp response is a structured error dict.

    MCPStdioDispatcher.call_mcp returns error DICTS instead of raising for:
    - RBAC rejections:   {"status":"rejected","error":...}
    - transport/timeout: {"status":"failed","error":...}
    - isError results:   {"status":"failed", "tool":..., "error":...}

    The existing try/except downgrade paths in begin_stage, _apply_generate,
    _apply_judge, and _finalize only catch *raised* exceptions, so they silently
    passed through error dicts, which broke finalize/verdict/attempt tracking.

    Raises PPLedgerError (a RuntimeError subclass carrying the original dict
    as ``.payload`` for structural classification downstream) on:
    - ``status`` in {"rejected","failed","error"}, or
    - ``"error"`` key present and ``status`` not in the known-good set.

    Returns ``resp`` unchanged on a normal response or on a non-dict (callers
    must tolerate both — no change to existing semantics).
    """
    if not isinstance(resp, dict):
        return resp
    status = resp.get("status")
    if status in {"rejected", "failed", "error"}:
        raise PPLedgerError(
            f"pp ledger call {tool!r} returned error payload "
            f"(status={status!r}): {resp.get('error', resp)!r}",
            resp,
        )
    if status not in _CALL_MCP_SUCCESS_STATUSES and "error" in resp:
        raise PPLedgerError(
            f"pp ledger call {tool!r} returned error (status={status!r}): "
            f"{resp['error']!r}",
            resp,
        )
    return resp


# W2-3: markers that positively identify a transport-shaped pp ledger failure
# (timeout, connection drop, lock contention, cold-start race) as opposed to a
# deterministic pp rejection (bad args, schema violation, business-rule
# denial). These are a FALLBACK for exceptions that carry no structured
# payload (see PPLedgerError.payload below, checked first) -- e.g. a raw
# transport exception raised before a call_mcp response dict ever formed.
# Deterministic markers are checked FIRST and win even when a transport word
# also appears in the message, because a rejection's own validation text can
# legitimately contain a word like "connection" — e.g. "connection_id
# invalid". A message that matches neither list is treated as deterministic:
# getting this discrimination wrong in the permissive direction would hide a
# real rejection, so an ambiguous failure must fail the stage rather than
# silently hold it open.
_DETERMINISTIC_FAILURE_MARKERS: tuple[str, ...] = (
    "validation", "invalid_", "schema", "attempt not found",
    "attempt_id not found", "unknown attempt", "rubric not found",
    "duplicate", "already recorded", "not authorized", "rbac",
)
_TRANSPORT_FAILURE_MARKERS: tuple[str, ...] = (
    "timed out", "timeout", "'phase': 'call_tool'", '"phase": "call_tool"',
    "connection", "brokenpipe", "not registered in backends.json",
    "mcp sdk not installed", "sqlite_busy", "database is locked",
    "busy_timeout", "call_tool raised after connect", "econnreset",
    "epipe", "socket", "server not configured",
)
# Structured payload keys that ALWAYS mean "deterministic", regardless of
# what the rendered message text says. A rejection dict (status=="rejected")
# is a positive business/governance decision -- RBAC denial, or the Cerberus
# venom gate's REFUSED / requires_human / fail-CLOSED-internal-error branches
# (dispatcher.py._venom_gate) -- never a retryable transport blip, even when
# the wrapped inner exception's text happens to contain a transport-sounding
# phrase (e.g. a venom gate fail-closed on a locked episodic audit store:
# "venom gate internal error: database is locked"). Only {"status":"failed"}
# is ambiguous enough to fall through to the text markers above.
_DETERMINISTIC_PAYLOAD_KEYS: tuple[str, ...] = (
    "gate_error", "hitl_required", "venom_refused",
)


def _classify_infra_failure(exc: Exception | None) -> str:
    """Classify a pp ledger call failure as "transport" or "deterministic".

    Structural check FIRST: if ``exc`` is a ``PPLedgerError`` carrying the
    original call_mcp response dict, a ``status == "rejected"`` payload (or
    any of ``_DETERMINISTIC_PAYLOAD_KEYS`` present and truthy) is always
    "deterministic" -- no message text is consulted. This is what keeps a
    fail-closed venom-gate rejection from being misclassified as "transport"
    just because its wrapped exception text contains a phrase like "database
    is locked". Only when no such structure is available (a raw exception
    that never became a call_mcp response dict) do we fall back to the
    marker-based text match below.

    Returns "transport" only when the exception text matches a known-good
    transport signal and no deterministic-rejection signal. Any other case --
    including ``exc is None`` -- returns "deterministic" so an unrecognized
    failure shape still fails the stage instead of masking a real rejection.
    """
    if exc is None:
        return "deterministic"
    payload = getattr(exc, "payload", None)
    if isinstance(payload, dict):
        if payload.get("status") == "rejected":
            return "deterministic"
        if any(payload.get(k) for k in _DETERMINISTIC_PAYLOAD_KEYS):
            return "deterministic"
    msg = str(exc).lower()
    if any(m in msg for m in _DETERMINISTIC_FAILURE_MARKERS):
        return "deterministic"
    if any(m in msg for m in _TRANSPORT_FAILURE_MARKERS):
        return "transport"
    return "deterministic"


# --------------------------------------------------------------------------- #
# Worktree isolation (write-safety)                                           #
# --------------------------------------------------------------------------- #
# In attended mode the host's visible `engineer` subagent writes code. The
# `hydra-block-direct-write` hook blocks engine-source writes unless the path
# resolves under the project root's worktree root (or HYDRA_PP_STAGE_ACTIVE=1,
# which we must NOT set session-wide). So we isolate the engineer into a linked
# git worktree under `resolve_worktree_root()` (default
# `<AIAPP_BASE>/.hydra-worktrees/<repo_id>/`, overridable via
# `HYDRA_WORKTREE_ROOT`, NOT inside the target repo) — already hook-allowed —
# and merge the result back into the repo on a passing finalize. Keeping the
# worktree outside repo_root avoids two real incidents: a test runner globbing
# `.harness/worktrees/**/tests` alongside the real `tests/` dir, and untracked
# `.hydra/`/`.harness/` state nested inside the target repo aborting a merge.
# This keeps HYDRA_ENFORCE_ROUTING fully on and the host session unable to
# hand-write project source. Fail-soft: if the repo isn't git or worktree
# provisioning fails, fall back to in-place writes.

def _git_timeout_s() -> int:
    """Return the git subprocess timeout in seconds from ``HYDRA_GIT_TIMEOUT_S``
    (default 60). Env: HYDRA_GIT_TIMEOUT_S.

    Fail-soft: any non-integer, missing, or non-positive value returns 60.
    """
    raw = os.environ.get("HYDRA_GIT_TIMEOUT_S")
    try:
        v = int(raw) if raw else 60
    except (TypeError, ValueError):
        v = 60
    return v if v > 0 else 60


def _baseline_timeout_s() -> int:
    """Return the baseline-smoke test-suite timeout in seconds from
    ``HYDRA_BASELINE_TIMEOUT_S`` (default 600). Env: HYDRA_BASELINE_TIMEOUT_S.

    Used for both the pre-change baseline run and the per-failure rerun.
    Raised from the old hardcoded 240 s to give slow suites more headroom.

    Fail-soft: any non-integer, missing, or non-positive value returns 600.
    """
    raw = os.environ.get("HYDRA_BASELINE_TIMEOUT_S")
    try:
        v = int(raw) if raw else 600
    except (TypeError, ValueError):
        v = 600
    return v if v > 0 else 600


def _git(args: list[str], cwd: str | Path, timeout: int | None = None) -> subprocess.CompletedProcess:
    if timeout is None:
        timeout = _git_timeout_s()
    return subprocess.run(["git", *args], cwd=str(cwd), capture_output=True,
                          text=True, timeout=timeout, check=False)


def _git_repo_root(path: str | Path) -> str | None:
    try:
        res = _git(["rev-parse", "--show-toplevel"], path)
    except Exception:  # noqa: BLE001
        return None
    return res.stdout.strip() if res.returncode == 0 else None


def _worktree_repo_id(repo_root: str) -> str:
    """Stable per-repo directory-name id derived from ``repo_root``.

    Used to namespace the shared worktree root so attended worktrees for
    different repos never collide when they share ``HYDRA_WORKTREE_ROOT`` /
    ``AIAPP_BASE``. Sanitized the same way ``_provision_worktree`` sanitizes
    ``run_id`` — alnum/-/_ only, never empty.
    """
    name = Path(repo_root).resolve().name if repo_root else ""
    safe = "".join(c for c in name if c.isalnum() or c in "-_")
    return safe or "repo"


def resolve_worktree_root(repo_root: str) -> Path:
    """Resolve the directory under which attended worktrees for ``repo_root``
    are provisioned, namespaced per-repo as ``<root>/<repo_id>``.

    Resolution order (mirrors the ecosystem ``AIAPP_BASE`` convention
    documented in ``docs/PORTABILITY.md``):

      1. ``HYDRA_WORKTREE_ROOT`` env var — explicit override, honored as-is.
      2. ``AIAPP_BASE`` env var — ``<AIAPP_BASE>/.hydra-worktrees``.
      3. Sibling fallback — ``<parent of repo_root>/.hydra-worktrees``. This
         mirrors the convention's own fallback (repo_root is normally a
         direct child of the shared base directory), so a bare checkout with
         no env configured still gets a real sibling location rather than an
         error.

    Moved OUT of ``<repo_root>/.harness/worktrees`` (2026-08): a test runner
    that globs the repo tree from repo_root would otherwise pick up every
    attended stage's worktree copy of ``tests/`` alongside the real one
    (a greenfield project reported 62 tests where 31 existed), and untracked
    ``.hydra/``/``.harness/`` state nested inside the target repo caused a
    real merge abort. The write-block hooks' allow-list is anchored to this
    same env var (see ``plugins/hydra/hooks/hydra-block-direct-write.ps1``),
    so relocating here and there must be kept in lockstep.
    """
    env_override = os.environ.get("HYDRA_WORKTREE_ROOT")
    if env_override:
        base = Path(env_override)
    else:
        aiapp_base = os.environ.get("AIAPP_BASE")
        if aiapp_base:
            base = Path(aiapp_base) / ".hydra-worktrees"
        else:
            base = Path(repo_root).resolve().parent / ".hydra-worktrees"
    return base / _worktree_repo_id(repo_root)


def _provision_worktree(repo_root: str, run_id: str) -> tuple[str, str] | None:
    """Create a linked worktree + branch off HEAD for an attended stage.

    Returns ``(worktree_path, branch)`` or None on any failure (caller falls
    back to in-place). The worktree lives under ``resolve_worktree_root()``
    (default ``<AIAPP_BASE>/.hydra-worktrees/<repo_id>/``, overridable via
    ``HYDRA_WORKTREE_ROOT``) — NOT inside ``repo_root`` — which the write-block
    hook's allow-list resolves the same way so the engineer's writes there
    stay hook-permitted.
    """
    safe = "".join(c for c in str(run_id) if c.isalnum() or c in "-_") or "run"
    branch = f"attended/{safe}"
    wt = resolve_worktree_root(repo_root) / f"attended-{safe}"
    try:
        wt.parent.mkdir(parents=True, exist_ok=True)
        if wt.exists():
            _git(["worktree", "remove", "--force", str(wt)], repo_root)
        # -B resets the branch if a stale one exists from a prior aborted run.
        res = _git(["worktree", "add", "-B", branch, str(wt), "HEAD"], repo_root)
        if res.returncode != 0:
            return None
    except Exception:  # noqa: BLE001
        return None
    return str(wt), branch


_BYPRODUCT_PATTERNS: list[str] = [
    "__pycache__/",
    ".pytest_cache/",
    "*.pyc",
    "node_modules/",
    ".tmp*/",
    ".hydra/",
    ".harness/",
    "*.log",
]


def _write_worktree_gitexcludes(worktree_path: str) -> None:
    """Append byproduct patterns to the worktree-local git exclude file.

    For linked worktrees the ``.git`` entry is a text file pointing at the
    private gitdir; ``git rev-parse --git-path info/exclude`` resolves the
    correct exclude file path regardless of worktree type.  Idempotent — patterns
    already present in the file are not duplicated.  Fail-soft on any error.
    """
    try:
        r = subprocess.run(
            ["git", "-C", worktree_path, "rev-parse", "--git-path", "info/exclude"],
            capture_output=True, text=True, timeout=5, check=False,
        )
        if r.returncode != 0 or not r.stdout.strip():
            return
        raw = r.stdout.strip()
        excl = Path(raw) if Path(raw).is_absolute() else Path(worktree_path) / raw
        excl.parent.mkdir(parents=True, exist_ok=True)
        existing = excl.read_text(encoding="utf-8") if excl.exists() else ""
        to_add = [p for p in _BYPRODUCT_PATTERNS if p not in existing]
        if to_add:
            with excl.open("a", encoding="utf-8") as _f:
                _f.write("\n".join(to_add) + "\n")
    except Exception:  # noqa: BLE001 — fail-soft
        pass


def _merge_worktree_back(repo_root: str, worktree_path: str, branch: str) -> dict[str, Any]:
    """Commit the engineer's changes in the worktree and merge them into the
    repo's checked-out branch. Returns a status dict; never raises."""
    out: dict[str, Any] = {"merged": False, "sha": None, "error": None}
    try:
        # Stage + commit any uncommitted work the engineer left in the worktree.
        _write_worktree_gitexcludes(worktree_path)
        st = _git(["status", "--porcelain"], worktree_path)
        if st.stdout.strip():
            _git(["add", "-A"], worktree_path)
            _git(["commit", "-m", f"attended engineering ({branch})",
                  "--no-verify"], worktree_path)
        head = _git(["rev-parse", "HEAD"], worktree_path)
        wt_sha = head.stdout.strip()
        base = _git(["rev-parse", "HEAD"], repo_root).stdout.strip()
        if wt_sha == base:
            out["error"] = "no_changes_to_merge"
            return out
        # Fast-forward / merge the branch into the repo's current branch.
        mres = _git(["merge", "--no-ff", "--no-edit", branch], repo_root)
        if mres.returncode != 0:
            # Abort a conflicted merge so the repo is left clean for the operator.
            _git(["merge", "--abort"], repo_root)
            out["error"] = f"merge_failed: {(mres.stderr or mres.stdout).strip()[:300]}"
            return out
        out["merged"] = True
        out["sha"] = _git(["rev-parse", "HEAD"], repo_root).stdout.strip()
    except Exception as e:  # noqa: BLE001
        out["error"] = f"merge_exception: {e!r}"[:300]
    return out


def _merge_branch_back(repo_root: str, branch: str) -> dict[str, Any]:
    """W2-4: merge a ``preserved_branch`` into the repo's checked-out branch
    WITHOUT a live worktree.

    Used only by the recovery path (`recover_stalled_stage`): the worktree
    that hosted ``branch`` was already removed by ``_finalize``, but
    ``_preserve_non_complete_work`` committed every uncommitted engineer
    change to the branch before that removal, so the branch itself still
    carries the full change set. Unlike ``_merge_worktree_back`` this never
    touches ``worktree_path`` (there isn't one) — it only reads/merges the
    already-committed branch.

    Two DISTINCT no-op shapes exist and must not be collapsed into one
    marker: ``branch_sha == base`` means the branch's tip IS the current
    HEAD (nothing to merge, the branch and HEAD are literally the same
    commit) -- reported as ``no_changes_to_merge``. But a branch that was
    already merged EARLIER (its tip is an ancestor of HEAD, not equal to
    it) does NOT hit that check: ``git merge --no-ff --no-edit <branch>``
    for an already-merged branch prints "Already up to date.", exits 0,
    and creates NO commit -- yet a naive caller that then does
    ``rev-parse HEAD`` unconditionally would report a fabricated "merged"
    sha (actually whatever unrelated commit HEAD already pointed to).  Do
    NOT parse git's "Already up to date." text to detect this -- that
    string is localizable and version-dependent. The only authoritative
    check is comparing commit ids: capture HEAD before the merge attempt
    and compare to HEAD after. If HEAD did not move, git created no
    commit and nothing was merged, reported as ``already_merged`` (with no
    sha) -- a genuinely different situation from ``no_changes_to_merge``
    (branch has nothing new) worth keeping distinguishable for the
    operator. ``out["base"]`` records the pre-merge HEAD whenever a merge
    commit IS created, so ``_revert_merge_commit`` can verify it is
    reverting a commit this call actually produced (mainline parent ==
    base) rather than trusting the reported sha blindly. Never raises."""
    out: dict[str, Any] = {"merged": False, "sha": None, "base": None, "error": None}
    try:
        chk = _git(["rev-parse", "--verify", branch], repo_root)
        if chk.returncode != 0:
            out["error"] = f"branch_not_found: {branch}"
            return out
        branch_sha = chk.stdout.strip()
        pre_head = _git(["rev-parse", "HEAD"], repo_root).stdout.strip()
        if branch_sha == pre_head:
            out["error"] = "no_changes_to_merge"
            return out
        mres = _git(["merge", "--no-ff", "--no-edit", branch], repo_root)
        if mres.returncode != 0:
            _git(["merge", "--abort"], repo_root)
            out["error"] = f"merge_failed: {(mres.stderr or mres.stdout).strip()[:300]}"
            return out
        post_head = _git(["rev-parse", "HEAD"], repo_root).stdout.strip()
        if post_head == pre_head:
            # git exited 0 but HEAD never moved -- "Already up to date.":
            # branch_sha is an ancestor of pre_head, not equal to it, so the
            # fast path above missed it. No commit was created; report the
            # truth instead of a phantom "merged" sha.
            out["error"] = "already_merged"
            return out
        out["merged"] = True
        out["sha"] = post_head
        out["base"] = pre_head
    except Exception as e:  # noqa: BLE001
        out["error"] = f"merge_exception: {e!r}"[:300]
    return out


def _revert_sequencer_git_dir(repo_root: str) -> Path | None:
    """Resolve repo_root's REAL git dir via ``git rev-parse --git-dir``,
    never by assuming ``<repo_root>/.git`` -- that assumption is wrong for a
    linked worktree, where the git dir lives under the main repo's
    ``.git/worktrees/<name>`` instead. Returns None if it cannot be
    resolved (never raises)."""
    try:
        res = _git(["rev-parse", "--git-dir"], repo_root)
        if res.returncode != 0:
            return None
        gd = res.stdout.strip()
        if not gd:
            return None
        path = Path(gd)
        return path if path.is_absolute() else Path(repo_root) / path
    except Exception:  # noqa: BLE001
        return None


def _revert_sequencer_state(repo_root: str) -> str:
    """Inspect repo_root's real git dir for REVERT sequencer state
    (``REVERT_HEAD`` / ``sequencer/todo``). This function is only ever
    called from ``_revert_merge_commit`` immediately after that same
    function's own ``git revert``, so it never needs to distinguish a
    revert sequencer from a cherry-pick one -- it does not check
    ``CHERRY_PICK_HEAD`` and must not claim to. This is the authority on
    whether an abort "worked" -- NOT the abort command's own exit code,
    which is nonzero for two unrelated reasons that must not be conflated:
    (1) a real sequencer was active and the abort itself failed to tear it
    down (state remains, genuinely bad), vs (2) the preceding revert never
    got far enough to create a sequencer at all (e.g. refused upfront over
    a dirty file), so "abort" has nothing to abort and correctly errors
    even though the repo was already clean. Checking exit code alone would
    misreport case (2) as a failed abort.

    Returns one of three states, never just a bool -- collapsing "clean" and
    "could not tell" into one falsy value is exactly the failure shape this
    workstream exists to eliminate (a record claiming a state that was never
    actually verified):

      "active"  -- REVERT_HEAD or sequencer/todo genuinely present on disk.
      "clean"   -- the git dir resolved and neither marker is present.
      "unknown" -- the git dir itself could not be resolved (repo_root
                   inaccessible, git errored, etc). This is NOT the same
                   fact as "clean" -- it means the state could not be
                   inspected at all, and callers must treat it as at least
                   as bad as "active" (fail toward "go look"), never as an
                   assurance of cleanliness.

    Never raises."""
    git_dir = _revert_sequencer_git_dir(repo_root)
    if git_dir is None:
        return "unknown"
    try:
        active = (git_dir / "REVERT_HEAD").exists() or (git_dir / "sequencer" / "todo").exists()
    except Exception:  # noqa: BLE001
        return "unknown"
    return "active" if active else "clean"


def _revert_merge_commit(
    repo_root: str, merge_sha: str, *, expected_base: str,
) -> dict[str, Any]:
    """Undo a merge commit this recovery itself just created, via ``git
    revert`` rather than ``git reset --hard`` -- it adds a new commit instead
    of rewriting history, so it never rewrites or discards any pre-existing
    commit. That guarantee is about commits, not about working-tree/index
    cleanliness: if the abort step below fails to actually clear a real
    sequencer, this leaves the repo sitting mid-revert (conflicted index /
    half-applied working tree), which ``out["error"]`` reports rather than
    hides.

    Provenance guard (the safety invariant this function exists to hold):
    ``HEAD == merge_sha`` alone is NOT sufficient proof that ``merge_sha``
    is a commit this recovery created. It is trivially satisfied by ANY
    commit that happens to be HEAD -- including an unrelated commit an
    operator or another workflow made, if a caller passes a stale/wrong sha
    while HEAD genuinely equals it. That gap is exactly how the live
    incident happened: a caller reported a fabricated "merged" sha for a
    merge that never occurred (see ``_merge_branch_back``'s ``already_merged``
    fix), HEAD legitimately equalled that sha (it was some earlier, real
    commit), and this function faithfully reverted it. When the caller
    passes ``expected_base`` (the HEAD it observed immediately BEFORE
    invoking the merge that supposedly produced ``merge_sha``), this
    function additionally requires ``merge_sha`` to be an actual merge
    commit (>1 parent) whose FIRST (mainline) parent is exactly
    ``expected_base``. A merge commit's first parent is definitionally
    "what HEAD was before this merge ran" -- so this checks the one fact
    that proves the commit was produced by merging INTO ``expected_base``,
    not merely that it happens to be checked out right now. ``expected_base``
    is a required keyword-only argument -- there is no way to call this
    function without supplying it, so the provenance guard can never be
    silently skipped by a caller that simply omits an argument. Every
    in-tree caller supplies it.

    Whether the abort "worked" is judged by the real post-abort repo state
    (``_revert_sequencer_state``), not by the abort command's exit code --
    ``git revert --abort`` legitimately exits nonzero when the preceding
    revert refused before ever starting a sequencer (e.g. a dirty file in
    the way), and that is NOT a failed cleanup, just nothing to clean up.
    ``out["abort_state"]`` carries that check's own three-way result
    (``"active"`` / ``"clean"`` / ``"unknown"``); ``out["abort_failed"]`` is
    True for BOTH ``"active"`` (sequencer genuinely still present) and
    ``"unknown"`` (the git dir couldn't be resolved, so cleanliness could
    not be verified) -- an uninspectable repo is never reported as clean,
    only as a distinctly-worded, equally unmissable warning.

    The "a non-passing recovery must not retain the merged code" property is
    conditional, not unconditional: it holds only when ``HEAD`` still equals
    ``merge_sha`` at entry. If HEAD has moved (something else touched
    repo_root since), the revert is skipped entirely and the merged code
    remains in the repo -- only an error is recorded, nothing is forced
    through.

    Handles both a real merge commit (2 parents, needs ``-m 1`` to pick the
    mainline parent) and a plain single-parent commit defensively, in case a
    future caller passes a fast-forwarded SHA. Never raises."""
    out: dict[str, Any] = {
        "reverted": False, "sha": None, "error": None, "abort_failed": False,
        "abort_state": None,
    }
    try:
        head = _git(["rev-parse", "HEAD"], repo_root).stdout.strip()
        if head != merge_sha:
            out["error"] = (
                f"revert_skipped_head_moved: HEAD={head} expected={merge_sha}")
            return out
        parents = _git(
            ["rev-list", "--parents", "-n", "1", merge_sha], repo_root,
        ).stdout.split()
        is_merge_commit = len(parents) > 2  # [commit, parent1, parent2, ...]
        mainline_parent = parents[1] if len(parents) > 1 else None
        if not is_merge_commit or mainline_parent != expected_base:
            out["error"] = (
                f"revert_refused_provenance_mismatch: merge_sha={merge_sha} "
                f"is_merge_commit={is_merge_commit} mainline_parent="
                f"{mainline_parent!r} expected_base={expected_base!r} -- "
                "this commit's mainline parent does not match the base "
                "this recovery observed before merging, so it cannot be "
                "proven this recovery created it. Refusing to revert; "
                "repo_root is left untouched."
            )
            return out
        revert_cmd = ["revert", "--no-edit"]
        if is_merge_commit:
            revert_cmd += ["-m", "1"]
        revert_cmd.append(merge_sha)
        rres = _git(revert_cmd, repo_root)
        if rres.returncode != 0:
            revert_err = (rres.stderr or rres.stdout).strip()[:300]
            ares = _git(["revert", "--abort"], repo_root)
            seq_state = _revert_sequencer_state(repo_root)
            out["abort_state"] = seq_state
            if seq_state == "active":
                abort_err = (ares.stderr or ares.stdout).strip()[:300]
                out["abort_failed"] = True
                out["error"] = (
                    f"revert_failed: {revert_err}; abort_failed: sequencer "
                    f"state still present after abort (abort rc="
                    f"{ares.returncode}: {abort_err}) -- repo_root is left "
                    "mid-revert (conflicted index / half-applied working "
                    "tree), not cleanly restored; operator must inspect "
                    "repo_root's full state before any retry."
                )
            elif seq_state == "unknown":
                # Fail TOWARD abort_failed here, not away from it: the git
                # dir could not be resolved, so whether a sequencer remains
                # is genuinely unverified -- reporting "clean" would convert
                # ignorance into a false assurance. This is a DISTINCT fact
                # from "found dirty" (seq_state == "active"), so it gets its
                # own marker rather than being folded into the same text.
                abort_err = (ares.stderr or ares.stdout).strip()[:300]
                out["abort_failed"] = True
                out["error"] = (
                    f"revert_failed: {revert_err}; abort_state_unknown: "
                    "could not resolve repo_root's git dir to verify whether "
                    f"a sequencer remains after the abort (abort rc="
                    f"{ares.returncode}: {abort_err}) -- this is NOT a "
                    "confirmation that repo_root is clean, only an inability "
                    "to check; operator must inspect repo_root's full state "
                    "before any retry."
                )
            else:
                # abort's own exit code is irrelevant here: either it
                # cleanly tore down a real sequencer, or there was never one
                # to begin with (revert refused upfront) -- both are
                # verified clean, which is all that matters.
                out["error"] = f"revert_failed: {revert_err}"
            return out
        out["reverted"] = True
        out["sha"] = _git(["rev-parse", "HEAD"], repo_root).stdout.strip()
    except Exception as e:  # noqa: BLE001
        out["error"] = f"revert_exception: {e!r}"[:300]
    return out


def _remove_worktree(repo_root: str, worktree_path: str) -> dict[str, Any]:
    """Remove a worktree checkout and report whether it actually happened.

    Best-effort by design (callers here are cleanup paths, not a place to
    raise), but "best-effort" must not mean "silently untruthful": the
    returned dict tells the caller what really occurred rather than being a
    fire-and-forget ``None``. Checks the git ``CompletedProcess`` returncode
    (a nonzero exit does NOT raise -- `_git` doesn't check=True) AND verifies
    the path is genuinely gone afterwards, rather than trusting either
    signal alone. That before/after observation is the same discipline that
    caught the merge-back no-op elsewhere in this module, where trusting
    git's exit code was precisely the error.

    The returned verdict is deliberately a *disk* verdict, not a *git
    bookkeeping* verdict: ``removed=True`` means "the checkout directory is
    confirmed gone", which is what the operator's original concern (worktree
    disk occupancy) actually asks. It does NOT by itself mean git's own
    ``.git/worktrees/<name>`` registration is deregistered -- on Windows an
    AV scanner or an open handle can let the directory removal race ahead of
    (or independently of) git's admin-dir cleanup, leaving `git worktree
    list` still reporting a path whose directory is already gone. When the
    directory is confirmed gone, this function best-effort re-checks that
    registration and, if it finds this worktree's entry still stale-listed,
    deregisters ONLY this worktree's own ``.git/worktrees/<name>`` admin
    directory (see ``_prune_single_worktree_admin_dir``) -- deliberately NOT
    ``git worktree prune``, which is repo-wide and takes effect immediately
    with no grace period, and would deregister every missing worktree
    registration in the repository, not just this one. Attended stages can
    run concurrently; a repo-wide prune fired from one stage's cleanup could
    deregister another stage's live worktree if its directory read as
    transiently absent (slow filesystem, network mount, mid-write). This
    follow-up is advisory only and never flips the disk-based verdict already
    decided, since it doesn't change whether the disk space was reclaimed.

    Returns ``{"removed": bool, "error": str | None}``. Never raises.
    """
    err: str | None = None
    try:
        res = _git(["worktree", "remove", "--force", worktree_path], repo_root)
        if res.returncode != 0:
            err = (res.stderr or res.stdout or "").strip()[:300]
    except Exception as exc:  # noqa: BLE001
        err = f"exception: {exc!r}"[:300]
    try:
        still_present = Path(worktree_path).exists()
    except Exception:  # noqa: BLE001 — treat an unverifiable path as "not proven gone"
        still_present = True
    if still_present:
        return {"removed": False, "error": err or "git reported success but path still exists"}
    # Path is genuinely gone -- that's a real removal even if git also
    # reported a (now-moot) error, e.g. a racing caller removed it first.
    # Best-effort: if git's own worktree list still shows the (now-gone)
    # path registered -- a stale admin dir -- deregister ONLY this
    # worktree's own admin directory (never a repo-wide `worktree prune`;
    # see docstring). Failure here is swallowed on purpose: this is advisory
    # cleanup, not part of the disk-based verdict.
    try:
        listing = _git(["worktree", "list", "--porcelain"], repo_root).stdout or ""
        normalized = str(Path(worktree_path)).replace("\\", "/")
        still_registered = any(
            line.startswith("worktree ") and line[len("worktree "):].strip().replace("\\", "/") == normalized
            for line in listing.splitlines()
        )
        if still_registered:
            _prune_single_worktree_admin_dir(repo_root, worktree_path)
    except Exception:  # noqa: BLE001 — advisory only, never affects the verdict below
        pass
    return {"removed": True, "error": None}


def _prune_single_worktree_admin_dir(repo_root: str, worktree_path: str) -> None:
    """Best-effort deregister ONLY the admin directory for ``worktree_path``.

    Deliberately does NOT call ``git worktree prune``: that command is
    repo-wide and takes effect immediately with no default grace period
    (verified empirically on Git 2.55.0.windows.3 in a scratch repo) -- it
    deregisters every missing worktree registration in the repository, not
    just the caller's own. Attended stages can run concurrently; a repo-wide
    prune fired from one stage's per-worktree cleanup could deregister
    another stage's live worktree if its directory happened to read as
    transiently absent (slow filesystem, network mount, mid-write).

    Instead, this walks ``<git-common-dir>/worktrees/*`` directly -- each
    admin directory contains a ``gitdir`` file pointing back at
    ``<worktree_path>/.git`` -- finds the single admin dir whose ``gitdir``
    matches ``worktree_path``, and removes only that directory. Every other
    worktree's registration, live or stale, is left completely untouched:
    there is no repo-wide operation for a race to reach.

    Never raises -- the entire body below is wrapped in its own try/except so
    the guarantee holds on its own merits, not merely because the caller
    (``_remove_worktree``) also happens to swallow failures from here. Any
    exception raised by ``_git``, filesystem iteration, or path/stat
    operations is caught and treated as "could not prune" -- silently
    returning, exactly like every other early-return branch in this function.
    This is advisory cleanup only; a raise here must never propagate up into
    the disk-based removal verdict its caller already decided.
    """
    try:
        common = _git(["rev-parse", "--git-common-dir"], repo_root)
        if common.returncode != 0:
            return
        common_dir = Path((common.stdout or "").strip())
        if not common_dir.is_absolute():
            common_dir = Path(repo_root) / common_dir
        worktrees_dir = common_dir / "worktrees"
        if not worktrees_dir.is_dir():
            return
        target = str(Path(worktree_path)).replace("\\", "/").rstrip("/")
        for admin_dir in worktrees_dir.iterdir():
            if not admin_dir.is_dir():
                continue
            gitdir_file = admin_dir / "gitdir"
            if not gitdir_file.is_file():
                continue
            try:
                pointed = gitdir_file.read_text(encoding="utf-8", errors="replace").strip()
            except Exception:  # noqa: BLE001
                continue
            pointed_norm = pointed.replace("\\", "/").rstrip("/")
            if pointed_norm.endswith("/.git"):
                pointed_norm = pointed_norm[: -len("/.git")]
            if pointed_norm == target:
                shutil.rmtree(admin_dir, ignore_errors=True)
                return
    except Exception:  # noqa: BLE001 -- earn the "Never raises" guarantee for real
        return


# --------------------------------------------------------------------------- #
# Attended-worktree janitor                                                   #
# --------------------------------------------------------------------------- #
# `_finalize` already removes a stage's worktree on every code path it reaches
# (pass, surfaced, aborted). This janitor exists for the case it never
# reaches: a killed session, a crashed host process, or any other exit mid
# await_generate/await_judge that leaves the worktree (and its registered git
# branch) on disk with nothing left to remove it. It is intentionally
# separate from pp's `janitor.ts` (mtime-staleness based) — an attended run
# can legitimately sit paused on HITL for days, and mtime staleness cannot
# tell that apart from a truly abandoned worktree; only the Hydra cursor's
# state can. pp's janitor must skip `attended/*` branches/worktrees entirely
# rather than gain attended-awareness itself (kept out of this module's
# scope; see docs/PORTABILITY.md-adjacent worktree relocation notes).
#
# Safety invariants (both required, neither may be relaxed by a caller):
#   - NEVER remove a worktree whose cursor is non-terminal (state not in
#     `_TERMINAL`), including a worktree with NO discoverable cursor at all —
#     "no cursor found" is treated as "cannot prove terminal", not as "safe
#     to remove". A worktree observed mid-provision (cursor not yet written)
#     must never be swept out from under `begin_stage`.
#   - NEVER delete a git branch. A preserved `attended/<run_id>` branch is the
#     only remaining copy of surfaced/non-landed work; only the linked
#     worktree checkout is removed, exactly like `_remove_worktree` does
#     everywhere else in this module.

def _find_attended_cursor(project_root: str, run_id: str) -> Path | None:
    """Locate the cursor file for ``run_id`` under ``<project_root>/.hydra/``.

    The cursor path is keyed by ``(workflow_id, run_id)`` and the janitor only
    knows ``run_id`` (parsed from the worktree dirname), so this globs across
    every workflow_id directory. Returns None if no match (or more than one
    ambiguous match) is found — ambiguity is treated the same as "no cursor"
    by the caller (fail toward not-removing).
    """
    root = Path(project_root) / ".hydra"
    if not root.is_dir():
        return None
    safe_run = "".join(c for c in str(run_id) if c.isalnum() or c in "-_") or "run"
    matches = sorted(root.glob(f"*/attended/{safe_run}.json"))
    if len(matches) != 1:
        return None
    return matches[0]


def sweep_stale_worktrees(
        repo_root: str, project_root: str | None = None,
        dry_run: bool = False) -> dict[str, Any]:
    """Remove attended worktrees whose cursor has reached a terminal state;
    skip (never remove) anything whose cursor is non-terminal or missing.

    Scans ``resolve_worktree_root(repo_root)`` for ``attended-*`` directories
    (the same naming ``_provision_worktree`` creates), looks up each one's
    cursor via ``_find_attended_cursor``, and removes only the ones proven
    terminal. Never deletes a branch. Never raises — per-entry failures are
    collected in the returned report rather than aborting the sweep.

    ``dry_run=True`` (the CLI operator entry point's default) runs the exact
    same terminality decision for every entry but never calls
    ``_remove_worktree`` — a candidate that would be removed is still
    appended to ``report["removed"]``, tagged ``"dry_run": True``, so callers
    get an honest preview without touching disk. ``dry_run=False`` (this
    function's own default, preserved for existing callers) behaves exactly
    as before.

    Returns ``{"removed": [...], "skipped": [...], "errors": [...]}`` where
    each entry is ``{"worktree": path, "run_id": ..., "reason": ...}``.
    """
    report: dict[str, Any] = {"removed": [], "skipped": [], "errors": []}
    root = project_root or repo_root
    wt_root = resolve_worktree_root(repo_root)
    if not wt_root.is_dir():
        return report
    for entry in sorted(wt_root.iterdir()):
        if not entry.is_dir() or not entry.name.startswith("attended-"):
            continue
        run_id = entry.name[len("attended-"):]
        try:
            cursor_file = _find_attended_cursor(root, run_id)
            if cursor_file is None:
                report["skipped"].append({
                    "worktree": str(entry), "run_id": run_id,
                    "reason": "no_cursor_found",
                })
                continue
            cursor = load_cursor(cursor_file)
            state = cursor.get("state")
            if state not in _TERMINAL:
                report["skipped"].append({
                    "worktree": str(entry), "run_id": run_id,
                    "reason": f"non_terminal_state:{state}",
                })
                continue
            if dry_run:
                report["removed"].append({
                    "worktree": str(entry), "run_id": run_id, "state": state,
                    "dry_run": True,
                })
                continue
            result = _remove_worktree(repo_root, str(entry))
            if result.get("removed"):
                report["removed"].append({
                    "worktree": str(entry), "run_id": run_id, "state": state,
                })
            else:
                # git refused (locked worktree, permission error, ...) or the
                # path is still on disk after the attempt -- report that
                # honestly rather than claiming a clean sweep. See
                # `_remove_worktree`'s docstring for why the exit code alone
                # is not trusted here.
                report["errors"].append({
                    "worktree": str(entry), "run_id": run_id,
                    "error": result.get("error") or "removal_failed",
                })
        except Exception as exc:  # noqa: BLE001 — one bad entry must not abort the sweep
            report["errors"].append({
                "worktree": str(entry), "run_id": run_id, "error": str(exc)[:300],
            })
    return report


# --------------------------------------------------------------------------- #
# Stage-active sentinel (Marker 2 for the PreToolUse write-enforcement hooks) #
# --------------------------------------------------------------------------- #
# The hooks (hydra-block-direct-write.ps1 / hydra-block-bash-writes.ps1 /
# hydra-block-direct-pp.ps1) only honor a bare HYDRA_PP_STAGE_ACTIVE=1 when
# this sentinel file exists — the old fallback of enumerating any attended-*
# worktree directory ("Marker 1") was retired because stale worktrees
# accumulate and make that check permanently true. This module is the ONLY
# writer/clearer, so the sentinel's presence is a true run-scoped signal: it
# exists exactly while begin_stage..._finalize/abort_stage spans one attended
# stage, on the same project the hooks check.

def _stage_active_sentinel_path(project_root: str | Path) -> Path:
    return Path(project_root) / ".harness" / "stage-active"


def _write_stage_active_sentinel(project_root: str | Path) -> None:
    """Write the sentinel at stage start. Fail-soft: any I/O error is
    swallowed so a write hiccup never blocks the attended stage — worst case
    the hooks fall back to full enforcement (fail-closed, not fail-open)."""
    try:
        sentinel = _stage_active_sentinel_path(project_root)
        sentinel.parent.mkdir(parents=True, exist_ok=True)
        sentinel.write_text("1", encoding="utf-8")
    except Exception:  # noqa: BLE001 — fail-soft
        pass


def _clear_stage_active_sentinel(project_root: str | Path) -> None:
    """Remove the sentinel at finalize/abort so a later, unrelated session
    doesn't inherit a stale bypass. Fail-soft."""
    try:
        sentinel = _stage_active_sentinel_path(project_root)
        if sentinel.exists():
            sentinel.unlink()
    except Exception:  # noqa: BLE001 — fail-soft
        pass


def _preserve_non_complete_work(cursor: dict[str, Any], worktree_path: str,
                                branch: str, run_id: str,
                                final_status: str = "surfaced") -> None:
    """Commit any uncommitted engineer changes to the attended branch before the
    worktree is removed on a non-complete outcome (MU12).

    Fail-soft: any exception or nonzero git exit emits ``attended.preserve_failed``
    and returns without changing the finalize outcome or cursor state machine.
    Skips silently when the worktree has no changes (nothing to preserve).

    ``final_status`` is interpolated into the commit message so the branch log
    shows the actual outcome (e.g. "surfaced") rather than a hardcoded string.

    On success sets ``cursor["preserved_branch"]`` so the caller and the step
    result exposed to the operator carry the branch name for pickup.
    """
    try:
        _write_worktree_gitexcludes(worktree_path)
        st = _git(["status", "--porcelain"], worktree_path)
        if not (st.stdout or "").strip():
            return  # nothing to preserve — skip silently
        _git(["add", "-A"], worktree_path)
        res = _git(
            ["-c", "user.name=hydra-attended",
             "-c", "user.email=hydra-attended@local",
             "commit",
             "-m", f"attended engineering ({final_status}): {run_id} — preserved for operator pickup",
             "--no-verify"],
            worktree_path,
        )
        if res.returncode != 0:
            raise RuntimeError((res.stderr or res.stdout or "").strip()[:200])
        cursor["preserved_branch"] = branch
        _trace(cursor, "attended.preserved", {"branch": branch, "run_id": run_id})
    except Exception as exc:  # noqa: BLE001
        _trace(cursor, "attended.preserve_failed",
               {"run_id": run_id, "error": str(exc)[:200]})


# --------------------------------------------------------------------------- #
# Cursor persistence                                                          #
# --------------------------------------------------------------------------- #

def cursor_path(project_root: str | Path, workflow_id: str, run_id: str) -> Path:
    """Sidecar path for an attended stage's cursor.

    Lives under ``<project>/.hydra/<workflow_id>/attended/<run_id>.json`` — the
    ``.hydra`` tree is already the per-workflow scratch/trace area.
    """
    safe_run = "".join(c for c in str(run_id) if c.isalnum() or c in "-_") or "run"
    return (Path(project_root) / ".hydra" / str(workflow_id)
            / "attended" / f"{safe_run}.json")


def load_cursor(path: str | Path) -> dict[str, Any]:
    """Load a cursor; raise FileNotFoundError if absent, ValueError if stale."""
    p = Path(path)
    data = json.loads(p.read_text(encoding="utf-8"))
    if not isinstance(data, dict) or data.get("schema") != CURSOR_SCHEMA:
        raise ValueError(f"attended cursor schema mismatch at {p} "
                         f"(want {CURSOR_SCHEMA}, got {data.get('schema')!r})")
    return data


def save_cursor(path: str | Path, cursor: dict[str, Any]) -> None:
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    # Atomic-ish write: temp + replace so a crash mid-write never leaves a
    # truncated cursor that would wedge the workflow.
    tmp = p.with_suffix(p.suffix + ".tmp")
    tmp.write_text(json.dumps(cursor, indent=2, default=str), encoding="utf-8")
    os.replace(tmp, p)


# --------------------------------------------------------------------------- #
# Rider (a) — smoke baseline helpers                                         #
# --------------------------------------------------------------------------- #

def _parse_failing_tests(output: str) -> set[str]:
    """Parse failing test IDs from pytest --tb=no -q output.

    Matches lines like: ``FAILED tests/test_foo.py::test_bar - reason``
    Returns a set of bare test IDs (no trailing reason).
    """
    failing: set[str] = set()
    for line in output.splitlines():
        s = line.strip()
        if s.startswith("FAILED "):
            # Strip "FAILED " prefix and any trailing " - reason" suffix.
            test_id = s[7:].split(" - ")[0].strip()
            if test_id:
                failing.add(test_id)
    return failing


def _capture_baseline_failures(
        project_path: str, repo_root: str | None = None) -> list[str]:
    """Run pytest before engineer changes; return sorted list of failing test IDs.

    Called at begin_stage time (fresh worktree = no engineer changes) so
    environment-specific failures (path-sensitive tests that always fail
    inside a worktree) are captured as the baseline.  A later smoke-fail
    is treated as clean if current failures ⊆ baseline.

    GAP-a2 (Fix 3): tries repo_root first when provided, since an attended
    worktree (resolved via ``resolve_worktree_root()``, outside repo_root)
    does NOT have a tests/ directory of its own — the tests live in the repo
    root.  Without repo_root, falls
    back to project_path then project_path.parent (less reliable for worktrees,
    which may be several levels deep under the repo root).
    Fail-soft: any exception returns an empty list (no baseline → smoke
    failures are NOT excused, which is the safe default).
    """
    import json as _json
    import sys as _sys
    # Build candidate list: prefer repo_root > project_path > parent
    candidates: list[str] = []
    if repo_root and repo_root != project_path:
        candidates.append(repo_root)
    candidates.append(project_path)
    if not repo_root:
        # Legacy fallback: try parent (unreliable for deep worktrees but better
        # than nothing when repo_root is unknown).
        parent = str(Path(project_path).parent)
        if parent and parent != project_path:
            candidates.append(parent)

    # MU17: the baseline only depends on the tree at branch point (HEAD of the
    # anchor repo), and the full-suite run costs minutes — enough to blow the
    # attended step budget on large repos. Cache completed baselines per
    # (anchor, HEAD sha) under <anchor>/.harness/baseline/<sha>.json so only
    # the first stage after a new commit pays the suite cost.
    # Completed baselines are cached as <sha>.json (a JSON list of failing tests).
    # Timeouts are now cached as a degraded marker: <sha>.timeout.json containing
    # {"timeout_s": <value>}.  On entry, if the marker exists for the current HEAD
    # sha, the function returns [] immediately without re-running the suite — raise
    # HYDRA_BASELINE_TIMEOUT_S to give the suite more budget.  This prevents
    # re-paying an already-too-slow suite on every stage (which would double the
    # damage and blow the step budget) while keeping the safe default: no baseline
    # → smoke failures are not excused.  Do NOT try the next candidate on timeout —
    # re-running is always just as slow.
    _cache_anchor = candidates[0] if candidates else project_path
    _cache_file: Path | None = None
    _timeout_marker: Path | None = None
    try:
        _sha = _git(["rev-parse", "HEAD"], _cache_anchor).stdout.strip()
        if _sha:
            _cache_file = (Path(_cache_anchor) / ".harness" / "baseline"
                           / f"{_sha}.json")
            _timeout_marker = (Path(_cache_anchor) / ".harness" / "baseline"
                               / f"{_sha}.timeout.json")
            if _cache_file.is_file():
                cached = _json.loads(_cache_file.read_text(encoding="utf-8"))
                if isinstance(cached, list):
                    return sorted(str(t) for t in cached)
            # If a timeout marker exists the suite already exceeded its budget at
            # this commit.  Skip silently; operator can delete the marker or raise
            # HYDRA_BASELINE_TIMEOUT_S.
            if _timeout_marker.is_file():
                import logging as _logging
                _logging.getLogger(__name__).info(
                    "baseline timeout marker found for sha=%s — skipping suite "
                    "(baseline degraded; raise HYDRA_BASELINE_TIMEOUT_S to retry)",
                    _sha,
                )
                return []
    except Exception:  # noqa: BLE001 — cache read is best-effort
        _cache_file = None
        _timeout_marker = None

    for cwd in candidates:
        tests_dir = Path(cwd) / "tests"
        if not tests_dir.is_dir():
            continue
        try:
            res = subprocess.run(
                [
                    _sys.executable, "-m", "pytest",
                    "tests/", "--no-header", "-q", "--tb=no",
                ],
                cwd=cwd,
                capture_output=True,
                text=True,
                check=False,
                timeout=_baseline_timeout_s(),
            )
            failing = sorted(_parse_failing_tests(res.stdout + "\n" + res.stderr))
            # Cache the completed result (empty list is a valid baseline) so
            # subsequent stages at the same HEAD skip the suite entirely.
            if _cache_file is not None:
                try:
                    _cache_file.parent.mkdir(parents=True, exist_ok=True)
                    _cache_file.write_text(_json.dumps(failing), encoding="utf-8")
                except Exception:  # noqa: BLE001 — cache write is best-effort
                    pass
            # Return the first successful (or empty) result — empty is valid
            # (all tests pass in this env = no baseline needed).
            return failing
        except subprocess.TimeoutExpired:
            # MU17 degraded-marker: write <sha>.timeout.json so subsequent calls
            # at the same HEAD return [] immediately without re-running the suite.
            # No baseline → smoke failures are not excused, the safe default.
            # Do NOT try the next candidate — the suite is too slow regardless of
            # which cwd we use.
            if _timeout_marker is not None:
                try:
                    _timeout_marker.parent.mkdir(parents=True, exist_ok=True)
                    _timeout_marker.write_text(
                        _json.dumps({"timeout_s": _baseline_timeout_s()}),
                        encoding="utf-8",
                    )
                except Exception:  # noqa: BLE001 — marker write is best-effort
                    pass
            return []
        except Exception:  # noqa: BLE001 — baseline failure is non-fatal
            continue
    return []


# GAP-h: heuristic — a regex for file-path-like tokens in critique text.
_PATH_TOKEN_RE = _re.compile(
    r'[A-Za-z0-9_][A-Za-z0-9_./-]*\.[a-zA-Z]{1,10}'
)


def _has_real_file_ref(critique_md: str, work_path: str) -> bool:
    """Return True if critique_md contains at least one path-like token that
    resolves to an existing file under work_path.  Heuristic — warn-only."""
    for match in _PATH_TOKEN_RE.finditer(critique_md):
        token = match.group().replace("\\", "/").lstrip("/")
        try:
            if (Path(work_path) / token).exists():
                return True
        except Exception:  # noqa: BLE001
            pass
    return False


# --------------------------------------------------------------------------- #
# Step results                                                                #
# --------------------------------------------------------------------------- #

def _host_action(cursor: dict[str, Any]) -> dict[str, Any] | None:
    return cursor.get("pending_action")


def _step_result(cursor: dict[str, Any], cursor_file: str | Path) -> dict[str, Any]:
    """Project a cursor into the result dict the CLI/MCP layer returns to the host."""
    state = cursor.get("state")
    if state in _TERMINAL:
        status = state
    else:
        status = "awaiting_host"
    res: dict[str, Any] = {
        "status": status,
        "workflow_id": cursor.get("workflow_id"),
        "run_id": cursor.get("run_id"),
        "stage_id": cursor.get("stage_id"),
        "task_id": cursor.get("task_id"),
        "squad_slug": cursor.get("squad_slug"),
        "state": state,
        "cursor_path": str(cursor_file),
        "cost_usd": float(cursor.get("cost_usd") or 0.0),
        "tokens_in": int(cursor.get("tokens_in") or 0),
        "tokens_out": int(cursor.get("tokens_out") or 0),
    }
    if status == "awaiting_host":
        res["host_action"] = _host_action(cursor)
        if state == "stalled_infra":
            # W2-3: surface the hold + reason on the non-terminal path too, not
            # just on a terminal outcome — an operator/recovery caller needs
            # to see this without the stage having been finalized.
            res["stalled_infra"] = True
            if cursor.get("error"):
                res["error"] = cursor["error"]
    if state in _TERMINAL:
        res["final_status"] = cursor.get("final_status") or state
        res["stage_outcome"] = cursor.get("outcome")
        res["smoke_status"] = cursor.get("smoke_status")
        res["changed_paths"] = cursor.get("changed_paths") or []
        if cursor.get("merge") is not None:
            res["merge"] = cursor["merge"]
        if cursor.get("error"):
            res["error"] = cursor["error"]
        # MU12: expose preserved_branch so the operator knows where to find
        # the work when the stage surfaces without completing.
        if cursor.get("preserved_branch"):
            res["preserved_branch"] = cursor["preserved_branch"]
        # Rider (b): expose charged flag so _cmd_attended_submit can skip
        # duplicate budget charges on a retried submit-host-result call.
        res["already_charged"] = bool(cursor.get("charged", False))
        if cursor.get("emitted_envelopes"):
            res["emitted_envelopes"] = cursor["emitted_envelopes"]
            res["emitted_envelope_count"] = len(cursor["emitted_envelopes"])
        if cursor.get("artifact_text"):
            res["artifact_text"] = cursor["artifact_text"]
    return res


def _trace(cursor: dict[str, Any], kind: str, payload: dict[str, Any]) -> None:
    wf = cursor.get("workflow_id")
    if not wf:
        return
    try:
        _telemetry.emit(Path(cursor["project_path"]), wf, kind,
                        {"run_id": cursor.get("run_id"), **payload})
    except Exception:  # noqa: BLE001 — never crash the driver on a trace write
        pass


# --------------------------------------------------------------------------- #
# Driver                                                                       #
# --------------------------------------------------------------------------- #

def begin_stage(
    dispatcher: Dispatcher,
    *,
    workflow_id: str,
    run_id: str,
    project_path: str,
    request_text: str,
    model_tier: str | None = None,
    judge_rubric_id: str = "rfc-2119-normative",
    project_root: str | Path | None = None,
    task_id: str | None = None,
    isolate: bool = True,
    hydra_context_block: str | None = None,
) -> dict[str, Any]:
    """Open an attended code stage and pause for the first host action (the
    ``engineer`` generation). ``run_id`` must already exist (the caller runs
    ``start_run`` / has a scaffolded run). Returns an ``awaiting_host`` step
    result whose ``host_action`` tells the host to spawn the visible
    ``engineer`` subagent.

    ``isolate`` (default True): provision a linked git worktree under
    ``.harness/worktrees/`` for the engineer to write into (write-safe under the
    ``hydra-block-direct-write`` hook), merged back on a passing finalize. Falls
    back to in-place writes when the target isn't a git repo.
    """
    # Browser isolation parity with the headless driver (PP-BV-ISO).
    os.environ.setdefault("PP_BROWSER_ENGINE", "playwright")
    cm = dispatcher.call_mcp

    st = _raise_on_error_payload(
        cm("pp_harness", "start_stage",
           {"run_id": run_id, "kind": "code", "gate_type": "code"},
           squad_id=_SQ),
        "start_stage",
    )
    stage_id = _pp_inner(st).get("stage_id")
    if not stage_id:
        raise RuntimeError(f"start_stage returned no stage_id: {st!r}")

    # Write-safety: isolate the engineer into a hook-allowed worktree.
    work_path = project_path
    worktree_path: str | None = None
    branch: str | None = None
    repo_root: str | None = None
    if isolate:
        repo_root = _git_repo_root(project_path)
        if repo_root:
            prov = _provision_worktree(repo_root, run_id)
            if prov is not None:
                worktree_path, branch = prov
                work_path = worktree_path

    base_prompt = _build_engineer_prompt(request_text, work_path)
    # 7b: prepend hydra_context_block (workflow/envelope metadata from start_run)
    # so the engineer subagent carries full Hydra routing context. Empty/None →
    # identical to previous behavior.
    if hydra_context_block:
        base_prompt = f"{hydra_context_block}\n\n{base_prompt}"
    # Rider (a): capture baseline failures before the engineer touches anything.
    # The worktree is a linked git worktree (same commits as the repo root); any
    # failures here are environment-specific rather than regressions introduced
    # by the engineer's changes.  Pass repo_root so pytest runs from the right
    # directory (worktrees don't carry their own tests/ dir).
    baseline_failures = _capture_baseline_failures(work_path, repo_root=repo_root)
    root = project_root or project_path
    cursor: dict[str, Any] = {
        "schema": CURSOR_SCHEMA,
        "workflow_id": workflow_id,
        "run_id": run_id,
        "stage_id": stage_id,
        "task_id": task_id,
        "project_path": project_path,
        "project_root": str(root),
        "work_path": work_path,
        "worktree_path": worktree_path,
        "branch": branch,
        "repo_root": repo_root,
        "request_text": request_text,
        # Persisted so the Reflexion retry prompt can prepend it (7b fix).
        "hydra_context_block": hydra_context_block or "",
        "model_tier": model_tier,
        "judge_rubric_id": judge_rubric_id,
        "state": "await_generate",
        "pre_dirty": sorted(_worktree_dirty_set(work_path)),
        "baseline_failures": baseline_failures,
        "producer": "claude",
        "generate_index": 0,   # GAP-f: tracks Reflexion×1 — 0=first attempt, 1=retry
        "reflexion_critique": "",
        "attempt_id": None,
        "cost_usd": 0.0,
        "tokens_in": 0,
        "tokens_out": 0,
        "changed_paths": [],
        "outcome": None,
        "smoke_status": "skipped",
        "smoke_reason": "",
        "final_status": None,
        "error": None,
        "pending_action": {
            "call_key": "generate-0",
            "agent_type": "engineer",
            "cwd": work_path,
            "isolated_worktree": bool(worktree_path),
            "prompt": base_prompt,
            "instructions": (
                "Spawn the visible `engineer` subagent in cwd to implement the "
                "request, editing files directly. Then call submit-host-result "
                "with {call_key, result:{text, cost_usd, tokens_in, tokens_out, "
                "model}} where `text` summarizes the change."),
        },
    }
    cfile = cursor_path(root, workflow_id, run_id)
    save_cursor(cfile, cursor)
    # Marker 2: write the run-scoped sentinel the write-enforcement hooks
    # check. Cleared in _finalize / abort_stage.
    _write_stage_active_sentinel(root)
    _trace(cursor, "attended.stage_started", {"stage_id": stage_id})
    return _step_result(cursor, cfile)


def _apply_generate(dispatcher: Dispatcher, cursor: dict[str, Any],
                    result: dict[str, Any]) -> None:
    """await_generate -> await_judge (or terminal on generate failure).

    The host's ``engineer`` subagent already wrote files in cwd; ``result`` is
    its summary + spend. We attribute the run-scoped diff, archive + record the
    attempt, route the judge via pp's ``gate_eligible_judges``, and stage the
    judge host-action.
    """
    cm = dispatcher.call_mcp
    work_path = cursor.get("work_path") or cursor["project_path"]
    stage_id = cursor["stage_id"]
    run_id = cursor["run_id"]
    producer = cursor["producer"]

    gen_text = str(result.get("text") or "")
    cursor["cost_usd"] = float(cursor["cost_usd"]) + float(result.get("cost_usd") or 0.0)
    cursor["tokens_in"] = int(cursor["tokens_in"]) + int(result.get("tokens_in") or 0)
    cursor["tokens_out"] = int(cursor["tokens_out"]) + int(result.get("tokens_out") or 0)

    pre_dirty = set(cursor.get("pre_dirty") or [])
    run_changed = _worktree_dirty_set(work_path) - pre_dirty
    cursor["changed_paths"] = sorted(set(cursor.get("changed_paths") or []) | run_changed)
    wrote_changes = bool(run_changed)

    gen_fail = _generate_failure_reason(
        {"status": "done", "result": result}, gen_text, wrote_changes)

    model_id = str(result.get("model") or cursor.get("model_tier") or f"{producer}-default")

    # GAP-f: generate_index tracks the Reflexion×1 retry (0=first, 1=Reflexion).
    gen_idx = cursor.get("generate_index", 0)

    if gen_fail:
        cursor["error"] = gen_fail
        cursor["outcome"] = "error"
        try:
            _raise_on_error_payload(
                cm("pp_harness", "archive_artifact", {
                    "run_id": run_id,
                    "relative_path": f"code/{producer}-attempt-{gen_idx}.failed.md",
                    "bytes": f"GENERATE FAILED: {gen_fail}\n\n{gen_text or '(no output)'}",
                    "stage_id": stage_id, "kind": "code", "encoding": "utf8",
                }, squad_id=_SQ),
                "archive_artifact",
            )
        except Exception:  # noqa: BLE001
            pass
        try:
            att = _raise_on_error_payload(
                cm("pp_harness", "record_attempt", {
                    "stage_id": stage_id, "producer": producer, "model_id": model_id,
                    "agent_type": "engineer",   # F29
                    "tokens_in": int(result.get("tokens_in") or 0),
                    "tokens_out": int(result.get("tokens_out") or 0),
                    "cost_usd": float(result.get("cost_usd") or 0.0),
                    "status": "error", "retry_index": gen_idx,
                    "notes": {"candidate_index": 1},
                }, squad_id=_SQ),
                "record_attempt",
            )
            cursor["attempt_id"] = _pp_inner(att).get("attempt_id")
        except Exception:  # noqa: BLE001
            pass
        # Generation failed; finalize as surfaced (no judge).
        _finalize(dispatcher, cursor, passed=False, gen_failed=True)
        return

    # Successful generate: archive the producer summary + record the attempt.
    try:
        _raise_on_error_payload(
            cm("pp_harness", "archive_artifact", {
                "run_id": run_id,
                "relative_path": f"code/{producer}-attempt-{gen_idx}.md",
                "bytes": gen_text or "(no summary returned)",
                "stage_id": stage_id, "kind": "code", "encoding": "utf8",
            }, squad_id=_SQ),
            "archive_artifact",
        )
    except Exception:  # noqa: BLE001
        pass
    # Finding 1: wrap record_attempt in try/except — an RPC failure here must
    # surface cleanly, not crash submit_host_result and orphan the stage.
    # LV-1: _raise_on_error_payload converts error dicts into a RuntimeError
    # (PPLedgerError) so the existing except clause fires for payload-level
    # failures too -- and it preserves the original dict as ``.payload``,
    # which is what venom-gate classification downstream reads to tell a
    # structural rejection from a transport-shaped failure.
    # The pp schema accepts agent_type as an optional top-level string; strict
    # mode only rejects the literal 'general-purpose', not 'engineer'.
    try:
        att = _raise_on_error_payload(
            cm("pp_harness", "record_attempt", {
                "stage_id": stage_id, "producer": producer, "model_id": model_id,
                "agent_type": "engineer",   # F29 — accepted optional; strict rejects 'general-purpose'
                "tokens_in": int(result.get("tokens_in") or 0),
                "tokens_out": int(result.get("tokens_out") or 0),
                "cost_usd": float(result.get("cost_usd") or 0.0),
                "status": "ok", "retry_index": gen_idx,
                "notes": {"candidate_index": 1},
            }, squad_id=_SQ),
            "record_attempt",
        )
        cursor["attempt_id"] = _pp_inner(att).get("attempt_id")
    except Exception as _ra_exc:  # noqa: BLE001
        # record_attempt RPC failed — surface the stage immediately rather than
        # crashing. The engineer's work is generated but cannot be tracked.
        cursor["error"] = f"record_attempt RPC failed: {_ra_exc!r}"
        _finalize(dispatcher, cursor, passed=False, gen_failed=False)
        return

    # Judge routing — honour pp's gate_eligible_judges (cross- vs same-vendor)
    # exactly like the headless loop, so the host spawns the right judge agent.
    gate_dec: dict[str, Any] = {}
    try:
        gate_dec = _pp_inner(_raise_on_error_payload(
            cm("pp_harness", "gate_eligible_judges", {
                "gate_type": _pp_gate_type("code", "code_style"),
                "generator_producer": producer,
                "prompt_keywords": cursor["request_text"][:1000],
            }, squad_id=_SQ),
            "gate_eligible_judges",
        ))
    except Exception:  # noqa: BLE001
        gate_dec = {}
    required_cross = bool(gate_dec.get("required_cross_vendor", True))
    gate_rubric = str(gate_dec.get("rubric_id") or cursor["judge_rubric_id"])
    # producer is "claude": a sanctioned same-vendor Claude judge is allowed only
    # when cross-vendor is NOT required; otherwise the host must spawn the
    # cross-vendor judge (codex/agy critique).
    judge_agent = "judge-cross-vendor" if required_cross else "judge-same-vendor"
    rubric_body = _rubric_md(gate_rubric)
    judge_text = _judge_artifact_text(
        work_path, sorted(run_changed), gen_text)

    cursor["gate_rubric"] = gate_rubric
    cursor["required_cross"] = required_cross
    cursor["state"] = "await_judge"
    # LV-8: scope the call_key with run_id + stage_id, not just the generate
    # index. This value round-trips verbatim as pp's record_verdict
    # idempotency_token (see _apply_judge below); an unscoped "judge-0"/
    # "judge-1" collides across every stage/run in the shared pp daemon DB,
    # so a second stage's genuine first verdict call silently short-circuits
    # into the *first* stage's verdict row via pp's unscoped idempotency
    # lookup. Scoping keeps the retry-safety property (same stage + same
    # gen_idx replay => same token => same idempotent short-circuit) while
    # eliminating cross-stage/cross-run collisions.
    #
    # Retry-fix follow-up: also fold in attempt_id (set on the cursor a few
    # lines above by record_attempt). run_id+stage_id+gen_idx is already
    # unique for the lifetime of this stage's logical verdict slot -- pp's
    # own findVerdictByIdempotencyToken/resolveIdempotentVerdict is what
    # actually enforces exactly-once, not this token's uniqueness. But pp's
    # guard error message itself tells callers to scope idempotency tokens by
    # attempt, and this token didn't. Including attempt_id makes token<->
    # attempt 1:1 by construction rather than by an argument spanning three
    # call sites (this one, the finalize_stage fallback below, and
    # recover_stalled_stage's replay) that a future refactor could silently
    # invalidate. attempt_id is stable across a stalled-infra re-drive and
    # across recover_stalled_stage (both replay the ORIGINAL captured
    # call_key/payload rather than rebuilding it from cursor state), so this
    # doesn't break retry-safety: the same judge call re-driven on the same
    # attempt still produces the same token.
    judge_call_key = f"judge-{run_id}-{stage_id}-{cursor['attempt_id']}-{gen_idx}"
    cursor["pending_action"] = {
        "call_key": judge_call_key,
        "agent_type": judge_agent,
        "rubric_id": gate_rubric,
        "required_cross_vendor": required_cross,
        "artifact_text": judge_text,
        "rubric_md": rubric_body,
        "cwd": work_path,
        "instructions": (
            f"Spawn the visible `{judge_agent}` subagent to judge the diff "
            f"against rubric {gate_rubric}. Then call submit-host-result with "
            "{call_key, result:{outcome:pass|revise|fail, critique_md, "
            "judge_producer, judge_model_id, score_json, cost_usd}}."),
    }
    _trace(cursor, "attended.attempt_recorded", {
        "stage_id": stage_id, "attempt_id": cursor["attempt_id"],
        "producer": producer, "judge_agent": judge_agent,
        "required_cross_vendor": required_cross, "wrote_changes": wrote_changes,
        "generate_index": gen_idx,
    })


def _apply_judge(dispatcher: Dispatcher, cursor: dict[str, Any],
                 result: dict[str, Any],
                 *, cursor_file: "str | Path | None" = None,
                 call_key: str | None = None) -> None:
    """await_judge -> terminal (or back to await_generate for Reflexion x1).

    F26+M8: a failed record_verdict/finalize_stage on a pass outcome downgrades
    the stage to surfaced (never proceeds to finalize_run complete).
    F31: required_cross_vendor but judge was same-vendor (degraded) → downgrade.
    GAP-f: Reflexion×1 — on the first revise, transition back to await_generate
    with call_key='generate-1' and the critique embedded in the prompt.
    GAP-h: warn-telemetry when critique_md cites no existing worktree file.
    GAP-a2: lazy baseline fallback from HYDRA_SMOKE_BASELINE_TESTS env var.
    Fix-1b: idempotency markers (verdict_recorded_for / smoke_result_for) written
    mid-function so a retried submit after a timeout never double-records in the
    pp ledger or restarts the ~28-min smoke.
    """
    cm = dispatcher.call_mcp
    producer = cursor["producer"]
    attempt_id = cursor.get("attempt_id")
    gate_rubric = cursor.get("gate_rubric") or cursor["judge_rubric_id"]
    required_cross = bool(cursor.get("required_cross"))
    gen_idx = cursor.get("generate_index", 0)   # GAP-f: 0=first attempt, 1=retry
    work_path = cursor.get("work_path") or cursor["project_path"]

    # W2-3: guard cost/token accrual against double-counting when a
    # transport-shaped record_verdict failure holds the cursor open
    # (state="stalled_infra") and the SAME judge result is resubmitted under
    # the SAME call_key to re-drive the stage. Without this guard a re-drive
    # would add the judge's cost_usd/tokens a second time.
    _judge_cost_applied = (call_key is not None
                           and cursor.get("judge_cost_applied_for") == call_key)
    if not _judge_cost_applied:
        # The double-counting guard above only activates when `call_key` is
        # not None -- it relies on real judge submissions always carrying one
        # (enforced by call topology: every host_action the driver hands out
        # for a judge step sets pending_action["call_key"]). Make that
        # structural rather than incidental: surface it in trace if it's ever
        # violated, instead of silently accruing cost with no re-drive guard.
        if call_key is None:
            _trace(cursor, "attended.judge_cost_no_call_key", {
                "stage_id": cursor.get("stage_id"),
                "warning": ("submit_verdict called with call_key=None; the "
                            "judge_cost_applied_for double-counting guard "
                            "cannot protect this accrual on a re-drive"),
            })
        cursor["cost_usd"] = float(cursor["cost_usd"]) + float(result.get("cost_usd") or 0.0)
        cursor["tokens_in"] = int(cursor["tokens_in"]) + int(result.get("tokens_in") or 0)
        cursor["tokens_out"] = int(cursor["tokens_out"]) + int(result.get("tokens_out") or 0)
        if call_key is not None:
            cursor["judge_cost_applied_for"] = call_key

    outcome = result.get("outcome") or result.get("verdict") or "revise"
    if outcome not in {"pass", "revise", "fail"}:
        outcome = "revise"
    critique_md = str(result.get("critique_md") or result.get("critique") or "")
    judge_producer = str(result.get("judge_producer")
                         or ("codex" if required_cross else "claude"))
    cross_vendor = judge_producer != producer
    degraded = required_cross and not cross_vendor
    # LV-3 defense-in-depth: when same-vendor judging is allowed (not
    # required_cross) and the judge producer is identical to the generator,
    # relabel with a "-same-vendor-host" suffix before record_verdict.  pp's
    # recordVerdict rejects generator-identical producer+model pairs; the
    # suffix keeps the model id honest while making the ledger entry
    # distinguishable.  cross_vendor is NOT recomputed — it was False (same
    # vendor) and stays False; score_json._judge_tier="same_vendor" is correct.
    if not required_cross and judge_producer == producer:
        judge_producer = f"{producer}-same-vendor-host"

    score_json = dict(result.get("score_json") or result.get("score") or {})
    score_json["_cross_vendor"] = cross_vendor
    score_json["_judge_tier"] = "cross_vendor" if required_cross else "same_vendor"
    score_json["_attended"] = True
    if degraded:
        score_json["_judge_degraded"] = True

    # Finding 2: track whether the outcome change is an infra failure (F31 /
    # F26+M8) vs a genuine artifact defect.  Infra failures must surface
    # immediately — Reflexion is reserved for code defects the engineer can fix.
    _infra_downgrade = False

    # F31: required cross-vendor but got same-vendor judge → downgrade pass to surfaced.
    if degraded and outcome == "pass":
        outcome = "revise"   # treat as revise so non-pass path runs
        _infra_downgrade = True
        cursor["error"] = ("required_cross_vendor=true but judge was same-vendor "
                           "(degraded); stage downgraded to surfaced")

    cursor["outcome"] = outcome

    # Fix-1b: idempotency — skip record_verdict if a prior attempt for this exact
    # call_key already succeeded and we persisted the marker.  A submit timeout
    # that kills mid-_run_smoke (before the outer save_cursor at line ~1172) would
    # otherwise cause a retry to double-write the pp verdict ledger.
    _record_verdict_ok = True
    _record_verdict_exc: Exception | None = None
    _verdict_already_recorded = (call_key is not None
                                  and cursor.get("verdict_recorded_for") == call_key)
    if _verdict_already_recorded:
        _trace(cursor, "attended.verdict_skip_idempotent", {
            "stage_id": cursor.get("stage_id"),
            "call_key": call_key,
            "reason": "verdict_recorded_for marker matches — skipping duplicate record_verdict",
        })
    elif attempt_id:
        # W2-4: persist the exact record_verdict payload BEFORE the call so a
        # stage stranded by a transport-shaped failure that ends up needing
        # the `/hydra:resume --action recover-stalled-stage` path (e.g. an
        # older cursor from before the stalled_infra hold existed) can replay
        # this call verbatim instead of needing the judge's raw result
        # reconstructed from scratch.
        _verdict_payload = {
            "attempt_id": attempt_id,
            "judge_producer": judge_producer,
            "judge_model_id": str(result.get("judge_model_id")
                                  or result.get("model") or f"{judge_producer}-default"),
            "outcome": outcome if outcome in {"pass", "revise", "fail"} else "revise",
            "critique_md": critique_md[:4000],
            "score_json": score_json,
            "rubric_id": gate_rubric,
            # W2-3: the attended call_key doubles as pp's idempotency token. A
            # re-drive after a stalled_infra hold resubmits the same call_key,
            # so pp's recordVerdict returns the original verdict_id instead of
            # inserting a duplicate row -- exactly-once even across a
            # transport-shaped retry (or the W2-4 recovery replay).
            **({"idempotency_token": call_key} if call_key else {}),
        }
        cursor["pending_verdict_payload"] = _verdict_payload
        if cursor_file is not None:
            save_cursor(cursor_file, cursor)
        # F26+M8: capture record_verdict success; a failure on a pass outcome downgrades.
        # LV-1: _raise_on_error_payload converts error dicts (rejected/failed) into
        # a RuntimeError (PPLedgerError) so the existing except fires for
        # payload-level errors too -- and it preserves the original dict as
        # ``.payload``, which is what venom-gate classification downstream
        # reads to tell a structural rejection from a transport-shaped failure.
        try:
            _raise_on_error_payload(
                cm("pp_harness", "record_verdict", _verdict_payload, squad_id=_SQ),
                "record_verdict",
            )
            # Persist marker before _run_smoke so a timeout mid-smoke leaves the
            # cursor in a state where a retry can skip this call.
            if call_key is not None and cursor_file is not None:
                cursor["verdict_recorded_for"] = call_key
                save_cursor(cursor_file, cursor)
        except Exception as exc:  # noqa: BLE001
            # W2-2: capture the failure reason instead of discarding it. This
            # exact swallow is what forced a manual forensic reconstruction of
            # the first stalled-verdict incident -- the ledger had no verdict
            # row and no trace explaining why.
            _record_verdict_ok = False
            _record_verdict_exc = exc
    if outcome == "pass" and not _record_verdict_ok:
        _rv_reason = str(_record_verdict_exc) if _record_verdict_exc is not None else "unknown error"
        _rv_kind = _classify_infra_failure(_record_verdict_exc)
        cursor["error"] = (cursor.get("error") or "") + \
            f" record_verdict RPC failed ({_rv_kind}): {_rv_reason}"
        _trace(cursor, "attended.verdict_rpc_failed", {
            "stage_id": cursor.get("stage_id"), "tool": "record_verdict",
            "call_key": call_key, "attempt_id": attempt_id,
            "reason": _rv_reason, "kind": _rv_kind,
        })
        if _rv_kind == "transport":
            # W2-3: hold the cursor open instead of downgrading the outcome
            # and finalizing. pending_action is left untouched (still the
            # judge's call_key), so a re-issued submit_host_result carrying
            # the SAME judge result re-enters this function and retries
            # record_verdict via the idempotency_token above. The worktree,
            # pp attempt row, and any smoke result are NOT touched here, so
            # they remain available to the recovery path (W2-4) or a manual
            # retry.
            cursor["state"] = "stalled_infra"
            _trace(cursor, "attended.stalled_infra", {
                "stage_id": cursor.get("stage_id"), "call_key": call_key,
                "attempt_id": attempt_id, "reason": _rv_reason,
            })
            return
        # Deterministic pp rejection (or an ambiguous failure we could not
        # positively classify as transport -- err toward failing the stage
        # rather than silently masking a real rejection): keep today's
        # behavior of downgrading to revise/surfaced.
        outcome = "revise"
        _infra_downgrade = True
        cursor["outcome"] = "revise"

    _trace(cursor, "attended.verdict", {
        "stage_id": cursor["stage_id"], "rubric_id": gate_rubric,
        "attempt_id": attempt_id, "producer": producer,
        "judge_producer": judge_producer, "outcome": outcome,
        "cross_vendor": cross_vendor, "generate_index": gen_idx,
        "degraded": degraded,
    })

    # GAP-h: warn when critique references no existing worktree file.
    if critique_md and not _has_real_file_ref(critique_md, work_path):
        _trace(cursor, "attended.judge.suspicious_critique", {
            "stage_id": cursor["stage_id"],
            "warning": "critique_md contains no path token matching an existing worktree file",
            "judge_producer": judge_producer,
            "critique_head": critique_md[:200],
        })

    # GAP-f: Reflexion×1 — on first revise (gen_idx==0), transition back to
    # await_generate with an augmented prompt that embeds the critique.
    # Finding 2: skip Reflexion for infra failures (F31 degraded judge, F26+M8
    # record_verdict RPC error) — retrying the engineer cannot fix an infra
    # problem and wastes a generation slot.
    if outcome == "revise" and gen_idx == 0 and not _infra_downgrade:
        cursor["generate_index"] = 1
        cursor["reflexion_critique"] = critique_md
        aug_prompt = _augment_with_critique(cursor["request_text"], critique_md)
        # 7b fix: re-prepend the hydra_context_block exactly once so the retry
        # prompt mirrors the initial generate-0 prompt structure.  The block was
        # stored in the cursor at begin_stage; an empty string is a no-op.
        _hcb = cursor.get("hydra_context_block", "")
        if _hcb:
            aug_prompt = f"{_hcb}\n\n{aug_prompt}"
        cursor["state"] = "await_generate"
        cursor["pending_action"] = {
            "call_key": "generate-1",
            "agent_type": "engineer",
            "cwd": work_path,
            "isolated_worktree": bool(cursor.get("worktree_path")),
            "prompt": aug_prompt,
            "retry_index": 1,
            "instructions": (
                "Spawn the visible `engineer` subagent to revise the implementation "
                "addressing the critique embedded in the prompt. Then call "
                "submit-host-result with {call_key, result:{text, cost_usd, "
                "tokens_in, tokens_out, model}}."),
        }
        # Fix-1b: clear idempotency markers when transitioning to a new generate
        # cycle so the next judge (judge-1) records its own verdict freshly.
        cursor.pop("verdict_recorded_for", None)
        cursor.pop("smoke_result_for", None)
        _trace(cursor, "attended.reflexion", {
            "stage_id": cursor["stage_id"], "generate_index": 1,
        })
        return  # Don't finalize — wait for generate-1

    # PP-VG-5: a code stage may finalize 'complete' only with a real smoke result.
    passed = False
    smoke_status = "skipped"
    smoke_reason = ""
    if outcome == "pass" and attempt_id:
        # Fix-1b: if the smoke already completed for this call_key (persisted before
        # a prior submit timed out inside _finalize), reuse the result without
        # re-running the ~28-min test suite or double-calling record_smoke_status.
        _cached_smoke = cursor.get("smoke_result_for") or {}
        _smoke_from_cache = (call_key is not None
                             and _cached_smoke.get("call_key") == call_key)
        if _smoke_from_cache:
            smoke_status = str(_cached_smoke.get("status") or "skipped")
            smoke_reason = str(_cached_smoke.get("reason") or "")
            _trace(cursor, "attended.smoke_skip_idempotent", {
                "stage_id": cursor.get("stage_id"),
                "call_key": call_key,
                "smoke_status": smoke_status,
                "reason": "smoke_result_for marker matches — reusing persisted smoke outcome",
            })
        else:
            smoke_status, smoke_reason = _run_smoke(
                dispatcher,
                project_path=work_path,
                stage_id=cursor["stage_id"])
            # GAP-a2 / Rider (a): compare against the baseline failures.
            # If every currently-failing test was ALREADY failing before the
            # engineer's change, the smoke failure is not attributable to this
            # change — excuse it.
            # Finding 6: bound the excusable set to prevent real regressions being
            # silently blessed by an overly broad baseline.
            if smoke_status == "fail":
                _captured_baseline = list(cursor.get("baseline_failures") or [])
                _env_bl_raw = os.environ.get("HYDRA_SMOKE_BASELINE_TESTS", "")
                _env_allowlist: set[str] | None = (
                    {t.strip() for t in _env_bl_raw.split(",") if t.strip()}
                    if _env_bl_raw else None
                )
                # Build excusable set:
                #  - env var present + captured non-empty → intersection (tightest bound)
                #  - env var present + captured empty → env var alone (legacy fallback)
                #  - env var absent → captured baseline alone
                _captured_set = set(_captured_baseline)
                if _env_allowlist is not None:
                    _excusable = ((_captured_set & _env_allowlist) if _captured_set
                                  else _env_allowlist)
                else:
                    _excusable = _captured_set

                if _excusable:
                    _max_excuse = int(os.environ.get("HYDRA_SMOKE_BASELINE_MAX", "10"))
                    if len(_excusable) > _max_excuse:
                        # Baseline too broad — refuse to excuse; treat as real failure.
                        _trace(cursor, "attended.smoke.baseline_too_broad", {
                            "stage_id": cursor.get("stage_id"),
                            "excusable_count": len(_excusable),
                            "max": _max_excuse,
                        })
                        smoke_reason = (
                            f"smoke: baseline too broad ({len(_excusable)} excusable "
                            f"tests > HYDRA_SMOKE_BASELINE_MAX={_max_excuse}); "
                            "treating as real failure"
                        )
                    else:
                        import sys as _sys
                        try:
                            _reruns = subprocess.run(
                                [_sys.executable, "-m", "pytest",
                                 "tests/", "--no-header", "-q", "--tb=no"],
                                cwd=work_path,
                                capture_output=True, text=True, check=False,
                                timeout=_baseline_timeout_s(),
                            )
                            _current_failing = _parse_failing_tests(
                                _reruns.stdout + "\n" + _reruns.stderr)
                        except Exception:  # noqa: BLE001
                            _current_failing = set()
                        _excused = _current_failing & _excusable
                        _new_failures = _current_failing - _excusable
                        # Always emit telemetry about excused failures (Finding 6).
                        if _current_failing or _excused:
                            _trace(cursor, "attended.smoke.baseline_excuse_decision", {
                                "stage_id": cursor.get("stage_id"),
                                "current_failing": sorted(_current_failing),
                                "excused": sorted(_excused),
                                "new_failures": sorted(_new_failures),
                                "excusable_set_size": len(_excusable),
                            })
                        if not _new_failures:
                            smoke_status = "pass"
                            smoke_reason = (
                                f"smoke: {len(_current_failing)} failure(s) all "
                                f"pre-existed in baseline ({len(_excused)} excused); "
                                "treated as pass"
                            )
            try:
                _raise_on_error_payload(
                    cm("pp_harness", "record_smoke_status", {
                        "stage_id": cursor["stage_id"], "candidate_index": 1,
                        "status": smoke_status,
                        "reason": (smoke_reason or "attended drive smoke")[:300],
                    }, squad_id=_SQ),
                    "record_smoke_status",
                )
            except Exception:  # noqa: BLE001
                pass
            # Fix-1b: persist smoke outcome before _finalize so a timeout between
            # here and the outer save_cursor does not restart the smoke on retry.
            if call_key is not None and cursor_file is not None:
                cursor["smoke_result_for"] = {
                    "call_key": call_key,
                    "status": smoke_status,
                    "reason": smoke_reason,
                }
                save_cursor(cursor_file, cursor)
        passed = smoke_status == "pass"
    cursor["smoke_status"] = smoke_status
    cursor["smoke_reason"] = smoke_reason

    # Honour pp's finalize-readiness gate (same auto-resolved deferrals as the
    # headless loop).
    if passed:
        try:
            rd = _pp_inner(_raise_on_error_payload(
                cm("pp_harness", "get_stage_finalize_readiness",
                   {"stage_id": cursor["stage_id"]}, squad_id=_SQ),
                "get_stage_finalize_readiness",
            ))
        except Exception:  # noqa: BLE001
            rd = {}
        if rd.get("can_pass") is False:
            na = rd.get("next_action") or "not_ready"
            _auto_resolved = {"run_artifact_validate", "run_tdd_pre_check",
                              "run_tdd_post_check", "record_smoke_or_assertion"}
            if na not in _auto_resolved:
                passed = False
                cursor["outcome"] = "surfaced"
                cursor["error"] = f"pp readiness: not ready (next_action={na})"

    _finalize(dispatcher, cursor, passed=passed, gen_failed=False)


def _finalize(dispatcher: Dispatcher, cursor: dict[str, Any], *,
              passed: bool, gen_failed: bool) -> None:
    """Finalize the stage + run and set the terminal cursor state. Mirrors the
    headless loop's downgrade-honouring finalize_run handling.

    F26+M8: a finalize_stage RPC failure on a passing stage downgrades to
    surfaced — we never proceed to finalize_run 'complete' with an un-recorded
    stage.
    Finding 4: worktree merge happens BEFORE finalize_run so a merge failure
    can downgrade finalize_run to 'surfaced' truthfully (previously the run
    was finalized 'complete' and only the cursor reflected the merge failure).
    F30: abort/error reason is included in summary_md (FinalizeRunSchema strips
    standalone `reason` / `project_path` keys).
    """
    cm = dispatcher.call_mcp
    stage_id = cursor["stage_id"]
    run_id = cursor["run_id"]
    attempt_id = cursor.get("attempt_id")

    # F26+M8: capture finalize_stage success; failure on pass → downgrade.
    # LV-1: _raise_on_error_payload converts error dicts into a RuntimeError
    # (PPLedgerError) so the existing except fires for payload-level
    # rejections/failures too -- and it preserves the original dict as
    # ``.payload``, which is what venom-gate classification downstream reads
    # to tell a structural rejection from a transport-shaped failure.
    _finalize_stage_ok = True
    try:
        _raise_on_error_payload(
            cm("pp_harness", "finalize_stage", {
                "stage_id": stage_id,
                "status": "passed" if passed else "surfaced",
                **({"winner_attempt_id": attempt_id} if (passed and attempt_id) else {}),
            }, squad_id=_SQ),
            "finalize_stage",
        )
    except Exception:  # noqa: BLE001
        _finalize_stage_ok = False
    if passed and not _finalize_stage_ok:
        passed = False
        cursor["outcome"] = "surfaced"
        cursor["error"] = (cursor.get("error") or "") + \
            " finalize_stage RPC failed; stage downgraded to surfaced"

    # Finding 4: merge the worktree BEFORE calling finalize_run so a merge
    # failure can truthfully downgrade the run to 'surfaced'.  Previously the
    # order was finalize_run(complete) → merge → cursor surfaced, which left the
    # pp ledger claiming 'complete' while no code actually landed.
    worktree_path = cursor.get("worktree_path")
    repo_root = cursor.get("repo_root")
    branch = cursor.get("branch")
    if worktree_path and repo_root and branch:
        if passed:
            merge = _merge_worktree_back(repo_root, worktree_path, branch)
            cursor["merge"] = merge
            if not merge.get("merged"):
                # Merge failed — surface the run so the operator knows code
                # did not land, and pass that truth to finalize_run below.
                # NEW: downgrade cursor['outcome'] so step_result / summary
                # report 'pass_unlanded' rather than 'pass' on a surfaced run.
                passed = False
                cursor["outcome"] = "pass_unlanded"
                cursor["error"] = (cursor.get("error") or "") + \
                    f" merge-back failed: {merge.get('error')}"
                # MU12: _merge_worktree_back already committed any uncommitted
                # work to the branch before attempting the merge — advertise the
                # branch so the operator can pick it up without a new commit.
                cursor["preserved_branch"] = branch
                _trace(cursor, "attended.preserved",
                       {"branch": branch, "run_id": run_id,
                        "via": "merge_helper_commit"})
        else:
            cursor["merge"] = {"merged": False, "error": "discarded_non_complete"}
            # MU12: commit any engineer changes to the attended branch BEFORE
            # removing the worktree so the operator can pick them up.  The
            # complete path is handled by _merge_worktree_back above; this
            # preserves work on non-complete outcomes (smoke-fail, judge-fail,
            # generate-fail).
            _preserve_non_complete_work(cursor, worktree_path, branch, run_id,
                                        final_status="surfaced")
        _remove_worktree(repo_root, worktree_path)

    # F30: build summary_md that embeds any error/abort reason.
    if gen_failed:
        summary = f"Attended drive: generate failed -- {cursor.get('error')}"
    elif passed:
        summary = (f"Attended drive: stage_outcome=pass; "
                   f"smoke={cursor.get('smoke_status')}.")
    else:
        extra = f" :: {cursor['error']}" if cursor.get("error") else ""
        summary = (f"Attended drive: stage_outcome={cursor.get('outcome')}; "
                   f"smoke={cursor.get('smoke_status')}{extra}.")

    fin = cm("pp_harness", "finalize_run", {
        "run_id": run_id,
        "status": "complete" if passed else "surfaced",
        "summary_md": summary,
    }, squad_id=_SQ)
    fin_inner = _pp_inner(fin)
    fin_status = fin_inner.get("effective_status") or fin_inner.get("status")
    fin_downgraded = bool(fin_inner.get("downgraded"))
    if passed and _pp_ok(fin) and not fin_downgraded \
            and fin_status not in {"surfaced", "failed", "aborted", "blocked"}:
        cursor["final_status"] = "complete"
        cursor["state"] = "complete"
    else:
        cursor["final_status"] = "surfaced"
        cursor["state"] = "surfaced"

    cursor["pending_action"] = None
    cursor["finalized"] = True
    # Marker 2: clear the run-scoped sentinel now that the stage is terminal —
    # a later, unrelated session must not inherit a stale bypass.
    _clear_stage_active_sentinel(cursor.get("project_root") or cursor["project_path"])
    # Rider (b): initialise the charged flag to False. _cmd_attended_submit sets
    # it to True after the first budget charge so retried submit calls don't
    # double-charge (the already_charged field in _step_result exposes this flag).
    cursor.setdefault("charged", False)
    _trace(cursor, "attended.finalized", {
        "stage_id": stage_id, "final_status": cursor["final_status"],
        "smoke_status": cursor.get("smoke_status"), "cost_usd": cursor.get("cost_usd"),
        "merged": (cursor.get("merge") or {}).get("merged"),
    })


def begin_squad_stage(
    *,
    workflow_id: str,
    task_id: str,
    squad_slug: str,
    entrypoint: str,
    lead_agent: str,
    pack_cwd: str,
    request_text: str,
    project_root: str | Path,
) -> dict[str, Any]:
    """Create a lightweight cursor for an attended non-engineering squad task
    (claude-skill or agent-impersonation entrypoint).

    No pp stage is opened; no worktree isolation is needed (these squads produce
    documents, not engine code). The cursor lives at
    ``cursor_path(project_root, workflow_id, task_id)`` so the submit-host-result
    CLI can find it by passing ``--run-id <task_id>``.

    Returns an ``awaiting_host`` step result whose ``host_action`` tells the host
    to spawn the visible pack agent subagent in ``pack_cwd``.
    """
    call_key = f"squad-{task_id}-0"
    cursor: dict[str, Any] = {
        "schema": CURSOR_SCHEMA,
        "kind": "squad",
        "workflow_id": workflow_id,
        "task_id": task_id,
        "run_id": task_id,   # mirrors engineering cursor shape for CLI compatibility
        "squad_slug": squad_slug,
        "entrypoint": entrypoint,
        "project_path": pack_cwd,
        "request_text": request_text,
        "state": "await_squad_agent",
        "cost_usd": 0.0,
        "tokens_in": 0,
        "tokens_out": 0,
        "final_status": None,
        "error": None,
        "finalized": False,
        "pending_action": {
            "call_key": call_key,
            "agent_type": lead_agent,
            "cwd": pack_cwd,
            "prompt": request_text,
            "instructions": "Run the pack agent and submit the artifact.",
        },
    }
    cfile = cursor_path(project_root, workflow_id, task_id)
    save_cursor(cfile, cursor)
    _trace(cursor, "attended.squad_stage_started", {
        "squad_slug": squad_slug,
        "entrypoint": entrypoint,
        "task_id": task_id,
    })
    return _step_result(cursor, cfile)


def _apply_squad_result(cursor: dict[str, Any], result: dict[str, Any]) -> None:
    """await_squad_agent → terminal.

    Accumulate spend from the host agent's result and mark the cursor complete.
    There are no pp ledger calls (this is not an engineering stage). The CLI
    (``_cmd_attended_submit``) charges the returned cost_usd through the
    authoritative HydraState budget path after this returns.
    """
    cursor["cost_usd"] = (float(cursor.get("cost_usd") or 0.0)
                          + float(result.get("cost_usd") or 0.0))
    cursor["tokens_in"] = (int(cursor.get("tokens_in") or 0)
                           + int(result.get("tokens_in") or 0))
    cursor["tokens_out"] = (int(cursor.get("tokens_out") or 0)
                            + int(result.get("tokens_out") or 0))
    cursor["artifact_text"] = str(result.get("text") or result.get("artifact") or "")
    # Native pack results may delegate typed work to another squad.  Keep the
    # raw list in the cursor so the CLI can validate/redact/ingest it under the
    # workflow lock; never silently discard a DEV_TASK or CREATIVE_BRIEF.
    emitted = result.get("emitted_envelopes", result.get("envelopes", []))
    cursor["emitted_envelopes"] = emitted if isinstance(emitted, list) else []
    cursor["final_status"] = "complete"
    cursor["state"] = "complete"
    cursor["pending_action"] = None
    cursor["finalized"] = True
    _trace(cursor, "attended.squad_result_applied", {
        "task_id": cursor.get("task_id"),
        "squad_slug": cursor.get("squad_slug"),
        "cost_usd": cursor.get("cost_usd"),
        "emitted_envelope_count": len(cursor["emitted_envelopes"]),
    })


def recover_stalled_stage(dispatcher: Dispatcher, *,
                          cursor_file: str | Path) -> dict[str, Any]:
    """W2-4: sanctioned recovery for an engineering stage stranded by a
    transport-shaped pp-ledger failure.

    Reachable ONLY via ``hydra resume --action recover-stalled-stage`` — never
    a parallel CLI verb. Governance is explicit that a paused/stranded
    workflow resumes only through approve/resume, so this is exposed as a
    resume action rather than a standalone command (see ``_cmd_resume_locked``
    in cli.py).

    Handles two cursor shapes:

    - ``state == "stalled_infra"`` (the W2-3 hold): the isolated worktree is
      still on disk and the pp attempt is still open. Only ``record_verdict``
      was skipped; everything downstream (smoke, merge, finalize) reuses the
      existing ``_finalize`` machinery unchanged, so recovery for this shape
      exercises the SAME code path a normal pass finalize does.
    - ``state == "surfaced"`` (an older cursor stranded by the pre-fix
      downgrade-then-finalize behavior): the worktree is already gone, but
      the branch that hosted the stage's work (``cursor["branch"]``, set once
      at worktree creation and never cleared) still carries every commit the
      engineer made. If there was ALSO uncommitted work when the worktree was
      torn down, ``_preserve_non_complete_work`` committed it and recorded
      ``cursor["preserved_branch"]``; that is preferred when present. When
      the engineer committed everything cleanly, ``preserved_branch`` is
      never set (there was nothing uncommitted to preserve) even though the
      branch is just as recoverable, so recovery falls back to
      ``cursor["branch"]`` after confirming it still exists in the repo --
      refusing recovery in that case would invert the incentive (the tidier
      the engineer, the less recoverable the stage). Recovery re-issues
      record_verdict, merges the resolved branch directly via
      ``_merge_branch_back``, and (best-effort) re-finalizes. Which branch
      was used, and whether it came from ``preserved_branch`` or the
      fallback, is recorded on the cursor (``recovery_branch``,
      ``recovery_branch_source``) and in the trace
      (``attended.recovery.branch_resolved``) so an operator can tell what
      was actually merged.

    Idempotent: replays ``record_verdict`` with the payload's
    ``idempotency_token`` (== the original judge call_key), so pp returns the
    already-recorded verdict_id on a repeat call instead of a duplicate row.
    Never double-charges: this function does not touch budget at all — the
    caller reads ``already_charged`` off the returned step result exactly as
    ``submit_host_result`` callers do and only calls ``charge_and_gate`` /
    ``mark_charged`` when it is False, so a stage that was already charged (the
    only way that can happen for the pre-fix "surfaced" shape, since its
    original submit charged on the downgraded outcome before this fix existed)
    is never charged a second time.
    """
    cm = dispatcher.call_mcp
    cursor = load_cursor(cursor_file)
    if cursor.get("kind") not in (None, "engineering"):
        return {"ok": False, "error": "recovery only supports engineering stage cursors"}
    state = cursor.get("state")
    if state not in ("stalled_infra", "surfaced"):
        return {"ok": False, "error": f"cursor state {state!r} is not recoverable"}

    # Resolve which branch to recover from. `preserved_branch` is set only by
    # `_preserve_non_complete_work`/the merge-failure path, which run when
    # there was UNCOMMITTED work to rescue -- an engineer who committed
    # everything cleanly leaves it unset even though `cursor["branch"]` (set
    # once at worktree creation and never cleared) still names a real branch
    # with every commit. Refusing recovery in that case inverts the
    # incentive (the tidier the engineer, the less recoverable the stage), so
    # fall back to `cursor["branch"]` -- but only after confirming the branch
    # actually exists; a stale/garbage-collected branch name must still
    # refuse cleanly rather than hand a bogus ref to git merge downstream.
    recovery_branch: str | None = None
    recovery_branch_source: str | None = None
    if state == "surfaced":
        preserved = cursor.get("preserved_branch")
        if preserved:
            recovery_branch = preserved
            recovery_branch_source = "preserved_branch"
        else:
            fallback = cursor.get("branch")
            fallback_repo_root = cursor.get("repo_root") or cursor.get("project_path")
            if fallback and fallback_repo_root:
                chk = _git(["rev-parse", "--verify", fallback], fallback_repo_root)
                if chk.returncode == 0:
                    recovery_branch = fallback
                    recovery_branch_source = "branch_fallback"
        if not recovery_branch:
            return {"ok": False, "error": (
                "surfaced cursor has no preserved_branch and no recoverable "
                "branch (cursor['branch'] is absent or no longer exists in "
                "the repo) to recover from")}
        cursor["recovery_branch"] = recovery_branch
        cursor["recovery_branch_source"] = recovery_branch_source
        _trace(cursor, "attended.recovery.branch_resolved", {
            "branch": recovery_branch, "source": recovery_branch_source,
        })

    stage_id = cursor.get("stage_id")
    attempt_id = cursor.get("attempt_id")
    payload = cursor.get("pending_verdict_payload")

    # Step 1: re-issue record_verdict if it was never recorded. Idempotent via
    # the payload's idempotency_token (see the comment where it is built).
    if not cursor.get("verdict_recorded_for"):
        if not (payload and attempt_id):
            return {"ok": False, "error": (
                "no pending_verdict_payload captured on this cursor -- cannot "
                "safely reconstruct the verdict. This cursor predates the "
                "W2-4 payload capture; it needs a manual pp-side replay.")}
        try:
            _raise_on_error_payload(
                cm("pp_harness", "record_verdict", payload, squad_id=_SQ),
                "record_verdict",
            )
        except Exception as exc:  # noqa: BLE001
            _trace(cursor, "attended.recovery.verdict_failed", {
                "stage_id": stage_id, "attempt_id": attempt_id, "reason": str(exc),
            })
            return {"ok": False, "error": f"record_verdict recovery failed: {exc}"}
        cursor["verdict_recorded_for"] = payload.get("idempotency_token") or "recovery"
        _trace(cursor, "attended.recovery.verdict_recorded", {
            "stage_id": stage_id, "attempt_id": attempt_id,
        })
        save_cursor(cursor_file, cursor)

    outcome = (payload.get("outcome") if payload else None) or cursor.get("outcome")

    if state == "stalled_infra":
        # The worktree + pp attempt are exactly as they were when the stage
        # stalled -- everything past record_verdict is the SAME code the
        # normal (non-stranded) path runs, so reuse it verbatim instead of
        # re-implementing smoke/merge/finalize here.
        passed = False
        if outcome == "pass" and attempt_id:
            work_path = cursor.get("work_path") or cursor["project_path"]
            smoke_status, smoke_reason = _run_smoke(
                dispatcher, project_path=work_path, stage_id=stage_id)
            cursor["smoke_status"] = smoke_status
            cursor["smoke_reason"] = smoke_reason
            try:
                _raise_on_error_payload(cm("pp_harness", "record_smoke_status", {
                    "stage_id": stage_id, "candidate_index": 1,
                    "status": smoke_status,
                    "reason": (smoke_reason or "recovery smoke")[:300],
                }, squad_id=_SQ), "record_smoke_status")
            except Exception:  # noqa: BLE001
                pass
            passed = smoke_status == "pass"
        _trace(cursor, "attended.recovery.resuming_finalize", {
            "stage_id": stage_id, "outcome": outcome, "passed": passed,
        })
        _finalize(dispatcher, cursor, passed=passed, gen_failed=False)
        save_cursor(cursor_file, cursor)
        out = _step_result(cursor, cursor_file)
        out["ok"] = True
        return out

    # state == "surfaced": worktree is gone; merge directly from the
    # resolved branch (preserved_branch, or the branch_fallback resolved
    # above), then best-effort re-finalize.
    repo_root = cursor.get("repo_root") or cursor.get("project_path")
    branch = recovery_branch
    merge = _merge_branch_back(repo_root, branch)
    cursor["merge"] = merge
    _trace(cursor, "attended.recovery.merge", {
        "stage_id": stage_id, "branch": branch, "merged": merge.get("merged"),
        "error": merge.get("error"),
    })
    if not merge.get("merged"):
        save_cursor(cursor_file, cursor)
        out = _step_result(cursor, cursor_file)
        out["ok"] = False
        if merge.get("error") == "already_merged":
            # State-shaped, not failure-shaped: the branch's work is
            # ALREADY present in repo_root (git reported "Already up to
            # date." -- no new commit was needed or created). That is not
            # "recovery failed to land the work"; it is "there was nothing
            # left for recovery to land". Still ok=False -- recovery itself
            # did not run smoke/re-finalize here, so the caller must not
            # treat this as a completed pass -- but the wording must not
            # read as "work missing" when the opposite is true.
            out["error"] = (
                "recovery found no merge to perform: the branch's work is "
                "already present in repo_root (already_merged) -- no new "
                "merge commit was needed or created"
            )
        else:
            out["error"] = f"recovery merge failed: {merge.get('error')}"
        return out

    if outcome == "pass" and cursor.get("smoke_status") not in ("pass", "fail"):
        smoke_status, smoke_reason = _run_smoke(
            dispatcher, project_path=repo_root, stage_id=stage_id)
        cursor["smoke_status"] = smoke_status
        cursor["smoke_reason"] = smoke_reason
        try:
            _raise_on_error_payload(cm("pp_harness", "record_smoke_status", {
                "stage_id": stage_id, "candidate_index": 1,
                "status": smoke_status,
                "reason": (smoke_reason or "recovery smoke (post-merge)")[:300],
            }, squad_id=_SQ), "record_smoke_status")
        except Exception:  # noqa: BLE001
            pass

    # The original (pre-fix) submit already called finalize_stage/finalize_run
    # with status="surfaced" once, leaving the pp run-level record permanently
    # "surfaced" even though this recovery may now find the stage passing.
    # Re-finalizing must be explicit and best-effort: report the outcome
    # honestly rather than silently claiming "complete" on the cursor while
    # pp's ledger still disagrees. finalizeRun in pp's daemon
    # (daemon/src/orchestrator/runs.ts) has no already-finalized guard -- it
    # unconditionally re-runs the full finalize procedure (gates, DB write,
    # master-plan patch) against whatever the stage rows say right now, so a
    # second call is safe and is exactly what reconciles the two ledgers.
    passed = outcome == "pass" and cursor.get("smoke_status") == "pass"

    # The merge above ran before the outcome was known (justified: in this
    # legacy "surfaced" shape the worktree is already gone, so repo_root is
    # the only place smoke can inspect the code). Now that the outcome IS
    # known, a non-passing recovery must not silently retain the merged
    # code -- that is the exact divergence class (repo has code no system of
    # record acknowledges) this workstream exists to close, in the more
    # dangerous direction of failing code landing quietly. Revert the merge
    # commit this recovery itself created; if the revert itself fails, make
    # the landed-but-unacknowledged state unmissable instead of pretending a
    # clean revert happened.
    revert: dict[str, Any] | None = None
    if not passed and merge.get("merged") and merge.get("sha"):
        revert = _revert_merge_commit(
            repo_root, merge["sha"], expected_base=merge.get("base"))
        cursor["merge"]["reverted"] = bool(revert.get("reverted"))
        if revert.get("reverted"):
            cursor["merge"]["revert_sha"] = revert.get("sha")
        else:
            cursor["merge"]["revert_error"] = revert.get("error")
            cursor["merge"]["abort_failed"] = bool(revert.get("abort_failed"))
            cursor["merge"]["abort_state"] = revert.get("abort_state")
            if revert.get("abort_state") == "unknown":
                cursor["error"] = (cursor.get("error") or "") + (
                    f"; recovery merge {merge['sha']} landed in {repo_root} on "
                    f"branch checked out there, outcome={outcome!r} "
                    f"smoke={cursor.get('smoke_status')!r} did not pass, the "
                    f"automatic revert failed, and repo_root's post-abort "
                    f"state could NOT be verified ({revert.get('error')}) -- "
                    "code is MERGED INTO THE REPO, UNACKNOWLEDGED by the pp "
                    "ledger, and whether repo_root is clean or mid-revert is "
                    "UNKNOWN (not confirmed clean); operator must inspect "
                    "repo_root's full state before any retry."
                )
            elif revert.get("abort_failed"):
                cursor["error"] = (cursor.get("error") or "") + (
                    f"; recovery merge {merge['sha']} landed in {repo_root} on "
                    f"branch checked out there, outcome={outcome!r} "
                    f"smoke={cursor.get('smoke_status')!r} did not pass, the "
                    f"automatic revert failed AND its abort also failed "
                    f"({revert.get('error')}) -- code is MERGED INTO THE REPO, "
                    "UNACKNOWLEDGED by the pp ledger, and repo_root is left "
                    "mid-revert (not cleanly restored); operator must inspect "
                    "repo_root's full state (conflicted index / half-applied "
                    "working tree) before any retry, not just revert manually."
                )
            else:
                cursor["error"] = (cursor.get("error") or "") + (
                    f"; recovery merge {merge['sha']} landed in {repo_root} on "
                    f"branch checked out there, but outcome={outcome!r} "
                    f"smoke={cursor.get('smoke_status')!r} did not pass and the "
                    f"automatic revert failed ({revert.get('error')}) -- code is "
                    "MERGED INTO THE REPO but UNACKNOWLEDGED by the pp ledger; "
                    "repo_root itself was cleanly restored (abort succeeded); "
                    "operator must inspect repo_root and revert manually."
                )
        _trace(cursor, "attended.recovery.merge_reverted", {
            "stage_id": stage_id, "branch": branch,
            "merge_sha": merge.get("sha"), "reverted": revert.get("reverted"),
            "revert_error": revert.get("error"),
            "abort_failed": bool(revert.get("abort_failed")),
            "abort_state": revert.get("abort_state"),
        })

    try:
        _raise_on_error_payload(cm("pp_harness", "finalize_stage", {
            "stage_id": stage_id,
            "status": "passed" if passed else "surfaced",
            **({"winner_attempt_id": attempt_id} if (passed and attempt_id) else {}),
        }, squad_id=_SQ), "finalize_stage")
        fin_stage_ok = True
    except Exception as exc:  # noqa: BLE001
        fin_stage_ok = False
        cursor["error"] = (cursor.get("error") or "") + f"; recovery finalize_stage failed: {exc}"
    if passed and not fin_stage_ok:
        passed = False

    # PP-VG-7 ordering: the stage row must already reflect "passed" (done
    # above) BEFORE finalize_run(complete) is requested, or pp's
    # surfaced-stages gate silently downgrades the run back to "surfaced" and
    # undoes this reconciliation. Replay finalize_run so the run-level record
    # matches reality instead of staying permanently stuck on the original
    # pre-fix "surfaced" write.
    summary = (f"Attended recovery: stage_outcome={outcome}; "
               f"smoke={cursor.get('smoke_status')}; "
               f"finalize_stage_ok={fin_stage_ok}.")
    if revert is not None:
        if revert.get("reverted"):
            summary += f" Merge {merge.get('sha')} reverted ({revert.get('sha')})."
        elif revert.get("abort_state") == "unknown":
            summary += (
                f" WARNING: merge {merge.get('sha')} landed in {repo_root}, the "
                f"automatic revert FAILED, and repo_root's post-abort state "
                f"could NOT be verified ({revert.get('error')}) -- code is "
                "merged, this stage did not pass, and whether repo_root is "
                "clean or mid-revert is UNKNOWN (not confirmed clean); "
                "operator must inspect repo_root's full state before any "
                "retry."
            )
        elif revert.get("abort_failed"):
            summary += (
                f" WARNING: merge {merge.get('sha')} landed in {repo_root}, the "
                f"automatic revert FAILED AND its abort also FAILED "
                f"({revert.get('error')}) -- code is merged, this stage did "
                "not pass, and repo_root is left mid-revert (conflicted "
                "index / half-applied working tree, not cleanly restored); "
                "operator must inspect repo_root's full state before any "
                "retry."
            )
        else:
            summary += (
                f" WARNING: merge {merge.get('sha')} landed in {repo_root} and "
                f"the automatic revert FAILED ({revert.get('error')}) -- code "
                "is merged but this stage did not pass; repo_root itself was "
                "cleanly restored (abort succeeded); operator must revert "
                "manually."
            )
    fin = cm("pp_harness", "finalize_run", {
        "run_id": cursor["run_id"],
        "status": "complete" if passed else "surfaced",
        "summary_md": summary,
    }, squad_id=_SQ)
    fin_inner = _pp_inner(fin)
    fin_status = fin_inner.get("effective_status") or fin_inner.get("status")
    fin_downgraded = bool(fin_inner.get("downgraded"))
    fin_run_ok = _pp_ok(fin)
    if not fin_run_ok:
        cursor["error"] = (cursor.get("error") or "") + \
            f"; recovery finalize_run did not report success: {fin!r}"

    # Never claim "complete" on the cursor unless pp's run-level record
    # actually agrees -- if finalize_run failed outright, or pp itself
    # downgraded (VG-7), or returned anything other than "complete", record
    # what pp actually holds instead of a divergent cursor claim. Mirrors
    # _finalize's downgrade-honouring check above.
    if passed and fin_run_ok and not fin_downgraded \
            and fin_status not in {"surfaced", "failed", "aborted", "blocked"}:
        cursor["final_status"] = "complete"
    else:
        cursor["final_status"] = "surfaced"
        if fin_downgraded:
            cursor["error"] = (cursor.get("error") or "") + \
                "; finalize_run downgraded complete->surfaced (PP-VG-7)"
    cursor["state"] = cursor["final_status"]
    cursor["pending_action"] = None
    cursor["finalized"] = True
    cursor.setdefault("charged", False)
    _trace(cursor, "attended.recovery.finalized", {
        "stage_id": stage_id, "final_status": cursor["final_status"],
        "merged": bool(merge.get("merged")),
        "merge_reverted": bool(revert.get("reverted")) if revert is not None else None,
        "finalize_stage_ok": fin_stage_ok,
        "finalize_run_ok": fin_run_ok, "finalize_run_status": fin_status,
        "finalize_run_downgraded": fin_downgraded,
    })
    save_cursor(cursor_file, cursor)
    out = _step_result(cursor, cursor_file)
    out["ok"] = True
    return out


def submit_host_result(
    dispatcher: Dispatcher,
    *,
    cursor_file: str | Path,
    call_key: str,
    result: dict[str, Any],
) -> dict[str, Any]:
    """Feed a host subagent's result back in and advance the cursor by exactly
    one transition. Idempotent on a stale/duplicate ``call_key`` (returns the
    current step result without re-applying), so a retried submit never
    double-records in the pp ledger.

    Handles both ``kind="engineering"`` (the default pp stage flow) and the
    lightweight ``kind="squad"`` cursors created by ``begin_squad_stage`` for
    non-engineering tasks (claude-skill / agent-impersonation).
    """
    cursor = load_cursor(cursor_file)
    state = cursor.get("state")
    if state in _TERMINAL:
        return _step_result(cursor, cursor_file)

    pending = cursor.get("pending_action") or {}
    expected_key = pending.get("call_key")
    if call_key != expected_key:
        # Duplicate / out-of-order submit — do not re-apply (exactly-once).
        out = _step_result(cursor, cursor_file)
        out["ignored"] = f"call_key {call_key!r} != expected {expected_key!r}"
        return out

    if state == "await_generate":
        _apply_generate(dispatcher, cursor, result)
    elif state in ("await_judge", "stalled_infra"):
        # W2-3: "stalled_infra" is a non-terminal hold state entered when a
        # transport-shaped record_verdict failure would otherwise have been
        # downgraded + finalized. Its pending_action.call_key is left
        # unchanged from the original judge step, so a re-issued
        # submit_host_result carrying the same call_key/result re-enters
        # _apply_judge here and retries record_verdict via the
        # idempotency_token — exactly-once even across the re-drive.
        _apply_judge(dispatcher, cursor, result,
                     cursor_file=cursor_file, call_key=call_key)
    elif state == "await_squad_agent":
        # Lightweight non-engineering squad flow — no pp protocol calls needed.
        _apply_squad_result(cursor, result)
    else:  # pragma: no cover — defensive
        cursor["state"] = "aborted"
        cursor["final_status"] = "aborted"
        cursor["error"] = f"unknown attended state {state!r}"

    save_cursor(cursor_file, cursor)
    return _step_result(cursor, cursor_file)


def mark_charged(cursor_file: str | Path) -> None:
    """Mark a terminal cursor as budget-charged (rider b idempotency guard).

    Called by _cmd_attended_submit immediately after charging the HydraState
    budget ledger. Subsequent submit calls that see ``already_charged=True``
    in the step result skip the charge, preventing double-billing on retried
    submit-host-result invocations.  Fail-soft: any I/O or schema error is
    silently ignored so a storage hiccup never blocks the calling workflow.
    """
    try:
        cursor = load_cursor(cursor_file)
        if cursor.get("state") in _TERMINAL:
            cursor["charged"] = True
            save_cursor(cursor_file, cursor)
    except Exception:  # noqa: BLE001 — never crash the caller on persist failure
        pass


def abort_stage(dispatcher: Dispatcher, *, cursor_file: str | Path,
                reason: str = "operator_abort") -> dict[str, Any]:
    """Best-effort abort: finalize the pp run ``aborted`` to release the lock and
    mark the cursor terminal. Never raises."""
    try:
        cursor = load_cursor(cursor_file)
    except Exception as e:  # noqa: BLE001
        return {"status": "aborted", "error": f"cursor_unreadable: {e}"}
    if cursor.get("state") in _TERMINAL:
        return _step_result(cursor, cursor_file)
    try:
        # F30: FinalizeRunSchema strips 'reason'/'project_path' — embed reason
        # in summary_md so it is never silently dropped.
        dispatcher.call_mcp("pp_harness", "finalize_run", {
            "run_id": cursor["run_id"], "status": "aborted",
            "summary_md": f"attended_abort: {reason}",
        }, squad_id=_SQ)
    except Exception:  # noqa: BLE001
        pass
    # Discard the isolated worktree (no merge on abort).
    worktree_path = cursor.get("worktree_path")
    repo_root = cursor.get("repo_root")
    if worktree_path and repo_root:
        _remove_worktree(repo_root, worktree_path)
        cursor["merge"] = {"merged": False, "error": "discarded_abort"}
    # Marker 2: clear the run-scoped sentinel on abort too, not just a clean
    # finalize — an aborted stage must not leave a stale bypass behind.
    _clear_stage_active_sentinel(cursor.get("project_root") or cursor.get("project_path"))
    cursor["state"] = "aborted"
    cursor["final_status"] = "aborted"
    cursor["error"] = reason
    cursor["pending_action"] = None
    save_cursor(cursor_file, cursor)
    return _step_result(cursor, cursor_file)
