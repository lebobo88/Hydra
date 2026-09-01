"""Shared pytest fixtures for the Hydra test suite.

E2-26 — hermetic Hydra home
===========================
The suite used to run against the operator's real ``~/.hydra``: it spawned the
live TheEights / pair-programmer / AgentSmith stdio daemons out of
``~/.hydra/backends.json`` and wrote test failure payloads into the production
``~/.hydra/eights-pending`` spool (463 entries in a 40-minute window). This
module redirects every piece of Hydra state that a test can touch to a
throwaway directory under ``<repo>/.tmp-pytest/`` and disables daemon spawning
outright.

The redirect happens at **module import time**, not inside a fixture, because
several ``hydra_core`` modules bind their paths as import-time constants or as
default arguments (``memory.EPISODIC_DB``, ``dispatcher.BACKEND_REGISTRY``).
pytest imports this conftest before it imports any test module, which is the
last moment at which setting the environment still wins. The session-scoped
``hermetic_hydra_home`` fixture below re-asserts the same environment so the
invariant is visible (and enforced) at run time as well.

Environment overrides used, and where each is resolved:

===========================  ================================================
``HYDRA_HOME``               ``hydra_core.memory.DEFAULT_DIR`` (added E2-26)
``HYDRA_EPISODIC_DB``        ``hydra_core.memory.EPISODIC_DB`` (added E2-26)
``HYDRA_CHECKPOINT_DB``      ``hydra_core.supervisor`` (pre-existing, C2)
``HYDRA_BACKENDS``           ``hydra_core.dispatcher.BACKEND_REGISTRY``
                             (added E2-26)
``HYDRA_EIGHTS_SPOOL``       ``pending_spool.resolve_spool_root`` (name
                             pre-existing in the CLI; now also honoured by
                             ``PendingSpool()`` itself — E2-26)
``HYDRA_EIGHTS_DEAD_LETTER`` ``pending_spool.resolve_dead_letter_root``
                             (added E2-26)
``HYDRA_TEST_NO_DAEMONS``    ``hydra_core.dispatcher`` spawn guard (added
                             E2-26); unset for ``live_daemon`` tests
===========================  ================================================

A test that genuinely needs a live daemon carries ``@pytest.mark.live_daemon``.
Those tests are skipped by default and run with ``pytest -m live_daemon``,
which also lifts the spawn guard for the marked test only.
"""
from __future__ import annotations

import json
import os
from pathlib import Path

import pytest


# --------------------------------------------------------------------------
# Hermetic Hydra home — installed at conftest import, before hydra_core loads.
# --------------------------------------------------------------------------

#: Repo-local because the sandboxed Windows runs cannot write the system temp
#: dir (same constraint the root conftest's ``tmp_path`` override documents).
HERMETIC_HOME = (
    Path(__file__).resolve().parent.parent / ".tmp-pytest" / "hydra-home"
)

LIVE_DAEMON_MARKER = "live_daemon"
NO_DAEMONS_ENV = "HYDRA_TEST_NO_DAEMONS"


def _install_hermetic_env() -> dict[str, str]:
    """Point every Hydra state path at :data:`HERMETIC_HOME`. Idempotent."""
    HERMETIC_HOME.mkdir(parents=True, exist_ok=True)

    # A backend registry with NO stdio backends: even if the spawn guard were
    # lifted, there is no server spec to fork. `_load_backend_registry` expects
    # a flat ``{name: spec}`` object.
    backends = HERMETIC_HOME / "backends.json"
    if not backends.exists():
        backends.write_text(json.dumps({}, indent=2), encoding="utf-8")

    env = {
        "HYDRA_HOME": str(HERMETIC_HOME),
        "HYDRA_EPISODIC_DB": str(HERMETIC_HOME / "episodic.db"),
        "HYDRA_CHECKPOINT_DB": str(HERMETIC_HOME / "checkpoints.db"),
        "HYDRA_BACKENDS": str(backends),
        "HYDRA_EIGHTS_SPOOL": str(HERMETIC_HOME / "eights-pending"),
        "HYDRA_EIGHTS_DEAD_LETTER": str(HERMETIC_HOME / "eights-pending-dead"),
        NO_DAEMONS_ENV: "1",
    }
    os.environ.update(env)
    return env


HERMETIC_ENV = _install_hermetic_env()


def pytest_collection_modifyitems(config, items):
    """Skip ``live_daemon`` tests unless they were explicitly selected.

    ``-m live_daemon`` (or any ``-m`` expression naming the marker) opts in;
    a default run never spawns a daemon.
    """
    selected = config.getoption("markexpr", default="") or ""
    if LIVE_DAEMON_MARKER in selected:
        return
    skip = pytest.mark.skip(
        reason="needs a live MCP daemon; run with -m live_daemon"
    )
    for item in items:
        if item.get_closest_marker(LIVE_DAEMON_MARKER) is not None:
            item.add_marker(skip)


@pytest.fixture(scope="session", autouse=True)
def hermetic_hydra_home() -> dict[str, str]:
    """Session guarantee that the hermetic environment is in place.

    The environment was already installed at import time (see the module
    docstring); this fixture re-asserts it so a stray ``os.environ.clear()``
    in one test cannot silently un-hermetic the rest of the session, and so
    the redirect is discoverable as a fixture rather than only as a side
    effect of importing this file.
    """
    return _install_hermetic_env()


@pytest.fixture(autouse=True)
def _hermetic_hydra_env(request, monkeypatch):
    """Per-test env hygiene.

    * Re-affirms the hermetic Hydra paths (a previous test may have pointed
      one of them elsewhere with its own ``monkeypatch``).
    * Lifts the daemon spawn guard for ``@pytest.mark.live_daemon`` tests.
    * Keeps the operator's ``HYDRA_CLAUDE_ENGINEER`` /
      ``HYDRA_DISABLE_CLAUDE_ENGINEER`` / ``HYDRA_BEST_OF_N`` shell env out of
      the run. ``HYDRA_CLAUDE_ENGINEER=1`` is an explicit force-on for
      headless Claude generation; if it leaked in, a scripted-dispatcher test
      exercising the drive loop would try to spawn a real ``claude``
      subprocess. Opt-in best-of-N must not leak in either and silently turn
      single-candidate drive tests into best-of runs (which would spawn real
      worktrees/subprocesses). Tests that need them opt in via
      ``monkeypatch.setenv`` in-test.
    """
    for key, value in HERMETIC_ENV.items():
        monkeypatch.setenv(key, value)
    if request.node.get_closest_marker(LIVE_DAEMON_MARKER) is not None:
        monkeypatch.delenv(NO_DAEMONS_ENV, raising=False)
    monkeypatch.delenv("HYDRA_CLAUDE_ENGINEER", raising=False)
    monkeypatch.delenv("HYDRA_DISABLE_CLAUDE_ENGINEER", raising=False)
    monkeypatch.delenv("HYDRA_BEST_OF_N", raising=False)
