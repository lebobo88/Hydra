"""Hydra Control — sanctioned workflow-resume MCP server.

Campaign mesh-console-unification C2 (2026-06-05).
C5 eights-audit (2026-06-07): added hydra.cockpit.audit tool.

Tools:
  - hydra.control.ping       — no-arg liveness probe (AgentMesh healthProbe)
  - hydra.workflow.resume    — resolve a pending HITL gate by launching a
                               DETACHED `hydra resume` CLI subprocess
  - hydra.cockpit.audit      — file a 'cockpit_write' eights audit envelope
                               for every cockpit write action (spool-safe)

Why detached: a LangGraph continuation is long-running (squad dispatch,
judging, synthesis) and cannot complete synchronously inside an MCP tool
call without blocking stdio and blowing the caller's per-call timeout. The
tool therefore validates, launches, and returns immediately with
{ok, launched: true, pid, log}; progress is observable via
hydra-mem.workflow_status and the workflow's trace.jsonl. The CLI itself is
idempotent — resuming a workflow whose gate is already cleared is a no-op —
so a retried launch never double-applies.

This server is intentionally SEPARATE from hydra_memory: hydra_memory is a
read-only surface that AgentMesh's read/stitch federation clients may call;
resume is a write and must only ever be reachable through meshd's sanctioned
write path (mesh.hitl.resolve). Keeping it on its own backend key keeps the
read/write split structural, not conventional.

If the `mcp` python package is not installed, degrades to the same
plain JSON-RPC-over-stdio loop as hydra_memory.
"""
from __future__ import annotations

import json
import logging
import os
import re
import subprocess
import sys
import time
import traceback
import uuid
from pathlib import Path
from typing import Any, Optional

logger = logging.getLogger("hydra_control")

# Add project root to sys.path so `hydra_core` resolves when launched as a
# child process from backends.json.
_HERE = Path(__file__).resolve()
sys.path.insert(0, str(_HERE.parents[2]))

_HYDRA_ROOT = Path(os.environ.get("HYDRA_ROOT") or _HERE.parents[2])

_RESUME_ACTIONS = ("approve", "reject", "modify-budget", "force-dispatch", "change-squads")

# workflow_id is used as a subprocess argument — restrict to UUID-ish tokens
# so a malicious payload can never smuggle flags or shell metacharacters.
_WORKFLOW_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9\-_]{0,63}$")
_OPTION_RE = re.compile(r"^[A-Za-z0-9 ,._\-]{0,200}$")

# C5: audit — cockpit write actions that may appear in hydra.cockpit.audit calls.
# This is informational/validation; we do not restrict the action field to this set
# so the tool can record any action the cockpit emits.
_COCKPIT_WRITE_ACTIONS = frozenset({
    "launch", "approve", "reject", "modify-budget",
    "force-dispatch", "change-squads", "replay", "tag_memory",
})

# ---------------------------------------------------------------------------
# MU10 — typed envelope extra-field allow-list (Phase 3a sequel).
#
# envelope_record builds hydra_env from only the 7 canonical outer fields
# plus the nested payload blob.  Type-specific REQUIRED fields
# (e.g. Handoff.payload_envelope_id, DecisionRecord.decision/rationale)
# could never reach the dict, so validate_envelope always failed on them.
#
# This allow-list enumerates the non-base fields for each envelope type so
# envelope_record can promote them from top-level args OR from the nested
# payload dict into hydra_env before validation — without reintroducing the
# Phase 3a payload-key shadowing vector (the reserved outer keys are locked).
# ---------------------------------------------------------------------------

# Fields that belong exclusively to the outer envelope envelope and must
# NEVER be overwritten by type-specific field promotion.
_RESERVED_ENVELOPE_KEYS: frozenset[str] = frozenset({
    "id", "type", "origin_squad", "target_squad", "workflow_id",
    "created_at", "parent_id",
})

# Per-type allow-list: required + safe optional non-base fields derived from
# hydra_core/schemas.py.  Derived from each class's field definitions; does
# NOT include any key from _RESERVED_ENVELOPE_KEYS (enforced below).
_ENVELOPE_EXTRA_FIELDS: dict[str, frozenset[str]] = {
    "C_SUITE_DECISION_PACKET": frozenset({
        "origin", "objective", "proposed_tasks", "approvals_required",
        "dissenting_opinions", "notes", "target_repo_id", "target_repo_subpath",
        "model_tier", "pp_team", "pp_profile",
    }),
    "PRD": frozenset({
        "source_goal_id", "summary", "user_personas", "user_stories",
        "acceptance_criteria", "dependencies", "non_functional_requirements",
    }),
    "ARCH_RFC": frozenset({
        "related_prd", "proposed_changes", "risk_assessment",
        "rollout_plan", "requires_approvals",
    }),
    "DEV_TASK": frozenset({
        "owner", "repo", "branch", "instructions", "files_touched",
        "test_plan", "status", "pr_url", "target_repo_id", "target_repo_subpath",
        "pp_team", "pp_profile",
    }),
    "CREATIVE_BRIEF": frozenset({
        "campaign_id", "objective", "target_audience", "key_messages",
        "channels", "brand_constraints", "assets_required",
    }),
    "SHOT_LIST": frozenset({
        "brief_id", "shots",
    }),
    "ASSET_JOB": frozenset({
        "shotlist_id", "model_type", "resolution", "fps", "style_refs",
        "output_bucket", "max_render_cost_usd", "provenance_required",
    }),
    "HITL_REQUEST": frozenset({
        "reason", "summary", "options", "default_option", "expires_at",
    }),
    "DECISION_RECORD": frozenset({
        "decision", "rationale", "dissenting_opinions", "artifacts", "sealed",
    }),
    "HANDOFF": frozenset({
        "granted_tools", "granted_memory_scopes", "payload_envelope_id", "expires_at",
    }),
    "SUPPORT_TICKET": frozenset({
        "ticket_id", "customer_ref", "subject", "body", "priority",
        "intent", "channel", "portable_context",
    }),
    "PORTABLE_CONTEXT": frozenset({
        # payload: PortableContextPayload — base logic already sets this from
        # args["payload"]; included here so the field is documented and the
        # promotion loop is a no-op rather than absent.
        "payload",
    }),
    "VOC_REPORT": frozenset({
        "period", "coverage", "themes", "escalation_patterns",
        "delight_signals", "recommendations",
    }),
}

# Sanity-check at module import: no allow-listed key may shadow a reserved
# outer field.  Caught immediately rather than at call time.
_bad_overlaps = {
    t: fields & _RESERVED_ENVELOPE_KEYS
    for t, fields in _ENVELOPE_EXTRA_FIELDS.items()
    if fields & _RESERVED_ENVELOPE_KEYS
}
assert not _bad_overlaps, (
    f"_ENVELOPE_EXTRA_FIELDS entries overlap _RESERVED_ENVELOPE_KEYS: {_bad_overlaps}"
)
del _bad_overlaps

# ---------------------------------------------------------------------------
# C5: EightsAttestor integration — spool-safe audit filing
# ---------------------------------------------------------------------------

def _get_attestor() -> Optional[Any]:
    """Return an EightsAttestor instance if hydra_core is importable, else None.

    The attestor is constructed WITHOUT a live dispatcher — it will spool any
    failed calls to ~/.hydra/eights-pending for replay on next workflow start.
    The cockpit bridge calls this tool via the stdio path it already holds, so
    the attestor only needs the spool path for offline resilience.
    """
    try:
        from hydra_core.eights.attestation import EightsAttestor  # noqa: PLC0415
        # No dispatcher: calls go best-effort; offline → spool automatically.
        return EightsAttestor(dispatcher=None, enabled=True)
    except Exception:  # noqa: BLE001 — hydra_core not on path in some test envs
        return None


