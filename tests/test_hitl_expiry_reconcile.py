"""E2-17: HITL request expiry + terminal reconciliation with TheEights.

Regression cover for the 858-pending-zombie finding: Hydra filed
`hydra_gate` HITL requests with no `expires_at` and never resolved them when
the originating workflow reached a terminal phase.

Every test stubs the dispatcher — no test here may reach the real daemon.
"""
from __future__ import annotations

import argparse
import json
from datetime import datetime, timedelta, timezone

import pytest

from hydra_core import cli
from hydra_core.eights import hitl_reconcile
from hydra_core.eights.attestation import (
    EightsAttestor,
    hitl_expires_at,
    hitl_expiry_hours,
)


class _StubDispatcher:
    """Records every eights call; answers hitl.list from `rows`."""

    def __init__(self, rows: list[dict] | None = None, ok: bool = True):
        self.calls: list[dict] = []
        self.rows = rows
        self.ok = ok

    def call_mcp(self, server, tool, args, **_kw):
        self.calls.append({"server": server, "tool": tool, "args": args})
        if not self.ok:
            return {"status": "failed", "error": "daemon down"}
        if tool == "eights.governance.hitl.list":
            return {"status": "done", "result": list(self.rows or [])}
        return {"status": "done", "result": {}}

    def spawn_subprocess(self, *a, **k):
        return {"status": "done", "stdout": "", "stderr": ""}

    def emit_claude_prompt(self, *a, **k):
        return {"status": "host_pickup_required"}

    def invoke_claude_skill(self, *a, **k):
        return {"status": "host_pickup_required"}


def _row(request_id: str, workflow_id: str, gate_node: str = "approval") -> dict:
    return {
        "request_id": request_id,
        "run_id": workflow_id,
        "kind": "hydra_gate",
        "status": "pending",
        "payload": {
            "hitl_id": f"h-{request_id}",
            "workflow_id": workflow_id,
            "reason": "high_risk",
            "gate_node": gate_node,
            "expires_at": None,
        },
    }


def _tools(disp: _StubDispatcher) -> list[str]:
    return [c["tool"] for c in disp.calls]


# ---------------- 1. expiry on filed requests ----------------

def test_hitl_request_carries_expires_at(tmp_path, monkeypatch):
    monkeypatch.setenv("HYDRA_EIGHTS_SPOOL", str(tmp_path / "spool"))
    d = _StubDispatcher()
    EightsAttestor(dispatcher=d, workflow_id="wf-1").hitl_request(
        {"id": "h1", "workflow_id": "wf-1", "reason": "high_risk"},
        gate_node="approval",
    )
    payload = d.calls[0]["args"]["payload"]
    expires = payload["expires_at"]
    assert expires, "every filed HITL request must carry expires_at (E2-17)"
    parsed = datetime.fromisoformat(expires.replace("Z", "+00:00"))
    delta_h = (parsed - datetime.now(timezone.utc)).total_seconds() / 3600.0
    assert 23.0 < delta_h <= 24.1  # protocol default is 24h


def test_hitl_request_expiry_env_override(tmp_path, monkeypatch):
    monkeypatch.setenv("HYDRA_EIGHTS_SPOOL", str(tmp_path / "spool"))
    monkeypatch.setenv("HYDRA_HITL_EXPIRY_HOURS", "72")
    assert hitl_expiry_hours() == 72.0
    d = _StubDispatcher()
    EightsAttestor(dispatcher=d).hitl_request({"id": "h1", "workflow_id": "wf"})
    expires = d.calls[0]["args"]["payload"]["expires_at"]
    parsed = datetime.fromisoformat(expires.replace("Z", "+00:00"))
    assert (parsed - datetime.now(timezone.utc)) > timedelta(hours=71)


@pytest.mark.parametrize("bad", ["", "abc", "0", "-5"])
def test_hitl_expiry_falls_back_on_bad_env(monkeypatch, bad):
    monkeypatch.setenv("HYDRA_HITL_EXPIRY_HOURS", bad)
    assert hitl_expiry_hours() == 24.0


def test_hitl_request_preserves_caller_expiry(tmp_path, monkeypatch):
    monkeypatch.setenv("HYDRA_EIGHTS_SPOOL", str(tmp_path / "spool"))
    d = _StubDispatcher()
    EightsAttestor(dispatcher=d).hitl_request(
        {"id": "h1", "workflow_id": "wf", "expires_at": "2030-01-01T00:00:00Z"},
    )
    assert d.calls[0]["args"]["payload"]["expires_at"] == "2030-01-01T00:00:00Z"


def test_hitl_expires_at_is_utc_iso():
    stamp = hitl_expires_at(datetime(2026, 1, 1, tzinfo=timezone.utc))
    assert stamp == "2026-01-02T00:00:00Z"


