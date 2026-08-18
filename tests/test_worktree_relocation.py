"""WS3: attended worktrees relocate outside the target repo.

Covers:
  - `resolve_worktree_root` resolution order (HYDRA_WORKTREE_ROOT >
    AIAPP_BASE > sibling-of-repo_root fallback), always outside repo_root.
  - `_provision_worktree` actually lands under the resolved root, not
    `<repo_root>/.harness/worktrees/`.
  - `_remove_worktree` cleans an external (non-repo-root-nested) worktree.
  - `_capture_baseline_failures` resolves via repo_root (containment math
    doesn't break once the worktree moves outside repo_root — regression
    guard for the existing GAP-a2 contract).
  - The Hydra-side janitor (`sweep_stale_worktrees`): removes a
    terminal-cursor worktree, refuses a non-terminal or cursor-less one,
    and never deletes a branch.

Real git repos, real filesystem paths — no mocks for the git plumbing.
"""
from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

from hydra_core import host_bridge
from hydra_core.host_bridge import (
    CURSOR_SCHEMA,
    _find_attended_cursor,
    _provision_worktree,
    _remove_worktree,
    cursor_path,
    resolve_worktree_root,
    save_cursor,
    sweep_stale_worktrees,
)


def _git(args: list[str], cwd: Path) -> subprocess.CompletedProcess:
    return subprocess.run(["git", *args], cwd=str(cwd), capture_output=True,
                          text=True, check=False)


def _init_repo(path: Path) -> Path:
    path.mkdir(parents=True, exist_ok=True)
    _git(["init", "-q"], path)
    _git(["-c", "user.name=t", "-c", "user.email=t@t", "commit",
          "--allow-empty", "-q", "-m", "init"], path)
    return path


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch):
    monkeypatch.delenv("HYDRA_WORKTREE_ROOT", raising=False)
    monkeypatch.delenv("AIAPP_BASE", raising=False)


# ===========================================================================
# resolve_worktree_root
# ===========================================================================

def test_resolve_worktree_root_env_override(tmp_path, monkeypatch):
    repo_root = _init_repo(tmp_path / "myrepo")
    custom = tmp_path / "custom-wt-root"
    monkeypatch.setenv("HYDRA_WORKTREE_ROOT", str(custom))

    root = resolve_worktree_root(str(repo_root))

    assert root == custom / "myrepo"


def test_resolve_worktree_root_aiapp_base(tmp_path, monkeypatch):
    repo_root = _init_repo(tmp_path / "myrepo")
    monkeypatch.setenv("AIAPP_BASE", str(tmp_path))

    root = resolve_worktree_root(str(repo_root))

    assert root == tmp_path / ".hydra-worktrees" / "myrepo"


def test_resolve_worktree_root_sibling_fallback_no_env(tmp_path):
    repo_root = _init_repo(tmp_path / "myrepo")

    root = resolve_worktree_root(str(repo_root))

    assert root == tmp_path / ".hydra-worktrees" / "myrepo"
    # Never a subdirectory of repo_root.
    assert not str(root).startswith(str(repo_root))


# ===========================================================================
# _provision_worktree lands under the resolved root, not <repo>/.harness/
# ===========================================================================

def test_provision_worktree_not_under_repo_harness(tmp_path):
    repo_root = _init_repo(tmp_path / "myrepo")

    prov = _provision_worktree(str(repo_root), "run_abc123")
    assert prov is not None
    wt_path, branch = prov
    try:
        expected_root = resolve_worktree_root(str(repo_root))
        assert Path(wt_path).parent == expected_root
        assert Path(wt_path) == expected_root / "attended-run_abc123"
        # REGRESSION GUARD: this is the exact bug being fixed — a worktree
        # nested inside the target repo pollutes any test-runner glob rooted
        # at repo_root (e.g. `**/tests/**`) with a stale duplicate suite.
        assert not (repo_root / ".harness" / "worktrees").exists(), (
            "no .harness/worktrees/ may be created inside the target repo"
        )
        assert branch == "attended/run_abc123"
    finally:
        _remove_worktree(str(repo_root), wt_path)


# ===========================================================================
# _remove_worktree cleans an external path
# ===========================================================================

def test_remove_worktree_cleans_external_path(tmp_path):
    repo_root = _init_repo(tmp_path / "myrepo")
    prov = _provision_worktree(str(repo_root), "run_ext1")
    assert prov is not None
    wt_path, _branch = prov
    assert Path(wt_path).is_dir()

    _remove_worktree(str(repo_root), wt_path)

    assert not Path(wt_path).exists()
    listing = _git(["worktree", "list", "--porcelain"], repo_root).stdout
    assert wt_path.replace("\\", "/") not in listing.replace("\\", "/")


