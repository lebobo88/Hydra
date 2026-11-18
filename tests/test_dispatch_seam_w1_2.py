"""W1.2 step B (Hydra#85): attended dispatch seam -- budget gate, the
squad-only tool-scope directive pass, and dispatch tickets (hashes only),
in audit mode. Hermetic: no daemons, no network.

Six milestone classes, in dependency order: M1 ticket identity/writer, M2
budget_gate, M3 squad directive + persist-failure status, M4 the CLI step
wiring (budget gate placement, open-cursor re-serve, tickets), M5 the poll
and submit wiring (idempotent reuse), M6 persist-failure recovery end to
end.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
from datetime import datetime, timezone
from pathlib import Path

import pytest

from hydra_core import cli, dispatch_seam, host_bridge
from hydra_core.squad_loader import ToolSpec
from hydra_core.state import HydraState, TaskState

PII: str = "user@example.com"


# --------------------------------------------------------------------------- #
# fakes shared by M3-M6 (CLI-level tests)                                     #
# --------------------------------------------------------------------------- #

class _Agent:
    slug = "boardroom"
    authority = "gatekeeper"


class _ScopedPack:
    slug = "executive"
    name = "Executive"
    entrypoint = "agent-impersonation"
    agents = [_Agent()]
    gates: list = []
    invoke = None
    tools = (ToolSpec(name="memory.search", mcp_server="hydra_memory",
                      privilege="read", notes="search memory"),)


class _FakeSup:
    def __init__(self, state: HydraState):
        self.values = state.model_dump(mode="json")

    def get_state(self, config):
        outer = self

        class _Snap:
            values = outer.values
            next = ()

        return _Snap()

    def update_state(self, config, values):
        self.values.update(values)

    def invoke(self, _inp, config=None):
        return self.values


class _Disp:
    """Records calls; answers the pp calls the attended engineering path makes."""
    live_execution = True

    def __init__(self):
        self.calls: list[tuple[str, str, dict, str | None]] = []

    def set_squad_packs(self, packs):
        pass

    def call_mcp(self, server, tool, args, squad_id=None, **_kw):
        self.calls.append((server, tool, dict(args), squad_id))
        if tool == "start_run":
            return {"status": "done", "result": {"run_id": "run-e1"}}
        if tool == "start_stage":
            return {"status": "done", "result": {"stage_id": "stage-1"}}
        if tool == "record_attempt":
            return {"status": "done", "result": {"attempt_id": "att-1"}}
        if tool == "gate_eligible_judges":
            return {"status": "done", "result": {
                "required_cross_vendor": False,
                "rubric_id": "rfc-2119-normative"}}
        if tool == "get_stage_finalize_readiness":
            return {"status": "done", "result": {"can_pass": True}}
        if tool == "finalize_run":
            return {"status": "done", "result": {
                "effective_status": "complete", "downgraded": False}}
        return {"status": "done", "result": {}}

    def tools(self) -> list[str]:
        return [t for _s, t, _a, _q in self.calls]


def _install(monkeypatch, state: HydraState, packs: dict, disp: "_Disp | None" = None):
    disp = disp or _Disp()
    sup = _FakeSup(state)
    monkeypatch.setattr(cli, "_attended_live_dispatcher", lambda *a, **k: disp)
    monkeypatch.setattr("hydra_core.supervisor.build_supervisor", lambda **k: sup)
    monkeypatch.setattr("hydra_core.squad_loader.discover_squads",
                        lambda *a, **k: packs)
    monkeypatch.setattr("hydra_core.squad_node._maybe_write_claude_shim",
                        lambda *a, **k: None)
    return disp, sup


def _eng_env(monkeypatch, tmp_path: Path) -> tuple[Path, Path]:
    """Project root with the agent stubs + a non-git target dir."""
    root = tmp_path / "proj"
    agents = root / "plugins" / "hydra" / "agents"
    agents.mkdir(parents=True)
    for name in ("engineer.md", "judge-cross-vendor.md", "judge-same-vendor.md"):
        (agents / name).write_text("# stub\n", encoding="utf-8")
    target = tmp_path / "target"
    target.mkdir()
    monkeypatch.setattr(cli, "_resolve_task_project_path",
                        lambda *a, **k: str(target))
    monkeypatch.setenv("HYDRA_ATTENDED_SMOKE_MODE", "sync")
    return root, target


def _run_step(project: Path, wf: str, capsys) -> dict:
    rc = cli._cmd_attended_step(argparse.Namespace(
        project=str(project), workflow_id=wf, verbose=False))
    captured = capsys.readouterr()
    out = captured.out
    assert rc == 0, (out[:800] or captured.err[:800])
    return json.loads(out)


def _run_step_expect(project: Path, wf: str, capsys, want_rc: int) -> dict:
    rc = cli._cmd_attended_step(argparse.Namespace(
        project=str(project), workflow_id=wf, verbose=False))
    out = capsys.readouterr().out
    assert rc == want_rc, out[:800]
    return json.loads(out)


def _run_submit(project: Path, wf: str, run_id: str, call_key: str,
                result: dict, capsys) -> dict:
    rf = project / f"result-{call_key}.json"
    rf.write_text(json.dumps(result), encoding="utf-8")
    rc = cli._cmd_attended_submit(argparse.Namespace(
        project=str(project), workflow_id=wf, run_id=run_id,
        call_key=call_key, result=str(rf), verbose=False))
    out = capsys.readouterr().out
    assert rc == 0, out[:800]
    return json.loads(out)


def _ticket(project: Path, wf: str, run_id: str, call_key: str) -> dict:
    """Reads a ticket file through the same extended-length-path-safe helper
    the writer itself uses (``dispatch_seam._long_path``) -- the 40+40+32
    character ticket filename this module's identity scheme produces can
    exceed Windows's 260-char ``MAX_PATH`` once nested under a long tmp_path,
    and plain ``pathlib.Path.exists()``/``read_text()`` do not transparently
    bypass that the way the writer's own I/O does."""
    p = dispatch_seam.ticket_path(project, wf, run_id, call_key)
    assert os.path.exists(dispatch_seam._long_path(p)), p
    with open(dispatch_seam._long_path(p), "r", encoding="utf-8") as f:
        return json.loads(f.read())


