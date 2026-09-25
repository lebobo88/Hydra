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
``HYDRA_PLAN_ARTIFACT_DEFAULT_ROOT``
                             ``hydra_core.plan_artifact._resolve_default_root``
                             — the plan artifact's "no target repo" fallback
                             root (WS1 follow-up); pointed at each test's own
                             ``tmp_path``, never the checkout's ``docs/plans/``
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
#: Same `.tmp-pytest` base the root `conftest.py`'s `tmp_path` override uses
#: -- the sandbox boundary `plan_artifact._resolve_default_root` checks
#: against (see `HYDRA_PLAN_ARTIFACT_SANDBOX_ROOT` below).
_TMP_PYTEST_ROOT = Path(__file__).resolve().parent.parent / ".tmp-pytest"

HERMETIC_HOME = _TMP_PYTEST_ROOT / "hydra-home"

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

    # WS1 follow-up: unlike backends.json (deliberately seeded once and
    # reused), `repos.json` must start EVERY session empty. `HERMETIC_HOME`
    # is a directory that persists ACROSS separate `pytest` invocations (it
    # is never wiped on disk), and `hydra_core.repo_registry._repos_json_
    # path()` now resolves under `$HYDRA_HOME` -- so any test that writes an
    # operator-registered repo entry without its own per-test isolation (a
    # bug in the TEST, not in the resolver) leaves a stale entry that
    # silently shadows a real allow-listed repo_id (e.g. "hydra" itself) for
    # every subsequent session, breaking unrelated tests with no visible
    # connection to the actual cause. This function runs only at session
    # start (module import + the session-scoped `hermetic_hydra_home`
    # fixture below), never per-test, so this is a one-time-per-session
    # reset, not a mid-session wipe of a test's own in-progress state.
    for stale in (HERMETIC_HOME / "repos.json", HERMETIC_HOME / "repos.json.lock"):
        stale.unlink(missing_ok=True)

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
def _hermetic_hydra_env(request, monkeypatch, tmp_path):
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
    # P5b: HYDRA_PLAN_PHASE now ships ON by default (unset == on; "0" is the
    # only disable spelling -- see _plan_phase_enabled's docstring). This is
    # no longer a blanket pin forcing a particular value across the suite --
    # it is the same leaked-env hygiene as HYDRA_CLAUDE_ENGINEER/
    # HYDRA_BEST_OF_N above: clearing whatever the operator's own shell
    # happens to have set so every test run exercises the actual SHIPPING
    # default (unset -> on) reproducibly, instead of silently inheriting
    # whatever a developer's local `export HYDRA_PLAN_PHASE=0` left behind.
    # Tests that need legacy (flag-off) behaviour opt out explicitly via
    # ``monkeypatch.setenv("HYDRA_PLAN_PHASE", "0")``; tests that need no
    # attended host at all pass ``force_trivial_plan_rigor=True`` to
    # ``build_supervisor`` instead (the same signal every hostless production
    # caller must set -- see cli.py's `_cmd_run`/`_cmd_replay`).
    monkeypatch.delenv("HYDRA_PLAN_PHASE", raising=False)
    # WS1 follow-up: redirect `plan_artifact.plan_artifact_repo_root`'s
    # "no resolved target repo" default root at this test's own `tmp_path`.
    # A wide swath of the suite hardcodes `Path(__file__).resolve().
    # parents[1]` (the checkout it happens to run from) as that fallback --
    # see e.g. `test_p5b_plan_lifecycle.py`'s `HYDRA_ROOT`, `test_hydra69_
    # part3.py`'s `_StubDispatcher`/`_build_sup` closures -- which meant a
    # PLAN write with no target repo landed a REAL `.html` file under the
    # tracked `docs/plans/` of whatever checkout pytest was invoked from
    # (the operator's own, when run outside an isolated worktree). Tests
    # that pass `tmp_path` itself as `default_root`/`project_root` (the
    # majority of the direct `plan_artifact_repo_root` unit tests in
    # `test_plan_artifact_target_repo.py`) are unaffected: `_resolve_default_
    # root`'s sandbox-root check (below) leaves any `default_root` that
    # already resolves inside `.tmp-pytest/` untouched, whatever specific
    # subdirectory a test happens to pick.
    monkeypatch.setenv("HYDRA_PLAN_ARTIFACT_DEFAULT_ROOT", str(tmp_path))
    monkeypatch.setenv("HYDRA_PLAN_ARTIFACT_SANDBOX_ROOT", str(_TMP_PYTEST_ROOT))


