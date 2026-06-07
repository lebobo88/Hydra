"""hydra — local CLI for the Enterprise Agent Mesh.

Subcommands:
  hydra doctor                       — health check (constitution, squads,
                                       venom, overlays, eights, langgraph, mcp)
  hydra verify                       — print constitution hash + refusal count
  hydra squads                       — list discovered squad packs (JSON)
  hydra run "<goal>" [--squad slug]  — start a workflow
  hydra status [<workflow_id>]       — list runs / show a run
  hydra approve <workflow_id>        — resume an HITL-paused run (= resume --action approve)
  hydra resume <workflow_id> --action approve|reject|modify-budget|
               force-dispatch|change-squads [--option …] [--live]
                                     — resolve a pending HITL gate from checkpoint
  hydra trace <workflow_id>          — tail the JSONL trace
  hydra replay <workflow_id>         — replay a workflow from a LangGraph checkpoint
               [--from-phase <phase>]  (default: intake)
               [--swap-model <id>]     (optional: test a different model)
               [--live]               (default: dry reconstruct, no spend)
                                     — mints a NEW workflow_id for the replay run
  hydra memory query <cell>          — query TheEights by cell
  hydra memory tag <key> --cells …   — attach cells to an episodic row
"""
from __future__ import annotations

import argparse
import json
import re
import sys
import warnings
from pathlib import Path
from uuid import uuid4

# Validation regex for workflow ids supplied via --workflow-id.
# BYTE-IDENTICAL to _WORKFLOW_ID_RE in mcp_servers/hydra_control/server.py.
# Do NOT change one without changing the other.
_WORKFLOW_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9\-_]{0,63}$")

# langgraph (imported lazily by `run`) transitively pulls in langchain_core,
# which emits a Pydantic-v1 UserWarning under Python 3.14. It is harmless, but
# when this module runs as a SessionStart/PreToolUse hook Claude Code surfaces
# hook stderr as an error. Silence it at the source so hook output stays clean.
warnings.filterwarnings("ignore", category=UserWarning, module=r"langchain_core.*")

from .squad_loader import discover_squads
from .state import HydraState
from .telemetry import emit, trace_path


class _NullDispatcher:
    """Inert dispatcher for the CLI smoke path. Real dispatchers come from
    the Claude Code plugin / MCP host."""
    def call_mcp(self, server, tool, args, **_kw):
        return {"status": "stub", "tool": tool, "args": args, "run_id": str(uuid4())[:8]}
    def spawn_subprocess(self, cmd, env=None):
        return {"status": "stub", "stdout": "(no subprocess from CLI)", "cmd": cmd}
    def emit_claude_prompt(self, prompt, agent=None):
        return {"status": "stub", "summary": prompt[:200], "agent": agent}
    def invoke_claude_skill(self, skill, args):
        return {"status": "stub", "summary": f"would invoke /{skill}", "args": args}


def _cmd_doctor(args) -> int:
    project = Path(args.project) if args.project else Path.cwd()
    fail_count = 0

    # --- Stage 1: constitution ----------------------------------------------
    try:
        from .immortal_head import load_constitution
        snap = load_constitution(project)
        print(f"OK:   constitution loaded  sha256={snap.sha256[:12]} "
              f"refusals={len(snap.refusals)} bytes={len(snap.text)}")
    except Exception as e:
        print(f"FAIL: constitution missing or unparseable — {e}")
        fail_count += 1

    # --- Stage 2: squad registry + deprecation ------------------------------
    packs = discover_squads(project)
    if not packs:
        print("FAIL: no squads discovered. Expected squads/<name>/squad.yaml.")
        return 1
    print(f"OK:   {len(packs)} squad(s) discovered:")
    from .version import is_deprecated
    for slug, p in packs.items():
        status = p.entrypoint
        marker = "[active]" if status != "stub" else "[ stub ]"
        dep_flag = ""
        if p.deprecated_after is not None:
            dep_flag = " [DEPRECATED]" if is_deprecated(p.deprecated_after) else f" [deprecates {p.deprecated_after}]"
        print(f"  {marker} {slug:20s}  v{p.version}  entrypoint={status:22s}  "
              f"agents={len(p.agents)}{dep_flag}")

    # --- Stage 4: cathedral overlays ----------------------------------------
    try:
        from .heads import load_aliases
        aliases = load_aliases(project)
        crowns = sorted({a.crown for a in aliases.values()})
        print(f"OK:   {len(aliases)} cathedral alias(es) across crowns: {crowns}")
    except Exception as e:
        print(f"WARN: cathedral overlay loader raised {type(e).__name__}: {e}")

    # --- Stage 3: TheEights vocabulary --------------------------------------
    try:
        from .eights import ALL_CELLS, CELL_SPECS
        if len(ALL_CELLS) == 8 and len(CELL_SPECS) == 8:
            print(f"OK:   TheEights vocabulary intact — {list(ALL_CELLS)}")
        else:
            print(f"FAIL: TheEights cell count off — {ALL_CELLS}")
            fail_count += 1
    except Exception as e:
        print(f"FAIL: TheEights import — {e}")
        fail_count += 1

    # --- Stage 3: episodic db reachable -------------------------------------
    try:
        from .memory import EPISODIC_DB, _ensure_episodic
        with _ensure_episodic(EPISODIC_DB) as conn:
            n = conn.execute("SELECT COUNT(*) FROM episodic").fetchone()[0]
        print(f"OK:   episodic db reachable  path={EPISODIC_DB} rows={n}")
    except Exception as e:
        print(f"WARN: episodic db — {e}")

    # --- Stage 5: Cerberus venom registry -----------------------------------
    try:
        from .venom import clear_registry, load_cerberus_venoms
        clear_registry()
        registered = load_cerberus_venoms(project)
        names = sorted(c.name for c in registered)
        if registered:
            print(f"OK:   Cerberus venom registry  count={len(registered)} names={names}")
        else:
            print("WARN: Cerberus venom registry empty — no venom is callable. "
                  "Check squads/engineering/cerberus.yaml.")
    except Exception as e:
        print(f"FAIL: Cerberus venom load — {e}")
        fail_count += 1

    # --- quick mode (hooks) -------------------------------------------------
    # Stop before the heavyweight checks. `--quick` is what SessionStart /
    # PreToolUse hooks run: it skips the langgraph import (whose transitive
    # langchain_core warning would pollute hook stderr) and the MCP subprocess
    # probes (too costly to spawn on every session start / tool call). It stays
    # honest — a real FAIL above still returns non-zero.
    if getattr(args, "quick", False):
        return 0 if fail_count == 0 else 1

    # --- runtime deps -------------------------------------------------------
    try:
        import langgraph  # type: ignore  # noqa
        print("OK:   langgraph installed")
    except ImportError:
        print("WARN: langgraph not installed — supervisor will use pure-python fallback")
    try:
        import pydantic  # type: ignore  # noqa
        print(f"OK:   pydantic available")
    except ImportError:
        print("FAIL: pydantic missing")
        fail_count += 1

    # --- MCP shim reachability ----------------------------------------------
    # Probe known MCP shims. Reachability is best-effort: failures warn but do
    # not fail the doctor (the dispatchers degrade gracefully).
    try:
        from .dispatcher import MCPStdioDispatcher, _load_mcp_config
    except ImportError:
        return 0 if fail_count == 0 else 1
    servers = _load_mcp_config(project)
    probes = [
        ("pp_harness", "ping", {}),
        ("hydra_memory", "list_tools", {}),
        ("executive_suite", "es.ping", {}),
        ("rlm_creative", "rlm.ping", {}),
        ("senate", "senate.ping", {}),
    ]
    dispatcher = MCPStdioDispatcher(project)
    for server, tool, tool_args in probes:
        if server not in servers:
            print(f"WARN: {server} not registered at user scope (~/.claude.json)")
            continue
        try:
            res = dispatcher.call_mcp(server, tool, tool_args)
        except Exception as e:
            print(f"WARN: {server} probe raised {type(e).__name__}: {e}")
            continue
        status = (res or {}).get("status", "unknown") if isinstance(res, dict) else "unknown"
        if status == "done":
            print(f"OK:   {server} reachable")
        else:
            err = (res or {}).get("error", "(no error field)") if isinstance(res, dict) else str(res)
            print(f"WARN: {server} unreachable — {err}")
    return 0 if fail_count == 0 else 1


