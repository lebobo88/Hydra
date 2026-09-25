"""Operator decision 2026-09-24: plan artifacts belong in the workflow's
TARGET repo's docs/plans/, not Hydra's own working tree.

Every plan-artifact read/write used to resolve the repo root from
``dispatcher.project_root`` (the Hydra checkout) regardless of
``state.target_repo_id`` -- live evidence: workflow ``plan-e2e-scratch``
wrote into ``Hydra/docs/plans/`` instead of its own target repo.

This file proves the fix's single shared resolver,
``hydra_core.plan_artifact.plan_artifact_repo_root``, and its four call
sites:
  (a) ``hydra_core.ingest``'s PLAN branch (the first write)
  (b) ``hydra_core.supervisor``'s ``node_plan_judge`` verdict re-render
  (c) ``hydra_core.cli``'s force-dispatch governance note
  (d) ``hydra_core.cli``'s ``--critique-ref repo:artifact:<path>`` read

all agree on the same resolved root, recorded once (``plan_artifact_repo_id``
/ ``plan_artifact_root``) and reused by every subsequent reader.
"""
from __future__ import annotations

import json
import subprocess
from pathlib import Path
from uuid import uuid4

import pytest

from hydra_core.ingest import dispatch_ingested_envelopes
from hydra_core.squad_loader import discover_squads
from hydra_core.state import BudgetLedger, HydraState

HYDRA_ROOT = Path(__file__).resolve().parents[1]


# --------------------------------------------------------------------------- #
# Shared helpers
# --------------------------------------------------------------------------- #

def _git_init(path: Path) -> None:
    subprocess.run(["git", "init", str(path)], capture_output=True, check=True)


def _minimal_plan_dict(workflow_id, *, plan_id=None, revision=1) -> dict:
    return {
        "id": str(plan_id or uuid4()),
        "type": "PLAN",
        "origin_squad": "planning",
        "target_squad": "hydra",
        "workflow_id": str(workflow_id),
        "rigor": "standard",
        "goal_restatement": "ship the thing",
        "summary": "ship the thing",
        "plan_revision": revision,
        "steps": [],
    }


class _ProjectRootDispatcher:
    """Minimal dispatcher stand-in exposing only what the PLAN branch reads."""
    def __init__(self, project_root: Path):
        self.project_root = project_root


@pytest.fixture
def packs():
    return discover_squads(HYDRA_ROOT)


# =========================================================================== #
# hydra_core.plan_artifact.plan_artifact_repo_root — the resolver itself
# =========================================================================== #

