"""B9: real gate_type derivation for attended engineering tasks (cli.py).

``cli._cmd_attended_step`` calls ``host_bridge.begin_stage`` with a real
gate_type derived from the ``TaskState``'s triggering envelope (looked up in
``state.envelopes`` by ``envelope_id``), instead of the old hardcoded
``code_style`` literal. These tests exercise ``cli._attended_task_gate_type``
directly against a hand-built ``HydraState`` -- the fourth of the four call
sites (host_bridge.py:1773/1980 attended; squad_node.py:1558/2067 headless)
named in the fix.
"""
from __future__ import annotations

from uuid import uuid4

from hydra_core import cli
from hydra_core.state import HydraState, TaskState


def _state_with_envelope(envelope_type: str | None) -> tuple[HydraState, TaskState]:
    state = HydraState(root_goal="t")
    env_id = uuid4()
    if envelope_type is not None:
        state.envelopes = [{"id": str(env_id), "type": envelope_type}]
    task = TaskState(owner_squad="engineering", description="do it",
                     envelope_id=env_id if envelope_type is not None else None)
    return state, task


def test_attended_task_gate_type_prd_envelope_maps_to_spec():
    state, task = _state_with_envelope("PRD")
    assert cli._attended_task_gate_type(task, state) == "spec"


def test_attended_task_gate_type_arch_rfc_envelope_maps_to_design():
    state, task = _state_with_envelope("ARCH_RFC")
    assert cli._attended_task_gate_type(task, state) == "design"


def test_attended_task_gate_type_dev_task_envelope_maps_to_code_style():
    state, task = _state_with_envelope("DEV_TASK")
    assert cli._attended_task_gate_type(task, state) == "code_style"


def test_attended_task_gate_type_handoff_envelope_maps_to_code_style():
    state, task = _state_with_envelope("HANDOFF")
    assert cli._attended_task_gate_type(task, state) == "code_style"


def test_attended_task_gate_type_no_envelope_id_returns_none():
    """A planner-synthesised default task (no originating envelope) carries
    no better signal -- caller falls through to host_bridge's own
    code_style DEFAULT via _pp_gate_type, not a value forced here."""
    state = HydraState(root_goal="t")
    task = TaskState(owner_squad="engineering", description="do it")
    assert cli._attended_task_gate_type(task, state) is None


def test_attended_task_gate_type_unresolvable_envelope_id_returns_none():
    """envelope_id set but no matching entry in state.envelopes (e.g. a
    checkpoint replay gap) must not raise or fabricate a gate_type."""
    state = HydraState(root_goal="t")
    task = TaskState(owner_squad="engineering", description="do it",
                     envelope_id=uuid4())
    assert cli._attended_task_gate_type(task, state) is None