def _cmd_verify(args) -> int:
    from .immortal_head import load_constitution

    project = Path(args.project) if args.project else None
    try:
        snap = load_constitution(project)
    except FileNotFoundError as e:
        print(f"FAIL: {e}", file=sys.stderr)
        return 1
    print(json.dumps({
        "path": str(snap.path),
        "sha256": snap.sha256,
        "refusals": len(snap.refusals),
        "bytes": len(snap.text),
    }, indent=2))
    return 0


def _cmd_memory_query(args) -> int:
    from .eights import ALL_CELLS
    from .memory import query_by_cell

    if args.cell not in ALL_CELLS:
        print(json.dumps({"error": f"invalid cell {args.cell!r}",
                          "valid": list(ALL_CELLS)}), file=sys.stderr)
        return 1
    rows = query_by_cell(args.cell, limit=int(args.limit),
                         workflow_id=args.workflow_id)
    print(json.dumps({"cell": args.cell, "count": len(rows), "rows": rows},
                     default=str, indent=2))
    return 0


def _cmd_memory_tag(args) -> int:
    from .memory import tag_episodic

    cells = [c.strip() for c in (args.cells or "").split(",") if c.strip()]
    if not cells:
        print(json.dumps({"error": "no cells supplied"}), file=sys.stderr)
        return 1
    merged = tag_episodic(args.key, cells, replace=bool(args.replace))
    print(json.dumps({"key": args.key, "cells": merged}, indent=2))
    return 0


def _cmd_squads(args) -> int:
    packs = discover_squads(Path(args.project) if args.project else None)
    print(json.dumps({
        slug: {
            "name": p.name,
            "entrypoint": p.entrypoint,
            "industries": list(p.industries),
            "accepts": list(p.accepts),
            "emits": list(p.emits),
            "agents": [a.slug for a in p.agents],
        }
        for slug, p in packs.items()
    }, indent=2))
    return 0