class TestResolverProperties:
    def test_no_target_repo_id_falls_back_to_default_root(self, tmp_path):
        from hydra_core.plan_artifact import plan_artifact_repo_root

        root, repo_id = plan_artifact_repo_root({}, tmp_path, purpose="write")
        assert root == tmp_path
        assert repo_id is None

    def test_fleet_target_repo_ids_falls_back_to_default_root(self, tmp_path):
        """Multi-repo / fleet mode has no single answer -- fall back, same as
        no target at all."""
        from hydra_core.plan_artifact import plan_artifact_repo_root

        values = {"target_repo_id": "a", "target_repo_ids": ["a", "b"]}
        root, repo_id = plan_artifact_repo_root(values, tmp_path, purpose="write")
        assert root == tmp_path
        assert repo_id is None

    def test_unresolvable_target_repo_id_falls_back_with_trace_event(
        self, tmp_path, monkeypatch,
    ):
        monkeypatch.delenv("HYDRA_EXTRA_REPOS", raising=False)
        from hydra_core.plan_artifact import plan_artifact_repo_root

        events: list[tuple[str, dict]] = []
        values = {"target_repo_id": "not-a-real-repo-id"}
        root, repo_id = plan_artifact_repo_root(
            values, tmp_path, purpose="write", emit=lambda k, p: events.append((k, p)),
        )
        assert root == tmp_path
        assert repo_id is None
        assert events, "an unresolvable target must emit a trace event naming the reason"
        assert events[0][0] == "plan_artifact_repo_root_fallback"
        assert "not-a-real-repo-id" in events[0][1].get("target_repo_id", "")

    def test_target_repo_id_resolves_via_repo_registry(self, tmp_path, monkeypatch):
        from hydra_core.plan_artifact import plan_artifact_repo_root

        target = tmp_path / "target"
        target.mkdir()
        _git_init(target)
        monkeypatch.setenv("HYDRA_EXTRA_REPOS", json.dumps({"tgt": str(target)}))

        root, repo_id = plan_artifact_repo_root(
            {"target_repo_id": "tgt"}, tmp_path, purpose="write",
        )
        assert root == target.resolve()
        assert repo_id == "tgt"

    def test_recorded_root_wins_over_target_repo_id(self, tmp_path, monkeypatch):
        """A previously-recorded root is authoritative for every reader --
        even if target_repo_id is also (still) present, the recorded value
        is what a write already committed to disk under. Proven for BOTH
        purposes: precedence 1 (a recorded root) applies before ``purpose``
        is ever consulted."""
        from hydra_core.plan_artifact import plan_artifact_repo_root

        target = tmp_path / "target"
        target.mkdir()
        _git_init(target)
        monkeypatch.setenv("HYDRA_EXTRA_REPOS", json.dumps({"tgt": str(target)}))

        recorded = tmp_path / "recorded_elsewhere"
        recorded.mkdir()
        values = {
            "target_repo_id": "tgt",
            "plan_artifact_repo_id": "recorded-id",
            "plan_artifact_root": str(recorded),
        }
        for purpose in ("read", "write"):
            root, repo_id = plan_artifact_repo_root(values, tmp_path, purpose=purpose)
            assert root == recorded
            assert repo_id == "recorded-id"

    def test_legacy_checkpoint_no_target_repo_id_falls_back_on_read(self, tmp_path):
        """A checkpoint predating this fix, from a NON-engineering workflow
        (no target_repo_id at all) -- readers fall back to the Hydra
        project root exactly like before. NOTE: this shape alone does not
        distinguish the fixed resolver from the pre-fix bug (target_repo_id
        was never set, so the buggy single-precedence resolver and the
        fixed purpose-aware one return the identical answer here) -- see
        `test_legacy_checkpoint_with_target_repo_id_read_stays_at_default_root`
        below for the shape that actually exercises the fix."""
        from hydra_core.plan_artifact import plan_artifact_repo_root

        values = {"plan_artifact_location": "repo:artifact:docs/plans/x.html"}
        root, repo_id = plan_artifact_repo_root(values, tmp_path, purpose="read")
        assert root == tmp_path
        assert repo_id is None

    def test_legacy_checkpoint_with_target_repo_id_read_stays_at_default_root(
        self, tmp_path, monkeypatch,
    ):
        """THE bug shape the cross-vendor judge FAILed on: a legacy
        checkpoint (``plan_artifact_location`` already set, i.e. an artifact
        exists) with NO ``plan_artifact_root`` ever recorded AND
        ``target_repo_id`` set (the five existing warerender-gta
        workflows). A READ must resolve to the Hydra default root -- where
        the artifact actually sits -- never re-derive a target-repo root
        from ``target_repo_id``, which would silently miss the real file."""
        from hydra_core.plan_artifact import plan_artifact_repo_root

        target = tmp_path / "target"
        target.mkdir()
        _git_init(target)
        monkeypatch.setenv("HYDRA_EXTRA_REPOS", json.dumps({"targetrepo": str(target)}))

        values = {
            "plan_artifact_location": "repo:artifact:docs/plans/x.html",
            "target_repo_id": "targetrepo",
        }
        root, repo_id = plan_artifact_repo_root(values, tmp_path, purpose="read")
        assert root == tmp_path
        assert repo_id is None

    def test_legacy_checkpoint_with_target_repo_id_write_stays_at_default_root(
        self, tmp_path, monkeypatch,
    ):
        """Same legacy shape, but for a revision WRITE (a `modify-plan` PLAN
        re-ingest on a legacy workflow that already has an artifact under
        the Hydra root but never recorded a root). The revision must land
        in the SAME place as the existing artifact, not re-derive a
        target-repo root and split the plan's history across two repos."""
        from hydra_core.plan_artifact import plan_artifact_repo_root

        target = tmp_path / "target"
        target.mkdir()
        _git_init(target)
        monkeypatch.setenv("HYDRA_EXTRA_REPOS", json.dumps({"targetrepo": str(target)}))

        values = {
            "plan_artifact_location": "repo:artifact:docs/plans/x.html",
            "target_repo_id": "targetrepo",
        }
        root, repo_id = plan_artifact_repo_root(values, tmp_path, purpose="write")
        assert root == tmp_path
        assert repo_id is None

    def test_true_first_write_with_target_repo_id_derives_target(self, tmp_path, monkeypatch):
        """Counterpart to the legacy-write test above: when there is NO
        existing ``plan_artifact_location`` at all (the true first write),
        a write DOES derive from ``target_repo_id`` -- this is the only
        branch allowed to do so."""
        from hydra_core.plan_artifact import plan_artifact_repo_root

        target = tmp_path / "target"
        target.mkdir()
        _git_init(target)
        monkeypatch.setenv("HYDRA_EXTRA_REPOS", json.dumps({"targetrepo": str(target)}))

        values = {"target_repo_id": "targetrepo"}
        root, repo_id = plan_artifact_repo_root(values, tmp_path, purpose="write")
        assert root == target.resolve()
        assert repo_id == "targetrepo"

    def test_read_with_no_location_and_no_recorded_root_falls_back(self, tmp_path, monkeypatch):
        """Documented behaviour for a read with NO plan_artifact_location and
        no recorded root at all (nothing has ever been written): always the
        default root, regardless of target_repo_id -- there is nothing to
        re-derive a repo for."""
        from hydra_core.plan_artifact import plan_artifact_repo_root

        target = tmp_path / "target"
        target.mkdir()
        _git_init(target)
        monkeypatch.setenv("HYDRA_EXTRA_REPOS", json.dumps({"targetrepo": str(target)}))

        root, repo_id = plan_artifact_repo_root(
            {"target_repo_id": "targetrepo"}, tmp_path, purpose="read",
        )
        assert root == tmp_path
        assert repo_id is None

    def test_stale_recorded_root_falls_back_with_trace_event(self, tmp_path):
        """A recorded root that no longer exists on disk must fall back
        (fail-soft), not raise -- and must say why."""
        from hydra_core.plan_artifact import plan_artifact_repo_root

        events: list[tuple[str, dict]] = []
        values = {"plan_artifact_root": str(tmp_path / "does-not-exist")}
        root, repo_id = plan_artifact_repo_root(
            values, tmp_path, purpose="read", emit=lambda k, p: events.append((k, p)),
        )
        assert root == tmp_path
        assert repo_id is None
        assert events and events[0][0] == "plan_artifact_repo_root_fallback"

    def test_accepts_a_live_hydrastate_object_not_only_a_dict(self, tmp_path, monkeypatch):
        """cli.py callers pass a plain `values` dict; ingest/supervisor
        callers pass a live `HydraState`. Both shapes must work."""
        from hydra_core.plan_artifact import plan_artifact_repo_root

        target = tmp_path / "target"
        target.mkdir()
        _git_init(target)
        monkeypatch.setenv("HYDRA_EXTRA_REPOS", json.dumps({"tgt": str(target)}))

        state = HydraState(root_goal="x", target_repo_id="tgt")
        root, repo_id = plan_artifact_repo_root(state, tmp_path, purpose="write")
        assert root == target.resolve()
        assert repo_id == "tgt"


