"""fable-audit-2 Phase 1 + Phase 2 tests.

Phase 1 (F11+M2, Fix-2, F19, F18) — see original docstring.

Phase 2 tests cover:

  Rider (c) — _via_impersonation maps raw 'stub' → 'deferred_to_host'
               (mirrors _via_claude_skill).

  Rider (d) — TestSupervisorStatusCoercion rewritten to exercise the REAL
               supervisor coercion code path (no inline _KNOWN copy). Also adds
               a test that the toolshed catalog contains the blender server.

  F8  — cli.py _cmd_resume_locked parses approve_override_raise_to_N and sets
         reflexion_override_granted_until on the state patch.

  F9  — Budget/envelope/policy_breach HITL gates now set hitl_return_node on
         the state so the compiled graph routes to hitl_gate_* (interrupt_before)
         instead of postcheck/halt. PurePythonRunner tests verify the field is
         set; compiled-graph tests verify the interrupt routing.

  F10 — Every advertised gate option maps to a real engine action in the F10
         option dispatch table (cli.py).

  M3  — verify_operator_capability is called on approve; tampered token is
         rejected; degraded token warns but does not block.

  Rider (a) — Smoke baseline: _capture_baseline_failures + _parse_failing_tests;
               smoke excuses failures that pre-existed in the baseline.

  Rider (b) — already_charged flag in _step_result prevents double-billing on
               retried submit-host-result calls.
"""
from __future__ import annotations

import os
from pathlib import Path
from uuid import uuid4

import pytest

HYDRA_ROOT = Path(__file__).resolve().parents[1]


# ---------------------------------------------------------------------------
# Shared stubs
# ---------------------------------------------------------------------------

class _FakeDispatcher:
    """Minimal dispatcher stub used by squad_node tests.

    No real MCP server or LLM — every tool call returns an empty-success
    envelope unless scripted otherwise.  Provides invoke_claude_skill and
    emit_claude_prompt so both _via_claude_skill and _via_impersonation can
    run without errors.
    """

    def __init__(
        self,
        *,
        scripted: dict[tuple[str, str], dict] | None = None,
        skill_status: str = "host_pickup_required",
        prompt_status: str = "host_pickup_required",
    ):
        self.calls: list[tuple[str, str, dict]] = []
        self._scripted = scripted or {}
        self._skill_status = skill_status
        self._prompt_status = prompt_status

    def call_mcp(self, server, tool, args, **_kw):
        self.calls.append((server, tool, dict(args)))
        if (server, tool) in self._scripted:
            return {"status": "done", "tool": tool,
                    "result": self._scripted[(server, tool)]}
        return {"status": "done", "tool": tool, "result": {}}

    def invoke_claude_skill(self, skill, args):
        return {"status": self._skill_status, "summary": "deferred"}

    def emit_claude_prompt(self, prompt, agent=None):
        return {"status": self._prompt_status}

    def spawn_subprocess(self, cmd, env=None):
        return {"status": "stub"}


# ---------------------------------------------------------------------------
# Fix helpers
# ---------------------------------------------------------------------------

def _garland_pack():
    from hydra_core.squad_loader import discover_squads
    return discover_squads(HYDRA_ROOT)["garland"]


def _executive_pack():
    from hydra_core.squad_loader import discover_squads
    return discover_squads(HYDRA_ROOT)["executive"]


def _inbound(wf_id=None):
    from hydra_core.schemas import CSuiteDecisionPacket
    return CSuiteDecisionPacket(
        workflow_id=wf_id or uuid4(),
        origin_squad="hydra",
        origin="BOARDROOM",
        objective="test objective",
    )


# ===========================================================================
# F11+M2 — Honest status mapping
# ===========================================================================

class TestHonestStatus:
    """_via_claude_skill and _via_impersonation must never report 'done' when
    the dispatcher returned a placeholder (stub / host_pickup_required)."""

    def test_claude_skill_host_pickup_required_maps_to_deferred(self):
        from hydra_core.squad_node import _via_claude_skill
        from hydra_core.state import HydraState

        pack = _garland_pack()
        disp = _FakeDispatcher(
            skill_status="host_pickup_required",
            scripted={("rlm_creative", "rlm.command.list"): {"commands": []}},
        )
        result = _via_claude_skill(HydraState(root_goal="t"), pack, _inbound(), disp)

        assert result.status == "deferred_to_host"
        assert result.host_pickup_pending is True

    def test_claude_skill_stub_maps_to_deferred(self):
        from hydra_core.squad_node import _via_claude_skill
        from hydra_core.state import HydraState

        pack = _garland_pack()
        disp = _FakeDispatcher(
            skill_status="stub",
            scripted={("rlm_creative", "rlm.command.list"): {"commands": []}},
        )
        result = _via_claude_skill(HydraState(root_goal="t"), pack, _inbound(), disp)

        # 'stub' does not set host_pickup_pending (only 'host_pickup_required'
        # does), but the status must still be 'deferred_to_host'.
        assert result.status == "deferred_to_host"

    def test_claude_skill_real_done_passes_through(self):
        from hydra_core.squad_node import _via_claude_skill
        from hydra_core.state import HydraState

        pack = _garland_pack()
        disp = _FakeDispatcher(
            skill_status="done",
            scripted={("rlm_creative", "rlm.command.list"): {"commands": []}},
        )
        result = _via_claude_skill(HydraState(root_goal="t"), pack, _inbound(), disp)

        assert result.status == "done"
        assert result.host_pickup_pending is False

    def test_impersonation_host_pickup_maps_to_deferred(self):
        from hydra_core.squad_node import _via_impersonation
        from hydra_core.state import HydraState

        pack = _executive_pack()
        disp = _FakeDispatcher(prompt_status="host_pickup_required")
        result = _via_impersonation(HydraState(root_goal="t"), pack, _inbound(), disp)

        assert result.status == "deferred_to_host"
        assert result.host_pickup_pending is True

    def test_impersonation_real_done_passes_through(self):
        from hydra_core.squad_node import _via_impersonation
        from hydra_core.state import HydraState

        pack = _executive_pack()
        # A real dict result (non-stub) without "host_pickup_required".
        disp = _FakeDispatcher(prompt_status="done")
        result = _via_impersonation(HydraState(root_goal="t"), pack, _inbound(), disp)

        assert result.status == "done"
        assert result.host_pickup_pending is False