# ===========================================================================
# _capture_baseline_failures containment math survives the relocation
# ===========================================================================

def test_capture_baseline_failures_uses_repo_root_for_relocated_worktree(
        tmp_path, monkeypatch):
    from hydra_core.host_bridge import _capture_baseline_failures
    from unittest.mock import MagicMock
    import subprocess as _sp

    repo_root = tmp_path / "myrepo"
    (repo_root / "tests").mkdir(parents=True)
    # Worktree now lives OUTSIDE repo_root entirely (sibling tree), unlike the
    # old <repo>/.harness/worktrees/attended-X nesting -- project_path.parent
    # would resolve to something unrelated to repo_root here.
    worktree = tmp_path / ".hydra-worktrees" / "myrepo" / "attended-runX"
    worktree.mkdir(parents=True)

    _mock_proc = MagicMock()
    _mock_proc.stdout = "FAILED tests/test_repo.py::test_r\n1 failed"
    _mock_proc.stderr = ""
    _mock_proc.returncode = 1
    monkeypatch.setattr(_sp, "run", lambda *a, **k: _mock_proc)

    result = _capture_baseline_failures(str(worktree), repo_root=str(repo_root))
    assert any("test_repo" in r for r in result), (
        "baseline must resolve via repo_root, not project_path.parent, once "
        "the worktree is relocated outside repo_root"
    )


# ===========================================================================
# Janitor: sweep_stale_worktrees
# ===========================================================================

def _cursor(state: str) -> dict:
    return {
        "schema": CURSOR_SCHEMA,
        "workflow_id": "wf-1",
        "run_id": "runA",
        "state": state,
    }


def test_janitor_removes_terminal_cursor_worktree(tmp_path):
    repo_root = _init_repo(tmp_path / "myrepo")
    project_root = repo_root
    prov = _provision_worktree(str(repo_root), "runA")
    assert prov is not None
    wt_path, branch = prov

    cfile = cursor_path(str(project_root), "wf-1", "runA")
    save_cursor(cfile, _cursor("complete"))

    report = sweep_stale_worktrees(str(repo_root), str(project_root))

    assert not Path(wt_path).exists(), "terminal-cursor worktree must be removed"
    assert len(report["removed"]) == 1
    assert report["removed"][0]["run_id"] == "runA"
    # Branch itself must survive -- only the worktree checkout is removed.
    branches = _git(["branch", "--list", branch], repo_root).stdout
    assert branch in branches, "janitor must never delete the branch"


def test_janitor_skips_non_terminal_cursor_worktree(tmp_path):
    repo_root = _init_repo(tmp_path / "myrepo")
    project_root = repo_root
    prov = _provision_worktree(str(repo_root), "runB")
    assert prov is not None
    wt_path, branch = prov
    try:
        cfile = cursor_path(str(project_root), "wf-1", "runB")
        save_cursor(cfile, _cursor("await_judge"))

        report = sweep_stale_worktrees(str(repo_root), str(project_root))

        assert Path(wt_path).exists(), (
            "non-terminal-cursor worktree (e.g. paused on HITL) must NOT be removed"
        )
        assert len(report["removed"]) == 0
        assert any(s["run_id"] == "runB" for s in report["skipped"])
    finally:
        _remove_worktree(str(repo_root), wt_path)


def test_janitor_skips_worktree_with_no_cursor(tmp_path):
    """A worktree observed mid-provision (cursor not yet written) must not be
    swept -- 'no cursor found' is 'cannot prove terminal', not 'safe to remove'."""
    repo_root = _init_repo(tmp_path / "myrepo")
    prov = _provision_worktree(str(repo_root), "runC")
    assert prov is not None
    wt_path, _branch = prov
    try:
        # Deliberately do NOT write a cursor file for runC.
        report = sweep_stale_worktrees(str(repo_root), str(repo_root))

        assert Path(wt_path).exists()
        assert len(report["removed"]) == 0
        assert any(s["run_id"] == "runC" and s["reason"] == "no_cursor_found"
                   for s in report["skipped"])
    finally:
        _remove_worktree(str(repo_root), wt_path)


def test_janitor_never_deletes_branch_even_when_removing(tmp_path):
    repo_root = _init_repo(tmp_path / "myrepo")
    prov = _provision_worktree(str(repo_root), "runD")
    assert prov is not None
    wt_path, branch = prov
    cfile = cursor_path(str(repo_root), "wf-1", "runD")
    save_cursor(cfile, _cursor("surfaced"))

    sweep_stale_worktrees(str(repo_root), str(repo_root))

    show = _git(["show-ref", "--verify", f"refs/heads/{branch}"], repo_root)
    assert show.returncode == 0, "branch ref must still exist after the sweep"