def _ticket_text(p: Path) -> str:
    with open(dispatch_seam._long_path(p), "r", encoding="utf-8") as f:
        return f.read()


def _eng_state(desc: str = "add a feature") -> tuple[HydraState, TaskState]:
    task = TaskState(owner_squad="engineering", description=desc,
                     target_repo_id="hydra")
    return HydraState(root_goal=desc, tasks=[task]), task


# =========================================================================== #
# M1 -- ticket identity and atomic writer                                     #
# =========================================================================== #

class TestM1TicketIdentity:
    def test_ticket_id_matches_independent_recomputation(self, tmp_path):
        wf, run_id, call_key = "wf-1", "run-1", "generate-0"

        def lp(s: str) -> bytes:
            b = s.encode("utf-8")
            return str(len(b)).encode("ascii") + b":" + b

        expected = hashlib.sha256(lp(wf) + lp(run_id) + lp(call_key)).hexdigest()
        action = {"call_key": call_key, "agent_type": "engineer", "prompt": "p"}

        path = dispatch_seam.write_dispatch_ticket(
            tmp_path, wf, run_id, action, kind="engineer", squad_slug=None,
            budget_usd=None, budget_remaining_usd=None)

        assert path is not None
        assert path == (tmp_path / ".hydra" / wf / "attended" / "tickets"
                        / f"run-1.generate-0.{expected[:32]}.json")
        ticket = json.loads(path.read_text(encoding="utf-8"))
        assert ticket["ticket_id"] == expected
        assert ticket["workflow_id"] == wf
        assert ticket["run_id"] == run_id
        assert ticket["call_key"] == call_key

    @pytest.mark.parametrize("run_a,call_a,run_b,call_b", [
        ("run-a", "generate-0", "run-b", "generate-0"),
        ("run/a", "generate-0", "runa", "generate-0"),
        ("", "generate-0", "///", "generate-0"),
        ("run-x", "", "run-x", "***"),
        ("ab", "c", "a", "bc"),
    ])
    def test_distinct_identities_give_distinct_tickets(
            self, tmp_path, run_a, call_a, run_b, call_b):
        wf = "wf-collide"
        a = dispatch_seam.write_dispatch_ticket(
            tmp_path, wf, run_a, {"call_key": call_a, "agent_type": "engineer",
                                  "prompt": "p"},
            kind="engineer", squad_slug=None, budget_usd=None,
            budget_remaining_usd=None)
        b = dispatch_seam.write_dispatch_ticket(
            tmp_path, wf, run_b, {"call_key": call_b, "agent_type": "engineer",
                                  "prompt": "p"},
            kind="engineer", squad_slug=None, budget_usd=None,
            budget_remaining_usd=None)

        assert a is not None and b is not None and a != b
        ta, tb = json.loads(a.read_text(encoding="utf-8")), json.loads(
            b.read_text(encoding="utf-8"))
        assert ta["ticket_id"] != tb["ticket_id"]
        assert ta["run_id"] == run_a and ta["call_key"] == call_a
        assert tb["run_id"] == run_b and tb["call_key"] == call_b

    def test_distinct_workflow_ids_give_distinct_tickets_for_same_identity(
            self, tmp_path):
        run_id, call_key = "run-1", "generate-0"
        action = {"call_key": call_key, "agent_type": "engineer", "prompt": "p"}
        a = dispatch_seam.write_dispatch_ticket(
            tmp_path, "wf-one", run_id, action, kind="engineer", squad_slug=None,
            budget_usd=None, budget_remaining_usd=None)
        b = dispatch_seam.write_dispatch_ticket(
            tmp_path, "wf-two", run_id, action, kind="engineer", squad_slug=None,
            budget_usd=None, budget_remaining_usd=None)

        assert a is not None and b is not None and a != b
        ta = json.loads(a.read_text(encoding="utf-8"))
        tb = json.loads(b.read_text(encoding="utf-8"))
        assert ta["ticket_id"] != tb["ticket_id"]

    def test_reuse_rule_returns_existing_unchanged(self, tmp_path):
        wf, run_id, call_key = "wf-reuse", "run-1", "generate-0"
        action = {"call_key": call_key, "agent_type": "engineer", "prompt": "p"}

        first = dispatch_seam.write_dispatch_ticket(
            tmp_path, wf, run_id, action, kind="engineer", squad_slug=None,
            budget_usd=1.0, budget_remaining_usd=0.5)
        bytes_before = first.read_bytes()
        mtime_before = first.stat().st_mtime_ns

        second = dispatch_seam.write_dispatch_ticket(
            tmp_path, wf, run_id, action, kind="engineer", squad_slug=None,
            budget_usd=99.0, budget_remaining_usd=98.0)

        assert second == first
        assert first.read_bytes() == bytes_before
        assert first.stat().st_mtime_ns == mtime_before
        trace = (tmp_path / ".hydra" / wf / "trace.jsonl").read_text(encoding="utf-8")
        written = [json.loads(ln) for ln in trace.splitlines() if ln.strip()
                   and json.loads(ln).get("kind") == "dispatch.ticket_written"]
        assert len(written) == 1

    def test_identity_mismatch_at_planted_path_is_refused_and_unchanged(
            self, tmp_path):
        wf, run_id, call_key = "wf-mismatch", "run-1", "generate-0"
        path = dispatch_seam.ticket_path(tmp_path, wf, run_id, call_key)
        path.parent.mkdir(parents=True, exist_ok=True)
        planted = {"workflow_id": wf, "run_id": "someone-else", "call_key": call_key}
        path.write_text(json.dumps(planted), encoding="utf-8")
        before = path.read_bytes()

        result = dispatch_seam.write_dispatch_ticket(
            tmp_path, wf, run_id, {"call_key": call_key, "agent_type": "engineer",
                                   "prompt": "p"},
            kind="engineer", squad_slug=None, budget_usd=None,
            budget_remaining_usd=None)

        assert result is None
        assert path.read_bytes() == before
        trace = (tmp_path / ".hydra" / wf / "trace.jsonl").read_text(encoding="utf-8")
        events = [json.loads(ln) for ln in trace.splitlines() if ln.strip()]
        failed = [e for e in events if e["kind"] == "dispatch.ticket_write_failed"]
        assert len(failed) == 1 and failed[0]["reason"] == "identity_mismatch"

    def test_unreadable_planted_file_is_refused_and_unchanged(self, tmp_path):
        wf, run_id, call_key = "wf-badjson", "run-1", "generate-0"
        path = dispatch_seam.ticket_path(tmp_path, wf, run_id, call_key)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("not json at all {{{", encoding="utf-8")
        before = path.read_bytes()

        result = dispatch_seam.write_dispatch_ticket(
            tmp_path, wf, run_id, {"call_key": call_key, "agent_type": "engineer",
                                   "prompt": "p"},
            kind="engineer", squad_slug=None, budget_usd=None,
            budget_remaining_usd=None)

        assert result is None
        assert path.read_bytes() == before
        trace = (tmp_path / ".hydra" / wf / "trace.jsonl").read_text(encoding="utf-8")
        events = [json.loads(ln) for ln in trace.splitlines() if ln.strip()]
        failed = [e for e in events if e["kind"] == "dispatch.ticket_write_failed"]
        assert len(failed) == 1 and failed[0]["reason"] == "existing_unreadable"

    def test_concurrent_writer_race_keeps_winner_and_reuses(self, tmp_path, monkeypatch):
        """Simulates the final file appearing BETWEEN the writer's pre-check
        and its exclusive publish: the first writer's bytes win, the second
        writer never overwrites them and returns the SAME path (reuse)."""
        wf, run_id, call_key = "wf-race", "run-1", "generate-0"
        action = {"call_key": call_key, "agent_type": "engineer", "prompt": "p"}
        path = dispatch_seam.ticket_path(tmp_path, wf, run_id, call_key)

        real_link = os.link
        winner_ticket = {
            "schema": 1, "mode": "audit", "workflow_id": wf, "run_id": run_id,
            "call_key": call_key, "ticket_id": dispatch_seam.compute_ticket_id(
                wf, run_id, call_key), "kind": "engineer", "agent_type": "engineer",
            "squad_slug": None, "head": "engineer", "prompt_sha256": "x",
            "budget_usd": None, "budget_remaining_usd": None,
            "created_at": "x", "expires_at": "x",
        }

        def _racing_link(src, dst):
            # The real winner publishes its own (different-content) ticket
            # at `dst` right before our call gets to link.
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(json.dumps(winner_ticket), encoding="utf-8")
            return real_link(src, dst)

        monkeypatch.setattr(dispatch_seam.os, "link", _racing_link)

        result = dispatch_seam.write_dispatch_ticket(
            tmp_path, wf, run_id, action, kind="engineer", squad_slug=None,
            budget_usd=None, budget_remaining_usd=None)

        assert result == path
        on_disk = json.loads(path.read_bytes())
        assert on_disk == winner_ticket
        # No partial / half-written file left behind anywhere under tickets/.
        leftovers = list(path.parent.iterdir())
        assert leftovers == [path]

    def test_exclusive_publish_failure_leaves_no_partial_file(
            self, tmp_path, monkeypatch):
        """The publish primitive (os.link) fails for a reason OTHER than the
        file already existing (fail-soft): no file, partial or otherwise, is
        left at the final ticket path."""
        wf, run_id, call_key = "wf-linkfail", "run-1", "generate-0"
        path = dispatch_seam.ticket_path(tmp_path, wf, run_id, call_key)

        def _boom(src, dst):
            raise OSError("simulated link failure")

        monkeypatch.setattr(dispatch_seam.os, "link", _boom)

        result = dispatch_seam.write_dispatch_ticket(
            tmp_path, wf, run_id, {"call_key": call_key, "agent_type": "engineer",
                                   "prompt": "p"},
            kind="engineer", squad_slug=None, budget_usd=None,
            budget_remaining_usd=None)

        assert result is None
        assert not path.exists()
        assert list(path.parent.iterdir()) == [] if path.parent.exists() else True
        trace = (tmp_path / ".hydra" / wf / "trace.jsonl").read_text(encoding="utf-8")
        events = [json.loads(ln) for ln in trace.splitlines() if ln.strip()]
        failed = [e for e in events if e["kind"] == "dispatch.ticket_write_failed"]
        assert len(failed) == 1 and failed[0]["reason"] == "OSError"

    def test_fields_exactly_and_ttl_default_and_override(self, tmp_path, monkeypatch):
        now = datetime(2026, 1, 1, tzinfo=timezone.utc)
        action = {"call_key": "k-1", "agent_type": "plug:engineer", "prompt": "p"}

        def _written(wf: str) -> dict:
            path = dispatch_seam.write_dispatch_ticket(
                tmp_path, wf, "run-1", action, kind="engineer",
                squad_slug="engineering", budget_usd=5.0,
                budget_remaining_usd=4.0, now=now)
            assert path is not None
            return json.loads(path.read_text(encoding="utf-8"))

        t = _written("wf-ttl-default")
        assert set(t.keys()) == {
            "schema", "mode", "workflow_id", "run_id", "call_key", "ticket_id",
            "kind", "agent_type", "squad_slug", "head", "prompt_sha256",
            "budget_usd", "budget_remaining_usd", "created_at", "expires_at"}
        assert (datetime.fromisoformat(t["expires_at"])
                - datetime.fromisoformat(t["created_at"])).total_seconds() == 3600

        monkeypatch.setenv(dispatch_seam.TICKET_TTL_ENV, "120")
        t2 = _written("wf-ttl-120")
        assert (datetime.fromisoformat(t2["expires_at"])
                - datetime.fromisoformat(t2["created_at"])).total_seconds() == 120

        for bad in ("abc", "0", "-5", ""):
            monkeypatch.setenv(dispatch_seam.TICKET_TTL_ENV, bad)
            tb = _written(f"wf-ttl-bad-{bad or 'empty'}")
            assert (datetime.fromisoformat(tb["expires_at"])
                    - datetime.fromisoformat(tb["created_at"])).total_seconds() == 3600
        assert dispatch_seam.DISPATCH_TICKET_TTL_SECONDS == 3600

    def test_judge_ticket_adds_artifact_sha256_no_text(self, tmp_path):
        action = {"call_key": "judge-0", "agent_type": "judge-cross-vendor",
                  "instructions": "judge it", "artifact_text": "diff here"}
        path = dispatch_seam.write_dispatch_ticket(
            tmp_path, "wf-judge", "run-1", action, kind="judge",
            squad_slug="engineering", budget_usd=None, budget_remaining_usd=None)
        t = json.loads(path.read_text(encoding="utf-8"))
        assert t["artifact_sha256"] == hashlib.sha256(
            b"diff here").hexdigest()
        assert "artifact_text" not in t and "instructions" not in t and "prompt" not in t
        assert t["prompt_sha256"] == hashlib.sha256(
            dispatch_seam.canonical_prompt_text(action).encode("utf-8")).hexdigest()

    def test_head_strips_plugin_prefix_for_squad_only(self, tmp_path):
        squad_path = dispatch_seam.write_dispatch_ticket(
            tmp_path, "wf-head", "task-1",
            {"call_key": "squad-task-1-0", "agent_type": "hydra:plan-author",
             "prompt": "p"},
            kind="squad", squad_slug="planning", budget_usd=None,
            budget_remaining_usd=None)
        no_colon_path = dispatch_seam.write_dispatch_ticket(
            tmp_path, "wf-head", "task-2",
            {"call_key": "squad-task-2-0", "agent_type": "boardroom", "prompt": "p"},
            kind="squad", squad_slug="executive", budget_usd=None,
            budget_remaining_usd=None)
        eng_path = dispatch_seam.write_dispatch_ticket(
            tmp_path, "wf-head", "run-3",
            {"call_key": "generate-0", "agent_type": "hydra:engineer", "prompt": "p"},
            kind="engineer", squad_slug="engineering", budget_usd=None,
            budget_remaining_usd=None)

        assert json.loads(squad_path.read_text())["head"] == "plan-author"
        assert json.loads(no_colon_path.read_text())["head"] == "boardroom"
        assert json.loads(eng_path.read_text())["head"] == "hydra:engineer"

    def test_invalid_workflow_id_writes_no_file_anywhere(self, tmp_path):
        assert dispatch_seam.WORKFLOW_ID_RE.pattern == cli._WORKFLOW_ID_RE.pattern
        for bad in ("../escape", "a/b", "", "-lead", "a" * 65):
            with pytest.raises(ValueError):
                dispatch_seam.ticket_path(tmp_path, bad, "run-1", "k")
            assert dispatch_seam.write_dispatch_ticket(
                tmp_path, bad, "run-1",
                {"call_key": "k", "agent_type": "a", "prompt": "p"},
                kind="engineer", squad_slug=None, budget_usd=None,
                budget_remaining_usd=None) is None
        assert not (tmp_path / ".hydra").exists()
        assert list(tmp_path.parent.glob("escape*")) == []

    # ----------------------------------------------------------------- #
    # Reflexion retry 1: three judge-flagged defects (Hydra#85)          #
    # ----------------------------------------------------------------- #

    @pytest.mark.parametrize("fault", ["write", "close"])
    def test_temp_file_fault_leaves_no_partial_file(
            self, tmp_path, monkeypatch, fault):
        """A failure while writing (or closing/flushing) the temp file --
        e.g. an ENOSPC -- must not leave the temp file (or any final file)
        behind: cleanup is not conditional on the write having succeeded."""
        wf, run_id, call_key = f"wf-fault-{fault}", "run-1", "generate-0"
        path = dispatch_seam.ticket_path(tmp_path, wf, run_id, call_key)
        real_open = open

        class _FaultFile:
            """Wraps a REAL file handle (``open(..., 'w')`` truncates/creates
            the file on disk immediately, before any ``write()`` call) so
            the fault happens after a real temp file already exists --
            exactly the scenario where cleanup must not be conditional on
            the write having succeeded."""

            def __init__(self, inner):
                self._inner = inner

            def __enter__(self):
                return self

            def __exit__(self, exc_type, exc, tb):
                self._inner.close()
                if fault == "close":
                    raise OSError(28, "No space left on device")
                return False

            def write(self, data):
                if fault == "write":
                    raise OSError(28, "No space left on device")
                return self._inner.write(data)

        def _fake_open(file, mode="r", *a, **kw):
            if str(file).endswith(".tmp") and "w" in mode:
                return _FaultFile(real_open(file, mode, *a, **kw))
            return real_open(file, mode, *a, **kw)

        monkeypatch.setattr("builtins.open", _fake_open)

        result = dispatch_seam.write_dispatch_ticket(
            tmp_path, wf, run_id,
            {"call_key": call_key, "agent_type": "engineer", "prompt": "p"},
            kind="engineer", squad_slug=None, budget_usd=None,
            budget_remaining_usd=None)

        assert result is None
        assert not path.exists()
        tickets_dir = path.parent
        assert not tickets_dir.exists() or list(tickets_dir.iterdir()) == []
        trace = (tmp_path / ".hydra" / wf / "trace.jsonl").read_text(
            encoding="utf-8")
        events = [json.loads(ln) for ln in trace.splitlines() if ln.strip()]
        failed = [e for e in events if e["kind"] == "dispatch.ticket_write_failed"]
        assert len(failed) == 1

    def test_ticket_ttl_seconds_rejects_oversized_value(self, monkeypatch):
        """``HYDRA_DISPATCH_TICKET_TTL_S=999999999999999999999`` parses as a
        positive int but is not representable in `timedelta` arithmetic --
        the parser itself must reject it (fall back to 3600) rather than
        hand it on to crash later. A large-but-valid override is honoured."""
        monkeypatch.setenv(dispatch_seam.TICKET_TTL_ENV,
                            "999999999999999999999")
        assert dispatch_seam.ticket_ttl_seconds() == 3600

        monkeypatch.setenv(dispatch_seam.TICKET_TTL_ENV, "86400")
        assert dispatch_seam.ticket_ttl_seconds() == 86400

    def test_oversized_ttl_does_not_crash_dispatch_large_valid_honoured(
            self, tmp_path, monkeypatch):
        """End to end through ``write_dispatch_ticket``: the oversized value
        must not raise (the old bug surfaced as an uncaught `OverflowError`
        from `timedelta`), and still produces a ticket with the 3600 s
        default; a large-but-valid TTL is honoured exactly."""
        action = {"call_key": "k-ttl", "agent_type": "engineer", "prompt": "p"}

        monkeypatch.setenv(dispatch_seam.TICKET_TTL_ENV,
                            "999999999999999999999")
        path = dispatch_seam.write_dispatch_ticket(
            tmp_path, "wf-ttl-huge", "run-1", action, kind="engineer",
            squad_slug=None, budget_usd=None, budget_remaining_usd=None)
        assert path is not None
        t = json.loads(path.read_text(encoding="utf-8"))
        assert (datetime.fromisoformat(t["expires_at"])
                - datetime.fromisoformat(t["created_at"])).total_seconds() == 3600

        monkeypatch.setenv(dispatch_seam.TICKET_TTL_ENV, "86400")
        path2 = dispatch_seam.write_dispatch_ticket(
            tmp_path, "wf-ttl-day", "run-1", action, kind="engineer",
            squad_slug=None, budget_usd=None, budget_remaining_usd=None)
        assert path2 is not None
        t2 = json.loads(path2.read_text(encoding="utf-8"))
        assert (datetime.fromisoformat(t2["expires_at"])
                - datetime.fromisoformat(t2["created_at"])).total_seconds() == 86400

    def test_extended_length_path_transform_is_pure(self):
        """``_extended_length`` is a pure string function, parameterised
        here so the Windows cases are covered on any OS (no platform
        check, no filesystem access inside the function itself)."""
        ext = dispatch_seam._extended_length
        assert ext(r"C:\foo\bar") == r"\\?\C:\foo\bar"
        assert ext(r"\\server\share\dir\file") == r"\\?\UNC\server\share\dir\file"
        assert ext(r"\\?\C:\foo\bar") == r"\\?\C:\foo\bar"
        assert ext(r"\\?\UNC\server\share\dir") == r"\\?\UNC\server\share\dir"
        assert ext("relative\\path") == "relative\\path"
        assert ext("relative/path") == "relative/path"

    def test_long_path_unc_drive_relative_and_posix(self, monkeypatch):
        """``_long_path`` with the platform check patched: a drive-absolute
        path, a UNC path, an already-prefixed path and a relative path on
        a patched-Windows run, then the POSIX no-op."""
        monkeypatch.setattr(dispatch_seam.os, "name", "nt")
        assert dispatch_seam._long_path(r"C:\foo\bar") == r"\\?\C:\foo\bar"
        assert (dispatch_seam._long_path(r"\\server\share\dir")
                == r"\\?\UNC\server\share\dir")
        assert dispatch_seam._long_path(r"\\?\C:\foo") == r"\\?\C:\foo"
        assert dispatch_seam._long_path("relative\\path") == "relative\\path"

        monkeypatch.setattr(dispatch_seam.os, "name", "posix")
        assert (dispatch_seam._long_path(r"\\server\share\dir")
                == r"\\server\share\dir")
        assert dispatch_seam._long_path(r"C:\foo\bar") == r"C:\foo\bar"


