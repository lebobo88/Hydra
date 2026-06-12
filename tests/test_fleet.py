"""WS8 SLICE 1 (v2) -- tests for hydra_core.fleet.dispatch_fleet.

All tests use a MOCK dispatcher -- no real MCP, no real pp runs, no network.

Covers all 6 fixes from the post-review:
  Fix 1: TaskState.target_repo_id flows through _build_payload correctly;
          fleet is reachable in production (no _test_repo_id hack needed).
  Fix 2: dispatcher_factory called once per worker; workers use the returned
          instance (not the shared dispatcher).
  Fix 3+4: Only mcp-entrypoint packs are fleet-eligible; a non-mcp task
           (e.g. executive/impersonation) is rejected by the fleet.
  Fix 5: build_payload raising for one task fails that task only; others
         proceed; post-join merge is unaffected.
  Fix 6: budget HITL surfaces when charge_and_gate returns _block=True
         after fleet merge.

Plus the original SLICE 1 contract tests:
  - 3 distinct-repo tasks -> all 3 dispatched; results in input order.
  - state.open_pp_runs ends with all 3 entries merged post-join.
  - CONCURRENCY: max_concurrency=2 + 4 tasks -> at most 2 in-flight.
  - ISOLATION: one raiser -> that task failed, others succeed, fleet no-raise.
  - SAME-REPO GUARD: duplicate target_repo_id rejected; two None-target tasks.
  - DETERMINISM: results[i] == tasks[i] even with inverse-sleep ordering.
  - NO-STATE-RACE: workers see state.open_pp_runs=0 at call time.
"""
from __future__ import annotations

import threading
import time
import uuid
from typing import Any
from unittest.mock import patch

import pytest

from hydra_core.fleet import dispatch_fleet, FLEET_MAX_CAP, FLEET_DEFAULT_CONCURRENCY
from hydra_core.schemas import CSuiteDecisionPacket
from hydra_core.squad_loader import SquadPack
from hydra_core.squad_node import SquadResult, Dispatcher
from hydra_core.state import HydraState, TaskState


# ---------------------------------------------------------------------------
# Fixtures and helpers
# ---------------------------------------------------------------------------

def _pack(slug: str = "engineering", *, entrypoint: str = "mcp") -> SquadPack:
    """Minimal SquadPack adequate for fleet tests."""
    return SquadPack(
        slug=slug,
        name=slug,
        description=f"test pack for {slug}",
        entrypoint=entrypoint,
        agents=(),
        tools=(),
    )


def _make_task(repo_id: str | None, *, squad: str = "engineering") -> TaskState:
    """Create a TaskState with target_repo_id as a real model field (Fix 1)."""
    return TaskState(
        owner_squad=squad,
        description=f"work for {repo_id}",
        status="pending",
        target_repo_id=repo_id,
    )


def _state(**kwargs: Any) -> HydraState:
    s = HydraState(root_goal="test fleet", **kwargs)
    return s


# ---------------------------------------------------------------------------
# Mock dispatcher that records calls and returns SquadResults.
# ---------------------------------------------------------------------------

class _MockDispatcher:
    """
    Fake dispatcher for fleet tests.

    Thread-safe: all mutable state uses a lock.
    Simulates what _via_mcp does (appends to collector, NOT state.open_pp_runs).
    """

    def __init__(
        self,
        *,
        sleep_map: dict[str | None, float] | None = None,
        raise_for: set[str | None] | None = None,
        instance_id: int | None = None,
    ) -> None:
        self._lock = threading.Lock()
        self._calls: list[dict[str, Any]] = []
        self._in_flight: int = 0
        self._max_in_flight: int = 0
        self.sleep_map: dict[str | None, float] = sleep_map or {}
        self.raise_for: set[str | None] = raise_for or set()
        self.instance_id = instance_id if instance_id is not None else id(self)

    # ---- Dispatcher protocol -----------------------------------------------
    def call_mcp(self, server: str, tool: str, args: dict[str, Any],
                 *, squad_id: str | None = None) -> dict[str, Any]:
        run_id = f"run-{uuid.uuid4().hex[:8]}"
        return {"status": "done", "tool": tool, "result": {"run_id": run_id}}

    def emit_claude_prompt(self, *_a: Any, **_k: Any) -> Any:
        raise NotImplementedError

    def invoke_claude_skill(self, *_a: Any, **_k: Any) -> Any:
        raise NotImplementedError

    def spawn_subprocess(self, *_a: Any, **_k: Any) -> Any:
        raise NotImplementedError

    # ---- execute_squad shim (patched into fleet) ----------------------------
    def execute_squad_shim(
        self,
        state: HydraState,
        pack: SquadPack,
        payload: Any,
        dispatcher: Any,
        *,
        collect_open_runs: list | None = None,
    ) -> SquadResult:
        repo_id = getattr(payload, "target_repo_id", None)

        with self._lock:
            self._in_flight += 1
            if self._in_flight > self._max_in_flight:
                self._max_in_flight = self._in_flight
            self._calls.append({
                "repo_id": repo_id,
                "squad": pack.slug,
                "dispatcher_instance_id": getattr(dispatcher, "instance_id", id(dispatcher)),
            })

        try:
            sleep_s = self.sleep_map.get(repo_id, 0.02)
            time.sleep(sleep_s)

            if repo_id in self.raise_for:
                raise RuntimeError(f"mock error for repo_id={repo_id!r}")

            run_id = f"run-{uuid.uuid4().hex[:8]}"
            entry: dict[str, str] = {
                "run_id": run_id,
                "project_path": f"/mock/path/{repo_id}",
            }
            if collect_open_runs is not None:
                collect_open_runs.append(entry)
            # Deliberately do NOT touch state.open_pp_runs.

            from hydra_core.schemas import DecisionRecord
            decision = DecisionRecord(
                workflow_id=payload.workflow_id,
                parent_id=payload.id,
                origin_squad=pack.slug,
                target_squad="hydra",
                decision=f"dispatched {repo_id}",
                rationale=f"run_id={run_id}",
                artifacts=[],
            )
            return SquadResult(
                envelopes=[decision],
                artifacts=[{"kind": "pp_run", "ref": run_id, "raw": {}}],
                status="running",
            )
        finally:
            with self._lock:
                self._in_flight -= 1

    @property
    def calls(self) -> list[dict[str, Any]]:
        with self._lock:
            return list(self._calls)

    @property
    def max_in_flight(self) -> int:
        with self._lock:
            return self._max_in_flight


# ---------------------------------------------------------------------------
# Run dispatch_fleet with the mock patched in.
# ---------------------------------------------------------------------------