def _file_cockpit_audit_envelope(
    *,
    action: str,
    actor: str,
    project: str,
    trace_id: str,
    workflow_id: Optional[str] = None,
    option: Optional[str] = None,
    detail: Optional[str] = None,
) -> dict[str, Any]:
    """Build and file a cockpit_write envelope to TheEights via EightsAttestor.

    Returns {ok, spooled?}: ok=True always (audit must NOT block the action).
    If the attestor spools (eights offline), spooled=True is returned so the
    caller can surface the degraded state to the operator without blocking.
    """
    envelope: dict[str, Any] = {
        "id": str(uuid.uuid4()),
        "type": "COCKPIT_WRITE",
        "workflow_id": workflow_id or "",
        "origin_squad": actor,          # actor='hydra-cockpit' from bridge
        "target_squad": None,
        "parent_id": None,
        # Additional cockpit-specific fields carried in the envelope payload:
        "action": action,
        "project": project,
        "trace_id": trace_id,
    }
    if option is not None:
        envelope["option"] = option
    if detail is not None:
        envelope["detail"] = detail

    attestor = _get_attestor()
    if attestor is None:
        # hydra_core not available — spool manually is not possible; log and proceed.
        logger.warning("hydra.cockpit.audit: EightsAttestor unavailable; audit not filed")
        return {"ok": True, "spooled": True, "reason": "attestor_unavailable"}

    spool_count_before = attestor.pending_count()
    attestor.envelope_record(envelope)
    spool_count_after = attestor.pending_count()

    spooled = spool_count_after > spool_count_before
    return {"ok": True, "spooled": spooled}


# ---------------------------------------------------------------------------
# Detached launch gate helpers
# ---------------------------------------------------------------------------

def _detached_allowed() -> bool:
    """Return True iff HYDRA_ALLOW_DETACHED=1 in the current environment.

    Read at call time (not import time) so a parent process or test monkeypatch
    can set the variable after the module is imported and have it take effect
    immediately.
    """
    return os.environ.get("HYDRA_ALLOW_DETACHED") == "1"


_FLEET_GOAL_RE: re.Pattern[str] = re.compile(
    # Token boundaries:
    #   (?:^|\s)    — flag must follow start-of-string or whitespace (not
    #                 embedded mid-word)
    #   (?=\s|$)    — id list must end at whitespace or end-of-string so a
    #                 trailing comma like `--repos a,b,` does NOT match
    # Two-or-more ids: [\w.-]+(,[\w.-]+)+  requires at least one comma-id pair.
    r"(?:^|\s)--(repos|fleet)[ =]\s*[\w.-]+(,[\w.-]+)+(?=\s|$)"
)


def _is_fleet_goal(goal: str) -> bool:
    """Return True if *goal* contains a multi-repo fleet token.

    A fleet token is ``--repos`` or ``--fleet`` followed by two or more
    comma-separated repo ids (e.g. ``--repos agentsmith,theeights``).  Fleet
    runs are detached by design — the cross-repo campaign spawns one worker per
    repo and collects results via ``as_completed``.  The attended cursor is
    single-stream and cannot host a fleet; fleet goals are therefore exempt
    from the ``HYDRA_ALLOW_DETACHED`` gate so that campaign paths continue to
    work from sessions that have not set the env var.
    """
    return bool(_FLEET_GOAL_RE.search(goal))


def _detached_refusal(kind: str) -> dict[str, Any]:
    """Return a standard refusal envelope for a blocked detached launch."""
    return {
        "ok": False,
        "error": "detached_disabled",
        "detail": f"detached {kind} is automation-only",
        "remediation": (
            "set HYDRA_ALLOW_DETACHED=1 or use "
            "plan/step/submit_host_result (attended)"
        ),
    }


def _launch_resume(workflow_id: str, action: str, option: str | None) -> dict[str, Any]:
    # Detached gate: resume is automation-only. No fleet exemption — a resume
    # call carries no fleet goal string, so fleet detection is not applicable.
    if not _detached_allowed():
        return _detached_refusal("resume")

    log_dir = _HYDRA_ROOT / ".hydra" / workflow_id
    log_dir.mkdir(parents=True, exist_ok=True)
    log_path = log_dir / "resume.log"

    cmd = [
        sys.executable, "-m", "hydra_core.cli",
        "resume", workflow_id,
        "--action", action,
        "--live",
    ]
    if option:
        cmd.extend(["--option", option])

    env = dict(os.environ)
    env.setdefault("PYTHONPATH", str(_HYDRA_ROOT))

    creationflags = 0
    start_new_session = False
    if os.name == "nt":
        creationflags = (
            getattr(subprocess, "DETACHED_PROCESS", 0)
            | getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
        )
    else:  # pragma: no cover — Windows-first deployment
        start_new_session = True

    with open(log_path, "ab") as log_f:
        log_f.write(
            f"\n--- resume launch {time.strftime('%Y-%m-%dT%H:%M:%S')} "
            f"action={action} option={option!r} ---\n".encode()
        )
        proc = subprocess.Popen(  # noqa: S603 — fixed argv, validated tokens
            cmd,
            cwd=str(_HYDRA_ROOT),
            env=env,
            stdin=subprocess.DEVNULL,
            stdout=log_f,
            stderr=log_f,
            creationflags=creationflags,
            start_new_session=start_new_session,
        )
    return {
        "ok": True,
        "launched": True,
        "pid": proc.pid,
        "workflow_id": workflow_id,
        "action": action,
        "log": str(log_path),
    }


def _launch_ingest(workflow_id: str, envelopes: list[dict[str, Any]]) -> dict[str, Any]:
    """Persist the host-completed skill envelopes to the workflow dir and launch
    a DETACHED `hydra ingest` so the deterministic engine dispatches engineering.

    Detached for the same reason as resume: the pp stage loop (start_run ->
    generate -> judge -> finalize) is long-running and cannot complete inside an
    MCP tool call without blowing the caller's per-call timeout. The CLI is
    idempotent (claim-before-dispatch ledger), so a retried submit never
    double-dispatches.
    """
    # UNGATED: ingest is the attended skill-squad continuation transport.
    # claude-skill squads (rlm-gaming, garland) run host-side and deliver
    # their envelopes back to the deterministic engine via
    # submit_envelopes → _launch_ingest.  Gating this would break every
    # attended claude-skill workflow regardless of HYDRA_ALLOW_DETACHED.
    wf_dir = _HYDRA_ROOT / ".hydra" / workflow_id
    wf_dir.mkdir(parents=True, exist_ok=True)
    # Unique per-submit filename so two concurrent submits to the SAME workflow
    # never overwrite each other's payload before the detached child reads it
    # (codex review item 5). The CLI's resume lock still serializes the actual
    # dispatch; this just keeps each child's input intact.
    env_path = wf_dir / f"ingest_envelopes_{uuid.uuid4().hex}.json"
    env_path.write_text(json.dumps({"envelopes": envelopes}, indent=2), encoding="utf-8")
    log_path = wf_dir / "ingest.log"

    cmd = [
        sys.executable, "-m", "hydra_core.cli",
        "ingest", workflow_id,
        "--envelopes", str(env_path),
        "--live",
    ]

    env = dict(os.environ)
    env.setdefault("PYTHONPATH", str(_HYDRA_ROOT))

    creationflags = 0
    start_new_session = False
    if os.name == "nt":
        creationflags = (
            getattr(subprocess, "DETACHED_PROCESS", 0)
            | getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
        )
    else:  # pragma: no cover — Windows-first deployment
        start_new_session = True

    with open(log_path, "ab") as log_f:
        log_f.write(
            f"\n--- ingest launch {time.strftime('%Y-%m-%dT%H:%M:%S')} "
            f"envelopes={len(envelopes)} ---\n".encode()
        )
        proc = subprocess.Popen(  # noqa: S603 — fixed argv, validated token
            cmd,
            cwd=str(_HYDRA_ROOT),
            env=env,
            stdin=subprocess.DEVNULL,
            stdout=log_f,
            stderr=log_f,
            creationflags=creationflags,
            start_new_session=start_new_session,
        )
    return {
        "ok": True,
        "launched": True,
        "pid": proc.pid,
        "workflow_id": workflow_id,
        "envelope_count": len(envelopes),
        "log": str(log_path),
    }