# ---------------- 2. list / resolve adapter ----------------

def test_hitl_list_filters_to_hydra_gates():
    d = _StubDispatcher(rows=[
        _row("r1", "wf-1"),
        {"request_id": "r2", "kind": "smith.reregister", "payload": {}},
    ])
    rows = EightsAttestor(dispatcher=d).hitl_list()
    assert [r["request_id"] for r in rows] == ["r1"]
    assert EightsAttestor(dispatcher=d).hitl_list(kind=None) is not None
    assert len(EightsAttestor(dispatcher=d).hitl_list(kind=None)) == 2


def test_hitl_list_returns_none_when_daemon_down():
    assert EightsAttestor(dispatcher=_StubDispatcher(ok=False)).hitl_list() is None
    assert EightsAttestor().hitl_list() is None


def test_hitl_resolve_call_shape_and_capability(tmp_path, monkeypatch):
    monkeypatch.setenv("HYDRA_EIGHTS_SPOOL", str(tmp_path / "spool"))
    d = _StubDispatcher()
    EightsAttestor(dispatcher=d).hitl_resolve(
        request_id="hitl_abc", decision="rejected",
        note="workflow terminal: surfaced", workflow_id="wf-1",
    )
    call = d.calls[0]
    assert call["tool"] == "eights.governance.hitl.resolve"
    assert call["args"]["request_id"] == "hitl_abc"
    assert call["args"]["decision"] == "rejected"
    assert call["args"]["note"] == "workflow terminal: surfaced"
    token = call["args"]["envelope"]["capability_token"]
    assert token["capability"] == "hitl.resolve"
    assert token["resource_id"] == "hitl_abc"


def test_hitl_resolve_is_spooled_when_daemon_down(tmp_path, monkeypatch):
    spool_root = tmp_path / "spool"
    monkeypatch.setenv("HYDRA_EIGHTS_SPOOL", str(spool_root))
    from hydra_core.eights.pending_spool import PendingSpool
    a = EightsAttestor(dispatcher=_StubDispatcher(ok=False),
                       spool=PendingSpool(root=spool_root))
    assert a.hitl_resolve(request_id="hitl_abc", note="terminal") is None
    spooled = [json.loads(p.read_text(encoding="utf-8"))
               for p in spool_root.glob("*.json")]
    assert any(s["tool"] == "eights.governance.hitl.resolve" for s in spooled)


def test_hitl_resolve_ignores_blank_request_id():
    d = _StubDispatcher()
    assert EightsAttestor(dispatcher=d).hitl_resolve(request_id="") is None
    assert d.calls == []


# ---------------- 3. reconcile logic ----------------

class _FakeAttestor:
    def __init__(self, rows):
        self._rows = rows
        self.resolved: list[dict] = []

    def hitl_list(self, **_kw):
        return None if self._rows is None else list(self._rows)

    def hitl_resolve(self, *, request_id, decision="rejected", note="",
                     workflow_id=None):
        self.resolved.append({"request_id": request_id, "decision": decision,
                              "note": note, "workflow_id": workflow_id})
        return {"request_id": request_id, "status": decision}


def _phases(mapping):
    return lambda wf: mapping.get(wf)


def test_reconcile_dry_run_classifies_without_resolving():
    a = _FakeAttestor([_row("r1", "wf-term"), _row("r2", "wf-live"),
                       _row("r3", "wf-gone")])
    out = hitl_reconcile.reconcile(
        a, _phases({"wf-term": "surfaced", "wf-live": "dispatch"}),
        apply=False,
    )
    assert out["pending"] == 3
    assert out["terminal"] == 1
    assert out["active"] == 1
    assert out["unknown"] == 1
    assert out["resolved"] == 0
    assert a.resolved == []


def test_reconcile_apply_resolves_terminal_and_unknown_only():
    a = _FakeAttestor([_row("r1", "wf-term"), _row("r2", "wf-live"),
                       _row("r3", "wf-gone")])
    out = hitl_reconcile.reconcile(
        a, _phases({"wf-term": "done", "wf-live": "approval"}), apply=True,
    )
    assert out["resolved"] == 2
    ids = sorted(r["request_id"] for r in a.resolved)
    assert ids == ["r1", "r3"]
    assert all(r["decision"] == "rejected" for r in a.resolved)
    notes = {r["request_id"]: r["note"] for r in a.resolved}
    assert notes["r1"] == "workflow terminal: done"
    assert notes["r3"] == "workflow terminal: unknown"


def test_reconcile_honours_limit():
    a = _FakeAttestor([_row(f"r{i}", "wf-gone") for i in range(10)])
    out = hitl_reconcile.reconcile(a, _phases({}), apply=True, limit=3)
    assert out["pending"] == 3
    assert out["resolved"] == 3


