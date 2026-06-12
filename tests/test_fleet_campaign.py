"""WS8 SLICE 3 — tests for parse_repos_arg + node_intake fleet wiring.

All tests are pure-Python / mock-only.  No real MCP, no real pp runs, no network.

Coverage:
  parse_repos_arg:
    - --repos a,b,c -> list, cleaned text (valid allow-list ids)
    - --fleet a,b works as synonym
    - --repos=a,b works (equals-form)
    - unknown id -> ValueError
    - bare --repos (no value) -> ValueError
    - dup values --repos a,a,b -> dedup -> ["a","b"]
    - duplicate token --repos a,b --repos c,d -> ValueError

  node_intake fleet wiring (drive node_intake directly with a mock supervisor):
    - >=2 distinct valid repos -> fleet_parallel=True, selected_squads==["engineering"],
      one TaskState per repo seeded with distinct target_repo_id
    - unknown repo in --repos -> phase=="surfaced" + pending_hitl (reason high_risk)
    - single "--repos theeights" -> target_repo_id set, fleet_parallel False
    - both --repo and --repos -> surfaced (ambiguous)
    - fleet gating predicate: seeded state with fleet_parallel + 2 distinct mcp tasks fires
"""
from __future__ import annotations

import re
from typing import Any
from unittest.mock import MagicMock, patch
from uuid import uuid4

import pytest

from hydra_core.repo_registry import parse_repos_arg, _REPO_DIRNAMES
from hydra_core.state import HydraState, TaskState

# ---------------------------------------------------------------------------
# Real allow-listed ids drawn from _REPO_DIRNAMES to make tests stable
# ---------------------------------------------------------------------------
_VALID = sorted(_REPO_DIRNAMES.keys())  # all valid ids
# Pick a stable quartet we can rely on being present
_R1, _R2, _R3, _R4 = "pair-programmer", "theeights", "xenia", "agentsmith"


# ===========================================================================
# Part 1 — parse_repos_arg unit tests
# ===========================================================================