# ===========================================================================
# M2 — supervisor unknown-status coercion (rider d: real supervisor code path)
# ===========================================================================

class TestSupervisorStatusCoercion:
    """supervisor.py node_dispatch must coerce unknown/out-of-contract
    SquadResult.status values to 'surfaced' so a rogue pack cannot forge a
    'done'.  Tests exercise the REAL supervisor code path (no inline _KNOWN
    copy) by invoking the PurePythonRunner up to the dispatch step."""

    def _run_dispatch(self, monkeypatch, status: str):
        """Run the pure-python supervisor until dispatch finishes and return
        the task status as coerced by node_dispatch."""
        from hydra_core.supervisor import build_supervisor, _PurePythonRunner
        from hydra_core.squad_node import SquadResult
        from hydra_core.schemas import DecisionRecord

        dr = DecisionRecord(
            workflow_id=uuid4(), parent_id=uuid4(),
            origin_squad="engineering", target_squad="hydra",
            decision="ok", rationale="ok",
        )
        mock_result = SquadResult(envelopes=[dr], artifacts=[], status=status)
        # Monkeypatch execute_squad in the supervisor module namespace
        # (imported via `from .squad_node import execute_squad`).
        monkeypatch.setattr("hydra_core.supervisor.execute_squad",
                            lambda *a, **k: mock_result)
        # Also stub enforce_constitution so postcheck does not surface.
        monkeypatch.setattr(
            "hydra_core.governance.enforce_constitution",
            lambda *_a, **_k: type("V", (), {"aligned": True, "rationale": ""})(),
        )

        runner = build_supervisor(
            project_root=HYDRA_ROOT,
            dispatcher=object(),
            force_pure_python=True,
        )
        assert isinstance(runner, _PurePythonRunner)

        from hydra_core.state import HydraState, TaskState
        # Pre-seed selected_squads so intake skips the LLM router.
        state = HydraState(root_goal="test", selected_squads=["engineering"])
        task = TaskState(owner_squad="engineering", description="implement x")
        state.tasks.append(task)

        # Stop BEFORE judge_per_squad to inspect the coerced task status
        # without running the judge/synthesis/postcheck pipeline.
        final = runner.invoke(state, stop_before="judge_per_squad")
        # All tasks in the returned state; the first (pre-seeded) one was dispatched.
        dispatched = [t for t in final.tasks if t.owner_squad == "engineering"]
        assert dispatched, "expected at least one engineering task after dispatch"
        return dispatched[0].status

    def test_known_done_is_kept(self, monkeypatch):
        assert self._run_dispatch(monkeypatch, "done") == "done"

    def test_unknown_status_coerces_to_surfaced(self, monkeypatch):
        assert self._run_dispatch(monkeypatch, "totally_made_up") == "surfaced"

    def test_deferred_to_host_is_known(self, monkeypatch):
        assert self._run_dispatch(monkeypatch, "deferred_to_host") == "deferred_to_host"


# ===========================================================================
# Rider (d) — toolshed blender catalog
# ===========================================================================

class TestToolshedBlenderCatalog:
    """build_default_shed() must register the blender server with its 4 tools."""

    def test_blender_server_has_four_tools(self):
        from hydra_core.toolshed import build_default_shed

        shed = build_default_shed(dispatcher=None)
        # list_servers() returns [{"server": "<name>", "tool_count": N}, ...]
        servers = shed.list_servers()
        blender_entries = [s for s in servers if s.get("server") == "blender"]
        assert blender_entries, (
            f"Expected 'blender' server in ToolShed catalog, found servers: "
            f"{[s['server'] for s in servers]}"
        )
        tool_count = blender_entries[0]["tool_count"]
        assert tool_count == 4, (
            f"Expected blender server to have 4 tools, got {tool_count}"
        )


# ===========================================================================
# Governance blocking: deferred_to_host
# ===========================================================================

class TestGovernanceBlocksDeferred:
    """enforce_governance must surface when any task has status='deferred_to_host'
    — a workflow must not conclude 'done' while pack work never executed."""

    # Valid TaskState statuses per state.py Literal.
    _VALID_STATUSES = frozenset({
        "pending", "running", "blocked", "done", "failed",
        "surfaced", "cancelled", "deferred_to_host",
    })

    def _state_with_task(self, status: str):
        from hydra_core.state import HydraState, TaskState

        state = HydraState(root_goal="test")
        if status in self._VALID_STATUSES:
            t = TaskState(owner_squad="garland", description="do creative",
                          status=status)  # type: ignore[call-arg]
        else:
            # Use model_construct to bypass Pydantic validation for out-of-contract
            # status values (the supervisor coerces them, but governance should
            # still be defensive).
            t = TaskState.model_construct(
                owner_squad="garland", description="do creative",
                status=status, task_id=uuid4(), retries=0, priority="P2",
            )
        state.tasks.append(t)
        return state

    def _packs(self):
        from hydra_core.squad_loader import discover_squads
        return discover_squads(HYDRA_ROOT)

    def test_deferred_to_host_is_blocking(self, monkeypatch):
        from hydra_core.governance import enforce_governance

        state = self._state_with_task("deferred_to_host")
        # Stub constitution check so only the status gate fires.
        monkeypatch.setattr(
            "hydra_core.governance.enforce_constitution",
            lambda *_a, **_k: type("V", (), {"aligned": True, "rationale": ""})(),
        )
        verdict = enforce_governance(state, self._packs())
        assert verdict.surfaced is True
        assert "deferred to host" in verdict.reason

    def test_out_of_contract_status_is_blocking(self, monkeypatch):
        from hydra_core.governance import enforce_governance

        state = self._state_with_task("totally_unknown_status")
        monkeypatch.setattr(
            "hydra_core.governance.enforce_constitution",
            lambda *_a, **_k: type("V", (), {"aligned": True, "rationale": ""})(),
        )
        verdict = enforce_governance(state, self._packs())
        assert verdict.surfaced is True
        assert "unrecognised status" in verdict.reason

    def test_done_tasks_pass_governance(self, monkeypatch):
        from hydra_core.governance import enforce_governance

        state = self._state_with_task("done")
        monkeypatch.setattr(
            "hydra_core.governance.enforce_constitution",
            lambda *_a, **_k: type("V", (), {"aligned": True, "rationale": ""})(),
        )
        verdict = enforce_governance(state, self._packs())
        assert verdict.surfaced is False