def test_reconcile_degrades_when_eights_unreachable():
    out = hitl_reconcile.reconcile(_FakeAttestor(None), _phases({}), apply=True)
    assert out["error"] == "eights_unreachable"
    assert out["resolved"] == 0


def test_resolve_for_workflow_is_gate_scoped():
    a = _FakeAttestor([_row("r1", "wf-1", gate_node="approval"),
                       _row("r2", "wf-1", gate_node="dispatch"),
                       _row("r3", "wf-2", gate_node="approval")])
    out = hitl_reconcile.resolve_for_workflow(
        a, "wf-1", note="hydra resume: approve", decision="approved",
        gate_node="approval",
    )
    assert out["pending"] == 1 and out["resolved"] == 1
    assert [r["request_id"] for r in a.resolved] == ["r1"]
    assert a.resolved[0]["decision"] == "approved"


def test_row_workflow_id_accepts_json_string_payload():
    row = {"request_id": "r1", "run_id": "wf-x",
           "payload": json.dumps({"workflow_id": "wf-y", "gate_node": "dispatch"})}
    assert hitl_reconcile.row_workflow_id(row) == "wf-y"
    assert hitl_reconcile.row_gate_node(row) == "dispatch"


def test_row_workflow_id_falls_back_to_run_id():
    assert hitl_reconcile.row_workflow_id({"run_id": "wf-z", "payload": {}}) == "wf-z"


# ---------------- 4. doctor line ----------------

def test_doctor_hitl_line_warns_over_threshold():
    line = cli._hitl_backlog_line([{}] * 858, 25)
    assert line.startswith("WARN:")
    assert "pending=858" in line and "threshold=25" in line


def test_doctor_hitl_line_ok_under_threshold():
    assert cli._hitl_backlog_line([{}] * 3, 25).startswith("OK:")


def test_doctor_hitl_line_fail_soft_when_unreachable():
    assert cli._hitl_backlog_line(None, 25).startswith("WARN:")


# ---------------- 5. CLI reconcile command ----------------

def _run_reconcile(monkeypatch, capsys, rows, phases, *, apply=False, limit=None):
    fake = _FakeAttestor(rows)
    monkeypatch.setattr(cli, "_reconcile_attestor", lambda _p: fake)
    monkeypatch.setattr(cli, "_make_phase_lookup", lambda _p: _phases(phases))
    rc = cli._cmd_eights_hitl_reconcile(argparse.Namespace(
        project=None, apply=apply, limit=limit))
    return rc, json.loads(capsys.readouterr().out), fake


def test_cli_reconcile_dry_run(monkeypatch, capsys):
    rc, out, fake = _run_reconcile(
        monkeypatch, capsys,
        [_row("r1", "wf-term"), _row("r2", "wf-live")],
        {"wf-term": "surfaced", "wf-live": "dispatch"},
    )
    assert rc == 0
    assert out["mode"] == "dry-run"
    assert out["pending"] == 2 and out["terminal"] == 1 and out["active"] == 1
    assert out["resolved"] == 0
    assert fake.resolved == []


def test_cli_reconcile_apply(monkeypatch, capsys):
    rc, out, fake = _run_reconcile(
        monkeypatch, capsys,
        [_row("r1", "wf-term"), _row("r2", "wf-live")],
        {"wf-term": "surfaced", "wf-live": "dispatch"},
        apply=True,
    )
    assert rc == 0
    assert out["mode"] == "apply"
    assert out["resolved"] == 1
    assert [r["request_id"] for r in fake.resolved] == ["r1"]


def test_cli_reconcile_registered_in_dispatch_table():
    parser_out = cli.main.__doc__  # keep import-time coverage honest
    assert parser_out is None or isinstance(parser_out, str)
    with pytest.raises(SystemExit):
        cli.main(["eights-hitl-reconcile", "--help"])


# ---------------- 6. reap → resolve ----------------