# =========================================================================== #
# (a) hydra_core.ingest — the PLAN branch's write
# =========================================================================== #

class TestIngestWritesToTargetRepo:
    def test_plan_with_target_repo_id_lands_in_target_not_hydra_root(
        self, packs, monkeypatch, tmp_path,
    ):
        monkeypatch.setenv("HYDRA_PLAN_PHASE", "1")
        hydra_root = tmp_path / "hydra_root"
        hydra_root.mkdir()
        target_repo = tmp_path / "target_repo"
        target_repo.mkdir()
        _git_init(target_repo)
        monkeypatch.setenv("HYDRA_EXTRA_REPOS", json.dumps({"targetrepo": str(target_repo)}))

        def _boom(*_a, **_k):
            raise AssertionError("execute_squad must not run for a PLAN")
        monkeypatch.setattr("hydra_core.ingest.execute_squad", _boom)

        state = HydraState(root_goal="x", target_repo_id="targetrepo")
        plan = _minimal_plan_dict(state.workflow_id)
        outcome = dispatch_ingested_envelopes(
            state, [plan], packs=packs, dispatcher=_ProjectRootDispatcher(hydra_root),
        )

        assert [it.status for it in outcome.items] == ["drafted"]
        assert outcome.plan_patch["plan_artifact_repo_id"] == "targetrepo"
        assert Path(outcome.plan_patch["plan_artifact_root"]) == target_repo.resolve()

        written = list((target_repo / "docs" / "plans").glob("*.html"))
        assert len(written) == 1, "the artifact must land in the TARGET repo"
        assert not (hydra_root / "docs" / "plans").exists(), (
            "the artifact must NOT be written into the Hydra project root "
            "when a target_repo_id is set"
        )

    def test_plan_without_target_repo_id_still_lands_in_hydra_root(
        self, packs, monkeypatch, tmp_path,
    ):
        """Counterpart / regression guard: unchanged behaviour when no
        target_repo_id is set."""
        monkeypatch.setenv("HYDRA_PLAN_PHASE", "1")

        def _boom(*_a, **_k):
            raise AssertionError("execute_squad must not run for a PLAN")
        monkeypatch.setattr("hydra_core.ingest.execute_squad", _boom)

        state = HydraState(root_goal="x")
        plan = _minimal_plan_dict(state.workflow_id)
        outcome = dispatch_ingested_envelopes(
            state, [plan], packs=packs, dispatcher=_ProjectRootDispatcher(tmp_path),
        )
        assert [it.status for it in outcome.items] == ["drafted"]
        assert outcome.plan_patch["plan_artifact_repo_id"] is None
        assert Path(outcome.plan_patch["plan_artifact_root"]) == tmp_path.resolve()
        written = list((tmp_path / "docs" / "plans").glob("*.html"))
        assert len(written) == 1

    def test_plan_with_unresolvable_target_repo_id_falls_back_to_hydra_root(
        self, packs, monkeypatch, tmp_path,
    ):
        monkeypatch.setenv("HYDRA_PLAN_PHASE", "1")
        monkeypatch.delenv("HYDRA_EXTRA_REPOS", raising=False)

        def _boom(*_a, **_k):
            raise AssertionError("execute_squad must not run for a PLAN")
        monkeypatch.setattr("hydra_core.ingest.execute_squad", _boom)

        events: list[tuple] = []
        state = HydraState(root_goal="x", target_repo_id="no-such-repo")
        plan = _minimal_plan_dict(state.workflow_id)
        outcome = dispatch_ingested_envelopes(
            state, [plan], packs=packs, dispatcher=_ProjectRootDispatcher(tmp_path),
            emit_fn=lambda k, p: events.append((k, p)),
        )
        assert [it.status for it in outcome.items] == ["drafted"], (
            "an unresolvable target must never lose the plan -- fall back, "
            "never fail the whole write"
        )
        assert outcome.plan_patch["plan_artifact_repo_id"] is None
        assert Path(outcome.plan_patch["plan_artifact_root"]) == tmp_path.resolve()
        assert any(k == "plan_artifact_repo_root_fallback" for k, _ in events)
        written = list((tmp_path / "docs" / "plans").glob("*.html"))
        assert len(written) == 1

    def test_revision_on_legacy_workflow_with_target_repo_id_stays_in_hydra_root(
        self, packs, monkeypatch, tmp_path,
    ):
        """A `modify-plan` re-ingest (revision 2) on a LEGACY workflow: the
        state already carries `plan_artifact_location` (an artifact exists
        under the Hydra root, from before root-recording shipped) and
        `target_repo_id` is set, but no `plan_artifact_root`/
        `plan_artifact_repo_id` was ever recorded. The revision write must
        land in the SAME Hydra root as the existing artifact, never
        re-derive the target repo and split the plan's history across two
        repos."""
        monkeypatch.setenv("HYDRA_PLAN_PHASE", "1")
        hydra_root = tmp_path / "hydra_root"
        hydra_root.mkdir()
        target_repo = tmp_path / "target_repo"
        target_repo.mkdir()
        _git_init(target_repo)
        monkeypatch.setenv("HYDRA_EXTRA_REPOS", json.dumps({"targetrepo": str(target_repo)}))

        def _boom(*_a, **_k):
            raise AssertionError("execute_squad must not run for a PLAN")
        monkeypatch.setattr("hydra_core.ingest.execute_squad", _boom)

        state = HydraState(root_goal="x", target_repo_id="targetrepo")
        # Simulate a legacy checkpoint: an artifact already exists under the
        # Hydra root, recorded before `plan_artifact_root` existed, and
        # revision 1 has already been authored (the ingest PLAN branch only
        # accepts the next sequential revision).
        state.plan_artifact_location = "repo:artifact:docs/plans/pre-existing.html"
        # `state.plan_revision` names the revision currently being authored
        # (see `hydra_core.ingest`'s `_expected_revision`), so it must equal
        # the incoming PLAN's `plan_revision`, not one less.
        state.plan_revision = 2

        plan = _minimal_plan_dict(state.workflow_id, revision=2)
        outcome = dispatch_ingested_envelopes(
            state, [plan], packs=packs, dispatcher=_ProjectRootDispatcher(hydra_root),
        )

        assert [it.status for it in outcome.items] == ["drafted"]
        assert outcome.plan_patch["plan_artifact_repo_id"] is None
        assert Path(outcome.plan_patch["plan_artifact_root"]) == hydra_root.resolve()
        assert not (target_repo / "docs" / "plans").exists(), (
            "a legacy revision must never split into the target repo"
        )

    def test_revision_on_new_style_workflow_stays_in_recorded_target_root(
        self, packs, monkeypatch, tmp_path,
    ):
        """A `modify-plan` re-ingest on a workflow that already recorded a
        root (post-fix) keeps using that SAME recorded root, not re-deriving
        it -- revisions never move repos."""
        monkeypatch.setenv("HYDRA_PLAN_PHASE", "1")
        hydra_root = tmp_path / "hydra_root"
        hydra_root.mkdir()
        target_repo = tmp_path / "target_repo"
        target_repo.mkdir()
        _git_init(target_repo)
        monkeypatch.setenv("HYDRA_EXTRA_REPOS", json.dumps({"targetrepo": str(target_repo)}))

        def _boom(*_a, **_k):
            raise AssertionError("execute_squad must not run for a PLAN")
        monkeypatch.setattr("hydra_core.ingest.execute_squad", _boom)

        state = HydraState(root_goal="x", target_repo_id="targetrepo")
        state.plan_artifact_location = "repo:artifact:docs/plans/rev1.html"
        state.plan_artifact_repo_id = "targetrepo"
        state.plan_artifact_root = str(target_repo.resolve())
        state.plan_revision = 2

        plan = _minimal_plan_dict(state.workflow_id, revision=2)
        outcome = dispatch_ingested_envelopes(
            state, [plan], packs=packs, dispatcher=_ProjectRootDispatcher(hydra_root),
        )

        assert [it.status for it in outcome.items] == ["drafted"]
        assert outcome.plan_patch["plan_artifact_repo_id"] == "targetrepo"
        assert Path(outcome.plan_patch["plan_artifact_root"]) == target_repo.resolve()
        assert not (hydra_root / "docs" / "plans").exists()