# =========================================================================== #
# M2 -- budget_gate unit behaviour                                            #
# =========================================================================== #

class TestM2BudgetGate:
    def test_none_when_under_budget(self):
        state, _task = _eng_state()
        state.budget.budget_usd = 10.0
        state.budget.spent_usd = 5.0
        before = state.model_dump()

        result = dispatch_seam.budget_gate(state)

        assert result is None
        assert state.model_dump() == before

    @pytest.mark.parametrize("spent,budget", [(10.0, 10.0), (12.0, 10.0)])
    def test_blocked_exactly_at_or_over_budget(self, spent, budget):
        state, _task = _eng_state()
        state.budget.budget_usd = budget
        state.budget.spent_usd = spent
        before = state.model_dump()

        result = dispatch_seam.budget_gate(state)

        assert result == {
            "ok": False,
            "status": "budget_blocked",
            "workflow_id": str(state.workflow_id),
            "spent_usd": spent,
            "budget_usd": budget,
        }
        assert state.model_dump() == before

    def test_files_no_hitl(self):
        state, _task = _eng_state()
        state.budget.budget_usd = 1.0
        state.budget.spent_usd = 1.0
        dispatch_seam.budget_gate(state)
        assert getattr(state, "pending_hitl", None) is None


# =========================================================================== #
# M3 -- squad directive pass + dispatch_seam_persist_failed                   #
# =========================================================================== #