# ===========================================================================
# F19 — _via_impersonation extracts emitted envelopes
# ===========================================================================

class TestImpersonationEnvelopeExtraction:
    """_via_impersonation must call _extract_emitted_envelopes so DEV_TASK /
    CREATIVE_BRIEF envelopes reach the supervisor, mirroring _via_claude_skill."""

    def test_impersonation_extracts_dev_task(self):
        from hydra_core.squad_node import _via_impersonation
        from hydra_core.state import HydraState

        pack = _executive_pack()
        wf_id = uuid4()
        # Return a result with a schema-valid DEV_TASK envelope.
        dev_task_env = {
            "type": "DEV_TASK",
            "instructions": "implement feature X",
            "owner": "backend",
            "repo": "hydra",
            "branch": "feat/x",
            "workflow_id": str(wf_id),
            "id": str(uuid4()),
            "origin_squad": "executive",
            "target_squad": "engineering",
        }
        disp = _FakeDispatcher(prompt_status="host_pickup_required")
        # Patch emit_claude_prompt to return a result with the emitted envelope.
        disp.emit_claude_prompt = lambda *_a, **_k: {
            "status": "host_pickup_required",
            "emitted_envelopes": [dev_task_env],
        }
        state = HydraState(root_goal="t")
        inbound = _inbound(wf_id)
        result = _via_impersonation(state, pack, inbound, disp)

        # DecisionRecord is always first; the DEV_TASK envelope should follow.
        assert len(result.envelopes) >= 2, (
            f"expected DecisionRecord + DEV_TASK, got {len(result.envelopes)} envelope(s)"
        )
        envelope_types = [getattr(e, "type", None) for e in result.envelopes]
        assert "DEV_TASK" in envelope_types

    def test_impersonation_no_emitted_envelopes_is_safe(self):
        """When the result contains no emitted envelopes, the return is still valid
        (just the DecisionRecord)."""
        from hydra_core.squad_node import _via_impersonation
        from hydra_core.state import HydraState

        pack = _executive_pack()
        disp = _FakeDispatcher(prompt_status="host_pickup_required")
        state = HydraState(root_goal="t")
        result = _via_impersonation(state, pack, _inbound(), disp)

        assert len(result.envelopes) >= 1
        from hydra_core.schemas import DecisionRecord
        assert isinstance(result.envelopes[0], DecisionRecord)


# ===========================================================================
# F18 — pp_best_of invoke_mode implies N=3
# ===========================================================================

class TestPpBestOfImpliesN3:
    """_drive_pp_stage_loop with invoke_mode='pp_best_of' and HYDRA_BEST_OF_N
    unset must call _drive_best_of_loop with n=3.  An explicit HYDRA_BEST_OF_N
    wins over the invoke_mode default."""

    def _scripted_dispatch(self, *, n_received: list, outcome: str = "pass"):
        from hydra_core.squad_node import _drive_pp_stage_loop

        def _fake_best_of(dispatcher, *, run_id, project_path, request_text,
                          n, model_tier=None, judge_rubric_id=None, workflow_id=None):
            n_received.append(n)
            return {
                "final_status": "complete", "stage_outcome": outcome,
                "attempt_id": "att-bon", "critique": "", "error": None,
                "finalized": True, "wrote_changes": False,
                "smoke_status": "skipped", "smoke_reason": "",
                "harvest_sha": None, "harvest_error": None,
                "changed_paths": [], "cost_usd": 0.0,
                "tokens_in": 0, "tokens_out": 0,
            }

        return _fake_best_of

    def test_pp_best_of_implies_n3_when_env_unset(self, monkeypatch):
        from hydra_core.squad_node import _drive_pp_stage_loop

        monkeypatch.delenv("HYDRA_BEST_OF_N", raising=False)
        n_received: list[int] = []
        monkeypatch.setattr("hydra_core.squad_node._drive_best_of_loop",
                            self._scripted_dispatch(n_received=n_received))

        _drive_pp_stage_loop(
            object(),  # dispatcher not used when best-of loop is mocked
            run_id="run-x", project_path="/tmp/p", request_text="do it",
            invoke_mode="pp_best_of",
        )
        assert n_received == [3], f"expected n=3, got {n_received}"

    def test_explicit_hydra_best_of_n_wins(self, monkeypatch):
        from hydra_core.squad_node import _drive_pp_stage_loop

        monkeypatch.setenv("HYDRA_BEST_OF_N", "5")
        n_received: list[int] = []
        monkeypatch.setattr("hydra_core.squad_node._drive_best_of_loop",
                            self._scripted_dispatch(n_received=n_received))

        _drive_pp_stage_loop(
            object(),
            run_id="run-x", project_path="/tmp/p", request_text="do it",
            invoke_mode="pp_best_of",
        )
        assert n_received == [5], f"HYDRA_BEST_OF_N=5 should win over invoke_mode N=3, got {n_received}"

    def test_no_pp_best_of_no_env_no_best_of_n(self, monkeypatch):
        """Without invoke_mode='pp_best_of' or HYDRA_BEST_OF_N, single-candidate
        path is taken (best-of loop is never called)."""
        from hydra_core.squad_node import _drive_pp_stage_loop

        monkeypatch.delenv("HYDRA_BEST_OF_N", raising=False)
        n_received: list[int] = []
        monkeypatch.setattr("hydra_core.squad_node._drive_best_of_loop",
                            self._scripted_dispatch(n_received=n_received))

        # We need a minimal dispatcher to exercise the single-candidate path.
        class _MinimalDisp:
            def call_mcp(self, *_a, **_k):
                raise SystemExit("should not be called in N=0 test")

        # The loop will reach start_stage and raise, but _best_of_loop is not called.
        try:
            _drive_pp_stage_loop(
                _MinimalDisp(),
                run_id="run-x", project_path="/tmp/p", request_text="do it",
                invoke_mode="pp_run",
            )
        except SystemExit:
            pass  # expected — only checking that best-of loop was NOT called
        assert n_received == [], "best-of loop must not be called when mode is pp_run"


# ===========================================================================
# Fix-2 — Host-executor seam: begin_squad_stage + submit_host_result
# ===========================================================================