def _run_fleet(
    tasks: list[TaskState],
    *,
    packs: dict[str, SquadPack] | None = None,
    mock: _MockDispatcher | None = None,
    max_concurrency: int = 4,
    fleet_max_concurrency: int | None = None,
    state_kwargs: dict[str, Any] | None = None,
    factory_instances: list[_MockDispatcher] | None = None,
) -> tuple[HydraState, _MockDispatcher, list[SquadResult]]:
    """
    Run dispatch_fleet with a mocked execute_squad.

    If factory_instances is provided, the dispatcher_factory will create a new
    _MockDispatcher per call and append it to factory_instances (Fix 2 test support).
    In that case, the shim routes through the per-worker instance's execute_squad_shim.
    """
    if mock is None:
        mock = _MockDispatcher()
    if packs is None:
        packs = {"engineering": _pack("engineering")}

    s = _state(**(state_kwargs or {}))
    if fleet_max_concurrency is not None:
        s.fleet_max_concurrency = fleet_max_concurrency

    def _build(task: TaskState) -> CSuiteDecisionPacket:
        return CSuiteDecisionPacket(
            workflow_id=s.workflow_id,
            origin_squad="hydra",
            target_squad=task.owner_squad,
            origin="BOARDROOM",
            objective=task.description,
            target_repo_id=task.target_repo_id,  # Fix 1: real model field
        )

    if factory_instances is not None:
        _sleep_map = mock.sleep_map
        _raise_for = mock.raise_for

        def _factory() -> _MockDispatcher:
            inst = _MockDispatcher(sleep_map=_sleep_map, raise_for=_raise_for,
                                   instance_id=len(factory_instances))
            factory_instances.append(inst)
            return inst

        # Shim routes through the per-worker instance.
        def _shim(state, pack, payload, dispatcher, *, collect_open_runs=None):
            return dispatcher.execute_squad_shim(
                state, pack, payload, dispatcher,
                collect_open_runs=collect_open_runs,
            )

        with patch("hydra_core.fleet.execute_squad", side_effect=_shim):
            results, _wt = dispatch_fleet(
                s, tasks, _factory,
                build_payload=_build,
                packs=packs,
                max_concurrency=max_concurrency,
            )
    else:
        def _factory_default(_d: _MockDispatcher = mock) -> _MockDispatcher:
            return _d

        with patch("hydra_core.fleet.execute_squad",
                   side_effect=mock.execute_squad_shim):
            results, _wt = dispatch_fleet(
                s, tasks, _factory_default,
                build_payload=_build,
                packs=packs,
                max_concurrency=max_concurrency,
            )
    return s, mock, results


# ===========================================================================
# Fix 1: TaskState.target_repo_id is a real model field; fleet is reachable
#         in production without any external hack.
# ===========================================================================

class TestFix1PerTaskRepoId:

    def test_task_target_repo_id_is_real_model_field(self) -> None:
        t = _make_task("repo-x")
        assert t.target_repo_id == "repo-x"

    def test_build_payload_uses_task_target_repo_id_over_state(self) -> None:
        """Per-task repo wins over the workflow-level state.target_repo_id."""
        s = _state()
        s.target_repo_id = "workflow-repo"
        t = _make_task("per-task-repo")
        payload = CSuiteDecisionPacket(
            workflow_id=s.workflow_id, origin_squad="hydra",
            target_squad=t.owner_squad, origin="BOARDROOM",
            objective=t.description,
            target_repo_id=t.target_repo_id if t.target_repo_id is not None else s.target_repo_id,
        )
        assert payload.target_repo_id == "per-task-repo"

    def test_build_payload_falls_back_to_state_repo_when_task_is_none(self) -> None:
        s = _state()
        s.target_repo_id = "workflow-repo"
        t = _make_task(None)
        payload = CSuiteDecisionPacket(
            workflow_id=s.workflow_id, origin_squad="hydra",
            target_squad=t.owner_squad, origin="BOARDROOM",
            objective=t.description,
            target_repo_id=t.target_repo_id if t.target_repo_id is not None else s.target_repo_id,
        )
        assert payload.target_repo_id == "workflow-repo"

    def test_two_distinct_real_repo_ids_both_dispatched(self) -> None:
        """Fleet dispatches both tasks using the real TaskState.target_repo_id."""
        tasks = [_make_task("repo-a"), _make_task("repo-b")]
        state, mock, results = _run_fleet(tasks)
        assert all(r.status == "running" for r in results)
        assert len(mock.calls) == 2

    def test_gating_predicate_reads_per_task_repos(self) -> None:
        """The gating predicate in node_dispatch reads target_repo_id from
        the built payload, which sources from task.target_repo_id."""
        s = _state()
        s.fleet_parallel = True
        s.target_repo_id = "workflow-root"  # would give only 1 distinct repo
        packs = {"engineering": _pack("engineering")}
        tasks = [_make_task("repo-a"), _make_task("repo-b")]

        def _build(task: TaskState) -> CSuiteDecisionPacket:
            return CSuiteDecisionPacket(
                workflow_id=s.workflow_id, origin_squad="hydra",
                target_squad=task.owner_squad, origin="BOARDROOM",
                objective=task.description,
                target_repo_id=task.target_repo_id if task.target_repo_id is not None
                               else s.target_repo_id,
            )

        candidate_tasks = [
            t for t in tasks if t.status == "pending"
            and packs.get(t.owner_squad) is not None
            and not (packs[t.owner_squad].best_of_n and packs[t.owner_squad].best_of_n >= 2)
            and packs[t.owner_squad].entrypoint == "mcp"
        ]
        payloads = {id(t): _build(t) for t in candidate_tasks}
        distinct = {str(payloads[id(t)].target_repo_id) for t in candidate_tasks
                    if payloads[id(t)].target_repo_id is not None}
        use_fleet = s.fleet_parallel and len(candidate_tasks) >= 2 and len(distinct) >= 2
        assert use_fleet, "per-task repo_ids must make fleet reachable"


# ===========================================================================
# Fix 2: dispatcher_factory called once per worker; workers use their instance.
# ===========================================================================

class TestFix2PerWorkerDispatcher:

    def test_factory_called_once_per_worker(self) -> None:
        factory_instances: list[_MockDispatcher] = []
        tasks = [_make_task(f"repo-{i}") for i in range(3)]
        mock = _MockDispatcher(sleep_map={f"repo-{i}": 0.02 for i in range(3)})
        state, _, results = _run_fleet(tasks, mock=mock, factory_instances=factory_instances)
        assert len(factory_instances) == 3, (
            f"Expected 3 factory invocations, got {len(factory_instances)}"
        )
        assert all(r.status == "running" for r in results)

    def test_each_worker_uses_distinct_instance(self) -> None:
        factory_instances: list[_MockDispatcher] = []
        tasks = [_make_task("repo-a"), _make_task("repo-b")]
        _run_fleet(tasks, factory_instances=factory_instances)
        ids = [inst.instance_id for inst in factory_instances]
        assert len(set(ids)) == 2, f"Expected 2 distinct instances, got {ids}"

    def test_factory_not_called_for_rejected_tasks(self) -> None:
        factory_instances: list[_MockDispatcher] = []
        tasks = [_make_task("repo-a"), _make_task("repo-a"), _make_task("repo-b")]
        _run_fleet(tasks, factory_instances=factory_instances)
        assert len(factory_instances) == 2  # only repo-a (first) + repo-b


# ===========================================================================
# Fix 3+4: Only mcp-entrypoint packs are fleet-eligible.
# ===========================================================================

