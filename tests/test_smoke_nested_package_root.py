"""W2: nested package-root smoke detection.

`_detect_smoke_command` used to look for `package.json` at the repo root
ONLY. pair-programmer's own package root is `daemon/`, so every pp stage's
smoke silently "skipped" ("no runnable build/test command detected") even on
a passing candidate — the stage still surfaced because PP-VG-5 requires a real
smoke execution before a code stage may finalize `complete`.

These tests pin: (1) a single qualifying nested package root is detected and
run in ITS OWN directory (not the repo root), (2) an ambiguous repo shape
(zero or more than one candidate) is conservatively skipped exactly as
before, (3) common vendor/build directories are never mistaken for a package
root, (4) workspace-declared members are treated as qualifying even without
their own test/build script, and (5) `_run_smoke` actually executes the
command in the nested directory.
"""
from __future__ import annotations

import json

from hydra_core.squad_node import (
    _detect_smoke_command,
    _detect_smoke_command_and_cwd,
    _find_nested_package_root,
)


def _write_pkg(path, scripts=None, workspaces=None):
    path.mkdir(parents=True, exist_ok=True)
    data = {}
    if scripts is not None:
        data["scripts"] = scripts
    if workspaces is not None:
        data["workspaces"] = workspaces
    (path / "package.json").write_text(json.dumps(data), encoding="utf-8")


def test_top_level_package_json_still_wins(tmp_path):
    """Unchanged behavior: a runnable command at the repo root is used as-is,
    never overridden by a nested candidate."""
    _write_pkg(tmp_path, scripts={"test": "vitest run"})
    (tmp_path / "daemon").mkdir()
    _write_pkg(tmp_path / "daemon", scripts={"test": "jest"})

    cmd, cwd = _detect_smoke_command_and_cwd(str(tmp_path))
    assert cmd == ["npm", "test", "--silent"]
    assert cwd == str(tmp_path)


def test_single_nested_package_root_detected_and_run_there(tmp_path):
    """The pair-programmer shape: no root package.json, but exactly one
    subdirectory (daemon/) has a runnable test script."""
    daemon = tmp_path / "daemon"
    _write_pkg(daemon, scripts={"test": "vitest run", "build": "tsc"})

    cmd, cwd = _detect_smoke_command_and_cwd(str(tmp_path))
    assert cmd == ["npm", "test", "--silent"]
    assert cwd == str(daemon)
    # The thin wrapper used by the existing call sites still returns argv only.
    assert _detect_smoke_command(str(tmp_path)) == cmd


def test_ambiguous_multiple_candidates_skips_conservatively(tmp_path):
    """Two subdirectories both qualify -> None (skip), never a guess."""
    _write_pkg(tmp_path / "server", scripts={"test": "jest"})
    _write_pkg(tmp_path / "client", scripts={"test": "vitest"})

    cmd, cwd = _detect_smoke_command_and_cwd(str(tmp_path))
    assert cmd is None
    assert cwd == str(tmp_path)
    assert _find_nested_package_root(tmp_path) is None


def test_zero_candidates_skips(tmp_path):
    """No package.json anywhere, no python/go/rust markers -> None."""
    (tmp_path / "README.md").write_text("hi\n", encoding="utf-8")
    cmd, _cwd = _detect_smoke_command_and_cwd(str(tmp_path))
    assert cmd is None


def test_vendor_and_build_dirs_ignored(tmp_path):
    """node_modules/, dist/, .git/, etc. must never be mistaken for a package
    root even though they may contain a package.json (npm packages do)."""
    _write_pkg(tmp_path / "node_modules" / "some-dep", scripts={"test": "x"})
    _write_pkg(tmp_path / "dist", scripts={"test": "x"})
    _write_pkg(tmp_path / ".git", scripts={"test": "x"})
    daemon = tmp_path / "daemon"
    _write_pkg(daemon, scripts={"build": "tsc"})

    cmd, cwd = _detect_smoke_command_and_cwd(str(tmp_path))
    assert cmd == ["npm", "run", "build", "--silent"]
    assert cwd == str(daemon)


def test_workspace_declared_member_qualifies_without_own_scripts(tmp_path):
    """A root package.json declaring `workspaces: ["daemon"]` makes `daemon/`
    a qualifying candidate even if its own package.json has no test/build
    script (workspace roots often run tests from the top level)."""
    _write_pkg(tmp_path, workspaces=["daemon"])
    daemon = tmp_path / "daemon"
    _write_pkg(daemon, scripts={"test": "vitest run"})

    # Root itself has no runnable script (workspaces only), so detection
    # falls through to the nested candidate — which IS named in workspaces.
    root = _find_nested_package_root(tmp_path)
    assert root == daemon


def test_workspace_declared_plus_different_scripted_dir_skips_ambiguously(tmp_path):
    """Mixed ambiguity: a dir that is BOTH workspace-declared and scripted
    counts once (see test_single_nested_package_root_detected_and_run_there /
    test_workspace_declared_member_qualifies_without_own_scripts), but a
    workspace-declared dir A plus a DIFFERENT dir B that merely has its own
    test/build script are two distinct qualifying candidates -> ambiguous ->
    skip (None), same as two purely-scripted candidates."""
    _write_pkg(tmp_path, workspaces=["daemon"])
    daemon = tmp_path / "daemon"
    _write_pkg(daemon, scripts={"test": "vitest run"})
    # `client` is NOT named in workspaces, but qualifies on its own script.
    client = tmp_path / "client"
    _write_pkg(client, scripts={"test": "jest"})

    assert _find_nested_package_root(tmp_path) is None
    cmd, cwd = _detect_smoke_command_and_cwd(str(tmp_path))
    assert cmd is None
    assert cwd == str(tmp_path)


def test_pytest_at_root_still_preferred_over_nested_node(tmp_path):
    """Existing Python-project detection is untouched and still takes
    priority over the new nested-node fallback."""
    (tmp_path / "pyproject.toml").write_text("[tool.x]\n", encoding="utf-8")
    _write_pkg(tmp_path / "daemon", scripts={"test": "jest"})

    cmd, cwd = _detect_smoke_command_and_cwd(str(tmp_path))
    assert cmd[-3:] == ["-m", "pytest", "-q"]
    assert cwd == str(tmp_path)


def test_run_smoke_executes_in_nested_cwd(tmp_path, monkeypatch):
    """_run_smoke must actually launch the command in the nested directory,
    not the repo root -- otherwise `npm test` would fail with ENOENT (no
    package.json) even though detection found a runnable command."""
    from hydra_core import squad_node

    daemon = tmp_path / "daemon"
    _write_pkg(daemon, scripts={"test": "echo ok"})

    captured = {}

    class _FakeCompleted:
        returncode = 0
        stdout = "ok\n"
        stderr = ""

    def _fake_run(cmd, cwd, **kwargs):
        captured["cwd"] = cwd
        captured["cmd"] = cmd
        return _FakeCompleted()

    monkeypatch.setattr(squad_node.subprocess, "run", _fake_run)

    status, _reason = squad_node._run_smoke(
        dispatcher=None, project_path=str(tmp_path), stage_id="stage-1")

    assert status == "pass"
    assert captured["cwd"] == str(daemon), (
        "smoke must run in the nested package root, not the repo root"
    )
