"""RCA path K (EIGHTS-RECORD-OUTCOME-RCA-2026-09-16.md §7): give the operator's
resume an attended route.

Before this fix, `hydra.workflow.resume` (mcp_servers/hydra_control/server.py
`_launch_resume`) ALWAYS spawned a DETACHED `hydra resume --live` subprocess.
In an interactive session `_detached_allowed()` is False, so every resume was
refused with `error: "detached_disabled"` — including for attended workflows,
which plugins/hydra/skills/approve/SKILL.md wrongly claimed were unaffected.

The fix: when detached launch is not allowed, route through the existing
non-detaching `_run_cli_json` transport (`["resume", wf, "--action", ...]`,
NEVER `--live`), synchronously, in-process on `_NullDispatcher`. Path S makes
`_NullDispatcher` never replay the eights spool, and E2-22 already makes
`node_dispatch` defer every mcp/claude-native squad task to the attended host
under a non-live dispatcher rather than executing it on the stub — so this
resume genuinely stops at the attended hand-off.
"""
from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path
from uuid import uuid4

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))


# ===========================================================================
# MCP-layer routing: detached-disallowed uses _run_cli_json, never Popen,
# and never passes --live.
# ===========================================================================

def test_resume_detached_disallowed_uses_run_cli_json_not_popen(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    from mcp_servers.hydra_control import server as _srv

    monkeypatch.delenv("HYDRA_ALLOW_DETACHED", raising=False)
    monkeypatch.setattr(_srv, "_HYDRA_ROOT", tmp_path)

    class _ShouldNotBeCalled:
        def __init__(self, *a, **kw):
            raise AssertionError(
                "Popen must NOT be called on the attended (non-detached) resume route"
            )

    monkeypatch.setattr(subprocess, "Popen", _ShouldNotBeCalled)

    captured_cli_args: list[list[str]] = []

    def _fake_run_cli_json(cli_args, *, timeout_s, err_label, workflow_id=None):
        captured_cli_args.append(list(cli_args))
        return {"ok": True, "workflow_id": workflow_id, "resumed": True,
                "action": "approve", "phase": "executing",
                "status": "executing", "gate_node": "approval",
                "pending_hitl": None}

    monkeypatch.setattr(_srv, "_run_cli_json", _fake_run_cli_json)

    out = _srv._launch_resume("wf-attended-1", "approve", None)

    assert out["ok"] is True, f"unexpected refusal: {out}"
    assert captured_cli_args, "_run_cli_json must have been called"
    argv = captured_cli_args[0]
    assert argv[0] == "resume"
    assert argv[1] == "wf-attended-1"
    assert "--live" not in argv, (
        "the attended (non-detached) resume route must NEVER pass --live "
        f"argv={argv}"
    )
    assert "--action" in argv and "approve" in argv
    assert "--gate-only" in argv, (
        "RESOLVE-GATE-ONLY (operator decision A): the attended route must "
        f"always pass --gate-only, argv={argv}"
    )


def test_resume_detached_disallowed_passes_option_and_critique_ref(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    from mcp_servers.hydra_control import server as _srv

    monkeypatch.delenv("HYDRA_ALLOW_DETACHED", raising=False)
    monkeypatch.setattr(_srv, "_HYDRA_ROOT", tmp_path)
    monkeypatch.setattr(subprocess, "Popen",
                        lambda *a, **k: (_ for _ in ()).throw(
                            AssertionError("Popen must not be called")))

    captured: list[list[str]] = []

    def _fake_run_cli_json(cli_args, *, timeout_s, err_label, workflow_id=None):
        captured.append(list(cli_args))
        return {"ok": True, "workflow_id": workflow_id}

    monkeypatch.setattr(_srv, "_run_cli_json", _fake_run_cli_json)

    out = _srv._launch_resume("wf-attended-2", "modify-budget", "250")
    assert out["ok"] is True
    argv = captured[0]
    assert "--live" not in argv
    assert "--option" in argv and "250" in argv

    # cross-vendor finding 5: this test previously never supplied a
    # critique_ref at all, so it could not prove passthrough. Actually
    # exercise the modify-plan + critique_ref call shape.
    captured.clear()
    out2 = _srv._launch_resume(
        "wf-attended-2b", "modify-plan", None, critique_ref="critique.txt",
    )
    assert out2["ok"] is True
    argv2 = captured[0]
    assert "--live" not in argv2
    assert "--critique-ref" in argv2 and "critique.txt" in argv2
    assert "--gate-only" in argv2


# ===========================================================================
# MCP-layer routing: detached-allowed keeps today's behaviour exactly.
# ===========================================================================

def test_resume_detached_allowed_unchanged(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    from mcp_servers.hydra_control import server as _srv

    monkeypatch.setenv("HYDRA_ALLOW_DETACHED", "1")
    monkeypatch.setattr(_srv, "_HYDRA_ROOT", tmp_path)

    popen_calls: list[list[str]] = []

    class _RecordPopen:
        pid = 424242

        def __init__(self, cmd, **kw):
            popen_calls.append(list(cmd))

    monkeypatch.setattr(subprocess, "Popen", _RecordPopen)

    def _should_not_be_called(*a, **kw):
        raise AssertionError("_run_cli_json must NOT be used on the detached route")

    monkeypatch.setattr(_srv, "_run_cli_json", _should_not_be_called)

    out = _srv._launch_resume("wf-detached-1", "approve", None)
    assert out.get("launched") is True
    assert popen_calls, "detached route must still launch via Popen"
    argv = popen_calls[0]
    assert "--live" in argv, "the detached route must keep passing --live"
    assert "resume" in argv and "wf-detached-1" in argv
    assert "--gate-only" not in argv, (
        "the detached/--live route must keep today's behaviour exactly — "
        "gate-only is an attended-only mode"
    )


# ===========================================================================
# Input validation happens before any subprocess, on both routes.
# ===========================================================================

def test_invalid_inputs_rejected_before_any_subprocess(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    from mcp_servers.hydra_control import server as _srv

    monkeypatch.delenv("HYDRA_ALLOW_DETACHED", raising=False)
    monkeypatch.setattr(_srv, "_HYDRA_ROOT", tmp_path)

    def _boom(*a, **kw):
        raise AssertionError("no subprocess must be spawned for invalid input")

    monkeypatch.setattr(subprocess, "Popen", _boom)
    monkeypatch.setattr(_srv, "_run_cli_json", _boom)

    handlers = _srv._tool_handlers()
    resume = handlers["hydra.workflow.resume"]

    bad_id = resume({"workflow_id": "../../etc/passwd", "action": "approve"})
    assert bad_id["ok"] is False
    assert bad_id["error"] == "invalid_workflow_id"

    bad_action = resume({"workflow_id": "wf-valid-1", "action": "not-a-real-action"})
    assert bad_action["ok"] is False
    assert bad_action["error"] == "invalid_action"

    bad_option = resume({
        "workflow_id": "wf-valid-1", "action": "modify-budget",
        "option": "250; rm -rf /",
    })
    assert bad_option["ok"] is False
    assert bad_option["error"] == "invalid_option"

    bad_critique = resume({
        "workflow_id": "wf-valid-1", "action": "modify-plan",
        "critique_ref": "-x",
    })
    assert bad_critique["ok"] is False
    assert bad_critique["error"] == "invalid_critique_ref"


# ===========================================================================
# Full-stack: an attended workflow paused at a real gate resumes via the
# non-live route without dispatching engineering on the stub.
# ===========================================================================

langgraph = pytest.importorskip("langgraph")

from hydra_core import cli  # noqa: E402
from hydra_core.state import HydraState  # noqa: E402
from hydra_core.supervisor import build_supervisor  # noqa: E402
from mcp_servers.hydra_memory.server import _tool_handlers as mem_handlers  # noqa: E402


class _CliNullDispatcher:
    """Mirrors `hydra_core.cli._NullDispatcher`'s `dry_run = True` marker so
    seeding the initial paused workflow never itself replays the eights spool
    (path S) — the same guard the CLI's real `_NullDispatcher` carries."""

    dry_run = True

    def dispatch(self, *a, **k):  # pragma: no cover
        return None

    def call_tool(self, *a, **k):  # pragma: no cover
        return None


def _start_paused_attended_workflow(
    tmp_path, monkeypatch, *, selected_squads: list[str] | None = None,
) -> str:
    """Run a workflow with an engineering task alongside executive (plus,
    optionally, additional squads — e.g. a claude-skill pack and a stub pack,
    cross-vendor finding 5), pausing at the approval gate (executive's
    requires_human_approval)."""
    monkeypatch.setenv("HYDRA_CHECKPOINT_DB", str(tmp_path / "checkpoints.db"))
    wf = uuid4()
    initial = HydraState(workflow_id=wf, root_goal="resume-attended-route test goal")
    initial.selected_squads = selected_squads or ["executive", "engineering"]
    initial.target_repo_id = "hydra"
    sup = build_supervisor(project_root=REPO_ROOT, dispatcher=_CliNullDispatcher())
    sup.invoke(initial, config={"configurable": {"thread_id": str(wf)}})
    return str(wf)


@pytest.fixture(autouse=True)
def _no_git_harvest(monkeypatch):
    # Never touch the real repo's git state from this test module.
    monkeypatch.setattr(
        "hydra_core.squad_node.harvest_pp_run_artifacts", lambda **_k: None,
        raising=False,
    )


def test_attended_resume_via_cli_defers_engineering_no_stub_completion(
    tmp_path, monkeypatch, capsys
):
    """The exact `python -m hydra_core.cli resume <id> --action approve`
    argv `_run_resume_attended` builds (minus --live), run in-process via
    `cli.main` (mirrors the MU7/MU15 test style): the gate clears,
    pending_hitl clears, and the engineering task is never marked done from
    stub output."""
    wf = _start_paused_attended_workflow(tmp_path, monkeypatch)

    h = mem_handlers()
    pre_status = h["hydra-mem.workflow_status"]({"workflow_id": wf})
    assert pre_status.get("pending_hitl"), (
        "precondition: workflow must be paused at a real HITL gate before resume"
    )

    rc = cli.main(["--project", str(REPO_ROOT), "resume", wf, "--action", "approve"])
    out = json.loads(capsys.readouterr().out)
    assert rc == 0, f"resume failed: {out}"
    assert out.get("resumed") is True
    assert out.get("pending_hitl") in (None, {}), (
        f"pending_hitl must clear after the resolved gate, got {out.get('pending_hitl')!r}"
    )

    post_status = h["hydra-mem.workflow_status"]({"workflow_id": wf})
    assert not post_status.get("pending_hitl"), (
        "the resolved gate must not leave a stale pending_hitl"
    )

    # The engineering task must be deferred to the host, never executed (and
    # therefore never "done") on the stub `_NullDispatcher`.
    eng_tasks = [t for t in post_status.get("tasks", [])
                if t.get("owner_squad") == "engineering"]
    assert eng_tasks, f"no engineering task found; tasks={post_status.get('tasks')}"
    for t in eng_tasks:
        assert t.get("status") != "done", (
            f"MUTATION-GUARD: engineering task marked done from stub output: {t}"
        )
    assert post_status.get("phase") not in ("done", "complete", "synthesis",
                                            "judge_synthesis"), (
        f"an attended non-live resume must not race past await_host into "
        f"synthesis/completion, got phase={post_status.get('phase')!r}"
    )


def test_attended_resume_leaves_populated_spool_byte_identical(
    tmp_path, monkeypatch, capsys
):
    """Same guarantee as test_cli.py's plan/run spool tests: the non-live
    resume path must never replay (and therefore never mutate) a pre-existing
    spooled eights entry."""
    from hydra_core.eights.pending_spool import SpooledCall
    from datetime import datetime, timezone

    pending = tmp_path / "pending"
    dead = tmp_path / "dead"
    monkeypatch.setenv("HYDRA_EIGHTS_SPOOL", str(pending))
    monkeypatch.setenv("HYDRA_EIGHTS_DEAD_LETTER", str(dead))

    pending.mkdir(parents=True, exist_ok=True)
    seed_path = pending / "seed.json"
    call = SpooledCall(
        id="seed", tool="eights.evolution.propose", args={"slug": "seed"},
        spooled_at=datetime.now(timezone.utc).isoformat(), attempts=0,
    )
    seed_path.write_text(call.to_json(), encoding="utf-8")
    seed_before = seed_path.read_bytes()

    wf = _start_paused_attended_workflow(tmp_path, monkeypatch)
    rc = cli.main(["--project", str(REPO_ROOT), "resume", wf, "--action", "approve"])
    capsys.readouterr()
    assert rc == 0

    assert seed_path.is_file(), "replay must not remove/move the pre-existing entry"
    assert seed_path.read_bytes() == seed_before, (
        "a non-live resume must never mutate a pre-existing spooled entry"
    )
    dead_names = {p.name for p in dead.iterdir()} if dead.is_dir() else set()
    assert "seed.json" not in dead_names


# ===========================================================================
# RESOLVE-GATE-ONLY (operator decision A/B/C): --gate-only never re-enters
# the compiled graph, refuses on an unverifiable operator identity before
# touching state, and reports TheEights resolution honestly as deferred.
# ===========================================================================

_OPERATOR_ENV = {"HYDRA_OPERATOR_ID": "lebobo88", "HYDRA_OPERATOR_KEY": "test-key-material"}


def _set_known_operator(monkeypatch) -> None:
    for k, v in _OPERATOR_ENV.items():
        monkeypatch.setenv(k, v)


def test_gate_only_approve_records_no_squad_result_no_spool(
    tmp_path, monkeypatch, capsys
):
    """A gate-only approve of a workflow spanning executive + engineering
    (mcp) + customer-support (claude-skill) + healthcare (stub) clears
    pending_hitl and records NO squad result, NO DecisionRecord, NO judge
    verdict, NO synthesis, and spools NOTHING — the spool directory is
    byte-identical and no new spool files exist."""
    pending = tmp_path / "pending"
    dead = tmp_path / "dead"
    monkeypatch.setenv("HYDRA_EIGHTS_SPOOL", str(pending))
    monkeypatch.setenv("HYDRA_EIGHTS_DEAD_LETTER", str(dead))
    pending.mkdir(parents=True, exist_ok=True)
    dead.mkdir(parents=True, exist_ok=True)
    _set_known_operator(monkeypatch)

    wf = _start_paused_attended_workflow(
        tmp_path, monkeypatch,
        selected_squads=["executive", "engineering", "customer-support", "healthcare"],
    )

    spool_before = sorted(p.name for p in pending.iterdir())
    dead_before = sorted(p.name for p in dead.iterdir())

    config = {"configurable": {"thread_id": wf}}
    sup = build_supervisor(project_root=REPO_ROOT, dispatcher=_CliNullDispatcher())
    pre_snap = sup.get_state(config)
    pre_values = pre_snap.values
    assert pre_values.get("pending_hitl"), "precondition: must pause at a real gate"
    pre_envelope_count = len(pre_values.get("envelopes") or [])
    pre_verdict_count = len(pre_values.get("verdicts") or [])
    pre_artifact_count = len(pre_values.get("artifacts") or [])

    rc = cli.main([
        "--project", str(REPO_ROOT), "resume", wf,
        "--action", "approve", "--gate-only",
    ])
    out = json.loads(capsys.readouterr().out)
    assert rc == 0, f"gate-only approve failed: {out}"
    assert out.get("ok") is True
    assert out.get("resumed") is False, "gate-only never re-enters the graph"
    assert out.get("gate_only") is True
    assert out.get("graph_reentered") is False
    assert out.get("pending_hitl") in (None, {})
    assert out.get("eights_resolution") == "deferred"

    post_snap = sup.get_state(config)
    post_values = post_snap.values
    assert not post_values.get("pending_hitl"), "gate must have cleared"

    # No squad ever ran: no new envelopes (DecisionRecords are envelopes),
    # no new judge verdicts, no new artifacts, no synthesis reached.
    assert len(post_values.get("envelopes") or []) == pre_envelope_count, (
        "gate-only must record NO DecisionRecord / envelope from any squad"
    )
    assert len(post_values.get("verdicts") or []) == pre_verdict_count, (
        "gate-only must record NO judge verdict"
    )
    assert len(post_values.get("artifacts") or []) == pre_artifact_count, (
        "gate-only must record NO artifact"
    )
    assert post_values.get("phase") not in (
        "done", "complete", "synthesis", "judge_synthesis",
    ), f"gate-only must never reach synthesis, got phase={post_values.get('phase')!r}"

    for t in post_values.get("tasks", []):
        status = t.get("status") if isinstance(t, dict) else getattr(t, "status", None)
        owner = t.get("owner_squad") if isinstance(t, dict) else getattr(t, "owner_squad", None)
        assert status != "done", (
            f"MUTATION-GUARD: task for squad={owner!r} marked done under "
            f"gate-only: {t}"
        )

    # Nothing NEW was spooled. The resolved gate's own pre-existing spooled
    # `hydra_gate` hitl_request entry (filed when the workflow first paused,
    # already present in spool_before) is legitimately PRUNED here (C3:
    # `_prune_spooled_hitl_requests`, gate-identity-keyed) so a later spool
    # replay never re-files a ticket for an already-resolved gate — that is a
    # removal, not a write of new spool content, and is expected either way
    # (gate_only or not). The guarantee this test proves is that gate_only
    # adds NOTHING to the spool.
    after_pending = set(p.name for p in pending.iterdir())
    assert after_pending.issubset(set(spool_before)), (
        f"gate-only must never ADD a new spool entry: "
        f"new={after_pending - set(spool_before)}"
    )
    assert sorted(p.name for p in dead.iterdir()) == dead_before, (
        "gate-only must never dead-letter anything"
    )


def test_gate_only_unknown_operator_refuses_before_any_mutation(
    tmp_path, monkeypatch, capsys
):
    """Decision B: with no HYDRA_OPERATOR_ID/HYDRA_OPERATOR_KEY set, a
    gate-only approve REFUSES with operator_identity_required and changes
    NOTHING — pending_hitl, workflow state, and the spool are all unchanged."""
    monkeypatch.delenv("HYDRA_OPERATOR_ID", raising=False)
    monkeypatch.delenv("HYDRA_OPERATOR_KEY", raising=False)
    pending = tmp_path / "pending"
    dead = tmp_path / "dead"
    monkeypatch.setenv("HYDRA_EIGHTS_SPOOL", str(pending))
    monkeypatch.setenv("HYDRA_EIGHTS_DEAD_LETTER", str(dead))
    pending.mkdir(parents=True, exist_ok=True)
    dead.mkdir(parents=True, exist_ok=True)

    wf = _start_paused_attended_workflow(tmp_path, monkeypatch)
    config = {"configurable": {"thread_id": wf}}
    sup = build_supervisor(project_root=REPO_ROOT, dispatcher=_CliNullDispatcher())
    pre_values = sup.get_state(config).values
    assert pre_values.get("pending_hitl")

    spool_before = sorted(p.name for p in pending.iterdir())

    rc = cli.main([
        "--project", str(REPO_ROOT), "resume", wf,
        "--action", "approve", "--gate-only",
    ])
    _captured = capsys.readouterr()
    out = json.loads(_captured.err or _captured.out or "{}")
    assert rc == 1, f"expected refusal exit code, got rc={rc}"
    assert out.get("ok") is False
    assert out.get("error") == "operator_identity_required"
    assert "HYDRA_OPERATOR_ID" in out.get("message", "")

    post_values = sup.get_state(config).values
    assert post_values.get("pending_hitl") == pre_values.get("pending_hitl"), (
        "an identity-refused gate-only resume must not clear pending_hitl"
    )
    assert post_values.get("hitl_history") == pre_values.get("hitl_history"), (
        "an identity-refused gate-only resume must not record a resolution"
    )
    assert sorted(p.name for p in pending.iterdir()) == spool_before


def test_gate_only_reject_and_abort_identical_to_full_resume(tmp_path, monkeypatch, capsys):
    """reject and force-dispatch/abort-option already end without graph
    re-entry in the non-gate-only path — gate-only must behave identically
    (operator decision A)."""
    wf = _start_paused_attended_workflow(tmp_path, monkeypatch)
    rc = cli.main([
        "--project", str(REPO_ROOT), "resume", wf,
        "--action", "reject", "--gate-only",
    ])
    out = json.loads(capsys.readouterr().out)
    assert rc == 0
    assert out["ok"] is True
    assert out["resumed"] is False
    assert out["action"] == "reject"
    assert out["phase"] == "surfaced"
    assert out["gate_only"] is True
    assert out["graph_reentered"] is False


def test_gate_only_force_dispatch_records_policy_override_but_never_invokes(
    tmp_path, monkeypatch, capsys
):
    """force-dispatch is a governance event (policy_override emitted) even
    under gate-only, but the graph itself is never re-entered."""
    _set_known_operator(monkeypatch)
    wf = _start_paused_attended_workflow(tmp_path, monkeypatch)
    rc = cli.main([
        "--project", str(REPO_ROOT), "resume", wf,
        "--action", "force-dispatch", "--gate-only",
    ])
    out = json.loads(capsys.readouterr().out)
    assert rc == 0, f"force-dispatch gate-only failed: {out}"
    assert out["ok"] is True
    assert out["resumed"] is False
    assert out["gate_only"] is True
    assert out["graph_reentered"] is False
    assert out["pending_hitl"] in (None, {})


def test_gate_only_recover_stalled_stage_unaffected_by_flag(tmp_path, monkeypatch, capsys):
    """recover-stalled-stage never touches sup.invoke at all (only
    sup.update_state) -- it must behave identically whether or not
    --gate-only is passed (it returns cursor_not_found here since no cursor
    was ever opened, which is enough to prove the flag changed nothing)."""
    wf = _start_paused_attended_workflow(tmp_path, monkeypatch)
    rc_a = cli.main([
        "--project", str(REPO_ROOT), "resume", wf,
        "--action", "recover-stalled-stage", "--option", "does-not-exist",
    ])
    _cap_a = capsys.readouterr()
    out_a = json.loads(_cap_a.out or _cap_a.err or "{}")
    rc_b = cli.main([
        "--project", str(REPO_ROOT), "resume", wf,
        "--action", "recover-stalled-stage", "--option", "does-not-exist",
        "--gate-only",
    ])
    _cap_b = capsys.readouterr()
    out_b = json.loads(_cap_b.out or _cap_b.err or "{}")
    assert rc_a == rc_b == 1
    assert out_a.get("error") == out_b.get("error") == "cursor_not_found"


# ===========================================================================
# Mutation proofs (reverted immediately after observing the failure --
# see the engineering return summary for the revert record).
# ===========================================================================
#
# Proof 1 -- drop the gate-only flag so the graph is re-entered: temporarily
# force `gate_only = False` unconditionally in `_cmd_resume_locked` (comment
# out the `getattr(args, "gate_only", False)` read) and re-run
# `test_gate_only_approve_records_no_squad_result_no_spool` -- it fails
# because the graph IS re-entered (engineering's task becomes eligible for
# `deferred_to_host` via node_dispatch, which is a DIFFERENT recorded status
# than the pristine "pending" this test expects untouched, and `resumed`
# flips True / `graph_reentered` is never set at all, an AttributeError-shaped
# KeyError on `out["graph_reentered"]`).
#
# Proof 2 -- allow the degraded token to proceed: temporarily remove the
# `if gate_only and _force_degraded: ... return 1` guard (and its sibling
# post-mint degraded check) and re-run
# `test_gate_only_unknown_operator_refuses_before_any_mutation` -- it fails
# because the resume proceeds, clears pending_hitl, and returns
# `resumed_hitl`-shaped success instead of `operator_identity_required`.
