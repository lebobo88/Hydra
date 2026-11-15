"""Unit tests for the pure snapshot/compare helpers backing
``tests/conftest.py``'s ``_real_hydra_home_pollution_guard`` session fixture.

These exercise the comparison LOGIC against throwaway ``tmp_path`` copies --
never the two real watched paths (the operator's real checkout's
``docs/plans/`` and the real ``~/.hydra/repos.json``). The session fixture
itself stays read-only against those real paths; this file proves the logic
it delegates to actually detects a change, independently of that read-only
constraint.
"""
from __future__ import annotations

from pathlib import Path

from conftest import (
    _diff_pollution_guard_snapshots,
    _snapshot_plans_dir,
    _snapshot_repos_json_sha256,
)


def test_changed_repos_json_digest_is_reported(tmp_path: Path):
    plans_dir = tmp_path / "docs" / "plans"
    plans_dir.mkdir(parents=True)
    repos_json = tmp_path / "repos.json"
    repos_json.write_text('{"hydra": "/repos/hydra"}', encoding="utf-8")

    before_plans = _snapshot_plans_dir(plans_dir)
    before_sha = _snapshot_repos_json_sha256(repos_json)

    repos_json.write_text('{"hydra": "/repos/hydra-TAMPERED"}', encoding="utf-8")

    after_plans = _snapshot_plans_dir(plans_dir)
    after_sha = _snapshot_repos_json_sha256(repos_json)

    problems = _diff_pollution_guard_snapshots(
        plans_dir=plans_dir,
        repos_json=repos_json,
        before_plans=before_plans,
        before_repos_sha=before_sha,
        after_plans=after_plans,
        after_repos_sha=after_sha,
    )
    assert len(problems) == 1
    assert "repos.json changed" in problems[0]


def test_new_file_in_watched_plans_dir_is_reported(tmp_path: Path):
    plans_dir = tmp_path / "docs" / "plans"
    plans_dir.mkdir(parents=True)
    (plans_dir / "existing.html").write_text("<p>pre-existing</p>", encoding="utf-8")
    repos_json = tmp_path / "repos.json"
    repos_json.write_text("{}", encoding="utf-8")

    before_plans = _snapshot_plans_dir(plans_dir)
    before_sha = _snapshot_repos_json_sha256(repos_json)

    (plans_dir / "leaked-plan.html").write_text("<p>leaked</p>", encoding="utf-8")

    after_plans = _snapshot_plans_dir(plans_dir)
    after_sha = _snapshot_repos_json_sha256(repos_json)

    problems = _diff_pollution_guard_snapshots(
        plans_dir=plans_dir,
        repos_json=repos_json,
        before_plans=before_plans,
        before_repos_sha=before_sha,
        after_plans=after_plans,
        after_repos_sha=after_sha,
    )
    assert len(problems) == 1
    assert "leaked-plan.html" in problems[0]
    # Pre-existing litter must never be flagged.
    assert "existing.html" not in problems[0]


def test_absent_to_present_repos_json_is_reported(tmp_path: Path):
    plans_dir = tmp_path / "docs" / "plans"
    plans_dir.mkdir(parents=True)
    repos_json = tmp_path / "repos.json"
    # repos.json does not exist yet at "before" time.

    before_plans = _snapshot_plans_dir(plans_dir)
    before_sha = _snapshot_repos_json_sha256(repos_json)
    assert before_sha is None

    repos_json.write_text('{"leaked": "/repos/leaked"}', encoding="utf-8")

    after_plans = _snapshot_plans_dir(plans_dir)
    after_sha = _snapshot_repos_json_sha256(repos_json)
    assert after_sha is not None

    problems = _diff_pollution_guard_snapshots(
        plans_dir=plans_dir,
        repos_json=repos_json,
        before_plans=before_plans,
        before_repos_sha=before_sha,
        after_plans=after_plans,
        after_repos_sha=after_sha,
    )
    assert len(problems) == 1
    assert "repos.json changed" in problems[0]


