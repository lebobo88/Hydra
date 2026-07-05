"""MU micro-usage audit regression tests (MU7). See docs/audits/MU-MICRO-USAGE-2026-07-05.md."""
from __future__ import annotations

import json
import sys
from pathlib import Path
from uuid import uuid4

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))

langgraph = pytest.importorskip("langgraph")

from hydra_core import cli  # noqa: E402
from hydra_core.state import HydraState  # noqa: E402
from hydra_core.supervisor import build_supervisor  # noqa: E402
from mcp_servers.hydra_memory.server import _tool_handlers as mem_handlers  # noqa: E402


class _NullDispatcher:
    def dispatch(self, *a, **k):  # pragma: no cover
        return None

    def call_tool(self, *a, **k):  # pragma: no cover
        return None


def _start_paused_workflow(tmp_path, monkeypatch) -> str:
    """Run a workflow that pauses at the approval gate; returns workflow_id.

    Uses the executive squad so the planner sets requires_human_approval and
    the graph interrupts before the `approval` node with a real pending_hitl.
    """
    monkeypatch.setenv("HYDRA_CHECKPOINT_DB", str(tmp_path / "checkpoints.db"))
    wf = uuid4()
    initial = HydraState(workflow_id=wf, root_goal="MU7 test goal: bare interrupt resume")
    initial.selected_squads = ["executive"]
    sup = build_supervisor(project_root=REPO_ROOT, dispatcher=_NullDispatcher())
    sup.invoke(initial, config={"configurable": {"thread_id": str(wf)}})
    return str(wf)


# ---------------------------------------------------------------------------
# MU7 tests
# ---------------------------------------------------------------------------

def test_mu7_approve_continues_bare_synthesis_interrupt(tmp_path, monkeypatch, capsys):
    """MU7: approve on a bare synthesis interrupt (pending_hitl=None, snap.next
    non-empty) must continue the graph, setting resumed=True and
    continued_bare_interrupt=True in the JSON output."""
    wf = _start_paused_workflow(tmp_path, monkeypatch)

    # Advance past the real approval gate to land at the bare synthesis interrupt.
    rc1 = cli.main(["--project", str(REPO_ROOT), "resume", wf, "--action", "approve"])
    capsys.readouterr()
    assert rc1 == 0

    # Verify we are now at the synthesis bare interrupt with no real gate filed.
    h = mem_handlers()
    status = h["hydra-mem.workflow_status"]({"workflow_id": wf})
    assert not status.get("pending_hitl"), (
        "pending_hitl must be None at the bare synthesis interrupt (MU7 precondition)"
    )

    # MU7 core: approve on the bare interrupt must continue the graph.
    rc2 = cli.main(["--project", str(REPO_ROOT), "resume", wf, "--action", "approve"])
    out = json.loads(capsys.readouterr().out)
    assert rc2 == 0, f"expected exit 0, got {rc2}: {out}"
    assert out["resumed"] is True, "MU7: resumed must be True for bare-interrupt approve"
    assert out["continued_bare_interrupt"] is True, (
        "MU7: continued_bare_interrupt must be True"
    )
    # Phase must have advanced past the synthesis pause.
    assert out.get("phase") != "synthesis", (
        f"MU7: phase must advance past 'synthesis', got {out.get('phase')!r}"
    )


def test_mu7_reject_bare_interrupt_parks_surfaced(tmp_path, monkeypatch, capsys):
    """MU7: reject on a bare synthesis interrupt parks the workflow at phase=surfaced
    without continuing the graph."""
    wf = _start_paused_workflow(tmp_path, monkeypatch)

    # Advance past the real approval gate to land at the bare synthesis interrupt.
    rc1 = cli.main(["--project", str(REPO_ROOT), "resume", wf, "--action", "approve"])
    capsys.readouterr()
    assert rc1 == 0

    # Confirm bare interrupt is active.
    h = mem_handlers()
    assert not h["hydra-mem.workflow_status"]({"workflow_id": wf}).get("pending_hitl")

    # MU7: reject on the bare interrupt must park the workflow as surfaced.
    rc2 = cli.main(["--project", str(REPO_ROOT), "resume", wf, "--action", "reject"])
    out = json.loads(capsys.readouterr().out)
    assert rc2 == 0, f"expected exit 0, got {rc2}: {out}"
    assert out["resumed"] is False, "MU7: resumed must be False for bare-interrupt reject"
    assert out.get("phase") == "surfaced", (
        f"MU7: phase must be 'surfaced' after bare-interrupt reject, got {out.get('phase')!r}"
    )
    assert out.get("continued_bare_interrupt") is False

    # Checkpoint must reflect the surfaced state.
    status = h["hydra-mem.workflow_status"]({"workflow_id": wf})
    assert status["phase"] == "surfaced", (
        f"checkpoint phase must be 'surfaced', got {status['phase']!r}"
    )


def test_mu7_terminal_still_no_pending_gate(tmp_path, monkeypatch, capsys):
    """MU7: a fully-completed workflow (snap.next empty) still returns
    reason='no_pending_gate' — the frozen contract is preserved."""
    wf = _start_paused_workflow(tmp_path, monkeypatch)

    # Drive through all interrupt points: approval gate, bare synthesis
    # interrupt, and bare judge_synthesis interrupt.  After three approves the
    # graph reaches END (snap.next becomes empty).
    for _ in range(3):
        rc = cli.main(["--project", str(REPO_ROOT), "resume", wf, "--action", "approve"])
        capsys.readouterr()
        assert rc == 0

    # Graph has completed.  A further approve must hit the frozen no_pending_gate
    # path — reason value is the contract that tests pin on (see MU7 spec).
    rc_final = cli.main(["--project", str(REPO_ROOT), "resume", wf, "--action", "approve"])
    out = json.loads(capsys.readouterr().out)
    assert rc_final == 0, f"expected exit 0, got {rc_final}: {out}"
    assert out["resumed"] is False
    assert out["reason"] == "no_pending_gate", (
        f"MU7: terminal workflow must return reason='no_pending_gate', got {out.get('reason')!r}"
    )