# =========================================================================== #
# (b) hydra_core.supervisor — node_plan_judge's verdict re-render
# =========================================================================== #

class _StubDispatcher:
    allow_offline_mcp_dispatch = True

    def __init__(self, project_root: Path | None = None):
        self.project_root = project_root


def _node(sup, name):
    from hydra_core.supervisor import _PurePythonRunner
    assert isinstance(sup, _PurePythonRunner)
    for step_name, fn in sup.steps:
        if step_name == name:
            return fn
    raise AssertionError(f"no such node: {name}")


def _basic_plan_ref(state: HydraState) -> dict:
    return {
        "id": str(uuid4()),
        "type": "PLAN",
        "workflow_id": str(state.workflow_id),
        "origin_squad": "planning",
        "target_squad": "hydra",
        "rigor": "standard",
        "goal_restatement": "ship the widget",
        "summary": "ship the widget",
        "steps": [
            {"step_id": "s1", "description": "build the widget",
             "target_squad": "engineering", "envelope_type": "DEV_TASK",
             "priority": "P2"},
        ],
        "plan_revision": 1,
    }


class TestNodePlanJudgeRerendersInTargetRepo:
    def test_rerender_updates_target_repo_file_not_hydra_root(self, tmp_path, monkeypatch):
        from hydra_core.artifact_store import write_repo_artifact, resolve_repo_artifact_path
        from hydra_core.plan_artifact import plan_slug, render_plan_html
        from hydra_core.schemas import Plan
        from hydra_core.supervisor import build_supervisor

        hydra_root = tmp_path / "hydra_root"
        hydra_root.mkdir()
        target_repo = tmp_path / "target_repo"
        target_repo.mkdir()
        _git_init(target_repo)
        monkeypatch.setenv("HYDRA_EXTRA_REPOS", json.dumps({"targetrepo": str(target_repo)}))

        sup = build_supervisor(
            project_root=HYDRA_ROOT,
            dispatcher=_StubDispatcher(project_root=hydra_root),
            critique_client=None, force_pure_python=True,
        )
        plan_judge = _node(sup, "plan_judge")

        state = HydraState(
            root_goal="ship the widget", budget=BudgetLedger(budget_usd=100.0),
            target_repo_id="targetrepo",
        )
        state.plan_status = "drafted"
        state.plan_revision = 1
        plan_ref = _basic_plan_ref(state)
        state.plan_ref = plan_ref

        # Mirror the ingest PLAN branch's write: into the TARGET repo, with
        # the repo identity recorded exactly like the real write does.
        plan_model = Plan.model_validate(plan_ref)
        slug = plan_slug(plan_model.goal_restatement, plan_model.workflow_id)
        html_text = render_plan_html(plan_model)
        ref = write_repo_artifact(target_repo, f"docs/plans/{slug}.html", html_text)
        state.plan_artifact_location = ref.model_dump(mode="json")["key"]
        state.plan_artifact_repo_id = "targetrepo"
        state.plan_artifact_root = str(target_repo.resolve())

        relpath = state.plan_artifact_location[len("repo:artifact:"):]
        before_html = resolve_repo_artifact_path(target_repo, relpath).read_text(
            encoding="utf-8",
        )
        assert "No verdict recorded yet." in before_html

        plan_judge(state)

        after_html = resolve_repo_artifact_path(target_repo, relpath).read_text(
            encoding="utf-8",
        )
        assert "No verdict recorded yet." not in after_html
        assert "outcome=revise" in after_html
        assert not (hydra_root / "docs" / "plans").exists(), (
            "the re-render must never touch the Hydra project root when the "
            "artifact was recorded against a target repo"
        )

    def test_rerender_legacy_checkpoint_updates_hydra_root_not_target_repo(
        self, tmp_path, monkeypatch,
    ):
        """The bug shape: a legacy checkpoint whose artifact was written
        under the Hydra root BEFORE root-recording shipped -- location is
        set, target_repo_id IS set, but plan_artifact_root/repo_id were
        never recorded. The re-render must update the SAME Hydra-root file
        ingest actually wrote, never create a stray file in the target
        repo."""
        from hydra_core.artifact_store import write_repo_artifact, resolve_repo_artifact_path
        from hydra_core.plan_artifact import plan_slug, render_plan_html
        from hydra_core.schemas import Plan
        from hydra_core.supervisor import build_supervisor

        hydra_root = tmp_path / "hydra_root"
        hydra_root.mkdir()
        target_repo = tmp_path / "target_repo"
        target_repo.mkdir()
        _git_init(target_repo)
        monkeypatch.setenv("HYDRA_EXTRA_REPOS", json.dumps({"targetrepo": str(target_repo)}))

        sup = build_supervisor(
            project_root=HYDRA_ROOT,
            dispatcher=_StubDispatcher(project_root=hydra_root),
            critique_client=None, force_pure_python=True,
        )
        plan_judge = _node(sup, "plan_judge")

        state = HydraState(
            root_goal="ship the widget", budget=BudgetLedger(budget_usd=100.0),
            target_repo_id="targetrepo",
        )
        state.plan_status = "drafted"
        state.plan_revision = 1
        plan_ref = _basic_plan_ref(state)
        state.plan_ref = plan_ref

        # Legacy write: lands under the Hydra root (as it always did before
        # root-recording), and NEITHER plan_artifact_repo_id NOR
        # plan_artifact_root is ever set on state -- exactly the shape of
        # the five pre-existing warerender-gta checkpoints.
        plan_model = Plan.model_validate(plan_ref)
        slug = plan_slug(plan_model.goal_restatement, plan_model.workflow_id)
        html_text = render_plan_html(plan_model)
        ref = write_repo_artifact(hydra_root, f"docs/plans/{slug}.html", html_text)
        state.plan_artifact_location = ref.model_dump(mode="json")["key"]

        relpath = state.plan_artifact_location[len("repo:artifact:"):]
        before_html = resolve_repo_artifact_path(hydra_root, relpath).read_text(
            encoding="utf-8",
        )
        assert "No verdict recorded yet." in before_html

        plan_judge(state)

        after_html = resolve_repo_artifact_path(hydra_root, relpath).read_text(
            encoding="utf-8",
        )
        assert "No verdict recorded yet." not in after_html
        assert "outcome=revise" in after_html
        assert not (target_repo / "docs" / "plans").exists(), (
            "a legacy re-render must never create a stray file in the "
            "target repo"
        )