# --------------------------------------------------------------------------
# Real-checkout / real-~/.hydra pollution guard.
# --------------------------------------------------------------------------
#
# Both real paths below are resolved at MODULE IMPORT TIME, before anything
# in this file (or in `hydra_core`) could apply the `HYDRA_HOME` redirect
# installed above -- and neither depends on the redirected values:
#   * `_REAL_CHECKOUT_DOCS_PLANS` is derived from `__file__` (this module's
#     own on-disk location), the same anchor `HERMETIC_HOME` above uses --
#     in a normal checkout that is `<repo>/docs/plans`; in an isolated
#     worktree it is that worktree's own `docs/plans` (never the operator's
#     real checkout), which is exactly the boundary this guard is meant to
#     protect regardless of which checkout the suite happens to run from.
#   * `_REAL_HOME_REPOS_JSON` is derived from `Path.home()`, which reads
#     `HOME`/`USERPROFILE` -- never `HYDRA_HOME` -- so it is unaffected by
#     the redirect above no matter the import order.
import hashlib as _hashlib

_REAL_CHECKOUT_DOCS_PLANS = Path(__file__).resolve().parent.parent / "docs" / "plans"
_REAL_HOME_REPOS_JSON = Path.home() / ".hydra" / "repos.json"


# ---------------------------------------------------------------------------
# Pure helpers -- parameterized on the watched paths so they are unit
# testable against throwaway tmp_path copies without ever touching the two
# real paths above. The session fixture below is the only caller that
# passes the real paths; every other caller (the unit tests further down)
# passes tmp_path stand-ins.
# ---------------------------------------------------------------------------


def _snapshot_plans_dir(plans_dir: Path) -> dict[str, str]:
    """Return a mapping of top-level file name -> sha256 of its bytes.

    Read-only: only ``.is_dir()``, ``.iterdir()``, ``.is_file()``, and
    ``.read_bytes()`` are used -- never a write. Content-hashing (rather than
    just tracking names) is required to detect an in-place overwrite of a
    pre-existing file, which a name-only snapshot would miss entirely.
    """
    if not plans_dir.is_dir():
        return {}
    snapshot: dict[str, str] = {}
    for p in plans_dir.iterdir():
        if p.is_file():
            snapshot[p.name] = _hashlib.sha256(p.read_bytes()).hexdigest()
    return snapshot


def _snapshot_repos_json_sha256(repos_json: Path) -> str | None:
    if not repos_json.is_file():
        return None
    return _hashlib.sha256(repos_json.read_bytes()).hexdigest()


def _diff_plans_dir(
    plans_dir: Path,
    before_plans: dict[str, str],
    after_plans: dict[str, str],
) -> list[str]:
    """Pure comparison of a single watched plans-dir snapshot pair. Split out
    of ``_diff_pollution_guard_snapshots`` so the fixture below can run the
    same added/removed/modified comparison against MULTIPLE watched plans
    dirs (the checkout the suite runs from, plus -- in a worktree run -- the
    operator's actual main checkout) without duplicating the repos.json
    check per dir."""
    added = sorted(set(after_plans) - set(before_plans))
    removed = sorted(set(before_plans) - set(after_plans))
    modified = sorted(
        name
        for name in set(before_plans) & set(after_plans)
        if before_plans[name] != after_plans[name]
    )
    problems: list[str] = []
    if added:
        problems.append(
            "new file(s) appeared under the watched plans dir "
            f"({plans_dir}): {added!r}"
        )
    if removed:
        problems.append(
            "file(s) disappeared from the watched plans dir "
            f"({plans_dir}): {removed!r}"
        )
    if modified:
        problems.append(
            "file(s) were overwritten under the watched plans dir "
            f"({plans_dir}): {modified!r}"
        )
    return problems


def _diff_repos_json(
    repos_json: Path,
    before_repos_sha: str | None,
    after_repos_sha: str | None,
) -> list[str]:
    """Pure comparison of a single watched repos.json digest pair."""
    if after_repos_sha != before_repos_sha:
        return [
            f"the watched repos.json changed ({repos_json}): "
            f"sha256 before={before_repos_sha!r} after={after_repos_sha!r}"
        ]
    return []


def _diff_pollution_guard_snapshots(
    *,
    plans_dir: Path,
    repos_json: Path,
    before_plans: dict[str, str],
    before_repos_sha: str | None,
    after_plans: dict[str, str],
    after_repos_sha: str | None,
) -> list[str]:
    """Pure comparison: given a before/after snapshot pair (already taken by
    the caller), return the list of human-readable problem strings -- empty
    if nothing changed. Takes no filesystem action itself. Thin orchestrator
    over ``_diff_plans_dir``/``_diff_repos_json`` above -- kept as its own
    function (rather than inlined at each call site) because it is the
    signature the existing unit tests in
    ``test_conftest_pollution_guard.py`` exercise directly."""
    return _diff_plans_dir(plans_dir, before_plans, after_plans) + _diff_repos_json(
        repos_json, before_repos_sha, after_repos_sha
    )


