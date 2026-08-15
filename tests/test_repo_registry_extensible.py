"""Tests for the extensible repo registry (Fix 2b).

``hydra_core.repo_registry`` historically resolved only a hardcoded allow-list
(``_REPO_DIRNAMES``) of dirs under a shared base, so a Hydra-driven engineering
run could not target an external or nested sibling repo (e.g.
``RLM-CLI-Starter/Projects/space-sim``) — codex fell back to a throwaway temp
checkout and the run surfaced without landing code.

These tests cover the operator-registered extension surface
(``~/.hydra/repos.json`` + ``HYDRA_EXTRA_REPOS``): registered repos resolve even
outside the shared base and even when nested under another git repo, while the
security guards (raw-path id rejection, real git-root verification) still hold.

All git operations are local-only (git init / git rev-parse).
"""
from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

import hydra_core.repo_registry as repo_registry_module
from hydra_core.repo_registry import (
    is_known_repo,
    resolve_repo_path,
    resolve_repo_project_path,
)


def _git_init(path: Path) -> None:
    """Run `git init` in *path* so rev-parse --show-toplevel succeeds."""
    path.mkdir(parents=True, exist_ok=True)
    subprocess.run(["git", "init", str(path)], capture_output=True, check=True)


def test_extra_repo_via_env_resolves(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A repo registered through HYDRA_EXTRA_REPOS resolves to its real path."""
    repo = tmp_path / "external" / "space-sim"
    _git_init(repo)
    monkeypatch.setenv("HYDRA_EXTRA_REPOS", json.dumps({"space-sim": str(repo)}))

    assert is_known_repo("space-sim") is True
    resolved = resolve_repo_path("space-sim")
    assert resolved == repo.resolve()


def test_extra_repo_resolves_outside_shared_base(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The base-escape guard does NOT apply to an explicitly-registered repo:
    it may live anywhere on disk, unrelated to HYDRA_REPO_BASE."""
    base = tmp_path / "base"
    base.mkdir()
    monkeypatch.setenv("HYDRA_REPO_BASE", str(base))

    repo = tmp_path / "somewhere" / "else" / "myproj"
    _git_init(repo)
    monkeypatch.setenv("HYDRA_EXTRA_REPOS", json.dumps({"myproj": str(repo)}))

    assert resolve_repo_path("myproj") == repo.resolve()


def test_extra_repo_case_insensitive(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo = tmp_path / "ExternalRepo"
    _git_init(repo)
    monkeypatch.setenv("HYDRA_EXTRA_REPOS", json.dumps({"externalrepo": str(repo)}))
    assert resolve_repo_path("ExternalRepo") == repo.resolve()


def test_nested_git_repo_registered_resolves(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A git repo NESTED under another git repo resolves when registered: its
    own rev-parse toplevel equals the candidate, so the nested-repo guard passes
    (this is exactly the Projects/space-sim layout)."""
    parent = tmp_path / "RLM-CLI-Starter"
    _git_init(parent)
    nested = parent / "Projects" / "space-sim"
    _git_init(nested)
    monkeypatch.setenv("HYDRA_EXTRA_REPOS", json.dumps({"space-sim": str(nested)}))

    assert resolve_repo_path("space-sim") == nested.resolve()


def test_extra_repo_non_git_dir_rejected(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A registered path that is not a real git repo is still rejected."""
    not_a_repo = tmp_path / "plain-dir"
    not_a_repo.mkdir()
    monkeypatch.setenv("HYDRA_EXTRA_REPOS", json.dumps({"plain": str(not_a_repo)}))
    with pytest.raises(ValueError, match="not a git repo"):
        resolve_repo_path("plain")


def test_extra_repo_via_home_repos_json(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The ~/.hydra/repos.json source is honored (env override merges on top)."""
    home = tmp_path / "home"
    (home / ".hydra").mkdir(parents=True)
    repo = tmp_path / "filereg"
    _git_init(repo)
    (home / ".hydra" / "repos.json").write_text(
        json.dumps({"filereg": str(repo)}), encoding="utf-8"
    )
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("USERPROFILE", str(home))
    monkeypatch.delenv("HYDRA_EXTRA_REPOS", raising=False)

    assert is_known_repo("filereg") is True
    assert resolve_repo_path("filereg") == repo.resolve()


def test_malformed_extra_config_is_failsoft(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A bad HYDRA_EXTRA_REPOS value must not break built-in resolution."""
    monkeypatch.setenv("HYDRA_EXTRA_REPOS", "{not valid json")
    # The built-in 'hydra' repo still resolves (this checkout is a git repo).
    assert resolve_repo_path("hydra").name == "Hydra"


def test_raw_path_id_still_rejected_with_extra(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The raw-path-in-id guard fires before any extra lookup."""
    monkeypatch.setenv("HYDRA_EXTRA_REPOS", json.dumps({"x": "/tmp/x"}))
    with pytest.raises(ValueError, match="raw paths are not accepted"):
        resolve_repo_path("../../etc")


def test_unknown_id_still_rejected_with_extra(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo = tmp_path / "known"
    _git_init(repo)
    monkeypatch.setenv("HYDRA_EXTRA_REPOS", json.dumps({"known": str(repo)}))
    with pytest.raises(ValueError, match="unknown repo_id"):
        resolve_repo_path("definitely-not-registered")


def test_extra_repo_subpath_resolves(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """resolve_repo_project_path narrows a registered repo to a safe subpath."""
    repo = tmp_path / "withsub"
    _git_init(repo)
    (repo / "pkg").mkdir()
    monkeypatch.setenv("HYDRA_EXTRA_REPOS", json.dumps({"withsub": str(repo)}))
    got = resolve_repo_project_path("withsub", "pkg")
    assert got == (repo / "pkg").resolve()


def test_builtin_nested_relative_dirname_resolves(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A hardcoded ``_REPO_DIRNAMES`` value MAY contain '/' to reach a repo
    nested under the shared base (e.g. the real 'hydra-galaga' entry, which
    maps to 'galaga-game-deepseek/hydra-galaga'). This exercises that shape
    against a synthetic base/tree so the test does not depend on the real
    checkout existing on the runner."""
    base = tmp_path / "base"
    base.mkdir()
    monkeypatch.setenv("HYDRA_REPO_BASE", str(base))

    nested = base / "parent-dir" / "child-repo"
    _git_init(nested)
    monkeypatch.setitem(
        repo_registry_module._REPO_DIRNAMES,
        "nested-builtin",
        "parent-dir/child-repo",
    )

    assert is_known_repo("nested-builtin") is True
    assert resolve_repo_path("nested-builtin") == nested.resolve()


def test_hydra_galaga_registry_entry_present() -> None:
    """The 'hydra-galaga' repo_id is registered with its expected nested
    directory value (galaga-game-deepseek/hydra-galaga under the shared base)."""
    assert repo_registry_module._REPO_DIRNAMES.get("hydra-galaga") == (
        "galaga-game-deepseek/hydra-galaga"
    )
    assert is_known_repo("hydra-galaga") is True


def test_repo_dirnames_validation_passes_on_current_registry() -> None:
    """Every current _REPO_DIRNAMES value is a safe relative, downward-only
    path -- this is exactly what runs at import time, so it must not raise."""
    repo_registry_module._validate_repo_dirnames(repo_registry_module._REPO_DIRNAMES)


def test_repo_dirnames_validation_rejects_absolute_value() -> None:
    with pytest.raises(RuntimeError, match="invalid _REPO_DIRNAMES entry"):
        repo_registry_module._validate_repo_dirnames({"bad": "C:/somewhere/else"})


def test_repo_dirnames_validation_rejects_parent_traversal() -> None:
    with pytest.raises(RuntimeError, match="invalid _REPO_DIRNAMES entry"):
        repo_registry_module._validate_repo_dirnames({"bad": "../escape"})


def test_extra_repo_subpath_escape_rejected(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo = tmp_path / "escapee"
    _git_init(repo)
    monkeypatch.setenv("HYDRA_EXTRA_REPOS", json.dumps({"escapee": str(repo)}))
    with pytest.raises(ValueError, match="escape"):
        resolve_repo_project_path("escapee", "../sibling")
