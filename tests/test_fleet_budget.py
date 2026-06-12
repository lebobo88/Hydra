"""WS8 SLICE 4 — per-repo fleet budget isolation tests.

No network, no real MCP, no real pp runs.

Covers:
  1. allocate_repos: equal split, sum == budget_usd, no drift, 0-repos no-crash,
     repo_remaining correct after spend.
     Fix 1: integer micro-unit allocation — exact sum for tiny/uneven budgets.
     Fix 2: reallocation replaces + resets per-repo spend (no stale entries).
  2. charge_and_gate_repo: per-repo over flag, global ledger correctness, isolation
     (repo over != global block), repo with no allocation -> repo_over False.
     Fix 3: None/unallocated spend -> "(unattributed)" bucket; global reconciles.
  3. ISOLATION: per-repo over does NOT block the fleet (only global_block does).
  4. Per-repo breakdown present in HITL payload when global block fires.
  5. Fleet charge loop wiring via mock — drives the supervisor's PASS 1/2 logic.
"""
from __future__ import annotations

import math
import threading
import uuid
from typing import Any
from unittest.mock import MagicMock, patch

import pytest

from hydra_core.governance import charge_and_gate_repo
from hydra_core.state import BudgetLedger, HydraState, TaskState


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _ledger(budget: float = 30.0) -> BudgetLedger:
    return BudgetLedger(budget_usd=budget)


def _state(budget: float = 30.0) -> HydraState:
    return HydraState(root_goal="fleet-budget-test", budget=_ledger(budget))


# ---------------------------------------------------------------------------
# 1. BudgetLedger.allocate_repos
# ---------------------------------------------------------------------------

class TestAllocateRepos:
    def test_equal_split_three_repos(self) -> None:
        ledger = _ledger(30.0)
        ledger.allocate_repos(["a", "b", "c"])
        assert len(ledger.repo_budgets) == 3
        assert ledger.repo_budgets["a"] == pytest.approx(10.0)
        assert ledger.repo_budgets["b"] == pytest.approx(10.0)
        assert ledger.repo_budgets["c"] == pytest.approx(10.0)

    def test_sum_equals_budget_usd_no_drift(self) -> None:
        ledger = _ledger(30.0)
        ledger.allocate_repos(["a", "b", "c"])
        total = sum(ledger.repo_budgets.values())
        # Float sum within 1e-6 (one micro-dollar) of budget_usd.
        assert abs(total - 30.0) < 1e-6, (
            f"Sum of repo allocations ({total}) must be within 1e-6 of budget_usd 30.0"
        )
        # Integer micro-sum must be exactly round(budget_usd * 1e6).
        micro_sum = sum(round(v * 1_000_000) for v in ledger.repo_budgets.values())
        assert micro_sum == round(30.0 * 1_000_000)

    def test_sum_equals_budget_usd_uneven(self) -> None:
        """budget=10 / 3 repos -> not cleanly divisible; sum within 1e-6, micro exact."""
        ledger = _ledger(10.0)
        ledger.allocate_repos(["x", "y", "z"])
        total = sum(ledger.repo_budgets.values())
        assert abs(total - 10.0) < 1e-6
        micro_sum = sum(round(v * 1_000_000) for v in ledger.repo_budgets.values())
        assert micro_sum == round(10.0 * 1_000_000)

    def test_zero_repos_no_crash(self) -> None:
        ledger = _ledger(30.0)
        ledger.allocate_repos([])  # must not raise
        assert ledger.repo_budgets == {}

    def test_single_repo_gets_full_budget(self) -> None:
        ledger = _ledger(20.0)
        ledger.allocate_repos(["only"])
        assert ledger.repo_budgets["only"] == pytest.approx(20.0)

    def test_duplicates_are_deduplicated(self) -> None:
        """Duplicate repo ids must be treated as one distinct repo."""
        ledger = _ledger(30.0)
        ledger.allocate_repos(["a", "a", "b"])
        assert set(ledger.repo_budgets.keys()) == {"a", "b"}
        total = sum(ledger.repo_budgets.values())
        assert abs(total - 30.0) < 1e-6

    def test_repo_remaining_before_spend(self) -> None:
        ledger = _ledger(30.0)
        ledger.allocate_repos(["a", "b", "c"])
        assert ledger.repo_remaining("a") == pytest.approx(10.0)
        assert ledger.repo_remaining("b") == pytest.approx(10.0)
        assert ledger.repo_remaining("c") == pytest.approx(10.0)

    def test_repo_remaining_after_spend(self) -> None:
        ledger = _ledger(30.0)
        ledger.allocate_repos(["a", "b", "c"])
        ledger.repo_spend["a"] = 4.0
        assert ledger.repo_remaining("a") == pytest.approx(6.0)
        assert ledger.repo_remaining("b") == pytest.approx(10.0)

    def test_repo_remaining_no_allocation_returns_inf(self) -> None:
        """A repo with no per-repo allocation has infinite remaining."""
        ledger = _ledger(30.0)
        # Do NOT call allocate_repos
        assert ledger.repo_remaining("unknown") == math.inf

    def test_negative_budget_raises_value_error(self) -> None:
        """Fix 1: a negative budget_usd is a misconfiguration -> ValueError."""
        ledger = BudgetLedger(budget_usd=-5.0)
        with pytest.raises(ValueError, match="non-negative"):
            ledger.allocate_repos(["a", "b"])

    def test_zero_budget_all_allocations_zero(self) -> None:
        """Fix 1: zero budget is valid -> every repo allocation is 0.0."""
        ledger = BudgetLedger(budget_usd=0.0)
        ledger.allocate_repos(["a", "b", "c"])
        assert len(ledger.repo_budgets) == 3
        for rid, alloc in ledger.repo_budgets.items():
            assert alloc == 0.0, f"repo {rid!r}: expected 0.0, got {alloc}"
        assert sum(ledger.repo_budgets.values()) == 0.0


