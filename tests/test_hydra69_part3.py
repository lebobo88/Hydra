"""Hydra#69 part 3 (defect D): plan-author contract + plan-verdict visibility.

D1. `_build_squad_prompt` (hydra_core.host_bridge) appends a schema-generated
    "## Required output: PLAN envelope" section for squad_slug=="planning"
    ONLY -- every other squad's prompt stays byte-for-byte unchanged.
D2. plugins/hydra/agents/plan-author.md names the key PlanStep fields and
    points at the schema-generated section (markdown-only, smoke-checked
    here just for the load-bearing strings).
D3. `node_plan_judge` (hydra_core.supervisor) adds `verdict_critique`
    (truncated) and `verdict_plan_revision` to `plan_detail`.
D4. The plan HTML artifact is re-rendered with the judge verdict after
    `node_plan_judge` runs, through the SAME `render_plan_html` +
    `write_repo_artifact` path ingest uses, preserving any pre-existing
    governance note; a re-render failure is fail-soft.
"""
from __future__ import annotations

from pathlib import Path
from uuid import uuid4

import pytest

from hydra_core import host_bridge
from hydra_core import schemas
from hydra_core.state import BudgetLedger, HydraState, TaskState


HYDRA_ROOT = Path(__file__).resolve().parents[1]


# --------------------------------------------------------------------------- #
# D1: schema-generated PLAN envelope contract section                        #
# --------------------------------------------------------------------------- #

def test_planning_prompt_lists_every_plan_and_planstep_field_from_schema():
    """The prompt's field list must be derived from the live pydantic models,
    not a hand-copied literal -- assert against the schema itself."""
    prompt = host_bridge._build_squad_prompt(
        workflow_id="wf-1", task_id="task-1", squad_slug="planning",
        request_text="Author a standard-rigor plan for: ship the widget",
    )
    assert "## Required output: PLAN envelope" in prompt
    for name in schemas.Plan.model_fields:
        assert f"`{name}`" in prompt, f"Plan field {name!r} missing from prompt"
    for name in schemas.PlanStep.model_fields:
        assert f"`{name}`" in prompt, f"PlanStep field {name!r} missing from prompt"
    for envelope_type in schemas.SCHEMA_REGISTRY:
        assert envelope_type in prompt, (
            f"allowed PlanStep.envelope_type value {envelope_type!r} missing"
        )
    assert "emitted_envelopes" in prompt


def test_planning_prompt_field_list_tracks_schema_additions(monkeypatch):
    """Mutation proof: if the doc-gen helper stopped reading the live schema
    (e.g. reverted to a hand-copied literal list), a field added to the model
    at runtime would NOT show up in the rendered doc. Prove the doc-gen walks
    `model_fields` by subclassing `PlanStep` with a throwaway field, pointing
    the schemas module at the subclass, and confirming the field appears."""
    import hydra_core.schemas as _schemas
    from typing import Optional

    class _PlanStepWithExtra(_schemas.PlanStep):
        totally_new_marker_field: Optional[str] = None

    monkeypatch.setattr(_schemas, "PlanStep", _PlanStepWithExtra)
    doc = host_bridge._plan_envelope_schema_doc()
    assert "`totally_new_marker_field`" in doc


def test_non_planning_squad_prompt_byte_for_byte_unchanged():
    """A non-planning squad's prompt must not change AT ALL when the new
    plan_revision/plan_critique/supersedes_plan_envelope_id kwargs are
    supplied -- they are only ever read inside the planning branch."""
    base = host_bridge._build_squad_prompt(
        workflow_id="wf-2", task_id="task-2", squad_slug="garland",
        request_text="draft the brand brief", goal="ship a campaign",
    )
    with_plan_kwargs = host_bridge._build_squad_prompt(
        workflow_id="wf-2", task_id="task-2", squad_slug="garland",
        request_text="draft the brand brief", goal="ship a campaign",
        plan_revision=3, plan_critique="irrelevant to garland",
        supersedes_plan_envelope_id=str(uuid4()),
    )
    assert base == with_plan_kwargs
    assert "Required output: PLAN envelope" not in base


def test_planning_revision_gt1_prompt_includes_supersedes_and_critique():
    prior_id = str(uuid4())
    prompt = host_bridge._build_squad_prompt(
        workflow_id="wf-3", task_id="task-3", squad_slug="planning",
        request_text="Revise the plan",
        plan_revision=2, plan_critique="the auth step is underspecified",
        supersedes_plan_envelope_id=prior_id,
    )
    assert "expected plan_revision: 2" in prompt
    assert f"supersedes: {prior_id}" in prompt
    assert "the auth step is underspecified" in prompt