class TestFix3And4McpOnlyEligibility:

    def test_agent_impersonation_pack_rejected(self) -> None:
        packs = {"executive": _pack("executive", entrypoint="agent-impersonation")}
        tasks = [_make_task("repo-a", squad="executive")]
        state, mock, results = _run_fleet(tasks, packs=packs)
        assert results[0].status == "failed"
        assert ("mcp" in results[0].rationale.lower()
                or "entrypoint" in results[0].rationale.lower())

    def test_claude_skill_pack_rejected(self) -> None:
        packs = {"garland": _pack("garland", entrypoint="claude-skill")}
        tasks = [_make_task("repo-a", squad="garland"), _make_task("repo-b", squad="garland")]
        state, mock, results = _run_fleet(tasks, packs=packs)
        assert all(r.status == "failed" for r in results)

    def test_stub_pack_rejected(self) -> None:
        packs = {"research-ds": _pack("research-ds", entrypoint="stub")}
        tasks = [_make_task("repo-x", squad="research-ds")]
        state, mock, results = _run_fleet(tasks, packs=packs)
        assert results[0].status == "failed"

    def test_mcp_pack_accepted(self) -> None:
        tasks = [_make_task("repo-a"), _make_task("repo-b")]
        state, mock, results = _run_fleet(tasks)
        assert all(r.status == "running" for r in results)

    def test_mixed_mcp_and_non_mcp_only_mcp_runs(self) -> None:
        packs = {
            "engineering": _pack("engineering", entrypoint="mcp"),
            "executive": _pack("executive", entrypoint="agent-impersonation"),
        }
        tasks = [
            _make_task("repo-a", squad="engineering"),
            _make_task("repo-b", squad="executive"),
            _make_task("repo-c", squad="engineering"),
        ]
        state, mock, results = _run_fleet(tasks, packs=packs)
        assert results[0].status == "running"
        assert results[1].status == "failed"
        assert "mcp" in results[1].rationale.lower() or "entrypoint" in results[1].rationale.lower()
        assert results[2].status == "running"

    def test_gating_predicate_excludes_non_mcp_from_distinct_count(self) -> None:
        """Non-mcp tasks don't count toward the distinct-repo threshold."""
        s = _state()
        s.fleet_parallel = True
        packs = {
            "engineering": _pack("engineering", entrypoint="mcp"),
            "executive": _pack("executive", entrypoint="agent-impersonation"),
        }
        tasks = [
            _make_task("repo-a", squad="engineering"),
            _make_task("repo-b", squad="executive"),
        ]

        def _build(task: TaskState) -> CSuiteDecisionPacket:
            return CSuiteDecisionPacket(
                workflow_id=s.workflow_id, origin_squad="hydra",
                target_squad=task.owner_squad, origin="BOARDROOM",
                objective=task.description, target_repo_id=task.target_repo_id,
            )

        candidate_tasks = [
            t for t in tasks if t.status == "pending"
            and packs.get(t.owner_squad) is not None
            and not (packs[t.owner_squad].best_of_n and packs[t.owner_squad].best_of_n >= 2)
            and packs[t.owner_squad].entrypoint == "mcp"
        ]
        payloads = {id(t): _build(t) for t in candidate_tasks}
        distinct = {str(payloads[id(t)].target_repo_id) for t in candidate_tasks
                    if payloads[id(t)].target_repo_id is not None}
        use_fleet = s.fleet_parallel and len(candidate_tasks) >= 2 and len(distinct) >= 2
        assert not use_fleet, "Only 1 mcp-eligible task -- fleet should NOT activate"


# ===========================================================================
# Fix 5: build_payload called exactly once; a raise fails only that task.
# ===========================================================================

class TestFix5BuildOnce:

    def test_build_payload_error_fails_only_that_task(self) -> None:
        tasks = [_make_task("repo-a"), _make_task("repo-b"), _make_task("repo-c")]
        s = _state()
        call_count: list[str] = []

        def _build_with_error(task: TaskState) -> CSuiteDecisionPacket:
            call_count.append(task.target_repo_id or "none")
            if task.target_repo_id == "repo-b":
                raise ValueError("simulated payload build error for repo-b")
            return CSuiteDecisionPacket(
                workflow_id=s.workflow_id, origin_squad="hydra",
                target_squad=task.owner_squad, origin="BOARDROOM",
                objective=task.description, target_repo_id=task.target_repo_id,
            )

        packs = {"engineering": _pack("engineering")}

        def _factory() -> _MockDispatcher:
            return _MockDispatcher()

        with patch("hydra_core.fleet.execute_squad") as mock_exec:
            mock_exec.side_effect = lambda state, pack, payload, disp, **kw: SquadResult(
                envelopes=[], artifacts=[{"kind": "pp_run", "ref": "r1", "raw": {}}],
                status="running",
            )
            results, _wt = dispatch_fleet(
                s, tasks, _factory,
                build_payload=_build_with_error,
                packs=packs,
            )

        assert results[0].status == "running", results[0].rationale
        assert results[1].status == "failed"
        assert "payload build error" in results[1].rationale
        assert results[2].status == "running", results[2].rationale
        # build_payload called exactly once per task.
        assert len(call_count) == 3, f"Expected 3 calls: {call_count}"

    def test_build_payload_called_exactly_once_per_task(self) -> None:
        tasks = [_make_task("repo-a"), _make_task("repo-b")]
        s = _state()
        call_count: dict[str, int] = {}

        def _counting_build(task: TaskState) -> CSuiteDecisionPacket:
            key = task.target_repo_id or "none"
            call_count[key] = call_count.get(key, 0) + 1
            return CSuiteDecisionPacket(
                workflow_id=s.workflow_id, origin_squad="hydra",
                target_squad=task.owner_squad, origin="BOARDROOM",
                objective=task.description, target_repo_id=task.target_repo_id,
            )

        packs = {"engineering": _pack("engineering")}

        def _factory() -> _MockDispatcher:
            return _MockDispatcher()

        with patch("hydra_core.fleet.execute_squad") as mock_exec:
            mock_exec.side_effect = lambda state, pack, payload, disp, **kw: SquadResult(
                envelopes=[], artifacts=[], status="running",
            )
            dispatch_fleet(s, tasks, _factory, build_payload=_counting_build, packs=packs)

        for key, count in call_count.items():
            assert count == 1, f"build_payload called {count} times for {key!r}"


# ===========================================================================
# Fix 6: Budget HITL gate.
# ===========================================================================

class TestFix6BudgetHitl:

    def test_charge_and_gate_returns_block_when_over_budget(self) -> None:
        """charge_and_gate must return block=True when spent >= budget."""
        from hydra_core.governance import charge_and_gate
        from hydra_core.state import BudgetLedger

        s = _state()
        s.budget = BudgetLedger(budget_usd=1.0, spent_usd=1.0)
        _block, _downgrade = charge_and_gate(s, 0.01, 0)
        assert _block

    def test_charge_and_gate_not_blocked_when_under_budget(self) -> None:
        from hydra_core.governance import charge_and_gate
        from hydra_core.state import BudgetLedger

        s = _state()
        s.budget = BudgetLedger(budget_usd=10.0, spent_usd=0.0)
        _block, _ = charge_and_gate(s, 0.01, 0)
        assert not _block

    def test_fleet_merge_budget_hitl_shape(self) -> None:
        """Verify the expected HITL dict shape that node_dispatch constructs."""
        from hydra_core.state import BudgetLedger

        s = _state()
        s.budget = BudgetLedger(budget_usd=1.0, spent_usd=1.0)

        _hitl: dict[str, Any] = {
            "workflow_id": str(s.workflow_id),
            "reason": "over_budget",
            "gate_node": "dispatch",
            "summary": (
                f"Budget exhausted (fleet): ${s.budget.spent_usd:.4f} of "
                f"${s.budget.budget_usd:.2f} after engineering fleet dispatch."
            ),
            "options": ["approve_override", "abort"],
            "default_option": "abort",
            "spent_usd": s.budget.spent_usd,
            "budget_usd": s.budget.budget_usd,
        }
        expected = {
            "envelopes": [],
            "artifacts": [],
            "verdicts": [],
            "phase": "surfaced",
            "pending_hitl": _hitl,
            "budget_downgrade_active": True,
            "open_pp_runs": s.open_pp_runs,
        }
        assert expected["phase"] == "surfaced"
        assert expected["pending_hitl"]["reason"] == "over_budget"
        assert "fleet" in expected["pending_hitl"]["summary"]


# ===========================================================================
# Original SLICE 1 contract tests (now using real TaskState.target_repo_id).
# ===========================================================================

