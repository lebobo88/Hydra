"""Regression tests for RA-12a, RA-12b, RA-10, and RA-3.

RA-12a  dispatch skip for attended_done_task_ids: tasks whose task_id is in
        state.attended_done_task_ids must skip node_dispatch without calling
        execute_squad, emit a dispatch.attended_already_complete trace, and
        be marked "done".

RA-12b  pp provenance threading: start_run payload must carry hydra_workflow_id
        (+ envelope enrichment fields) so the pp DB links run rows to the
        workflow (MU16 cost gate).

RA-10   goal-text --squad flag: tail-position --squad <slug[,slug...]> forces
        selection and strips the flag from root_goal; mid-prose + non-slug next
        word is ignored + traced; unknown slug at tail errors; the mid-prose+tail
        repo-flag combo no longer aborts (parse_repo_arg multiplicity fix).

RA-3    CONTRIBUTING-SQUADS.md has a "Tool declaration naming" subsection and
        customer-support/squad.yaml has a comment near the tools block.

No network / no LLMs / no subprocesses.
"""
from __future__ import annotations

from pathlib import Path
from uuid import uuid4

import pytest

HYDRA_ROOT = Path(__file__).resolve().parents[1]


# ---------------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------------


class _StubDispatcher:
    """Minimal dispatcher protocol stand-in. Records MCP calls for assertions."""
    # E2-22: this suite exercises the in-graph mcp dispatch path with a
    # scripted pp harness. Opt in explicitly — node_dispatch otherwise
    # defers mcp packs to the attended host on a non-live dispatcher.
    allow_offline_mcp_dispatch = True

    def __init__(self):
        self.calls: list[tuple[str, str, dict]] = []

    def call_mcp(self, server: str, tool: str, args: dict, **_kw) -> dict:
        self.calls.append((server, tool, dict(args)))
        return {"status": "done", "tool": tool, "result": {"run_id": "run-test-123", "ok": True}}

    def spawn_subprocess(self, cmd, env=None) -> dict:
        return {"status": "done", "stdout": "", "stderr": ""}

    def emit_claude_prompt(self, prompt: str, agent: str | None = None) -> dict:
        return {"status": "host_pickup_required", "agent": agent}

    def invoke_claude_skill(self, skill: str, args: dict) -> dict:
        return {"status": "host_pickup_required", "skill": skill}

    def set_squad_packs(self, packs) -> None:
        pass


@pytest.fixture(autouse=True)
def _patch_harvest(monkeypatch):
    """Prevent git operations from running during tests. Also stub the
    target-repo scaffolding helpers (.gitignore / test-runner exclude
    patching) -- several tests here dispatch _via_mcp against the real
    HYDRA_ROOT checkout, and those helpers would otherwise write into it.
    They are exercised hermetically against tmp_path in
    tests/test_target_repo_scaffolding.py."""
    monkeypatch.setattr(
        "hydra_core.squad_node.harvest_pp_run_artifacts",
        lambda **_k: None,
    )
    monkeypatch.setattr(
        "hydra_core.squad_node.ensure_target_repo_ignores",
        lambda _project_path: None,
    )
    monkeypatch.setattr(
        "hydra_core.squad_node.ensure_target_repo_test_excludes",
        lambda _project_path: None,
    )


def _build_sup(disp=None):
    from hydra_core.supervisor import build_supervisor
    return build_supervisor(
        project_root=HYDRA_ROOT,
        dispatcher=disp or _StubDispatcher(),
        force_pure_python=True,
    )


def _run(state, *, stop_before=None):
    from hydra_core.supervisor import _PurePythonRunner
    sup = _build_sup()
    assert isinstance(sup, _PurePythonRunner)
    return sup.invoke(state, stop_before=stop_before)


# ===========================================================================
# RA-12a: dispatch skip for attended_done_task_ids
# ===========================================================================