def test_overwritten_existing_file_is_reported_as_modified(tmp_path: Path):
    plans_dir = tmp_path / "docs" / "plans"
    plans_dir.mkdir(parents=True)
    (plans_dir / "existing.html").write_text("<p>original</p>", encoding="utf-8")
    (plans_dir / "untouched.html").write_text("<p>leave me alone</p>", encoding="utf-8")
    repos_json = tmp_path / "repos.json"
    repos_json.write_text("{}", encoding="utf-8")

    before_plans = _snapshot_plans_dir(plans_dir)
    before_sha = _snapshot_repos_json_sha256(repos_json)

    # Overwrite an existing file in place -- name-only snapshotting would
    # miss this entirely since the set of names is unchanged.
    (plans_dir / "existing.html").write_text("<p>TAMPERED</p>", encoding="utf-8")

    after_plans = _snapshot_plans_dir(plans_dir)
    after_sha = _snapshot_repos_json_sha256(repos_json)

    problems = _diff_pollution_guard_snapshots(
        plans_dir=plans_dir,
        repos_json=repos_json,
        before_plans=before_plans,
        before_repos_sha=before_sha,
        after_plans=after_plans,
        after_repos_sha=after_sha,
    )
    assert len(problems) == 1
    assert "existing.html" in problems[0]
    assert "overwritten" in problems[0]
    # A file that was neither added, removed, nor modified must never be
    # named in the problem list.
    assert "untouched.html" not in problems[0]


def test_removed_file_is_reported(tmp_path: Path):
    plans_dir = tmp_path / "docs" / "plans"
    plans_dir.mkdir(parents=True)
    (plans_dir / "existing.html").write_text("<p>pre-existing</p>", encoding="utf-8")
    (plans_dir / "doomed.html").write_text("<p>about to vanish</p>", encoding="utf-8")
    repos_json = tmp_path / "repos.json"
    repos_json.write_text("{}", encoding="utf-8")

    before_plans = _snapshot_plans_dir(plans_dir)
    before_sha = _snapshot_repos_json_sha256(repos_json)

    (plans_dir / "doomed.html").unlink()

    after_plans = _snapshot_plans_dir(plans_dir)
    after_sha = _snapshot_repos_json_sha256(repos_json)

    problems = _diff_pollution_guard_snapshots(
        plans_dir=plans_dir,
        repos_json=repos_json,
        before_plans=before_plans,
        before_repos_sha=before_sha,
        after_plans=after_plans,
        after_repos_sha=after_sha,
    )
    assert len(problems) == 1
    assert "doomed.html" in problems[0]
    assert "disappeared" in problems[0]
    assert "existing.html" not in problems[0]


def test_unchanged_state_reports_nothing(tmp_path: Path):
    plans_dir = tmp_path / "docs" / "plans"
    plans_dir.mkdir(parents=True)
    (plans_dir / "existing.html").write_text("<p>pre-existing</p>", encoding="utf-8")
    repos_json = tmp_path / "repos.json"
    repos_json.write_text('{"hydra": "/repos/hydra"}', encoding="utf-8")

    before_plans = _snapshot_plans_dir(plans_dir)
    before_sha = _snapshot_repos_json_sha256(repos_json)

    # Nothing touched between snapshots.

    after_plans = _snapshot_plans_dir(plans_dir)
    after_sha = _snapshot_repos_json_sha256(repos_json)

    problems = _diff_pollution_guard_snapshots(
        plans_dir=plans_dir,
        repos_json=repos_json,
        before_plans=before_plans,
        before_repos_sha=before_sha,
        after_plans=after_plans,
        after_repos_sha=after_sha,
    )
    assert problems == []


# ---------------------------------------------------------------------------
# Provenance classification (workflow_id on added plan files), the extractor,
# the capture wrapper, and the session-level exit-code / warning reporting.
# ---------------------------------------------------------------------------

import uuid  # noqa: E402

import hydra_core.ingest as _ingest_mod  # noqa: E402
import hydra_core.supervisor as _supervisor_mod  # noqa: E402
from conftest import (  # noqa: E402
    _SESSION_WORKFLOW_IDS,
    ConcurrentPlanWriterWarning,
    _classify_plans_dir_changes,
    _install_workflow_id_capture,
    _PollutionGuardPlugin,
    _read_plan_workflow_id,
)

pytest_plugins = ["pytester"]

SESSION_ID: str = "11111111-1111-4111-8111-111111111111"
FOREIGN_ID: str = "22222222-2222-4222-8222-222222222222"


def _plan_html(workflow_id: str | None) -> str:
    li = f"<li>workflow_id: {workflow_id}</li>" if workflow_id is not None else ""
    return f"<h1>Plan</h1><h2>Provenance</h2><ul><li>plan_revision: 1</li>{li}</ul>"


def _setup(tmp_path: Path):
    plans_dir = tmp_path / "docs" / "plans"
    plans_dir.mkdir(parents=True)
    (plans_dir / "existing.html").write_text(_plan_html(FOREIGN_ID), encoding="utf-8")
    repos_json = tmp_path / "repos.json"
    repos_json.write_text("{}", encoding="utf-8")
    return plans_dir, repos_json