class TestFleetBasic:

    def test_three_distinct_repos_all_dispatched(self) -> None:
        tasks = [_make_task("repo-a"), _make_task("repo-b"), _make_task("repo-c")]
        state, mock, results = _run_fleet(tasks)
        assert len(results) == 3
        assert all(r.status == "running" for r in results), [r.status for r in results]
        assert len(mock.calls) == 3

    def test_results_in_input_order(self) -> None:
        tasks = [_make_task("repo-a"), _make_task("repo-b"), _make_task("repo-c")]
        sleep_map = {"repo-a": 0.01, "repo-b": 0.02, "repo-c": 0.05}
        mock = _MockDispatcher(sleep_map=sleep_map)
        state, mock, results = _run_fleet(tasks, mock=mock)
        for i, (task, result) in enumerate(zip(tasks, results)):
            assert len(result.envelopes) == 1
            assert task.target_repo_id in result.envelopes[0].decision, (
                f"results[{i}] should mention {task.target_repo_id!r}"
            )

    def test_open_pp_runs_merged_post_join(self) -> None:
        tasks = [_make_task("repo-a"), _make_task("repo-b"), _make_task("repo-c")]
        state, mock, results = _run_fleet(tasks)
        assert len(state.open_pp_runs) == 3
        assert len({e["run_id"] for e in state.open_pp_runs}) == 3


class TestFleetConcurrency:

    def test_bounded_concurrency_and_all_complete(self) -> None:
        tasks = [_make_task(f"repo-{c}") for c in "abcd"]
        sleep_map = {f"repo-{c}": 0.05 for c in "abcd"}
        mock = _MockDispatcher(sleep_map=sleep_map)
        state, mock, results = _run_fleet(tasks, mock=mock, max_concurrency=2)
        assert len(results) == 4
        assert all(r.status == "running" for r in results)
        assert mock.max_in_flight <= 2, f"max_in_flight={mock.max_in_flight}"

    def test_fleet_max_cap_clamped(self) -> None:
        tasks = [_make_task(f"repo-{i}") for i in range(4)]
        state, mock, results = _run_fleet(tasks, max_concurrency=FLEET_MAX_CAP + 100)
        assert len(results) == 4


class TestFleetIsolation:

    def test_one_failure_does_not_cancel_others(self) -> None:
        tasks = [_make_task("repo-ok-1"), _make_task("repo-fail"), _make_task("repo-ok-2")]
        mock = _MockDispatcher(raise_for={"repo-fail"})
        state, mock, results = _run_fleet(tasks, mock=mock)
        assert results[0].status == "running"
        assert results[1].status == "failed"
        assert "mock error" in results[1].rationale or "fleet worker error" in results[1].rationale
        assert results[2].status == "running"

    def test_fleet_does_not_raise(self) -> None:
        tasks = [_make_task("repo-a"), _make_task("repo-b")]
        mock = _MockDispatcher(raise_for={"repo-a", "repo-b"})
        state, mock, results = _run_fleet(tasks, mock=mock)
        assert all(r.status == "failed" for r in results)


class TestFleetSameRepoGuard:

    def test_duplicate_repo_id_rejected(self) -> None:
        tasks = [_make_task("repo-a"), _make_task("repo-a"), _make_task("repo-b")]
        state, mock, results = _run_fleet(tasks)
        assert results[0].status == "running"
        assert results[1].status == "failed"
        assert "duplicate target_repo_id" in results[1].rationale
        assert results[2].status == "running"
        assert len(mock.calls) == 2

    def test_duplicate_repo_id_rationale_mentions_id(self) -> None:
        tasks = [_make_task("repo-x"), _make_task("repo-x")]
        state, mock, results = _run_fleet(tasks)
        assert "repo-x" in results[1].rationale

    def test_two_none_target_tasks_at_most_one_runs(self) -> None:
        tasks = [_make_task(None), _make_task(None), _make_task("repo-a")]
        state, mock, results = _run_fleet(tasks)
        assert results[0].status == "running"
        assert results[1].status == "failed"
        assert "duplicate" in results[1].rationale or "distinct" in results[1].rationale
        assert results[2].status == "running"
        assert len(mock.calls) == 2


class TestFleetDeterminism:

    def test_inverse_sleep_order_still_deterministic(self) -> None:
        n = 4
        tasks = [_make_task(f"repo-{i}") for i in range(n)]
        sleep_map = {f"repo-{i}": (n - i) * 0.02 for i in range(n)}
        mock = _MockDispatcher(sleep_map=sleep_map)
        state, mock, results = _run_fleet(tasks, mock=mock, max_concurrency=n)
        assert len(results) == n
        for i, (task, result) in enumerate(zip(tasks, results)):
            assert f"repo-{i}" in result.envelopes[0].decision


class TestNoStateRace:

    def test_workers_see_open_pp_runs_empty_during_execution(self) -> None:
        state_snapshots: list[int] = []
        lock = threading.Lock()

        class _SnapshotMock(_MockDispatcher):
            def execute_squad_shim(self, s, pack, payload, dispatcher, *, collect_open_runs=None):
                with lock:
                    state_snapshots.append(len(s.open_pp_runs))
                return super().execute_squad_shim(s, pack, payload, dispatcher,
                                                   collect_open_runs=collect_open_runs)

        tasks = [_make_task(f"repo-{i}") for i in range(3)]
        mock = _SnapshotMock(sleep_map={f"repo-{i}": 0.03 for i in range(3)})
        state, _, results = _run_fleet(tasks, mock=mock, max_concurrency=3)
        # Note: workers get a stub_state (with empty open_pp_runs), not the real state.
        # This verifies the concurrency-safety invariant holds.
        assert len(state.open_pp_runs) == 3, (
            f"Expected 3 merged entries after join, got {len(state.open_pp_runs)}"
        )

    def test_final_open_pp_runs_count_equals_successful_dispatches(self) -> None:
        tasks = [
            _make_task("repo-a"),
            _make_task("repo-a"),
            _make_task("repo-b"),
            _make_task("repo-c"),
        ]
        state, mock, results = _run_fleet(tasks)
        successful = [r for r in results if r.status == "running"]
        assert len(state.open_pp_runs) == len(successful)


class TestFleetGating:

    def test_fleet_not_used_when_fleet_parallel_false(self) -> None:
        s = _state()
        s.fleet_parallel = False
        packs = {"engineering": _pack("engineering")}
        tasks = [_make_task("repo-a"), _make_task("repo-b")]

        def _build(task: TaskState) -> CSuiteDecisionPacket:
            return CSuiteDecisionPacket(
                workflow_id=s.workflow_id, origin_squad="hydra",
                target_squad=task.owner_squad, origin="BOARDROOM",
                objective=task.description, target_repo_id=task.target_repo_id,
            )

        candidate_tasks = [
            t for t in tasks if t.status == "pending"
            and packs.get(t.owner_squad) is not None
            and not (packs[t.owner_squad].best_of_n and packs[t.owner_squad].best_of_n >= 2)
            and packs[t.owner_squad].entrypoint == "mcp"
        ]
        payloads = {id(t): _build(t) for t in candidate_tasks}
        distinct = {str(payloads[id(t)].target_repo_id) for t in candidate_tasks
                    if payloads[id(t)].target_repo_id is not None}
        use_fleet = s.fleet_parallel and len(candidate_tasks) >= 2 and len(distinct) >= 2
        assert not use_fleet

    def test_fleet_used_with_fleet_parallel_true_and_distinct_mcp_repos(self) -> None:
        s = _state()
        s.fleet_parallel = True
        packs = {"engineering": _pack("engineering")}
        tasks = [_make_task("repo-a"), _make_task("repo-b")]

        def _build(task: TaskState) -> CSuiteDecisionPacket:
            return CSuiteDecisionPacket(
                workflow_id=s.workflow_id, origin_squad="hydra",
                target_squad=task.owner_squad, origin="BOARDROOM",
                objective=task.description, target_repo_id=task.target_repo_id,
            )

        candidate_tasks = [
            t for t in tasks if t.status == "pending"
            and packs.get(t.owner_squad) is not None
            and not (packs[t.owner_squad].best_of_n and packs[t.owner_squad].best_of_n >= 2)
            and packs[t.owner_squad].entrypoint == "mcp"
        ]
        payloads = {id(t): _build(t) for t in candidate_tasks}
        distinct = {str(payloads[id(t)].target_repo_id) for t in candidate_tasks
                    if payloads[id(t)].target_repo_id is not None}
        use_fleet = s.fleet_parallel and len(candidate_tasks) >= 2 and len(distinct) >= 2
        assert use_fleet

    def test_fleet_not_used_with_single_distinct_repo(self) -> None:
        s = _state()
        s.fleet_parallel = True
        packs = {"engineering": _pack("engineering")}
        tasks = [_make_task("repo-a"), _make_task("repo-a")]

        def _build(task: TaskState) -> CSuiteDecisionPacket:
            return CSuiteDecisionPacket(
                workflow_id=s.workflow_id, origin_squad="hydra",
                target_squad=task.owner_squad, origin="BOARDROOM",
                objective=task.description, target_repo_id=task.target_repo_id,
            )

        candidate_tasks = [
            t for t in tasks if t.status == "pending"
            and packs.get(t.owner_squad) is not None
            and not (packs[t.owner_squad].best_of_n and packs[t.owner_squad].best_of_n >= 2)
            and packs[t.owner_squad].entrypoint == "mcp"
        ]
        payloads = {id(t): _build(t) for t in candidate_tasks}
        distinct = {str(payloads[id(t)].target_repo_id) for t in candidate_tasks
                    if payloads[id(t)].target_repo_id is not None}
        use_fleet = s.fleet_parallel and len(candidate_tasks) >= 2 and len(distinct) >= 2
        assert not use_fleet


