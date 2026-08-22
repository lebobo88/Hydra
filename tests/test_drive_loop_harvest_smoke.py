"""Regression tests for the headless drive-loop's generate-classification,
host-side smoke runner, and working-tree harvest.

Covers the silent-stall fix: codex CAN edit the worktree under
``--sandbox workspace-write`` but CANNOT ``git commit`` or spawn child test
runners there. A "commit/test blocked" narration after a successful edit must
NOT be classified as a failed generate, the smoke must run outside the sandbox,
and the harness must land codex's working-tree edits itself.

No network, no LLMs. Git is invoked against throwaway temp repos.
"""
from __future__ import annotations

import subprocess

import pytest

from hydra_core import squad_node
from hydra_core.squad_node import (
    _detect_smoke_command,
    _generate_failure_reason,
    _run_smoke,
    _worktree_dirty_set,
    harvest_pp_run_artifacts,
)

_OK = {"status": "done", "result": {}}


# ─── _generate_failure_reason: diff-aware classification ───────────────────

def test_commit_blocked_after_write_is_not_a_failure():
    """codex narrates 'Permission denied' (commit step) AFTER writing code →
    not a generate failure, because the harness owns commit/smoke."""
    text = ("Implemented the fix. build passed, lint passed. Commit could not be "
            "created: Git cannot create .git/index.lock (Permission denied).")
    assert _generate_failure_reason(_OK, text, wrote_changes=True) is None


def test_marker_with_no_changes_is_a_failure():
    """Same narration but the run wrote NOTHING → a real (read-only) failure."""
    text = "writing is blocked by read-only sandbox; permission denied"
    reason = _generate_failure_reason(_OK, text, wrote_changes=False)
    assert reason is not None
    assert "permission denied" in reason.lower()


def test_timeout_fails_regardless_of_changes():
    reason = _generate_failure_reason(
        {"timeout": True, "error": "deadline"}, "", wrote_changes=True)
    assert reason is not None and "timed out" in reason


def test_empty_output_fails_regardless_of_changes():
    reason = _generate_failure_reason(_OK, "   ", wrote_changes=True)
    assert reason is not None and "no output" in reason


def test_clean_success_returns_none():
    assert _generate_failure_reason(
        _OK, "Changed signal.ts and hud.ts.", wrote_changes=True) is None


# ─── _run_smoke: host-side execution, exit code authoritative ──────────────

def test_smoke_pass_on_zero_exit(monkeypatch):
    # W2: _run_smoke resolves argv+cwd via _detect_smoke_command_and_cwd; patch
    # that seam (cwd == "." mirrors the pre-nested-root default).
    monkeypatch.setattr(
        squad_node, "_detect_smoke_command_and_cwd",
        lambda _p: (["python", "-c", "import sys; sys.exit(0)"], "."),
    )
    status, reason = _run_smoke(object(), project_path=".", stage_id="s1")
    assert status == "pass"
    assert "exit=0" in reason


def test_smoke_fail_on_nonzero_exit(monkeypatch):
    monkeypatch.setattr(
        squad_node, "_detect_smoke_command_and_cwd",
        lambda _p: (["python", "-c", "import sys; sys.exit(3)"], "."),
    )
    status, reason = _run_smoke(object(), project_path=".", stage_id="s1")
    assert status == "fail"
    assert "exit=3" in reason


def test_smoke_skipped_when_no_command(monkeypatch):
    monkeypatch.setattr(
        squad_node, "_detect_smoke_command_and_cwd", lambda _p: (None, "."))
    status, reason = _run_smoke(object(), project_path=".", stage_id="s1")
    assert status == "skipped"
    assert "no runnable" in reason


# ─── _detect_smoke_command ─────────────────────────────────────────────────

def test_detect_npm_test(tmp_path):
    (tmp_path / "package.json").write_text(
        '{"scripts": {"test": "vitest run", "build": "tsc"}}', encoding="utf-8")
    assert _detect_smoke_command(str(tmp_path)) == ["npm", "test", "--silent"]


def test_detect_npm_build_when_no_test(tmp_path):
    (tmp_path / "package.json").write_text(
        '{"scripts": {"build": "tsc"}}', encoding="utf-8")
    assert _detect_smoke_command(str(tmp_path)) == ["npm", "run", "build", "--silent"]


def test_detect_pytest(tmp_path):
    import sys
    (tmp_path / "pyproject.toml").write_text("[project]\nname='x'\n", encoding="utf-8")
    # F3: pytest is resolved via the running interpreter (bare `pytest` is often
    # not on PATH → FileNotFoundError at launch).
    assert _detect_smoke_command(str(tmp_path)) == [sys.executable, "-m", "pytest", "-q"]


def test_detect_none_for_empty_project(tmp_path):
    assert _detect_smoke_command(str(tmp_path)) is None


# ─── harvest_pp_run_artifacts: commit working-tree edits ───────────────────

def _git(root, *args):
    return subprocess.run(
        ["git", "-c", "user.name=t", "-c", "user.email=t@t", *args],
        cwd=root, capture_output=True, text=True, check=False,
    )


