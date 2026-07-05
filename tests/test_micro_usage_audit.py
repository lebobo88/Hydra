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


# ---------------------------------------------------------------------------
# MU6 — worktree-aware _get_base (repo_registry.py)
# ---------------------------------------------------------------------------

import types as _types  # noqa: E402  (used only in MU6 helpers below)

import hydra_core.repo_registry as _rr  # noqa: E402
from hydra_core.repo_registry import _GIT_PROBE_CACHE, _get_base  # noqa: E402


def test_mu6_worktree_probe_resolves_main_repo_base(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """MU6: when the git probe returns a common-dir pointing at a fake main
    repo's .git, _get_base() must return the base two levels above that .git
    (i.e. tmp_path/AiApp, NOT the worktree's naive parent)."""
    # Fake layout: tmp_path/AiApp/Hydra/.git
    fake_main_git = tmp_path / "AiApp" / "Hydra" / ".git"
    fake_main_git.mkdir(parents=True)
    expected_base = tmp_path / "AiApp"

    monkeypatch.delenv("HYDRA_REPO_BASE", raising=False)

    # Replace the cache dict via monkeypatch so it is isolated and automatically
    # restored after the test — avoids polluting subsequent tests with a stale
    # tmp_path-based entry.
    monkeypatch.setattr(_rr, "_GIT_PROBE_CACHE", {})

    # Build a minimal subprocess-module replacement whose run() returns the
    # fake common-dir for --git-common-dir calls.
    def _fake_run(cmd, **kwargs):  # noqa: ANN001
        if "--git-common-dir" in cmd:
            class _R:
                returncode = 0
                stdout = str(fake_main_git) + "\n"
            return _R()
        import subprocess as _real_sp
        return _real_sp.run(cmd, **kwargs)

    monkeypatch.setattr(_rr, "subprocess", _types.SimpleNamespace(run=_fake_run))

    result = _get_base()
    assert result == expected_base, (
        f"MU6: expected worktree-resolved base {expected_base}, got {result}"
    )


def test_mu6_probe_failure_falls_back_naive(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """MU6: when the git probe raises an exception, _get_base() falls back to
    the naive __file__-derived base."""
    monkeypatch.delenv("HYDRA_REPO_BASE", raising=False)
    # Isolate the cache so stale entries from prior runs don't short-circuit the probe.
    monkeypatch.setattr(_rr, "_GIT_PROBE_CACHE", {})

    def _raise(*args, **kwargs):  # noqa: ANN001,ANN002,ANN003
        raise OSError("git not found (simulated)")

    monkeypatch.setattr(_rr, "subprocess", _types.SimpleNamespace(run=_raise))

    result = _get_base()
    expected = Path(_rr.__file__).resolve().parents[1].parent
    assert result == expected, (
        f"MU6: expected naive fallback {expected}, got {result}"
    )


def test_mu6_env_override_still_wins(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """MU6: HYDRA_REPO_BASE always wins, even when the probe cache is poisoned
    with a wrong value."""
    # Poison a fresh isolated cache with a wrong entry; monkeypatch restores the
    # original dict after the test so no cross-test contamination occurs.
    repo_dir_key = str(Path(_rr.__file__).resolve().parents[1])
    monkeypatch.setattr(
        _rr, "_GIT_PROBE_CACHE", {repo_dir_key: tmp_path / "wrong" / ".git"}
    )

    override = tmp_path / "override"
    monkeypatch.setenv("HYDRA_REPO_BASE", str(override))

    result = _get_base()
    assert result == override, (
        f"MU6: env override must win; expected {override}, got {result}"
    )