def _cmd_run(args) -> int:
    project = Path(args.project) if args.project else Path.cwd()
    # --workflow-id: use the caller-supplied id if present and valid; otherwise
    # mint a fresh uuid4(). The Hydra Cockpit bridge pre-allocates the id so it
    # can return it to the UI immediately (fire-and-attach) before the run ends.
    wf_id_override = getattr(args, "workflow_id_override", None)
    if wf_id_override is not None:
        if not _WORKFLOW_ID_RE.match(wf_id_override):
            warnings.warn(
                f"--workflow-id {wf_id_override!r} does not match "
                r"^[A-Za-z0-9][A-Za-z0-9\-_]{0,63}$ — minting a fresh uuid4() instead.",
                stacklevel=2,
            )
            workflow_id = uuid4()
        else:
            # HydraState.workflow_id is typed UUID; attempt to coerce.
            # The Hydra Cockpit bridge always supplies a standard uuid4() string
            # (e.g. "5ebd4268-5de0-4dbf-a82d-42c596d4818e").  Non-UUID tokens
            # that pass the regex (e.g. "my-custom-id") are not valid UUID literals
            # and will fail Pydantic validation; warn and fall back in that case.
            try:
                from uuid import UUID as _UUID
                workflow_id = _UUID(wf_id_override)
            except ValueError:
                warnings.warn(
                    f"--workflow-id {wf_id_override!r} is a valid identifier but not a "
                    "UUID (HydraState requires UUID) — minting a fresh uuid4() instead.",
                    stacklevel=2,
                )
                workflow_id = uuid4()
    else:
        workflow_id = uuid4()
    initial = HydraState(workflow_id=workflow_id, root_goal=args.goal)
    if args.squad:
        initial.selected_squads = [s.strip() for s in args.squad.split(",") if s.strip()]
    critique_client = None
    if args.live:
        from .dispatcher import MCPStdioDispatcher
        from .judge import MCPCritiqueClient
        dispatcher = MCPStdioDispatcher(project, verbose=args.verbose)
        # Reuse the same dispatcher for cross-vendor judge calls; pp_codex /
        # pp_gemini servers must be registered at user scope (~/.claude.json).
        critique_client = MCPCritiqueClient(dispatcher=dispatcher, cwd=project)
    else:
        dispatcher = _NullDispatcher()
    # Lazy import: pulls in langgraph (and the langchain_core warning). Keeping
    # it out of module scope means `doctor`/`squads`/`verify` never load it.
    from .supervisor import build_supervisor
    sup = build_supervisor(
        project_root=project,
        dispatcher=dispatcher,
        critique_client=critique_client,
        force_pure_python=getattr(args, "no_checkpoint", False),
    )
    emit(project, workflow_id, "workflow_start", {"goal": args.goal})
    from .supervisor import _PurePythonRunner
    if isinstance(sup, _PurePythonRunner):
        final = sup.invoke(initial)
    else:                                                # langgraph compiled graph
        final_state_dict = sup.invoke(
            initial,
            config={"configurable": {"thread_id": str(workflow_id)}},
        )
        final = HydraState.model_validate(final_state_dict) if isinstance(final_state_dict, dict) else final_state_dict
    print(json.dumps({
        "workflow_id": str(workflow_id),
        "phase": getattr(final, "phase", "?"),
        "selected_squads": getattr(final, "selected_squads", []),
        "tasks": [{"squad": t.owner_squad, "status": t.status} for t in getattr(final, "tasks", [])],
        "trace": str(trace_path(project, workflow_id)),
    }, indent=2))
    return 0


_RESUME_LOCK_GRACE_S = 30        # min age before a dead-owner lock is reclaimed
_RESUME_LOCK_HARD_CAP_S = 86_400  # PID-reuse safety valve: dead-or-alive, 24h max


def _pid_alive(pid: int) -> bool:
    """Best-effort liveness check for a lock-owner PID (cross-platform)."""
    if pid <= 0:
        return False
    import os as _os
    if _os.name == "nt":
        import ctypes
        PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
        STILL_ACTIVE = 259
        kernel32 = ctypes.windll.kernel32
        handle = kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
        if not handle:
            return False
        try:
            code = ctypes.c_ulong()
            ok = kernel32.GetExitCodeProcess(handle, ctypes.byref(code))
            return bool(ok) and code.value == STILL_ACTIVE
        finally:
            kernel32.CloseHandle(handle)
    try:  # pragma: no cover — POSIX path, Windows-first deployment
        _os.kill(pid, 0)
        return True
    except OSError:
        return False


def _acquire_resume_lock(project: Path, wf: str):
    """Atomic claim-and-resume guard (Codex verdict_ZCsp2WBc3e item 1;
    reclaim semantics hardened per verdict_uO18YVw9V4).

    O_CREAT|O_EXCL is atomic on NTFS and POSIX — exactly one of two
    near-simultaneous resumes wins the claim; the loser exits benignly with
    reason=resume_in_progress instead of double-invoking the graph.

    Reclaim policy — OWNER LIVENESS, never wall-clock for a live owner
    (verdict_sTc2ZQgHHB): the lock file carries the owner PID.
      - PID readable and ALIVE  → claim held, indefinitely. There is NO
        wall-clock path that reclaims a live owner.
      - PID readable and DEAD   → reclaim after a short grace (protects the
        window between open and pid-write+fsync).
      - PID UNREADABLE (corrupt/empty lock — liveness unverifiable) →
        reclaim only after the 24h hard cap. The cap applies to THIS case
        only: it bounds an unverifiable lock, never a live one.

    Returns (fd, lock_path) on success, or (None, lock_path) when another
    live resume holds the claim.
    """
    import os as _os
    import time as _time
    lock_dir = project / ".hydra" / wf
    lock_dir.mkdir(parents=True, exist_ok=True)
    lock_path = lock_dir / "resume.lock"
    for attempt in (0, 1):
        try:
            fd = _os.open(str(lock_path), _os.O_CREAT | _os.O_EXCL | _os.O_WRONLY)
            _os.write(fd, str(_os.getpid()).encode())
            _os.fsync(fd)
            return fd, lock_path
        except FileExistsError:
            if attempt == 1:
                return None, lock_path
            try:
                age = _time.time() - lock_path.stat().st_mtime
            except OSError:
                age = 0.0
            pid_readable = True
            try:
                owner_pid = int(lock_path.read_text().strip())
            except (OSError, ValueError):
                pid_readable = False
                owner_pid = 0
            if pid_readable:
                # Liveness is the sole authority for readable locks.
                reclaim = age >= _RESUME_LOCK_GRACE_S and not _pid_alive(owner_pid)
            else:
                # Liveness unverifiable — bounded by the hard cap only.
                reclaim = age >= _RESUME_LOCK_HARD_CAP_S
            if reclaim:
                try:
                    lock_path.unlink()
                except OSError:
                    pass
                continue  # one re-claim attempt via O_EXCL (still atomic)
            return None, lock_path
    return None, lock_path  # pragma: no cover — loop always returns