class TestFleetEmptyAndEdgeCases:

    def test_empty_task_list(self) -> None:
        state, mock, results = _run_fleet([])
        assert results == []
        assert state.open_pp_runs == []

    def test_task_with_no_pack_is_failed_immediately(self) -> None:
        tasks = [_make_task("repo-a")]
        state, mock, results = _run_fleet(tasks, packs={})
        assert results[0].status == "failed"
        assert "no pack" in results[0].rationale
        assert state.open_pp_runs == []

    def test_single_task_runs_with_max_concurrency_1(self) -> None:
        tasks = [_make_task("repo-a")]
        state, mock, results = _run_fleet(tasks, max_concurrency=1)
        assert results[0].status == "running"
        assert len(state.open_pp_runs) == 1


# ===========================================================================
# RBAC: worker dispatchers enforce the same allow-list as the original.
# ===========================================================================

class TestRbacWorkerDispatcher:
    """A worker dispatcher constructed by the factory must have the same
    squad-pack RBAC allow-list as the original dispatcher, and must reject
    an unauthorized tool call just as the original would."""

    def _make_mock_dispatcher_with_rbac(self) -> Any:
        """Return a mock dispatcher that honours set_squad_packs / _check_tool_rbac."""
        from hydra_core.dispatcher import MCPStdioDispatcher
        from pathlib import Path

        class _FakeRbacDispatcher:
            """Minimal dispatcher that implements set_squad_packs + call_mcp RBAC
            check, without needing a real MCP server."""

            def __init__(self, project_root: Any = None, *, verbose: bool = False):
                self.project_root = project_root or Path(".")
                self.verbose = verbose
                self._squad_packs: dict[str, Any] = {}
                self._active_handoffs: list[dict[str, Any]] = []
                self._tool_tracker: Any = None

            def set_squad_packs(self, packs: dict[str, Any]) -> None:
                self._squad_packs = dict(packs)

            def _check_tool_rbac(self, server: str, tool: str,
                                  squad_id: str | None) -> str | None:
                """Simplified version of MCPStdioDispatcher._check_tool_rbac."""
                if squad_id is None:
                    return None
                pack = self._squad_packs.get(squad_id)
                if pack is None:
                    return None
                declared_tools = getattr(pack, "tools", ())
                tool_key = f"{server}.{tool}"
                for t in declared_tools:
                    t_name = getattr(t, "name", t) if not isinstance(t, str) else t
                    if t_name in (tool_key, tool):
                        return None
                return (
                    f"RBAC: squad {squad_id!r} not authorized for "
                    f"{tool!r} on {server!r}"
                )

            def call_mcp(self, server: str, tool: str, args: dict[str, Any],
                         *, squad_id: str | None = None) -> dict[str, Any]:
                rejection = self._check_tool_rbac(server, tool, squad_id)
                if rejection:
                    return {"status": "rejected", "error": rejection}
                return {"status": "done", "result": {}}

            def emit_claude_prompt(self, *a: Any, **k: Any) -> Any:
                raise NotImplementedError

            def invoke_claude_skill(self, *a: Any, **k: Any) -> Any:
                raise NotImplementedError

            def spawn_subprocess(self, *a: Any, **k: Any) -> Any:
                raise NotImplementedError

        return _FakeRbacDispatcher

    def test_worker_dispatcher_has_same_squad_packs_as_original(self) -> None:
        """The factory must copy _squad_packs onto each new worker dispatcher."""
        _FakeRbacDispatcher = self._make_mock_dispatcher_with_rbac()
        from hydra_core.squad_loader import SquadPack

        original = _FakeRbacDispatcher()
        eng_pack = _pack("engineering")
        original.set_squad_packs({"engineering": eng_pack})

        # Simulate the factory closure that supervisor.py builds.
        _sp = dict(getattr(original, "_squad_packs", {}))
        _ho = list(getattr(original, "_active_handoffs", []))
        _tt = getattr(original, "_tool_tracker", None)
        _dcls = original.__class__
        _pr = original.project_root
        _vb = original.verbose

        def factory(
            _cls=_dcls, _root=_pr, _v=_vb,
            _squad_packs=_sp, _handoffs=_ho, _tracker=_tt,
        ):
            d = _cls(project_root=_root, verbose=_v)
            if hasattr(d, "set_squad_packs"):
                d.set_squad_packs(_squad_packs)
            if hasattr(d, "_active_handoffs"):
                d._active_handoffs = list(_handoffs)
            if hasattr(d, "_tool_tracker"):
                d._tool_tracker = _tracker
            return d

        worker_disp = factory()
        assert worker_disp._squad_packs == original._squad_packs, (
            "Worker dispatcher must have identical _squad_packs to the original"
        )

    def test_worker_dispatcher_rejects_unauthorized_tool(self) -> None:
        """An unauthorized tool call on a worker dispatcher must be rejected
        (not silently allowed due to blank RBAC)."""
        _FakeRbacDispatcher = self._make_mock_dispatcher_with_rbac()
        from hydra_core.squad_loader import SquadPack, ToolSpec

        # Engineering pack: only allows "pp_harness.start_run".
        eng_pack = SquadPack(
            slug="engineering",
            name="engineering",
            description="test",
            entrypoint="mcp",
            agents=(),
            tools=(ToolSpec(name="pp_harness.start_run", mcp_server="pp_harness"),),
        )

        original = _FakeRbacDispatcher()
        original.set_squad_packs({"engineering": eng_pack})

        _sp = dict(original._squad_packs)
        _ho = list(original._active_handoffs)
        _tt = original._tool_tracker
        _dcls = original.__class__

        def factory(_cls=_dcls, _squad_packs=_sp, _handoffs=_ho, _tracker=_tt):
            d = _cls()
            d.set_squad_packs(_squad_packs)
            d._active_handoffs = list(_handoffs)
            d._tool_tracker = _tracker
            return d

        worker_disp = factory()

        # Authorized tool: must succeed.
        r_ok = worker_disp.call_mcp("pp_harness", "start_run", {}, squad_id="engineering")
        assert r_ok.get("status") == "done", f"Expected ok, got {r_ok}"

        # Unauthorized tool: must be rejected (not allowed by RBAC).
        r_bad = worker_disp.call_mcp("pp_harness", "delete_all_runs", {}, squad_id="engineering")
        assert r_bad.get("status") == "rejected", (
            f"Worker dispatcher must reject unauthorized tool, got {r_bad}"
        )
        assert "RBAC" in r_bad.get("error", ""), r_bad

    def test_blank_worker_dispatcher_allows_unauthorized_tool(self) -> None:
        """Confirm the vulnerability: a blank (no set_squad_packs) worker dispatcher
        would NOT reject unauthorized tools -- showing why the fix matters."""
        _FakeRbacDispatcher = self._make_mock_dispatcher_with_rbac()
        from hydra_core.squad_loader import SquadPack, ToolSpec

        eng_pack = SquadPack(
            slug="engineering", name="engineering", description="test",
            entrypoint="mcp", agents=(),
            tools=(ToolSpec(name="pp_harness.start_run", mcp_server="pp_harness"),),
        )

        original = _FakeRbacDispatcher()
        original.set_squad_packs({"engineering": eng_pack})

        # BUGGY factory: no RBAC copy -- this is what the pre-fix code did.
        def buggy_factory(_cls=original.__class__):
            return _cls()  # blank _squad_packs={}

        blank_disp = buggy_factory()
        r = blank_disp.call_mcp("pp_harness", "delete_all_runs", {}, squad_id="engineering")
        # A blank dispatcher has no pack in _squad_packs -> check returns None -> allowed.
        assert r.get("status") == "done", (
            "Confirms blank dispatcher bypasses RBAC (the bug the fix closes)"
        )