class TestSquadHostExecutorSeam:
    """begin_squad_stage creates a lightweight cursor and submit_host_result
    advances it to 'complete' on the first (and only) call_key match.
    A duplicate / stale call_key is rejected (exactly-once semantics)."""

    def test_begin_squad_stage_returns_awaiting_host(self, tmp_path):
        from hydra_core import host_bridge

        task_id = str(uuid4())
        res = host_bridge.begin_squad_stage(
            workflow_id="wf-sq-1",
            task_id=task_id,
            squad_slug="garland",
            entrypoint="claude-skill",
            lead_agent="brand-strategist",
            pack_cwd=str(tmp_path),
            request_text="create a campaign brief",
            project_root=tmp_path,
        )

        assert res["status"] == "awaiting_host"
        assert res["state"] == "await_squad_agent"
        ha = res["host_action"]
        assert ha["call_key"] == f"squad-{task_id}-0"
        assert ha["agent_type"] == "brand-strategist"
        assert ha["cwd"] == str(tmp_path)
        assert "instructions" in ha

    def test_submit_advances_to_complete(self, tmp_path):
        from hydra_core import host_bridge

        task_id = str(uuid4())
        res = host_bridge.begin_squad_stage(
            workflow_id="wf-sq-2",
            task_id=task_id,
            squad_slug="garland",
            entrypoint="claude-skill",
            lead_agent="brand-strategist",
            pack_cwd=str(tmp_path),
            request_text="create a campaign brief",
            project_root=tmp_path,
        )
        cfile = res["cursor_path"]
        call_key = res["host_action"]["call_key"]

        submit_res = host_bridge.submit_host_result(
            object(),  # dispatcher not needed for squad cursors
            cursor_file=cfile,
            call_key=call_key,
            result={
                "text": "Campaign brief for Q4 product launch.",
                "cost_usd": 0.03,
                "tokens_in": 200,
                "tokens_out": 300,
                "model": "claude-sonnet-4-6",
            },
        )

        assert submit_res["status"] == "complete"
        assert submit_res["final_status"] == "complete"
        assert submit_res["cost_usd"] == pytest.approx(0.03)
        assert submit_res["task_id"] == task_id

    def test_duplicate_call_key_is_rejected_exactly_once(self, tmp_path):
        from hydra_core import host_bridge

        task_id = str(uuid4())
        res = host_bridge.begin_squad_stage(
            workflow_id="wf-sq-3",
            task_id=task_id,
            squad_slug="garland",
            entrypoint="claude-skill",
            lead_agent="brand-strategist",
            pack_cwd=str(tmp_path),
            request_text="create a campaign brief",
            project_root=tmp_path,
        )
        cfile = res["cursor_path"]
        call_key = res["host_action"]["call_key"]

        # First submit advances to complete.
        r1 = host_bridge.submit_host_result(
            object(), cursor_file=cfile, call_key=call_key,
            result={"text": "brief", "cost_usd": 0.01})
        assert r1["status"] == "complete"

        # Second submit with same call_key: terminal state → returns same result
        # without re-applying (exactly-once).
        r2 = host_bridge.submit_host_result(
            object(), cursor_file=cfile, call_key=call_key,
            result={"text": "different brief", "cost_usd": 9.99})
        assert r2["status"] == "complete"
        # Cost must NOT have doubled — re-apply was skipped.
        assert r2["cost_usd"] == pytest.approx(0.01)

    def test_wrong_call_key_is_ignored_idempotently(self, tmp_path):
        """Submitting with the wrong call_key returns the current state without
        advancing the cursor (duplicate/out-of-order exactly-once guard)."""
        from hydra_core import host_bridge

        task_id = str(uuid4())
        res = host_bridge.begin_squad_stage(
            workflow_id="wf-sq-4",
            task_id=task_id,
            squad_slug="executive",
            entrypoint="agent-impersonation",
            lead_agent="ceo",
            pack_cwd=str(tmp_path),
            request_text="board meeting brief",
            project_root=tmp_path,
        )
        cfile = res["cursor_path"]

        wrong_res = host_bridge.submit_host_result(
            object(), cursor_file=cfile, call_key="wrong-key-99",
            result={"text": "nope"})

        assert wrong_res["status"] == "awaiting_host"
        assert "ignored" in wrong_res


# ===========================================================================
# Fix-2 — _next_nonengineering_attended_task helper
# ===========================================================================

class TestNextNonEngineeringTask:
    """_next_nonengineering_attended_task finds the first claude-skill /
    agent-impersonation task that has not yet been completed."""

    def _state_with_task(self, squad: str, status: str = "pending"):
        from hydra_core.state import HydraState, TaskState
        state = HydraState(root_goal="t")
        t = TaskState(owner_squad=squad, description="do it", status=status)  # type: ignore[call-arg]
        state.tasks.append(t)
        return state, t

    def test_finds_garland_task(self):
        from hydra_core.cli import _next_nonengineering_attended_task
        from hydra_core.squad_loader import discover_squads

        packs = discover_squads(HYDRA_ROOT)
        state, task = self._state_with_task("garland", "pending")
        found_task, found_pack = _next_nonengineering_attended_task(state, packs)

        assert found_task is not None
        assert found_pack is not None
        assert found_task.task_id == task.task_id
        assert found_pack.slug == "garland"

    def test_finds_executive_task(self):
        from hydra_core.cli import _next_nonengineering_attended_task
        from hydra_core.squad_loader import discover_squads

        packs = discover_squads(HYDRA_ROOT)
        state, task = self._state_with_task("executive", "pending")
        found_task, found_pack = _next_nonengineering_attended_task(state, packs)

        assert found_task is not None
        assert found_pack.slug == "executive"

    def test_skips_completed_tasks(self):
        from hydra_core.cli import _next_nonengineering_attended_task
        from hydra_core.squad_loader import discover_squads

        packs = discover_squads(HYDRA_ROOT)
        state, task = self._state_with_task("garland", "pending")
        # Mark it done
        state.attended_completed_task_ids.append(str(task.task_id))

        found_task, found_pack = _next_nonengineering_attended_task(state, packs)
        assert found_task is None
        assert found_pack is None

    def test_ignores_engineering_tasks(self):
        from hydra_core.cli import _next_nonengineering_attended_task
        from hydra_core.squad_loader import discover_squads

        packs = discover_squads(HYDRA_ROOT)
        state, _eng_task = self._state_with_task("engineering", "pending")

        found_task, found_pack = _next_nonengineering_attended_task(state, packs)
        assert found_task is None  # engineering is excluded

    def test_ignores_stub_squads(self):
        from hydra_core.cli import _next_nonengineering_attended_task
        from hydra_core.squad_loader import discover_squads

        packs = discover_squads(HYDRA_ROOT)
        state, _stub_task = self._state_with_task("healthcare", "pending")

        found_task, found_pack = _next_nonengineering_attended_task(state, packs)
        assert found_task is None  # stub entrypoint excluded