def test_ra12a_attended_done_skips_dispatch():
    """A task in attended_done_task_ids must not invoke start_run.

    RA-12a block in node_dispatch marks the task done BEFORE the fleet/
    sequential dispatch so neither path ever calls execute_squad.
    """
    from hydra_core.state import HydraState, TaskState
    from hydra_core.supervisor import _PurePythonRunner

    task_id = uuid4()
    disp = _StubDispatcher()
    sup = _build_sup(disp)
    assert isinstance(sup, _PurePythonRunner)

    state = HydraState(
        root_goal="fix the bug",
        selected_squads=["engineering"],
        tasks=[TaskState(
            task_id=task_id,
            owner_squad="engineering",
            description="fix the bug",
            status="pending",
        )],
        # Signal that the attended host already drove this to completion.
        attended_done_task_ids=[str(task_id)],
    )

    sup.invoke(state)

    start_run_calls = [(s, t) for s, t, _ in disp.calls if t == "start_run"]
    assert start_run_calls == [], (
        f"start_run must NOT be called for an attended-done task; got: {disp.calls}"
    )


def test_ra12a_attended_done_emits_trace(monkeypatch):
    """dispatch.attended_already_complete trace event fires for attended-done tasks."""
    from hydra_core.state import HydraState, TaskState
    from hydra_core.supervisor import build_supervisor, _PurePythonRunner

    emitted: list[dict] = []

    def _capture(root, wf_id, kind, payload):
        emitted.append({"kind": kind, "payload": payload})

    monkeypatch.setattr("hydra_core.supervisor.emit_trace", _capture)

    task_id = uuid4()
    disp = _StubDispatcher()
    sup = build_supervisor(
        project_root=HYDRA_ROOT,
        dispatcher=disp,
        force_pure_python=True,
    )
    assert isinstance(sup, _PurePythonRunner)

    # WS1-E: engineering dispatch requires an explicit, resolved target repo
    # -- this test's concern is the attended_already_complete trace, so give
    # it a real target ("hydra", this checkout).
    state = HydraState(
        root_goal="fix the bug",
        selected_squads=["engineering"],
        target_repo_id="hydra",
        tasks=[TaskState(
            task_id=task_id,
            owner_squad="engineering",
            description="fix the bug",
            status="pending",
        )],
        attended_done_task_ids=[str(task_id)],
    )
    sup.invoke(state)

    attended_traces = [
        e for e in emitted
        if e["kind"] == "dispatch.attended_already_complete"
    ]
    assert attended_traces, (
        f"Expected dispatch.attended_already_complete trace; "
        f"got kinds: {[e['kind'] for e in emitted]}"
    )
    payload = attended_traces[0]["payload"]
    assert str(task_id) in str(payload.get("task_id") or "")
    assert payload.get("squad") == "engineering"


def test_ra12a_non_done_task_still_dispatches():
    """A task NOT in attended_done_task_ids is dispatched normally."""
    from hydra_core.state import HydraState, TaskState
    from hydra_core.supervisor import _PurePythonRunner

    done_task_id = uuid4()
    other_task_id = uuid4()
    disp = _StubDispatcher()
    sup = _build_sup(disp)
    assert isinstance(sup, _PurePythonRunner)

    # WS1-E: engineering dispatch requires an explicit, resolved target repo
    # -- this test's concern is non-done tasks still dispatching, so give it
    # a real target ("hydra", this checkout).
    state = HydraState(
        root_goal="fix the bug",
        selected_squads=["engineering"],
        target_repo_id="hydra",
        tasks=[
            TaskState(
                task_id=done_task_id,
                owner_squad="engineering",
                description="already done task",
                status="pending",
            ),
            TaskState(
                task_id=other_task_id,
                owner_squad="engineering",
                description="pending task",
                status="pending",
            ),
        ],
        attended_done_task_ids=[str(done_task_id)],
    )
    sup.invoke(state)

    start_run_calls = [(s, t) for s, t, _ in disp.calls if t == "start_run"]
    assert start_run_calls, (
        "start_run must be called for the task NOT in attended_done_task_ids"
    )