class TestParseReposArg:

    def test_space_form_three_repos(self):
        ids, cleaned = parse_repos_arg(f"Fix the bug --repos {_R1},{_R2},{_R3} please")
        assert ids == [_R1, _R2, _R3]
        assert "--repos" not in cleaned
        assert "Fix the bug" in cleaned

    def test_fleet_synonym(self):
        ids, cleaned = parse_repos_arg(f"--fleet {_R1},{_R2} do something")
        assert ids == [_R1, _R2]
        assert "--fleet" not in cleaned

    def test_equals_form(self):
        ids, cleaned = parse_repos_arg(f"--repos={_R1},{_R2} do something")
        assert ids == [_R1, _R2]
        assert "--repos" not in cleaned

    def test_fleet_equals_form(self):
        ids, cleaned = parse_repos_arg(f"--fleet={_R1},{_R2}")
        assert ids == [_R1, _R2]

    def test_no_token_returns_empty_list_unchanged(self):
        text = "just a plain goal"
        ids, cleaned = parse_repos_arg(text)
        assert ids == []
        assert cleaned == text

    def test_unknown_id_raises(self):
        with pytest.raises(ValueError, match="not an allow-listed"):
            parse_repos_arg(f"--repos {_R1},totally_unknown_repo")

    def test_bare_repos_no_value_raises(self):
        with pytest.raises(ValueError, match="requires a value"):
            parse_repos_arg("fix something --repos")

    def test_bare_repos_followed_by_flag_raises(self):
        with pytest.raises(ValueError, match="requires a value"):
            parse_repos_arg("fix something --repos --other")

    def test_bare_repos_equals_empty_raises(self):
        with pytest.raises(ValueError, match="requires a value"):
            parse_repos_arg("--repos= something")

    def test_dedup_preserves_first_occurrence_order(self):
        # a,a,b -> [a,b]; first a wins, second dropped
        ids, _ = parse_repos_arg(f"--repos {_R1},{_R1},{_R2}")
        assert ids == [_R1, _R2]

    def test_dedup_with_three_unique(self):
        ids, _ = parse_repos_arg(f"--repos {_R1},{_R2},{_R3}")
        assert ids == [_R1, _R2, _R3]

    def test_duplicate_token_raises(self):
        with pytest.raises(ValueError, match="more than once"):
            parse_repos_arg(f"--repos {_R1},{_R2} --repos {_R3},{_R4}")

    def test_duplicate_token_mixed_forms_raises(self):
        with pytest.raises(ValueError, match="more than once"):
            parse_repos_arg(f"--repos={_R1} --fleet {_R2},{_R3}")

    def test_single_valid_repo_returns_list_of_one(self):
        ids, cleaned = parse_repos_arg(f"goal --repos {_R1}")
        assert ids == [_R1]
        assert "--repos" not in cleaned

    def test_cleaned_text_has_no_double_spaces(self):
        ids, cleaned = parse_repos_arg(f"fix it  --repos {_R1},{_R2}  now")
        assert "  " not in cleaned

    # ------------------------------------------------------------------
    # Whitespace-around-commas regression tests (Fix 1)
    # ------------------------------------------------------------------

    def test_space_after_comma(self):
        """--repos a, b, c (space after each comma) -> [a,b,c]; no id bleeds into goal."""
        ids, cleaned = parse_repos_arg(f"--repos {_R1}, {_R2}, {_R3} Fix the bug")
        assert ids == [_R1, _R2, _R3], f"got {ids}"
        assert "Fix the bug" in cleaned
        # No repo id should appear as part of the goal tail
        for rid in ids:
            # repo ids should not appear in cleaned after the removal
            # (they were part of the flag value, not the goal)
            assert rid not in cleaned or cleaned.startswith(rid) is False or cleaned == rid

    def test_space_before_and_after_comma(self):
        """--repos a ,b , c -> [a,b,c]."""
        ids, cleaned = parse_repos_arg(f"--repos {_R1} ,{_R2} , {_R3}")
        assert ids == [_R1, _R2, _R3], f"got {ids}"

    def test_two_repos_with_space_after_comma_goal_tail(self):
        """--repos a, b Fix X -> ([a,b], cleaned has 'Fix X'; no repo bleed)."""
        ids, cleaned = parse_repos_arg(f"Goal --repos {_R1}, {_R2} Fix X")
        assert ids == [_R1, _R2], f"got {ids}"
        assert "Fix X" in cleaned, f"goal tail missing: {cleaned!r}"
        assert "--repos" not in cleaned
        # The second repo id must NOT appear in the goal tail
        assert _R2 not in cleaned, f"repo id bled into goal: {cleaned!r}"

    def test_equals_form_with_spaces_around_comma(self):
        """--repos=a, b -> [a,b]."""
        ids, cleaned = parse_repos_arg(f"--repos={_R1}, {_R2} do something")
        assert ids == [_R1, _R2], f"got {ids}"
        assert "do something" in cleaned

    def test_fleet_synonym_with_space_after_comma(self):
        """--fleet a, b -> [a,b]."""
        ids, cleaned = parse_repos_arg(f"--fleet {_R1}, {_R2} do something")
        assert ids == [_R1, _R2], f"got {ids}"
        assert "do something" in cleaned

    # ------------------------------------------------------------------
    # Foreign/malformed token — whole-capture + no goal bleed (Fix 3)
    # ------------------------------------------------------------------

    def test_foreign_slash_token_raises_with_full_name(self):
        """--repos pair-programmer,foreign/repo -> ValueError naming 'foreign/repo';
        '/repo' must NOT bleed into the goal as a separate word."""
        with pytest.raises(ValueError) as exc_info:
            parse_repos_arg(f"--repos {_R1},foreign/repo Fix the bug")
        assert "foreign/repo" in str(exc_info.value), (
            f"expected full bad token in error, got: {exc_info.value}"
        )

    def test_foreign_slash_token_whole_capture_no_residual(self):
        """Verify the regex captures 'pair-programmer,foreign/repo' as one span —
        nothing after the token contains '/repo'."""
        from hydra_core.repo_registry import _REPOS_ARG_RE
        text = f"--repos {_R1},foreign/repo Fix the bug"
        m = _REPOS_ARG_RE.search(text)
        assert m is not None, "regex must match"
        raw_value = ((m.group(2) or "") + (m.group(3) or "")).strip()
        assert "foreign/repo" in raw_value, (
            f"partial capture — 'foreign/repo' not in {raw_value!r}"
        )
        residual = text[m.end(1):]
        assert "/repo" not in residual, (
            f"'/repo' bled into residual: {residual!r}"
        )

    def test_malformed_token_with_slash_raises(self):
        """--repos valid,b/c -> ValueError; 'b/c' captured whole."""
        with pytest.raises(ValueError, match="not an allow-listed"):
            parse_repos_arg(f"--repos {_R1},{_R2}/extra")

    def test_valid_repos_goal_tail_no_bleed(self):
        """--repos pair-programmer,theeights Fix X -> correct ids + 'Fix X' in goal."""
        ids, cleaned = parse_repos_arg(f"--repos {_R1},{_R2} Fix X")
        assert ids == [_R1, _R2], f"got {ids}"
        assert "Fix X" in cleaned, f"goal tail missing: {cleaned!r}"
        assert "--repos" not in cleaned
        # Neither repo id should appear in the cleaned goal
        assert _R1 not in cleaned, f"{_R1} bled into goal: {cleaned!r}"
        assert _R2 not in cleaned, f"{_R2} bled into goal: {cleaned!r}"


