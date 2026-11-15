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
import functools as _functools
import hashlib as _hashlib
import html as _html
import re as _re
import uuid as _uuid
from collections.abc import Callable, Collection

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


_UUID_LI_RE = _re.compile(r"<li>\s*workflow_id:\s*([^<]*?)\s*</li>")
_PROVENANCE_HEADING = "<h2>Provenance</h2>"


def _canonical_uuid(value: object) -> str | None:
    """Canonical lowercase hyphenated UUID string, or ``None`` if ``value`` is
    not exactly that form."""
    text = str(value).strip().lower()
    try:
        parsed = _uuid.UUID(text)
    except (ValueError, AttributeError, TypeError):
        return None
    return text if str(parsed) == text else None


def _read_plan_workflow_id(path: Path) -> str | None:
    """Return the workflow_id a plan artifact claims, or ``None``.

    ``.html``: the ``<li>workflow_id: ...</li>`` line that
    ``hydra_core.plan_artifact.render_plan_html`` emits under
    ``<h2>Provenance</h2>`` (html-unescaped). ``.json``: a top-level
    ``workflow_id`` key. ``None`` for an unreadable file, a missing line/key,
    or a value that is not a canonical UUID. READ-ONLY: only ``read_bytes``.
    """
    try:
        text = path.read_bytes().decode("utf-8")
    except (OSError, UnicodeDecodeError):
        return None
    if path.suffix.lower() == ".json":
        try:
            data = json.loads(text)
        except ValueError:
            return None
        if isinstance(data, dict) and "workflow_id" in data:
            return _canonical_uuid(data["workflow_id"])
        return None
    idx = text.rfind(_PROVENANCE_HEADING)
    if idx < 0:
        return None
    match = _UUID_LI_RE.search(text, idx)
    if match is None:
        return None
    return _canonical_uuid(_html.unescape(match.group(1)))


class ConcurrentPlanWriterWarning(pytest.PytestWarning):
    """A new plan artifact with a workflow_id this session did not create
    appeared in a watched plans dir (an operator/other process writing
    concurrently). Reported, never fatal."""


def _classify_plans_dir_changes(
    plans_dir: Path,
    before_plans: dict[str, str],
    after_plans: dict[str, str],
    session_workflow_ids: Collection[str] = frozenset(),
    read_workflow_id: Callable[[Path], str | None] = _read_plan_workflow_id,
) -> tuple[list[str], list[str]]:
    """Pure classifier of one watched plans-dir snapshot pair.

    Returns ``(violations, warnings)``. Per file:

    * added, no readable workflow_id -> violation (fail closed)
    * added, workflow_id in ``session_workflow_ids`` -> violation
    * added, any other (foreign) workflow_id -> warning only
    * removed -> violation
    * modified pre-existing file -> violation, even with a foreign id
    """
    session_ids = {_canonical_uuid(i) or str(i) for i in session_workflow_ids}
    violations: list[str] = []
    warnings_: list[str] = []
    for name in sorted(set(after_plans) - set(before_plans)):
        path = plans_dir / name
        wid = read_workflow_id(path)
        if wid is None:
            violations.append(f"{path}: new file, no readable workflow_id")
        elif (_canonical_uuid(wid) or wid) in session_ids:
            violations.append(
                f"{path}: new file, workflow_id {wid} created by this test session"
            )
        else:
            warnings_.append(
                f"{path}: new file with workflow_id {wid} -- "
                "concurrent writer outside the test session"
            )
    for name in sorted(set(before_plans) - set(after_plans)):
        violations.append(
            f"{plans_dir / name}: file disappeared from the watched plans dir (removed)"
        )
    for name in sorted(
        n for n in set(before_plans) & set(after_plans) if before_plans[n] != after_plans[n]
    ):
        violations.append(
            f"{plans_dir / name}: pre-existing file was overwritten (modified)"
        )
    return violations, warnings_


def _diff_plans_dir(
    plans_dir: Path,
    before_plans: dict[str, str],
    after_plans: dict[str, str],
    session_workflow_ids: Collection[str] = frozenset(),
    read_workflow_id: Callable[[Path], str | None] = _read_plan_workflow_id,
) -> list[str]:
    """Violations-only view of ``_classify_plans_dir_changes`` (the original
    list[str] contract); foreign-id warnings are dropped here -- callers that
    want them use the classifier directly."""
    return _classify_plans_dir_changes(
        plans_dir, before_plans, after_plans, session_workflow_ids, read_workflow_id
    )[0]


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
    session_workflow_ids: Collection[str] = frozenset(),
    read_workflow_id: Callable[[Path], str | None] = _read_plan_workflow_id,
    warnings_out: list[str] | None = None,
) -> list[str]:
    """Pure comparison: given a before/after snapshot pair (already taken by
    the caller), return the list of human-readable violation strings -- empty
    if nothing (fatal) changed. Takes no filesystem action itself beyond the
    injected ``read_workflow_id``. Concurrent-writer warnings are appended to
    ``warnings_out`` when given. The original keyword arguments are unchanged;
    the three new ones are optional."""
    violations, warns = _classify_plans_dir_changes(
        plans_dir, before_plans, after_plans, session_workflow_ids, read_workflow_id
    )
    if warnings_out is not None:
        warnings_out.extend(warns)
    return violations + _diff_repos_json(repos_json, before_repos_sha, after_repos_sha)


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