class TestM3SquadDirective:
    def test_squad_directive_appended_once_and_redacted(self):
        class _PiiPack(_ScopedPack):
            tools = (ToolSpec(name="memory.search", mcp_server="hydra_memory",
                              privilege="read", notes=f"ask {PII} for access"),)

        pack = _PiiPack()
        directive = dispatch_seam.build_tool_scope_directive(pack)
        assert PII in directive
        action = {"call_key": "k", "agent_type": "p:boardroom", "prompt": "brief"}

        once, changed_once = dispatch_seam.prepare_host_action(
            action, kind="squad", pack=pack)
        twice, changed_twice = dispatch_seam.prepare_host_action(
            once, kind="squad", pack=pack)

        assert changed_once is True and changed_twice is False
        assert PII not in once["prompt"]
        red = host_bridge._outbound_text(directive, kind="squad")
        assert once["prompt"] == action["prompt"] + "\n\n" + red
        assert once["prompt"].count(red) == 1
        assert twice == once
        assert host_bridge._outbound_text(once["prompt"], kind="squad") == once["prompt"]
        # The input is never mutated.
        assert action == {"call_key": "k", "agent_type": "p:boardroom", "prompt": "brief"}

    def test_pack_without_tools_leaves_prompt_unchanged(self):
        class _NoToolsPack(_ScopedPack):
            tools = ()

        action = {"call_key": "k", "agent_type": "p:boardroom", "prompt": "brief"}
        out, changed = dispatch_seam.prepare_host_action(
            action, kind="squad", pack=_NoToolsPack())
        assert changed is False
        assert out["prompt"] == "brief"

    @pytest.mark.parametrize("kind", ["engineer", "judge"])
    def test_engineer_and_judge_actions_are_noop(self, kind):
        action = {"call_key": "k", "agent_type": "engineer", "prompt": "do the thing"}
        out, changed = dispatch_seam.prepare_host_action(
            action, kind=kind, pack=_ScopedPack())
        assert changed is False
        assert out == action and out is not action

    def test_cursor_persist_failure_returns_exact_status(
            self, tmp_path, monkeypatch, capsys):
        task = TaskState(owner_squad="executive", description="brief the board")
        state = HydraState(root_goal="brief", tasks=[task])
        wf = str(state.workflow_id)
        _install(monkeypatch, state, {"executive": _ScopedPack()})

        def _flaky(path, cursor):
            raise OSError("disk full")

        monkeypatch.setattr(dispatch_seam, "_persist_cursor", _flaky)

        rc = cli._cmd_attended_step(argparse.Namespace(
            project=str(tmp_path), workflow_id=wf, verbose=False))
        out = json.loads(capsys.readouterr().out)

        assert rc == 1
        cursor_file = host_bridge.cursor_path(tmp_path, wf, str(task.task_id))
        assert out == {
            "ok": False,
            "status": "dispatch_seam_persist_failed",
            "cursor_path": str(cursor_file),
            "retryable": True,
        }
        assert not (tmp_path / ".hydra" / wf / "attended" / "tickets").exists()
        cursor = host_bridge.load_cursor(cursor_file)
        pack = _ScopedPack()
        directive = dispatch_seam.build_tool_scope_directive(pack)
        red = host_bridge._outbound_text(directive, kind="squad")
        assert red not in cursor["pending_action"]["prompt"]