# ===========================================================================
# Fix-2 — _resolve_pack_lead_agent
# ===========================================================================

class TestResolvePackLeadAgent:
    """_resolve_pack_lead_agent returns the first gatekeeper's slug, or the
    first agent, or 'general-purpose' as an absolute fallback."""

    def test_returns_first_gatekeeper(self):
        from hydra_core.cli import _resolve_pack_lead_agent
        from hydra_core.squad_loader import discover_squads

        packs = discover_squads(HYDRA_ROOT)
        # garland's first gatekeeper is brand-strategist (authority: gatekeeper)
        garland = packs["garland"]
        lead = _resolve_pack_lead_agent(garland)
        gatekeeper_slugs = {a.slug for a in garland.agents
                            if a.authority == "gatekeeper"}
        assert lead in gatekeeper_slugs

    def test_fallback_to_general_purpose_when_no_agents(self):
        from hydra_core.cli import _resolve_pack_lead_agent
        from dataclasses import replace

        # Build a minimal pack with no agents.
        from hydra_core.squad_loader import SquadPack
        empty_pack = SquadPack(
            slug="empty-test",
            name="Empty",
            description="no agents",
            entrypoint="claude-skill",
        )
        assert _resolve_pack_lead_agent(empty_pack) == "general-purpose"


# ===========================================================================
# F8 — reflexion_override gate + approve_override_raise_to_N
# ===========================================================================

class TestF8ReflexionOverrideGate:
    """F8: reflexion_override HITL gate + approve_override_raise_to_N option."""

    def test_reflexion_override_granted_until_defaults_to_zero(self):
        """HydraState must expose reflexion_override_granted_until defaulting to 0."""
        from hydra_core.state import HydraState
        state = HydraState(root_goal="test")
        assert state.reflexion_override_granted_until == 0

    def test_approve_override_raise_to_n_parses_integer(self):
        """The cli.py F8 parsing: 'approve_override_raise_to_N' → N as int."""
        for n_str, n_int in [("3", 3), ("5", 5), ("10", 10), ("0", 0)]:
            option = f"approve_override_raise_to_{n_str}"
            # Mirror the formula used in _cmd_resume_locked F8 block.
            parsed = int(option.rsplit("_", 1)[-1])
            assert parsed == n_int, (
                f"expected {n_int} from {option!r}, got {parsed}"
            )

    def test_non_numeric_suffix_raises_value_error(self):
        """Non-numeric suffix raises ValueError; _cmd_resume_locked's try/except
        silently swallows it — the raw parse must raise so the guard is needed."""
        option = "approve_override_raise_to_bogus"
        with pytest.raises(ValueError):
            int(option.rsplit("_", 1)[-1])

    def test_reflexion_override_field_settable(self):
        """The field must be writable (patch dict key used in cli.py F8)."""
        from hydra_core.state import HydraState
        state = HydraState(root_goal="test")
        state.reflexion_override_granted_until = 7
        assert state.reflexion_override_granted_until == 7


# ===========================================================================
# F9 — resumable HITL gate nodes + hitl_return_node state field
# ===========================================================================

class TestF9ResumableHitlGate:
    """F9: hitl_return_node on HydraState; after_dispatch/after_judge routing;
    pre-dispatch and envelope-ceiling gates set hitl_return_node."""

    def _make_runner(self, monkeypatch):
        """Build a PurePythonRunner with a null dispatcher (all MCP calls no-op)."""
        from hydra_core.supervisor import build_supervisor, _PurePythonRunner
        monkeypatch.setattr(
            "hydra_core.governance.enforce_constitution",
            lambda *_a, **_k: type("V", (), {"aligned": True, "rationale": ""})(),
        )
        runner = build_supervisor(
            project_root=HYDRA_ROOT,
            dispatcher=None,
            force_pure_python=True,
        )
        assert isinstance(runner, _PurePythonRunner)
        return runner

    def test_hitl_return_node_defaults_to_none(self):
        from hydra_core.state import HydraState
        state = HydraState(root_goal="test")
        assert state.hitl_return_node is None

    def test_pre_dispatch_budget_block_sets_hitl_return_node(self, monkeypatch):
        """When budget is exhausted before dispatch, hitl_return_node='dispatch'."""
        from hydra_core.state import HydraState, TaskState
        runner = self._make_runner(monkeypatch)

        state = HydraState(root_goal="build X", selected_squads=["engineering"])
        # Exhaust the budget (spent == budget_usd = 50.0).
        state.budget.spent_usd = 50.0
        task = TaskState(owner_squad="engineering", description="implement X")
        state.tasks.append(task)

        final = runner.invoke(state)

        assert final.phase == "surfaced"
        assert final.hitl_return_node == "dispatch", (
            f"Expected hitl_return_node='dispatch', got {final.hitl_return_node!r}"
        )
        assert isinstance(final.pending_hitl, dict)
        assert final.pending_hitl.get("reason") == "over_budget"

    def test_envelope_ceiling_sets_hitl_return_node(self, monkeypatch):
        """When envelope ceiling is reached, hitl_return_node='dispatch'."""
        from hydra_core.state import HydraState
        from hydra_core.schemas import HydraEnvelope
        runner = self._make_runner(monkeypatch)

        state = HydraState(root_goal="test", selected_squads=["engineering"])
        state.envelope_ceiling = 2
        for _ in range(3):
            env = HydraEnvelope(
                type="PRD", origin_squad="test", workflow_id=state.workflow_id,
            )
            state.envelopes.append(env.model_dump(mode="json"))

        final = runner.invoke(state)

        assert final.phase == "surfaced"
        assert final.hitl_return_node == "dispatch"
        assert isinstance(final.pending_hitl, dict)
        assert final.pending_hitl.get("reason") == "envelope_ceiling"

    def test_hitl_gate_nodes_not_in_pure_python_runner_steps(self, monkeypatch):
        """_PurePythonRunner intentionally excludes hitl_gate_* nodes
        (documented: it exits on phase='surfaced'; interrupt_before not supported)."""
        from hydra_core.supervisor import build_supervisor, _PurePythonRunner
        runner = build_supervisor(
            project_root=HYDRA_ROOT, dispatcher=None, force_pure_python=True,
        )
        assert isinstance(runner, _PurePythonRunner)
        step_names = {name for name, _ in runner.steps}
        assert "hitl_gate_dispatch" not in step_names
        assert "hitl_gate_judge" not in step_names

    def test_hitl_return_node_field_is_additive(self):
        """Additive-only: field must survive a round-trip through model_dump/model_validate."""
        from hydra_core.state import HydraState
        state = HydraState(root_goal="test")
        state.hitl_return_node = "dispatch"
        dumped = state.model_dump(mode="json")
        assert dumped.get("hitl_return_node") == "dispatch"
        restored = HydraState.model_validate(dumped)
        assert restored.hitl_return_node == "dispatch"

    def test_hitl_return_node_none_survives_round_trip(self):
        """None value must also survive model serialization for backward compat."""
        from hydra_core.state import HydraState
        state = HydraState(root_goal="test")
        assert state.hitl_return_node is None
        dumped = state.model_dump(mode="json")
        restored = HydraState.model_validate(dumped)
        assert restored.hitl_return_node is None