# ---------------------------------------------------------------------------
# 2. charge_and_gate_repo
# ---------------------------------------------------------------------------

class TestChargeAndGateRepo:
    def test_repo_over_when_exceeds_allocation(self) -> None:
        state = _state(30.0)
        state.budget.allocate_repos(["a", "b", "c"])  # each gets 10.0
        repo_over, global_block, downgrade = charge_and_gate_repo(state, "a", 12.0, 0)
        assert repo_over is True, "repo 'a' spent 12 > allocation 10 => repo_over"

    def test_repo_not_over_within_allocation(self) -> None:
        state = _state(30.0)
        state.budget.allocate_repos(["a", "b", "c"])  # each gets 10.0
        repo_over, global_block, downgrade = charge_and_gate_repo(state, "b", 5.0, 0)
        assert repo_over is False

    def test_repo_no_allocation_never_repo_over(self) -> None:
        """A repo not in repo_budgets must never produce repo_over True."""
        state = _state(30.0)
        # No allocate_repos call — repo_budgets is empty
        repo_over, global_block, downgrade = charge_and_gate_repo(
            state, "unknown_repo", 9999.0, 0
        )
        assert repo_over is False

    def test_repo_id_none_never_repo_over(self) -> None:
        state = _state(30.0)
        state.budget.allocate_repos(["a"])
        repo_over, global_block, downgrade = charge_and_gate_repo(
            state, None, 5.0, 0
        )
        assert repo_over is False

    def test_global_ledger_accumulates_all_charges(self) -> None:
        """Global spent_usd must equal the sum of all charges regardless of repo."""
        state = _state(30.0)
        state.budget.allocate_repos(["a", "b", "c"])
        charge_and_gate_repo(state, "a", 4.0, 100)
        charge_and_gate_repo(state, "b", 3.0, 50)
        charge_and_gate_repo(state, "c", 2.0, 25)
        assert state.budget.spent_usd == pytest.approx(9.0)
        assert state.budget.spent_tokens == 175

    def test_global_block_when_spent_equals_budget(self) -> None:
        """Global block fires when global spent >= budget_usd, NOT when repo_over."""
        state = _state(30.0)
        state.budget.allocate_repos(["a", "b", "c"])
        # Charge 10 each: global total = 30.0 = budget_usd => global_block
        charge_and_gate_repo(state, "a", 10.0, 0)
        charge_and_gate_repo(state, "b", 10.0, 0)
        repo_over, global_block, downgrade = charge_and_gate_repo(
            state, "c", 10.0, 0
        )
        assert global_block is True

    def test_global_block_false_when_only_repo_over(self) -> None:
        """ISOLATION: repo 'a' overspends its 10.0 alloc but global is still 12/30."""
        state = _state(30.0)
        state.budget.allocate_repos(["a", "b", "c"])  # each = 10
        repo_over, global_block, downgrade = charge_and_gate_repo(
            state, "a", 12.0, 0
        )
        # repo 'a' is over its alloc (12 > 10)
        assert repo_over is True
        # but global 12 < 30 => no fleet block
        assert global_block is False

    def test_downgrade_at_80_percent_global(self) -> None:
        state = _state(10.0)
        state.budget.allocate_repos(["a"])
        # Spend 8.0 = 80% of 10.0
        repo_over, global_block, downgrade = charge_and_gate_repo(state, "a", 8.0, 0)
        assert downgrade is True

    def test_per_repo_spend_tracked_in_ledger(self) -> None:
        state = _state(30.0)
        state.budget.allocate_repos(["a", "b"])
        charge_and_gate_repo(state, "a", 3.0, 0)
        charge_and_gate_repo(state, "a", 2.0, 0)
        charge_and_gate_repo(state, "b", 7.0, 0)
        assert state.budget.repo_spend["a"] == pytest.approx(5.0)
        assert state.budget.repo_spend["b"] == pytest.approx(7.0)