# =========================================================================== #
# M4 -- CLI wiring in _cmd_attended_step                                     #
# =========================================================================== #

class TestM4StepWiring:
    def test_squad_step_prompt_scoped_and_ticketed(self, tmp_path, monkeypatch, capsys):
        task = TaskState(owner_squad="executive",
                         description=f"brief the board, contact {PII} first")
        state = HydraState(root_goal="brief", tasks=[task])
        wf = str(state.workflow_id)
        pack = _ScopedPack()
        _install(monkeypatch, state, {"executive": pack})

        res = _run_step(tmp_path, wf, capsys)

        action = res["host_action"]
        directive = dispatch_seam.build_tool_scope_directive(pack)
        assert directive
        red = host_bridge._outbound_text(directive, kind="squad")
        assert PII not in action["prompt"]
        assert action["prompt"].endswith(red)
        cursor = host_bridge.load_cursor(res["cursor_path"])
        assert cursor["pending_action"]["prompt"] == action["prompt"]

        ticket_file = dispatch_seam.ticket_path(
            tmp_path, wf, res["run_id"], action["call_key"])
        assert res["dispatch_ticket"] == str(ticket_file)
        ticket = _ticket(tmp_path, wf, res["run_id"], action["call_key"])
        assert ticket["kind"] == "squad" and ticket["mode"] == "audit"
        assert ticket["call_key"] == action["call_key"]
        assert ticket["agent_type"] == action["agent_type"]
        assert ticket["squad_slug"] == "executive"
        assert ticket["workflow_id"] == wf
        assert PII not in _ticket_text(ticket_file)
        trace = (tmp_path / ".hydra" / wf / "trace.jsonl").read_text(encoding="utf-8")
        written = [json.loads(ln) for ln in trace.splitlines()
                   if ln.strip() and "dispatch.ticket_written" in ln]
        assert len(written) == 1 and written[0]["call_key"] == action["call_key"]
        assert "prompt" not in written[0] and PII not in trace

    def test_repeated_squad_step_reissues_same_ticket_idempotently(
            self, tmp_path, monkeypatch, capsys):
        task = TaskState(owner_squad="executive", description="brief the board")
        state = HydraState(root_goal="brief", tasks=[task])
        wf = str(state.workflow_id)
        pack = _ScopedPack()
        _install(monkeypatch, state, {"executive": pack})

        first = _run_step(tmp_path, wf, capsys)
        second = _run_step(tmp_path, wf, capsys)

        assert second["host_action"]["call_key"] == first["host_action"]["call_key"]
        assert second["dispatch_ticket"] == first["dispatch_ticket"]
        directive = dispatch_seam.build_tool_scope_directive(pack)
        red = host_bridge._outbound_text(directive, kind="squad")
        assert second["host_action"]["prompt"].count(red) == 1
        tickets = list((tmp_path / ".hydra" / wf / "attended" / "tickets").iterdir())
        assert len(tickets) == 1

    @pytest.mark.parametrize("kind", ["engineering", "squad"])
    def test_over_budget_step_returns_budget_blocked_without_cursor_or_ticket(
            self, tmp_path, monkeypatch, capsys, kind):
        if kind == "engineering":
            state, task = _eng_state()
            packs: dict = {}
        else:
            task = TaskState(owner_squad="executive", description="brief the board")
            state = HydraState(root_goal="brief", tasks=[task])
            packs = {"executive": _ScopedPack()}
        state.budget.spent_usd = state.budget.budget_usd
        wf = str(state.workflow_id)
        disp, _ = _install(monkeypatch, state, packs)
        root = tmp_path
        if kind == "engineering":
            root, _target = _eng_env(monkeypatch, tmp_path)

        res = _run_step(root, wf, capsys)

        assert res["status"] == "budget_blocked" and res["ok"] is False
        assert res["task_id"] == str(task.task_id)
        assert res["squad_slug"] == ("engineering" if kind == "engineering" else "executive")
        assert res["spent_usd"] == res["budget_usd"]
        assert not (root / ".hydra" / wf / "attended").exists()
        assert "start_run" not in disp.tools()

    def test_engineer_step_gets_ticket_and_prompt_unchanged(
            self, tmp_path, monkeypatch, capsys):
        state, task = _eng_state("add a feature")
        wf = str(state.workflow_id)
        _install(monkeypatch, state, {})
        root, target = _eng_env(monkeypatch, tmp_path)

        res = _run_step(root, wf, capsys)

        expected = host_bridge.begin_stage(
            _Disp(), workflow_id=wf, run_id="run-e1", project_path=str(target),
            request_text="add a feature", model_tier=getattr(task, "model_tier", None),
            project_root=tmp_path / "other", task_id=str(task.task_id),
            hydra_context_block=None,
            gate_type=cli._attended_task_gate_type(task, state))["host_action"]
        assert res["host_action"]["prompt"] == expected["prompt"]
        assert res["host_action"] == expected
        ticket = _ticket(root, wf, res["run_id"], "generate-0")
        assert ticket["kind"] == "engineer"
        assert ticket["call_key"] == res["host_action"]["call_key"]
        assert ticket["head"] == "engineer"
        assert res["dispatch_ticket"] == str(
            dispatch_seam.ticket_path(root, wf, res["run_id"], "generate-0"))

    def test_ticket_write_failure_is_fail_soft(self, tmp_path, monkeypatch, capsys):
        task = TaskState(owner_squad="executive", description="brief the board")
        state = HydraState(root_goal="brief", tasks=[task])
        wf = str(state.workflow_id)
        _install(monkeypatch, state, {"executive": _ScopedPack()})

        # A regular file where the tickets directory must go: mkdir fails.
        attended = tmp_path / ".hydra" / wf / "attended"
        attended.mkdir(parents=True)
        (attended / "tickets").write_text("not a directory", encoding="utf-8")

        res = _run_step(tmp_path, wf, capsys)

        assert res["host_action"]["call_key"]
        assert res["dispatch_ticket"] is None
        trace = (tmp_path / ".hydra" / wf / "trace.jsonl").read_text(encoding="utf-8")
        events = [json.loads(line) for line in trace.splitlines() if line.strip()]
        failed = [e for e in events if e["kind"] == "dispatch.ticket_write_failed"]
        assert len(failed) == 1 and failed[0]["ticket_kind"] == "squad"
        assert not [e for e in events if e["kind"] == "dispatch.ticket_written"]

    def test_budget_gate_applies_only_when_a_new_stage_would_open(
            self, tmp_path, monkeypatch, capsys):
        task = TaskState(owner_squad="executive", description="brief the board")
        state = HydraState(root_goal="brief", tasks=[task])
        wf = str(state.workflow_id)
        _disp, sup = _install(monkeypatch, state, {"executive": _ScopedPack()})
        sup.values["budget"]["spent_usd"] = sup.values["budget"]["budget_usd"]

        # No cursor yet: blocked, nothing created.
        blocked = _run_step(tmp_path, wf, capsys)
        assert blocked["status"] == "budget_blocked"
        assert not (tmp_path / ".hydra" / wf / "attended").exists()

        # Open the stage while budget remains, then exhaust the budget.
        sup.values["budget"]["spent_usd"] = 0.0
        first = _run_step(tmp_path, wf, capsys)
        sup.values["budget"]["spent_usd"] = sup.values["budget"]["budget_usd"]

        again = _run_step(tmp_path, wf, capsys)

        assert again["ok"] is True and again["status"] == "awaiting_host"
        assert again["host_action"] == first["host_action"]
        assert again["dispatch_ticket"] == first["dispatch_ticket"]

    def test_squad_cursor_attempt_mismatch_falls_through_to_begin_squad_stage(
            self, tmp_path, monkeypatch, capsys):
        """A cursor whose attempt differs from the freshly computed one (a
        re-issued planning attempt) is NOT re-served -- begin_squad_stage
        runs again, exactly as on base."""
        task = TaskState(owner_squad="planning", description="draft the plan")
        state = HydraState(root_goal="plan", tasks=[task])
        wf = str(state.workflow_id)

        class _PlanPack(_ScopedPack):
            slug = "planning"
            entrypoint = "claude-native"
            tools = ()

        _disp, sup = _install(monkeypatch, state, {"planning": _PlanPack()})

        first = _run_step(tmp_path, wf, capsys)
        # Simulate a rejected plan attempt bump (Hydra#69 defect C).
        sup.values.setdefault("plan_submit_attempts", {})[str(task.task_id)] = 1

        second = _run_step(tmp_path, wf, capsys)

        assert second["host_action"]["call_key"] != first["host_action"]["call_key"]
        assert second["host_action"]["call_key"].endswith("-1")