def test_planning_revision_1_prompt_omits_supersedes_section():
    prompt = host_bridge._build_squad_prompt(
        workflow_id="wf-4", task_id="task-4", squad_slug="planning",
        request_text="Author the first draft",
        plan_revision=1,
    )
    assert "expected plan_revision: 1" in prompt
    assert "supersedes:" not in prompt


class _FakeSupForStep:
    """Mirrors `test_p5c_plan_operator_surfaces.py`'s `_AttendedFakeSup`: a
    minimal `sup` stand-in whose `get_state` returns a fixed checkpoint
    snapshot with `next=()` (so `_run_first_step_dispatch_pass` is a no-op)."""

    def __init__(self, values: dict):
        self.values = dict(values)

    def get_state(self, config):
        outer = self

        class _Snap:
            values = outer.values
            next = ()

        return _Snap()

    def update_state(self, config, patch, as_node=None):
        self.values.update(patch)

    def invoke(self, *a, **k):
        raise AssertionError("must not invoke the graph in this test")


class _NopStepDispatcher:
    live_execution = True

    def call_mcp(self, server, tool, args, **_kw):
        return {"status": "done", "result": {}}

    def set_squad_packs(self, packs):
        pass


def test_cli_attended_step_threads_plan_revision_critique_supersedes(
    tmp_path, monkeypatch,
):
    """D1: the ACTUAL `hydra_core.cli` attended-step call site (not just
    `begin_squad_stage` called directly) must thread the planning task's own
    `plan_revision`/`plan_critique`/`supersedes_plan_envelope_id` into the
    host_action prompt. Uses the real `squads/planning/squad.yaml` pack
    (entrypoint `claude-native`) via real `discover_squads` against the repo
    root, with the graph itself faked out."""
    from hydra_core.cli import _cmd_attended_step
    import argparse

    task = TaskState(
        owner_squad="planning",
        description="Revise the plan",
        plan_revision=2,
        plan_critique="tighten the acceptance criteria",
        supersedes_plan_envelope_id="prior-envelope-id",
    )
    state = HydraState(root_goal="ship the widget", tasks=[task], plan_revision=2)
    values = state.model_dump(mode="json")

    sup = _FakeSupForStep(values)
    monkeypatch.setattr("hydra_core.cli._attended_live_dispatcher",
                        lambda *a, **k: _NopStepDispatcher())
    monkeypatch.setattr("hydra_core.supervisor.build_supervisor",
                        lambda **k: sup)

    rc = _cmd_attended_step(argparse.Namespace(
        project=str(HYDRA_ROOT), workflow_id=str(state.workflow_id), verbose=False))
    assert rc == 0

    import json as _json
    cfile = HYDRA_ROOT / ".hydra" / str(state.workflow_id) / "attended" / \
        f"{task.task_id}.json"
    assert cfile.is_file(), f"expected cursor at {cfile}"
    try:
        cursor = _json.loads(cfile.read_text(encoding="utf-8"))
        prompt = cursor["pending_action"]["prompt"]
        assert "expected plan_revision: 2" in prompt
        assert "supersedes: prior-envelope-id" in prompt
        assert "tighten the acceptance criteria" in prompt
    finally:
        import shutil
        shutil.rmtree(HYDRA_ROOT / ".hydra" / str(state.workflow_id), ignore_errors=True)


def test_begin_squad_stage_threads_plan_revision_into_prompt(tmp_path):
    res = host_bridge.begin_squad_stage(
        workflow_id="wf-5", task_id="task-5", squad_slug="planning",
        entrypoint="claude-skill", lead_agent="plan-author",
        pack_cwd=str(tmp_path), request_text="Author a plan",
        project_root=str(tmp_path),
        plan_revision=2, plan_critique="tighten the acceptance criteria",
        supersedes_plan_envelope_id="prior-envelope-id",
    )
    prompt = res["host_action"]["prompt"]
    assert "expected plan_revision: 2" in prompt
    assert "supersedes: prior-envelope-id" in prompt
    assert "tighten the acceptance criteria" in prompt


# --------------------------------------------------------------------------- #
# D2: plan-author.md return-format section                                   #
# --------------------------------------------------------------------------- #

def test_plan_author_md_names_key_planstep_fields_and_points_at_prompt():
    md = (HYDRA_ROOT / "plugins" / "hydra" / "agents" / "plan-author.md").read_text(
        encoding="utf-8"
    )
    assert "Required output: PLAN envelope" in md
    assert "emitted_envelopes" in md
    for field in ("step_id", "target_squad", "envelope_type", "description",
                  "acceptance_criteria", "depends_on"):
        assert field in md, f"plan-author.md must name PlanStep field {field!r}"


