"""B7 — pp-harness lock release on supervisor surface.

Verifies that `node_postcheck` drains `state.open_pp_runs` by calling
`pp-daemon.finalize_run(status="aborted")` on every entry whenever the
workflow surfaces. Closes the bootstrap-session failure mode where a
crashed supervisor left `<project>/.harness/.lock` orphaned past the
pp-daemon's TTL, blocking the next `/pp:run` on the same project until
the operator manually removed the lock file.

Covers three behaviors:
  * No drain on a clean `done` path (pp owns the runs from start_run on).
  * Full drain on `surfaced` when finalize_run succeeds.
  * Partial drain on `surfaced` when finalize_run raises for some entries
    — failed entries remain on `state.open_pp_runs` so an operator-driven
    `force_unlock` (pair-programmer P3) can still salvage the lock.
"""
from __future__ import annotations

from typing import Any

import pytest

from hydra_core.squad_node import abort_open_pp_runs
from hydra_core.state import HydraState


class _RecordingDispatcher:
    """Records every (server, tool, args) tuple call_mcp receives.

    Configurable to either succeed-all, fail-all, or fail-on-specific-run_id
    so we can exercise the partial-drain path. Other Dispatcher methods are
    unused by `abort_open_pp_runs` and stubbed with `NotImplementedError`.
    """

    def __init__(self, *, fail_run_ids: set[str] | None = None) -> None:
        self.calls: list[tuple[str, str, dict[str, Any]]] = []
        self.fail_run_ids: set[str] = fail_run_ids or set()

    def call_mcp(self, server: str, tool: str, args: dict[str, Any]) -> dict[str, Any]:
        self.calls.append((server, tool, args))
        rid = args.get("run_id")
        if isinstance(rid, str) and rid in self.fail_run_ids:
            raise RuntimeError(f"simulated pp-daemon failure for {rid}")
        return {"status": "done", "tool": tool, "result": {"run_id": rid, "status": "aborted"}}

    def emit_claude_prompt(self, *_a: Any, **_k: Any) -> Any:  # pragma: no cover
        raise NotImplementedError

    def invoke_claude_skill(self, *_a: Any, **_k: Any) -> Any:  # pragma: no cover
        raise NotImplementedError

    def spawn_subprocess(self, *_a: Any, **_k: Any) -> Any:  # pragma: no cover
        raise NotImplementedError


def _state_with_open_runs(*entries: dict[str, str]) -> HydraState:
    s = HydraState(root_goal="test")
    s.open_pp_runs = list(entries)
    return s


def test_abort_drains_every_entry_on_success() -> None:
    state = _state_with_open_runs(
        {"run_id": "run_A", "project_path": "C:/proj/a"},
        {"run_id": "run_B", "project_path": "C:/proj/b"},
    )
    dispatcher = _RecordingDispatcher()

    drained = abort_open_pp_runs(state, dispatcher, reason="envelope_ceiling")

    assert len(drained) == 2
    assert state.open_pp_runs == []
    # Both calls must hit pp-daemon.finalize_run with status="aborted"
    assert len(dispatcher.calls) == 2
    for server, tool, args in dispatcher.calls:
        assert server == "pp-daemon"
        assert tool == "finalize_run"
        assert args["status"] == "aborted"
        assert args["reason"] == "envelope_ceiling"
        assert args["run_id"] in {"run_A", "run_B"}
        assert args["project_path"] in {"C:/proj/a", "C:/proj/b"}


def test_abort_partial_drain_leaves_failed_entries_in_state() -> None:
    # Two runs registered; pp-daemon will raise for run_B (e.g., it was
    # already finalized externally, or the daemon is mid-restart). The
    # successful entry MUST be drained; the failed entry MUST stay on
    # state so an operator force_unlock can finish the cleanup.
    state = _state_with_open_runs(
        {"run_id": "run_A", "project_path": "C:/proj/a"},
        {"run_id": "run_B", "project_path": "C:/proj/b"},
        {"run_id": "run_C", "project_path": "C:/proj/c"},
    )
    dispatcher = _RecordingDispatcher(fail_run_ids={"run_B"})

    drained = abort_open_pp_runs(state, dispatcher)

    drained_ids = {entry["run_id"] for entry in drained}
    remaining_ids = {entry["run_id"] for entry in state.open_pp_runs}
    assert drained_ids == {"run_A", "run_C"}
    assert remaining_ids == {"run_B"}
    # All 3 calls were attempted (we don't short-circuit on failure)
    assert len(dispatcher.calls) == 3


def test_abort_with_empty_open_pp_runs_is_a_noop() -> None:
    state = _state_with_open_runs()
    dispatcher = _RecordingDispatcher()

    drained = abort_open_pp_runs(state, dispatcher)

    assert drained == []
    assert state.open_pp_runs == []
    assert dispatcher.calls == []


def test_abort_skips_entries_without_run_id() -> None:
    # Defensive: state could be corrupted by a partial write or by an old
    # checkpoint format. abort_open_pp_runs must not crash on missing keys.
    state = HydraState(root_goal="test")
    state.open_pp_runs = [
        {"project_path": "C:/proj/a"},  # no run_id
        {"run_id": "", "project_path": "C:/proj/b"},  # empty run_id
        {"run_id": "run_C", "project_path": "C:/proj/c"},
    ]
    dispatcher = _RecordingDispatcher()

    drained = abort_open_pp_runs(state, dispatcher)

    assert len(drained) == 1
    assert drained[0]["run_id"] == "run_C"
    # Only the one with a real run_id reached the dispatcher
    assert len(dispatcher.calls) == 1


def test_abort_reason_defaults_to_supervisor_surfaced() -> None:
    state = _state_with_open_runs({"run_id": "run_A", "project_path": "/p"})
    dispatcher = _RecordingDispatcher()

    abort_open_pp_runs(state, dispatcher)

    assert dispatcher.calls[0][2]["reason"] == "supervisor_surfaced"


def test_open_pp_runs_field_is_replace_not_append_reducer() -> None:
    # Regression guard: B7's drain logic depends on `open_pp_runs` having
    # REPLACE semantics on the LangGraph reducer side. If a future refactor
    # adds `Annotated[..., _append]`, abort_open_pp_runs assignment of
    # `state.open_pp_runs = remaining` would be silently concatenated with
    # the original list across node boundaries, defeating the drain.
    from typing import get_type_hints, get_args
    import typing

    hints = get_type_hints(HydraState, include_extras=True)
    annotation = hints["open_pp_runs"]
    # Plain list[dict[str, str]] — NOT Annotated[..., _append]
    # If someone wraps it in Annotated, get_args would expose the reducer.
    assert typing.get_origin(annotation) is list, (
        "open_pp_runs must use plain list[...] semantics — drain assignment "
        "requires replace, not append. See state.py B7 comment."
    )