def _prune_spooled_hitl_requests(workflow_id: str, gate_node: str | None) -> int:
    """Late-spool reconciliation (mesh-console-unification C3,
    Codex verdict_IhqMFtUpua item 2; gate-identity scoping per
    verdict_-o_Ks3I_dI).

    A gate filed while TheEights was down sits in the eights-pending spool.
    If the operator resolves that gate from the LIVE surface (mesh.hitl.list
    'hydra-live' rows have no eights ticket), a later spool replay would file
    a ticket for an already-resolved gate — a permanent orphan in the
    pending queue. Pruning at resume time prevents the orphan at its source.

    SCOPE — keyed to the GATE IDENTITY (workflow_id + gate_node), the same
    dedupe key the mesh merge uses. A different unresolved gate in the SAME
    workflow (different gate_node) survives. Only when the resolved gate has
    no recorded gate_node (pre-C2 state) does the prune fall back to entries
    that ALSO lack a gate_node — never a wildcard over the workflow. All
    other spooled payload classes (attestations, envelope records,
    proposals) are always preserved.

    COMPLETENESS INVARIANT (verdict_QLdpFA8Qdq): every spooled hitl.request
    written by C2+ code carries payload.gate_node — `EightsAttestor
    .hitl_request` ALWAYS emits it ("unspecified" floor when a caller passes
    none; pinned by test_hitl_request_always_carries_gate_node). And because
    the spool entry and the checkpoint's pending_hitl are written by the
    SAME node execution, they are version-consistent: a keyed gate can never
    coexist with an unkeyed spool entry for itself. The keyed/unkeyed
    branches above therefore partition reality exactly — no orphan class
    falls between them.
    """
    import os as _os
    from .eights.pending_spool import DEFAULT_SPOOL_ROOT
    root = Path(_os.environ.get("HYDRA_EIGHTS_SPOOL") or DEFAULT_SPOOL_ROOT)
    if not root.exists():
        return 0
    pruned = 0
    for f in root.glob("*.json"):
        try:
            d = json.loads(f.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue  # corrupt files are an operator concern — never touched
        if d.get("tool") != "eights.governance.hitl.request":
            continue
        args = d.get("args") or {}
        if not (d.get("workflow_id") == workflow_id or args.get("run_id") == workflow_id):
            continue
        spooled_gate = ((args.get("payload") or {}).get("gate_node")
                        if isinstance(args.get("payload"), dict) else None)
        if gate_node:
            if spooled_gate != gate_node:
                continue  # a DIFFERENT gate in this workflow — must replay
        elif spooled_gate:
            continue  # resolved gate has no identity; never wildcard a keyed entry
        try:
            f.unlink()
            pruned += 1
        except OSError:
            pass
    return pruned


def _release_resume_lock(fd, lock_path) -> None:
    import os as _os
    try:
        _os.close(fd)
    except OSError:
        pass
    try:
        lock_path.unlink()
    except OSError:
        pass


def _cmd_resume(args) -> int:
    """Resume an HITL-paused workflow from its checkpoint.

    Campaign mesh-console-unification C2 (2026-06-05): replaces the old
    `approve` stub. Clears `pending_hitl`, appends the resolution to
    `hitl_history`, applies action-specific patches, then re-invokes the
    compiled graph with the workflow's thread_id so LangGraph continues from
    the interrupt. Idempotent: a workflow with no pending gate is a no-op
    (exit 0) so a retried resume launch never double-applies. Concurrent
    resumes are serialized by an atomic per-workflow lock file.
    """
    project = Path(args.project) if args.project else Path.cwd()
    wf = str(args.workflow_id)
    action = args.action
    option = getattr(args, "option", None)

    # Atomic claim BEFORE reading gate state (claim-then-check): the loser of
    # a concurrent double-resume must never observe the still-uncleared gate.
    lock_fd, lock_path = _acquire_resume_lock(project, wf)
    if lock_fd is None:
        print(json.dumps({
            "workflow_id": wf,
            "resumed": False,
            "reason": "resume_in_progress",
            "lock": str(lock_path),
        }))
        return 0
    try:
        return _cmd_resume_locked(args, project, wf, action, option)
    finally:
        _release_resume_lock(lock_fd, lock_path)


def _cmd_resume_locked(args, project: Path, wf: str, action: str, option) -> int:

    critique_client = None
    if getattr(args, "live", False):
        from .dispatcher import MCPStdioDispatcher
        from .judge import MCPCritiqueClient
        dispatcher = MCPStdioDispatcher(project, verbose=getattr(args, "verbose", False))
        critique_client = MCPCritiqueClient(dispatcher=dispatcher, cwd=project)
    else:
        dispatcher = _NullDispatcher()

    from .supervisor import build_supervisor, _PurePythonRunner
    sup = build_supervisor(
        project_root=project,
        dispatcher=dispatcher,
        critique_client=critique_client,
    )
    if isinstance(sup, _PurePythonRunner):
        print(json.dumps({
            "error": "langgraph unavailable — resume requires the checkpointing supervisor",
        }), file=sys.stderr)
        return 1

    config = {"configurable": {"thread_id": wf}}
    snap = sup.get_state(config)
    if snap is None or not snap.values:
        print(json.dumps({"workflow_id": wf, "error": "not_found"}))
        return 1
    values = snap.values
    pending = values.get("pending_hitl")
    if not pending:
        print(json.dumps({
            "workflow_id": wf,
            "resumed": False,
            "reason": "no_pending_gate",
            "phase": values.get("phase"),
        }))
        return 0

    from datetime import datetime, timezone
    resolution = {
        **(pending if isinstance(pending, dict) else {}),
        "resolution": action,
        "option": option,
        "resolved_at": datetime.now(timezone.utc).isoformat(),
    }
    patch: dict = {"pending_hitl": None, "hitl_history": [resolution]}

    if action == "change-squads":
        if not option:
            print(json.dumps({"error": "change-squads needs --option \"squad-a,squad-b\""}),
                  file=sys.stderr)
            return 1
        patch["selected_squads"] = [s.strip() for s in option.split(",") if s.strip()]
    if action == "modify-budget":
        try:
            budget = values.get("budget")
            b = dict(budget) if isinstance(budget, dict) else (
                budget.model_dump(mode="json") if hasattr(budget, "model_dump") else {})
            b["budget_usd"] = float(option)
            patch["budget"] = b
        except (TypeError, ValueError):
            print(json.dumps({"error": f"modify-budget needs a numeric --option, got {option!r}"}),
                  file=sys.stderr)
            return 1

    sup.update_state(config, patch)
    # C3: prevent a later spool replay from filing a ticket for this
    # now-resolved gate (late-spool orphan reconciliation, gate-identity-keyed).
    pruned_spool = _prune_spooled_hitl_requests(wf, resolution.get("gate_node"))
    emit(project, wf, "hitl_resumed", {
        "action": action,
        "option": option,
        "gate_node": resolution.get("gate_node"),
        "pruned_spooled_hitl_requests": pruned_spool,
    })

    if action == "reject":
        # A rejected gate does NOT continue the graph; the workflow stays
        # parked as 'surfaced' with the resolution on record.
        sup.update_state(config, {"phase": "surfaced"})
        print(json.dumps({
            "workflow_id": wf,
            "resumed": False,
            "action": "reject",
            "phase": "surfaced",
        }, indent=2))
        return 0

    final_dict = sup.invoke(None, config=config)
    phase = final_dict.get("phase") if isinstance(final_dict, dict) else getattr(final_dict, "phase", "?")
    print(json.dumps({
        "workflow_id": wf,
        "resumed": True,
        "action": action,
        "phase": phase,
        "trace": str(trace_path(project, wf)),
    }, indent=2))
    return 0


def _cmd_status(args) -> int:
    project = Path(args.project) if args.project else Path.cwd()
    base = project / ".hydra"
    if args.workflow_id:
        p = trace_path(project, args.workflow_id)
        if not p.exists():
            print(f"no trace at {p}")
            return 1
        print(p.read_text(encoding="utf-8"))
        return 0
    if not base.exists():
        print("(no workflows yet)")
        return 0
    for d in sorted(base.iterdir()):
        if d.is_dir():
            print(d.name)
    return 0


def _cmd_trace(args) -> int:
    project = Path(args.project) if args.project else Path.cwd()
    p = trace_path(project, args.workflow_id)
    if not p.exists():
        print(f"no trace at {p}")
        return 1
    print(p.read_text(encoding="utf-8"))
    return 0


# ---------- gateway management ----------

# ---------------------------------------------------------------------------
# Replay subcommand constants  (C6)
# ---------------------------------------------------------------------------

# The canonical phase order — mirrors supervisor.py interrupt_before boundaries.
# Used both for --from-phase validation and for graph re-entry position.
_KNOWN_PHASES = frozenset([
    "intake", "planning", "approval", "dispatch",
    "executing", "judge", "synthesis", "postcheck",
])

# Model-id charset: alphanumeric plus hyphen, dot, underscore, slash, colon.
# Covers ids like "claude-sonnet-4-6", "gpt-4o", "gemini-2-flash", "openai/o3".
# Max 128 chars so no argv token can be unreasonably long.
_MODEL_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9\-_./:]{0,127}$")