# =========================================================================== #
# M5 -- await_smoke poll branch + _cmd_attended_submit wiring                #
# =========================================================================== #

class TestM5PollAndSubmit:
    def test_engineer_action_from_submit_gets_ticket_and_prompt_unchanged(
            self, tmp_path, monkeypatch, capsys):
        state, task = _eng_state()
        wf = str(state.workflow_id)
        _install(monkeypatch, state, {})
        root, _target = _eng_env(monkeypatch, tmp_path)
        step = _run_step(root, wf, capsys)
        judged = _run_submit(root, wf, step["run_id"], "generate-0",
                             {"text": "x", "cost_usd": 0.0, "model": "m"}, capsys)

        retry = _run_submit(
            root, wf, step["run_id"], judged["host_action"]["call_key"],
            {"outcome": "revise", "critique_md": "fix foo.py", "cost_usd": 0.0,
             "judge_producer": "claude"}, capsys)

        assert retry["host_action"]["call_key"] == "generate-1"
        ticket = _ticket(root, wf, retry["run_id"], "generate-1")
        assert ticket["kind"] == "engineer"
        assert ticket["prompt_sha256"] == hashlib.sha256(
            dispatch_seam.canonical_prompt_text(retry["host_action"]).encode(
                "utf-8")).hexdigest()
        assert retry["dispatch_ticket"] == str(
            dispatch_seam.ticket_path(root, wf, retry["run_id"], "generate-1"))

    def test_judge_action_from_submit_gets_ticket(self, tmp_path, monkeypatch, capsys):
        state, task = _eng_state()
        wf = str(state.workflow_id)
        _install(monkeypatch, state, {})
        root, _target = _eng_env(monkeypatch, tmp_path)
        step = _run_step(root, wf, capsys)
        cfile = Path(step["cursor_path"])
        copy = tmp_path / "cursor-copy.json"
        shutil.copyfile(cfile, copy)
        gen = {"text": "edited foo.py", "cost_usd": 0.05, "tokens_in": 5,
               "tokens_out": 5, "model": "claude-sonnet-4-6"}

        res = _run_submit(root, wf, step["run_id"], "generate-0", gen, capsys)

        direct = host_bridge.submit_host_result(
            _Disp(), cursor_file=copy, call_key="generate-0", result=gen)
        assert res["state"] == "await_judge"
        assert res["host_action"] == direct["host_action"]
        ticket = _ticket(root, wf, res["run_id"], res["host_action"]["call_key"])
        assert ticket["kind"] == "judge"
        assert ticket["agent_type"] == res["host_action"]["agent_type"]
        assert "prompt" not in ticket and "instructions" not in ticket
        assert ticket["artifact_sha256"] == hashlib.sha256(
            res["host_action"]["artifact_text"].encode("utf-8")).hexdigest()

    def test_poll_smoke_action_goes_through_seam_idempotently(
            self, tmp_path, monkeypatch, capsys):
        state, _task = _eng_state()
        wf = str(state.workflow_id)
        disp, sup = _install(monkeypatch, state, {})
        root, _target = _eng_env(monkeypatch, tmp_path)
        step = _run_step(root, wf, capsys)
        cfile = step["cursor_path"]
        cursor = host_bridge.load_cursor(cfile)
        cursor["state"] = "await_smoke"
        cursor["pending_action"] = {
            "call_key": "judge-0", "action": "poll_smoke", "poll": True,
            "instructions": f"Smoke is running, ask {PII}; poll again."}
        host_bridge.save_cursor(cfile, cursor)
        monkeypatch.setattr(host_bridge, "poll_smoke_job", lambda *a, **k: None)
        state_before = json.dumps(sup.values.get("budget"), sort_keys=True)

        first = _run_step(root, wf, capsys)
        ticket_file = dispatch_seam.ticket_path(root, wf, first["run_id"], "judge-0")
        first_text = _ticket_text(ticket_file)
        second = _run_step(root, wf, capsys)

        assert first["host_action"]["action"] == "poll_smoke"
        assert first["dispatch_ticket"] == str(ticket_file) == second["dispatch_ticket"]
        assert second["host_action"] == first["host_action"]
        assert _ticket_text(ticket_file) == first_text
        # The ticket never holds raw text at all (hashes only) -- the PII
        # planted directly into this hand-crafted cursor's `instructions`
        # (bypassing host_bridge's own build-time redaction, which is step A
        # and not this seam's job) can therefore never reach the ticket file.
        assert PII not in first_text
        assert json.dumps(sup.values.get("budget"), sort_keys=True) == state_before
        trace = (root / ".hydra" / wf / "trace.jsonl").read_text(encoding="utf-8")
        written = [json.loads(ln) for ln in trace.splitlines()
                   if ln.strip() and json.loads(ln).get("kind") == "dispatch.ticket_written"
                   and json.loads(ln).get("call_key") == "judge-0"]
        assert len(written) == 1


