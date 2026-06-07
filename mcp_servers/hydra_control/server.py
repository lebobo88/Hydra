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
        "type": "cockpit_write",
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


def _launch_resume(workflow_id: str, action: str, option: str | None) -> dict[str, Any]:
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

    return {
        "hydra.control.ping": ping,
        "hydra.workflow.resume": workflow_resume,
        "hydra.cockpit.audit": cockpit_audit,
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
    "hydra.cockpit.audit": {
        "description": (
            "C5: File a 'cockpit_write' audit envelope to TheEights for every "
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
}


# ---------- Try the real MCP SDK first (mirrors hydra_memory/server.py) ----

def _serve_with_mcp_sdk() -> bool:
    try:
        from mcp.server import Server  # type: ignore
        from mcp.server.stdio import stdio_server  # type: ignore
        import mcp.types as t  # type: ignore
    except ImportError:
        return False

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
        result = handlers[name](arguments)
        return [t.TextContent(type="text", text=json.dumps(result))]

    import asyncio

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