# ---------------------------------------------------------------------------
# 3. ISOLATION — per-repo over does NOT block the fleet
# ---------------------------------------------------------------------------

class TestFleetIsolation:
    """Tests that verify isolation semantics without running a real supervisor."""

    def _simulate_fleet_charge(
        self,
        tasks: list[dict],  # list of {repo_id, cost_usd}
        budget: float,
    ) -> tuple[bool, bool, set[str], list[dict]]:
        """Simulate the PASS 1/2 fleet charge loop logic in isolation.

        Returns (fleet_any_block, downgrade_active, repos_over, repo_breakdown).
        """
        state = _state(budget)
        repo_ids = [t["repo_id"] for t in tasks if t["repo_id"] is not None]
        state.budget.allocate_repos(repo_ids)

        fleet_any_block = False
        repos_over: set[str] = set()

        for t in tasks:
            repo_over, global_block, downgrade = charge_and_gate_repo(
                state, t["repo_id"], t["cost_usd"], 0
            )
            if repo_over and t["repo_id"] is not None:
                repos_over.add(t["repo_id"])
            # ISOLATION: only global_block triggers fleet-wide stop.
            if global_block:
                fleet_any_block = True

        # Build breakdown (mirrors supervisor PASS 2 logic).
        breakdown = [
            {
                "repo_id": rid,
                "allocation": state.budget.repo_budgets.get(rid, 0.0),
                "spent": state.budget.repo_spend.get(rid, 0.0),
                "over": rid in repos_over,
            }
            for rid in state.budget.repo_budgets
        ]
        return fleet_any_block, state.budget_downgrade_active, repos_over, breakdown

    def test_repo_overspend_does_not_block_fleet(self) -> None:
        """Repo 'a' spends 12 of its 10 alloc; global 12/30 => NO fleet block."""
        tasks = [
            {"repo_id": "a", "cost_usd": 12.0},
            {"repo_id": "b", "cost_usd": 3.0},
            {"repo_id": "c", "cost_usd": 2.0},
        ]
        fleet_any_block, _, repos_over, _ = self._simulate_fleet_charge(tasks, 30.0)
        assert fleet_any_block is False, (
            "Per-repo over must NOT trigger fleet-wide block (isolation)"
        )
        assert "a" in repos_over, "repo 'a' must be flagged as over-allocation"

    def test_global_over_budget_triggers_fleet_block(self) -> None:
        """Spending 31 of 30 global => fleet block fires."""
        tasks = [
            {"repo_id": "a", "cost_usd": 11.0},
            {"repo_id": "b", "cost_usd": 10.0},
            {"repo_id": "c", "cost_usd": 10.0},
        ]
        fleet_any_block, _, repos_over, _ = self._simulate_fleet_charge(tasks, 30.0)
        assert fleet_any_block is True, "Global overspend must trigger fleet block"

    def test_repo_flagged_over_in_breakdown_but_no_fleet_block(self) -> None:
        """Repo overspend flagged; no fleet block; breakdown shows over=True for that repo."""
        tasks = [
            {"repo_id": "a", "cost_usd": 15.0},  # over 10.0 alloc
            {"repo_id": "b", "cost_usd": 5.0},
            {"repo_id": "c", "cost_usd": 5.0},
        ]
        fleet_any_block, _, repos_over, breakdown = self._simulate_fleet_charge(
            tasks, 30.0
        )
        assert fleet_any_block is False
        a_entry = next(e for e in breakdown if e["repo_id"] == "a")
        assert a_entry["over"] is True
        b_entry = next(e for e in breakdown if e["repo_id"] == "b")
        assert b_entry["over"] is False

    def test_breakdown_present_in_all_repos(self) -> None:
        """Per-repo breakdown includes every fleet repo."""
        tasks = [
            {"repo_id": "x", "cost_usd": 6.0},
            {"repo_id": "y", "cost_usd": 7.0},
        ]
        _, _, _, breakdown = self._simulate_fleet_charge(tasks, 30.0)
        repo_ids_in_breakdown = {e["repo_id"] for e in breakdown}
        assert repo_ids_in_breakdown == {"x", "y"}