def _launch_run(goal: str, *, squad: str | None, budget: float | None,
                workflow_id: str | None, risk: str | None = None) -> dict[str, Any]:
    """Launch a NEW workflow via a DETACHED `hydra run --live`.

    The host-facing deterministic launch surface (generalises web/server's
    launcher). The supervisor LLM calls this instead of hand-orchestrating /
    hand-writing code: engineering then dispatches through the pp stage loop in
    Python. Pre-allocates the workflow_id so the caller can attach immediately.
    """
    # Detached gate: fleet goals are exempt because the cross-repo campaign is
    # detached by design and sets HYDRA_ALLOW_DETACHED internally before
    # dispatching individual repo workers.
    if not _detached_allowed() and not _is_fleet_goal(goal):
        return _detached_refusal("launch")

    wf = workflow_id or str(uuid.uuid4())
    log_dir = _HYDRA_ROOT / ".hydra" / wf
    log_dir.mkdir(parents=True, exist_ok=True)
    log_path = log_dir / "run.log"

    cmd = [sys.executable, "-m", "hydra_core.cli", "run", goal,
           "--live", "--workflow-id", wf]
    if squad:
        cmd.extend(["--squad", squad])
    if budget is not None:
        cmd.extend(["--budget", str(budget)])
    if risk is not None:
        cmd.extend(["--risk", risk])

    env = dict(os.environ)
    env.setdefault("PYTHONPATH", str(_HYDRA_ROOT))

    creationflags = 0
    start_new_session = False
    if os.name == "nt":
        creationflags = (
            getattr(subprocess, "DETACHED_PROCESS", 0)
            | getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
        )
    else:  # pragma: no cover — Windows-first deployment
        start_new_session = True

    with open(log_path, "ab") as log_f:
        log_f.write(
            f"\n--- run launch {time.strftime('%Y-%m-%dT%H:%M:%S')} "
            f"wf={wf} squad={squad!r} budget={budget!r} ---\n".encode()
        )
        proc = subprocess.Popen(  # noqa: S603 — fixed argv, validated tokens
            cmd, cwd=str(_HYDRA_ROOT), env=env,
            stdin=subprocess.DEVNULL, stdout=log_f, stderr=log_f,
            creationflags=creationflags, start_new_session=start_new_session,
        )
    return {"ok": True, "launched": True, "pid": proc.pid,
            "workflow_id": wf, "log": str(log_path)}


# Planning halts after intake+planner; it never dispatches, so it is cheap and
# bounded. Generous caps cover the planner's optional MCP enrichment calls and,
# for attended step/submit, the pp start_run / judge-smoke round-trip.
# G6 durability: step raised 300→900 s (one engineer generation + smoke can
# take several minutes); submit raised to 1800 s (full stage loop with judge).
# All three defaults can be overridden at runtime via their env knobs.
_PLAN_TIMEOUT_S = int(os.environ.get("HYDRA_PLAN_TIMEOUT_S", "180"))
_STEP_TIMEOUT_S = int(os.environ.get("HYDRA_STEP_TIMEOUT_S", "900"))
_SUBMIT_TIMEOUT_S = int(os.environ.get("HYDRA_SUBMIT_TIMEOUT_S", "1800"))


def _run_cli_json(cli_args: list[str], *, timeout_s: int,
                  err_label: str, workflow_id: str | None = None) -> dict[str, Any]:
    """Run `python -m hydra_core.cli <cli_args>` SYNCHRONOUSLY and return its
    JSON stdout IN-BAND. The non-detaching transport attended mode uses for
    plan / step / submit-host-result (each prints exactly one JSON object)."""
    cmd = [sys.executable, "-m", "hydra_core.cli", *cli_args]
    env = dict(os.environ)
    env.setdefault("PYTHONPATH", str(_HYDRA_ROOT))
    try:
        proc = subprocess.run(  # noqa: S603 — fixed argv, validated tokens
            cmd, cwd=str(_HYDRA_ROOT), env=env,
            # MU8: the child MUST NOT inherit this server's stdin. The server's
            # stdin is the gateway's synchronous MCP pipe with a read always
            # pending; Windows serializes operations on the shared file object,
            # so an inheriting child freezes inside Py_InitializeFromConfig
            # (lseek on fd 0 → NtQueryInformationFile blocks forever). Mirrors
            # _launch_run, which already passes DEVNULL for the same reason.
            stdin=subprocess.DEVNULL,
            capture_output=True, text=True, timeout=timeout_s,
        )
    except subprocess.TimeoutExpired as exc:
        # MU8b: surface whatever partial output was buffered so callers can
        # diagnose a slow/hung CLI without losing all context.
        def _dec(v: bytes | str | None) -> str:
            if v is None:
                return ""
            return v.decode("utf-8", errors="replace") if isinstance(v, bytes) else v
        # G6: map each label to its env knob so operators know exactly what to
        # raise.  For 'submit', re-issuing is safe because submit-host-result is
        # idempotent on call_key.  For 'step', a killed step can leave stale
        # state that must be cleaned up manually.
        _knob_map: dict[str, str] = {
            "plan": "HYDRA_PLAN_TIMEOUT_S",
            "step": "HYDRA_STEP_TIMEOUT_S",
            "submit": "HYDRA_SUBMIT_TIMEOUT_S",
        }
        _knob = _knob_map.get(err_label,
                               f"HYDRA_{err_label.upper()}_TIMEOUT_S")
        _remediation = f"Increase timeout via {_knob}={timeout_s * 2}"
        if err_label == "submit":
            _remediation += (
                "; re-issuing the same submit-host-result is idempotent on "
                "call_key (safe to retry)"
            )
        _tout: dict[str, Any] = {
            "ok": False,
            "error": f"{err_label}_timeout",
            "detail": f"exceeded {timeout_s}s",
            "workflow_id": workflow_id,
            "partial_stdout": _dec(exc.stdout)[-1000:],
            "partial_stderr": _dec(exc.stderr)[-1000:],
            "remediation": _remediation,
        }
        if err_label == "step":
            # A killed step can leave these artefacts requiring manual cleanup.
            _tout["stale_state"] = [
                "resume.lock (the workflow's resume lock file)",
                "orphan pp run — finalize-abort it via the pp harness",
                (
                    "orphan .harness/worktrees/attended-* worktree "
                    "(git worktree remove <path>)"
                ),
            ]
        return _tout
    if proc.returncode != 0:
        return {"ok": False, "error": f"{err_label}_failed", "workflow_id": workflow_id,
                "detail": (proc.stderr or "")[-2000:]}
    out = (proc.stdout or "").strip()
    try:
        return json.loads(out)
    except json.JSONDecodeError:
        start = out.find("{")
        if start >= 0:
            try:
                data, _ = json.JSONDecoder().raw_decode(out[start:])
                return data
            except json.JSONDecodeError:
                pass
        return {"ok": False, "error": f"{err_label}_unparseable",
                "workflow_id": workflow_id, "raw": out[-2000:]}


def _run_plan(goal: str, *, squad: str | None, budget: float | None,
              workflow_id: str | None, risk: str | None = None) -> dict[str, Any]:
    """Run `hydra plan` SYNCHRONOUSLY and return the planner state IN-BAND.

    The non-detaching counterpart to `_launch_run`: attended (host-bridged)
    execution needs the routing + TaskState plan returned to the host so the
    host can drive dispatch itself (visible Agent subagents), rather than a
    detached headless subprocess. The CLI halts after planner (plan_only adds
    "dispatch" to interrupt_before), so this returns quickly without executing
    any squad. The pre-allocated workflow_id threads continuity into a later
    `hydra.workflow.resume`.
    """
    wf = workflow_id or str(uuid.uuid4())
    cli_args = ["plan", goal, "--workflow-id", wf]
    if squad:
        cli_args.extend(["--squad", squad])
    if budget is not None:
        cli_args.extend(["--budget", str(budget)])
    if risk is not None:
        cli_args.extend(["--risk", risk])
    return _run_cli_json(cli_args, timeout_s=_PLAN_TIMEOUT_S,
                         err_label="plan", workflow_id=wf)