# =========================================================================== #
# (c) hydra_core.cli._append_plan_governance_note — force-dispatch
# =========================================================================== #

class TestGovernanceNoteTargetsRecordedRoot:
    def test_note_appends_to_target_repo_file(self, tmp_path, monkeypatch):
        from hydra_core.artifact_store import write_repo_artifact
        from hydra_core.cli import _append_plan_governance_note

        hydra_root = tmp_path / "hydra_root"
        hydra_root.mkdir()
        target_repo = tmp_path / "target_repo"
        target_repo.mkdir()
        _git_init(target_repo)

        ref = write_repo_artifact(target_repo, "docs/plans/x.html", "<h1>Plan</h1>\n")
        relpath = ref.model_dump(mode="json")["key"][len("repo:artifact:"):]

        values = {
            "plan_artifact_location": f"repo:artifact:{relpath}",
            "plan_artifact_repo_id": "targetrepo",
            "plan_artifact_root": str(target_repo.resolve()),
        }
        emitted: list[tuple] = []
        monkeypatch.setattr(
            "hydra_core.cli.emit", lambda *a, **k: emitted.append(a),
        )

        _append_plan_governance_note(
            hydra_root, "wf-1", values, "operator force-dispatched",
        )

        written = (target_repo / relpath).read_text(encoding="utf-8")
        assert "operator force-dispatched" in written
        assert not (hydra_root / "docs" / "plans").exists(), (
            "the note must land in the target repo, never the Hydra root"
        )
        assert not [a for a in emitted if len(a) >= 3 and a[2] == "plan_governance_note_failed"]

    def test_note_falls_back_to_hydra_root_without_a_recorded_root(self, tmp_path, monkeypatch):
        """Counterpart / regression guard: no recorded root behaves exactly
        like before this fix (writes against `project`)."""
        from hydra_core.artifact_store import write_repo_artifact
        from hydra_core.cli import _append_plan_governance_note

        write_repo_artifact(tmp_path, "docs/plans/x.html", "<h1>Plan</h1>\n")
        values = {"plan_artifact_location": "repo:artifact:docs/plans/x.html"}
        monkeypatch.setattr("hydra_core.cli.emit", lambda *a, **k: None)

        _append_plan_governance_note(tmp_path, "wf-1", values, "note text")

        written = (tmp_path / "docs" / "plans" / "x.html").read_text(encoding="utf-8")
        assert "note text" in written

    def test_note_legacy_checkpoint_with_target_repo_id_stays_in_hydra_root(
        self, tmp_path, monkeypatch,
    ):
        """The bug shape: legacy checkpoint (location set, no recorded
        root) that ALSO has target_repo_id set -- the note must append to
        the Hydra-root file that actually exists, never create a fresh file
        in the target repo."""
        from hydra_core.artifact_store import write_repo_artifact
        from hydra_core.cli import _append_plan_governance_note

        hydra_root = tmp_path / "hydra_root"
        hydra_root.mkdir()
        target_repo = tmp_path / "target_repo"
        target_repo.mkdir()
        _git_init(target_repo)
        monkeypatch.setenv("HYDRA_EXTRA_REPOS", json.dumps({"targetrepo": str(target_repo)}))

        write_repo_artifact(hydra_root, "docs/plans/x.html", "<h1>Plan</h1>\n")
        values = {
            "plan_artifact_location": "repo:artifact:docs/plans/x.html",
            "target_repo_id": "targetrepo",
        }
        monkeypatch.setattr("hydra_core.cli.emit", lambda *a, **k: None)

        _append_plan_governance_note(hydra_root, "wf-1", values, "note text")

        written = (hydra_root / "docs" / "plans" / "x.html").read_text(encoding="utf-8")
        assert "note text" in written
        assert not (target_repo / "docs" / "plans").exists(), (
            "the note must never create a fresh file in the target repo "
            "for a legacy checkpoint"
        )