def _cmd_replay(args) -> int:
    """Replay a past workflow from its LangGraph checkpoint.

    C6 (Hydra Cockpit): adds a deterministic CLI surface for replay so the
    Cockpit bridge can launch it as a fixed-argv detached subprocess.

    Behaviour
    ---------
    * Loads the LangGraph checkpoint for <source_workflow_id> (keyed by
      thread_id=workflow_id in SqliteSaver at ~/.hydra/checkpoints.db).
    * Reconstructs the graph from --from-phase (default: intake) — the graph
      is re-invoked with the state snapshot at that phase boundary.
    * Mints a NEW workflow_id for the replay run (original is untouched).
    * Emits a replay_start trace event and a workflow_start for the new id.
    * --swap-model: string; stored in replay_state.swap_model and surfaced in
      the trace for regression / cost-study use. The supervisor honours it when
      building the dispatcher if an MCPStdioDispatcher override is present.
    * --live: uses the live MCP dispatcher (real spend). Without --live the
      NullDispatcher is used (dry reconstruct, no spend). The Cockpit bridge
      is venom-gated when --live is requested.
    * The new workflow_id is printed to stdout as JSON so the bridge can
      capture it from the log header line (fire-and-attach).

    Idempotency: a replay always produces a distinct new lineage; the source
    checkpoint is read-only and never mutated.
    """
    project = Path(args.project) if args.project else Path.cwd()
    source_wf = str(args.workflow_id)

    # Validate source workflow_id
    if not _WORKFLOW_ID_RE.match(source_wf):
        print(json.dumps({
            "error": f"invalid workflow_id {source_wf!r}",
            "detail": "must match ^[A-Za-z0-9][A-Za-z0-9\\-_]{{0,63}}$",
        }), file=sys.stderr)
        return 1

    from_phase = getattr(args, "from_phase", None) or "intake"
    # Validate --from-phase against known phases
    if from_phase not in _KNOWN_PHASES:
        print(json.dumps({
            "error": f"invalid --from-phase {from_phase!r}",
            "valid": sorted(_KNOWN_PHASES),
        }), file=sys.stderr)
        return 1

    swap_model = getattr(args, "swap_model", None)
    if swap_model is not None and not _MODEL_ID_RE.match(swap_model):
        print(json.dumps({
            "error": f"invalid --swap-model {swap_model!r}",
            "detail": "must match ^[A-Za-z0-9][A-Za-z0-9\\-_./:]{{0,127}}$",
        }), file=sys.stderr)
        return 1

    live = getattr(args, "live", False)

    # Mint a NEW workflow_id for the replay lineage
    replay_wf = uuid4()

    # Build dispatcher
    critique_client = None
    if live:
        from .dispatcher import MCPStdioDispatcher
        from .judge import MCPCritiqueClient
        dispatcher = MCPStdioDispatcher(project, verbose=getattr(args, "verbose", False))
        critique_client = MCPCritiqueClient(dispatcher=dispatcher, cwd=project)
    else:
        dispatcher = _NullDispatcher()

    # Lazy import (same as _cmd_run)
    from .supervisor import build_supervisor, _PurePythonRunner
    sup = build_supervisor(
        project_root=project,
        dispatcher=dispatcher,
        critique_client=critique_client,
    )

    if isinstance(sup, _PurePythonRunner):
        print(json.dumps({
            "error": "langgraph unavailable — replay requires the checkpointing supervisor",
        }), file=sys.stderr)
        return 1

    # Load source checkpoint
    source_config = {"configurable": {"thread_id": source_wf}}
    snap = sup.get_state(source_config)
    if snap is None or not snap.values:
        print(json.dumps({
            "source_workflow_id": source_wf,
            "error": "checkpoint_not_found",
            "detail": f"No checkpoint for workflow_id={source_wf!r}. "
                      "Run `hydra status` to list known workflows.",
        }), file=sys.stderr)
        return 1

    # Reconstruct state at the requested phase boundary
    values: dict = dict(snap.values)
    current_phase = values.get("phase", "intake")

    # Reset state to the from_phase starting point:
    # keep the root_goal, selected_squads, budget; clear runtime artifacts.
    replay_initial = HydraState(
        workflow_id=replay_wf,
        root_goal=values.get("root_goal", ""),
        phase=from_phase,
        selected_squads=values.get("selected_squads", []),
    )
    # Copy budget snapshot if present
    budget = values.get("budget")
    if budget is not None:
        if isinstance(budget, dict):
            try:
                from .state import BudgetLedger
                replay_initial.budget = BudgetLedger.model_validate(budget)
            except Exception:
                pass  # non-fatal: replay proceeds with default budget
        else:
            replay_initial.budget = budget

    # Record the replay provenance in the trace (source id, phase, swap_model)
    emit(project, replay_wf, "replay_start", {
        "source_workflow_id": source_wf,
        "source_phase": current_phase,
        "from_phase": from_phase,
        "swap_model": swap_model,
        "live": live,
    })
    emit(project, replay_wf, "workflow_start", {
        "goal": replay_initial.root_goal,
        "replay": True,
        "source_workflow_id": source_wf,
    })

    # Invoke the graph with the new thread_id
    replay_config = {"configurable": {"thread_id": str(replay_wf)}}

    # If swap_model is requested, stash it in the environment so any
    # model-selection logic in the supervisor/judge can honour it.
    # We don't mutate the dispatcher here (that's a deeper extension);
    # we document it in the trace and expose it for callers that check
    # the state snapshot.
    import os as _os
    if swap_model:
        _os.environ["HYDRA_REPLAY_MODEL"] = swap_model

    final_dict = sup.invoke(
        replay_initial,
        config=replay_config,
    )
    phase = (
        final_dict.get("phase")
        if isinstance(final_dict, dict)
        else getattr(final_dict, "phase", "?")
    )

    print(json.dumps({
        "source_workflow_id": source_wf,
        "replay_workflow_id": str(replay_wf),
        "from_phase": from_phase,
        "swap_model": swap_model,
        "live": live,
        "phase": phase,
        "trace": str(trace_path(project, replay_wf)),
    }, indent=2))
    return 0