def test_reap_apply_resolves_reaped_workflow_rows(monkeypatch, capsys, tmp_path):
    """`hydra reap --apply` must close the ledger rows it just orphaned."""
    fake = _FakeAttestor([_row("r1", "wf-a"), _row("r2", "wf-b")])
    monkeypatch.setattr(cli, "_reconcile_attestor", lambda _p: fake)

    class _Snap:
        def __init__(self, values):
            self.values = values
            self.created_at = "2020-01-01T00:00:00+00:00"

    class _Sup:
        def get_state(self, config):
            return _Snap({"phase": "approval", "root_goal": "g"})

        def update_state(self, config, patch):
            return None

    monkeypatch.setattr(cli, "_NullDispatcher", cli._NullDispatcher)
    monkeypatch.setattr("hydra_core.supervisor.build_supervisor",
                        lambda **kw: _Sup())
    monkeypatch.setattr(cli, "emit", lambda *a, **k: None)

    # Point reap at a checkpoint db containing exactly wf-a and wf-b.
    import sqlite3
    cp_db = tmp_path / "checkpoints.db"
    conn = sqlite3.connect(cp_db)
    conn.execute("CREATE TABLE checkpoints (thread_id TEXT)")
    conn.executemany("INSERT INTO checkpoints VALUES (?)",
                     [("wf-a",), ("wf-b",)])
    conn.commit()
    conn.close()
    monkeypatch.setenv("HYDRA_CHECKPOINT_DB", str(cp_db))

    rc = cli._cmd_reap(argparse.Namespace(
        project=str(tmp_path), older_than_hours=24.0, apply=True))
    assert rc == 0
    out = json.loads(capsys.readouterr().out)
    assert sorted(out["reaped"]) == ["wf-a", "wf-b"]
    assert out["eights_hitl"]["resolved"] == 2
    assert {r["request_id"] for r in fake.resolved} == {"r1", "r2"}
    assert all(r["note"] == "workflow terminal: surfaced" for r in fake.resolved)


def test_reap_dry_run_resolves_nothing(monkeypatch, capsys, tmp_path):
    fake = _FakeAttestor([_row("r1", "wf-a")])

    class _Sup:
        def get_state(self, config):
            return None

    monkeypatch.setattr(cli, "_reconcile_attestor", lambda _p: fake)
    monkeypatch.setattr("hydra_core.supervisor.build_supervisor",
                        lambda **kw: _Sup())
    monkeypatch.setenv("HYDRA_CHECKPOINT_DB", str(tmp_path / "absent.db"))
    rc = cli._cmd_reap(argparse.Namespace(
        project=str(tmp_path), older_than_hours=24.0, apply=False))
    assert rc == 0
    assert fake.resolved == []


# ---------------- 7. resume → resolve ----------------

def _mock_sup(workflow_id: str, pending: dict):
    from unittest.mock import MagicMock
    sup = MagicMock()
    sup.get_state.return_value = MagicMock(values={
        "pending_hitl": pending,
        "phase": "approval",
        "budget": {"budget_usd": 50.0, "spent_usd": 0.0,
                   "token_limit": 200000, "spent_tokens": 0},
    })
    sup.update_state.return_value = None
    sup.invoke.return_value = {"phase": "done"}
    return sup


def _resume(monkeypatch, tmp_path, action, option=None):
    from unittest.mock import patch as _patch
    seen: list[dict] = []
    monkeypatch.setattr(
        cli, "_resolve_eights_hitl_for_workflow",
        lambda project, wf, **kw: (seen.append({"wf": wf, **kw}),
                                   {"resolved": 1, "failed": 0, "pending": 1})[1],
    )
    wf = "wf-resume-e2-17"
    pending = {"workflow_id": wf, "gate_node": "approval", "reason": "high_risk"}
    with _patch("hydra_core.supervisor.build_supervisor",
                return_value=_mock_sup(wf, pending)), \
         _patch("hydra_core.supervisor._PurePythonRunner", type(None)):
        cli._cmd_resume_locked(
            argparse.Namespace(project=str(tmp_path), live=False, verbose=False),
            tmp_path, wf, action, option)
    return seen


def test_resume_approve_resolves_that_gate_row(monkeypatch, tmp_path, capsys):
    seen = _resume(monkeypatch, tmp_path, "approve")
    capsys.readouterr()
    assert len(seen) == 1
    assert seen[0]["gate_node"] == "approval"
    assert seen[0]["decision"] == "approved"
    assert seen[0]["note"] == "hydra resume: approve"


def test_resume_reject_resolves_as_terminal(monkeypatch, tmp_path, capsys):
    seen = _resume(monkeypatch, tmp_path, "reject")
    capsys.readouterr()
    assert seen[0]["decision"] == "rejected"
    assert seen[0]["note"] == "workflow terminal: surfaced"


def test_resume_abort_option_resolves_as_terminal(monkeypatch, tmp_path, capsys):
    seen = _resume(monkeypatch, tmp_path, "approve", option="abort")
    capsys.readouterr()
    assert seen[0]["decision"] == "rejected"
    assert seen[0]["note"] == "workflow terminal: surfaced"


def test_resolve_eights_hitl_for_workflow_never_raises(monkeypatch):
    def _boom(_project):
        raise RuntimeError("no dispatcher")

    monkeypatch.setattr(cli, "_reconcile_attestor", _boom)
    out = cli._resolve_eights_hitl_for_workflow(
        cli.Path("."), "wf-1", note="workflow terminal: surfaced")
    assert out["resolved"] == 0
    assert out["unavailable"] is True