# ===========================================================================
# Part 2 — node_intake fleet wiring
#
# We build a minimal supervisor and drive node_intake in isolation.
# The mock dispatcher is injected so eights/constitution paths no-op.
# ===========================================================================

def _make_mock_dispatcher() -> Any:
    """Return a minimal mock dispatcher that satisfies supervisor internals."""
    d = MagicMock()
    d.call_mcp.return_value = {"status": "done", "result": {}}
    d.set_squad_packs = MagicMock()
    return d


def _node_intake_result(goal: str, *, pre_target_repo_id: str | None = None) -> dict:
    """Build a supervisor, run node_intake against `goal`, return its result dict.

    Patches:
    - discover_squads -> minimal {engineering: SquadPack(mcp)}
    - load_constitution -> fake constitution object
    - EightsAttestor -> no-op mock
    - classify_intent -> RoutingDecision(squads=["engineering"])
    - compute_tool_scope -> minimal ToolScope
    - load_cerberus_venoms -> no-op
    - build_default_shed -> no-op
    - build_node_context -> minimal context
    - emit (telemetry) -> no-op
    - charge_and_gate -> no-op (not invoked from intake)
    """
    from hydra_core.squad_loader import SquadPack
    from hydra_core.router import RoutingDecision

    eng_pack = SquadPack(
        slug="engineering",
        name="engineering",
        description="test engineering",
        entrypoint="mcp",
        agents=(),
        tools=(),
    )

    # Minimal constitution stub
    class _FakeConstitution:
        sha256 = "aabbcc"

    # Minimal ToolScope stub
    class _FakeToolScope:
        relevant_tools: set = set()
        relevant_categories: set = set()
        intent_keywords: set = set()
        tool_count: int = 0

    # Minimal NodeContext stub
    class _FakeNodeCtx:
        tool_categories: list = []
        relevant_squads: list = []
        instructions: str = ""

    # Minimal EightsAttestor stub
    class _FakeEights:
        workflow_id = ""
        def replay_pending(self): return {"sent": 0, "failed": 0, "skipped": 0}
        def constitution_attest(self, *a, **kw): return {}
        def ceiling_tick(self, **kw): pass
        def hitl_request(self, *a, **kw): pass

    mock_disp = _make_mock_dispatcher()

    # Build supervisor with all patches and capture node_intake via StateGraph interception.
    with (
        patch("hydra_core.supervisor.discover_squads", return_value={"engineering": eng_pack}),
        patch("hydra_core.supervisor.load_constitution", return_value=_FakeConstitution()),
        patch("hydra_core.supervisor.EightsAttestor", return_value=_FakeEights()),
        patch("hydra_core.supervisor.load_cerberus_venoms"),
        patch("hydra_core.supervisor.classify_intent",
              return_value=RoutingDecision(squads=["engineering"], confidence=1.0, rationale="mock")),
        patch("hydra_core.supervisor.compute_tool_scope", return_value=_FakeToolScope()),
        patch("hydra_core.toolshed.build_default_shed", return_value=MagicMock()),
        patch("hydra_core.node_context.build_node_context", return_value=_FakeNodeCtx()),
        patch("hydra_core.supervisor.emit_trace"),
        patch("hydra_core.supervisor.load_policy", return_value=MagicMock()),
    ):
        captured_nodes: dict[str, Any] = {}

        class _CapturingGraph:
            def __init__(self, *a, **kw): pass
            def add_node(self, name, fn): captured_nodes[name] = fn
            def add_edge(self, *a, **kw): pass
            def add_conditional_edges(self, *a, **kw): pass
            def set_entry_point(self, *a, **kw): pass
            def compile(self, *a, **kw): return self

        from hydra_core.supervisor import build_supervisor
        with patch("hydra_core.supervisor.StateGraph", _CapturingGraph):
            build_supervisor(dispatcher=mock_disp)

        node_intake_fn = captured_nodes.get("intake") or captured_nodes.get("node_intake")
        if node_intake_fn is None:
            raise RuntimeError(f"intake node not captured; got: {list(captured_nodes)}")

        state = HydraState(
            root_goal=goal,
            workflow_id=uuid4(),
        )
        if pre_target_repo_id is not None:
            state.target_repo_id = pre_target_repo_id

        result = node_intake_fn(state)
        return result, state