def _cmd_gateway_backup(args) -> int:
    """Back up ~/.claude.json and ~/.claude/settings.json before gateway migration."""
    import shutil
    from datetime import datetime, timezone
    ts = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S")
    backup_dir = Path.home() / ".hydra" / "backups" / ts
    backup_dir.mkdir(parents=True, exist_ok=True)

    sources = [
        (Path.home() / ".claude.json", "claude.json.bak"),
        (Path.home() / ".claude" / "settings.json", "settings.json.bak"),
    ]
    for src, dst_name in sources:
        if src.exists():
            shutil.copy2(src, backup_dir / dst_name)
            print(f"  backed up: {src} -> {backup_dir / dst_name}")
        else:
            print(f"  skipped (not found): {src}")
    print(f"\nBackup dir: {backup_dir}")
    return 0


def _cmd_gateway_export_backends(args) -> int:
    """Export mcpServers block from ~/.claude.json to ~/.hydra/backends.json."""
    from .dispatcher import _load_user_scope_mcp, BACKEND_REGISTRY
    servers = _load_user_scope_mcp()
    if not servers:
        print("No mcpServers found in ~/.claude.json")
        return 1
    BACKEND_REGISTRY.parent.mkdir(parents=True, exist_ok=True)
    BACKEND_REGISTRY.write_text(
        json.dumps(servers, indent=2, default=str), encoding="utf-8"
    )
    print(f"Exported {len(servers)} backends to {BACKEND_REGISTRY}")
    for name in sorted(servers):
        spec = servers[name]
        print(f"  {name}: {spec.get('command')} {' '.join(spec.get('args', []))[:60]}")
    return 0


def _cmd_gateway_migrate_hooks(args) -> int:
    """Update settings.json hook matchers and permissions for gateway prefix."""
    settings_path = Path.home() / ".claude" / "settings.json"
    if not settings_path.exists():
        print(f"settings.json not found at {settings_path}")
        return 1

    raw = settings_path.read_text(encoding="utf-8")
    original = raw

    replacements = [
        # Permission entries first: a bare "__*" tool wildcard is rejected by
        # Claude Code's allow-rule validator, so map the installer-written
        # "mcp__agentsmith__*" to a partial glob that names the scope it widens.
        ("mcp__agentsmith__*", "mcp__hydra_gateway__agentsmith__agentsmith_*"),
        # Repair pass: fix entries already migrated to the rejected bare-glob form.
        ("mcp__hydra_gateway__agentsmith__*", "mcp__hydra_gateway__agentsmith__agentsmith_*"),
        ("mcp__pp_harness__", "mcp__hydra_gateway__pp_harness__"),
        ("mcp__pp_codex__", "mcp__hydra_gateway__pp_codex__"),
        ("mcp__pp_gemini__", "mcp__hydra_gateway__pp_gemini__"),
        ("mcp__eights__", "mcp__hydra_gateway__eights__"),
        ("mcp__agentsmith__", "mcp__hydra_gateway__agentsmith__"),
        ("mcp__hydra_memory__", "mcp__hydra_gateway__hydra_memory__"),
        ("mcp__executive_suite__", "mcp__hydra_gateway__executive_suite__"),
        ("mcp__rlm_creative__", "mcp__hydra_gateway__rlm_creative__"),
    ]
    count = 0
    for old, new in replacements:
        # Naturally idempotent: every migrated form starts with
        # "mcp__hydra_gateway__", which never contains an un-migrated
        # "mcp__<backend>__" substring, so no "already migrated" guard needed.
        occurrences = raw.count(old)
        if occurrences:
            raw = raw.replace(old, new)
            count += occurrences
            print(f"  {old} -> {new} ({occurrences} occurrences)")

    if count == 0:
        print("No matchers to update (already migrated or no matches found)")
        return 0

    settings_path.write_text(raw, encoding="utf-8")
    print(f"\nUpdated {count} matcher/permission entries in {settings_path}")
    return 0