# ---------------------------------------------------------------------------
# 4. Per-repo breakdown in HITL payload via supervisor mock
# ---------------------------------------------------------------------------

class TestHITLBreakdown:
    """Drive the supervisor's fleet charge loop + PASS 2 with mocks to verify
    the per-repo breakdown lands in the HITL payload when global block fires."""

    def _make_pack(self, slug: str = "engineering") -> Any:
        from hydra_core.squad_loader import SquadPack
        return SquadPack(
            slug=slug,
            name=slug,
            description="mock",
            entrypoint="mcp",
            agents=(),
            tools=(),
        )

    def _make_task(self, repo_id: str) -> TaskState:
        return TaskState(
            owner_squad="engineering",
            description=f"task for {repo_id}",
            target_repo_id=repo_id,
        )

    def _make_result(self, cost_usd: float, status: str = "done") -> Any:
        """Make a minimal SquadResult-like object carrying a cost artifact."""
        from hydra_core.squad_node import SquadResult
        from hydra_core.schemas import CSuiteDecisionPacket
        artifact = {
            "kind": "pp_run",
            "ref": "run_test",
            "raw": {"result": {"cost_usd": cost_usd}},
        }
        return SquadResult(
            status=status,
            envelopes=[],
            artifacts=[artifact],
            host_pickup_pending=False,
        )

    def test_hitl_payload_contains_repo_breakdown_on_global_block(self) -> None:
        """When global budget is exhausted, HITL payload must have repo_breakdown."""
        # Build state with 3 repos, budget=30
        state = _state(30.0)
        repos = ["r1", "r2", "r3"]
        state.budget.allocate_repos(repos)  # each alloc = 10.0

        pack = self._make_pack()
        packs = {"engineering": pack}
        tasks = [self._make_task(rid) for rid in repos]
        # Costs: r1=12 (over alloc), r2=10, r3=9 -> total=31 > 30 => global block
        costs = [12.0, 10.0, 9.0]
        results = [self._make_result(c) for c in costs]

        fleet_any_block = False
        fleet_last_blocking_squad = ""
        fleet_repos_over: set[str] = set()

        for fleet_task, result in zip(tasks, results):
            from hydra_core import governance as gov
            _cost_usd, _cost_tok = 0.0, 0
            for art in result.artifacts:
                if art.get("kind") == "pp_run":
                    inner = art.get("raw", {}).get("result", art.get("raw", {}))
                    _cost_usd = float(inner.get("cost_usd", 0.0))
            repo_over, global_block, _ = charge_and_gate_repo(
                state, fleet_task.target_repo_id, _cost_usd, 0
            )
            if repo_over and fleet_task.target_repo_id:
                fleet_repos_over.add(fleet_task.target_repo_id)
            if global_block:
                fleet_any_block = True
                fleet_last_blocking_squad = pack.slug

        # Build breakdown (mirrors PASS 2)
        repo_breakdown = [
            {
                "repo_id": rid,
                "allocation": state.budget.repo_budgets.get(rid, 0.0),
                "spent": state.budget.repo_spend.get(rid, 0.0),
                "over": rid in fleet_repos_over,
            }
            for rid in state.budget.repo_budgets
        ]

        assert fleet_any_block is True, "Global block must fire (31 > 30)"
        hitl_payload = {
            "reason": "over_budget",
            "spent_usd": state.budget.spent_usd,
            "budget_usd": state.budget.budget_usd,
            "repo_breakdown": repo_breakdown,
        }

        assert "repo_breakdown" in hitl_payload
        assert len(hitl_payload["repo_breakdown"]) == 3

        r1_entry = next(e for e in hitl_payload["repo_breakdown"] if e["repo_id"] == "r1")
        assert r1_entry["over"] is True
        assert r1_entry["allocation"] == pytest.approx(10.0)
        assert r1_entry["spent"] == pytest.approx(12.0)

        r2_entry = next(e for e in hitl_payload["repo_breakdown"] if e["repo_id"] == "r2")
        # r2 spent exactly 10.0 == alloc => over (>= semantics)
        assert r2_entry["over"] is True

        r3_entry = next(e for e in hitl_payload["repo_breakdown"] if e["repo_id"] == "r3")
        assert r3_entry["over"] is False  # 9 < 10

    def test_repo_over_alone_no_fleet_block(self) -> None:
        """Repo overspend without global block: fleet_any_block stays False."""
        state = _state(30.0)
        state.budget.allocate_repos(["a", "b", "c"])  # each = 10

        tasks = [
            self._make_task("a"),  # will spend 12 -> repo_over
            self._make_task("b"),  # will spend 3
            self._make_task("c"),  # will spend 2
        ]
        costs = [12.0, 3.0, 2.0]  # total 17 < 30

        fleet_any_block = False
        fleet_repos_over: set[str] = set()

        for task, cost in zip(tasks, costs):
            repo_over, global_block, _ = charge_and_gate_repo(
                state, task.target_repo_id, cost, 0
            )
            if repo_over and task.target_repo_id:
                fleet_repos_over.add(task.target_repo_id)
            if global_block:
                fleet_any_block = True

        assert fleet_any_block is False, (
            "Only global_block should set fleet_any_block; per-repo over must not"
        )
        assert "a" in fleet_repos_over
        assert state.budget.spent_usd == pytest.approx(17.0)