def test_ra12a_empty_attended_done_dispatches_all():
    """With an empty attended_done_task_ids, pending tasks are dispatched normally."""
    from hydra_core.state import HydraState, TaskState
    from hydra_core.supervisor import _PurePythonRunner

    task_id = uuid4()
    disp = _StubDispatcher()
    sup = _build_sup(disp)
    assert isinstance(sup, _PurePythonRunner)

    # WS1-E: engineering dispatch requires an explicit, resolved target repo
    # -- this test's concern is the empty-attended-done-ids path, so give it
    # a real target ("hydra", this checkout).
    state = HydraState(
        root_goal="implement the feature",
        selected_squads=["engineering"],
        target_repo_id="hydra",
        tasks=[TaskState(
            task_id=task_id,
            owner_squad="engineering",
            description="implement the feature",
            status="pending",
        )],
        attended_done_task_ids=[],  # empty → no skip
    )
    sup.invoke(state)

    start_run_calls = [(s, t) for s, t, _ in disp.calls if t == "start_run"]
    assert start_run_calls, "start_run must be called when attended_done_task_ids is empty"


# ===========================================================================
# RA-12b: pp provenance threading
# ===========================================================================


def test_ra12b_start_run_carries_workflow_id():
    """start_run payload must include hydra_workflow_id = str(state.workflow_id)."""
    from hydra_core.state import HydraState
    from hydra_core.supervisor import _PurePythonRunner

    wf_id = uuid4()
    disp = _StubDispatcher()
    sup = _build_sup(disp)
    assert isinstance(sup, _PurePythonRunner)

    # WS1-E: engineering dispatch requires an explicit, resolved target repo
    # -- this test's concern is workflow_id threading, so give it a real
    # target ("hydra", this checkout).
    state = HydraState(
        workflow_id=wf_id,
        root_goal="implement the feature",
        selected_squads=["engineering"],
        target_repo_id="hydra",
    )
    sup.invoke(state)

    start_run_args = [a for s, t, a in disp.calls if t == "start_run"]
    assert start_run_args, "start_run must have been called"

    payload = start_run_args[0]
    assert "hydra_workflow_id" in payload, (
        f"start_run payload missing hydra_workflow_id; keys: {list(payload)}"
    )
    assert payload["hydra_workflow_id"] == str(wf_id), (
        f"hydra_workflow_id mismatch: {payload['hydra_workflow_id']!r} != {str(wf_id)!r}"
    )


def test_ra12b_start_run_carries_envelope_enrichment():
    """start_run payload carries hydra_origin_squad and hydra_envelope_type."""
    from hydra_core.state import HydraState
    from hydra_core.squad_node import _via_mcp
    from hydra_core.schemas import CSuiteDecisionPacket
    from hydra_core.squad_loader import discover_squads

    wf_id = uuid4()
    packs = discover_squads(HYDRA_ROOT)
    pack = packs.get("engineering")
    if pack is None:
        pytest.skip("engineering pack not discovered in test environment")

    state = HydraState(workflow_id=wf_id, root_goal="implement the feature")
    disp = _StubDispatcher()

    # WS1-E: engineering dispatch requires an explicit, resolved target repo
    # -- this test's concern is envelope-enrichment threading, so give it a
    # real target ("hydra", this checkout).
    inbound = CSuiteDecisionPacket(
        workflow_id=wf_id,
        origin_squad="hydra",
        target_squad="engineering",
        origin="BOARDROOM",
        objective="implement the feature",
        target_repo_id="hydra",
    )

    _via_mcp(state, pack, inbound, disp)

    start_run_args = [a for s, t, a in disp.calls if t == "start_run"]
    assert start_run_args, "start_run must have been called"
    payload = start_run_args[0]

    assert payload.get("hydra_workflow_id") == str(wf_id), (
        f"hydra_workflow_id missing or wrong: {payload.get('hydra_workflow_id')!r}"
    )
    assert payload.get("hydra_origin_squad") == "hydra", (
        f"hydra_origin_squad missing or wrong: {payload.get('hydra_origin_squad')!r}"
    )
    assert "hydra_envelope_type" in payload, (
        f"hydra_envelope_type missing; keys: {list(payload)}"
    )
    assert payload["hydra_envelope_type"] == "C_SUITE_DECISION_PACKET"
    assert "hydra_envelope_id" in payload, (
        f"hydra_envelope_id missing; keys: {list(payload)}"
    )


# ===========================================================================
# RA-10: goal-text --squad flag in node_intake
# ===========================================================================


