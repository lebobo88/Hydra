"""C2 tests — campaign mesh-console-unification (2026-06-05).

Covers the new mesh-console surface:
  - hydra-mem read tools (workflows_list / workflow_status / squad_list /
    hitl_pending) against a hermetic checkpoints.db
  - the approval gate filing the frozen hydra_gate payload contract
  - `hydra resume` end-to-end: pause at the approval gate, resume clears
    pending_hitl and advances past the gate; idempotent on a cleared gate
  - hydra_control validation (no subprocess launched on bad input)

All workflow state lives under a tmp_path checkpoints.db via the
HYDRA_CHECKPOINT_DB env override — never the operator's ~/.hydra store.
"""
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
from mcp_servers.hydra_control.server import _tool_handlers as control_handlers  # noqa: E402


class _NullDispatcher:
    def dispatch(self, *a, **k):  # pragma: no cover — only shape matters
        return None

    def call_tool(self, *a, **k):  # pragma: no cover
        return None


def _start_paused_workflow(tmp_path, monkeypatch) -> str:
    """Run a workflow that pauses at the approval gate; returns workflow_id."""
    monkeypatch.setenv("HYDRA_CHECKPOINT_DB", str(tmp_path / "checkpoints.db"))
    wf = uuid4()
    initial = HydraState(workflow_id=wf, root_goal="C2 test goal: mesh console surface")
    # executive squad declares hitl_required gates → planner sets
    # requires_human_approval → graph interrupts before `approval`.
    initial.selected_squads = ["executive"]
    sup = build_supervisor(project_root=REPO_ROOT, dispatcher=_NullDispatcher())
    sup.invoke(initial, config={"configurable": {"thread_id": str(wf)}})
    return str(wf)


# --- hydra-mem read tools -----------------------------------------------------

def test_workflow_status_and_hitl_pending_see_paused_gate(tmp_path, monkeypatch):
    wf = _start_paused_workflow(tmp_path, monkeypatch)
    h = mem_handlers()

    status = h["hydra-mem.workflow_status"]({"workflow_id": wf})
    assert status["workflow_id"] == wf
    assert status["phase"] == "approval"
    assert status["pending_hitl"], "approval gate must set pending_hitl"
    assert status["pending_hitl"]["gate_node"] == "approval"

    listing = h["hydra-mem.workflows_list"]({"limit": 10})
    ids = [w["workflow_id"] for w in listing["workflows"]]
    assert wf in ids
    row = next(w for w in listing["workflows"] if w["workflow_id"] == wf)
    assert row["has_pending_hitl"] is True

    gates = h["hydra-mem.hitl_pending"]({})
    match = [g for g in gates["gates"] if g["workflow_id"] == wf]
    assert len(match) == 1
    assert match[0]["gate_node"] == "approval"
    # Frozen contract fields surface on the live gate too
    hitl = match[0]["hitl"]
    assert hitl["reason"] == "high_risk"
    assert "approve" in hitl["options"]


def test_workflow_status_not_found(tmp_path, monkeypatch):
    monkeypatch.setenv("HYDRA_CHECKPOINT_DB", str(tmp_path / "checkpoints.db"))
    h = mem_handlers()
    out = h["hydra-mem.workflow_status"]({"workflow_id": "no-such-workflow"})
    # No checkpoints.db at all → degraded/missing, never a fabricated state
    assert out.get("error") == "not_found" or out.get("degraded") is True


def test_squad_list_returns_packs_with_routing_fields(monkeypatch):
    monkeypatch.setenv("HYDRA_ROOT", str(REPO_ROOT))
    h = mem_handlers()
    out = h["hydra-mem.squad_list"]({})
    assert out["count"] >= 1
    slugs = {s["slug"] for s in out["squads"]}
    assert "executive" in slugs
    exec_pack = next(s for s in out["squads"] if s["slug"] == "executive")
    assert exec_pack["agents"], "roster must be populated"
    assert isinstance(exec_pack["accepts"], list)
    assert isinstance(exec_pack["emits"], list)
    assert isinstance(exec_pack["gates"], list)


def test_ping_reports_episodic_db():
    h = mem_handlers()
    out = h["hydra-mem.ping"]({})
    assert out["ok"] is True
    assert out["server"] == "hydra_memory"


# --- approval gate files the frozen hydra_gate contract ------------------------