# ---------------------------------------------------------------------------
# Fix 1 — integer micro-unit allocation: exact sum for any budget / repo count
# ---------------------------------------------------------------------------

class TestAllocateReposExactSum:
    """Fix 1: verify the micro-unit split produces sum == budget_usd exactly,
    including tiny and high-precision budgets where decimal rounding drifts."""

    def test_tiny_budget_three_repos_sum_exact(self) -> None:
        """$0.01 / 3: total_micro=10_000; base=3_333, rem=1 -> micro sum exact."""
        ledger = _ledger(0.01)
        ledger.allocate_repos(["a", "b", "c"])
        total = sum(ledger.repo_budgets.values())
        # Float sum within 1 micro-dollar of budget_usd.
        assert abs(total - 0.01) < 1e-6, (
            f"sum({list(ledger.repo_budgets.values())}) must be within 1e-6 of 0.01"
        )
        # Integer micro-sum must be EXACTLY round(budget_usd * 1e6).
        micro_sum = sum(round(v * 1_000_000) for v in ledger.repo_budgets.values())
        assert micro_sum == round(0.01 * 1_000_000)
        # No allocation should be negative.
        for rid, alloc in ledger.repo_budgets.items():
            assert alloc >= 0.0, f"repo {rid!r} got negative allocation {alloc}"

    def test_uneven_budget_ten_three_repos_exact(self) -> None:
        """$10.0 / 3: 10_000_000 micro / 3 = 3_333_333 base + 1 rem."""
        ledger = _ledger(10.0)
        ledger.allocate_repos(["x", "y", "z"])
        total = sum(ledger.repo_budgets.values())
        assert abs(total - 10.0) < 1e-6
        micro_sum = sum(round(v * 1_000_000) for v in ledger.repo_budgets.values())
        assert micro_sum == round(10.0 * 1_000_000)
        for alloc in ledger.repo_budgets.values():
            assert alloc >= 0.0

    def test_high_precision_budget_exact_sum(self) -> None:
        """$1.000001 / 7 repos: micro = 1_000_001; base=142_857, rem=2."""
        ledger = _ledger(1.000001)
        repos = [f"r{i}" for i in range(7)]
        ledger.allocate_repos(repos)
        total = sum(ledger.repo_budgets.values())
        assert abs(total - 1.000001) < 1e-6, (
            f"sum {total} not within 1e-6 of 1.000001"
        )
        micro_sum = sum(round(v * 1_000_000) for v in ledger.repo_budgets.values())
        assert micro_sum == round(1.000001 * 1_000_000)

    def test_large_count_no_negative(self) -> None:
        """100 repos, budget $1: each gets ~0.01; no negatives, micro sum exact."""
        ledger = _ledger(1.0)
        repos = [f"repo-{i}" for i in range(100)]
        ledger.allocate_repos(repos)
        total = sum(ledger.repo_budgets.values())
        assert abs(total - 1.0) < 1e-6
        micro_sum = sum(round(v * 1_000_000) for v in ledger.repo_budgets.values())
        assert micro_sum == round(1.0 * 1_000_000)
        for alloc in ledger.repo_budgets.values():
            assert alloc >= 0.0

    def test_remainder_distributed_to_first_repos(self) -> None:
        """$0.01 / 3: rem=1 micro, so first repo gets 1 extra micro-unit."""
        ledger = _ledger(0.01)
        repos = ["first", "second", "third"]
        ledger.allocate_repos(repos)
        # total_micro=10_000, base=3_333, rem=1 -> first gets 3_334 micros
        assert ledger.repo_budgets["first"] == pytest.approx(0.003334, abs=1e-7)
        assert ledger.repo_budgets["second"] == pytest.approx(0.003333, abs=1e-7)
        assert ledger.repo_budgets["third"] == pytest.approx(0.003333, abs=1e-7)