def _cmd_gateway_remove_old_backends(args) -> int:
    """Remove old backend entries from ~/.claude.json (keep only hydra_gateway)."""
    from .dispatcher import BACKEND_REGISTRY
    if not BACKEND_REGISTRY.exists():
        print("ERROR: ~/.hydra/backends.json must exist before removing old entries.")
        print("Run: hydra gateway-export-backends first.")
        return 1

    claude_json = Path.home() / ".claude.json"
    if not claude_json.exists():
        print("~/.claude.json not found")
        return 1

    raw = json.loads(claude_json.read_text(encoding="utf-8"))
    mcp = raw.get("mcpServers", {})
    keep = {"hydra_gateway", "hydra_toolshed"}
    removed = [k for k in list(mcp) if k not in keep]
    for k in removed:
        del mcp[k]

    raw["mcpServers"] = mcp
    claude_json.write_text(json.dumps(raw, indent=2, default=str), encoding="utf-8")
    print(f"Removed {len(removed)} backend entries from ~/.claude.json: {removed}")
    print(f"Remaining: {sorted(mcp.keys())}")
    return 0


def _cmd_gateway_rollback(args) -> int:
    """Restore ~/.claude.json and settings.json from a backup."""
    import shutil
    backup_dir = Path(args.backup) if args.backup else None
    if not backup_dir:
        backups_root = Path.home() / ".hydra" / "backups"
        if backups_root.exists():
            dirs = sorted(backups_root.iterdir(), reverse=True)
            if dirs:
                backup_dir = dirs[0]
    if not backup_dir or not backup_dir.exists():
        print("No backup found. Specify --backup <path>")
        return 1

    targets = [
        ("claude.json.bak", Path.home() / ".claude.json"),
        ("settings.json.bak", Path.home() / ".claude" / "settings.json"),
    ]
    for bak_name, target in targets:
        bak = backup_dir / bak_name
        if bak.exists():
            shutil.copy2(bak, target)
            print(f"  restored: {bak} -> {target}")
        else:
            print(f"  skipped (no backup): {bak_name}")
    print(f"\nRollback complete from {backup_dir}")
    return 0


def _cmd_gateway_setup(args) -> int:
    """Interactive setup for fresh machines. Discovers siblings, writes backends.json."""
    import os
    templates_path = Path(__file__).parent / "gateway_templates.json"
    if not templates_path.exists():
        print(f"Template registry not found at {templates_path}")
        return 1

    templates = json.loads(templates_path.read_text(encoding="utf-8"))
    hydra_root = Path(__file__).resolve().parents[1]

    default_paths = {
        "HYDRA_ROOT": str(hydra_root),
        "PP_ROOT": str(hydra_root.parent / "pair-programmer"),
        "EIGHTS_ROOT": str(hydra_root.parent / "TheEights"),
        "AGENTSMITH_ROOT": str(hydra_root.parent / "AgentSmith"),
        "ES_ROOT": str(hydra_root.parent / "ExecutiveSuite"),
        "RLM_ROOT": str(hydra_root.parent / "RLM-Creative"),
        "SENATE_ROOT": str(hydra_root.parent / "Senate"),
        "USERPROFILE": os.environ.get("USERPROFILE", str(Path.home())),
    }

    backends: dict[str, dict] = {}
    for name, template in templates.items():
        if name.startswith("_"):
            continue
        required = template.get("required", False)
        desc = template.get("description", name)

        spec: dict[str, Any] = {"type": template.get("type", "stdio")}
        spec["command"] = template["command"]

        if "args_template" in template:
            spec["args"] = [_interpolate(a, default_paths) for a in template["args_template"]]
        else:
            spec["args"] = template.get("args", [])

        if "cwd_template" in template:
            spec["cwd"] = _interpolate(template["cwd_template"], default_paths)

        if "env_template" in template:
            spec["env"] = {k: _interpolate(v, default_paths) for k, v in template["env_template"].items()}
        elif "env" in template:
            spec["env"] = template["env"]

        check_path = spec["args"][0] if spec["args"] else spec.get("cwd", "")
        exists = Path(check_path).exists() if check_path else False

        if exists or required:
            backends[name] = spec
            status = "FOUND" if exists else "REQUIRED (not found)"
            print(f"  [{status}] {name}: {desc}")
        else:
            print(f"  [SKIP]  {name}: {desc} — not found at {check_path}")

    from .dispatcher import BACKEND_REGISTRY
    BACKEND_REGISTRY.parent.mkdir(parents=True, exist_ok=True)
    BACKEND_REGISTRY.write_text(json.dumps(backends, indent=2), encoding="utf-8")
    print(f"\nWrote {len(backends)} backends to {BACKEND_REGISTRY}")
    return 0


