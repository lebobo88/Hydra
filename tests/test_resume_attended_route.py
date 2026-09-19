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
import shutil
import subprocess
import sys
import time
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


def _hermetic_project(tmp_path, monkeypatch) -> Path:
    """An isolated project root (never REPO_ROOT) so a test's own workflow
    telemetry/checkpoint/lock never lands in the shared attended worktree
    checkout — squads/CONSTITUTION.md are redirected to the real REPO_ROOT
    tree (the same pattern tests/test_p5b_plan_lifecycle.py uses)."""
    project = tmp_path / "proj"
    project.mkdir(parents=True, exist_ok=True)
    shutil.copy(REPO_ROOT / "CONSTITUTION.md", project / "CONSTITUTION.md")
    from hydra_core.squad_loader import discover_squads as _real_discover_squads
    monkeypatch.setattr("hydra_core.cli.discover_squads",
                        lambda *_a, **_k: _real_discover_squads(REPO_ROOT))
    monkeypatch.setattr("hydra_core.supervisor.discover_squads",
                        lambda *_a, **_k: _real_discover_squads(REPO_ROOT))
    return project


def _start_paused_attended_workflow(
    tmp_path, monkeypatch, *, selected_squads: list[str] | None = None,
) -> tuple[Path, str]:
    """Run a workflow with an engineering task alongside executive (plus,
    optionally, additional squads — e.g. a claude-skill pack and a stub pack,
    cross-vendor finding 5), pausing at the approval gate (executive's
    requires_human_approval).

    Returns ``(project, workflow_id)``. Uses an isolated hermetic project
    root (never REPO_ROOT, per the same discipline `_hermetic_project` and
    `_start_paused_attended_workflow_at` already apply below) — this was the
    older of the two seeding helpers and, unlike its sibling, used to write
    its `.hydra/<workflow>` checkpoint/lock/telemetry traces straight into
    the shared attended worktree checkout."""
    project = _hermetic_project(tmp_path, monkeypatch)
    monkeypatch.setenv("HYDRA_CHECKPOINT_DB", str(tmp_path / "checkpoints.db"))
    wf = uuid4()
    initial = HydraState(workflow_id=wf, root_goal="resume-attended-route test goal")
    initial.selected_squads = selected_squads or ["executive", "engineering"]
    initial.target_repo_id = "hydra"
    sup = build_supervisor(project_root=project, dispatcher=_CliNullDispatcher())
    sup.invoke(initial, config={"configurable": {"thread_id": str(wf)}})
    return project, str(wf)


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
    project, wf = _start_paused_attended_workflow(tmp_path, monkeypatch)

    h = mem_handlers()
    pre_status = h["hydra-mem.workflow_status"]({"workflow_id": wf})
    assert pre_status.get("pending_hitl"), (
        "precondition: workflow must be paused at a real HITL gate before resume"
    )

    rc = cli.main(["--project", str(project), "resume", wf, "--action", "approve"])
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

    project, wf = _start_paused_attended_workflow(tmp_path, monkeypatch)
    rc = cli.main(["--project", str(project), "resume", wf, "--action", "approve"])
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

    project, wf = _start_paused_attended_workflow(
        tmp_path, monkeypatch,
        selected_squads=["executive", "engineering", "customer-support", "healthcare"],
    )

    spool_before = sorted(p.name for p in pending.iterdir())
    dead_before = sorted(p.name for p in dead.iterdir())

    config = {"configurable": {"thread_id": wf}}
    sup = build_supervisor(project_root=project, dispatcher=_CliNullDispatcher())
    pre_snap = sup.get_state(config)
    pre_values = pre_snap.values
    assert pre_values.get("pending_hitl"), "precondition: must pause at a real gate"
    pre_envelope_count = len(pre_values.get("envelopes") or [])
    pre_verdict_count = len(pre_values.get("verdicts") or [])
    pre_artifact_count = len(pre_values.get("artifacts") or [])

    rc = cli.main([
        "--project", str(project), "resume", wf,
        "--action", "approve", "--gate-only",
    ])
    out = json.loads(capsys.readouterr().out)
    assert rc == 0, f"gate-only approve failed: {out}"
    assert out.get("ok") is True
    assert out.get("resumed") is False, "gate-only never re-enters the graph"
    assert out.get("gate_only") is True
    assert out.get("graph_reentered") is False
    assert out.get("pending_hitl") in (None, {})
    # Operator decision 2: the hermetic suite's HYDRA_TEST_NO_DAEMONS=1
    # blocks the narrow live TheEights client from ever forking a real
    # daemon (see hydra_core.cli._build_gate_only_eights_client), so the
    # attempted resolution honestly reports "unavailable" -- never the old
    # hardcoded "deferred" (this route now genuinely tries, and says so).
    assert out.get("eights_resolution") == "unavailable"
    assert out.get("eights_resolution_reason")

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

    project, wf = _start_paused_attended_workflow(tmp_path, monkeypatch)
    config = {"configurable": {"thread_id": wf}}
    sup = build_supervisor(project_root=project, dispatcher=_CliNullDispatcher())
    pre_values = sup.get_state(config).values
    assert pre_values.get("pending_hitl")

    spool_before = sorted(p.name for p in pending.iterdir())

    rc = cli.main([
        "--project", str(project), "resume", wf,
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
    re-entry in the non-gate-only path — that BEHAVIOR (never re-entering
    the graph) is identical under gate-only too (operator decision A). The
    JSON body itself is additive, not byte-for-byte identical (cross-vendor
    finding 5) -- `gate_only`/`eights_resolution` are new keys, checked
    explicitly below rather than assumed. A known operator identity is
    required here since cross-vendor finding 2 now covers `reject` under
    gate_only too."""
    _set_known_operator(monkeypatch)
    project, wf = _start_paused_attended_workflow(tmp_path, monkeypatch)
    rc = cli.main([
        "--project", str(project), "resume", wf,
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
    project, wf = _start_paused_attended_workflow(tmp_path, monkeypatch)
    rc = cli.main([
        "--project", str(project), "resume", wf,
        "--action", "force-dispatch", "--gate-only",
    ])
    out = json.loads(capsys.readouterr().out)
    assert rc == 0, f"force-dispatch gate-only failed: {out}"
    assert out["ok"] is True
    assert out["resumed"] is False
    assert out["gate_only"] is True
    assert out["graph_reentered"] is False
    assert out["pending_hitl"] in (None, {})


# ===========================================================================
# Cross-vendor findings 2, 3, 6: identity coverage widened to every
# state-mutating action, and the checkpoint-patch/spool-prune interleaving
# is reconciled on retry. `_hermetic_project` is defined above (used by both
# seeding helpers in this module).
# ===========================================================================

def _start_paused_attended_workflow_at(
    project, monkeypatch, *, selected_squads: list[str] | None = None,
) -> str:
    monkeypatch.setenv("HYDRA_CHECKPOINT_DB", str(Path(project) / "checkpoints.db"))
    wf = uuid4()
    initial = HydraState(workflow_id=wf, root_goal="resume-attended-route test goal")
    initial.selected_squads = selected_squads or ["executive", "engineering"]
    initial.target_repo_id = "hydra"
    sup = build_supervisor(project_root=project, dispatcher=_CliNullDispatcher())
    sup.invoke(initial, config={"configurable": {"thread_id": str(wf)}})
    return str(wf)


def test_gate_only_reject_refused_without_identity_state_unchanged(
    tmp_path, monkeypatch, capsys
):
    """Cross-vendor finding 2 (HIGH): `reject` was never a member of the
    historical `_MUTATING_RESUME_ACTIONS` allow-list, so a gate-only reject
    cleared pending_hitl with NO identity check at all. It must now refuse
    identically to an unidentified approve -- pending_hitl, hitl_history,
    phase, and the spool are all unchanged."""
    monkeypatch.delenv("HYDRA_OPERATOR_ID", raising=False)
    monkeypatch.delenv("HYDRA_OPERATOR_KEY", raising=False)
    project = _hermetic_project(tmp_path, monkeypatch)
    pending = tmp_path / "pending"
    dead = tmp_path / "dead"
    monkeypatch.setenv("HYDRA_EIGHTS_SPOOL", str(pending))
    monkeypatch.setenv("HYDRA_EIGHTS_DEAD_LETTER", str(dead))
    pending.mkdir(parents=True, exist_ok=True)
    dead.mkdir(parents=True, exist_ok=True)

    wf = _start_paused_attended_workflow_at(project, monkeypatch)
    config = {"configurable": {"thread_id": wf}}
    sup = build_supervisor(project_root=project, dispatcher=_CliNullDispatcher())
    pre_values = sup.get_state(config).values
    assert pre_values.get("pending_hitl"), "precondition: must pause at a real gate"
    spool_before = sorted(p.name for p in pending.iterdir())

    rc = cli.main([
        "--project", str(project), "resume", wf,
        "--action", "reject", "--gate-only",
    ])
    _cap = capsys.readouterr()
    out = json.loads(_cap.err or _cap.out or "{}")
    assert rc == 1, f"expected refusal exit code, got rc={rc} out={out}"
    assert out.get("ok") is False
    assert out.get("error") == "operator_identity_required"

    post_values = sup.get_state(config).values
    assert post_values.get("pending_hitl") == pre_values.get("pending_hitl"), (
        "an identity-refused gate-only reject must not clear pending_hitl"
    )
    assert post_values.get("hitl_history") == pre_values.get("hitl_history"), (
        "an identity-refused gate-only reject must not record a resolution"
    )
    assert post_values.get("phase") != "surfaced", (
        "an identity-refused gate-only reject must not surface the workflow"
    )
    assert sorted(p.name for p in pending.iterdir()) == spool_before


def test_gate_only_known_identity_missing_key_refused_as_degraded(
    tmp_path, monkeypatch, capsys
):
    """A known HYDRA_OPERATOR_ID with NO HYDRA_OPERATOR_KEY can only ever mint
    a degraded capability (sig.degraded=True); gate-only must refuse it
    exactly like an unknown operator, before touching state. Operator
    decision 1 catches this even earlier than the post-checkpoint mint+verify
    now: the pre-lock precheck (`_precheck_operator_identity_gate_only`)
    refuses before the resume lock is even acquired, since a missing signing
    key already rules out a non-degraded mint regardless of which gate is
    loaded."""
    monkeypatch.setenv("HYDRA_OPERATOR_ID", "lebobo88")
    monkeypatch.delenv("HYDRA_OPERATOR_KEY", raising=False)
    project = _hermetic_project(tmp_path, monkeypatch)
    wf = _start_paused_attended_workflow_at(project, monkeypatch)
    config = {"configurable": {"thread_id": wf}}
    sup = build_supervisor(project_root=project, dispatcher=_CliNullDispatcher())
    pre_values = sup.get_state(config).values

    rc = cli.main([
        "--project", str(project), "resume", wf,
        "--action", "approve", "--gate-only",
    ])
    _cap = capsys.readouterr()
    out = json.loads(_cap.err or _cap.out or "{}")
    assert rc == 1, f"expected refusal exit code, got rc={rc} out={out}"
    assert out.get("ok") is False
    assert out.get("error") == "operator_identity_required"
    assert "HYDRA_OPERATOR_KEY" in out.get("message", "")

    post_values = sup.get_state(config).values
    assert post_values.get("pending_hitl") == pre_values.get("pending_hitl")


def test_gate_only_mint_exception_refused(tmp_path, monkeypatch, capsys):
    """`mint_for_approval` raising outright must refuse gate-only rather than
    proceeding without a capability token (the legacy non-gate_only warn-
    and-proceed posture is deliberately NOT applied on this route)."""
    _set_known_operator(monkeypatch)
    project = _hermetic_project(tmp_path, monkeypatch)
    wf = _start_paused_attended_workflow_at(project, monkeypatch)
    config = {"configurable": {"thread_id": wf}}
    sup = build_supervisor(project_root=project, dispatcher=_CliNullDispatcher())
    pre_values = sup.get_state(config).values

    def _boom(**_k):
        raise RuntimeError("mint exploded")

    monkeypatch.setattr("hydra_core.auth.capability.mint_for_approval", _boom)

    rc = cli.main([
        "--project", str(project), "resume", wf,
        "--action", "approve", "--gate-only",
    ])
    _cap = capsys.readouterr()
    out = json.loads(_cap.err or _cap.out or "{}")
    assert rc == 1, f"expected refusal exit code, got rc={rc} out={out}"
    assert out.get("ok") is False
    assert out.get("error") == "operator_identity_required"
    assert "mint failed" in out.get("message", "")

    post_values = sup.get_state(config).values
    assert post_values.get("pending_hitl") == pre_values.get("pending_hitl")


def test_gate_only_verify_failure_refused(tmp_path, monkeypatch, capsys):
    """A minted capability that fails `verify_operator_capability` (tampered/
    invalid, not merely degraded) must refuse gate-only rather than warn-
    and-proceed."""
    _set_known_operator(monkeypatch)
    project = _hermetic_project(tmp_path, monkeypatch)
    wf = _start_paused_attended_workflow_at(project, monkeypatch)
    config = {"configurable": {"thread_id": wf}}
    sup = build_supervisor(project_root=project, dispatcher=_CliNullDispatcher())
    pre_values = sup.get_state(config).values

    def _fake_verify(*_a, **_k):
        return {"valid": False, "reason": "signature_mismatch"}

    monkeypatch.setattr(
        "hydra_core.auth.capability.verify_operator_capability", _fake_verify)

    rc = cli.main([
        "--project", str(project), "resume", wf,
        "--action", "approve", "--gate-only",
    ])
    _cap = capsys.readouterr()
    out = json.loads(_cap.err or _cap.out or "{}")
    assert rc == 1, f"expected refusal exit code, got rc={rc} out={out}"
    assert "capability_verify_failed" in out.get("error", "")

    post_values = sup.get_state(config).values
    assert post_values.get("pending_hitl") == pre_values.get("pending_hitl")


def test_gate_only_no_pending_gate_prunes_stale_spool_after_interleaved_failure(
    tmp_path, monkeypatch, capsys
):
    """Cross-vendor finding 3 (HIGH): the checkpoint patch (pending_hitl=None,
    hitl_history append) is written BEFORE the spool prune. Simulate a child
    killed exactly between those two writes -- the checkpoint already
    reflects the resolved gate, but its spooled hitl.request survives.

    A retry lands on the BARE-INTERRUPT gate_only early return, not the
    deeper `no_pending_gate` branch: under gate_only `sup.invoke` is never
    called, so `snap.next` never advances past the original interrupt --
    `pending_hitl` is already cleared, but the graph is still "paused"
    there, which is exactly what a bare interrupt looks like. That branch
    must reconcile the spool itself, or `retry_after_partial_gate_only_is_
    safe` (mcp_servers/hydra_control/server.py) is a lie."""
    _set_known_operator(monkeypatch)
    project = _hermetic_project(tmp_path, monkeypatch)
    pending = tmp_path / "pending"
    monkeypatch.setenv("HYDRA_EIGHTS_SPOOL", str(pending))
    monkeypatch.setenv("HYDRA_EIGHTS_DEAD_LETTER", str(tmp_path / "dead"))
    pending.mkdir(parents=True, exist_ok=True)

    wf = _start_paused_attended_workflow_at(project, monkeypatch)
    config = {"configurable": {"thread_id": wf}}
    sup = build_supervisor(project_root=project, dispatcher=_CliNullDispatcher())
    pre_values = sup.get_state(config).values
    pending_hitl = pre_values.get("pending_hitl")
    assert pending_hitl, "precondition: must pause at a real gate"
    gate_node = pending_hitl.get("gate_node")
    assert gate_node

    # Spool a stale hitl.request for the SAME gate, exactly as EightsAttestor
    # would have when the gate was first filed.
    stale = pending / "stale-hitl.json"
    stale.write_text(json.dumps({
        "id": "stale-hitl", "tool": "eights.governance.hitl.request",
        "workflow_id": wf, "attempts": 0,
        "args": {"run_id": wf, "payload": {"gate_node": gate_node}},
    }), encoding="utf-8")

    # Simulate the crash: apply exactly the checkpoint patch
    # `_cmd_resume_locked` would apply for an approve, WITHOUT its spool
    # prune -- a kill between the two writes.
    from datetime import datetime, timezone
    resolution = {
        **pending_hitl, "resolution": "approve", "option": None,
        "resolved_at": datetime.now(timezone.utc).isoformat(),
    }
    sup.update_state(config, {"pending_hitl": None, "hitl_history": [resolution]})
    assert stale.exists(), "precondition: the stale spool entry must still be present"

    # Retry: the same resume call again, now hitting the bare-interrupt
    # gate_only early return (see docstring).
    rc = cli.main([
        "--project", str(project), "resume", wf,
        "--action", "approve", "--gate-only",
    ])
    out = json.loads(capsys.readouterr().out)
    assert rc == 0, f"retry must be a safe no-op, got rc={rc} out={out}"
    assert out.get("pending_hitl") is None
    assert out.get("pruned_spooled_hitl_requests", 0) >= 1, (
        f"the retry must reconcile the stale spooled hitl.request: {out}"
    )
    assert not stale.exists(), (
        "a retry after an interleaved patch-then-prune failure must remove "
        "the stale spooled hitl.request for the already-resolved gate"
    )


def test_gate_only_recover_stalled_stage_refused_before_option_check(
    tmp_path, monkeypatch, capsys
):
    """Cross-vendor finding 1 (CRITICAL): recover-stalled-stage is a LIVE
    operation (a real MCPStdioDispatcher can replay a pp verdict, run
    smoke/finalize, and merge code) -- the OPPOSITE of what gate-only
    promises (operator decision A). The attended (--gate-only) route must
    refuse it outright, even before validating --option / looking for a
    cursor file, so it is refused identically whether or not a matching
    cursor exists. The non-gate-only (would-be --live) CLI path is
    unaffected and still reaches the real cursor-not-found check."""
    project = _hermetic_project(tmp_path, monkeypatch)
    wf = _start_paused_attended_workflow_at(project, monkeypatch)

    rc_gate_only = cli.main([
        "--project", str(project), "resume", wf,
        "--action", "recover-stalled-stage", "--option", "does-not-exist",
        "--gate-only",
    ])
    _cap_go = capsys.readouterr()
    out_go = json.loads(_cap_go.err or _cap_go.out or "{}")
    assert rc_gate_only == 1
    assert out_go.get("ok") is False
    assert out_go.get("error") == "recovery_is_live_operation"
    assert "detached" in out_go.get("message", "").lower()

    rc_legacy = cli.main([
        "--project", str(project), "resume", wf,
        "--action", "recover-stalled-stage", "--option", "does-not-exist",
    ])
    _cap_legacy = capsys.readouterr()
    out_legacy = json.loads(_cap_legacy.out or _cap_legacy.err or "{}")
    assert rc_legacy == 1
    assert out_legacy.get("error") == "cursor_not_found", (
        "the non-gate-only CLI path must be unaffected by the gate-only "
        f"refusal: {out_legacy}"
    )


def test_gate_only_recover_stalled_stage_refused_no_live_dispatcher(
    tmp_path, monkeypatch, capsys
):
    """Cross-vendor finding 1 / 6: with a VALID stalled recovery cursor on
    disk (so the refusal cannot be mistaken for the unrelated
    cursor_not_found early-return), the attended gate-only route must refuse
    BEFORE ever constructing a live `MCPStdioDispatcher` -- the whole point
    of the refusal is that this dispatcher must never be built on this
    route."""
    from hydra_core import host_bridge

    project = _hermetic_project(tmp_path, monkeypatch)
    wf = _start_paused_attended_workflow_at(project, monkeypatch)
    run_id = "stalled-run-1"
    cfile = host_bridge.cursor_path(project, wf, run_id)
    host_bridge.save_cursor(cfile, {
        "schema": host_bridge.CURSOR_SCHEMA,
        "run_id": run_id,
        "workflow_id": wf,
        "task_id": "t-1",
        "stage_id": "generate-0",
        "status": "running",
    })

    class _ShouldNotBeConstructed:
        def __init__(self, *a, **k):
            raise AssertionError(
                "MCPStdioDispatcher must NOT be constructed on the attended "
                "gate-only resume route for recover-stalled-stage"
            )

    monkeypatch.setattr("hydra_core.dispatcher.MCPStdioDispatcher",
                        _ShouldNotBeConstructed)

    rc = cli.main([
        "--project", str(project), "resume", wf,
        "--action", "recover-stalled-stage", "--option", run_id,
        "--gate-only",
    ])
    _cap = capsys.readouterr()
    out = json.loads(_cap.err or _cap.out or "{}")
    assert rc == 1, f"expected refusal exit code, got rc={rc} out={out}"
    assert out.get("ok") is False
    assert out.get("error") == "recovery_is_live_operation"
    assert cfile.exists(), "the refusal must not touch the cursor file"


def test_run_resume_attended_refuses_recover_stalled_stage_before_subprocess(
    monkeypatch,
):
    """Cross-vendor finding 1: the MCP server (`_run_resume_attended`) must
    refuse recover-stalled-stage BEFORE ever calling `_run_cli_json` -- i.e.
    before any subprocess is spawned -- rather than relying solely on the
    CLI's own `--gate-only` refusal (defence in depth, server side)."""
    from mcp_servers.hydra_control import server as _srv

    def _boom(*a, **k):
        raise AssertionError(
            "_run_cli_json must NOT be called for recover-stalled-stage on "
            "the attended resume route"
        )

    monkeypatch.setattr(_srv, "_run_cli_json", _boom)

    out = _srv._run_resume_attended("wf-recover-1", "recover-stalled-stage", "run-1")
    assert out.get("ok") is False
    assert out.get("error") == "recovery_is_live_operation"


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
#
# Proof 3 -- move the single top-of-path auth check below the no-pending
# branches: temporarily comment out the `if gate_only:` capability-resolution
# block immediately after `pending = values.get("pending_hitl")` in
# `_cmd_resume_locked` and re-run
# `test_gate_only_unauthenticated_bare_interrupt_approve_refuses_no_side_effects`
# below -- it fails because the bare-interrupt approve proceeds (writes the
# `hitl_resumed` telemetry event and prunes the spool) with no identity
# check at all.


# ===========================================================================
# RESOLVE-GATE-ONLY follow-up (cross-vendor critique of ed04aac,
# gpt-5.6-terra): authenticate EXACTLY ONCE at the top of the gate-only
# path, before ANY side effect -- covering every no-pending branch, which
# previously wrote telemetry and pruned the spool with zero identity
# verification.
# ===========================================================================

def _trace_bytes(project: Path, wf: str) -> bytes | None:
    from hydra_core.telemetry import trace_path
    p = trace_path(project, wf)
    return p.read_bytes() if p.is_file() else None


def _advance_to_bare_interrupt(project, monkeypatch, capsys) -> str:
    """Start a paused (`executive`-only) workflow and clear its one real gate
    via a legacy (non-gate-only) approve, landing at the bare synthesis
    interrupt (`pending_hitl=None`, `snap.next` non-empty) -- the same setup
    `tests/test_micro_usage_audit.py`'s MU7 tests use."""
    wf = _start_paused_attended_workflow_at(
        project, monkeypatch, selected_squads=["executive"])
    rc = cli.main(["--project", str(project), "resume", wf, "--action", "approve"])
    capsys.readouterr()
    assert rc == 0
    config = {"configurable": {"thread_id": wf}}
    sup = build_supervisor(project_root=project, dispatcher=_CliNullDispatcher())
    snap = sup.get_state(config)
    assert not snap.values.get("pending_hitl"), (
        "precondition: bare interrupt requires pending_hitl to already be clear"
    )
    assert getattr(snap, "next", ()), (
        "precondition: bare interrupt requires a non-empty snap.next"
    )
    return wf


@pytest.mark.parametrize("action", ["approve", "force-dispatch"])
def test_gate_only_unauthenticated_bare_interrupt_refuses_no_side_effects(
    action, tmp_path, monkeypatch, capsys
):
    """CRITICAL (finding 1): an unauthenticated gate-only approve/force-
    dispatch on a BARE interrupt (no real pending_hitl gate) must refuse with
    operator_identity_required -- and must do so BEFORE the `hitl_resumed`
    telemetry write and the stale-spool prune this branch used to run
    unconditionally. The trace file and the spool directory must be
    byte-identical/untouched across the call."""
    monkeypatch.delenv("HYDRA_OPERATOR_ID", raising=False)
    monkeypatch.delenv("HYDRA_OPERATOR_KEY", raising=False)
    project = _hermetic_project(tmp_path, monkeypatch)
    pending = tmp_path / "pending"
    dead = tmp_path / "dead"
    monkeypatch.setenv("HYDRA_EIGHTS_SPOOL", str(pending))
    monkeypatch.setenv("HYDRA_EIGHTS_DEAD_LETTER", str(dead))
    pending.mkdir(parents=True, exist_ok=True)
    dead.mkdir(parents=True, exist_ok=True)

    wf = _advance_to_bare_interrupt(project, monkeypatch, capsys)
    trace_before = _trace_bytes(project, wf)
    spool_before = sorted(p.name for p in pending.iterdir())

    rc = cli.main([
        "--project", str(project), "resume", wf,
        "--action", action, "--gate-only",
    ])
    _cap = capsys.readouterr()
    out = json.loads(_cap.err or _cap.out or "{}")
    assert rc == 1, f"expected refusal exit code, got rc={rc} out={out}"
    assert out.get("ok") is False
    assert out.get("error") == "operator_identity_required"

    assert _trace_bytes(project, wf) == trace_before, (
        "an identity-refused gate-only bare-interrupt resume must write NO "
        "telemetry (hitl_resumed or otherwise)"
    )
    assert sorted(p.name for p in pending.iterdir()) == spool_before, (
        "an identity-refused gate-only bare-interrupt resume must not prune "
        "or otherwise touch the spool"
    )


def test_gate_only_unauthenticated_bare_interrupt_reject_refuses_no_side_effects(
    tmp_path, monkeypatch, capsys
):
    """Same guarantee as above, for the bare-interrupt reject branch (which
    mutates checkpoint state -- `sup.update_state(config, {"phase":
    "surfaced"})` -- with no pending_hitl dict to attach a capability to)."""
    monkeypatch.delenv("HYDRA_OPERATOR_ID", raising=False)
    monkeypatch.delenv("HYDRA_OPERATOR_KEY", raising=False)
    project = _hermetic_project(tmp_path, monkeypatch)
    pending = tmp_path / "pending"
    monkeypatch.setenv("HYDRA_EIGHTS_SPOOL", str(pending))
    monkeypatch.setenv("HYDRA_EIGHTS_DEAD_LETTER", str(tmp_path / "dead"))
    pending.mkdir(parents=True, exist_ok=True)

    wf = _advance_to_bare_interrupt(project, monkeypatch, capsys)
    config = {"configurable": {"thread_id": wf}}
    sup = build_supervisor(project_root=project, dispatcher=_CliNullDispatcher())
    pre_values = sup.get_state(config).values
    trace_before = _trace_bytes(project, wf)
    spool_before = sorted(p.name for p in pending.iterdir())

    rc = cli.main([
        "--project", str(project), "resume", wf,
        "--action", "reject", "--gate-only",
    ])
    _cap = capsys.readouterr()
    out = json.loads(_cap.err or _cap.out or "{}")
    assert rc == 1, f"expected refusal exit code, got rc={rc} out={out}"
    assert out.get("ok") is False
    assert out.get("error") == "operator_identity_required"

    post_values = sup.get_state(config).values
    assert post_values.get("phase") == pre_values.get("phase"), (
        "an identity-refused gate-only bare-interrupt reject must not "
        "surface the workflow"
    )
    assert _trace_bytes(project, wf) == trace_before
    assert sorted(p.name for p in pending.iterdir()) == spool_before


def test_gate_only_unauthenticated_no_pending_gate_refuses_no_side_effects(
    tmp_path, monkeypatch, capsys
):
    """CRITICAL (finding 1): the frozen `no_pending_gate` terminal branch
    (snap.next empty) also used to prune the spool with zero identity
    verification. An unauthenticated gate-only call here must refuse before
    that reconciliation runs."""
    monkeypatch.delenv("HYDRA_OPERATOR_ID", raising=False)
    monkeypatch.delenv("HYDRA_OPERATOR_KEY", raising=False)
    project = _hermetic_project(tmp_path, monkeypatch)
    pending = tmp_path / "pending"
    monkeypatch.setenv("HYDRA_EIGHTS_SPOOL", str(pending))
    monkeypatch.setenv("HYDRA_EIGHTS_DEAD_LETTER", str(tmp_path / "dead"))
    pending.mkdir(parents=True, exist_ok=True)

    _set_known_operator(monkeypatch)
    wf = _start_paused_attended_workflow_at(
        project, monkeypatch, selected_squads=["executive"])
    # Drive the workflow to a genuinely terminal state (snap.next empty) via
    # the legacy (non-gate-only, identified) transport -- mirrors MU7's
    # test_mu7_terminal_still_no_pending_gate.
    for _ in range(3):
        rc = cli.main(["--project", str(project), "resume", wf, "--action", "approve"])
        capsys.readouterr()
        assert rc == 0

    # Now drop identity and attempt an unauthenticated gate-only resume.
    monkeypatch.delenv("HYDRA_OPERATOR_ID", raising=False)
    monkeypatch.delenv("HYDRA_OPERATOR_KEY", raising=False)
    trace_before = _trace_bytes(project, wf)
    spool_before = sorted(p.name for p in pending.iterdir())

    rc = cli.main([
        "--project", str(project), "resume", wf,
        "--action", "approve", "--gate-only",
    ])
    _cap = capsys.readouterr()
    out = json.loads(_cap.err or _cap.out or "{}")
    assert rc == 1, f"expected refusal exit code, got rc={rc} out={out}"
    assert out.get("ok") is False
    assert out.get("error") == "operator_identity_required"

    assert _trace_bytes(project, wf) == trace_before
    assert sorted(p.name for p in pending.iterdir()) == spool_before


def test_gate_only_live_conflict_rejected_before_dispatcher_or_spool(
    tmp_path, monkeypatch, capsys
):
    """Finding 3: `--gate-only --live` must be rejected before ANY work --
    before a dispatcher is constructed and before the spool is read. The
    argparse mutually-exclusive group raises SystemExit(2) before
    `hydra_core.cli._cmd_resume_locked` (and therefore this module's own
    body) ever executes, so neither spy below can fire without turning the
    expected SystemExit into an AssertionError."""
    from hydra_core import dispatcher as _dispatcher_mod

    class _ShouldNotBeConstructed:
        def __init__(self, *a, **k):
            raise AssertionError(
                "MCPStdioDispatcher must NOT be constructed when "
                "--gate-only --live is rejected before any work"
            )

    monkeypatch.setattr(_dispatcher_mod, "MCPStdioDispatcher", _ShouldNotBeConstructed)

    def _boom_prune(*a, **k):
        raise AssertionError(
            "_prune_spooled_hitl_requests must NOT be called when "
            "--gate-only --live is rejected before any work"
        )

    monkeypatch.setattr(cli, "_prune_spooled_hitl_requests", _boom_prune)

    with pytest.raises(SystemExit) as exc_info:
        cli.main([
            "--project", str(tmp_path), "resume", "wf-conflict",
            "--action", "approve", "--gate-only", "--live",
        ])
    assert exc_info.value.code == 2


def test_gate_only_live_conflict_runtime_guard_defence_in_depth(
    tmp_path, monkeypatch, capsys
):
    """Defence in depth: even a caller that bypasses argparse entirely (a
    hand-built `args` namespace, e.g. a future non-CLI caller of
    `_cmd_resume_locked`) is refused by the explicit runtime check at the
    very top of the function -- before the recover-stalled-stage branch,
    before any dispatcher construction, and before the checkpoint is ever
    loaded."""
    from types import SimpleNamespace
    from hydra_core import dispatcher as _dispatcher_mod

    class _ShouldNotBeConstructed:
        def __init__(self, *a, **k):
            raise AssertionError(
                "MCPStdioDispatcher must NOT be constructed under the "
                "runtime gate_only+live guard"
            )

    monkeypatch.setattr(_dispatcher_mod, "MCPStdioDispatcher", _ShouldNotBeConstructed)

    args = SimpleNamespace(
        project=str(tmp_path), workflow_id="wf-conflict", action="approve",
        option=None, critique_ref=None, live=True, gate_only=True,
        verbose=False, operator=None,
    )
    rc = cli._cmd_resume_locked(args, tmp_path, "wf-conflict", "approve", None)
    _cap = capsys.readouterr()
    out = json.loads(_cap.err or _cap.out or "{}")
    assert rc == 1
    assert out.get("ok") is False
    assert out.get("error") == "gate_only_live_conflict"


def test_gate_only_degraded_reason_verify_failure_refuses(
    tmp_path, monkeypatch, capsys
):
    """Finding 4: a verifier that returns `{valid: False, reason: "degraded
    ..."}` must be refused under gate_only -- the substring-based
    degrade-and-proceed posture is legacy-only and must NOT apply here."""
    _set_known_operator(monkeypatch)
    project = _hermetic_project(tmp_path, monkeypatch)
    wf = _start_paused_attended_workflow_at(project, monkeypatch)
    config = {"configurable": {"thread_id": wf}}
    sup = build_supervisor(project_root=project, dispatcher=_CliNullDispatcher())
    pre_values = sup.get_state(config).values

    def _fake_verify(*_a, **_k):
        return {"valid": False, "reason": "degraded: no operator key configured"}

    monkeypatch.setattr(
        "hydra_core.auth.capability.verify_operator_capability", _fake_verify)

    rc = cli.main([
        "--project", str(project), "resume", wf,
        "--action", "approve", "--gate-only",
    ])
    _cap = capsys.readouterr()
    out = json.loads(_cap.err or _cap.out or "{}")
    assert rc == 1, f"expected refusal exit code, got rc={rc} out={out}"
    assert out.get("ok") is False
    assert out.get("error") == "operator_identity_required"

    post_values = sup.get_state(config).values
    assert post_values.get("pending_hitl") == pre_values.get("pending_hitl"), (
        "a degraded-reason verify failure must refuse under gate_only, not "
        "proceed"
    )


def test_gate_only_verify_exception_refuses(tmp_path, monkeypatch, capsys):
    """Finding 4: `verify_operator_capability` raising an exception must
    refuse under gate_only rather than warn-and-proceed (an exception is not
    proof of a valid capability)."""
    _set_known_operator(monkeypatch)
    project = _hermetic_project(tmp_path, monkeypatch)
    wf = _start_paused_attended_workflow_at(project, monkeypatch)
    config = {"configurable": {"thread_id": wf}}
    sup = build_supervisor(project_root=project, dispatcher=_CliNullDispatcher())
    pre_values = sup.get_state(config).values

    def _boom(*_a, **_k):
        raise RuntimeError("verify exploded")

    monkeypatch.setattr(
        "hydra_core.auth.capability.verify_operator_capability", _boom)

    rc = cli.main([
        "--project", str(project), "resume", wf,
        "--action", "approve", "--gate-only",
    ])
    _cap = capsys.readouterr()
    out = json.loads(_cap.err or _cap.out or "{}")
    assert rc == 1, f"expected refusal exit code, got rc={rc} out={out}"
    assert out.get("ok") is False
    assert out.get("error") == "operator_identity_required"

    post_values = sup.get_state(config).values
    assert post_values.get("pending_hitl") == pre_values.get("pending_hitl")


# ===========================================================================
# Operator decision 1: identity is verified BEFORE the resume lock exists.
# ===========================================================================

def test_precheck_unauthenticated_never_existed_workflow_creates_no_files(
    tmp_path, monkeypatch, capsys
):
    """An unauthenticated gate-only resume for a workflow id that NEVER
    existed must create NOTHING on disk anywhere under the isolated state
    root -- no `.hydra/<workflow>/` lock directory, no resume.lock, no
    checkpoint database, no telemetry -- because
    `_precheck_operator_identity_gate_only` runs in `_cmd_resume` BEFORE
    `_acquire_resume_lock` (whose `lock_dir.mkdir(...)` is otherwise the
    very first side effect) and BEFORE `build_supervisor` ever opens the
    checkpoint database. The whole temp state tree is snapshotted
    before/after -- not just the two paths this test happens to think of."""
    monkeypatch.delenv("HYDRA_OPERATOR_ID", raising=False)
    monkeypatch.delenv("HYDRA_OPERATOR_KEY", raising=False)
    project = tmp_path / "proj"
    project.mkdir(parents=True, exist_ok=True)
    ckpt_db = tmp_path / "checkpoints.db"
    monkeypatch.setenv("HYDRA_CHECKPOINT_DB", str(ckpt_db))

    def _snapshot() -> set[str]:
        return {str(p.relative_to(tmp_path)) for p in tmp_path.rglob("*")}

    before = _snapshot()
    wf = str(uuid4())

    rc = cli.main([
        "--project", str(project), "resume", wf,
        "--action", "approve", "--gate-only",
    ])
    _cap = capsys.readouterr()
    out = json.loads(_cap.err or _cap.out or "{}")
    assert rc == 1, f"expected refusal exit code, got rc={rc} out={out}"
    assert out.get("ok") is False
    assert out.get("error") == "operator_identity_required"

    after = _snapshot()
    assert after == before, (
        "an unauthenticated gate-only resume for a workflow that never "
        f"existed must create NOTHING on disk: new={after - before}"
    )
    assert not (project / ".hydra").exists(), (
        "no .hydra/<workflow> resume-lock directory may be created"
    )
    assert not ckpt_db.exists(), "no checkpoint database may be created"


# ===========================================================================
# Operator decision 2: TheEights is resolved NOW on the gate-only route, with
# a narrow, spool-free, replay-free live call. `GateOnlyHitlClient` is
# injected via `hydra_core.cli._build_gate_only_eights_client` so no test
# here ever needs a real daemon.
# ===========================================================================

class _StubReachableEightsClient:
    """A reachable TheEights: one pending ticket matching the resolved gate."""

    def __init__(self, workflow_id: str, gate_node: str | None):
        self._workflow_id = workflow_id
        self._gate_node = gate_node
        self.resolved_calls: list[tuple[str, str, str]] = []

    def hitl_list(self, *, status: str = "pending", kind=None):
        return [{
            "request_id": "req-1",
            "kind": "hydra_gate",
            "payload": {
                "workflow_id": self._workflow_id,
                "gate_node": self._gate_node,
            },
        }]

    def hitl_resolve(self, *, request_id: str, decision: str, note: str = ""):
        self.resolved_calls.append((request_id, decision, note))
        return {"status": "ok", "request_id": request_id}


class _StubUnreachableEightsClient:
    """An unreachable TheEights: hitl_list reports the daemon never serviced
    the call (the same `None` `EightsAttestor.hitl_list` and
    `GateOnlyHitlClient.hitl_list` both return on failure)."""

    def hitl_list(self, *, status: str = "pending", kind=None):
        return None

    def hitl_resolve(self, **_kw):  # pragma: no cover
        raise AssertionError(
            "hitl_resolve must never be called once hitl_list already "
            "reported TheEights unreachable"
        )


def _forbid_replay_and_spool(monkeypatch) -> None:
    """Spy fixtures for the mutation proof: replay_pending,
    replay_pending_async, and any spool write must never fire on the
    gate-only eights resolution path."""
    from hydra_core.eights.attestation import EightsAttestor
    from hydra_core.eights.pending_spool import PendingSpool

    def _boom_replay(self, *a, **k):
        raise AssertionError(
            "replay_pending must NEVER be called on the gate-only resume "
            "route's TheEights resolution"
        )

    def _boom_replay_async(self, *a, **k):
        raise AssertionError(
            "replay_pending_async must NEVER be called on the gate-only "
            "resume route's TheEights resolution"
        )

    def _boom_spool(self, **kw):
        raise AssertionError(
            "nothing may be spooled on the gate-only resume route's "
            f"TheEights resolution: {kw}"
        )

    monkeypatch.setattr(EightsAttestor, "replay_pending", _boom_replay)
    monkeypatch.setattr(EightsAttestor, "replay_pending_async", _boom_replay_async)
    monkeypatch.setattr(PendingSpool, "spool", _boom_spool)


def test_gate_only_reachable_eights_resolves_no_replay_no_spool(
    tmp_path, monkeypatch, capsys
):
    """A gate-only approve with an injected REACHABLE TheEights resolves the
    matching ticket and reports "resolved" -- and never touches replay or
    the spool."""
    _set_known_operator(monkeypatch)
    project, wf = _start_paused_attended_workflow(tmp_path, monkeypatch)
    config = {"configurable": {"thread_id": wf}}
    sup = build_supervisor(project_root=project, dispatcher=_CliNullDispatcher())
    pending_hitl = sup.get_state(config).values.get("pending_hitl")
    assert pending_hitl, "precondition: must pause at a real gate"
    gate_node = pending_hitl.get("gate_node")

    stub = _StubReachableEightsClient(wf, gate_node)
    monkeypatch.setattr(cli, "_build_gate_only_eights_client",
                        lambda _project, _wf: stub)
    _forbid_replay_and_spool(monkeypatch)

    rc = cli.main([
        "--project", str(project), "resume", wf,
        "--action", "approve", "--gate-only",
    ])
    out = json.loads(capsys.readouterr().out)
    assert rc == 0, f"gate-only approve failed: {out}"
    assert out.get("pending_hitl") in (None, {})
    assert out.get("eights_resolution") == "resolved"
    assert out.get("eights_resolved_count") == 1
    assert stub.resolved_calls == [("req-1", "approved", "hydra resume: approve")]


def test_gate_only_unreachable_eights_reports_unavailable_gate_still_clears(
    tmp_path, monkeypatch, capsys
):
    """With an injected UNREACHABLE TheEights: the result reports
    "unavailable" with a reason, the local gate is still cleared, and
    nothing is spooled/replayed."""
    _set_known_operator(monkeypatch)
    project, wf = _start_paused_attended_workflow(tmp_path, monkeypatch)

    stub = _StubUnreachableEightsClient()
    monkeypatch.setattr(cli, "_build_gate_only_eights_client",
                        lambda _project, _wf: stub)
    _forbid_replay_and_spool(monkeypatch)

    rc = cli.main([
        "--project", str(project), "resume", wf,
        "--action", "approve", "--gate-only",
    ])
    out = json.loads(capsys.readouterr().out)
    assert rc == 0, f"gate-only approve failed: {out}"
    assert out.get("pending_hitl") in (None, {}), (
        "the local gate must still clear even when TheEights is unreachable"
    )
    assert out.get("eights_resolution") == "unavailable"
    assert out.get("eights_resolution_reason") == "eights_unreachable"
    assert "eights_resolved_count" not in out


# ===========================================================================
# Cross-vendor critique of add9e1a (gpt-5.6-terra, revise; security 4):
# findings 1a/1b (bounded inner deadline + retry-reconciliation) and 2
# (recover-stalled-stage refused before the resume lock).
# ===========================================================================

class _StubSlowEightsClient:
    """A reachable-but-WEDGED TheEights: `hitl_list` blocks longer than the
    gate-only inner deadline. Sleeps in `hitl_list` (before any matching or
    resolving) so `hitl_resolve` is never reached, deterministically proving
    the abandoned attempt never got far enough to write anything."""

    def __init__(self, delay_s: float):
        self._delay_s = delay_s
        self.resolved_calls: list[tuple[str, str, str]] = []

    def hitl_list(self, *, status: str = "pending", kind=None):
        time.sleep(self._delay_s)
        return [{"request_id": "req-slow", "kind": "hydra_gate",
                "payload": {"workflow_id": "irrelevant", "gate_node": None}}]

    def hitl_resolve(self, *, request_id: str, decision: str, note: str = ""):
        self.resolved_calls.append((request_id, decision, note))
        return {"status": "ok", "request_id": request_id}


class _FakeGateOnlyDispatcher:
    """Records whether/how `close_pooled_sessions` was invoked -- the real
    method lives on `MCPStdioDispatcher` (hydra_core/dispatcher.py); this
    fake stands in for it via `_StubSlowEightsClientWithDispatcher.dispatcher`
    so the mutation-sensitive close-on-deadline behavior can be tested
    without a live TheEights daemon."""

    def __init__(self):
        self.close_calls: list[float] = []

    def close_pooled_sessions(self, *, timeout_s: float = 1.5):
        self.close_calls.append(timeout_s)


class _StubSlowEightsClientWithDispatcher(_StubSlowEightsClient):
    """`_StubSlowEightsClient` plus a `.dispatcher` attribute exposing the
    fake `close_pooled_sessions`, matching `GateOnlyHitlClient`'s public
    `dispatcher` attribute (cross-vendor finding 2)."""

    def __init__(self, delay_s: float):
        super().__init__(delay_s)
        self.dispatcher = _FakeGateOnlyDispatcher()


class _StubNoTicketEightsClient:
    """A REACHABLE TheEights with no matching (or no) pending ticket at all
    -- distinct from `_StubUnreachableEightsClient` (which reports the
    daemon unreachable, `hitl_list` -> None). This is the "already resolved
    on TheEights' side" case a retry-reconciliation must treat as success."""

    def __init__(self):
        self.resolved_calls: list[tuple[str, str, str]] = []

    def hitl_list(self, *, status: str = "pending", kind=None):
        return []

    def hitl_resolve(self, **_kw):  # pragma: no cover
        raise AssertionError(
            "hitl_resolve must never be called when hitl_list reports no "
            "matching ticket"
        )


def test_gate_only_slow_eights_reports_unavailable_within_inner_deadline(
    tmp_path, monkeypatch, capsys
):
    """Finding 1a: an injected TheEights client SLOWER than the inner
    deadline must not block the gate-only resume past that deadline. The
    call returns "unavailable" (reason: deadline) well inside the inner
    budget, the local gate still clears, nothing is spooled, and the slow
    client's `hitl_resolve` is never reached (proving the abandoned attempt
    got nowhere close to writing anything)."""
    _set_known_operator(monkeypatch)
    monkeypatch.setenv("HYDRA_GATE_ONLY_EIGHTS_TIMEOUT_S", "0.3")
    project, wf = _start_paused_attended_workflow(tmp_path, monkeypatch)

    slow = _StubSlowEightsClient(delay_s=5.0)
    monkeypatch.setattr(cli, "_build_gate_only_eights_client",
                        lambda _project, _wf: slow)
    _forbid_replay_and_spool(monkeypatch)

    _t0 = time.perf_counter()
    rc = cli.main([
        "--project", str(project), "resume", wf,
        "--action", "approve", "--gate-only",
    ])
    _elapsed = time.perf_counter() - _t0
    out = json.loads(capsys.readouterr().out)
    assert rc == 0, f"gate-only approve failed: {out}"
    assert _elapsed < 3.0, (
        f"gate-only resume took {_elapsed:.2f}s against a 5s-slow eights "
        "client -- the inner deadline did not bound the call"
    )
    assert out.get("pending_hitl") in (None, {}), (
        "the local gate must still clear even when TheEights is wedged"
    )
    assert out.get("eights_resolution") == "unavailable"
    assert out.get("eights_resolution_reason") == "deadline"
    assert slow.resolved_calls == [], (
        "the abandoned slow attempt must never reach hitl_resolve within "
        "the test's lifetime"
    )


def test_gate_only_slow_eights_closes_dispatcher_session_on_deadline(
    tmp_path, monkeypatch, capsys
):
    """Cross-vendor finding 2: when the inner deadline fires, the gate-only
    resume must make a best-effort attempt to close the dispatcher/session
    the abandoned attempt was using (`close_pooled_sessions`) -- proven here
    via a fake dispatcher (`_FakeGateOnlyDispatcher`) that records whether
    it was called, bounded so the whole resume still returns quickly."""
    _set_known_operator(monkeypatch)
    monkeypatch.setenv("HYDRA_GATE_ONLY_EIGHTS_TIMEOUT_S", "0.3")
    project, wf = _start_paused_attended_workflow(tmp_path, monkeypatch)

    slow = _StubSlowEightsClientWithDispatcher(delay_s=5.0)
    monkeypatch.setattr(cli, "_build_gate_only_eights_client",
                        lambda _project, _wf: slow)
    _forbid_replay_and_spool(monkeypatch)

    _t0 = time.perf_counter()
    rc = cli.main([
        "--project", str(project), "resume", wf,
        "--action", "approve", "--gate-only",
    ])
    _elapsed = time.perf_counter() - _t0
    out = json.loads(capsys.readouterr().out)
    assert rc == 0, f"gate-only approve failed: {out}"
    assert _elapsed < 3.0, (
        f"gate-only resume took {_elapsed:.2f}s -- the close-on-deadline "
        "attempt must stay bounded, not block the resume"
    )
    assert out.get("eights_resolution") == "unavailable"
    assert out.get("eights_resolution_reason") == "deadline"
    assert slow.dispatcher.close_calls, (
        "the abandoned attempt's dispatcher must have close_pooled_sessions "
        "invoked on the inner deadline"
    )


def test_gate_only_retry_no_pending_reconciles_and_resolves(
    tmp_path, monkeypatch, capsys
):
    """Finding 1b: a retry that lands on the no-pending-gate route (the
    checkpoint already shows the gate cleared, e.g. a prior attempt was
    killed after the checkpoint patch but before/during TheEights
    resolution) must reconcile TheEights' ledger for the last resolved gate
    -- with a REACHABLE client showing a still-pending ticket, it resolves
    it and reports "resolved", using the ORIGINAL action's decision."""
    _set_known_operator(monkeypatch)
    project = _hermetic_project(tmp_path, monkeypatch)

    wf = _start_paused_attended_workflow_at(project, monkeypatch)
    config = {"configurable": {"thread_id": wf}}
    sup = build_supervisor(project_root=project, dispatcher=_CliNullDispatcher())
    pre_values = sup.get_state(config).values
    pending_hitl = pre_values.get("pending_hitl")
    assert pending_hitl, "precondition: must pause at a real gate"
    gate_node = pending_hitl.get("gate_node")
    assert gate_node

    # Simulate a prior partial gate-only approve: checkpoint patched
    # (pending_hitl cleared, hitl_history recorded) but never reconciled
    # against TheEights (the exact interleaving this fix targets).
    from datetime import datetime, timezone
    resolution = {
        **pending_hitl, "resolution": "approve", "option": None,
        "resolved_at": datetime.now(timezone.utc).isoformat(),
    }
    sup.update_state(config, {"pending_hitl": None, "hitl_history": [resolution]})

    stub = _StubReachableEightsClient(wf, gate_node)
    monkeypatch.setattr(cli, "_build_gate_only_eights_client",
                        lambda _project, _wf: stub)
    _forbid_replay_and_spool(monkeypatch)

    rc = cli.main([
        "--project", str(project), "resume", wf,
        "--action", "approve", "--gate-only",
    ])
    out = json.loads(capsys.readouterr().out)
    assert rc == 0, f"retry must be a safe no-op, got out={out}"
    assert out.get("pending_hitl") is None
    assert out.get("eights_resolution") == "resolved", out
    assert out.get("eights_resolved_count") == 1
    assert stub.resolved_calls == [
        ("req-1", "approved", "hydra resume retry-reconcile: approve")
    ]


def test_gate_only_retry_no_pending_ticket_reports_none_pending_no_error(
    tmp_path, monkeypatch, capsys
):
    """Finding 1b: the same retry, but TheEights shows NO pending ticket for
    the last-resolved gate (already resolved, e.g. by an earlier attempt
    that got further than this retry needs to know about) -- reported as
    "none_pending", never an error, and `hitl_resolve` is never called."""
    _set_known_operator(monkeypatch)
    project = _hermetic_project(tmp_path, monkeypatch)

    wf = _start_paused_attended_workflow_at(project, monkeypatch)
    config = {"configurable": {"thread_id": wf}}
    sup = build_supervisor(project_root=project, dispatcher=_CliNullDispatcher())
    pre_values = sup.get_state(config).values
    pending_hitl = pre_values.get("pending_hitl")
    assert pending_hitl, "precondition: must pause at a real gate"

    from datetime import datetime, timezone
    resolution = {
        **pending_hitl, "resolution": "approve", "option": None,
        "resolved_at": datetime.now(timezone.utc).isoformat(),
    }
    sup.update_state(config, {"pending_hitl": None, "hitl_history": [resolution]})

    stub = _StubNoTicketEightsClient()
    monkeypatch.setattr(cli, "_build_gate_only_eights_client",
                        lambda _project, _wf: stub)
    _forbid_replay_and_spool(monkeypatch)

    rc = cli.main([
        "--project", str(project), "resume", wf,
        "--action", "approve", "--gate-only",
    ])
    out = json.loads(capsys.readouterr().out)
    assert rc == 0, f"retry must be a safe no-op, got out={out}"
    assert out.get("pending_hitl") is None
    assert out.get("eights_resolution") == "none_pending", out
    assert "eights_resolution_reason" not in out
    assert stub.resolved_calls == []


def test_gate_only_recover_stalled_stage_refused_no_files_written(
    tmp_path, monkeypatch, capsys
):
    """Finding 2: recover-stalled-stage under --gate-only must be refused
    BEFORE `_acquire_resume_lock` creates `.hydra/<workflow>/` and
    `resume.lock` -- a full temp-tree snapshot before/after the refusal must
    be byte-for-byte identical (no file created, moved, or modified)."""
    project = _hermetic_project(tmp_path, monkeypatch)
    wf = _start_paused_attended_workflow_at(project, monkeypatch)

    def _snapshot() -> set[str]:
        return {
            str(p.relative_to(project))
            for p in project.rglob("*") if p.is_file()
        }

    before = _snapshot()
    # `.hydra/<workflow>/` may already exist from the initial invoke's own
    # telemetry/checkpoint writes (unrelated to the resume lock); the
    # smoking gun for finding 2 is specifically `resume.lock`, which only
    # `_acquire_resume_lock` ever creates.
    lock_path = project / ".hydra" / wf / "resume.lock"
    assert not lock_path.exists(), (
        "precondition: no resume.lock yet (nothing to confuse the snapshot "
        "with)"
    )

    # `resume.lock` is unlinked in `_release_resume_lock`'s `finally`, so its
    # absence AFTER the call alone cannot distinguish "never created" from
    # "created then removed" -- assert `_acquire_resume_lock` itself is
    # never even called, which is the real claim (refused BEFORE the lock).
    def _boom_acquire(*_a, **_k):
        raise AssertionError(
            "_acquire_resume_lock must never be called for a gate-only "
            "recover-stalled-stage refusal"
        )
    monkeypatch.setattr(cli, "_acquire_resume_lock", _boom_acquire)

    rc = cli.main([
        "--project", str(project), "resume", wf,
        "--action", "recover-stalled-stage", "--option", "does-not-exist",
        "--gate-only",
    ])
    _cap = capsys.readouterr()
    out = json.loads(_cap.err or _cap.out or "{}")
    after = _snapshot()

    assert rc == 1
    assert out.get("ok") is False
    assert out.get("error") == "recovery_is_live_operation"
    assert not lock_path.exists(), (
        "the refusal must run before _acquire_resume_lock ever creates "
        "resume.lock"
    )
    assert after == before, (
        f"the gate-only recover-stalled-stage refusal wrote files: "
        f"added={after - before} removed={before - after}"
    )