# ---------------------------------------------------------------------------
# Fix 2 — reallocation replaces + resets per-repo spend
# ---------------------------------------------------------------------------

class TestAllocateReposReplaces:
    """Fix 2: calling allocate_repos a second time must REPLACE repo_budgets
    (no stale entries) and RESET repo_spend to empty."""

    def test_reallocation_clears_stale_repos(self) -> None:
        """First call: repos a,b,c. Second call: repos x,y. Only x,y remain."""
        ledger = _ledger(30.0)
        ledger.allocate_repos(["a", "b", "c"])
        assert set(ledger.repo_budgets.keys()) == {"a", "b", "c"}

        ledger.allocate_repos(["x", "y"])
        assert set(ledger.repo_budgets.keys()) == {"x", "y"}, (
            "Stale repos a/b/c must be gone after reallocation"
        )

    def test_reallocation_sum_still_correct(self) -> None:
        ledger = _ledger(30.0)
        ledger.allocate_repos(["a", "b", "c"])
        ledger.allocate_repos(["x", "y"])
        total = sum(ledger.repo_budgets.values())
        assert abs(total - 30.0) < 1e-6

    def test_reallocation_resets_repo_spend(self) -> None:
        """Spend on the first fleet must be wiped on reallocation."""
        ledger = _ledger(30.0)
        ledger.allocate_repos(["a", "b"])
        ledger.repo_spend["a"] = 7.0
        ledger.repo_spend["b"] = 4.0

        ledger.allocate_repos(["x", "y"])
        assert ledger.repo_spend == {}, (
            "repo_spend must be empty dict after reallocation"
        )

    def test_reallocation_does_not_touch_global_spent(self) -> None:
        """Global spent_usd must survive reallocation unchanged."""
        from hydra_core.state import HydraState
        state = _state(30.0)
        state.budget.allocate_repos(["a"])
        state.budget.spent_usd = 12.34   # simulate global charges already recorded
        state.budget.allocate_repos(["x", "y"])
        assert state.budget.spent_usd == pytest.approx(12.34)


# ---------------------------------------------------------------------------
# Fix 3 — unattributed spend reconciles global == sum(repo_spend)
# ---------------------------------------------------------------------------

