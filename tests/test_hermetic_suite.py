"""E2-26 guard: the suite must not touch the operator's live Hydra state.

These tests fail loudly if the hermetic redirect installed by
``tests/conftest.py`` regresses — either because an env override stopped being
honoured by its resolving module, or because the daemon spawn guard stopped
short-circuiting ``MCPStdioDispatcher.call_mcp``.
"""
from __future__ import annotations

import os
from pathlib import Path

import pytest

from hydra_core import dispatcher as dispatcher_mod
from hydra_core import memory as memory_mod
from hydra_core.dispatcher import MCPStdioDispatcher
from hydra_core.eights import pending_spool


# Recomputed here rather than imported from ``conftest``: with a rootdir
# conftest.py present, the module name ``conftest`` is ambiguous.
HERMETIC_HOME = (
    Path(__file__).resolve().parent.parent / ".tmp-pytest" / "hydra-home"
)
REAL_HYDRA_HOME = Path.home() / ".hydra"


def _under_hermetic_home(path: Path) -> bool:
    try:
        Path(path).resolve().relative_to(HERMETIC_HOME.resolve())
    except ValueError:
        return False
    return True


@pytest.mark.parametrize(
    "env_var",
    [
        "HYDRA_HOME",
        "HYDRA_EPISODIC_DB",
        "HYDRA_CHECKPOINT_DB",
        "HYDRA_BACKENDS",
        "HYDRA_EIGHTS_SPOOL",
        "HYDRA_EIGHTS_DEAD_LETTER",
    ],
)
def test_state_env_redirected_under_tmp(env_var: str) -> None:
    value = os.environ.get(env_var)
    assert value, f"{env_var} must be set by the hermetic conftest"
    assert _under_hermetic_home(Path(value)), (
        f"{env_var}={value!r} escapes the hermetic home {HERMETIC_HOME}"
    )


def test_module_constants_follow_the_env() -> None:
    """The resolving modules actually honour the overrides."""
    assert _under_hermetic_home(memory_mod.EPISODIC_DB)
    assert _under_hermetic_home(memory_mod.DEFAULT_DIR)
    assert _under_hermetic_home(dispatcher_mod.BACKEND_REGISTRY)
    assert _under_hermetic_home(pending_spool.resolve_spool_root())
    assert _under_hermetic_home(pending_spool.resolve_dead_letter_root())


def test_default_pending_spool_writes_into_tmp(tmp_path: Path) -> None:
    """``PendingSpool()`` with no explicit root must not target real ~/.hydra."""
    spool = pending_spool.PendingSpool()
    assert _under_hermetic_home(spool.root)
    assert _under_hermetic_home(spool.dead_letter_root)
    assert spool.root != pending_spool.DEFAULT_SPOOL_ROOT
    assert not _is_under(spool.root, REAL_HYDRA_HOME)


def _is_under(path: Path, parent: Path) -> bool:
    try:
        Path(path).resolve().relative_to(parent.resolve())
    except (ValueError, OSError):
        return False
    return True


def test_backend_registry_has_no_stdio_backends() -> None:
    """Even with the guard lifted there is no server spec to fork."""
    assert dispatcher_mod._load_backend_registry() == {}


def test_call_mcp_refuses_to_spawn_a_daemon(tmp_path: Path) -> None:
    """The headline E2-26 assertion: no `node …/daemon/dist/index.js` child."""
    assert dispatcher_mod.daemons_disabled() is True
    disp = MCPStdioDispatcher(project_root=tmp_path)
    result = disp.call_mcp("eights", "eights.squad.list", {})
    assert result == {
        "status": "failed",
        "error": dispatcher_mod.NO_DAEMONS_ERROR,
    }


def test_guard_can_be_lifted(monkeypatch: pytest.MonkeyPatch) -> None:
    """`live_daemon` tests get the real code path back (spawn not attempted
    here — only the branch condition is asserted)."""
    monkeypatch.delenv("HYDRA_TEST_NO_DAEMONS", raising=False)
    assert dispatcher_mod.daemons_disabled() is False


@pytest.mark.live_daemon
def test_live_daemon_marker_opt_in(tmp_path: Path) -> None:
    """Exercises the opt-in path itself.

    Skipped by every default run; under ``pytest -m live_daemon`` the conftest
    lifts ``HYDRA_TEST_NO_DAEMONS`` for marked tests, so this asserts the
    guard is genuinely off. It still contacts no daemon — the hermetic backend
    registry is empty, so the call fails as an unregistered server.
    """
    assert dispatcher_mod.daemons_disabled() is False
    disp = MCPStdioDispatcher(project_root=tmp_path)
    result = disp.call_mcp("eights", "eights.squad.list", {})
    assert result.get("error") != dispatcher_mod.NO_DAEMONS_ERROR