def _intake(goal: str, pre_selected: list[str] | None = None):
    """Run node_intake only (stop_before='planner') and return resulting state."""
    from hydra_core.state import HydraState
    state = HydraState(root_goal=goal)
    if pre_selected is not None:
        state.selected_squads = pre_selected
    return _run(state, stop_before="planner")


def test_ra10_tail_squad_flag_forces_selection():
    """Tail --squad engineering forces selection and strips the flag from goal."""
    goal = "Fix the authentication bug --squad engineering"
    final = _intake(goal)

    assert "engineering" in final.selected_squads, (
        f"Expected engineering in selected_squads; got {final.selected_squads}"
    )
    assert "--squad" not in (final.root_goal or ""), (
        f"'--squad' not stripped from root_goal: {final.root_goal!r}"
    )


def test_ra10_tail_squad_goal_content_preserved():
    """After stripping the --squad flag, the actual task description survives."""
    goal = "Fix authentication bug in login flow --squad engineering"
    final = _intake(goal)

    assert "Fix authentication bug" in (final.root_goal or ""), (
        f"Task description missing after strip: {final.root_goal!r}"
    )
    # The slug must not be left in the goal text
    assert "engineering" not in (final.root_goal or ""), (
        f"Squad slug not stripped from goal: {final.root_goal!r}"
    )


def test_ra10_midprose_non_slug_ignored_with_trace(monkeypatch):
    """Mid-prose --squad with non-slug word is ignored with a trace event."""
    from hydra_core.state import HydraState
    from hydra_core.supervisor import build_supervisor, _PurePythonRunner

    emitted: list[dict] = []

    def _capture(root, wf_id, kind, payload):
        emitted.append({"kind": kind, "payload": payload})

    monkeypatch.setattr("hydra_core.supervisor.emit_trace", _capture)

    # Pad the goal so --squad occurs well before the tail 120-char window.
    padding = "A" * 200
    goal = f"Fix the feature using --squad nonexistent here {padding}"

    state = HydraState(root_goal=goal)
    sup = build_supervisor(
        project_root=HYDRA_ROOT,
        dispatcher=_StubDispatcher(),
        force_pure_python=True,
    )
    assert isinstance(sup, _PurePythonRunner)
    sup.invoke(state, stop_before="planner")

    ignored_traces = [
        e for e in emitted
        if e["kind"] == "intake.squad_flag_like_token_ignored"
    ]
    assert ignored_traces, (
        f"Expected intake.squad_flag_like_token_ignored trace; "
        f"got kinds: {[e['kind'] for e in emitted]}"
    )


def test_ra10_unknown_slug_at_tail_errors():
    """Unknown slug at tail position surfaces a HITL (not a silent ignore)."""
    goal = "Fix the bug --squad totally-made-up-squad"
    final = _intake(goal)

    assert final.phase == "surfaced", (
        f"Expected phase='surfaced' for unknown slug at tail; got {final.phase}"
    )
    assert final.pending_hitl is not None, "Expected pending_hitl for unknown slug"
    assert "totally-made-up-squad" in str(final.pending_hitl.get("summary", "")), (
        f"Unknown slug not in HITL summary: {final.pending_hitl.get('summary')!r}"
    )


def test_ra10_squad_flag_cli_preseeded_wins():
    """CLI-preseeded selected_squads wins: RA-10 block is only active when list is empty."""
    goal = "Fix the bug --squad garland"
    final = _intake(goal, pre_selected=["engineering"])

    assert "engineering" in final.selected_squads, (
        f"CLI-preseeded engineering must win; got {final.selected_squads}"
    )


# ===========================================================================
# RA-10: --repo multiplicity fix in parse_repo_arg
# ===========================================================================