# --------------------------------------------------------------------------- #
# D3/D4: node_plan_judge -- plan_detail fields + artifact re-render          #
# --------------------------------------------------------------------------- #

class _StubDispatcher:
    allow_offline_mcp_dispatch = True

    def __init__(self, project_root: Path | None = None):
        self.project_root = project_root

    def call_mcp(self, server, tool, args, **_kw):
        return {"status": "done", "tool": tool, "result": {"ok": True}}

    def spawn_subprocess(self, cmd, env=None):
        return {"status": "done", "stdout": "", "stderr": ""}

    def emit_claude_prompt(self, prompt, agent=None):
        return {"status": "done", "agent": agent, "summary": "stub"}

    def invoke_claude_skill(self, skill, args):
        return {"status": "done", "skill": skill, "summary": "stub"}


def _node(sup, name):
    from hydra_core.supervisor import _PurePythonRunner
    assert isinstance(sup, _PurePythonRunner)
    for step_name, fn in sup.steps:
        if step_name == name:
            return fn
    raise AssertionError(f"no such node: {name}")


def _build_sup(artifact_root: Path):
    """`project_root` (squad discovery) stays pinned to the real repo root;
    `dispatcher.project_root` (artifact writes) points at the isolated
    `tmp_path` -- `node_plan_judge`'s re-render prefers `dispatcher.
    project_root` over the closure's `project_root` (mirroring the ingest
    PLAN branch), so this is the same split the real attended path uses
    (repo checkout for squad discovery, dispatcher-scoped root for writes)."""
    from hydra_core.supervisor import build_supervisor
    return build_supervisor(
        project_root=HYDRA_ROOT,
        dispatcher=_StubDispatcher(project_root=artifact_root),
        critique_client=None,
        force_pure_python=True,
    )


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
    }


def test_plan_detail_carries_verdict_critique_and_revision(tmp_path):
    sup = _build_sup(tmp_path)
    plan_judge = _node(sup, "plan_judge")

    state = HydraState(root_goal="ship the widget", budget=BudgetLedger(budget_usd=100.0))
    state.plan_status = "drafted"
    state.plan_revision = 1
    state.plan_ref = _basic_plan_ref(state)

    patch = plan_judge(state)
    plan_detail = patch["pending_hitl"]["plan_detail"]

    assert "verdict_critique" in plan_detail
    assert plan_detail["verdict_critique"], "critique text must be non-empty (NoOp judge)"
    assert "[skeleton]" in plan_detail["verdict_critique"]
    assert plan_detail["verdict_plan_revision"] == 1


def test_plan_detail_verdict_critique_is_truncated_at_2000_chars():
    from hydra_core.supervisor import _truncate_plan_critique, _PLAN_CRITIQUE_MAX_CHARS

    long_text = "x" * 5000
    truncated = _truncate_plan_critique(long_text)
    assert truncated is not None
    assert len(truncated) <= _PLAN_CRITIQUE_MAX_CHARS
    assert truncated.endswith("[truncated]")
    assert _truncate_plan_critique(None) is None
    short = "short critique"
    assert _truncate_plan_critique(short) == short


def _write_initial_plan_artifact(tmp_path: Path, plan_env_dict: dict) -> str:
    """Mirror the ingest PLAN branch's write so the test starts from the same
    on-disk shape node_plan_judge's re-render must update."""
    from hydra_core.artifact_store import write_repo_artifact
    from hydra_core.plan_artifact import plan_slug, render_plan_html
    from hydra_core.schemas import Plan

    plan_model = Plan.model_validate(plan_env_dict)
    slug = plan_slug(plan_model.goal_restatement, plan_model.workflow_id)
    html_text = render_plan_html(plan_model)
    ref = write_repo_artifact(tmp_path, f"docs/plans/{slug}.html", html_text)
    return ref.model_dump(mode="json")["key"]


def test_node_plan_judge_rerenders_artifact_with_verdict(tmp_path):
    sup = _build_sup(tmp_path)
    plan_judge = _node(sup, "plan_judge")

    state = HydraState(root_goal="ship the widget", budget=BudgetLedger(budget_usd=100.0))
    state.plan_status = "drafted"
    state.plan_revision = 1
    plan_ref = _basic_plan_ref(state)
    state.plan_ref = plan_ref
    state.plan_artifact_location = _write_initial_plan_artifact(tmp_path, plan_ref)

    from hydra_core.artifact_store import resolve_repo_artifact_path
    relpath = state.plan_artifact_location[len("repo:artifact:"):]
    before_html = resolve_repo_artifact_path(tmp_path, relpath).read_text(encoding="utf-8")
    assert "No verdict recorded yet." in before_html

    plan_judge(state)

    after_html = resolve_repo_artifact_path(tmp_path, relpath).read_text(encoding="utf-8")
    assert "No verdict recorded yet." not in after_html
    assert "outcome=revise" in after_html
    assert "plan_revision=1" in after_html
    assert "[skeleton]" in after_html