# =========================================================================== #
# (d) hydra_core.cli._read_plan_critique — --critique-ref
# =========================================================================== #

class TestCritiqueRefResolvesAgainstRecordedRoot:
    def test_repo_artifact_ref_resolves_against_recorded_root(self, tmp_path):
        from hydra_core.artifact_store import write_repo_artifact
        from hydra_core.cli import _read_plan_critique

        hydra_root = tmp_path / "hydra_root"
        hydra_root.mkdir()
        target_repo = tmp_path / "target_repo"
        target_repo.mkdir()
        _git_init(target_repo)

        critique_text = "Needs a rollback step; the auth flow isn't covered."
        write_repo_artifact(
            target_repo, "docs/plans/critique.txt", critique_text,
            allowed_suffixes=frozenset({".txt"}),
        )

        values = {
            "plan_artifact_repo_id": "targetrepo",
            "plan_artifact_root": str(target_repo.resolve()),
        }
        result = _read_plan_critique(
            "repo:artifact:docs/plans/critique.txt", hydra_root, values,
        )
        assert result == critique_text

    def test_repo_artifact_ref_without_recorded_root_uses_hydra_root(self, tmp_path):
        """Counterpart / regression guard: unchanged behaviour for a legacy
        checkpoint (no plan_artifact_root recorded) or a bare `project`-only
        call (`values=None`)."""
        from hydra_core.artifact_store import write_repo_artifact
        from hydra_core.cli import _read_plan_critique

        critique_text = "plain legacy critique text"
        write_repo_artifact(
            tmp_path, "docs/plans/critique.txt", critique_text,
            allowed_suffixes=frozenset({".txt"}),
        )
        assert _read_plan_critique(
            "repo:artifact:docs/plans/critique.txt", tmp_path,
        ) == critique_text

    def test_repo_artifact_ref_legacy_checkpoint_with_target_repo_id_uses_hydra_root(
        self, tmp_path, monkeypatch,
    ):
        """The bug shape: legacy checkpoint (no recorded root) with
        target_repo_id set -- --critique-ref must resolve against the
        Hydra root where the file actually is, not the target repo."""
        from hydra_core.artifact_store import write_repo_artifact
        from hydra_core.cli import _read_plan_critique

        hydra_root = tmp_path / "hydra_root"
        hydra_root.mkdir()
        target_repo = tmp_path / "target_repo"
        target_repo.mkdir()
        _git_init(target_repo)
        monkeypatch.setenv("HYDRA_EXTRA_REPOS", json.dumps({"targetrepo": str(target_repo)}))

        critique_text = "legacy critique text, no recorded root"
        write_repo_artifact(
            hydra_root, "docs/plans/critique.txt", critique_text,
            allowed_suffixes=frozenset({".txt"}),
        )
        values = {
            "plan_artifact_location": "repo:artifact:docs/plans/critique.txt",
            "target_repo_id": "targetrepo",
        }
        result = _read_plan_critique(
            "repo:artifact:docs/plans/critique.txt", hydra_root, values,
        )
        assert result == critique_text


# =========================================================================== #
# state.py — the new fields
# =========================================================================== #

def test_hydrastate_carries_plan_artifact_repo_fields():
    state = HydraState(root_goal="x")
    assert state.plan_artifact_repo_id is None
    assert state.plan_artifact_root is None