def test_ra10_midprose_plus_tail_repo_no_longer_aborts():
    """Mid-prose --repo <unknown> + tail --repo <known> must NOT raise 'more than once'.

    Before fix: ALL --repo matches were counted regardless of prose-safe rule,
    so a goal with '--repo flags' (mid-prose, unknown id) plus '--repo hydra'
    (tail, known id) aborted with "specified more than once".
    After fix: only explicit occurrences (tail-position OR known id) are counted.
    """
    from hydra_core.repo_registry import parse_repo_arg, RepoFlagIgnored

    # Build a goal where '--repo flags' is at the start (far outside the final
    # 120 chars) and '--repo hydra' is at the end (tail, known id).
    filler = "x" * 180
    goal = f"--repo flags {filler} --repo hydra"
    # Length = ~205 chars; tail_thresh = max(0, 205-120) = 85
    # '--repo flags' at pos 0:   0 < 85 AND 'flags' not known → NOT explicit
    # '--repo hydra' at pos ~194: 194 >= 85 AND 'hydra' is known → explicit
    # _ra10_explicit = 1 → no "more than once" error

    try:
        parse_repo_arg(goal)
    except RepoFlagIgnored:
        # CORRECT: mid-prose unknown --repo correctly raised RepoFlagIgnored
        pass
    except ValueError as e:
        if "more than once" in str(e):
            pytest.fail(
                f"RA-10 multiplicity fix broken: 'more than once' still raised: {e}"
            )
        # Other ValueError (e.g., unknown repo id at explicit position) is acceptable


def test_ra10_two_known_repo_ids_still_errors():
    """Two known repo ids both at tail must still raise 'more than once'."""
    from hydra_core.repo_registry import parse_repo_arg

    # Short goal: tail_thresh = 0, so EVERY position is "tail"
    goal = "--repo hydra --repo agentsmith"
    with pytest.raises(ValueError, match="more than once"):
        parse_repo_arg(goal)


def test_ra10_single_known_repo_succeeds():
    """Single known --repo id at tail still works cleanly."""
    from hydra_core.repo_registry import parse_repo_arg

    repo_id, cleaned = parse_repo_arg("Fix the bug --repo hydra")
    assert repo_id == "hydra"
    assert "--repo" not in cleaned


# ===========================================================================
# RA-3: Documentation changes
# ===========================================================================


def test_ra3_contributing_squads_has_tool_declaration_naming():
    """CONTRIBUTING-SQUADS.md must contain the Tool declaration naming subsection
    with the correct auto-authorized claude-skill shim tool pair.

    The auto-authorized pair is {shim_prefix}.command.list /
    {shim_prefix}.output.write (from _SKILL_PACK_SHIMS), NOT slug.ping /
    invoke_skill.  This test pins those names so a wrong doc value is caught.
    """
    doc = (HYDRA_ROOT / "CONTRIBUTING-SQUADS.md").read_text(encoding="utf-8")
    assert "Tool declaration naming" in doc, (
        "CONTRIBUTING-SQUADS.md missing 'Tool declaration naming' subsection"
    )
    # Subsection must explain the HOST-side vs RBAC distinction
    assert "host-side" in doc.lower() or "HOST-side" in doc, (
        "Subsection must mention host-side capability bindings"
    )
    assert "RBAC" in doc or "_check_tool_rbac" in doc, (
        "Subsection must reference dispatcher RBAC"
    )
    # Pin the CORRECT auto-authorized shim tool pair names so a wrong claim
    # (e.g. .ping / .invoke_skill) is caught immediately.
    assert "command.list" in doc, (
        "Subsection must name 'command.list' as one of the auto-authorized shim tools "
        "(the pair is {shim_prefix}.command.list / {shim_prefix}.output.write)"
    )
    assert "output.write" in doc, (
        "Subsection must name 'output.write' as one of the auto-authorized shim tools "
        "(the pair is {shim_prefix}.command.list / {shim_prefix}.output.write)"
    )


def test_ra3_customer_support_squad_yaml_has_tool_comment():
    """squads/customer-support/squad.yaml must have a comment near the tools block."""
    yaml_path = HYDRA_ROOT / "squads" / "customer-support" / "squad.yaml"
    content = yaml_path.read_text(encoding="utf-8")
    has_ref = (
        "CONTRIBUTING-SQUADS.md" in content
        or "Tool declaration" in content
        or "tool declaration" in content
    )
    assert has_ref, (
        "customer-support/squad.yaml missing reference to CONTRIBUTING-SQUADS.md "
        "or tool declaration naming note"
    )