def test_approval_gate_files_hydra_gate_payload(tmp_path, monkeypatch):
    filed: list[dict] = []

    from hydra_core.eights.attestation import EightsAttestor

    def fake_call(self, tool, payload):
        filed.append({"tool": tool, "payload": payload})
        return {"request_id": "hitl_test"}

    monkeypatch.setattr(EightsAttestor, "_call", fake_call)
    wf = _start_paused_workflow(tmp_path, monkeypatch)

    hitl_calls = [f for f in filed if f["tool"] == "eights.governance.hitl.request"]
    assert hitl_calls, "approval gate must file into TheEights hitl_queue"
    req = hitl_calls[-1]["payload"]
    assert req["kind"] == "hydra_gate"
    assert req["run_id"] == wf
    p = req["payload"]
    # Frozen contract: hitl_id, workflow_id, reason, summary, options,
    # default_option, gate_node, expires_at
    for field in ("hitl_id", "workflow_id", "reason", "summary", "options",
                  "default_option", "gate_node", "expires_at"):
        assert field in p, f"missing frozen-contract field {field}"
    assert p["workflow_id"] == wf
    assert p["gate_node"] == "approval"


# --- hydra resume ---------------------------------------------------------------

def test_resume_approve_clears_gate_and_advances(tmp_path, monkeypatch, capsys):
    wf = _start_paused_workflow(tmp_path, monkeypatch)

    rc = cli.main(["--project", str(REPO_ROOT), "resume", wf, "--action", "approve"])
    out = json.loads(capsys.readouterr().out)
    assert rc == 0
    assert out["resumed"] is True
    assert out["phase"] != "approval", "graph must advance past the gate"

    h = mem_handlers()
    status = h["hydra-mem.workflow_status"]({"workflow_id": wf})
    assert not status["pending_hitl"], "pending_hitl must be cleared"
    gates = h["hydra-mem.hitl_pending"]({})
    assert not [g for g in gates["gates"] if g["workflow_id"] == wf]


def test_resume_is_idempotent_on_cleared_gate(tmp_path, monkeypatch, capsys):
    wf = _start_paused_workflow(tmp_path, monkeypatch)
    rc1 = cli.main(["--project", str(REPO_ROOT), "resume", wf, "--action", "approve"])
    capsys.readouterr()
    assert rc1 == 0

    rc2 = cli.main(["--project", str(REPO_ROOT), "resume", wf, "--action", "approve"])
    out = json.loads(capsys.readouterr().out)
    assert rc2 == 0
    assert out["resumed"] is False
    assert out["reason"] == "no_pending_gate"


def test_resume_reject_parks_workflow_surfaced(tmp_path, monkeypatch, capsys):
    wf = _start_paused_workflow(tmp_path, monkeypatch)
    rc = cli.main(["--project", str(REPO_ROOT), "resume", wf, "--action", "reject"])
    out = json.loads(capsys.readouterr().out)
    assert rc == 0
    assert out["resumed"] is False
    assert out["phase"] == "surfaced"

    h = mem_handlers()
    status = h["hydra-mem.workflow_status"]({"workflow_id": wf})
    assert status["phase"] == "surfaced"
    assert not status["pending_hitl"]


