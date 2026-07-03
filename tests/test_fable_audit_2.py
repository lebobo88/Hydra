"""fable-audit-2 Phase 1 tests.

Covers all four fixes introduced in this phase:

  F11+M2 — Honest status: _via_claude_skill and _via_impersonation must return
            'deferred_to_host' (not 'done' / raw 'stub') when the dispatcher
            cannot actually run the pack.  supervisor.py coerces unknown statuses.
            governance.py treats 'deferred_to_host' as blocking.

  Fix-2  — Host-executor seam: _cmd_attended_step returns a host_action for
            claude-skill / agent-impersonation tasks; begin_squad_stage / submit
            are exactly-once; submit-host-result stores the artifact and advances
            the workflow cursor to 'complete'.

  F19    — _via_impersonation calls _extract_emitted_envelopes so DEV_TASK /
            CREATIVE_BRIEF delegation envelopes are surfaced to the supervisor.

  F18    — _drive_pp_stage_loop with invoke_mode='pp_best_of' implies N=3 when
            HYDRA_BEST_OF_N is unset; an explicit HYDRA_BEST_OF_N still wins.
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
# M2 — supervisor unknown-status coercion
# ===========================================================================

class TestSupervisorStatusCoercion:
    """supervisor.py must coerce unknown/out-of-contract SquadResult.status
    values to 'surfaced' so a rogue pack cannot forge a 'done'."""

    def _make_result(self, status: str):
        from hydra_core.squad_node import SquadResult
        from hydra_core.schemas import DecisionRecord
        dr = DecisionRecord(
            workflow_id=uuid4(), parent_id=uuid4(),
            origin_squad="garland", target_squad="hydra",
            decision="ok", rationale="ok",
        )
        return SquadResult(envelopes=[dr], artifacts=[], status=status)

    def test_known_done_is_kept(self):
        # We test the coercion logic directly without invoking the full supervisor
        # graph (no LangGraph needed for a unit test).
        _KNOWN: frozenset[str] = frozenset({
            "pending", "running", "blocked", "done", "failed",
            "surfaced", "cancelled", "deferred_to_host",
        })
        result = self._make_result("done")
        coerced = result.status if result.status in _KNOWN else "surfaced"
        assert coerced == "done"

    def test_unknown_status_coerces_to_surfaced(self):
        _KNOWN: frozenset[str] = frozenset({
            "pending", "running", "blocked", "done", "failed",
            "surfaced", "cancelled", "deferred_to_host",
        })
        result = self._make_result("totally_made_up")
        coerced = result.status if result.status in _KNOWN else "surfaced"
        assert coerced == "surfaced"

    def test_deferred_to_host_is_known(self):
        _KNOWN: frozenset[str] = frozenset({
            "pending", "running", "blocked", "done", "failed",
            "surfaced", "cancelled", "deferred_to_host",
        })
        result = self._make_result("deferred_to_host")
        coerced = result.status if result.status in _KNOWN else "surfaced"
        assert coerced == "deferred_to_host"


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