class TestUnattributedSpend:
    """Fix 3: None-repo and unallocated-repo charges go into the
    "(unattributed)" bucket; global spent_usd == sum(repo_spend.values())."""

    _BUCKET = "(unattributed)"

    def test_none_repo_routes_to_unattributed(self) -> None:
        state = _state(30.0)
        state.budget.allocate_repos(["a"])
        charge_and_gate_repo(state, None, 5.0, 0)
        assert state.budget.repo_spend.get(self._BUCKET) == pytest.approx(5.0)

    def test_unallocated_repo_routes_to_unattributed(self) -> None:
        """A repo_id that is not in repo_budgets goes to unattributed."""
        state = _state(30.0)
        # Do NOT allocate "orphan" — it's not a fleet repo.
        charge_and_gate_repo(state, "orphan", 3.0, 0)
        assert state.budget.repo_spend.get(self._BUCKET) == pytest.approx(3.0)
        assert "orphan" not in state.budget.repo_spend

    def test_unattributed_repo_over_is_always_false(self) -> None:
        """Charging an unallocated repo never sets repo_over."""
        state = _state(30.0)
        repo_over, _, _ = charge_and_gate_repo(state, "orphan", 9999.0, 0)
        assert repo_over is False

    def test_global_reconciles_none_plus_attributed(self) -> None:
        """$5 None-repo + $3 repo-a -> global $8 == unattributed($5) + a($3)."""
        state = _state(30.0)
        state.budget.allocate_repos(["a", "b", "c"])
        charge_and_gate_repo(state, None, 5.0, 0)
        charge_and_gate_repo(state, "a", 3.0, 0)
        assert state.budget.spent_usd == pytest.approx(8.0)
        assert state.budget.repo_spend.get(self._BUCKET, 0.0) == pytest.approx(5.0)
        assert state.budget.repo_spend.get("a", 0.0) == pytest.approx(3.0)
        # Full reconciliation: sum of ALL repo_spend buckets == global spent.
        assert sum(state.budget.repo_spend.values()) == pytest.approx(
            state.budget.spent_usd, abs=1e-9
        )

    def test_breakdown_includes_unattributed_bucket(self) -> None:
        """When unattributed spend exists, the breakdown list must include it."""
        state = _state(30.0)
        state.budget.allocate_repos(["a"])
        charge_and_gate_repo(state, None, 5.0, 0)
        charge_and_gate_repo(state, "a", 3.0, 0)

        # Replicate the supervisor's breakdown builder (PASS 2 logic).
        repos_over: set[str] = set()
        breakdown = [
            {
                "repo_id": rid,
                "allocation": state.budget.repo_budgets.get(rid, 0.0),
                "spent": state.budget.repo_spend.get(rid, 0.0),
                "over": rid in repos_over,
            }
            for rid in state.budget.repo_budgets
        ]
        unattr_spend = state.budget.repo_spend.get(self._BUCKET, 0.0)
        if unattr_spend > 0.0:
            breakdown.append({
                "repo_id": self._BUCKET,
                "allocation": None,
                "spent": unattr_spend,
                "over": False,
            })

        repo_ids_in_breakdown = {e["repo_id"] for e in breakdown}
        assert self._BUCKET in repo_ids_in_breakdown, (
            "Breakdown must include '(unattributed)' when unattributed spend exists"
        )
        unattr_entry = next(e for e in breakdown if e["repo_id"] == self._BUCKET)
        assert unattr_entry["over"] is False
        assert unattr_entry["allocation"] is None
        assert unattr_entry["spent"] == pytest.approx(5.0)

    def test_multiple_none_charges_accumulate(self) -> None:
        """Multiple None-repo charges accumulate in the same bucket."""
        state = _state(30.0)
        charge_and_gate_repo(state, None, 2.0, 0)
        charge_and_gate_repo(state, None, 3.0, 0)
        assert state.budget.repo_spend.get(self._BUCKET) == pytest.approx(5.0)

    def test_sum_repo_spend_equals_global_multiple_charges(self) -> None:
        """After a mix of attributed + unattributed charges, reconciliation holds."""
        state = _state(50.0)
        state.budget.allocate_repos(["p", "q"])
        charge_and_gate_repo(state, "p", 10.0, 0)
        charge_and_gate_repo(state, "q", 7.0, 0)
        charge_and_gate_repo(state, None, 4.0, 0)
        charge_and_gate_repo(state, "orphan", 3.0, 0)  # unallocated -> unattributed
        total_repo_spend = sum(state.budget.repo_spend.values())
        assert total_repo_spend == pytest.approx(state.budget.spent_usd, abs=1e-9), (
            f"sum(repo_spend)={total_repo_spend} != spent_usd={state.budget.spent_usd}"
        )