def _run_step(workflow_id: str) -> dict[str, Any]:
    """Open the next attended engineering stage and return its host_action."""
    return _run_cli_json(["step", workflow_id], timeout_s=_STEP_TIMEOUT_S,
                         err_label="step", workflow_id=workflow_id)


def _run_submit_host_result(workflow_id: str, run_id: str, call_key: str,
                            result: dict[str, Any]) -> dict[str, Any]:
    """Feed a host subagent result into an attended stage (advances one step).

    The CLI takes the result as a JSON file; write it to a temp file scoped to
    the workflow's .hydra dir so the argv stays fixed.
    """
    res_dir = _HYDRA_ROOT / ".hydra" / workflow_id / "attended"
    res_dir.mkdir(parents=True, exist_ok=True)
    safe_key = "".join(c for c in call_key if c.isalnum() or c in "-_") or "result"
    res_file = res_dir / f"hostresult-{safe_key}.json"
    res_file.write_text(json.dumps(result), encoding="utf-8")
    return _run_cli_json(
        ["submit-host-result", workflow_id, "--run-id", run_id,
         "--call-key", call_key, "--result", str(res_file)],
        timeout_s=_SUBMIT_TIMEOUT_S, err_label="submit", workflow_id=workflow_id)


def _tool_handlers() -> dict[str, Any]:
    def ping(args: dict[str, Any]) -> dict[str, Any]:
        return {
            "ok": True,
            "server": "hydra_control",
            "hydra_root": str(_HYDRA_ROOT),
            "ts": time.time(),
        }

    def workflow_resume(args: dict[str, Any]) -> dict[str, Any]:
        workflow_id = str(args.get("workflow_id") or "")
        action = str(args.get("action") or "")
        option = args.get("option")
        option = str(option) if option not in (None, "") else None

        if not _WORKFLOW_ID_RE.match(workflow_id):
            return {"ok": False, "error": "invalid_workflow_id"}
        if action not in _RESUME_ACTIONS:
            return {"ok": False, "error": "invalid_action",
                    "valid": list(_RESUME_ACTIONS)}
        if option is not None and not _OPTION_RE.match(option):
            return {"ok": False, "error": "invalid_option"}

        try:
            return _launch_resume(workflow_id, action, option)
        except Exception as e:  # noqa: BLE001 — surfaced, never silent
            logger.exception("resume launch failed")
            return {"ok": False, "launched": False,
                    "error": f"launch_failed: {e}"}

    def cockpit_audit(args: dict[str, Any]) -> dict[str, Any]:
        """C5: File a cockpit_write audit envelope to TheEights.

        SPOOL-SAFE: if TheEights is offline, the attestor spools the payload
        locally and this call returns {ok:true, spooled:true}. The audit must
        NEVER block the operator action — it surfaces degradation, never fails.

        Required fields: action, actor, project, trace_id
        Optional: workflow_id, option, detail

        workflow_id is validated with _WORKFLOW_ID_RE when present.
        """
        action = str(args.get("action") or "")
        actor = str(args.get("actor") or "")
        project = str(args.get("project") or "")
        trace_id = str(args.get("trace_id") or "")
        workflow_id: Optional[str] = args.get("workflow_id")
        option_raw = args.get("option")
        detail_raw = args.get("detail")

        # Validate required fields
        if not action:
            return {"ok": False, "error": "action is required"}
        if not actor:
            return {"ok": False, "error": "actor is required"}
        if not project:
            return {"ok": False, "error": "project is required"}
        if not trace_id:
            return {"ok": False, "error": "trace_id is required"}

        # Validate workflow_id when present
        if workflow_id is not None:
            workflow_id = str(workflow_id)
            if workflow_id and not _WORKFLOW_ID_RE.match(workflow_id):
                return {"ok": False, "error": "invalid_workflow_id"}

        # Normalize optional fields
        option = str(option_raw) if option_raw not in (None, "") else None
        detail = str(detail_raw) if detail_raw not in (None, "") else None

        try:
            result = _file_cockpit_audit_envelope(
                action=action,
                actor=actor,
                project=project,
                trace_id=trace_id,
                workflow_id=workflow_id if workflow_id else None,
                option=option,
                detail=detail,
            )
            return result
        except Exception as e:  # noqa: BLE001 — audit must never crash caller
            logger.exception("cockpit_audit: unexpected error during envelope filing")
            # Return ok=True with spooled=True — audit degraded, action proceeds
            return {"ok": True, "spooled": True, "reason": f"exception:{type(e).__name__}"}

    def workflow_submit_envelopes(args: dict[str, Any]) -> dict[str, Any]:
        """Inject host-completed skill envelopes back into a running workflow.

        The seam between a host-run claude-skill squad (rlm-gaming/garland, which
        cannot run headlessly) and the deterministic engineering engine. Launches
        a detached `hydra ingest` that forwards DEV_TASK/PRD/ARCH_RFC to the
        engineering squad and runs the pp stage loop. Idempotent at the CLI layer.
        """
        workflow_id = str(args.get("workflow_id") or "")
        envelopes = args.get("envelopes")
        if not _WORKFLOW_ID_RE.match(workflow_id):
            return {"ok": False, "error": "invalid_workflow_id"}
        if not isinstance(envelopes, list) or not envelopes:
            return {"ok": False, "error": "envelopes must be a non-empty list"}
        if not all(isinstance(e, dict) for e in envelopes):
            return {"ok": False, "error": "each envelope must be an object"}
        if len(envelopes) > 100:
            return {"ok": False, "error": "too many envelopes (max 100)"}
        try:
            return _launch_ingest(workflow_id, envelopes)
        except Exception as e:  # noqa: BLE001 — surfaced, never silent
            logger.exception("ingest launch failed")
            return {"ok": False, "launched": False, "error": f"launch_failed: {e}"}

    _RISK_VALUES = frozenset({"low", "medium", "high"})

    def workflow_launch(args: dict[str, Any]) -> dict[str, Any]:
        """Launch a NEW workflow deterministically (detached `hydra run --live`).

        The sanctioned engineering launch surface for the hybrid supervisor: the
        host LLM hands the goal here instead of hand-writing code, so engineering
        runs through the pp stage loop in Python.
        """
        goal = str(args.get("goal") or "").strip()
        if not goal:
            return {"ok": False, "error": "goal is required"}
        if len(goal) > 8000:
            return {"ok": False, "error": "goal too long (max 8000 chars)"}
        squad = args.get("squad")
        squad = str(squad) if squad not in (None, "") else None
        if squad is not None and not re.match(r"^[A-Za-z0-9_\-,]{1,200}$", squad):
            return {"ok": False, "error": "invalid_squad"}
        budget = args.get("budget")
        try:
            budget = float(budget) if budget not in (None, "") else None
        except (TypeError, ValueError):
            return {"ok": False, "error": "budget must be numeric"}
        workflow_id = args.get("workflow_id")
        workflow_id = str(workflow_id) if workflow_id not in (None, "") else None
        if workflow_id is not None and not _WORKFLOW_ID_RE.match(workflow_id):
            return {"ok": False, "error": "invalid_workflow_id"}
        # F5: risk param — enum low|medium|high (optional).
        risk = args.get("risk")
        risk = str(risk) if risk not in (None, "") else None
        if risk is not None and risk not in _RISK_VALUES:
            return {"ok": False, "error": f"invalid_risk (must be low|medium|high, got {risk!r})"}
        try:
            return _launch_run(goal, squad=squad, budget=budget, workflow_id=workflow_id, risk=risk)
        except Exception as e:  # noqa: BLE001 — surfaced, never silent
            logger.exception("run launch failed")
            return {"ok": False, "launched": False, "error": f"launch_failed: {e}"}

    def workflow_plan(args: dict[str, Any]) -> dict[str, Any]:
        """Plan a goal WITHOUT dispatching, returning the plan in-band.

        The non-detaching planning surface for attended (host-bridged)
        execution: routes + decomposes the goal and returns the planner's
        TaskState plan (and any pending approval HITL) so the host can drive
        dispatch itself with visible Agent subagents. Unlike
        `hydra.workflow.launch` this is synchronous and executes no squad.
        """
        goal = str(args.get("goal") or "").strip()
        if not goal:
            return {"ok": False, "error": "goal is required"}
        if len(goal) > 8000:
            return {"ok": False, "error": "goal too long (max 8000 chars)"}
        squad = args.get("squad")
        squad = str(squad) if squad not in (None, "") else None
        if squad is not None and not re.match(r"^[A-Za-z0-9_\-,]{1,200}$", squad):
            return {"ok": False, "error": "invalid_squad"}
        budget = args.get("budget")
        try:
            budget = float(budget) if budget not in (None, "") else None
        except (TypeError, ValueError):
            return {"ok": False, "error": "budget must be numeric"}
        workflow_id = args.get("workflow_id")
        workflow_id = str(workflow_id) if workflow_id not in (None, "") else None
        if workflow_id is not None and not _WORKFLOW_ID_RE.match(workflow_id):
            return {"ok": False, "error": "invalid_workflow_id"}
        # F5: risk param — enum low|medium|high (optional).
        risk = args.get("risk")
        risk = str(risk) if risk not in (None, "") else None
        if risk is not None and risk not in _RISK_VALUES:
            return {"ok": False, "error": f"invalid_risk (must be low|medium|high, got {risk!r})"}
        try:
            return _run_plan(goal, squad=squad, budget=budget, workflow_id=workflow_id, risk=risk)
        except Exception as e:  # noqa: BLE001 — surfaced, never silent
            logger.exception("plan failed")
            return {"ok": False, "error": f"plan_failed: {e}"}

    def workflow_step(args: dict[str, Any]) -> dict[str, Any]:
        """Open the next attended engineering stage; return its host_action.

        Attended (host-bridged) execution: after `hydra.workflow.plan`, call this
        to scaffold a pp run and pause for a VISIBLE host `engineer` subagent.
        Returns {status:"awaiting_host", host_action:{agent_type, prompt, cwd,
        call_key}, run_id, ...}. The host then spawns the Agent and feeds the
        result back via `hydra.workflow.submit_host_result`.
        """
        workflow_id = str(args.get("workflow_id") or "")
        if not _WORKFLOW_ID_RE.match(workflow_id):
            return {"ok": False, "error": "invalid_workflow_id"}
        try:
            return _run_step(workflow_id)
        except Exception as e:  # noqa: BLE001 — surfaced, never silent
            logger.exception("attended step failed")
            return {"ok": False, "error": f"step_failed: {e}"}

    def workflow_submit_host_result(args: dict[str, Any]) -> dict[str, Any]:
        """Feed a host subagent's result into an attended stage (advance one step).

        On stage completion the engine charges the accrued cost on the
        checkpointed HydraState budget (tripwires stay live) and records the task
        outcome — attended execution is never budget-blind.
        """
        workflow_id = str(args.get("workflow_id") or "")
        run_id = str(args.get("run_id") or "")
        call_key = str(args.get("call_key") or "")
        result = args.get("result")
        if not _WORKFLOW_ID_RE.match(workflow_id):
            return {"ok": False, "error": "invalid_workflow_id"}
        if not run_id or not re.match(r"^[A-Za-z0-9_\-]{1,128}$", run_id):
            return {"ok": False, "error": "invalid_run_id"}
        if not call_key or not re.match(r"^[A-Za-z0-9_\-]{1,128}$", call_key):
            return {"ok": False, "error": "invalid_call_key"}
        if not isinstance(result, dict):
            return {"ok": False, "error": "result must be an object"}
        try:
            return _run_submit_host_result(workflow_id, run_id, call_key, result)
        except Exception as e:  # noqa: BLE001 — surfaced, never silent
            logger.exception("attended submit failed")
            return {"ok": False, "error": f"submit_failed: {e}"}

    # ------------------------------------------------------------------
    # F32-H: four new governance-federation tools called by AgentSmith's
    # HydraBridge (hydra-bridge.ts:74,108,129,150). Argument shapes match
    # exactly what the bridge sends — do NOT edit AgentSmith.
    # ------------------------------------------------------------------

    def venom_cross_check(args: dict[str, Any]) -> dict[str, Any]:
        """Run require_cerberus_pass against the live venom registry.

        Called by AgentSmith HydraBridge.venomCrossCheck(capability, args).
        Never raises — transport-shaped errors return ok=false with rationale.
        """
        capability = str(args.get("capability") or "")
        context = args.get("args") or args.get("context")
        if not capability:
            return {"ok": False, "rationale": "capability is required"}
        try:
            from hydra_core.venom import require_cerberus_pass, VenomUnregistered
        except Exception as imp_exc:  # noqa: BLE001
            return {"ok": False, "rationale": f"venom module unavailable: {imp_exc}"}
        try:
            verdict = require_cerberus_pass(
                capability, context,
                raise_on_refuse=False,   # never raises — we translate to ok/rationale
            )
            if verdict.allowed:
                return {"ok": True, "rationale": "cerberus pass"}
            return {
                "ok": False,
                "rationale": "; ".join(verdict.refusal_reasons) or "cerberus refused",
            }
        except VenomUnregistered:
            # Capability not in registry → not a known venom; treat as pass.
            return {"ok": True, "rationale": "capability not in venom registry — not a venom-class action"}
        except Exception as exc:  # noqa: BLE001 — transport-shaped error → ok=false
            logger.exception("venom_cross_check: unexpected error")
            return {"ok": False, "rationale": f"gate error: {type(exc).__name__}: {exc}"}

    def squad_list(args: dict[str, Any]) -> dict[str, Any]:  # noqa: ARG001
        """Squad discovery. Called by AgentSmith HydraBridge.squadRegistry().

        Returns {squads: [{slug, name, entrypoint, active, version}]}.
        """
        try:
            from hydra_core.squad_loader import discover_squads
            packs = discover_squads(_HYDRA_ROOT)
            squads = []
            for slug, pack in sorted(packs.items()):
                squads.append({
                    "slug": slug,
                    "name": pack.name,
                    "entrypoint": pack.entrypoint,
                    "active": pack.entrypoint != "stub",
                    "version": str(pack.version),
                })
            return {"ok": True, "squads": squads}
        except Exception as exc:  # noqa: BLE001
            logger.exception("squad_list: discovery failed")
            return {"ok": False, "error": f"squad_list failed: {exc}", "squads": []}

    def envelope_record(args: dict[str, Any]) -> dict[str, Any]:
        """Record an envelope to episodic db / trace.

        Called by AgentSmith HydraBridge.envelopeRecord(envelope).
        Args match the bridge's shape: {kind, from_squad, to_squad?, workflow_id, payload}.
        Also accepts Hydra-native envelope shape: {type, origin_squad, ...}.
        Validates via hydra_core.schemas.validate_envelope (best-effort);
        appends to episodic db using the existing attestor helper.
        """
        # Normalise bridge → Hydra envelope shape.
        kind = str(args.get("kind") or args.get("type") or "")
        from_squad = str(args.get("from_squad") or args.get("origin_squad") or "")
        to_squad = args.get("to_squad") or args.get("target_squad")
        workflow_id_raw = str(args.get("workflow_id") or "")
        payload = args.get("payload")

        if not kind:
            return {"ok": False, "error": "kind (or type) is required"}

        envelope_id = str(uuid.uuid4())
        # Build the envelope ONCE from bridge args.  Payload is nested under its
        # own key — NOT merged/flattened — so payload keys (e.g. a crafted
        # "type" or "workflow_id") can never shadow the reserved outer fields
        # during validation (fable-audit-2 Phase 3a finding 1, round 2).
        # We validate EXACTLY this dict and persist EXACTLY this dict on success.
        hydra_env: dict[str, Any] = {
            "id": envelope_id,
            "type": kind,
            "origin_squad": from_squad or "agentsmith",
            "target_squad": to_squad,
            "workflow_id": workflow_id_raw or str(uuid.uuid4()),
        }
        if payload is not None:
            hydra_env["payload"] = payload

        # MU10 — promote type-specific required/optional fields into the
        # envelope dict so validate_envelope can find them.
        # Priority: top-level args first (explicit caller), then nested
        # payload dict (bridge-style callers who embed fields inside payload).
        # Only keys in the pre-validated allow-list are touched; reserved
        # outer keys are never overwritten (the allow-list is verified at
        # module import to exclude them).
        _allowed = _ENVELOPE_EXTRA_FIELDS.get(kind, frozenset())
        _payload_dict: dict[str, Any] = payload if isinstance(payload, dict) else {}
        for _k in _allowed:
            _val = args.get(_k)
            if _val is None:
                _val = _payload_dict.get(_k)
            if _val is not None:
                hydra_env[_k] = _val

        # Validate the same object we will persist. On failure REJECT — do NOT
        # persist. Pydantic treats "payload" as an extra field (ignored), so
        # only the canonical envelope fields are checked.
        try:
            from hydra_core.schemas import validate_envelope as _validate
            _validate(hydra_env)
        except Exception as val_exc:  # noqa: BLE001
            return {
                "ok": False,
                "error": f"envelope validation failed: {val_exc}",
            }

        # Persist to episodic db via the EightsAttestor spool path (the
        # existing envelope-persist helper — reuse, don't reinvent).
        attestor = _get_attestor()
        if attestor is not None:
            attestor.envelope_record(hydra_env)

        # Also persist locally to episodic SQLite for in-process recall.
        try:
            from hydra_core.memory import append_episodic
            append_episodic(
                workflow_id=workflow_id_raw or "no-workflow",
                kind=kind,
                payload={"envelope": hydra_env, "payload": payload},
                origin_squad=from_squad or "agentsmith",
            )
        except Exception:  # noqa: BLE001
            pass

        return {"ok": True, "envelope_id": envelope_id}

    def telemetry_tail(args: dict[str, Any]) -> dict[str, Any]:
        """Return recent telemetry/trace events from a workflow's trace.jsonl.

        Called by AgentSmith HydraBridge.telemetryTail(workflow_id, ...).
        Args: {workflow_id?: str, limit?: int, since?: str}.
        Returns {events: [{kind, payload, cursor}]}.
        """
        workflow_id = str(args.get("workflow_id") or "")
        limit = min(int(args.get("limit") or 50), 500)
        since_cursor = str(args.get("since") or "")

        events: list[dict[str, Any]] = []

        if workflow_id:
            trace_file = _HYDRA_ROOT / ".hydra" / workflow_id / "trace.jsonl"
            if trace_file.exists():
                try:
                    lines = trace_file.read_text(encoding="utf-8").splitlines()
                    # Apply since_cursor (line index) if provided.
                    start = 0
                    if since_cursor:
                        try:
                            start = int(since_cursor) + 1
                        except ValueError:
                            start = 0
                    for idx, line in enumerate(lines[start:], start=start):
                        line = line.strip()
                        if not line:
                            continue
                        try:
                            ev = json.loads(line)
                        except json.JSONDecodeError:
                            continue
                        events.append({
                            "kind": ev.get("kind", "event"),
                            "payload": ev,
                            "cursor": str(idx),
                        })
                        if len(events) >= limit:
                            break
                except Exception as exc:  # noqa: BLE001
                    return {"ok": False, "error": f"trace read error: {exc}", "events": []}
        else:
            # No workflow_id: scan all recent workflows and tail each.
            hydra_dir = _HYDRA_ROOT / ".hydra"
            if hydra_dir.is_dir():
                wf_dirs = sorted(
                    (d for d in hydra_dir.iterdir() if d.is_dir() and (d / "trace.jsonl").exists()),
                    key=lambda d: d.stat().st_mtime, reverse=True,
                )[:5]
                for wf_dir in wf_dirs:
                    try:
                        lines = (wf_dir / "trace.jsonl").read_text(encoding="utf-8").splitlines()
                        for idx, line in enumerate(lines[-limit:]):
                            line = line.strip()
                            if not line:
                                continue
                            try:
                                ev = json.loads(line)
                            except json.JSONDecodeError:
                                continue
                            events.append({
                                "kind": ev.get("kind", "event"),
                                "payload": ev,
                                "cursor": f"{wf_dir.name}:{idx}",
                            })
                    except Exception:  # noqa: BLE001
                        continue
                events = events[-limit:]

        return {"ok": True, "events": events}

    def workflow_budget(args: dict[str, Any]) -> dict[str, Any]:
        """Read or set the budget ledger for a workflow.

        No workflow_id   → list all workflows (latest-first) with budget summary.
        With workflow_id → full ledger: budget_usd, spent_usd, repo_budgets, etc.
        With set_budget  → patch budget_usd via the M3 capability verification
                           path (same as resume modify-budget but without graph
                           re-invocation). Returns {set: true, budget_usd, ...}.
        """
        workflow_id_raw = args.get("workflow_id")
        set_budget_raw = args.get("set_budget")

        workflow_id = str(workflow_id_raw) if workflow_id_raw not in (None, "") else None
        if workflow_id is not None and not _WORKFLOW_ID_RE.match(workflow_id):
            return {"ok": False, "error": "invalid_workflow_id"}

        set_budget: float | None = None
        if set_budget_raw is not None:
            try:
                set_budget = float(set_budget_raw)
            except (TypeError, ValueError):
                return {"ok": False, "error": "set_budget must be numeric"}
            if set_budget < 0:
                return {"ok": False, "error": "set_budget must be non-negative"}

        cli_args = ["budget"]
        if workflow_id:
            cli_args.append(workflow_id)
        if set_budget is not None:
            cli_args.extend(["--set", str(set_budget)])

        try:
            result = _run_cli_json(
                cli_args,
                timeout_s=60,
                err_label="budget",
                workflow_id=workflow_id,
            )
            if not isinstance(result, dict):
                return {"ok": False, "error": "budget command returned non-object"}
            result.setdefault("ok", "error" not in result)
            return result
        except Exception as e:  # noqa: BLE001
            logger.exception("workflow_budget failed")
            return {"ok": False, "error": f"budget_failed: {e}"}

    return {
        "hydra.control.ping": ping,
        "hydra.workflow.launch": workflow_launch,
        "hydra.workflow.plan": workflow_plan,
        "hydra.workflow.step": workflow_step,
        "hydra.workflow.submit_host_result": workflow_submit_host_result,
        "hydra.workflow.resume": workflow_resume,
        "hydra.workflow.submit_envelopes": workflow_submit_envelopes,
        "hydra.workflow.budget": workflow_budget,
        "hydra.cockpit.audit": cockpit_audit,
        "hydra.venom.cross_check": venom_cross_check,
        "hydra.squad.list": squad_list,
        "hydra.envelope.record": envelope_record,
        "hydra.telemetry.tail": telemetry_tail,
    }


