"""WS1-C: tests for hydra_core.repo_registry's self-service admin surface
(register_repo / unregister_repo / list_registered_repos) and the
`hydra repo register|unregister|list` CLI subcommand.

All tests are offline (local git only) and hermetic: every test monkeypatches
_REPOS_JSON_PATH / _REPOS_LOCK_PATH to a tmp_path location so the operator's
real ~/.hydra/repos.json is never touched.
"""
from __future__ import annotations

import concurrent.futures
import json
import subprocess
from pathlib import Path

import pytest

from hydra_core import repo_registry
from hydra_core.repo_registry import (
    _REPO_DIRNAMES,
    is_known_repo,
    list_registered_repos,
    register_repo,
    resolve_repo_path,
    unregister_repo,
)


@pytest.fixture(autouse=True)
def _isolated_repos_json(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """Redirect the registry's config + lock paths into tmp_path, and clear
    HYDRA_EXTRA_REPOS so no operator env leaks into the test."""
    cfg = tmp_path / ".hydra" / "repos.json"
    lock = tmp_path / ".hydra" / "repos.json.lock"
    monkeypatch.setattr(repo_registry, "_REPOS_JSON_PATH", cfg)
    monkeypatch.setattr(repo_registry, "_REPOS_LOCK_PATH", lock)
    monkeypatch.delenv("HYDRA_EXTRA_REPOS", raising=False)
    return cfg


def _git_repo(path: Path) -> Path:
    path.mkdir(parents=True, exist_ok=True)
    subprocess.run(["git", "init", str(path)], capture_output=True, check=True)
    return path


# ---------------------------------------------------------------------------
# register_repo
# ---------------------------------------------------------------------------


def test_register_repo_atomic_write_and_resolve(tmp_path: Path, _isolated_repos_json: Path) -> None:
    target = _git_repo(tmp_path / "some-repo")
    result = register_repo("some-repo", str(target))
    assert result == {"repo_id": "some-repo", "path": str(target)}

    assert _isolated_repos_json.is_file()
    data = json.loads(_isolated_repos_json.read_text(encoding="utf-8"))
    assert data == {"some-repo": str(target)}

    assert is_known_repo("some-repo")
    resolved = resolve_repo_path("some-repo")
    assert resolved == target.resolve()


def test_register_repo_init_creates_directory_and_git_repo(tmp_path: Path) -> None:
    """The galaga case: a non-existent path, registered with --init."""
    target = tmp_path / "brand-new" / "project"
    assert not target.exists()
    register_repo("hydra-galaga-test", str(target), init=True)
    assert target.is_dir()
    assert (target / ".git").exists()
    resolve_repo_path("hydra-galaga-test")  # does not raise


def test_register_repo_non_git_dir_without_init_fails_naming_init(tmp_path: Path) -> None:
    target = tmp_path / "plain-dir"
    target.mkdir()
    with pytest.raises(ValueError, match="--init"):
        register_repo("plain-dir", str(target))


def test_register_repo_missing_path_without_init_fails_naming_init(tmp_path: Path) -> None:
    target = tmp_path / "does-not-exist"
    with pytest.raises(ValueError, match="--init"):
        register_repo("missing-dir", str(target))


def test_register_repo_refuses_to_shadow_builtin_without_force(tmp_path: Path) -> None:
    builtin_id = next(iter(_REPO_DIRNAMES))
    target = _git_repo(tmp_path / "shadow")
    with pytest.raises(ValueError, match="built-in"):
        register_repo(builtin_id, str(target))


def test_register_repo_shadow_builtin_with_force_succeeds(tmp_path: Path) -> None:
    builtin_id = next(iter(_REPO_DIRNAMES))
    target = _git_repo(tmp_path / "shadow-forced")
    register_repo(builtin_id, str(target), force=True)
    # Operator-registered entries win over the built-in allow-list at
    # resolution time (see _load_extra_repos / resolve_repo_path ordering).
    assert resolve_repo_path(builtin_id) == target.resolve()


def test_register_repo_relative_path_rejected(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="absolute"):
        register_repo("relative-repo", "relative/path")


def test_register_repo_rolls_back_on_verify_failure(
    tmp_path: Path, _isolated_repos_json: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """If post-write verification (resolve_repo_path) fails, the prior file
    contents must be restored -- register_repo must not leave a torn or
    silently-broken registration behind."""
    # Seed a prior, valid entry so we can assert it survives the rollback.
    prior_target = _git_repo(tmp_path / "prior-good")
    register_repo("prior-good", str(prior_target))
    prior_raw = _isolated_repos_json.read_text(encoding="utf-8")

    # Force resolve_repo_path to fail for the NEW entry only, post-write.
    real_resolve = repo_registry.resolve_repo_path

    def _failing_resolve(repo_id: str):
        if repo_id == "will-fail":
            raise ValueError("simulated verify failure")
        return real_resolve(repo_id)

    monkeypatch.setattr(repo_registry, "resolve_repo_path", _failing_resolve)

    bad_target = _git_repo(tmp_path / "will-fail-dir")
    with pytest.raises(ValueError, match="verification failed"):
        register_repo("will-fail", str(bad_target))

    # Rolled back: file contents are byte-identical to before the failed call.
    assert _isolated_repos_json.read_text(encoding="utf-8") == prior_raw
    assert "will-fail" not in json.loads(prior_raw)


def test_register_repo_concurrent_registration_both_land(tmp_path: Path, _isolated_repos_json: Path) -> None:
    """Two concurrent register_repo calls for DIFFERENT ids must both succeed
    and both be present afterward -- the lock serializes the read-modify-write
    so neither write is lost."""
    target_a = _git_repo(tmp_path / "concurrent-a")
    target_b = _git_repo(tmp_path / "concurrent-b")

    with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
        fut_a = pool.submit(register_repo, "concurrent-a", str(target_a))
        fut_b = pool.submit(register_repo, "concurrent-b", str(target_b))
        fut_a.result(timeout=10)
        fut_b.result(timeout=10)

    registered = list_registered_repos()
    assert registered.get("concurrent-a") == str(target_a)
    assert registered.get("concurrent-b") == str(target_b)


# ---------------------------------------------------------------------------
# unregister_repo / list_registered_repos
# ---------------------------------------------------------------------------


def test_unregister_repo_removes_entry(tmp_path: Path) -> None:
    target = _git_repo(tmp_path / "to-remove")
    register_repo("to-remove", str(target))
    assert is_known_repo("to-remove")

    result = unregister_repo("to-remove")
    assert result == {"repo_id": "to-remove", "removed": True}
    assert not is_known_repo("to-remove")


def test_unregister_repo_unknown_id_raises(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="not registered"):
        unregister_repo("never-registered")


def test_list_registered_repos_empty_when_no_config(tmp_path: Path) -> None:
    assert list_registered_repos() == {}


def test_list_registered_repos_sorted(tmp_path: Path) -> None:
    target_z = _git_repo(tmp_path / "z-repo")
    target_a = _git_repo(tmp_path / "a-repo")
    register_repo("z-repo", str(target_z))
    register_repo("a-repo", str(target_a))
    assert list(list_registered_repos().keys()) == ["a-repo", "z-repo"]


# ---------------------------------------------------------------------------
# CLI: `hydra repo register|unregister|list`
# ---------------------------------------------------------------------------


def test_cli_repo_register_and_list(tmp_path: Path, _isolated_repos_json: Path) -> None:
    from hydra_core.cli import main

    target = _git_repo(tmp_path / "cli-repo")
    rc = main(["repo", "register", "cli-repo", str(target)])
    assert rc == 0
    assert is_known_repo("cli-repo")

    rc_list = main(["repo", "list"])
    assert rc_list == 0


def test_cli_repo_register_non_git_without_init_exits_nonzero_naming_init(
    tmp_path: Path, capsys: pytest.CaptureFixture
) -> None:
    from hydra_core.cli import main

    target = tmp_path / "cli-plain"
    target.mkdir()
    rc = main(["repo", "register", "cli-plain", str(target)])
    assert rc != 0
    err = capsys.readouterr().err
    assert "--init" in err


def test_cli_repo_unregister(tmp_path: Path) -> None:
    from hydra_core.cli import main

    target = _git_repo(tmp_path / "cli-unreg")
    assert main(["repo", "register", "cli-unreg", str(target)]) == 0
    assert main(["repo", "unregister", "cli-unreg"]) == 0
    assert not is_known_repo("cli-unreg")