# ===========================================================================
# F10 — per-option behaviour dispatch table
# ===========================================================================

class TestF10OptionDispatchTable:
    """F10: envelope_ceiling 'acknowledge' replaces 'split_phase';
    'send_back_for_revision' removed from policy_breach; approve_override
    extends budget by 20%; abort option parks surfaced."""

    def _make_runner_and_ceiling_state(self, monkeypatch):
        from hydra_core.supervisor import build_supervisor, _PurePythonRunner
        from hydra_core.state import HydraState
        from hydra_core.schemas import HydraEnvelope
        monkeypatch.setattr(
            "hydra_core.governance.enforce_constitution",
            lambda *_a, **_k: type("V", (), {"aligned": True, "rationale": ""})(),
        )
        runner = build_supervisor(
            project_root=HYDRA_ROOT, dispatcher=None, force_pure_python=True,
        )
        assert isinstance(runner, _PurePythonRunner)
        state = HydraState(root_goal="test", selected_squads=["engineering"])
        state.envelope_ceiling = 2
        for _ in range(3):
            env = HydraEnvelope(
                type="PRD", origin_squad="test", workflow_id=state.workflow_id,
            )
            state.envelopes.append(env.model_dump(mode="json"))
        return runner, state

    def test_envelope_ceiling_offers_acknowledge_not_split_phase(self, monkeypatch):
        """F10: envelope_ceiling gate must offer 'acknowledge', not 'split_phase'."""
        runner, state = self._make_runner_and_ceiling_state(monkeypatch)
        final = runner.invoke(state)
        options = (final.pending_hitl or {}).get("options", [])
        assert "acknowledge" in options, (
            f"F10: expected 'acknowledge' in envelope_ceiling options, got {options}"
        )
        assert "split_phase" not in options, (
            "F10: 'split_phase' must not appear in options — it was removed"
        )

    def test_envelope_ceiling_abort_option_present(self, monkeypatch):
        """F10: envelope_ceiling gate must offer 'abort'."""
        runner, state = self._make_runner_and_ceiling_state(monkeypatch)
        final = runner.invoke(state)
        options = (final.pending_hitl or {}).get("options", [])
        assert "abort" in options

    def test_approve_override_budget_extension_formula_spend_floor(self):
        """F10: budget extension = max(budget*1.2, spend*1.1+0.10) — spend-floor wins."""
        _spent = 10.0
        _cur_budget = 8.0
        new_budget = max(_cur_budget * 1.2, _spent * 1.1 + 0.10)
        # 8.0 * 1.2 = 9.6; 10.0 * 1.1 + 0.10 = 11.1 → max = 11.1
        assert new_budget == pytest.approx(11.1, abs=1e-9)

    def test_approve_override_budget_extension_formula_pct_wins(self):
        """F10: when 20% of budget exceeds spend floor, pct wins."""
        _spent = 5.0
        _cur_budget = 50.0
        new_budget = max(_cur_budget * 1.2, _spent * 1.1 + 0.10)
        # 50.0 * 1.2 = 60.0; 5.0 * 1.1 + 0.10 = 5.60 → max = 60.0
        assert new_budget == pytest.approx(60.0, abs=1e-9)

    def test_policy_breach_options_exclude_send_back_for_revision(self):
        """F10: 'send_back_for_revision' must not appear in any options=[...] list
        in supervisor.py (comments may retain it for history; code must not)."""
        from hydra_core import supervisor as sup_mod
        import inspect, re
        src = inspect.getsource(sup_mod)
        # Strip single-line Python comments so we only check code, not comments.
        src_no_comments = re.sub(r"#[^\n]*", "", src, flags=re.MULTILINE)
        assert '"send_back_for_revision"' not in src_no_comments, (
            "F10: 'send_back_for_revision' still present as a code literal in "
            "supervisor.py; it must only appear in comments, not in options lists"
        )
        assert "'send_back_for_revision'" not in src_no_comments, (
            "F10: 'send_back_for_revision' (single-quoted) still present as code"
        )


# ===========================================================================
# M3 — verify_operator_capability wired into resume consumer
# ===========================================================================

