"""Tests for RA-1 (hook bypass hardening) and RA-9 (doctor WS-AUTH key probe).

RA-1 — Three PreToolUse enforcement hooks must only honor HYDRA_PP_STAGE_ACTIVE=1
       when a real active-stage filesystem marker exists. A bare env var (leaked or
       set outside a stage) must NOT silently bypass enforcement.

RA-9 — `hydra doctor` full mode must surface a WS-AUTH key probe line: OK when
       HYDRA_OPERATOR_KEY is configured (env or backends.json), WARN when absent.
       Never FAIL the doctor, never print the key, fail-soft on IO/parse errors.

Pwsh hook tests are gated on `pwsh`/`powershell` presence (skipif absent).
Python doctor tests run unconditionally.
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
from contextlib import ExitStack
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

# ---------------------------------------------------------------------------
# Shared constants
# ---------------------------------------------------------------------------

HYDRA_ROOT = Path(__file__).resolve().parents[1]
HOOKS_DIR = HYDRA_ROOT / "plugins" / "hydra" / "hooks"

_PWSH = shutil.which("pwsh") or shutil.which("powershell")

# ---------------------------------------------------------------------------
# Hook invocation helper
# ---------------------------------------------------------------------------


def _run_hook(script_name: str, payload: dict, *, env_extras: dict | None = None,
              project_dir: Path | None = None) -> subprocess.CompletedProcess:
    """Invoke a PreToolUse hook script with a JSON payload on stdin.

    Sets CLAUDE_PROJECT_DIR to project_dir (if given) so the marker-check
    resolution is isolated to that directory and does NOT fall back to the
    main Hydra repo (which has active attended-* worktrees in the current run).
    """
    stdin = json.dumps(payload)
    env = {**os.environ}
    if env_extras:
        env.update(env_extras)
    if project_dir is not None:
        env["CLAUDE_PROJECT_DIR"] = str(project_dir)
    return subprocess.run(
        [_PWSH, "-NoProfile", "-File", str(HOOKS_DIR / script_name)],
        input=stdin,
        capture_output=True,
        text=True,
        env=env,
        timeout=20,
    )


# ===========================================================================
# RA-1 — bare HYDRA_PP_STAGE_ACTIVE=1 (no marker) → hook enforces normally
# ===========================================================================


@pytest.mark.skipif(_PWSH is None, reason="pwsh/powershell not on PATH")
def test_direct_write_no_bypass_without_marker(tmp_path):
    """hydra-block-direct-write: HYDRA_PP_STAGE_ACTIVE=1 without a marker must NOT bypass."""
    # tmp_path has no .harness directory → no marker at all
    result = _run_hook(
        "hydra-block-direct-write.ps1",
        {"tool_name": "Write", "tool_input": {"file_path": "src/main.py"}},
        env_extras={"HYDRA_ENFORCE_ROUTING": "1", "HYDRA_PP_STAGE_ACTIVE": "1"},
        project_dir=tmp_path,
    )
    assert result.returncode == 2, (
        f"Expected exit 2 (block) when HYDRA_PP_STAGE_ACTIVE=1 without marker; "
        f"rc={result.returncode}\nstdout={result.stdout}\nstderr={result.stderr}"
    )
    # Stderr must mention BLOCKED
    assert "BLOCKED" in result.stderr or "block" in result.stderr.lower(), (
        f"Expected BLOCKED in stderr; got: {result.stderr!r}"
    )


@pytest.mark.skipif(_PWSH is None, reason="pwsh/powershell not on PATH")
def test_bash_writes_no_bypass_without_marker(tmp_path):
    """hydra-block-bash-writes: HYDRA_PP_STAGE_ACTIVE=1 without a marker must NOT bypass."""
    result = _run_hook(
        "hydra-block-bash-writes.ps1",
        {"tool_name": "Bash", "tool_input": {"command": "echo x > foo.py"}},
        env_extras={"HYDRA_ENFORCE_ROUTING": "1", "HYDRA_PP_STAGE_ACTIVE": "1"},
        project_dir=tmp_path,
    )
    assert result.returncode == 2, (
        f"Expected exit 2 (block) when HYDRA_PP_STAGE_ACTIVE=1 without marker; "
        f"rc={result.returncode}\nstdout={result.stdout}\nstderr={result.stderr}"
    )
    assert "BLOCKED" in result.stderr or "block" in result.stderr.lower()


@pytest.mark.skipif(_PWSH is None, reason="pwsh/powershell not on PATH")
def test_direct_pp_no_bypass_without_marker(tmp_path):
    """hydra-block-direct-pp: HYDRA_PP_STAGE_ACTIVE=1 without a marker must NOT bypass."""
    result = _run_hook(
        "hydra-block-direct-pp.ps1",
        {
            "tool_name": "Skill",
            "tool_input": {"skill": "pp:run"},
            "cwd": str(tmp_path),   # non-pp cwd → not exempted
        },
        env_extras={"HYDRA_ENFORCE_ROUTING": "1", "HYDRA_PP_STAGE_ACTIVE": "1"},
        project_dir=tmp_path,
    )
    assert result.returncode == 2, (
        f"Expected exit 2 (block) when HYDRA_PP_STAGE_ACTIVE=1 without marker; "
        f"rc={result.returncode}\nstdout={result.stdout}\nstderr={result.stderr}"
    )
    assert "BLOCKED" in result.stderr or "block" in result.stderr.lower()


# ===========================================================================
# RA-1 — HYDRA_PP_STAGE_ACTIVE=1 WITH .harness/stage-active marker → bypass
# ===========================================================================


@pytest.mark.skipif(_PWSH is None, reason="pwsh/powershell not on PATH")
def test_direct_write_bypasses_with_stage_active_file(tmp_path):
    """hydra-block-direct-write: bypasses when .harness/stage-active marker file exists."""
    harness = tmp_path / ".harness"
    harness.mkdir()
    (harness / "stage-active").write_text("stage-id=test-001", encoding="utf-8")

    result = _run_hook(
        "hydra-block-direct-write.ps1",
        {"tool_name": "Write", "tool_input": {"file_path": "src/main.py"}},
        env_extras={"HYDRA_ENFORCE_ROUTING": "1", "HYDRA_PP_STAGE_ACTIVE": "1"},
        project_dir=tmp_path,
    )
    assert result.returncode == 0, (
        f"Expected exit 0 (bypass) when .harness/stage-active exists; "
        f"rc={result.returncode}\nstdout={result.stdout}\nstderr={result.stderr}"
    )


@pytest.mark.skipif(_PWSH is None, reason="pwsh/powershell not on PATH")
def test_bash_writes_bypasses_with_stage_active_file(tmp_path):
    """hydra-block-bash-writes: bypasses when .harness/stage-active marker file exists."""
    harness = tmp_path / ".harness"
    harness.mkdir()
    (harness / "stage-active").write_text("stage-id=test-001", encoding="utf-8")

    result = _run_hook(
        "hydra-block-bash-writes.ps1",
        {"tool_name": "Bash", "tool_input": {"command": "echo x > foo.py"}},
        env_extras={"HYDRA_ENFORCE_ROUTING": "1", "HYDRA_PP_STAGE_ACTIVE": "1"},
        project_dir=tmp_path,
    )
    assert result.returncode == 0, (
        f"Expected exit 0 (bypass) when .harness/stage-active exists; "
        f"rc={result.returncode}\nstdout={result.stdout}\nstderr={result.stderr}"
    )


@pytest.mark.skipif(_PWSH is None, reason="pwsh/powershell not on PATH")
def test_direct_pp_bypasses_with_stage_active_file(tmp_path):
    """hydra-block-direct-pp: bypasses when .harness/stage-active marker file exists."""
    harness = tmp_path / ".harness"
    harness.mkdir()
    (harness / "stage-active").write_text("stage-id=test-001", encoding="utf-8")

    result = _run_hook(
        "hydra-block-direct-pp.ps1",
        {
            "tool_name": "Skill",
            "tool_input": {"skill": "pp:run"},
            "cwd": str(tmp_path),
        },
        env_extras={"HYDRA_ENFORCE_ROUTING": "1", "HYDRA_PP_STAGE_ACTIVE": "1"},
        project_dir=tmp_path,
    )
    assert result.returncode == 0, (
        f"Expected exit 0 (bypass) when .harness/stage-active exists; "
        f"rc={result.returncode}\nstdout={result.stdout}\nstderr={result.stderr}"
    )


# ===========================================================================
# RA-1 hardening (2026-08) — a stale .harness/worktrees/attended-* directory
# alone no longer satisfies the bypass. Historically ("Marker 1") the mere
# existence of ANY attended-* worktree directory was treated as proof of an
# active stage; stale worktrees accumulate across completed/aborted runs
# (17 were observed live in one session), which made that check permanently
# true and silently disabled write enforcement repo-wide. The bypass is now
# tied exclusively to the .harness/stage-active sentinel, which
# hydra_core.host_bridge writes at begin_stage and clears at
# finalize/abort — a run-scoped signal a leftover directory can't fake.
# ===========================================================================


@pytest.mark.skipif(_PWSH is None, reason="pwsh/powershell not on PATH")
def test_bash_writes_stale_worktree_dir_alone_does_not_bypass(tmp_path):
    """hydra-block-bash-writes: a bare attended-* worktree dir (no stage-active
    sentinel) must NOT bypass — this is the exact stale-directory bug being
    hardened against."""
    worktrees = tmp_path / ".harness" / "worktrees"
    worktrees.mkdir(parents=True)
    (worktrees / "attended-run_XYZTEST").mkdir()

    result = _run_hook(
        "hydra-block-bash-writes.ps1",
        {"tool_name": "Bash", "tool_input": {"command": "echo x > foo.py"}},
        env_extras={"HYDRA_ENFORCE_ROUTING": "1", "HYDRA_PP_STAGE_ACTIVE": "1"},
        project_dir=tmp_path,
    )
    assert result.returncode == 2, (
        f"Expected exit 2 (block) — a stale attended-* worktree dir alone "
        f"must not satisfy the bypass; "
        f"rc={result.returncode}\nstdout={result.stdout}\nstderr={result.stderr}"
    )
    assert "BLOCKED" in result.stderr or "block" in result.stderr.lower()


@pytest.mark.skipif(_PWSH is None, reason="pwsh/powershell not on PATH")
def test_bash_writes_requires_genuinely_written_sentinel(tmp_path):
    """hydra-block-bash-writes: the bypass requires an actual stage-active
    sentinel FILE on disk — bare env var + an unrelated directory tree is not
    enough, even when that tree looks plausible (nested .harness dirs, an
    empty stage-active-like name that isn't the real leaf file)."""
    # A directory (not a file) named 'stage-active' must not count as a leaf
    # sentinel — Test-Path -PathType Leaf in the hook requires a real file.
    (tmp_path / ".harness").mkdir()
    (tmp_path / ".harness" / "stage-active").mkdir()

    result = _run_hook(
        "hydra-block-bash-writes.ps1",
        {"tool_name": "Bash", "tool_input": {"command": "echo x > foo.py"}},
        env_extras={"HYDRA_ENFORCE_ROUTING": "1", "HYDRA_PP_STAGE_ACTIVE": "1"},
        project_dir=tmp_path,
    )
    assert result.returncode == 2, (
        f"Expected exit 2 (block) — a stage-active DIRECTORY is not a "
        f"genuinely written sentinel file; "
        f"rc={result.returncode}\nstdout={result.stdout}\nstderr={result.stderr}"
    )

    # Now write the real sentinel FILE (what host_bridge.begin_stage does) —
    # bypass must engage.
    (tmp_path / ".harness" / "stage-active").rmdir()
    (tmp_path / ".harness" / "stage-active").write_text("1", encoding="utf-8")
    result2 = _run_hook(
        "hydra-block-bash-writes.ps1",
        {"tool_name": "Bash", "tool_input": {"command": "echo x > foo.py"}},
        env_extras={"HYDRA_ENFORCE_ROUTING": "1", "HYDRA_PP_STAGE_ACTIVE": "1"},
        project_dir=tmp_path,
    )
    assert result2.returncode == 0, (
        f"Expected exit 0 (bypass) once the real sentinel file exists; "
        f"rc={result2.returncode}\nstdout={result2.stdout}\nstderr={result2.stderr}"
    )


# ===========================================================================
# Anchored allow-list (2026-08) — hydra-block-direct-write.ps1 /
# hydra-block-bash-writes.ps1 must not treat an UNANCHORED substring match
# ('\worktrees\', '\dist\', '\build\', '\.hydra\', ...) anywhere on disk as
# proof of a legitimate pp-engineer output path. The allow fragment check now
# only fires once the target resolves under the project root (or an explicit
# HYDRA_WORKTREE_ROOT).
# ===========================================================================


@pytest.mark.skipif(_PWSH is None, reason="pwsh/powershell not on PATH")
def test_direct_write_rejects_out_of_root_path_containing_worktrees_fragment(tmp_path):
    """A target path OUTSIDE the project root that merely contains
    '\\worktrees\\' must still be BLOCKED — the old unanchored
    $norm.Contains($frag) check let a write to ANY such path bypass
    regardless of where it actually lived on disk."""
    project_dir = tmp_path / "project"
    project_dir.mkdir()
    outside_dir = tmp_path / "elsewhere" / "worktrees" / "not-mine"
    outside_dir.mkdir(parents=True)
    outside_target = outside_dir / "evil.py"

    result = _run_hook(
        "hydra-block-direct-write.ps1",
        {"tool_name": "Write", "tool_input": {"file_path": str(outside_target)}},
        env_extras={"HYDRA_ENFORCE_ROUTING": "1"},
        project_dir=project_dir,
    )
    assert result.returncode == 2, (
        f"Expected exit 2 (block) for an out-of-root path that merely "
        f"contains '\\worktrees\\'; rc={result.returncode}\n"
        f"stdout={result.stdout}\nstderr={result.stderr}"
    )
    assert "BLOCKED" in result.stderr


@pytest.mark.skipif(_PWSH is None, reason="pwsh/powershell not on PATH")
def test_direct_write_allows_in_root_worktree_path(tmp_path):
    """A target genuinely under <projectRoot>\\.harness\\worktrees\\ is still
    allowed — anchoring must not break the legitimate pp-engineer path."""
    project_dir = tmp_path / "project"
    wt_dir = project_dir / ".harness" / "worktrees" / "attended-run1"
    wt_dir.mkdir(parents=True)
    target = wt_dir / "feature.py"

    result = _run_hook(
        "hydra-block-direct-write.ps1",
        {"tool_name": "Write", "tool_input": {"file_path": str(target)}},
        env_extras={"HYDRA_ENFORCE_ROUTING": "1"},
        project_dir=project_dir,
    )
    assert result.returncode == 0, (
        f"Expected exit 0 (allow) for a path under the project's own "
        f".harness\\worktrees\\; rc={result.returncode}\n"
        f"stdout={result.stdout}\nstderr={result.stderr}"
    )


@pytest.mark.skipif(_PWSH is None, reason="pwsh/powershell not on PATH")
def test_bash_writes_rejects_out_of_root_path_containing_worktrees_fragment(tmp_path):
    """Twin of the direct-write anchoring test for the Bash write-idiom hook:
    an out-of-root destination that merely contains '\\worktrees\\' must still
    be blocked."""
    project_dir = tmp_path / "project"
    project_dir.mkdir()
    outside_dir = tmp_path / "elsewhere" / "worktrees" / "not-mine"
    outside_dir.mkdir(parents=True)
    outside_target = outside_dir / "evil.py"

    result = _run_hook(
        "hydra-block-bash-writes.ps1",
        {
            "tool_name": "Bash",
            "tool_input": {"command": f'echo x > "{outside_target}"'},
            "cwd": str(project_dir),
        },
        env_extras={"HYDRA_ENFORCE_ROUTING": "1"},
        project_dir=project_dir,
    )
    assert result.returncode == 2, (
        f"Expected exit 2 (block) for an out-of-root redirection target that "
        f"merely contains '\\worktrees\\'; rc={result.returncode}\n"
        f"stdout={result.stdout}\nstderr={result.stderr}"
    )
    assert "BLOCKED" in result.stderr


@pytest.mark.skipif(_PWSH is None, reason="pwsh/powershell not on PATH")
def test_bash_writes_allows_in_root_worktree_path(tmp_path):
    """A redirection target genuinely under <projectRoot>\\.harness\\worktrees\\
    is still allowed."""
    project_dir = tmp_path / "project"
    wt_dir = project_dir / ".harness" / "worktrees" / "attended-run1"
    wt_dir.mkdir(parents=True)
    target = wt_dir / "feature.py"

    result = _run_hook(
        "hydra-block-bash-writes.ps1",
        {
            "tool_name": "Bash",
            "tool_input": {"command": f'echo x > "{target}"'},
            "cwd": str(project_dir),
        },
        env_extras={"HYDRA_ENFORCE_ROUTING": "1"},
        project_dir=project_dir,
    )
    assert result.returncode == 0, (
        f"Expected exit 0 (allow) for a redirection target under the "
        f"project's own .harness\\worktrees\\; rc={result.returncode}\n"
        f"stdout={result.stdout}\nstderr={result.stderr}"
    )


@pytest.mark.skipif(_PWSH is None, reason="pwsh/powershell not on PATH")
def test_bash_writes_relative_dest_without_cwd_still_blocks(tmp_path):
    """Regression pin: an unresolvable/relative destination (no `cwd` reported
    in the payload) must still reach the blocked-extension check and BLOCK —
    it must never be treated as "anchored" via an ambient fallback.

    This is the exact fail-open the anchoring rewrite introduced and then had
    to be corrected for: Test-BlockedDest originally fell back to this hook
    PROCESS's own (Get-Location).Path when the payload had no `cwd`. That
    ambient cwd is incidental — concretely, when the hook (or its test suite)
    happens to run from inside a real `.harness\\worktrees\\attended-*`
    directory, a bare relative destination like 'foo.py' would resolve
    against that ambient cwd, land "under" the worktree root, and be waved
    through by the allow-fragment check even though nothing tied it to the
    actual Bash tool call's real working directory. No `cwd` in the payload
    now means the destination is UNANCHORED and falls straight through to the
    plain extension block: fail closed, never fail open."""
    # No "cwd" key in the payload at all — the exact shape of the pre-existing
    # test_bash_writes_hook_blocks_redirect_to_py-style callers.
    result = _run_hook(
        "hydra-block-bash-writes.ps1",
        {"tool_name": "Bash", "tool_input": {"command": "echo x > foo.py"}},
        env_extras={"HYDRA_ENFORCE_ROUTING": "1"},
        project_dir=tmp_path,
    )
    assert result.returncode == 2, (
        f"Expected exit 2 (block) for a relative destination with no cwd "
        f"reported — an unresolvable destination must fail CLOSED, not be "
        f"waved through by the anchor gate; rc={result.returncode}\n"
        f"stdout={result.stdout}\nstderr={result.stderr}"
    )
    assert "BLOCKED" in result.stderr


# ===========================================================================
# RA-1 — warning message when no marker
# ===========================================================================


@pytest.mark.skipif(_PWSH is None, reason="pwsh/powershell not on PATH")
def test_bash_writes_emits_warning_when_bare_stage_active(tmp_path):
    """Hook emits the warning line when HYDRA_PP_STAGE_ACTIVE=1 without a marker."""
    result = _run_hook(
        "hydra-block-bash-writes.ps1",
        {"tool_name": "Bash", "tool_input": {"command": "echo x > foo.py"}},
        env_extras={"HYDRA_ENFORCE_ROUTING": "1", "HYDRA_PP_STAGE_ACTIVE": "1"},
        project_dir=tmp_path,
    )
    # The warning must appear on stdout (Write-Host goes to stdout)
    combined = result.stdout + result.stderr
    assert "bare HYDRA_PP_STAGE_ACTIVE=1 ignored" in combined or \
           "no active stage marker" in combined, (
        f"Expected warning line about bare HYDRA_PP_STAGE_ACTIVE; "
        f"stdout={result.stdout!r} stderr={result.stderr!r}"
    )


# ===========================================================================
# RA-1 — static text checks (no pwsh required)
# ===========================================================================


def test_hooks_contain_stage_active_marker_check():
    """All three enforcement hooks must gate the bypass on the run-scoped
    .harness/stage-active sentinel — and must NOT resurrect the retired
    "Marker 1" directory-enumeration bypass.

    Historically the hooks also treated the mere existence of any
    .harness/worktrees/attended-* directory as proof of an active stage
    ("Marker 1"). Stale worktrees accumulate across completed/aborted runs and
    made that check permanently true, silently disabling enforcement
    repo-wide, so it was retired: the sentinel host_bridge writes at
    begin_stage / clears at finalize/abort is now the sole source of truth.
    This test pins that contract so a future edit can't quietly reintroduce
    the directory-enumeration bypass.
    """
    for name in (
        "hydra-block-direct-write.ps1",
        "hydra-block-bash-writes.ps1",
        "hydra-block-direct-pp.ps1",
    ):
        text = (HOOKS_DIR / name).read_text(encoding="utf-8")
        assert "stage-active" in text, f"{name}: missing stage-active marker check"
        # The retired Marker-1 enumeration (Get-ChildItem ... -Filter
        # 'attended-*') must be gone as a live bypass. A prose mention of
        # "attended-*" in the retirement comment is fine; the active
        # directory-filter idiom is not.
        assert "Filter 'attended-*'" not in text, (
            f"{name}: retired Marker-1 attended-* directory enumeration "
            f"reintroduced — the bypass must be sentinel-only"
        )
        assert "retired" in text.lower(), (
            f"{name}: missing documentation that the attended-* directory "
            f"marker was retired in favor of the stage-active sentinel"
        )
        assert "bare HYDRA_PP_STAGE_ACTIVE" in text or "no active stage marker" in text, (
            f"{name}: missing warning message for bare HYDRA_PP_STAGE_ACTIVE"
        )


def test_session_contract_warns_on_leaked_stage_active():
    """hydra-session-contract.ps1 must flag HYDRA_PP_STAGE_ACTIVE=1 at session start.

    The hook was updated from a WARNING to a NOTE that documents the S6
    hardening: enforcement is NOT bypassed by the bare env var alone — the
    run-scoped .harness/stage-active sentinel is also required (the old
    attended-* worktree-directory marker was retired). This test verifies the
    note is present with the correct marker-required semantics (not the old
    literal 'WARNING').
    """
    text = (HOOKS_DIR / "hydra-session-contract.ps1").read_text(encoding="utf-8")
    assert "HYDRA_PP_STAGE_ACTIVE" in text, (
        "hydra-session-contract.ps1 missing HYDRA_PP_STAGE_ACTIVE=1 check"
    )
    assert "NOTE" in text or "note" in text.lower(), (
        "hydra-session-contract.ps1 leaked-var message must use NOTE (not WARNING)"
    )
    assert "marker" in text.lower(), (
        "hydra-session-contract.ps1 must document that a filesystem stage marker "
        "is required — bare HYDRA_PP_STAGE_ACTIVE no longer bypasses enforcement"
    )


# ===========================================================================
# RA-9 — doctor WS-AUTH key probe
# ===========================================================================


def _run_doctor_wsauth(tmp_path: Path, capsys, monkeypatch, *,
                       home_dir: Path | None = None) -> tuple[int, str]:
    """Run `hydra doctor` (full mode) with a minimal mock dispatcher.

    Patches MCPStdioDispatcher, _load_mcp_config, and PendingSpool so doctor
    reaches the full MCP probes section (including the RA-9 WS-AUTH probe at
    the bottom) without needing real MCP servers. Optionally patches
    pathlib.Path.home so the backends.json lookup targets a tmp directory.
    """
    from hydra_core import cli

    class _MockDispatcher:
        def call_mcp(self, server: str, tool: str, args: dict, **_kw) -> dict:
            return {"status": "failed", "error": "not_registered"}

    _empty_spool = MagicMock()
    _empty_spool.count.return_value = 0

    ctx_list = [
        patch("hydra_core.dispatcher.MCPStdioDispatcher", return_value=_MockDispatcher()),
        patch("hydra_core.dispatcher._load_mcp_config", return_value={}),
        patch("hydra_core.eights.pending_spool.PendingSpool", return_value=_empty_spool),
    ]
    if home_dir is not None:
        ctx_list.append(patch("pathlib.Path.home", return_value=home_dir))

    with ExitStack() as stack:
        for ctx in ctx_list:
            stack.enter_context(ctx)
        rc = cli.main(["--project", str(HYDRA_ROOT), "doctor"])

    out = capsys.readouterr().out
    return rc, out


def test_doctor_wsauth_ok_from_env(tmp_path, capsys, monkeypatch):
    """Doctor reports OK WS-AUTH (source=env) when HYDRA_OPERATOR_KEY is in env."""
    monkeypatch.setenv("HYDRA_OPERATOR_KEY", "test-operator-key-abc")
    rc, out = _run_doctor_wsauth(tmp_path, capsys, monkeypatch)
    wsauth = [ln for ln in out.splitlines() if "WS-AUTH" in ln]
    assert wsauth, f"No WS-AUTH output in doctor output:\n{out}"
    assert any("OK" in ln for ln in wsauth), (
        f"Expected OK line for env-configured key; got:\n{chr(10).join(wsauth)}"
    )
    assert any("source=env" in ln for ln in wsauth), (
        f"Expected 'source=env' in WS-AUTH line; got:\n{chr(10).join(wsauth)}"
    )
    # Doctor must never print the key value itself
    assert "test-operator-key-abc" not in out, "Doctor must NOT print the operator key"


def test_doctor_wsauth_ok_from_backends_json(tmp_path, capsys, monkeypatch):
    """Doctor reports OK WS-AUTH (source=backends.json) when key is in backends.json."""
    monkeypatch.delenv("HYDRA_OPERATOR_KEY", raising=False)

    fake_hydra = tmp_path / ".hydra"
    fake_hydra.mkdir()
    backends = {
        "xenia_tickets": {
            "command": "python",
            "env": {"HYDRA_OPERATOR_KEY": "from-backends-key-xyz"},
        }
    }
    (fake_hydra / "backends.json").write_text(json.dumps(backends), encoding="utf-8")

    rc, out = _run_doctor_wsauth(tmp_path, capsys, monkeypatch, home_dir=tmp_path)
    wsauth = [ln for ln in out.splitlines() if "WS-AUTH" in ln]
    assert wsauth, f"No WS-AUTH output:\n{out}"
    assert any("OK" in ln for ln in wsauth), (
        f"Expected OK line for backends.json key; got:\n{chr(10).join(wsauth)}"
    )
    assert any("source=backends.json" in ln for ln in wsauth), (
        f"Expected 'source=backends.json' in WS-AUTH line; got:\n{chr(10).join(wsauth)}"
    )
    # Doctor must never print the key value itself
    assert "from-backends-key-xyz" not in out, "Doctor must NOT print the operator key"


def test_doctor_wsauth_warn_when_absent(tmp_path, capsys, monkeypatch):
    """Doctor reports WARN WS-AUTH when key is absent from both env and backends.json."""
    monkeypatch.delenv("HYDRA_OPERATOR_KEY", raising=False)
    # tmp_path has no .hydra/backends.json
    rc, out = _run_doctor_wsauth(tmp_path, capsys, monkeypatch, home_dir=tmp_path)
    wsauth = [ln for ln in out.splitlines() if "WS-AUTH" in ln]
    assert wsauth, f"No WS-AUTH output:\n{out}"
    assert any("WARN" in ln for ln in wsauth), (
        f"Expected WARN line when key is absent; got:\n{chr(10).join(wsauth)}"
    )
    assert not any("FAIL" in ln for ln in wsauth), (
        "WS-AUTH probe must never emit FAIL (never increments fail_count)"
    )
    # WARN message must mention xenia
    assert any("xenia" in ln.lower() or "fail-closed" in ln for ln in wsauth), (
        f"WARN should mention xenia/fail-closed; got:\n{chr(10).join(wsauth)}"
    )


def test_doctor_wsauth_malformed_backends_json_warns_unprovisioned(tmp_path, capsys, monkeypatch):
    """Malformed backends.json → fail-soft → WARN unprovisioned; doctor RC unaffected."""
    monkeypatch.delenv("HYDRA_OPERATOR_KEY", raising=False)

    fake_hydra = tmp_path / ".hydra"
    fake_hydra.mkdir()
    (fake_hydra / "backends.json").write_text("NOT VALID JSON {{{{", encoding="utf-8")

    rc, out = _run_doctor_wsauth(tmp_path, capsys, monkeypatch, home_dir=tmp_path)
    wsauth = [ln for ln in out.splitlines() if "WS-AUTH" in ln]
    assert wsauth, f"No WS-AUTH output:\n{out}"
    # Malformed JSON → fail-soft → treated as absent → WARN
    assert any("WARN" in ln for ln in wsauth), (
        f"Expected WARN for malformed backends.json; got:\n{chr(10).join(wsauth)}"
    )
    assert not any("FAIL" in ln for ln in wsauth), (
        "WS-AUTH probe must never emit FAIL on IO/parse error"
    )
    # Doctor RC must not be affected by the key probe (probe never calls fail_count += 1)
    # rc can be 0 or 1 depending on other checks (not WS-AUTH), but must not be > 1.
    assert rc in (0, 1), f"Unexpected doctor RC={rc}"


def test_doctor_wsauth_backends_json_no_key_in_env(tmp_path, capsys, monkeypatch):
    """backends.json present but no HYDRA_OPERATOR_KEY in any spec env → WARN."""
    monkeypatch.delenv("HYDRA_OPERATOR_KEY", raising=False)

    fake_hydra = tmp_path / ".hydra"
    fake_hydra.mkdir()
    backends = {
        "xenia_tickets": {
            "command": "python",
            "env": {"SOME_OTHER_KEY": "value"},   # no HYDRA_OPERATOR_KEY
        }
    }
    (fake_hydra / "backends.json").write_text(json.dumps(backends), encoding="utf-8")

    rc, out = _run_doctor_wsauth(tmp_path, capsys, monkeypatch, home_dir=tmp_path)
    wsauth = [ln for ln in out.splitlines() if "WS-AUTH" in ln]
    assert wsauth, f"No WS-AUTH output:\n{out}"
    assert any("WARN" in ln for ln in wsauth), (
        f"Expected WARN when backends.json has no HYDRA_OPERATOR_KEY; "
        f"got:\n{chr(10).join(wsauth)}"
    )