_TOOL_SCHEMAS: dict[str, dict[str, Any]] = {
    "hydra.control.ping": {
        "description": ("No-arg liveness probe: returns ok + hydra root. "
                        "Used by AgentMesh's mcp-tool-call healthProbe."),
        "inputSchema": {"type": "object", "properties": {}},
    },
    "hydra.workflow.resume": {
        "description": (
            "Resolve a pending HITL gate: launches a DETACHED `hydra resume` "
            "CLI subprocess and returns immediately ({ok, launched, pid, log}). "
            "Idempotent at the CLI layer — no pending gate means no-op. "
            "WRITE tool: only reachable via meshd's sanctioned write path."),
        "inputSchema": {
            "type": "object",
            "properties": {
                "workflow_id": {"type": "string"},
                "action": {"type": "string", "enum": list(_RESUME_ACTIONS)},
                "option": {"type": "string"},
            },
            "required": ["workflow_id", "action"],
        },
    },
    "hydra.workflow.launch": {
        "description": (
            "Launch a NEW Hydra workflow deterministically (detached "
            "`hydra run --live`). The sanctioned engineering launch surface for "
            "the hybrid supervisor — hand the goal here instead of hand-writing "
            "code; engineering runs through the pair-programmer stage loop in "
            "Python. Returns immediately ({ok, launched, pid, workflow_id, log}). "
            "Pre-allocates the workflow_id so the caller can attach + resume."),
        "inputSchema": {
            "type": "object",
            "properties": {
                "goal": {"type": "string"},
                "squad": {"type": "string",
                          "description": "Comma-separated squad slugs to force-select (optional)."},
                "budget": {"type": "number", "description": "Budget cap in USD (optional)."},
                "workflow_id": {"type": "string",
                                "description": "Pre-allocated workflow id (optional)."},
                "risk": {"type": "string", "enum": ["low", "medium", "high"],
                         "description": "Operator risk tolerance hint forwarded as --risk to the CLI (optional)."},
            },
            "required": ["goal"],
        },
    },
    "hydra.workflow.plan": {
        "description": (
            "Plan a goal WITHOUT dispatching, returning the plan IN-BAND "
            "(synchronous `hydra plan`). The non-detaching planning surface for "
            "attended (host-bridged) execution: routes + decomposes the goal and "
            "returns {ok, workflow_id, selected_squads, tasks (TaskState[]), "
            "requires_human_approval, pending_hitl, budget} so the host can drive "
            "dispatch itself with visible Agent subagents. Executes NO squad. When "
            "the planner requires approval, the run halts at the approval gate and "
            "`pending_hitl` is populated (resolve via hydra.workflow.resume). The "
            "pre-allocated workflow_id threads continuity into resume."),
        "inputSchema": {
            "type": "object",
            "properties": {
                "goal": {"type": "string"},
                "squad": {"type": "string",
                          "description": "Comma-separated squad slugs to force-select (optional)."},
                "budget": {"type": "number", "description": "Budget cap in USD (optional)."},
                "workflow_id": {"type": "string",
                                "description": "Pre-allocated workflow id (optional)."},
                "risk": {"type": "string", "enum": ["low", "medium", "high"],
                         "description": "Operator risk tolerance hint forwarded as --risk to the CLI (optional)."},
            },
            "required": ["goal"],
        },
    },
    "hydra.workflow.step": {
        "description": (
            "Attended (host-bridged) execution: open the NEXT engineering stage "
            "and pause for a visible host `engineer` subagent. Scaffolds a pp run "
            "off the planned task ledger and returns {status:'awaiting_host', "
            "host_action:{agent_type:'engineer', prompt, cwd, call_key}, run_id, "
            "stage_id}. The host spawns Agent(engineer) in cwd, then calls "
            "hydra.workflow.submit_host_result. Requires a prior hydra.workflow.plan "
            "(shares its workflow_id checkpoint). Returns "
            "{status:'no_pending_engineering_task'} when engineering is done."),
        "inputSchema": {
            "type": "object",
            "properties": {"workflow_id": {"type": "string"}},
            "required": ["workflow_id"],
        },
    },
    "hydra.workflow.submit_host_result": {
        "description": (
            "Attended execution: feed a host subagent's result back into a stage "
            "and advance it ONE step. After the engineer result the engine records "
            "the attempt and pauses for the judge subagent (host_action.agent_type "
            "= judge-cross-vendor|judge-same-vendor per gate routing); after the "
            "judge verdict it runs smoke + finalizes and charges the accrued cost "
            "on the checkpointed HydraState budget. result is the subagent's output "
            "object (engineer: {text,cost_usd,tokens_in,tokens_out,model}; judge: "
            "{outcome,critique_md,judge_producer,score_json,cost_usd}). Idempotent "
            "on a stale/duplicate call_key (never double-records)."),
        "inputSchema": {
            "type": "object",
            "properties": {
                "workflow_id": {"type": "string"},
                "run_id": {"type": "string"},
                "call_key": {"type": "string",
                             "description": "The host_action.call_key being fulfilled."},
                "result": {"type": "object",
                           "description": "The host subagent's result object."},
            },
            "required": ["workflow_id", "run_id", "call_key", "result"],
        },
    },
    "hydra.workflow.submit_envelopes": {
        "description": (
            "Inject host-completed skill envelopes (DEV_TASK/PRD/ARCH_RFC from a "
            "host-run rlm-gaming/garland skill) back into a running workflow. "
            "Launches a DETACHED `hydra ingest` that forwards them to the "
            "engineering squad and drives the pair-programmer stage loop "
            "(real codegen + cross-vendor judge). Returns immediately "
            "({ok, launched, pid, log}). Idempotent at the CLI layer — a retried "
            "submit never double-dispatches (claim-before-dispatch ledger). "
            "WRITE tool: only reachable via the sanctioned host bridge."),
        "inputSchema": {
            "type": "object",
            "properties": {
                "workflow_id": {"type": "string"},
                "envelopes": {
                    "type": "array",
                    "items": {"type": "object"},
                    "description": (
                        "Typed Hydra envelopes the host-run skill emitted "
                        "(each with id/type/origin_squad/workflow_id, e.g. a "
                        "DEV_TASK with instructions/repo/pp_team)."),
                },
            },
            "required": ["workflow_id", "envelopes"],
        },
    },
    "hydra.cockpit.audit": {
        "description": (
            "C5: File a 'COCKPIT_WRITE' audit envelope to TheEights for every "
            "cockpit write action. SPOOL-SAFE: if TheEights is offline the payload "
            "is spooled locally and replayed on next workflow start. Returns "
            "{ok:true, spooled:false} on live filing or {ok:true, spooled:true} "
            "when the daemon is offline. NEVER returns ok:false for a spool — "
            "the audit must not block the operator action."),
        "inputSchema": {
            "type": "object",
            "properties": {
                "action": {
                    "type": "string",
                    "description": "The cockpit write action (e.g. launch, approve, reject).",
                },
                "actor": {
                    "type": "string",
                    "description": "Fixed server-side actor identity (e.g. 'hydra-cockpit').",
                },
                "project": {
                    "type": "string",
                    "description": "Fixed server-side project (e.g. 'Hydra').",
                },
                "trace_id": {
                    "type": "string",
                    "description": "Per-action trace id for audit lineage (fresh per write).",
                },
                "workflow_id": {
                    "type": "string",
                    "description": (
                        "Hydra workflow id (optional — present for resume/launch actions). "
                        "Validated with _WORKFLOW_ID_RE when supplied."
                    ),
                },
                "option": {
                    "type": "string",
                    "description": "Optional action option (e.g. budget amount, squad list).",
                },
                "detail": {
                    "type": "string",
                    "description": "Optional human-readable detail for the audit ledger.",
                },
            },
            "required": ["action", "actor", "project", "trace_id"],
        },
    },
    # F32-H: four governance-federation tools (AgentSmith HydraBridge contract).
    "hydra.venom.cross_check": {
        "description": (
            "Run Cerberus' require_cerberus_pass against the live venom registry "
            "for a capability + context. Called by AgentSmith HydraBridge for "
            "cross-system venom validation. Never raises — transport errors return "
            "ok=false with rationale. Returns {ok: bool, rationale: str}."),
        "inputSchema": {
            "type": "object",
            "properties": {
                "capability": {
                    "type": "string",
                    "description": "Registered venom capability name (e.g. 'shell.destructive').",
                },
                "context": {
                    "type": "object",
                    "description": "Proposed invocation context / args (optional).",
                },
                "args": {
                    "type": "object",
                    "description": "Alias for context (bridge compatibility).",
                },
            },
            "required": ["capability"],
        },
    },
    "hydra.squad.list": {
        "description": (
            "Discover all squad packs registered in this Hydra instance. "
            "Called by AgentSmith HydraBridge.squadRegistry(). "
            "Returns {squads: [{slug, name, entrypoint, active, version}]}."),
        "inputSchema": {
            "type": "object",
            "properties": {},
        },
    },
    "hydra.envelope.record": {
        "description": (
            "Validate and persist an envelope to the episodic db and eights trace. "
            "Called by AgentSmith HydraBridge.envelopeRecord(). "
            "Accepts AgentSmith bridge shape ({kind, from_squad, to_squad?, "
            "workflow_id, payload}) or Hydra-native shape ({type, origin_squad, ...}). "
            "Type-specific required fields (e.g. decision+rationale for DECISION_RECORD, "
            "payload_envelope_id for HANDOFF) may be supplied either at the top level "
            "or nested inside the payload dict — the tool promotes them automatically "
            "via an allow-list (_ENVELOPE_EXTRA_FIELDS) without allowing payload keys "
            "to shadow the reserved outer fields (id, type, origin_squad, target_squad, "
            "workflow_id, created_at, parent_id). "
            "Returns {ok: true, envelope_id: str}."),
        "inputSchema": {
            "type": "object",
            "additionalProperties": True,
            "description": (
                "Additional properties beyond the base fields are forwarded "
                "to the envelope via a per-type allow-list; reserved outer "
                "fields (id, type, origin_squad, target_squad, workflow_id, "
                "created_at, parent_id) are always set from base args and can "
                "never be overwritten by additional properties."),
            "properties": {
                "kind": {
                    "type": "string",
                    "description": "Envelope type / kind (bridge field; use 'type' for Hydra-native).",
                },
                "type": {
                    "type": "string",
                    "description": "Envelope type (Hydra-native; alias for kind).",
                },
                "from_squad": {
                    "type": "string",
                    "description": "Origin squad (bridge field).",
                },
                "origin_squad": {
                    "type": "string",
                    "description": "Origin squad (Hydra-native; alias for from_squad).",
                },
                "to_squad": {
                    "type": "string",
                    "description": "Target squad (optional).",
                },
                "workflow_id": {
                    "type": "string",
                    "description": "Workflow id for audit lineage.",
                },
                "payload": {
                    "description": (
                        "Envelope payload blob (any). For typed envelopes, "
                        "type-specific required fields may be nested here "
                        "and will be promoted to the envelope top level. "
                        "Keys 'workflow_id' and 'type' inside payload are "
                        "never promoted (anti-shadow guard)."),
                },
                "decision": {
                    "type": "string",
                    "description": "DECISION_RECORD: decision text (required for that type).",
                },
                "rationale": {
                    "type": "string",
                    "description": "DECISION_RECORD: rationale text (required for that type).",
                },
                "payload_envelope_id": {
                    "type": "string",
                    "description": "HANDOFF: UUID of the artifact being handed off (required for that type).",
                },
                "reason": {
                    "type": "string",
                    "description": "HITL_REQUEST: reason literal (required for that type).",
                },
                "summary": {
                    "type": "string",
                    "description": "HITL_REQUEST: summary text (required for that type).",
                },
                "options": {
                    "type": "array",
                    "description": "HITL_REQUEST: option strings (required for that type).",
                },
            },
        },
    },
    "hydra.telemetry.tail": {
        "description": (
            "Return recent telemetry/trace events from a workflow's trace.jsonl. "
            "Called by AgentSmith HydraBridge.telemetryTail(). "
            "Returns {events: [{kind, payload, cursor}]}. "
            "Pass 'since' (a prior cursor) to get only new events since last poll."),
        "inputSchema": {
            "type": "object",
            "properties": {
                "workflow_id": {
                    "type": "string",
                    "description": "Workflow id to tail (optional — tails recent workflows if absent).",
                },
                "limit": {
                    "type": "integer",
                    "description": "Max events to return (default 50, max 500).",
                },
                "since": {
                    "type": "string",
                    "description": "Cursor from a prior tail call — returns only events after this.",
                },
            },
        },
    },
    "hydra.workflow.budget": {
        "description": (
            "Read or set the budget ledger for a Hydra workflow. "
            "No workflow_id → list all known workflows latest-first with budget summary "
            "(budget_usd, spent_usd, phase). "
            "With workflow_id → full ledger incl repo_budgets/repo_spend. "
            "With set_budget → patch budget_usd via the M3 capability verification "
            "path (same gate as resume modify-budget, without graph re-invocation). "
            "Returns {set: true, budget_usd, prior_budget_usd, spent_usd} on set."),
        "inputSchema": {
            "type": "object",
            "properties": {
                "workflow_id": {
                    "type": "string",
                    "description": "Workflow id to inspect or patch (omit to list all).",
                },
                "set_budget": {
                    "type": "number",
                    "description": (
                        "New budget_usd ceiling to write into the checkpoint. "
                        "Requires workflow_id. Triggers M3 capability verification. "
                        "Must be non-negative."
                    ),
                },
            },
        },
    },
}