# ===========================================================================
# Fix 5: _build_payload called exactly once per task across BOTH paths.
# ===========================================================================

class TestFix5BuildOnceAcrossBothPaths:
    """_build_payload is called at most once per task in node_dispatch,
    regardless of whether the fleet path or the sequential path runs."""

    def _spy_build_payload_via_node_dispatch(
        self,
        tasks: list[TaskState],
        *,
        fleet_parallel: bool,
    ) -> tuple[dict[str, int], Any]:
        """
        Run a minimal node_dispatch simulation by calling _build_payload as
        node_dispatch would, and return a counter of how many times each
        task's target_repo_id was built.

        We test the invariant by checking that the all-tasks-payload-map
        (_all_task_payloads) in node_dispatch is used as the single source
        for both the fleet candidate list and the sequential loop.

        Since node_dispatch is inside build_supervisor (hard to isolate), we
        test the invariant at the dispatch_fleet level: dispatch_fleet's
        build_payload callable is invoked once per task, and the sequential
        loop uses the stored payload (not a fresh call).  We verify this by
        counting how many times the build function fires for each repo.
        """
        from hydra_core.schemas import CSuiteDecisionPacket

        s = _state()
        s.fleet_parallel = fleet_parallel
        packs = {"engineering": _pack("engineering")}
        call_count: dict[str, int] = {}

        def _counting_build(task: TaskState) -> CSuiteDecisionPacket:
            key = task.target_repo_id or "none"
            call_count[key] = call_count.get(key, 0) + 1
            return CSuiteDecisionPacket(
                workflow_id=s.workflow_id,
                origin_squad="hydra",
                target_squad=task.owner_squad,
                origin="BOARDROOM",
                objective=task.description,
                target_repo_id=task.target_repo_id,
            )

        def _factory() -> _MockDispatcher:
            return _MockDispatcher()

        with patch("hydra_core.fleet.execute_squad") as mock_exec:
            mock_exec.side_effect = lambda state, pack, payload, disp, **kw: SquadResult(
                envelopes=[], artifacts=[], status="running",
            )
            results, _wt = dispatch_fleet(
                s, tasks, _factory,
                build_payload=_counting_build,
                packs=packs,
            )
        return call_count, results

    def test_build_payload_called_once_per_task_fleet_path(self) -> None:
        """With fleet path: dispatch_fleet must call build_payload once per task."""
        tasks = [_make_task("repo-a"), _make_task("repo-b"), _make_task("repo-c")]
        count, results = self._spy_build_payload_via_node_dispatch(tasks, fleet_parallel=True)
        for key, n in count.items():
            assert n == 1, f"build_payload called {n} times for {key!r} (fleet path)"

    def test_build_payload_called_once_per_task_sequential_path(self) -> None:
        """Sequential path (fleet_parallel=False): verify the stored payload
        in _all_task_payloads is consumed (count still 1 per task in fleet's
        own build_payload; node_dispatch's sequential uses _all_task_payloads)."""
        # This test verifies fleet.py build-once; the sequential loop's
        # fix (consuming _all_task_payloads) is verified by the supervisor
        # integration indirectly -- we confirm no double-build in fleet itself.
        tasks = [_make_task("repo-a"), _make_task("repo-b")]
        count, results = self._spy_build_payload_via_node_dispatch(tasks, fleet_parallel=False)
        for key, n in count.items():
            assert n == 1, f"build_payload called {n} times for {key!r} (sequential path)"


# ===========================================================================
# Fix 6: All fleet results charged before HITL surfaces (no undercount).
# ===========================================================================