# --------------------------------------------------------------------------
# Provenance: which workflow ids did THIS session create?
# --------------------------------------------------------------------------
#
# Every in-process plan-artifact HTML is produced by
# ``hydra_core.plan_artifact.render_plan_html``. ingest.py and supervisor.py
# each reach it through a FUNCTION-LOCAL ``from .plan_artifact import
# render_plan_html`` (re-reads the module attribute at call time), so wrapping
# the module attribute once is seen by both. The wrapper is installed in
# ``pytest_sessionstart`` (before collection, so test modules importing the
# name at module top also bind the wrapper) and records ``plan.workflow_id``
# of every call. The set only grows.
# Blind spots: subprocess writers (separate interpreter, unwrapped); anything
# that bound the original function before session start; hand-written
# artifacts that never go through render_plan_html (their id looks foreign ->
# warning, not violation, if they carry a valid UUID).
_SESSION_WORKFLOW_IDS: set[str] = set()


def _install_workflow_id_capture(sink: set[str] = _SESSION_WORKFLOW_IDS) -> bool:
    """Wrap ``plan_artifact.render_plan_html`` to record workflow ids into
    ``sink``. Idempotent. Returns True if a wrapper was installed now."""
    from hydra_core import plan_artifact

    original = plan_artifact.render_plan_html
    if getattr(original, "_hermetic_workflow_id_capture", False):
        return False

    @_functools.wraps(original)
    def _capturing_render_plan_html(plan, *args, **kwargs):
        wid = getattr(plan, "workflow_id", None)
        if wid is not None:
            sink.add(_canonical_uuid(wid) or str(wid))
        return original(plan, *args, **kwargs)

    setattr(_capturing_render_plan_html, "_hermetic_workflow_id_capture", True)
    plan_artifact.render_plan_html = _capturing_render_plan_html
    return True


class _PollutionGuardPlugin:
    """Session-level read-only guard over the real ``docs/plans`` dir(s) (the
    suite's own checkout AND, in a worktree run, the main checkout -- see
    ``_resolve_main_checkout_plans_dir``) and the real ``~/.hydra/repos.json``.

    ``pytest_sessionstart`` snapshots; ``pytest_sessionfinish`` classifies,
    emits non-fatal ``ConcurrentPlanWriterWarning``s and, on violations,
    forces a non-zero exit status; ``pytest_terminal_summary`` prints the
    per-file report. READ-ONLY against watched paths: only ``is_dir``,
    ``is_file``, ``iterdir``, ``read_bytes`` (plus the local no-network
    ``git rev-parse`` inside ``_watched_plans_dirs``). Pre-existing litter is
    never flagged. The report does not claim which test wrote a file.
    """

    def __init__(
        self,
        *,
        plans_dirs: Callable[[], list[Path]] = _watched_plans_dirs,
        repos_json: Path = _REAL_HOME_REPOS_JSON,
        session_workflow_ids: Collection[str] = _SESSION_WORKFLOW_IDS,
        install_capture: bool = True,
    ) -> None:
        self._plans_dirs_fn = plans_dirs
        self._repos_json = repos_json
        self._session_ids = session_workflow_ids
        self._install_capture = install_capture
        self._started = False
        self._dirs: list[Path] = []
        self._before_plans: dict[Path, dict[str, str]] = {}
        self._before_repos: str | None = None
        self.violations: list[str] = []
        self.warnings: list[str] = []
        self.escalated_warnings: list[str] = []

    def pytest_sessionstart(self, session) -> None:
        if hasattr(session.config, "workerinput"):  # xdist worker
            return
        if self._install_capture:
            _install_workflow_id_capture()
        self._dirs = self._plans_dirs_fn()
        self._before_plans = {d: _snapshot_plans_dir(d) for d in self._dirs}
        self._before_repos = _snapshot_repos_json_sha256(self._repos_json)
        self._started = True

    def pytest_sessionfinish(self, session, exitstatus) -> None:
        if not self._started:
            return
        violations: list[str] = []
        warns: list[str] = []
        for d in self._dirs:
            v, w = _classify_plans_dir_changes(
                d, self._before_plans[d], _snapshot_plans_dir(d), self._session_ids
            )
            violations.extend(v)
            warns.extend(w)
        violations.extend(
            _diff_repos_json(
                self._repos_json,
                self._before_repos,
                _snapshot_repos_json_sha256(self._repos_json),
            )
        )
        self.violations, self.warnings = violations, warns
        self.escalated_warnings = []
        for text in warns:
            try:
                session.config.issue_config_time_warning(
                    ConcurrentPlanWriterWarning(text), stacklevel=2
                )
            except ConcurrentPlanWriterWarning:
                # A warning filter (``-W error`` / ``filterwarnings = error``)
                # escalated the notice to an exception. It must stay
                # non-fatal: keep the text and print it in the terminal
                # summary instead. Only this class is caught.
                self.escalated_warnings.append(text)
        if violations and session.exitstatus == pytest.ExitCode.OK:
            session.exitstatus = pytest.ExitCode.TESTS_FAILED

    def pytest_terminal_summary(self, terminalreporter) -> None:
        for text in self.escalated_warnings:
            terminalreporter.write_line(
                f"ConcurrentPlanWriterWarning (non-fatal; escalated by a "
                f"warning filter): {text}",
                yellow=True,
            )
        if not self.violations:
            return
        terminalreporter.write_sep(
            "=",
            "HERMETIC BOUNDARY VIOLATION -- a watched path changed during the "
            "test session",
            red=True,
        )
        for line in self.violations:
            terminalreporter.write_line(f"  - {line}", red=True)


def pytest_configure(config) -> None:
    if hasattr(config, "workerinput"):
        return
    config.pluginmanager.register(_PollutionGuardPlugin(), "hermetic-pollution-guard")