def _diff(plans_dir, repos_json, mutate, *, session_ids=(SESSION_ID,)):
    before_plans = _snapshot_plans_dir(plans_dir)
    before_sha = _snapshot_repos_json_sha256(repos_json)
    mutate()
    warns: list[str] = []
    problems = _diff_pollution_guard_snapshots(
        plans_dir=plans_dir,
        repos_json=repos_json,
        before_plans=before_plans,
        before_repos_sha=before_sha,
        after_plans=_snapshot_plans_dir(plans_dir),
        after_repos_sha=_snapshot_repos_json_sha256(repos_json),
        session_workflow_ids=set(session_ids),
        warnings_out=warns,
    )
    return problems, warns


def test_case_a_new_file_with_session_created_workflow_id_is_violation(tmp_path):
    plans_dir, repos_json = _setup(tmp_path)
    problems, warns = _diff(
        plans_dir,
        repos_json,
        lambda: (plans_dir / "new.html").write_text(_plan_html(SESSION_ID), encoding="utf-8"),
    )
    assert len(problems) == 1
    assert "new.html" in problems[0]
    assert f"workflow_id {SESSION_ID} created by this test session" in problems[0]
    assert warns == []


def test_case_b_new_file_without_workflow_id_is_violation(tmp_path):
    plans_dir, repos_json = _setup(tmp_path)
    problems, warns = _diff(
        plans_dir,
        repos_json,
        lambda: (plans_dir / "noid.html").write_text(_plan_html(None), encoding="utf-8"),
    )
    assert len(problems) == 1
    assert "noid.html" in problems[0]
    assert "new file, no readable workflow_id" in problems[0]
    assert warns == []


def test_case_c_new_file_with_foreign_workflow_id_warns_without_failing(tmp_path):
    plans_dir, repos_json = _setup(tmp_path)
    problems, warns = _diff(
        plans_dir,
        repos_json,
        lambda: (plans_dir / "foreign.html").write_text(_plan_html(FOREIGN_ID), encoding="utf-8"),
    )
    assert problems == []
    assert len(warns) == 1
    assert "foreign.html" in warns[0]
    assert FOREIGN_ID in warns[0]
    assert "concurrent writer outside the test session" in warns[0]


def test_case_d_removed_file_is_violation(tmp_path):
    plans_dir, repos_json = _setup(tmp_path)
    problems, _ = _diff(plans_dir, repos_json, lambda: (plans_dir / "existing.html").unlink())
    assert len(problems) == 1
    assert "existing.html" in problems[0]
    assert "removed" in problems[0]


def test_case_e_modified_preexisting_file_with_foreign_id_is_violation(tmp_path):
    plans_dir, repos_json = _setup(tmp_path)
    problems, warns = _diff(
        plans_dir,
        repos_json,
        lambda: (plans_dir / "existing.html").write_text(
            _plan_html(FOREIGN_ID) + "<!-- edited -->", encoding="utf-8"
        ),
    )
    assert len(problems) == 1
    assert "existing.html" in problems[0]
    assert "modified" in problems[0]
    assert warns == []


def test_case_f_repos_json_change_is_violation(tmp_path):
    plans_dir, repos_json = _setup(tmp_path)
    problems, _ = _diff(
        plans_dir, repos_json, lambda: repos_json.write_text('{"x": "/y"}', encoding="utf-8")
    )
    assert len(problems) == 1
    assert "repos.json changed" in problems[0]


def test_classifier_returns_violations_and_warnings_separately(tmp_path):
    plans_dir, _ = _setup(tmp_path)
    after = {"a.html": "1", "b.html": "2"}
    ids = {"a.html": SESSION_ID, "b.html": FOREIGN_ID}
    violations, warns = _classify_plans_dir_changes(
        plans_dir, {}, after, {SESSION_ID}, lambda p: ids[p.name]
    )
    assert len(violations) == 1 and "a.html" in violations[0]
    assert len(warns) == 1 and "b.html" in warns[0]


def test_read_plan_workflow_id_extractor(tmp_path):
    wid = str(uuid.uuid4())
    good = tmp_path / "good.html"
    good.write_text(_plan_html(wid.upper()), encoding="utf-8")
    assert _read_plan_workflow_id(good) == wid  # canonicalised
    escaped = tmp_path / "esc.html"
    escaped.write_text(_plan_html(wid).replace("-", "&#45;"), encoding="utf-8")
    assert _read_plan_workflow_id(escaped) == wid  # html-unescaped
    for name, text in {
        "noline.html": _plan_html(None),
        "notuuid.html": _plan_html("not-a-uuid"),
        "noheading.html": f"<li>workflow_id: {wid}</li>",
    }.items():
        p = tmp_path / name
        p.write_text(text, encoding="utf-8")
        assert _read_plan_workflow_id(p) is None, name
    binary = tmp_path / "bin.html"
    binary.write_bytes(b"\xff\xfe\x00bad")
    assert _read_plan_workflow_id(binary) is None
    assert _read_plan_workflow_id(tmp_path / "missing.html") is None
    j = tmp_path / "p.json"
    j.write_text(f'{{"workflow_id": "{wid}"}}', encoding="utf-8")
    assert _read_plan_workflow_id(j) == wid
    j.write_text("[1]", encoding="utf-8")
    assert _read_plan_workflow_id(j) is None