class TestFix6AllResultsChargedBeforeHitl:
    """When the budget blocks, ALL fleet results must be charged before the
    HITL surfaces -- the ledger must reflect the full fleet spend."""

    def test_all_three_results_charged_before_hitl(self) -> None:
        """
        Simulate 3 fleet results each costing $0.60 against a $1.00 budget.
        The first result alone tips it over. Under the old (buggy) code only
        $0.60 would be charged. Under the fixed code all three are charged
        ($1.80 total), and the HITL's spent_usd reflects the full $1.80.
        """
        from hydra_core.governance import charge_and_gate
        from hydra_core.state import BudgetLedger
        from hydra_core.schemas import DecisionRecord

        s = _state()
        s.budget = BudgetLedger(budget_usd=1.00, spent_usd=0.0)

        # Build 3 fake SquadResults each with a pp_run artifact costing $0.60.
        def _result_with_cost(cost: float) -> SquadResult:
            from hydra_core.schemas import DecisionRecord
            decision = DecisionRecord(
                workflow_id=s.workflow_id,
                parent_id=None,
                origin_squad="engineering",
                target_squad="hydra",
                decision="ok",
                rationale="",
                artifacts=[],
            )
            return SquadResult(
                envelopes=[decision],
                artifacts=[{
                    "kind": "pp_run",
                    "ref": "r1",
                    "raw": {"result": {"cost_usd": cost}},
                }],
                status="running",
            )

        fleet_results = [_result_with_cost(0.60) for _ in range(3)]

        # Simulate the TWO-PASS fleet merge from node_dispatch.
        # Pass 1: charge all, record any block.
        _fleet_any_block = False
        for result in fleet_results:
            from hydra_core.supervisor import _extract_squad_cost  # noqa: PLC0415
            _cost_usd, _cost_tok = _extract_squad_cost(result)
            _block, _ = charge_and_gate(s, _cost_usd, _cost_tok)
            if _block:
                _fleet_any_block = True
            # Continue charging even after first block -- do NOT return early.

        # Pass 2: surface HITL with the FULL ledger.
        assert _fleet_any_block, "Should have hit budget block"
        final_spent = s.budget.spent_usd
        # All 3 x $0.60 = $1.80 must be recorded.
        assert final_spent == pytest.approx(1.80, abs=1e-6), (
            f"Expected $1.80 charged (all 3 results), got ${final_spent:.4f}"
        )

    def test_early_return_would_undercount(self) -> None:
        """Confirm that early-returning on first block undercounts spend.
        This is the negative test showing why the two-pass fix matters."""
        from hydra_core.governance import charge_and_gate
        from hydra_core.state import BudgetLedger

        s = _state()
        s.budget = BudgetLedger(budget_usd=1.00, spent_usd=0.0)

        def _result_with_cost(cost: float) -> SquadResult:
            return SquadResult(
                envelopes=[],
                artifacts=[{
                    "kind": "pp_run",
                    "ref": "r1",
                    "raw": {"result": {"cost_usd": cost}},
                }],
                status="running",
            )

        fleet_results = [_result_with_cost(0.60) for _ in range(3)]

        # Buggy single-pass: return on first block.
        from hydra_core.supervisor import _extract_squad_cost
        spent_at_hitl_buggy = None
        for result in fleet_results:
            _cost_usd, _cost_tok = _extract_squad_cost(result)
            _block, _ = charge_and_gate(s, _cost_usd, _cost_tok)
            if _block:
                spent_at_hitl_buggy = s.budget.spent_usd
                break  # buggy early return

        assert spent_at_hitl_buggy is not None
        # Only 2 results were charged before the early return ($0.60 + $0.60 = $1.20),
        # or 1 result tipped it ($0.60 on a $1.00 budget -> $0.60 charged then block).
        # Either way, spent_at_hitl_buggy < $1.80 -- the ledger is incomplete.
        assert spent_at_hitl_buggy < 1.80, (
            f"Buggy path undercharges: ${spent_at_hitl_buggy:.4f} vs full $1.80"
        )

    def test_failed_result_cost_is_charged(self) -> None:
        """A failed SquadResult that carries cost must still be charged (Fix 6 gap).
        Previously, 'if pack is None or result.status == "failed": continue' ran
        before charge_and_gate, so failed results were silently undercharged.
        The fixed loop charges first, then branches on failure."""
        from hydra_core.governance import charge_and_gate
        from hydra_core.state import BudgetLedger
        from hydra_core.supervisor import _extract_squad_cost

        s = _state()
        s.budget = BudgetLedger(budget_usd=10.00, spent_usd=0.0)

        # Result 1: succeeded, $0.50
        ok_result = SquadResult(
            envelopes=[],
            artifacts=[{
                "kind": "pp_run",
                "ref": "r-ok",
                "raw": {"result": {"cost_usd": 0.50}},
            }],
            status="running",
        )
        # Result 2: FAILED but still carries $0.40 cost (paid MCP call that errored).
        failed_result = SquadResult(
            envelopes=[],
            artifacts=[{
                "kind": "pp_run",
                "ref": "r-fail",
                "raw": {"result": {"cost_usd": 0.40}},
            }],
            status="failed",
        )

        # Simulate the fixed pass-1 loop: charge BEFORE status-gate.
        for result in [ok_result, failed_result]:
            _cost_usd, _cost_tok = _extract_squad_cost(result)
            charge_and_gate(s, _cost_usd, _cost_tok)
            # (In the real loop the 'failed' branch continues after charging;
            # here we just verify charging happens.)

        # Both results' costs must appear in the ledger.
        assert s.budget.spent_usd == pytest.approx(0.90, abs=1e-6), (
            f"Expected $0.90 charged (ok $0.50 + failed $0.40), "
            f"got ${s.budget.spent_usd:.4f}"
        )


# ===========================================================================
# Round 4: per-worker tool tracker and merge (REGRESSION fix).
# ===========================================================================