def test_resume_unknown_workflow_errors(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("HYDRA_CHECKPOINT_DB", str(tmp_path / "checkpoints.db"))
    rc = cli.main(["--project", str(REPO_ROOT), "resume", str(uuid4()),
                   "--action", "approve"])
    out = json.loads(capsys.readouterr().out)
    assert rc == 1
    assert out["error"] == "not_found"


# --- concurrency guards (Reflexion ×1 — Codex verdict_ZCsp2WBc3e) ---------------

def test_concurrent_resume_loses_claim(tmp_path, monkeypatch, capsys):
    """A second resume while the lock is held exits benignly without touching
    the gate (atomic claim-then-check — never double-invokes the graph)."""
    wf = _start_paused_workflow(tmp_path, monkeypatch)

    lock = REPO_ROOT / ".hydra" / wf / "resume.lock"
    lock.parent.mkdir(parents=True, exist_ok=True)
    lock.write_text("4242")  # simulate an in-flight resume holding the claim
    try:
        rc = cli.main(["--project", str(REPO_ROOT), "resume", wf, "--action", "approve"])
        out = json.loads(capsys.readouterr().out)
        assert rc == 0
        assert out["resumed"] is False
        assert out["reason"] == "resume_in_progress"

        # Gate untouched: still pending
        h = mem_handlers()
        status = h["hydra-mem.workflow_status"]({"workflow_id": wf})
        assert status["pending_hitl"], "loser of the claim must not clear the gate"
    finally:
        lock.unlink(missing_ok=True)


def test_live_owner_lock_is_never_reclaimed_even_when_old(tmp_path, monkeypatch, capsys):
    """A lock whose owner PID is ALIVE holds the claim regardless of age —
    a legitimately long-running resume is never double-invoked
    (verdict_uO18YVw9V4)."""
    import os
    import time as _time
    wf = _start_paused_workflow(tmp_path, monkeypatch)

    lock = REPO_ROOT / ".hydra" / wf / "resume.lock"
    lock.parent.mkdir(parents=True, exist_ok=True)
    lock.write_text(str(os.getpid()))  # our own (alive) PID owns the lock
    old = _time.time() - 7200  # 2h old — way past the old wall-clock window
    os.utime(lock, (old, old))
    try:
        rc = cli.main(["--project", str(REPO_ROOT), "resume", wf, "--action", "approve"])
        out = json.loads(capsys.readouterr().out)
        assert rc == 0
        assert out["reason"] == "resume_in_progress"
        h = mem_handlers()
        assert h["hydra-mem.workflow_status"]({"workflow_id": wf})["pending_hitl"]
    finally:
        lock.unlink(missing_ok=True)


def test_dead_owner_lock_is_reclaimed(tmp_path, monkeypatch, capsys):
    """A lock owned by a dead PID (crashed resume) past the grace window is
    reclaimed and the resume proceeds."""
    import os
    import time as _time
    wf = _start_paused_workflow(tmp_path, monkeypatch)

    lock = REPO_ROOT / ".hydra" / wf / "resume.lock"
    lock.parent.mkdir(parents=True, exist_ok=True)
    # Spawn-and-reap a real process so the PID is genuinely dead.
    import subprocess
    proc = subprocess.Popen([sys.executable, "-c", "pass"])
    proc.wait()
    lock.write_text(str(proc.pid))
    old = _time.time() - 120  # past the 30s grace
    os.utime(lock, (old, old))

    rc = cli.main(["--project", str(REPO_ROOT), "resume", wf, "--action", "approve"])
    out = json.loads(capsys.readouterr().out)
    assert rc == 0
    assert out["resumed"] is True, "dead-owner lock must be reclaimed"
    assert not lock.exists() or lock.read_text() == "", "winner releases its own lock"


def test_alive_owner_lock_held_past_hard_cap(tmp_path, monkeypatch, capsys):
    """The 24h hard cap must NOT reclaim a readable, ALIVE owner — liveness
    is the sole authority for readable locks (verdict_sTc2ZQgHHB)."""
    import os
    import time as _time
    wf = _start_paused_workflow(tmp_path, monkeypatch)

    lock = REPO_ROOT / ".hydra" / wf / "resume.lock"
    lock.parent.mkdir(parents=True, exist_ok=True)
    lock.write_text(str(os.getpid()))  # alive owner
    ancient = _time.time() - (25 * 3600)  # past the 24h cap
    os.utime(lock, (ancient, ancient))
    try:
        rc = cli.main(["--project", str(REPO_ROOT), "resume", wf, "--action", "approve"])
        out = json.loads(capsys.readouterr().out)
        assert rc == 0
        assert out["reason"] == "resume_in_progress", \
            "alive owner must hold the claim even past the hard cap"
        h = mem_handlers()
        assert h["hydra-mem.workflow_status"]({"workflow_id": wf})["pending_hitl"]
    finally:
        lock.unlink(missing_ok=True)


def test_unreadable_lock_reclaimed_only_after_hard_cap(tmp_path, monkeypatch, capsys):
    """Corrupt/empty lock (liveness unverifiable): held before the 24h cap,
    reclaimed after it."""
    import os
    import time as _time
    wf = _start_paused_workflow(tmp_path, monkeypatch)
    lock = REPO_ROOT / ".hydra" / wf / "resume.lock"
    lock.parent.mkdir(parents=True, exist_ok=True)

    # Before the cap: unreadable lock holds the claim
    lock.write_text("not-a-pid")
    recent = _time.time() - 3600  # 1h — past grace, before cap
    os.utime(lock, (recent, recent))
    rc = cli.main(["--project", str(REPO_ROOT), "resume", wf, "--action", "approve"])
    out = json.loads(capsys.readouterr().out)
    assert rc == 0
    assert out["reason"] == "resume_in_progress"

    # Past the cap: reclaimed, resume proceeds
    ancient = _time.time() - (25 * 3600)
    os.utime(lock, (ancient, ancient))
    rc2 = cli.main(["--project", str(REPO_ROOT), "resume", wf, "--action", "approve"])
    out2 = json.loads(capsys.readouterr().out)
    assert rc2 == 0
    assert out2["resumed"] is True


def test_resume_releases_lock_after_completion(tmp_path, monkeypatch, capsys):
    wf = _start_paused_workflow(tmp_path, monkeypatch)
    rc = cli.main(["--project", str(REPO_ROOT), "resume", wf, "--action", "approve"])
    capsys.readouterr()
    assert rc == 0
    assert not (REPO_ROOT / ".hydra" / wf / "resume.lock").exists()

    # And a follow-up resume reaches the idempotent no-op path (not the lock path)
    rc2 = cli.main(["--project", str(REPO_ROOT), "resume", wf, "--action", "approve"])
    out = json.loads(capsys.readouterr().out)
    assert rc2 == 0
    assert out["reason"] == "no_pending_gate"


def test_node_approval_does_not_clobber_foreign_gate(tmp_path, monkeypatch):
    """If a non-approval gate lands on pending_hitl before the approval
    continuation runs, node_approval must leave it untouched."""
    monkeypatch.setenv("HYDRA_CHECKPOINT_DB", str(tmp_path / "checkpoints.db"))
    from uuid import uuid4 as _uuid4
    wf = _uuid4()
    initial = HydraState(workflow_id=wf, root_goal="C2 clobber-guard test")
    initial.selected_squads = ["executive"]
    sup = build_supervisor(project_root=REPO_ROOT, dispatcher=_NullDispatcher())
    config = {"configurable": {"thread_id": str(wf)}}
    sup.invoke(initial, config=config)  # pauses before approval

    foreign = {"gate_node": "judge_synthesis", "reason": "policy_breach",
               "summary": "foreign gate", "options": ["reject"]}
    sup.update_state(config, {"pending_hitl": foreign})
    sup.invoke(None, config=config)  # continuation runs node_approval

    snap = sup.get_state(config)
    cur = snap.values.get("pending_hitl")
    assert cur is not None, "foreign gate must survive node_approval"
    assert cur.get("gate_node") == "judge_synthesis"


def test_read_surface_while_writer_connection_open(tmp_path, monkeypatch):
    """mode=ro readers work while the supervisor's writer connection is live
    (WAL — no reader/writer lockout)."""
    wf = _start_paused_workflow(tmp_path, monkeypatch)
    # Keep a live writer connection open (the supervisor's checkpointer holds
    # one for the process lifetime) and read through the tool surface.
    import sqlite3
    writer = sqlite3.connect(str(tmp_path / "checkpoints.db"))
    try:
        writer.execute("BEGIN")  # active write txn
        h = mem_handlers()
        status = h["hydra-mem.workflow_status"]({"workflow_id": wf})
        assert status["phase"] == "approval"
        gates = h["hydra-mem.hitl_pending"]({})
        assert [g for g in gates["gates"] if g["workflow_id"] == wf]
    finally:
        writer.rollback()
        writer.close()


# --- hydra_control validation ----------------------------------------------------

def test_control_ping():
    h = control_handlers()
    out = h["hydra.control.ping"]({})
    assert out["ok"] is True
    assert out["server"] == "hydra_control"


def test_control_resume_rejects_bad_input(monkeypatch):
    h = control_handlers()
    import mcp_servers.hydra_control.server as ctl

    def boom(*a, **k):  # pragma: no cover — must never be reached
        raise AssertionError("subprocess must not launch on invalid input")

    monkeypatch.setattr(ctl, "_launch_resume", boom)

    assert h["hydra.workflow.resume"]({"workflow_id": "x; rm -rf /", "action": "approve"}) == {
        "ok": False, "error": "invalid_workflow_id"}
    bad_action = h["hydra.workflow.resume"]({"workflow_id": "wf-1", "action": "destroy"})
    assert bad_action["ok"] is False
    assert bad_action["error"] == "invalid_action"
    bad_opt = h["hydra.workflow.resume"]({"workflow_id": "wf-1", "action": "approve",
                                          "option": "x\n--evil"})
    assert bad_opt["ok"] is False
    assert bad_opt["error"] == "invalid_option"


def test_control_resume_launches_detached(monkeypatch):
    h = control_handlers()
    import mcp_servers.hydra_control.server as ctl

    captured: dict = {}

    def fake_launch(workflow_id, action, option):
        captured.update(workflow_id=workflow_id, action=action, option=option)
        return {"ok": True, "launched": True, "pid": 4242,
                "workflow_id": workflow_id, "action": action, "log": "x"}

    monkeypatch.setattr(ctl, "_launch_resume", fake_launch)
    out = h["hydra.workflow.resume"]({"workflow_id": "wf-abc", "action": "approve",
                                      "option": "approve"})
    assert out["launched"] is True
    assert captured == {"workflow_id": "wf-abc", "action": "approve", "option": "approve"}