def _init_repo(root):
    _git(root, "init", "-q")
    (root / "src.ts").write_text("const a = 1;\n", encoding="utf-8")
    _git(root, "add", "-A")
    _git(root, "commit", "-q", "-m", "init")


def test_harvest_commits_only_run_scoped_paths(tmp_path):
    _init_repo(tmp_path)
    # Track a second file so we can leave an UNRELATED operator edit on it.
    (tmp_path / "other.ts").write_text("// tracked\n", encoding="utf-8")
    _git(tmp_path, "add", "other.ts")
    _git(tmp_path, "commit", "-q", "-m", "track other")

    # The run edits src.ts (codex could not commit it itself).
    (tmp_path / "src.ts").write_text("const a = 2; // fixed\n", encoding="utf-8")
    # The operator has an unrelated WIP edit open on other.ts — NOT this run's.
    (tmp_path / "other.ts").write_text("// operator's WIP edit\n", encoding="utf-8")
    # The run's archived metadata.
    hdir = tmp_path / ".harness" / "run_X"
    hdir.mkdir(parents=True)
    (hdir / "summary.md").write_text("ok", encoding="utf-8")

    sha = harvest_pp_run_artifacts(
        project_path=str(tmp_path), run_id="run_X", workflow_id="wf1",
        changed_paths=["src.ts"])

    assert sha
    committed = _git(tmp_path, "show", "--stat", "--name-only", "HEAD").stdout
    assert "src.ts" in committed          # the run's edit landed
    assert "other.ts" not in committed    # operator's unrelated WIP was NOT swept
    # operator's WIP is still uncommitted in the tree
    porcelain = _git(tmp_path, "status", "--porcelain").stdout
    assert "other.ts" in porcelain


def test_harvest_returns_none_when_nothing_to_commit(tmp_path):
    _init_repo(tmp_path)
    # No changed_paths and no archived dir → nothing to do, no commit.
    assert harvest_pp_run_artifacts(
        project_path=str(tmp_path), run_id="run_none", workflow_id="wf1",
        changed_paths=[]) is None


def test_harvest_fail_soft_on_non_git_dir(tmp_path):
    (tmp_path / "loose.txt").write_text("x", encoding="utf-8")
    assert harvest_pp_run_artifacts(
        project_path=str(tmp_path), run_id="r", workflow_id="wf1",
        changed_paths=["loose.txt"]) is None


# ─── _worktree_dirty_set ───────────────────────────────────────────────────

def test_worktree_dirty_set_reports_modified_paths(tmp_path):
    _init_repo(tmp_path)
    assert _worktree_dirty_set(str(tmp_path)) == set()
    (tmp_path / "src.ts").write_text("changed\n", encoding="utf-8")
    (tmp_path / "new.ts").write_text("brand new\n", encoding="utf-8")
    dirty = _worktree_dirty_set(str(tmp_path))
    assert "src.ts" in dirty and "new.ts" in dirty


def test_worktree_dirty_set_empty_on_non_git(tmp_path):
    assert _worktree_dirty_set(str(tmp_path)) == set()


# ─── F10: smoke infra_error classification ─────────────────────────────────
def test_run_smoke_launch_failure_is_infra_error(tmp_path, monkeypatch):
    import subprocess as _sp
    from hydra_core import squad_node
    (tmp_path / "pyproject.toml").write_text("[project]\nname='x'\n", encoding="utf-8")

    def _boom(*_a, **_k):
        raise FileNotFoundError(2, "The system cannot find the file specified")
    monkeypatch.setattr(squad_node.subprocess, "run", _boom)
    status, reason = squad_node._run_smoke(None, project_path=str(tmp_path), stage_id="s")
    assert status == "infra_error"  # NOT "skipped"


def test_run_smoke_eperm_exit_is_infra_error(tmp_path, monkeypatch):
    from hydra_core import squad_node
    (tmp_path / "pyproject.toml").write_text("[project]\nname='x'\n", encoding="utf-8")

    class _Res:
        returncode = 1
        stderr = "Error: spawn EPERM\n"
        stdout = ""
    monkeypatch.setattr(squad_node.subprocess, "run", lambda *_a, **_k: _Res())
    status, _ = squad_node._run_smoke(None, project_path=str(tmp_path), stage_id="s")
    assert status == "infra_error"  # started then crashed for infra reason, not a real fail


def test_run_smoke_genuine_test_failure_is_fail(tmp_path, monkeypatch):
    from hydra_core import squad_node
    (tmp_path / "pyproject.toml").write_text("[project]\nname='x'\n", encoding="utf-8")

    class _Res:
        returncode = 1
        stderr = "1 failed, 3 passed\n"
        stdout = ""
    monkeypatch.setattr(squad_node.subprocess, "run", lambda *_a, **_k: _Res())
    status, _ = squad_node._run_smoke(None, project_path=str(tmp_path), stage_id="s")
    assert status == "fail"  # real assertion failure stays a fail


def test_run_smoke_no_command_is_skipped(tmp_path):
    from hydra_core import squad_node
    status, _ = squad_node._run_smoke(None, project_path=str(tmp_path), stage_id="s")
    assert status == "skipped"  # genuinely nothing to run