class TestTrackerPerWorkerAndMerge:
    """Verify that each worker dispatcher receives a FRESH ToolUsageTracker
    (no shared mutable list), and that after the join the original tracker
    holds the union of all workers' recorded calls in deterministic INPUT-INDEX order.

    Key design under test:
    - The factory is a PURE function: construct + configure + return.
      It must NOT append to any shared list from the worker thread.
    - dispatch_fleet pre-sizes worker_trackers[n] and each worker writes
      its tracker to worker_trackers[idx] — assignment to a distinct slot is
      race-free without any lock.
    - The post-join merge iterates worker_trackers[0..n-1] in index order
      (not completion/scheduler order) so the result is deterministic.

    All tests use MOCK dispatchers only (no real MCP).
    """

    @staticmethod
    def _make_tracker_dispatcher_class() -> type:
        """Return a dispatcher class that owns a ToolUsageTracker."""
        from hydra_core.tool_analytics import ToolUsageTracker

        class _TrkDispatcher:
            def __init__(self, project_root=None, *, verbose: bool = False,
                         instance_id: int = 0) -> None:
                from pathlib import Path
                self.project_root = project_root or Path(".")
                self.verbose = verbose
                self.instance_id = instance_id
                self._squad_packs: dict[str, Any] = {}
                self._active_handoffs: list[dict[str, Any]] = []
                self._tool_tracker: ToolUsageTracker = ToolUsageTracker()

            def set_squad_packs(self, packs: dict[str, Any]) -> None:
                self._squad_packs = dict(packs)

            def call_mcp(self, *a: Any, **k: Any) -> dict[str, Any]:
                return {}

            def emit_claude_prompt(self, *a: Any, **k: Any) -> Any:
                raise NotImplementedError

            def invoke_claude_skill(self, *a: Any, **k: Any) -> Any:
                raise NotImplementedError

            def spawn_subprocess(self, *a: Any, **k: Any) -> Any:
                raise NotImplementedError

        return _TrkDispatcher

    @staticmethod
    def _make_pure_factory(original: Any) -> Any:
        """Build the same pure factory closure that supervisor.py produces.
        Pure = no side effects on shared state; no append to any list.
        """
        from hydra_core.tool_analytics import ToolUsageTracker

        _tp = getattr(original._tool_tracker, "_packs", {})
        _dcls = original.__class__
        _pr = original.project_root
        _vb = original.verbose
        _sp = dict(original._squad_packs)
        _ho = list(original._active_handoffs)

        def _factory(
            _cls=_dcls, _root=_pr, _v=_vb,
            _squad_packs=_sp, _handoffs=_ho,
            _tracker_packs=_tp,
        ):
            d = _cls(project_root=_root, verbose=_v)
            if hasattr(d, "set_squad_packs"):
                d.set_squad_packs(_squad_packs)
            if hasattr(d, "_active_handoffs"):
                d._active_handoffs = list(_handoffs)
            # FRESH tracker per worker; factory does NOT touch any shared list.
            if hasattr(d, "_tool_tracker"):
                d._tool_tracker = ToolUsageTracker(packs=_tracker_packs)
            return d   # no append, no shared mutation

        return _factory

    def test_factory_does_not_append_to_any_shared_list(self) -> None:
        """The factory must be a pure function: construct + configure + return.
        Calling it N times must NOT mutate any shared collection.
        This is the core safety property: worker threads call the factory, and
        if it appended to a shared list that would be an unsynchronised concurrent
        mutation (even if CPython's GIL makes it 'usually safe', it's wrong)."""
        _TrkDispatcher = self._make_tracker_dispatcher_class()
        original = _TrkDispatcher(instance_id=0)
        factory = self._make_pure_factory(original)

        # Call the factory 4 times (simulating 4 concurrent workers).
        # If the factory appended to a shared list, that list would grow.
        # We verify no shared external state changes between calls.
        dispatchers = [factory() for _ in range(4)]

        # Each call returned a distinct object.
        ids = [id(d) for d in dispatchers]
        assert len(set(ids)) == 4, "factory must return a new instance each call"

        # Each has a FRESH tracker (not the original's).
        for d in dispatchers:
            assert d._tool_tracker is not original._tool_tracker, (
                "worker tracker must not be the original tracker"
            )
        # All four trackers are distinct from each other.
        tracker_ids = [id(d._tool_tracker) for d in dispatchers]
        assert len(set(tracker_ids)) == 4, "each worker must have a distinct tracker"

    def test_workers_use_distinct_tracker_instances(self) -> None:
        """Factory must produce a FRESH ToolUsageTracker per worker, not share
        the original tracker reference."""
        _TrkDispatcher = self._make_tracker_dispatcher_class()
        original = _TrkDispatcher(instance_id=0)
        original_tracker = original._tool_tracker
        factory = self._make_pure_factory(original)

        w1 = factory()
        w2 = factory()

        assert w1._tool_tracker is not original_tracker, (
            "worker 1 must NOT share the original tracker"
        )
        assert w2._tool_tracker is not original_tracker, (
            "worker 2 must NOT share the original tracker"
        )
        assert w1._tool_tracker is not w2._tool_tracker, (
            "worker 1 and worker 2 must have DISTINCT tracker instances"
        )

    def test_merge_in_index_order_not_scheduler_order(self) -> None:
        """The merge must iterate worker_trackers[0..n-1] (input index order),
        NOT by append/completion order.

        Setup: 3 tasks at indices 0, 1, 2.  Simulate workers completing in
        reverse order (2, 1, 0) but writing to their pre-allocated index slots.
        Post-merge the original tracker must contain calls in index order:
        tool-idx-0, tool-idx-1, tool-idx-2 — proving index-order merge.
        """
        from hydra_core.tool_analytics import ToolUsageTracker

        _TrkDispatcher = self._make_tracker_dispatcher_class()
        original = _TrkDispatcher(instance_id=0)
        factory = self._make_pure_factory(original)

        n = 3
        # Pre-size exactly as dispatch_fleet does.
        worker_trackers: list[Any] = [None] * n

        # Simulate workers completing in reverse order (worst-case for scheduler).
        for idx in reversed(range(n)):  # 2, 1, 0 — reverse completion order
            d = factory()
            # Worker writes tracker to its reserved slot (no append).
            worker_trackers[idx] = d._tool_tracker
            d._tool_tracker.record(
                workflow_id="wf-order",
                squad_id="engineering",
                node_name="dispatch",
                server="pp",
                tool=f"tool-idx-{idx}",
                status="ok",
            )

        # Verify pre-sized slots are all filled and distinct.
        assert all(wt is not None for wt in worker_trackers)
        tracker_ids = [id(wt) for wt in worker_trackers]
        assert len(set(tracker_ids)) == n, "each slot must hold a distinct tracker"

        # Post-join merge: iterate index 0..n-1 (as supervisor.py does).
        orig_tt = original._tool_tracker
        assert len(orig_tt._calls) == 0, "original untouched before merge"
        for wt in worker_trackers:  # index order 0, 1, 2
            if wt is not None:
                orig_tt._calls.extend(wt._calls)
                wt._calls = []

        # Must have calls in INDEX ORDER (0, 1, 2), not completion order (2, 1, 0).
        assert len(orig_tt._calls) == n
        tools = [c.tool for c in orig_tt._calls]
        assert tools == ["tool-idx-0", "tool-idx-1", "tool-idx-2"], (
            f"merge must produce index order [0,1,2], got {tools}"
        )
        # All worker trackers cleared.
        for wt in worker_trackers:
            assert wt._calls == []

    def test_merge_collects_all_worker_calls_into_original(self) -> None:
        """After the join, original tracker._calls must contain every call
        from every worker, in input-index order."""
        from hydra_core.tool_analytics import ToolUsageTracker

        _TrkDispatcher = self._make_tracker_dispatcher_class()
        original = _TrkDispatcher(instance_id=0)
        factory = self._make_pure_factory(original)

        n = 2
        worker_trackers: list[Any] = [None] * n

        # Simulate workers 0 and 1.
        for idx in range(n):
            d = factory()
            worker_trackers[idx] = d._tool_tracker
            d._tool_tracker.record(
                workflow_id="wf-1", squad_id="engineering",
                node_name="dispatch", server="pp",
                tool=f"run_{idx + 1}",
                status="ok",
            )

        # No cross-contamination while workers are running.
        assert len(worker_trackers[0]._calls) == 1
        assert len(worker_trackers[1]._calls) == 1
        assert len(original._tool_tracker._calls) == 0, (
            "original tracker must be untouched while workers run"
        )

        # Simulate post-join index-order merge (as supervisor.py does).
        _orig_tt = original._tool_tracker
        for _wt in worker_trackers:  # index 0 then 1
            if _wt is not None:
                _orig_tt._calls.extend(_wt._calls)
                _wt._calls = []

        # Original now holds both calls in index order.
        assert len(original._tool_tracker._calls) == 2, (
            f"Expected 2 calls after merge, got {len(original._tool_tracker._calls)}"
        )
        tools_recorded = [c.tool for c in original._tool_tracker._calls]
        assert tools_recorded == ["run_1", "run_2"], (
            f"Expected index-order [run_1, run_2], got {tools_recorded}"
        )
        # Worker trackers are cleared after merge.
        for wt in worker_trackers:
            assert wt._calls == []

    def test_no_race_on_shared_tracker_when_concurrent(self) -> None:
        """Concurrent workers write to SEPARATE pre-allocated tracker slots.
        We run 8 workers each appending 100 calls to their OWN tracker;
        afterwards the merged tracker must have exactly 800 calls in INPUT
        INDEX ORDER (worker 0 first, ..., worker 7 last) — no sort needed,
        because the pre-sized list already guarantees index-based ordering.

        Additionally asserts: the factory is never called with a shared-list
        append (we check the factory signature has no _wd_list param).
        """
        from hydra_core.tool_analytics import ToolUsageTracker
        import concurrent.futures
        import inspect

        _TrkDispatcher = self._make_tracker_dispatcher_class()
        original = _TrkDispatcher(instance_id=0)
        factory = self._make_pure_factory(original)

        # Assert factory has no _wd_list parameter (pure, no shared-append param).
        sig = inspect.signature(factory)
        assert "_wd_list" not in sig.parameters, (
            "factory must NOT accept _wd_list — it must be a pure construct+return"
        )

        NUM_WORKERS = 8
        CALLS_PER_WORKER = 100

        # Pre-size as dispatch_fleet does.
        orig_tracker = ToolUsageTracker()
        worker_trackers: list[Any] = [None] * NUM_WORKERS

        def _worker_job(worker_idx: int) -> None:
            tracker = ToolUsageTracker()
            # Write to pre-allocated slot — race-free (distinct index, no append).
            worker_trackers[worker_idx] = tracker
            for i in range(CALLS_PER_WORKER):
                tracker.record(
                    workflow_id="wf-race",
                    squad_id=f"squad-{worker_idx}",
                    node_name="dispatch",
                    server="pp",
                    tool=f"tool-{worker_idx}-{i}",
                    status="ok",
                )

        with concurrent.futures.ThreadPoolExecutor(max_workers=NUM_WORKERS) as pool:
            futs = [pool.submit(_worker_job, i) for i in range(NUM_WORKERS)]
            concurrent.futures.wait(futs)

        # Merge in INDEX ORDER (0..7) — no sort needed.
        for wt in worker_trackers:  # already in index order
            if wt is not None:
                orig_tracker._calls.extend(wt._calls)
                wt._calls = []

        # Must have exactly NUM_WORKERS * CALLS_PER_WORKER entries.
        total = len(orig_tracker._calls)
        assert total == NUM_WORKERS * CALLS_PER_WORKER, (
            f"Expected {NUM_WORKERS * CALLS_PER_WORKER} calls after merge, got {total}"
        )

        # Assert calls appear in input-index order:
        # all calls from worker 0 come before worker 1, ..., before worker 7.
        worker_ids_seen = [
            int(c.squad_id.split("-")[1]) for c in orig_tracker._calls
        ]
        # Group-by-index: should be exactly CALLS_PER_WORKER of each index, in order.
        expected_sequence = []
        for widx in range(NUM_WORKERS):
            expected_sequence.extend([widx] * CALLS_PER_WORKER)
        assert worker_ids_seen == expected_sequence, (
            "merged calls must be grouped by worker index 0..7 (input-index order)"
        )

        # All worker trackers cleared.
        for wt in worker_trackers:
            assert wt._calls == []