# =========================================================================== #
# M6 -- persist-failure recovery end to end                                  #
# =========================================================================== #

class TestM6PersistRecovery:
    def test_squad_persist_failure_then_recovery_opens_no_second_cursor_or_run(
            self, tmp_path, monkeypatch, capsys):
        # Fixture workflow: sufficient budget, one prior ENGINEERING step
        # (through _cmd_attended_step against the fake dispatcher) so the
        # fake dispatcher records exactly one start_run BEFORE the squad
        # step -- proving the recovery path never opens a second pp run.
        eng_task = TaskState(owner_squad="engineering", description="add a feature",
                             target_repo_id="hydra")
        squad_task = TaskState(owner_squad="executive", description="brief the board")
        state = HydraState(root_goal="goal", tasks=[eng_task, squad_task])
        state.budget.budget_usd = 100.0
        wf = str(state.workflow_id)
        disp, sup = _install(monkeypatch, state, {"executive": _ScopedPack()})
        root, _target = _eng_env(monkeypatch, tmp_path)

        _run_step(root, wf, capsys)  # engineering step opens the one pp run
        assert disp.tools().count("start_run") == 1
        sup.values.setdefault("attended_completed_task_ids", []).append(
            str(eng_task.task_id))

        with pytest.MonkeyPatch.context() as mp2:
            def _flaky(path, cursor):
                raise OSError("disk full")
            mp2.setattr(dispatch_seam, "_persist_cursor", _flaky)
            failed = _run_step_expect(root, wf, capsys, want_rc=1)
        assert failed["status"] == "dispatch_seam_persist_failed"
        assert disp.tools().count("start_run") == 1

        # The patch is gone (the `with` block above restored the real
        # `_persist_cursor`): the next squad step on the SAME workflow
        # succeeds with the SAME call_key -- no second cursor, no second
        # pp run.
        recovered = _run_step(root, wf, capsys)

        assert recovered["ok"] is True
        assert recovered["status"] == "awaiting_host"
        assert disp.tools().count("start_run") == 1

        task_id = str(squad_task.task_id)
        cursor_glob = list((root / ".hydra" / wf / "attended").glob(f"{task_id}*.json"))
        assert len(cursor_glob) == 1
        tickets_dir = root / ".hydra" / wf / "attended" / "tickets"
        matching = [
            p for p in tickets_dir.iterdir()
            if json.loads(_ticket_text(p)).get("run_id") == task_id
        ]
        assert len(matching) == 1
        assert json.loads(_ticket_text(matching[0]))["call_key"] == \
            recovered["host_action"]["call_key"]