def test_node_plan_judge_rerender_preserves_governance_note(tmp_path):
    from hydra_core.plan_artifact import append_governance_note
    from hydra_core.artifact_store import resolve_repo_artifact_path, write_repo_artifact

    sup = _build_sup(tmp_path)
    plan_judge = _node(sup, "plan_judge")

    state = HydraState(root_goal="ship the widget", budget=BudgetLedger(budget_usd=100.0))
    state.plan_status = "drafted"
    state.plan_revision = 1
    plan_ref = _basic_plan_ref(state)
    state.plan_ref = plan_ref
    state.plan_artifact_location = _write_initial_plan_artifact(tmp_path, plan_ref)

    relpath = state.plan_artifact_location[len("repo:artifact:"):]
    existing = resolve_repo_artifact_path(tmp_path, relpath).read_text(encoding="utf-8")
    with_note = append_governance_note(existing, "operator force-dispatched past plan_gate")
    write_repo_artifact(tmp_path, relpath, with_note)

    plan_judge(state)

    after_html = resolve_repo_artifact_path(tmp_path, relpath).read_text(encoding="utf-8")
    assert "operator force-dispatched past plan_gate" in after_html
    assert "Governance Notes" in after_html
    assert "outcome=revise" in after_html


def test_node_plan_judge_rerender_failure_is_fail_soft(tmp_path, monkeypatch):
    """A re-render failure must never break the gate -- only emit a trace
    event."""
    sup = _build_sup(tmp_path)
    plan_judge = _node(sup, "plan_judge")

    state = HydraState(root_goal="ship the widget", budget=BudgetLedger(budget_usd=100.0))
    state.plan_status = "drafted"
    state.plan_revision = 1
    plan_ref = _basic_plan_ref(state)
    state.plan_ref = plan_ref
    state.plan_artifact_location = _write_initial_plan_artifact(tmp_path, plan_ref)

    import hydra_core.plan_artifact as plan_artifact_mod

    def _boom(*a, **kw):
        raise ValueError("simulated render failure")

    monkeypatch.setattr(plan_artifact_mod, "render_plan_html", _boom)

    # The gate must still be produced normally despite the re-render blowing up.
    patch = plan_judge(state)
    assert patch["pending_hitl"]["reason"] == "plan_approval"
    assert patch["pending_hitl"]["plan_detail"]["verdict_outcome"] == "revise"


def test_node_plan_judge_no_artifact_location_skips_rerender_quietly(tmp_path):
    """No `plan_artifact_location` (e.g. legacy checkpoint) -- the gate still
    builds normally, no crash."""
    sup = _build_sup(tmp_path)
    plan_judge = _node(sup, "plan_judge")

    state = HydraState(root_goal="ship the widget", budget=BudgetLedger(budget_usd=100.0))
    state.plan_status = "drafted"
    state.plan_revision = 1
    state.plan_ref = _basic_plan_ref(state)
    state.plan_artifact_location = None

    patch = plan_judge(state)
    assert patch["pending_hitl"]["reason"] == "plan_approval"


# --------------------------------------------------------------------------- #
# D4 helper: extract_governance_section                                      #
# --------------------------------------------------------------------------- #

def test_extract_governance_section_roundtrips_through_append():
    from hydra_core.plan_artifact import append_governance_note, extract_governance_section

    html = "<h1>Plan</h1>\n<p>body</p>\n"
    assert extract_governance_section(html) is None
    with_note = append_governance_note(html, "a note")
    section = extract_governance_section(with_note)
    assert section is not None
    assert "a note" in section
    assert "<h2>Governance Notes</h2>" in section


# --------------------------------------------------------------------------- #
# D5: approve/SKILL.md content assertions (markdown-only, load-bearing text) #
# --------------------------------------------------------------------------- #

def test_approve_skill_md_documents_verdict_critique_and_revision():
    md = (HYDRA_ROOT / "plugins" / "hydra" / "skills" / "approve" / "SKILL.md").read_text(
        encoding="utf-8"
    )
    assert "verdict_critique" in md
    assert "verdict_plan_revision" in md


def test_approve_skill_md_corrects_never_paused_claim():
    md = (HYDRA_ROOT / "plugins" / "hydra" / "skills" / "approve" / "SKILL.md").read_text(
        encoding="utf-8"
    )
    assert "exactly as if the workflow had never\n  paused" not in md
    assert "materialise" in md.lower() or "materialises" in md.lower()
    assert "modify-budget" in md
    assert "re-files" in md or "re-file" in md