def _interpolate(template: str, values: dict[str, str]) -> str:
    result = template
    for key, val in values.items():
        result = result.replace(f"{{{key}}}", val)
    return result


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="hydra", description="Enterprise Agent Mesh supervisor")
    ap.add_argument("--project", help="Project root (defaults to cwd)")
    sub = ap.add_subparsers(dest="cmd", required=True)

    dp = sub.add_parser("doctor")
    dp.add_argument(
        "--quick",
        action="store_true",
        help=(
            "Fast health check for hooks: constitution, squads, TheEights "
            "vocabulary, episodic DB only. Skips the langgraph import and the "
            "MCP subprocess probes."
        ),
    )
    sub.add_parser("verify")
    sub.add_parser("squads")
    r = sub.add_parser("run")
    r.add_argument("goal")
    r.add_argument("--squad", help="Comma-separated squad slugs to force-select")
    r.add_argument("--live", action="store_true", help="Use the live MCP dispatcher (talks to pp_harness etc.)")
    r.add_argument("--verbose", action="store_true", help="Verbose MCP tool list / errors")
    r.add_argument(
        "--no-checkpoint",
        action="store_true",
        help=(
            "Force the pure-Python supervisor runner (no LangGraph checkpoints, "
            "no HITL interrupts). Use for smoke tests / dev loops; production "
            "runs should let LangGraph pause at HITL gates."
        ),
    )
    r.add_argument(
        "--workflow-id",
        dest="workflow_id_override",
        default=None,
        metavar="ID",
        help=(
            "Pre-allocate the workflow id (UUID-like: [A-Za-z0-9][A-Za-z0-9-_]{0,63}). "
            "When supplied and valid, the run uses this id instead of minting a fresh one. "
            "Used by the Hydra Cockpit bridge to return the id to the UI before the run "
            "completes (fire-and-attach). If omitted or invalid, a fresh uuid4() is minted "
            "and a warning is emitted."
        ),
    )
    s = sub.add_parser("status")
    s.add_argument("workflow_id", nargs="?")
    t = sub.add_parser("trace")
    t.add_argument("workflow_id")
    ap_approve = sub.add_parser("approve")
    ap_approve.add_argument("workflow_id")
    ap_approve.add_argument("--live", action="store_true",
                            help="Continue with the live MCP dispatcher")
    # C2 (mesh-console-unification): real HITL resume from checkpoint.
    rs = sub.add_parser("resume")
    rs.add_argument("workflow_id")
    rs.add_argument("--action", required=True,
                    choices=["approve", "reject", "modify-budget",
                             "force-dispatch", "change-squads"])
    rs.add_argument("--option", help=(
        "Action argument: chosen option label, new budget USD for "
        "modify-budget, or comma-separated squads for change-squads"))
    rs.add_argument("--live", action="store_true",
                    help="Continue with the live MCP dispatcher (talks to pp_harness etc.)")
    rs.add_argument("--verbose", action="store_true")

    # C6: replay subcommand
    rp = sub.add_parser("replay", help="Replay a workflow from a LangGraph checkpoint")
    rp.add_argument("workflow_id", help="Source workflow id to replay from")
    rp.add_argument(
        "--from-phase",
        dest="from_phase",
        default="intake",
        choices=sorted(_KNOWN_PHASES),
        help="Phase to restart from (default: intake)",
    )
    rp.add_argument(
        "--swap-model",
        dest="swap_model",
        default=None,
        metavar="MODEL_ID",
        help=(
            "Model id to use instead of the original (e.g. 'claude-sonnet-4-6'). "
            "Must match [A-Za-z0-9][A-Za-z0-9\\-_./:]{{0,127}}."
        ),
    )
    rp.add_argument(
        "--live",
        action="store_true",
        help=(
            "Use the live MCP dispatcher (real spend). "
            "Without --live the run is a dry reconstruct (NullDispatcher). "
            "The Cockpit bridge venom-gates --live replay."
        ),
    )
    rp.add_argument("--verbose", action="store_true")

    # gateway management
    sub.add_parser("gateway-backup")
    sub.add_parser("gateway-export-backends")
    sub.add_parser("gateway-migrate-hooks")
    sub.add_parser("gateway-remove-old-backends")
    gr = sub.add_parser("gateway-rollback")
    gr.add_argument("--backup", help="Path to backup directory")
    sub.add_parser("gateway-setup")

    # `memory query <cell>` and `memory tag <key> --cells …`
    mem = sub.add_parser("memory")
    msub = mem.add_subparsers(dest="memcmd", required=True)
    mq = msub.add_parser("query")
    mq.add_argument("cell", help="One of qian|kun|zhen|xun|kan|li|gen|dui")
    mq.add_argument("--limit", type=int, default=50)
    mq.add_argument("--workflow-id", dest="workflow_id", default=None)
    mt = msub.add_parser("tag")
    mt.add_argument("key")
    mt.add_argument("--cells", required=True, help="Comma-separated cell slugs")
    mt.add_argument("--replace", action="store_true")

    args = ap.parse_args(argv)

    if args.cmd == "memory":
        memcmds = {"query": _cmd_memory_query, "tag": _cmd_memory_tag}
        return memcmds[args.memcmd](args)

    return {
        "doctor": _cmd_doctor,
        "verify": _cmd_verify,
        "squads": _cmd_squads,
        "run": _cmd_run,
        "status": _cmd_status,
        "trace": _cmd_trace,
        # C2: approve == resume --action approve (the old stub printed a
        # plugin pointer and did nothing; resume is now first-class).
        "approve": lambda a: _cmd_resume(argparse.Namespace(
            project=a.project, workflow_id=a.workflow_id, action="approve",
            option=None, live=getattr(a, "live", False), verbose=False)),
        "resume": _cmd_resume,
        "replay": _cmd_replay,
        "gateway-backup": _cmd_gateway_backup,
        "gateway-export-backends": _cmd_gateway_export_backends,
        "gateway-migrate-hooks": _cmd_gateway_migrate_hooks,
        "gateway-remove-old-backends": _cmd_gateway_remove_old_backends,
        "gateway-rollback": _cmd_gateway_rollback,
        "gateway-setup": _cmd_gateway_setup,
    }[args.cmd](args)


if __name__ == "__main__":                                                  # pragma: no cover
    sys.exit(main())
