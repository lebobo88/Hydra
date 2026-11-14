"""WS1 follow-up: `hydra_core.repo_registry`'s repos.json / lock resolvers
must honour `$HYDRA_HOME`, not hardcode `Path.home() / ".hydra"`.

Deliberately does NOT live in `test_repo_admin.py` -- that file's autouse
`_isolated_repos_json` fixture monkeypatches `_repos_json_path`/
`_repos_lock_path` directly, which would hide a regression in the resolvers'
OWN `HYDRA_HOME` branch (the very thing this file exists to prove).
"""
from __future__ import annotations

from pathlib import Path

import pytest

from hydra_core import repo_registry


def test_repos_json_path_honours_hydra_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """`_repos_json_path()` must resolve under `$HYDRA_HOME` when it is set.

    This is a PROPERTY test, not a coverage test: if `_hydra_home()`'s
    `HYDRA_HOME` branch were ever removed (reverting to a hardcoded
    ``Path.home() / ".hydra"``), this assertion would fail in any normal
    dev/CI environment, because `tmp_path` and the real `Path.home()` are
    different directories.
    """
    monkeypatch.setenv("HYDRA_HOME", str(tmp_path))
    resolved = repo_registry._repos_json_path()
    assert resolved == tmp_path / "repos.json"
    assert resolved != Path.home() / ".hydra" / "repos.json"


def test_repos_lock_path_honours_hydra_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("HYDRA_HOME", str(tmp_path))
    resolved = repo_registry._repos_lock_path()
    assert resolved == tmp_path / "repos.json.lock"
    assert resolved != Path.home() / ".hydra" / "repos.json.lock"


def test_repos_json_path_defaults_to_real_home_when_hydra_home_unset(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Production default (HYDRA_HOME unset) must stay `~/.hydra` -- the
    hermetic suite always sets HYDRA_HOME (tests/conftest.py), so this is
    the one place in the suite that deliberately unsets it to prove the
    fallback still matches production."""
    monkeypatch.delenv("HYDRA_HOME", raising=False)
    resolved = repo_registry._repos_json_path()
    assert resolved == Path.home() / ".hydra" / "repos.json"


def test_load_extra_repos_reads_under_hydra_home(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """End-to-end: register_repo's write path and _load_extra_repos' read
    path must agree, and both must land under $HYDRA_HOME -- never the
    operator's real ~/.hydra."""
    monkeypatch.setenv("HYDRA_HOME", str(tmp_path))
    monkeypatch.delenv("HYDRA_EXTRA_REPOS", raising=False)

    target = tmp_path / "some-repo"
    target.mkdir()
    import subprocess
    subprocess.run(["git", "init", "-q"], cwd=target, check=True)

    repo_registry.register_repo("hh-repo", str(target))

    assert (tmp_path / "repos.json").is_file()
    assert repo_registry.is_known_repo("hh-repo")
    assert repo_registry._load_extra_repos()["hh-repo"] == target.resolve()