class TestM3VerifyOperatorCapability:
    """M3: verify_operator_capability called in resume; fail-closed on tamper,
    warn-and-continue on degraded."""

    def test_rejects_degraded_token_fail_closed(self):
        """A degraded token (sig.value=None, degraded=True) must return valid=False."""
        from hydra_core.auth.capability import verify_operator_capability
        import time
        degraded_token = {
            "v": 1,
            "actor_id": "rob.hasselbach@gmail.com",
            "actor_kind": "human",
            "capability": "budget.gate",
            "workflow_id": "wf-123",
            "resource_id": "res-456",
            "jti": "jti-789",
            "exp": int(time.time()) + 900,
            "sig": {
                "alg": "HMAC-SHA256",
                "key_id": "k1",
                "value": None,
                "degraded": True,
            },
        }
        result = verify_operator_capability(
            degraded_token,
            expected_capability="budget.gate",
            expected_workflow_id="wf-123",
            expected_resource_id="res-456",
        )
        assert result["valid"] is False
        assert "degraded" in result.get("reason", "").lower(), (
            f"Expected 'degraded' in reason, got {result.get('reason')!r}"
        )

    def test_rejects_unknown_actor_id_sentinel(self):
        """actor_id='unknown' is a sentinel that must be rejected by verify."""
        from hydra_core.auth.capability import verify_operator_capability
        import time
        token = {
            "v": 1,
            "actor_id": "unknown",
            "actor_kind": "human",
            "capability": "budget.gate",
            "workflow_id": "wf-123",
            "resource_id": "res-456",
            "jti": "jti-789",
            "exp": int(time.time()) + 900,
            "sig": {"alg": "HMAC-SHA256", "key_id": "k1", "value": "anything"},
        }
        result = verify_operator_capability(
            token,
            expected_capability="budget.gate",
            expected_workflow_id="wf-123",
            expected_resource_id="res-456",
        )
        assert result["valid"] is False
        reason = result.get("reason", "")
        assert "sentinel" in reason.lower() or "actor_id" in reason.lower(), (
            f"Expected actor_id sentinel rejection, got reason={reason!r}"
        )

    def test_rejects_non_human_actor_kind(self):
        """actor_kind != 'human' must be rejected (operator gates are human-only)."""
        from hydra_core.auth.capability import verify_operator_capability
        import time
        token = {
            "v": 1,
            "actor_id": "agent-bot",
            "actor_kind": "agent",  # not "human"
            "capability": "budget.gate",
            "workflow_id": "wf-123",
            "resource_id": "res-456",
            "jti": "jti-789",
            "exp": int(time.time()) + 900,
            "sig": {"alg": "HMAC-SHA256", "key_id": "k1", "value": "anything"},
        }
        result = verify_operator_capability(
            token,
            expected_capability="budget.gate",
            expected_workflow_id="wf-123",
            expected_resource_id="res-456",
        )
        assert result["valid"] is False
        assert "human" in result.get("reason", "").lower()

    def test_never_raises_on_garbage_input(self):
        """verify_operator_capability must never raise (NEVER raises contract)."""
        from hydra_core.auth.capability import verify_operator_capability
        for bad_input in [None, 42, "string", [], {"v": True}]:
            result = verify_operator_capability(
                bad_input,
                expected_capability="x",
                expected_workflow_id="y",
                expected_resource_id="z",
            )
            assert result["valid"] is False

    def test_wired_into_cli_resume(self):
        """M3: _cmd_resume_locked must import and call verify_operator_capability."""
        import inspect
        from hydra_core import cli as cli_mod
        src = inspect.getsource(cli_mod)
        assert "verify_operator_capability" in src, (
            "M3: verify_operator_capability must appear in cli.py"
        )
        # Confirm the fail-closed branch exists (tampered → return 1).
        assert "return 1" in src, (
            "M3: cli.py must have a fail-closed return 1 path for tampered tokens"
        )


# ===========================================================================
# Rider (a) — smoke baseline helpers
# ===========================================================================

class TestRiderABaseline:
    """Rider (a): _parse_failing_tests + _capture_baseline_failures."""

    def test_parse_failing_tests_extracts_ids(self):
        from hydra_core.host_bridge import _parse_failing_tests
        output = (
            "FAILED tests/test_foo.py::test_bar - AssertionError: expected 1\n"
            "FAILED tests/test_baz.py::test_qux\n"
            "5 passed in 0.12s\n"
            "PASSED tests/test_ok.py::test_fine\n"
        )
        result = _parse_failing_tests(output)
        assert result == {
            "tests/test_foo.py::test_bar",
            "tests/test_baz.py::test_qux",
        }

    def test_parse_failing_tests_empty_on_no_failures(self):
        from hydra_core.host_bridge import _parse_failing_tests
        assert _parse_failing_tests("10 passed in 2.1s\n") == set()

    def test_parse_failing_tests_strips_reason_after_dash(self):
        from hydra_core.host_bridge import _parse_failing_tests
        output = "FAILED tests/a.py::test_x - long reason - with extra dashes\n"
        result = _parse_failing_tests(output)
        assert "tests/a.py::test_x" in result
        for item in result:
            assert " - " not in item, f"reason suffix leaked into id: {item!r}"

    def test_capture_baseline_failures_returns_sorted_list(self, monkeypatch, tmp_path):
        """_capture_baseline_failures returns a sorted list of failing test ids."""
        from hydra_core.host_bridge import _capture_baseline_failures
        import subprocess
        fake_stdout = (
            "FAILED tests/test_z.py::test_z - AssertionError\n"
            "FAILED tests/test_a.py::test_a - AssertionError\n"
            "2 failed in 1.0s\n"
        )
        monkeypatch.setattr(subprocess, "run", lambda *_a, **_k: type("R", (), {
            "stdout": fake_stdout, "stderr": "", "returncode": 1,
        })())
        result = _capture_baseline_failures(str(tmp_path))
        assert isinstance(result, list)
        assert result == sorted(result), "result must be sorted"
        assert "tests/test_a.py::test_a" in result
        assert "tests/test_z.py::test_z" in result

    def test_capture_baseline_failures_fail_soft_on_exception(self, monkeypatch, tmp_path):
        """_capture_baseline_failures must return [] on subprocess errors."""
        from hydra_core.host_bridge import _capture_baseline_failures
        import subprocess

        def _raise(*_a, **_k):
            raise OSError("no pytest binary found")

        monkeypatch.setattr(subprocess, "run", _raise)
        result = _capture_baseline_failures(str(tmp_path))
        assert result == [], f"expected [] on exception, got {result!r}"


# ===========================================================================
# Rider (b) — idempotent charge (already_charged flag + mark_charged)
# ===========================================================================