def _resolve_main_checkout_plans_dir() -> Path | None:
    """Resolve the ``docs/plans`` dir of the repo the BUILTIN ``"hydra"``
    repo id points at, independent of anything an individual test does --
    the exact resolution `hydra_core.plan_artifact.plan_artifact_repo_root`
    performs via `hydra_core.repo_registry.resolve_repo_path("hydra")` for
    any workflow whose ``target_repo_id`` is the builtin ``"hydra"`` id
    (e.g. ``hydra plan --repo hydra``, or a DEV_TASK step naming
    ``target_repo_id: "hydra"``, as `tests/test_hydra69_followup.py`'s
    `_cmd_plan`-driven E2E test does).

    This is a SEPARATE watched path from ``_REAL_CHECKOUT_DOCS_PLANS``
    (derived from this file's own on-disk location): when the suite runs
    from an isolated WORKTREE, ``_REAL_CHECKOUT_DOCS_PLANS`` is that
    worktree's own ``docs/plans`` -- but `resolve_repo_path("hydra")`'s
    worktree-aware base-directory probe (`repo_registry._get_base()`)
    still resolves to the OPERATOR'S actual main checkout regardless of
    which worktree is running, because "hydra" means "the one real Hydra
    repo" by design. A test that resolves a write root through that path
    without redirecting it lands a real file in the main checkout even
    though the suite itself is sandboxed -- this is the leak the guard
    must also watch for.

    Read-only: `resolve_repo_path` only runs `git rev-parse
    --show-toplevel` (a local, no-network git invocation), never a write.
    Returns ``None`` (never raises) if resolution fails for any reason,
    so a guard that cannot resolve the main checkout degrades to watching
    only the suite's own checkout rather than crashing every session.
    """
    try:
        from hydra_core.repo_registry import resolve_repo_path

        return resolve_repo_path("hydra") / "docs" / "plans"
    except Exception:  # noqa: BLE001 -- guard scope degrading beats crashing
        return None


def _watched_plans_dirs() -> list[Path]:
    """All real ``docs/plans`` directories this pytest session's tests must
    never write into, deduplicated. See ``_resolve_main_checkout_plans_dir``
    for why a worktree run needs a SECOND watched dir beyond its own."""
    dirs = {_REAL_CHECKOUT_DOCS_PLANS.resolve()}
    main_dir = _resolve_main_checkout_plans_dir()
    if main_dir is not None:
        try:
            dirs.add(main_dir.resolve())
        except OSError:
            pass
    return sorted(dirs)


# Backwards-compatible aliases bound to the two REAL paths -- kept so the
# session fixture below reads exactly as before (real-path snapshot calls),
# while the pure logic above is the thing under unit test.
def _snapshot_real_plans_dir() -> dict[str, str]:
    return _snapshot_plans_dir(_REAL_CHECKOUT_DOCS_PLANS)


def _snapshot_real_repos_json_sha256() -> str | None:
    return _snapshot_repos_json_sha256(_REAL_HOME_REPOS_JSON)


@pytest.fixture(scope="session", autouse=True)
def _real_hydra_home_pollution_guard():
    """Fail the session if any test wrote into a REAL checkout's tracked
    ``docs/plans/`` (the suite's own checkout AND -- when a worktree run
    resolves it differently -- the operator's actual main checkout; see
    ``_resolve_main_checkout_plans_dir``) or mutated the operator's REAL
    ``~/.hydra/repos.json``.

    READ-ONLY by construction: every call this fixture makes against the
    watched real paths is ``.is_dir()``, ``.is_file()``, ``.iterdir()``, or
    ``.read_bytes()`` -- never a write, ``unlink``, ``rename``, or
    ``mkdir`` -- plus the local, no-network ``git rev-parse
    --show-toplevel`` that ``resolve_repo_path`` runs to find the main
    checkout. Pre-existing litter (files already present before the session
    started) is never flagged or touched; only NEW files / a CHANGED hash /
    a REMOVED file trips this guard. The actual snapshot/compare logic
    lives in ``_snapshot_plans_dir``/``_snapshot_repos_json_sha256``/
    ``_diff_pollution_guard_snapshots``/``_diff_plans_dir`` above, which are
    unit tested against tmp_path stand-ins in
    ``test_conftest_pollution_guard.py`` -- this fixture only supplies the
    real paths and the before/after timing.
    """
    watched_plans_dirs = _watched_plans_dirs()
    before_plans_by_dir = {d: _snapshot_plans_dir(d) for d in watched_plans_dirs}
    before_repos_sha = _snapshot_real_repos_json_sha256()
    yield
    after_plans_by_dir = {d: _snapshot_plans_dir(d) for d in watched_plans_dirs}
    after_repos_sha = _snapshot_real_repos_json_sha256()

    problems: list[str] = []
    for plans_dir in watched_plans_dirs:
        problems.extend(
            _diff_plans_dir(
                plans_dir,
                before_plans_by_dir[plans_dir],
                after_plans_by_dir[plans_dir],
            )
        )
    problems.extend(
        _diff_repos_json(_REAL_HOME_REPOS_JSON, before_repos_sha, after_repos_sha)
    )
    if problems:
        pytest.fail(
            "HERMETIC BOUNDARY VIOLATION -- a test wrote outside the "
            "sandbox: " + "; ".join(problems),
            pytrace=False,
        )