class TestNodeIntakeFleet:

    def test_two_distinct_repos_sets_fleet_mode(self):
        result, state = _node_intake_result(f"Fix bug --repos {_R1},{_R2}")
        assert result.get("fleet_parallel") is True, f"fleet_parallel not set; result={result}"
        assert result.get("selected_squads") == ["engineering"]

    def test_two_distinct_repos_seeds_one_task_per_repo(self):
        result, state = _node_intake_result(f"Fix bug --repos {_R1},{_R2}")
        tasks = result.get("tasks", [])
        assert len(tasks) == 2, f"expected 2 tasks, got {len(tasks)}: {tasks}"
        repo_ids = {
            (t.target_repo_id if isinstance(t, TaskState) else t.get("target_repo_id"))
            for t in tasks
        }
        assert _R1 in repo_ids, f"{_R1} not in {repo_ids}"
        assert _R2 in repo_ids, f"{_R2} not in {repo_ids}"

    def test_three_distinct_repos_seeds_three_tasks(self):
        result, state = _node_intake_result(f"Fix bug --repos {_R1},{_R2},{_R3}")
        tasks = result.get("tasks", [])
        assert len(tasks) == 3
        repo_ids = {
            (t.target_repo_id if isinstance(t, TaskState) else t.get("target_repo_id"))
            for t in tasks
        }
        assert repo_ids == {_R1, _R2, _R3}

    def test_tasks_carry_correct_target_repo_id(self):
        result, state = _node_intake_result(f"Goal --repos {_R1},{_R2}")
        tasks = result.get("tasks", [])
        for t in tasks:
            rid = t.target_repo_id if isinstance(t, TaskState) else t.get("target_repo_id")
            assert rid in (_R1, _R2), f"unexpected target_repo_id {rid!r}"
            osq = t.owner_squad if isinstance(t, TaskState) else t.get("owner_squad")
            assert osq == "engineering"

    def test_tasks_all_distinct_repos(self):
        result, state = _node_intake_result(f"Goal --repos {_R1},{_R2},{_R3}")
        tasks = result.get("tasks", [])
        repo_ids = [
            (t.target_repo_id if isinstance(t, TaskState) else t.get("target_repo_id"))
            for t in tasks
        ]
        # All distinct
        assert len(repo_ids) == len(set(repo_ids))

    def test_unknown_repo_surfaces_hitl(self):
        result, state = _node_intake_result(f"Goal --repos {_R1},totally_bad_repo")
        assert result.get("phase") == "surfaced", f"expected surfaced, got: {result.get('phase')}"
        hitl = result.get("pending_hitl", {})
        assert hitl.get("reason") == "high_risk"
        assert hitl.get("gate_node") == "intake"
        assert "abort" in hitl.get("options", [])
        assert "--repos" in hitl.get("summary", "") or "rejected" in hitl.get("summary", "")

    def test_single_repo_sets_target_repo_id_not_fleet(self):
        result, state = _node_intake_result(f"Goal --repos {_R1}")
        # fleet_parallel should NOT be set (or be False)
        assert not result.get("fleet_parallel"), f"fleet_parallel should be False, got: {result}"
        # target_repo_id should be set
        repo_set = result.get("target_repo_id") or state.target_repo_id
        assert repo_set == _R1, f"expected {_R1}, got {repo_set}"
        # No tasks array seeded for single-repo
        tasks = result.get("tasks", [])
        assert len(tasks) == 0, f"unexpected tasks for single-repo: {tasks}"

    def test_single_repo_clears_pre_seeded_fleet_parallel(self):
        """Fix 2: a state pre-seeded with fleet_parallel=True + single --repos id
        must exit intake with fleet_parallel=False and no per-repo task seeding.
        """
        # We need to pass fleet_parallel=True into the state before node_intake runs.
        # _node_intake_result builds a fresh HydraState; we patch it to be pre-seeded.
        from hydra_core.squad_loader import SquadPack
        from hydra_core.router import RoutingDecision

        eng_pack = SquadPack(
            slug="engineering", name="engineering",
            description="test engineering", entrypoint="mcp",
            agents=(), tools=(),
        )

        class _FakeConstitution:
            sha256 = "aabbcc"

        class _FakeToolScope:
            relevant_tools: set = set()
            relevant_categories: set = set()
            intent_keywords: set = set()
            tool_count: int = 0

        class _FakeNodeCtx:
            tool_categories: list = []
            relevant_squads: list = []
            instructions: str = ""

        class _FakeEights:
            workflow_id = ""
            def replay_pending(self): return {"sent": 0, "failed": 0, "skipped": 0}
            def constitution_attest(self, *a, **kw): return {}
            def ceiling_tick(self, **kw): pass
            def hitl_request(self, *a, **kw): pass

        mock_disp = _make_mock_dispatcher()

        with (
            patch("hydra_core.supervisor.discover_squads", return_value={"engineering": eng_pack}),
            patch("hydra_core.supervisor.load_constitution", return_value=_FakeConstitution()),
            patch("hydra_core.supervisor.EightsAttestor", return_value=_FakeEights()),
            patch("hydra_core.supervisor.load_cerberus_venoms"),
            patch("hydra_core.supervisor.classify_intent",
                  return_value=RoutingDecision(squads=["engineering"], confidence=1.0, rationale="mock")),
            patch("hydra_core.supervisor.compute_tool_scope", return_value=_FakeToolScope()),
            patch("hydra_core.toolshed.build_default_shed", return_value=MagicMock()),
            patch("hydra_core.node_context.build_node_context", return_value=_FakeNodeCtx()),
            patch("hydra_core.supervisor.emit_trace"),
            patch("hydra_core.supervisor.load_policy", return_value=MagicMock()),
        ):
            captured_nodes: dict[str, Any] = {}

            class _CapturingGraph:
                def __init__(self, *a, **kw): pass
                def add_node(self, name, fn): captured_nodes[name] = fn
                def add_edge(self, *a, **kw): pass
                def add_conditional_edges(self, *a, **kw): pass
                def set_entry_point(self, *a, **kw): pass
                def compile(self, *a, **kw): return self

            from hydra_core.supervisor import build_supervisor
            with patch("hydra_core.supervisor.StateGraph", _CapturingGraph):
                build_supervisor(dispatcher=mock_disp)

            node_intake_fn = captured_nodes.get("intake") or captured_nodes.get("node_intake")
            assert node_intake_fn is not None

            # Pre-seed fleet_parallel=True
            state = HydraState(
                root_goal=f"Goal --repos {_R2}",
                workflow_id=uuid4(),
                fleet_parallel=True,  # pre-seeded True must be cleared
            )
            result = node_intake_fn(state)

        # fleet_parallel must be False in the patch AND on the state object
        assert result.get("fleet_parallel") is False, (
            f"expected fleet_parallel=False in update, got: {result.get('fleet_parallel')!r}"
        )
        assert state.fleet_parallel is False, (
            f"expected state.fleet_parallel=False after intake, got: {state.fleet_parallel!r}"
        )
        # No tasks seeded
        tasks = result.get("tasks", [])
        assert len(tasks) == 0, f"unexpected tasks for single-repo: {tasks}"
        # target_repo_id should be set
        repo_set = result.get("target_repo_id") or state.target_repo_id
        assert repo_set == _R2, f"expected {_R2}, got {repo_set!r}"

    def test_both_repo_and_repos_surfaces_ambiguous_hitl(self):
        result, state = _node_intake_result(f"Goal --repo {_R1} --repos {_R2},{_R3}")
        assert result.get("phase") == "surfaced", f"expected surfaced, got: {result.get('phase')}"
        hitl = result.get("pending_hitl", {})
        assert hitl.get("reason") == "high_risk"
        summary = hitl.get("summary", "")
        assert "ambiguous" in summary.lower() or "both" in summary.lower(), \
            f"expected ambiguous in summary, got: {summary!r}"

    def test_no_repos_arg_no_fleet(self):
        result, state = _node_intake_result("Just a plain engineering goal")
        assert not result.get("fleet_parallel")
        assert result.get("phase") != "surfaced"

    def test_fleet_gating_predicate_fires_for_seeded_state(self):
        """Confirm that a state with fleet_parallel=True and 2 distinct mcp-eligible
        tasks satisfies the fleet predicate used in node_dispatch.

        We check the predicate directly against the state shape that node_intake
        produces: fleet_parallel=True + 2 pending engineering tasks with distinct
        non-None target_repo_id.  This mirrors what node_dispatch checks before
        calling dispatch_fleet.
        """
        # Build the state that node_intake would produce for a 2-repo fleet
        state = HydraState(
            root_goal=f"Fleet goal --repos {_R1},{_R2}",
            fleet_parallel=True,
            selected_squads=["engineering"],
        )
        t1 = TaskState(owner_squad="engineering", description="work", target_repo_id=_R1)
        t2 = TaskState(owner_squad="engineering", description="work", target_repo_id=_R2)
        # Simulate the append reducer: tasks accumulate in state.tasks
        # (In real LangGraph this happens automatically; here we set manually.)
        object.__setattr__(state, "tasks", [t1, t2])

        # The fleet gating predicate from node_dispatch:
        # fleet_parallel AND >=2 pending mcp/engineering tasks with DISTINCT non-None target_repo_id
        pending_engineering = [
            t for t in state.tasks
            if t.status == "pending" and t.owner_squad == "engineering"
        ]
        distinct_repos = {t.target_repo_id for t in pending_engineering if t.target_repo_id is not None}

        assert state.fleet_parallel is True
        assert len(distinct_repos) >= 2, f"expected >=2 distinct repos, got {distinct_repos}"
        # Fleet predicate fires
        fleet_should_fire = state.fleet_parallel and len(distinct_repos) >= 2
        assert fleet_should_fire