def test_session_capture_reaches_function_local_import_by_name(tmp_path):
    """ingest.py / supervisor.py import render_plan_html INSIDE functions, so
    they resolve the (wrapped) module attribute at call time. Prove (1) neither
    module holds a module-level binding, (2) the by-name import ingest performs
    yields the capturing wrapper, (3) a render records the id and the rendered
    HTML round-trips through the extractor."""
    from hydra_core.schemas import Plan

    assert not hasattr(_ingest_mod, "render_plan_html")
    assert not hasattr(_supervisor_mod, "render_plan_html")
    _install_workflow_id_capture()  # idempotent; sessionstart already did it
    from hydra_core.plan_artifact import render_plan_html  # same form ingest uses

    assert getattr(render_plan_html, "_hermetic_workflow_id_capture", False)
    wid = uuid.uuid4()
    plan = Plan(
        origin_squad="hydra", workflow_id=wid, rigor="standard",
        goal_restatement="g", summary="s", steps=[],
    )
    html_text = render_plan_html(plan)
    assert str(wid) in _SESSION_WORKFLOW_IDS
    p = tmp_path / "rendered.html"
    p.write_text(html_text, encoding="utf-8")
    assert _read_plan_workflow_id(p) == str(wid)


_WRITER: str = """
from pathlib import Path

def test_writes():
    Path({target!r}).write_text({body!r}, encoding="utf-8")
"""


def _run_session(
    pytester, plans_dir, repos_json, target_name, body, session_ids=(), extra_args=()
):
    plugin = _PollutionGuardPlugin(
        plans_dirs=lambda: [plans_dir],
        repos_json=repos_json,
        session_workflow_ids=set(session_ids),
        install_capture=False,
    )
    pytester.makepyfile(
        test_writer=_WRITER.format(target=str(plans_dir / target_name), body=body)
    )
    return pytester.runpytest_inprocess(
        "-p", "no:cacheprovider", *extra_args, plugins=[plugin]
    )


def test_session_violation_yields_nonzero_exit_and_report(pytester, tmp_path):
    plans_dir, repos_json = _setup(tmp_path)
    result = _run_session(pytester, plans_dir, repos_json, "leak.html", _plan_html(None))
    assert result.ret != 0
    result.stdout.fnmatch_lines(
        ["*HERMETIC BOUNDARY VIOLATION -- a watched path changed during the test session*"]
    )
    result.stdout.fnmatch_lines(["*leak.html: new file, no readable workflow_id*"])


def test_session_foreign_only_change_exits_zero_with_warning(pytester, tmp_path):
    plans_dir, repos_json = _setup(tmp_path)
    result = _run_session(
        pytester, plans_dir, repos_json, "other.html", _plan_html(FOREIGN_ID)
    )
    assert result.ret == 0
    out = result.stdout.str()
    assert "ConcurrentPlanWriterWarning" in out
    assert "concurrent writer outside the test session" in out
    assert FOREIGN_ID in out
    assert "HERMETIC BOUNDARY VIOLATION" not in out
    assert issubclass(ConcurrentPlanWriterWarning, Warning)


def test_session_foreign_only_change_under_w_error_still_exits_zero(pytester, tmp_path):
    plans_dir, repos_json = _setup(tmp_path)
    result = _run_session(
        pytester, plans_dir, repos_json, "other.html", _plan_html(FOREIGN_ID),
        extra_args=("-W", "error"),
    )
    assert result.ret == 0
    out = result.stdout.str()
    assert "other.html" in out
    assert FOREIGN_ID in out
    assert "concurrent writer outside the test session" in out
    assert "HERMETIC BOUNDARY VIOLATION" not in out


def test_session_violation_under_w_error_still_exits_nonzero(pytester, tmp_path):
    plans_dir, repos_json = _setup(tmp_path)
    result = _run_session(
        pytester, plans_dir, repos_json, "leak.html", _plan_html(None),
        extra_args=("-W", "error"),
    )
    assert result.ret != 0
    result.stdout.fnmatch_lines(["*HERMETIC BOUNDARY VIOLATION*"])