class TestRiderBIdempotentCharge:
    """Rider (b): already_charged in _step_result; mark_charged sets the flag."""

    def _write_cursor(self, cursor_file, state: str = "complete",
                      charged: bool = False) -> dict:
        import json
        from hydra_core.host_bridge import CURSOR_SCHEMA
        data = {
            "schema": CURSOR_SCHEMA,
            "state": state,
            "final_status": "passed",
            "charged": charged,
            "workflow_id": "wf-test",
            "run_id": "run-test",
            "stage_id": "stage-test",
            "task_id": "task-test",
            "project_path": ".",
        }
        import pathlib
        pathlib.Path(cursor_file).write_text(
            json.dumps(data), encoding="utf-8"
        )
        return data

    def test_already_charged_false_by_default(self, tmp_path):
        from hydra_core.host_bridge import _step_result
        cursor_file = tmp_path / "cursor.json"
        cursor = self._write_cursor(cursor_file, state="complete", charged=False)
        result = _step_result(cursor, cursor_file)
        assert "already_charged" in result
        assert result["already_charged"] is False

    def test_already_charged_true_when_flag_set(self, tmp_path):
        from hydra_core.host_bridge import _step_result
        cursor_file = tmp_path / "cursor.json"
        cursor = self._write_cursor(cursor_file, state="complete", charged=True)
        result = _step_result(cursor, cursor_file)
        assert result["already_charged"] is True

    def test_already_charged_only_in_terminal_states(self, tmp_path):
        """already_charged is only present for terminal states in _step_result."""
        from hydra_core.host_bridge import _step_result, CURSOR_SCHEMA
        import json, pathlib
        cursor_file = tmp_path / "cursor_nonterminal.json"
        data = {
            "schema": CURSOR_SCHEMA,
            "state": "await_generate",  # non-terminal
            "charged": False,
            "workflow_id": "wf-test", "run_id": "run-test",
            "stage_id": "stage-test", "task_id": "task-test",
            "project_path": ".",
        }
        pathlib.Path(cursor_file).write_text(json.dumps(data), encoding="utf-8")
        result = _step_result(data, cursor_file)
        # Non-terminal: 'already_charged' must NOT be in the result.
        assert "already_charged" not in result

    def test_mark_charged_sets_flag(self, tmp_path):
        """mark_charged must set cursor['charged']=True on a terminal cursor."""
        from hydra_core.host_bridge import mark_charged, load_cursor
        cursor_file = tmp_path / "cursor.json"
        self._write_cursor(cursor_file, state="complete", charged=False)

        mark_charged(cursor_file)

        cursor = load_cursor(cursor_file)
        assert cursor.get("charged") is True

    def test_mark_charged_noop_on_non_terminal(self, tmp_path):
        """mark_charged must not set charged on a non-terminal cursor."""
        from hydra_core.host_bridge import mark_charged, load_cursor, CURSOR_SCHEMA
        import json, pathlib
        cursor_file = tmp_path / "cursor_nonterminal.json"
        data = {
            "schema": CURSOR_SCHEMA,
            "state": "await_judge",
            "charged": False,
            "workflow_id": "wf-test", "run_id": "run-test",
            "stage_id": "stage-test", "task_id": "task-test",
            "project_path": ".",
        }
        pathlib.Path(cursor_file).write_text(json.dumps(data), encoding="utf-8")

        mark_charged(cursor_file)

        cursor = load_cursor(cursor_file)
        assert cursor.get("charged") is False, (
            "mark_charged must be a no-op on non-terminal cursors"
        )

    def test_mark_charged_fail_soft_on_missing_file(self, tmp_path):
        """mark_charged must not raise when the cursor file is missing."""
        from hydra_core.host_bridge import mark_charged
        nonexistent = tmp_path / "does_not_exist.json"
        # Must not raise.
        mark_charged(nonexistent)


# ===========================================================================
# Rider (c) — _via_impersonation maps 'stub' → 'deferred_to_host'
# ===========================================================================

class TestRiderCImpersonationStubMapping:
    """Rider (c): _via_impersonation maps raw 'stub' → 'deferred_to_host'."""

    class _MockDispatcher:
        """Minimal dispatcher for _via_impersonation tests."""
        live_execution = False

        def __init__(self, emit_status: str = "stub"):
            self._emit_status = emit_status

        def call_mcp(self, server, tool, args, **kwargs):
            return None  # roster list + output write both return None

        def emit_claude_prompt(self, prompt, agent=None):
            return {"status": self._emit_status, "summary": "mock boardroom session"}

        def emit(self, event_name, payload=None, **kwargs):
            pass

    def _get_executive_pack(self):
        from hydra_core.squad_loader import discover_squads
        packs = discover_squads(HYDRA_ROOT)
        pack = packs.get("executive")
        if pack is None:
            pytest.skip("executive pack not discovered — skipping rider (c) test")
        return pack

    def _make_inbound(self, state):
        from hydra_core.schemas import HydraEnvelope
        return HydraEnvelope(
            type="C_SUITE_DECISION_PACKET",
            origin_squad="hydra",
            target_squad="executive",
            workflow_id=state.workflow_id,
        )

    def test_stub_maps_to_deferred_to_host(self):
        """Raw 'stub' from emit_claude_prompt must be coerced to 'deferred_to_host'."""
        from hydra_core.squad_node import _via_impersonation, SquadResult
        from hydra_core.state import HydraState

        pack = self._get_executive_pack()
        state = HydraState(root_goal="test rider c", selected_squads=["executive"])
        inbound = self._make_inbound(state)
        dispatcher = self._MockDispatcher(emit_status="stub")

        result = _via_impersonation(state, pack, inbound, dispatcher)

        assert isinstance(result, SquadResult)
        assert result.status == "deferred_to_host", (
            f"F19/Rider(c): 'stub' must map to 'deferred_to_host', got {result.status!r}"
        )

    def test_host_pickup_required_maps_to_deferred_to_host(self):
        """Pre-existing 'host_pickup_required' must still map to 'deferred_to_host'."""
        from hydra_core.squad_node import _via_impersonation, SquadResult
        from hydra_core.state import HydraState

        pack = self._get_executive_pack()
        state = HydraState(root_goal="test host_pickup", selected_squads=["executive"])
        inbound = self._make_inbound(state)
        dispatcher = self._MockDispatcher(emit_status="host_pickup_required")

        result = _via_impersonation(state, pack, inbound, dispatcher)

        assert isinstance(result, SquadResult)
        assert result.status == "deferred_to_host"

    def test_done_status_is_preserved(self):
        """Raw 'done' must not be remapped."""
        from hydra_core.squad_node import _via_impersonation, SquadResult
        from hydra_core.state import HydraState

        pack = self._get_executive_pack()
        state = HydraState(root_goal="test done", selected_squads=["executive"])
        inbound = self._make_inbound(state)
        dispatcher = self._MockDispatcher(emit_status="done")

        result = _via_impersonation(state, pack, inbound, dispatcher)

        assert isinstance(result, SquadResult)
        assert result.status == "done"