# ---------- Try the real MCP SDK first (mirrors hydra_memory/server.py) ----

def _serve_with_mcp_sdk() -> bool:
    try:
        from mcp.server import Server  # type: ignore
        from mcp.server.stdio import stdio_server  # type: ignore
        import mcp.types as t  # type: ignore
    except ImportError:
        return False

    import asyncio  # needed by _call_tool below (off-loop offload)
    handlers = _tool_handlers()
    server = Server("hydra-control")

    @server.list_tools()
    async def _list_tools():
        return [
            t.Tool(
                name=name,
                description=_TOOL_SCHEMAS.get(name, {}).get("description", name),
                inputSchema=_TOOL_SCHEMAS.get(name, {}).get(
                    "inputSchema", {"type": "object"}),
            )
            for name in handlers
        ]

    @server.call_tool()
    async def _call_tool(name: str, arguments: dict):
        if name not in handlers:
            raise ValueError(f"unknown tool: {name}")
        # Handlers are SYNC and may block for many seconds (some shell out via
        # subprocess.run up to _SUBMIT_TIMEOUT_S). Running them inline would
        # freeze the server's event loop (heartbeats/other tools). Offload to a
        # worker thread so the loop stays responsive; also keeps the handler
        # off the loop thread so its own dispatcher._run has no running loop.
        result = await asyncio.to_thread(handlers[name], arguments)
        return [t.TextContent(type="text", text=json.dumps(result))]

    async def run() -> None:
        async with stdio_server() as (r, w):
            await server.run(r, w, server.create_initialization_options())

    asyncio.run(run())
    return True


# ---------- Fallback: bare JSON-RPC over stdio ----------

def _serve_bare() -> None:
    handlers = _tool_handlers()
    sys.stderr.write("hydra-control: serving in bare-stdio fallback mode (no mcp SDK)\n")
    for raw in sys.stdin:
        raw = raw.strip()
        if not raw:
            continue
        try:
            msg = json.loads(raw)
        except json.JSONDecodeError as e:
            sys.stdout.write(json.dumps({"error": "parse_error", "detail": str(e)}) + "\n")
            sys.stdout.flush()
            continue
        try:
            method = msg.get("method") or msg.get("tool")
            args = msg.get("params") or msg.get("arguments") or {}
            if method == "list_tools":
                out = {"id": msg.get("id"), "result": list(handlers)}
            elif method in handlers:
                out = {"id": msg.get("id"), "result": handlers[method](args)}
            else:
                out = {"id": msg.get("id"), "error": f"unknown_method: {method!r}"}
        except Exception as e:
            out = {"id": msg.get("id"), "error": str(e),
                   "traceback": traceback.format_exc()}
        sys.stdout.write(json.dumps(out) + "\n")
        sys.stdout.flush()


def main() -> None:
    if not _serve_with_mcp_sdk():
        _serve_bare()
