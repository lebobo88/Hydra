"""E2-18 — `hydra-block-bash-writes.ps1` must honour `cd` before resolving a
relative write destination.

The bash write-guard resolved a relative redirection/heredoc target against the
payload's session cwd (`$json.cwd`) and ignored any `cd` / `pushd` /
`Set-Location` earlier in the SAME command. Inside a fix worktree,

    cd <.hydra-worktrees\\Hydra\\fix-X> && echo x >> tests/probe.py

therefore resolved to `<projectRoot>\\tests\\probe.py` — under the project root,
outside every worktree root — and was BLOCKED, while `hydra-block-direct-write.ps1`
ALLOWED the Edit tool on that very same file. The two guards that must stay in
lockstep disagreed.

These tests pin the effective-cwd rule: the LAST `cd`-style directory before the
write idiom wins, relative `cd` targets compose onto the previous effective cwd,
and every pre-existing block stays blocked.

Pwsh tests are gated on `pwsh`/`powershell` presence (skipif absent).
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
from pathlib import Path

import pytest

HYDRA_ROOT = Path(__file__).resolve().parents[1]
HOOKS_DIR = HYDRA_ROOT / "plugins" / "hydra" / "hooks"
HOOK = "hydra-block-bash-writes.ps1"

_PWSH = shutil.which("pwsh") or shutil.which("powershell")

pytestmark = pytest.mark.skipif(
    _PWSH is None, reason="pwsh/powershell not on PATH"
)


def _run_bash_hook(command: str, *, cwd: Path | None, project_dir: Path,
                   worktree_root: Path | None = None) -> subprocess.CompletedProcess:
    payload: dict = {"tool_name": "Bash", "tool_input": {"command": command}}
    if cwd is not None:
        payload["cwd"] = str(cwd)
    env = {**os.environ}
    env["HYDRA_ENFORCE_ROUTING"] = "1"
    env["CLAUDE_PROJECT_DIR"] = str(project_dir)
    env.pop("HYDRA_PP_STAGE_ACTIVE", None)
    if worktree_root is not None:
        env["HYDRA_WORKTREE_ROOT"] = str(worktree_root)
    else:
        env.pop("HYDRA_WORKTREE_ROOT", None)
    return subprocess.run(
        [_PWSH, "-NoProfile", "-File", str(HOOKS_DIR / HOOK)],
        input=json.dumps(payload),
        capture_output=True,
        text=True,
        env=env,
        timeout=30,
    )


@pytest.fixture()
def fake_tree(tmp_path: Path):
    """A project root plus a sibling `.hydra-worktrees` root, mirroring the real
    relocated layout (`<AIAPP_BASE>\\.hydra-worktrees\\<repo>\\<branch>`)."""
    project_dir = tmp_path / "Hydra"
    (project_dir / "tests").mkdir(parents=True)
    wt_root = tmp_path / ".hydra-worktrees"
    worktree = wt_root / "Hydra" / "fix-E2-18"
    (worktree / "tests").mkdir(parents=True)
    return project_dir, wt_root, worktree


def test_cd_into_worktree_allows_relative_redirect(fake_tree):
    """(1) `cd <worktree> && echo a >> tests/p.py` resolves under the worktree
    root and is ALLOWED — the exact command the Edit hook already permits."""
    project_dir, wt_root, worktree = fake_tree
    result = _run_bash_hook(
        f'cd {worktree} && echo a >> tests/p.py',
        cwd=project_dir,
        project_dir=project_dir,
        worktree_root=wt_root,
    )
    assert result.returncode == 0, (
        "cd into a worktree root must anchor the relative destination there; "
        f"rc={result.returncode}\nstdout={result.stdout}\nstderr={result.stderr}"
    )


def test_no_cd_relative_redirect_at_project_root_still_blocks(fake_tree):
    """(2) The same redirection WITHOUT a `cd` resolves under the project root
    and stays BLOCKED — the fix must not weaken the default."""
    project_dir, wt_root, _worktree = fake_tree
    result = _run_bash_hook(
        "echo a >> tests/p.py",
        cwd=project_dir,
        project_dir=project_dir,
        worktree_root=wt_root,
    )
    assert result.returncode == 2, (
        "a relative write under the project root must stay blocked; "
        f"rc={result.returncode}\nstdout={result.stdout}\nstderr={result.stderr}"
    )
    assert "BLOCKED" in result.stderr


def test_quoted_cd_path_with_spaces_allows_heredoc(fake_tree):
    """(3) A QUOTED `cd` target containing spaces is parsed, and the heredoc
    write that follows resolves under the worktree root — ALLOWED."""
    project_dir, wt_root, _worktree = fake_tree
    spaced = wt_root / "Hydra" / "fix with space"
    spaced.mkdir(parents=True)
    result = _run_bash_hook(
        f'cd "{spaced}" && cat > rel.ts <<EOF\nconst a = 1;\nEOF',
        cwd=project_dir,
        project_dir=project_dir,
        worktree_root=wt_root,
    )
    assert result.returncode == 0, (
        "a quoted cd path with spaces must be honoured; "
        f"rc={result.returncode}\nstdout={result.stdout}\nstderr={result.stderr}"
    )


def test_relative_cd_from_project_root_still_blocks(fake_tree):
    """(4) A RELATIVE `cd` composes onto the payload cwd — `cd tests` from the
    project root lands inside the project root and stays BLOCKED."""
    project_dir, wt_root, _worktree = fake_tree
    result = _run_bash_hook(
        "cd tests && echo a >> p.py",
        cwd=project_dir,
        project_dir=project_dir,
        worktree_root=wt_root,
    )
    assert result.returncode == 2, (
        "a relative cd inside the project root must not escape the guard; "
        f"rc={result.returncode}\nstdout={result.stdout}\nstderr={result.stderr}"
    )
    assert "BLOCKED" in result.stderr


def test_last_cd_before_the_write_wins(fake_tree):
    """`cd <worktree> ; cd <projectRoot>` — the LAST directory in force at the
    write position decides, so this must BLOCK despite the earlier worktree cd."""
    project_dir, wt_root, worktree = fake_tree
    result = _run_bash_hook(
        f'cd {worktree} ; cd {project_dir} ; echo a >> tests/p.py',
        cwd=project_dir,
        project_dir=project_dir,
        worktree_root=wt_root,
    )
    assert result.returncode == 2, (
        "the last cd before the redirection must win; "
        f"rc={result.returncode}\nstdout={result.stdout}\nstderr={result.stderr}"
    )
    assert "BLOCKED" in result.stderr


def test_cd_after_the_write_does_not_apply(fake_tree):
    """A `cd` that appears AFTER the write idiom must not retroactively
    relocate it — the write still resolves under the project root and BLOCKS."""
    project_dir, wt_root, worktree = fake_tree
    result = _run_bash_hook(
        f'echo a >> tests/p.py ; cd {worktree}',
        cwd=project_dir,
        project_dir=project_dir,
        worktree_root=wt_root,
    )
    assert result.returncode == 2, (
        "a trailing cd must not launder an earlier write; "
        f"rc={result.returncode}\nstdout={result.stdout}\nstderr={result.stderr}"
    )
    assert "BLOCKED" in result.stderr


def test_unresolvable_cd_target_fails_closed(fake_tree):
    """`cd $DEST` cannot be resolved from command text; the effective cwd stays
    where it was (the project root) and the write BLOCKS — fail closed."""
    project_dir, wt_root, _worktree = fake_tree
    result = _run_bash_hook(
        'cd "$DEST" && echo a >> tests/p.py',
        cwd=project_dir,
        project_dir=project_dir,
        worktree_root=wt_root,
    )
    assert result.returncode == 2, (
        "an unresolvable cd target must not be treated as an escape hatch; "
        f"rc={result.returncode}\nstdout={result.stdout}\nstderr={result.stderr}"
    )
    assert "BLOCKED" in result.stderr


def test_pushd_and_set_location_are_recognised(fake_tree):
    """`pushd` and `Set-Location` are honoured the same way as `cd`."""
    project_dir, wt_root, worktree = fake_tree
    for verb in ("pushd", "Set-Location"):
        result = _run_bash_hook(
            f'{verb} {worktree} && echo a >> tests/p.py',
            cwd=project_dir,
            project_dir=project_dir,
            worktree_root=wt_root,
        )
        assert result.returncode == 0, (
            f"{verb} must anchor the relative destination like cd does; "
            f"rc={result.returncode}\nstdout={result.stdout}\nstderr={result.stderr}"
        )


def test_absolute_out_of_root_target_still_blocks_after_cd(fake_tree):
    """A `cd` into a worktree must not launder an ABSOLUTE destination that
    lives outside every allowed root."""
    project_dir, wt_root, worktree = fake_tree
    outside = project_dir / "hydra_core" / "supervisor.py"
    outside.parent.mkdir(parents=True, exist_ok=True)
    result = _run_bash_hook(
        f'cd {worktree} && echo a >> "{outside}"',
        cwd=project_dir,
        project_dir=project_dir,
        worktree_root=wt_root,
    )
    assert result.returncode == 2, (
        "an absolute in-project destination must stay blocked regardless of cd; "
        f"rc={result.returncode}\nstdout={result.stdout}\nstderr={result.stderr}"
    )
    assert "BLOCKED" in result.stderr


def test_relative_dest_without_payload_cwd_still_blocks(fake_tree):
    """No `cwd` in the payload means UNANCHORED: even with a `cd` in the
    command the destination falls through to the plain extension block."""
    project_dir, wt_root, worktree = fake_tree
    result = _run_bash_hook(
        f'cd {worktree} && echo a >> tests/p.py',
        cwd=None,
        project_dir=project_dir,
        worktree_root=wt_root,
    )
    assert result.returncode == 2, (
        "without a reported cwd the destination must fail closed; "
        f"rc={result.returncode}\nstdout={result.stdout}\nstderr={result.stderr}"
    )
    assert "BLOCKED" in result.stderr


# ---------------------------------------------------------------------------
# Static lockstep pin (no pwsh required for the assertion itself)
# ---------------------------------------------------------------------------


def test_both_hooks_document_the_effective_cwd_rule():
    """The lockstep comment block must mention the cd rule in BOTH hooks, so a
    future editor of the Write/Edit guard knows its twin resolves cwd."""
    bash_hook = (HOOKS_DIR / HOOK).read_text(encoding="utf-8")
    edit_hook = (HOOKS_DIR / "hydra-block-direct-write.ps1").read_text(encoding="utf-8")
    assert "E2-18" in bash_hook and "cd" in bash_hook
    assert "E2-18" in edit_hook, (
        "hydra-block-direct-write.ps1 must carry the lockstep note about the "
        "Bash hook's cd/pushd/Set-Location resolution"
    )
