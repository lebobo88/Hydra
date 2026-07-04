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
import os
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
    def run_host_agent(self, agent_type, prompt, *, cwd=None, timeout_s=None):
        return None


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
    # Explicit --repo/--repos flags are folded into the goal text so the
    # supervisor's intake parser (parse_repo_arg / parse_repos_arg) handles them
    # through the single, tested code path — no second parser. Mutually exclusive
    # (intake surfaces an HITL if both end up present).
    _goal = args.goal
    if getattr(args, "repo", None):
        _goal = f"{_goal} --repo {args.repo}"
    if getattr(args, "repos", None):
        _goal = f"{_goal} --repos {args.repos}"
    if getattr(args, "subdir", None):
        _goal = f"{_goal} --subdir {args.subdir}"
    initial = HydraState(workflow_id=workflow_id, root_goal=_goal)
    if args.squad:
        initial.selected_squads = [s.strip() for s in args.squad.split(",") if s.strip()]
    # --budget: set the workflow budget cap (the genuinely-missing wire — the
    # slash commands advertise it but the CLI run parser never accepted it).
    if getattr(args, "budget", None) is not None:
        initial.budget.budget_usd = float(args.budget)
    # --risk: recorded for audit / downstream gating. There is no dedicated
    # HydraState risk field yet, so we surface it on the start event rather than
    # silently dropping the operator's intent.
    _risk = getattr(args, "risk", None)
    critique_client = None
    if args.live:
        from .dispatcher import MCPStdioDispatcher
        from .judge import MCPCritiqueClient
        dispatcher = MCPStdioDispatcher(project, verbose=args.verbose)
        # Reuse the same dispatcher for cross-vendor judge calls; pp_codex /
        # pp_gemini servers must be registered at user scope (~/.claude.json).
        critique_client = MCPCritiqueClient(dispatcher=dispatcher, cwd=project)
        # Live path drives pp to actual code generation (start_run alone only
        # scaffolds). The skill/gateway path leaves this flag unset.
        dispatcher.drive_pp_loop = True
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
    emit(project, workflow_id, "workflow_start",
         {"goal": _goal, "budget_usd": initial.budget.budget_usd, "risk": _risk})
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


def _cmd_plan(args) -> int:
    """Non-detaching planning surface for attended (host-bridged) execution.

    Runs intake -> planner and HALTS before any squad executes (plan_only adds
    "dispatch" to the graph's interrupt_before, so the run stops at the planner
    output in both the approval-required and no-approval cases). Returns the
    planner's TaskState plan in-band so the host can then drive dispatch itself
    via the visible Agent subagents — instead of `hydra run --live` detaching a
    headless subprocess the operator cannot watch.

    Requires the LangGraph/checkpoint path: the pure-Python runner has no
    interrupt semantics, so it would run straight through dispatch. The
    pre-allocated workflow_id threads continuity (plan -> step ->
    submit_host_result -> resume all share it).
    """
    project = Path(args.project) if args.project else Path.cwd()

    # Mirror _cmd_run's workflow-id handling so a caller (the hydra.workflow.plan
    # MCP tool) can pre-allocate the id and attach to the same checkpoint.
    wf_id_override = getattr(args, "workflow_id_override", None)
    workflow_id = uuid4()
    if wf_id_override is not None and _WORKFLOW_ID_RE.match(wf_id_override):
        try:
            from uuid import UUID as _UUID
            workflow_id = _UUID(wf_id_override)
        except ValueError:
            workflow_id = uuid4()

    _goal = args.goal
    if getattr(args, "repo", None):
        _goal = f"{_goal} --repo {args.repo}"
    if getattr(args, "repos", None):
        _goal = f"{_goal} --repos {args.repos}"
    if getattr(args, "subdir", None):
        _goal = f"{_goal} --subdir {args.subdir}"

    initial = HydraState(workflow_id=workflow_id, root_goal=_goal)
    if args.squad:
        initial.selected_squads = [s.strip() for s in args.squad.split(",") if s.strip()]
    if getattr(args, "budget", None) is not None:
        initial.budget.budget_usd = float(args.budget)

    # Planning never dispatches, so a NullDispatcher is correct and cheap — it
    # lacks the `live_execution` marker, so drive_pp_loop is never auto-enabled.
    dispatcher = _NullDispatcher()
    from .supervisor import build_supervisor, _PurePythonRunner
    sup = build_supervisor(
        project_root=project,
        dispatcher=dispatcher,
        plan_only=True,
    )
    if isinstance(sup, _PurePythonRunner):
        print(json.dumps({
            "ok": False,
            "error": "langgraph unavailable — plan requires the checkpointing supervisor",
        }), file=sys.stderr)
        return 1

    _risk = getattr(args, "risk", None)
    emit(project, workflow_id, "workflow_plan", {"goal": _goal,
                                                 "budget_usd": initial.budget.budget_usd,
                                                 "risk": _risk})
    config = {"configurable": {"thread_id": str(workflow_id)}}
    sup.invoke(initial, config=config)
    snap = sup.get_state(config)
    values = snap.values if snap is not None else {}
    try:
        final = HydraState.model_validate(values) if values else initial
    except Exception:  # noqa: BLE001 — fall back to a best-effort view
        final = initial

    def _task_view(t) -> dict:
        if hasattr(t, "model_dump"):
            return t.model_dump(mode="json")
        return dict(t) if isinstance(t, dict) else {"value": str(t)}

    pending = final.pending_hitl
    print(json.dumps({
        "ok": True,
        "workflow_id": str(workflow_id),
        "phase": getattr(final, "phase", "?"),
        "selected_squads": list(getattr(final, "selected_squads", [])),
        "requires_human_approval": bool(getattr(final, "requires_human_approval", False)),
        "tasks": [_task_view(t) for t in getattr(final, "tasks", [])],
        "pending_hitl": pending if isinstance(pending, dict) else None,
        "budget": final.budget.model_dump(mode="json") if hasattr(final, "budget") else {},
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
        # Resume re-enters dispatch — drive pp to real codegen on the live path.
        dispatcher.drive_pp_loop = True
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

    # WS-AUTH run-A: mint + verify an operator-capability token for ALL
    # state-mutating resume actions (approve, force-dispatch, modify-budget,
    # change-squads).  These actions mutate checkpointed state or re-enter the
    # graph and must carry operator identity so downstream nodes can verify the
    # action is fresh and authorised.
    # Degraded posture (no HYDRA_OPERATOR_KEY → warn-and-proceed) is UNIFORM
    # across all actions — this is intentional for foundation run A; gated
    # consumers enforce cryptographic proof in runs B/C.
    # (WS-AUTH run-A comment: this block is intentionally non-enforcing on the
    # operator side; the degraded-warn posture is the documented run-A stance.)
    _MUTATING_RESUME_ACTIONS = frozenset({"approve", "force-dispatch",
                                          "modify-budget", "change-squads"})
    operator_capability_patch: dict | None = None
    if action in _MUTATING_RESUME_ACTIONS:
        import logging as _logging
        _log_cli = _logging.getLogger(__name__)
        _operator = (
            getattr(args, "operator", None)
            or os.environ.get("HYDRA_OPERATOR_ID", "")
            or ""
        )
        # Sentinel check: empty or "unknown" operator identity means we cannot
        # issue a valid human capability — doing so would let any unidentified
        # action bypass the actor_id requirement in verify_operator_capability.
        # Force degraded mint (sig.value=None) in that case and warn loudly.
        # This applies uniformly to all _MUTATING_RESUME_ACTIONS (WS-AUTH run-A).
        _UNKNOWN_OPERATORS = {"", "unknown"}
        _force_degraded = _operator.strip() in _UNKNOWN_OPERATORS
        if _force_degraded:
            _log_cli.warning(
                "operator identity unknown for action=%r; capability degraded — "
                "set HYDRA_OPERATOR_ID (or args.operator) to a real operator id "
                "to issue a verifiable capability token",
                action,
            )
            # Use a sentinel actor_id for the degraded token payload so the
            # wire format is consistent; the sig.value=None marks it unusable.
            _operator = _operator or "unknown"
        try:
            from .auth.capability import mint_for_approval
            if _force_degraded:
                # Force degraded by temporarily unsetting the key env var.
                # We do this in a narrow scope to avoid races; the key is
                # restored immediately after the call returns.
                _saved_key = os.environ.pop("HYDRA_OPERATOR_KEY", None)
                try:
                    _cap_token = mint_for_approval(
                        workflow_id=wf,
                        pending_hitl=pending if isinstance(pending, dict) else {},
                        operator=_operator,
                    )
                finally:
                    if _saved_key is not None:
                        os.environ["HYDRA_OPERATOR_KEY"] = _saved_key
            else:
                _cap_token = mint_for_approval(
                    workflow_id=wf,
                    pending_hitl=pending if isinstance(pending, dict) else {},
                    operator=_operator,
                )
            operator_capability_patch = _cap_token
            if _cap_token.get("sig", {}).get("degraded") and not _force_degraded:
                # Real operator but no key configured.
                _log_cli.warning(
                    "operator capability degraded (no HYDRA_OPERATOR_KEY); "
                    "gated consumers will reject — set HYDRA_OPERATOR_KEY to enable "
                    "cryptographic proof of approval"
                )
        except Exception as _cap_exc:  # noqa: BLE001 — never block an approval on mint failure
            _log_cli.warning(
                "mint_for_approval raised %s: %s — approval proceeds without capability token",
                type(_cap_exc).__name__, _cap_exc,
            )

        # M3: verify the just-minted capability before applying the patch.
        # Fail-closed on a tampered/invalid token; warn-and-continue on a
        # degraded token (no key or unknown operator — already warned at mint).
        if operator_capability_patch is not None:
            try:
                from .auth.capability import verify_operator_capability as _verify_cap
                _pending_for_verify = pending if isinstance(pending, dict) else {}
                _m3_cap_name = str(
                    _pending_for_verify.get("capability")
                    or _pending_for_verify.get("gate_node")
                    or _pending_for_verify.get("reason")
                    or "hitl_approve"
                )
                _m3_resource_id = str(
                    _pending_for_verify.get("resource_id")
                    or _pending_for_verify.get("proposal_id")
                    or _pending_for_verify.get("workflow_id")
                    or wf
                )
                _m3_result = _verify_cap(
                    operator_capability_patch,
                    expected_capability=_m3_cap_name,
                    expected_workflow_id=wf,
                    expected_resource_id=_m3_resource_id,
                )
                if not _m3_result.get("valid"):
                    _m3_reason = _m3_result.get("reason", "unknown")
                    # Degrade-warn for cases where no key was configured or the
                    # token is intentionally degraded (foundation run posture).
                    _m3_sig = (operator_capability_patch.get("sig") or {})
                    _m3_is_degraded = (
                        _m3_sig.get("degraded") is True
                        or _m3_sig.get("value") is None
                        or "degraded" in _m3_reason
                        or "no operator key" in _m3_reason
                        or "no key" in _m3_reason
                    )
                    if _m3_is_degraded:
                        _log_cli.warning(
                            "capability verify: degraded (%s) — approval proceeds "
                            "(set HYDRA_OPERATOR_KEY to enable cryptographic enforcement)",
                            _m3_reason,
                        )
                    else:
                        print(json.dumps({
                            "error": f"capability_verify_failed: {_m3_reason}",
                            "workflow_id": wf,
                        }), file=sys.stderr)
                        return 1
            except Exception as _m3_exc:  # noqa: BLE001
                _log_cli.warning(
                    "verify_operator_capability raised %s: %s — approval proceeds",
                    type(_m3_exc).__name__, _m3_exc,
                )

    patch: dict = {"pending_hitl": None, "hitl_history": [resolution]}
    if operator_capability_patch is not None:
        patch["operator_capability"] = operator_capability_patch

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

    # F8: reflexion_override → approve_override_raise_to_N handler.
    # When the operator approves a reflexion_override gate with the raise-to-N
    # option, parse N and set reflexion_override_granted_until on the state so
    # the next pass through node_judge_per_squad respects the raised ceiling.
    if (action == "approve"
            and isinstance(pending, dict)
            and pending.get("reason") == "reflexion_override"
            and isinstance(option, str)
            and option.startswith("approve_override_raise_to_")):
        try:
            _raise_n = int(option.rsplit("_", 1)[-1])
            patch["reflexion_override_granted_until"] = _raise_n
        except (ValueError, TypeError):
            pass

    # F10: per-option behaviour dispatch table.
    # Every option that any gate advertises must map to a real engine action.
    # No cosmetic options may remain (R3-tail post-mortem, 2026-05-21).
    _gate_reason = pending.get("reason", "") if isinstance(pending, dict) else ""

    # approve_override on over_budget gates: extend the budget ceiling by 20%
    # (at least 10 cents above current spend) so the re-entered dispatch node
    # can proceed. The extended budget is persisted into the checkpoint via patch.
    if option == "approve_override" and _gate_reason == "over_budget":
        _budget = values.get("budget")
        if _budget is not None:
            _b = (dict(_budget) if isinstance(_budget, dict) else
                  (_budget.model_dump(mode="json") if hasattr(_budget, "model_dump") else {}))
            _spent = float(_b.get("spent_usd") or 0.0)
            _cur_budget = float(_b.get("budget_usd") or 0.0)
            _b["budget_usd"] = max(_cur_budget * 1.2, _spent * 1.1 + 0.10)
            patch["budget"] = _b

    # Minor: warn when an over_budget gate is approved WITHOUT extending the
    # budget ceiling. The gate will immediately re-trigger on the next dispatch
    # iteration unless modify-budget is used first.  This is a one-line guard
    # so the operator knows why the resume appears to have no effect.
    if action == "approve" and _gate_reason == "over_budget" and option != "approve_override":
        import logging as _log_ob_mod
        _log_ob_mod.getLogger(__name__).warning(
            "over_budget gate approved without 'approve_override'; budget ceiling "
            "unchanged — gate will re-trigger on next dispatch. "
            "Use --option approve_override to extend the ceiling, or "
            "--action modify-budget to set a new explicit budget."
        )
        emit(project, wf, "hitl.over_budget_reapprove_without_extend", {
            "action": action, "option": option, "gate_reason": _gate_reason,
        })

    # acknowledge / accept_partial / approve_with_criteria: gate-clear + resume
    # is the complete engine action (no additional state change required).
    # These are validated by the option dispatch table; their mere presence here
    # prevents the "unknown option" path that would otherwise be the default.
    # pylint: disable=pointless-statement
    if option in ("acknowledge", "accept_partial", "approve_with_criteria"):
        pass  # gate-clear + resume is sufficient

    # F9: clear hitl_return_node when resuming so the routing functions return
    # to their normal paths (after_dispatch → judge_per_squad, etc.).
    if action in ("approve", "force-dispatch"):
        patch["hitl_return_node"] = None

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

    # F10: abort option → park the workflow surfaced without resuming the graph.
    # Handled AFTER the patch+spool-prune+emit so the gate resolution is fully
    # recorded before returning (mirrors the reject path).
    if option == "abort":
        sup.update_state(config, {"phase": "surfaced"})
        print(json.dumps({
            "workflow_id": wf,
            "resumed": False,
            "action": "abort_option",
            "phase": "surfaced",
        }, indent=2))
        return 0

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


def _load_envelopes_file(path: Path) -> list[dict]:
    """Load a JSON file of envelope dicts. Accepts a bare list or
    {"envelopes": [...]} / {"emitted_envelopes": [...]}."""
    raw = json.loads(path.read_text(encoding="utf-8"))
    if isinstance(raw, list):
        return [e for e in raw if isinstance(e, dict)]
    if isinstance(raw, dict):
        for key in ("envelopes", "emitted_envelopes"):
            seq = raw.get(key)
            if isinstance(seq, list):
                return [e for e in seq if isinstance(e, dict)]
    raise ValueError(
        "envelopes file must be a JSON list or an object with an "
        "'envelopes'/'emitted_envelopes' list"
    )


def _cmd_ingest(args) -> int:
    """Inject host-completed skill envelopes into a running workflow and
    dispatch the engineering leg deterministically through the pp stage loop.

    This is the continuation transport (the seam between a host-run claude-skill
    squad like rlm-gaming and the deterministic engineering engine). The host
    runs the skill, captures its emitted DEV_TASK/PRD/ARCH_RFC, and calls this
    with the SAME workflow_id so engineering dispatches exactly once.

    Exactly-once: serialized by the same atomic resume lock `hydra resume` uses,
    and claim-before-dispatch against the per-workflow ingest ledger so a crash
    or a retried submit never double-dispatches (which would leak a pp lock).
    """
    project = Path(args.project) if args.project else Path.cwd()
    wf = str(args.workflow_id)

    if not _WORKFLOW_ID_RE.match(wf):
        print(json.dumps({"error": f"invalid workflow_id {wf!r}"}), file=sys.stderr)
        return 1

    try:
        envelopes = _load_envelopes_file(Path(args.envelopes))
    except (OSError, ValueError) as e:
        print(json.dumps({"error": f"could not read --envelopes: {e}"}), file=sys.stderr)
        return 1
    if not envelopes:
        print(json.dumps({"workflow_id": wf, "ingested": False,
                          "reason": "no_envelopes"}))
        return 0

    lock_fd, lock_path = _acquire_resume_lock(project, wf)
    if lock_fd is None:
        print(json.dumps({"workflow_id": wf, "ingested": False,
                          "reason": "resume_in_progress", "lock": str(lock_path)}))
        return 0
    try:
        return _cmd_ingest_locked(args, project, wf, envelopes)
    finally:
        _release_resume_lock(lock_fd, lock_path)


def _cmd_ingest_locked(args, project: Path, wf: str, envelopes: list[dict]) -> int:
    from .ingest import (
        claim_ingested_ids,
        dispatch_ingested_envelopes,
        load_ingested_ids,
    )

    critique_client = None
    if getattr(args, "live", False):
        from .dispatcher import MCPStdioDispatcher
        from .judge import MCPCritiqueClient
        dispatcher = MCPStdioDispatcher(project, verbose=getattr(args, "verbose", False))
        critique_client = MCPCritiqueClient(dispatcher=dispatcher, cwd=project)
        # Ingest re-enters engineering dispatch — drive pp to real codegen.
        dispatcher.drive_pp_loop = True
    else:
        dispatcher = _NullDispatcher()

    packs = discover_squads(project)
    if hasattr(dispatcher, "set_squad_packs"):
        dispatcher.set_squad_packs(packs)

    # Ingest is a CONTINUATION of an existing workflow — it must run against the
    # workflow's checkpoint so engineering inherits target_repo_id/budget/task
    # ledger AND so budget gating + the over_budget HITL park are durable. If the
    # checkpoint is unavailable we FAIL LOUD rather than fabricate a fresh,
    # budget-blind state (codex follow-up: a silent non-checkpoint path was
    # ungated). Mirrors `hydra resume`.
    config = {"configurable": {"thread_id": wf}}
    from .supervisor import build_supervisor, _PurePythonRunner
    sup = build_supervisor(project_root=project, dispatcher=dispatcher,
                           critique_client=critique_client)
    if isinstance(sup, _PurePythonRunner):
        print(json.dumps({
            "workflow_id": wf, "ingested": False,
            "error": "langgraph unavailable — ingest requires the checkpointing supervisor",
        }), file=sys.stderr)
        return 1
    snap = sup.get_state(config)
    if snap is None or not snap.values:
        print(json.dumps({"workflow_id": wf, "ingested": False, "error": "not_found",
                          "detail": "no checkpoint for this workflow_id"}), file=sys.stderr)
        return 1
    try:
        state = HydraState.model_validate(snap.values)
    except Exception as e:  # noqa: BLE001
        print(json.dumps({"workflow_id": wf, "ingested": False,
                          "error": f"checkpoint_invalid: {e}"}), file=sys.stderr)
        return 1

    def _emit_ingest(event: str, payload: dict) -> None:
        emit(project, wf, event, payload)

    def _persist(new_tasks, new_envelopes) -> None:
        """Persist incrementally so open_pp_runs (the lock-release ledger),
        tasks, and budget are durable after EACH item — not deferred to the end
        of the batch (codex review item 1: deferral could leak a pp lock on a
        mid-batch crash)."""
        try:
            sup.update_state(config, {
                "tasks": new_tasks,
                "envelopes": new_envelopes,
                "open_pp_runs": state.open_pp_runs,
                "budget": state.budget.model_dump(mode="json"),
            })
        except Exception as e:  # noqa: BLE001 — never lose the dispatch result on a persist miss
            emit(project, wf, "ingest.persist_failed", {"error": str(e)})

    # PER-ITEM claim-before-dispatch (codex review item 1): claim each id to the
    # ledger immediately before dispatching THAT item, then persist incrementally.
    # A mid-batch crash therefore (a) only claims the in-flight item — items not
    # yet reached stay un-claimed and are dispatched on retry (no silent drop),
    # and (b) leaves open_pp_runs durable up to the prior item. The in-flight
    # item is at-most-once; its pp run, if started, is finalize-aborted by the
    # drive loop's own exception handler or drained by `hydra reap`.
    from .ingest import IngestItemResult, release_ingested_ids
    # Only un-claim a status that PROVABLY never reached execute_squad, so a
    # corrected re-submit with the same id is not suppressed. `unknown_target`
    # qualifies (routing rejected it before any squad call). `failed` does NOT —
    # it can be a post-`start_run` drive-loop abort that already registered an
    # open pp run, and un-claiming that would make it re-dispatchable (double
    # run). A `failed` id stays claimed; retry with a fresh envelope id (codex
    # follow-up: at-most-once must never re-dispatch a started run).
    _NOT_DISPATCHED = {"unknown_target"}
    processed: set[str] = set(load_ingested_ids(project, wf))
    agg_items: list = []
    over_budget = False
    for env_dict in envelopes:
        eid = env_dict.get("id")
        eid = str(eid) if eid is not None else None
        if eid and eid in processed:
            agg_items.append(IngestItemResult(
                envelope_id=eid, envelope_type=env_dict.get("type"), target=None,
                status="skipped_duplicate", detail="already in ingest ledger"))
            continue
        if eid:
            claim_ingested_ids(project, wf, [eid])  # claim BEFORE dispatch
        out_i = dispatch_ingested_envelopes(
            state, [env_dict], packs=packs, dispatcher=dispatcher,
            already_ingested=processed, emit_fn=_emit_ingest,
        )
        # Un-claim if this envelope never reached a squad (wrong type/parse fail)
        # so it can be re-submitted after correction; otherwise mark it processed.
        item_status = out_i.items[-1].status if out_i.items else "failed"
        if eid and item_status in _NOT_DISPATCHED:
            release_ingested_ids(project, wf, [eid])
        elif eid:
            processed.add(eid)
        agg_items.extend(out_i.items)
        _persist(out_i.new_tasks, out_i.new_envelopes)  # incremental durability
        if out_i.over_budget:
            over_budget = True
            break

    # Over-budget: surface an over_budget HITL via the checkpoint so the workflow
    # parks for /hydra:approve, matching the in-graph dispatch budget gate.
    if over_budget:
        hitl = {
            "workflow_id": wf, "reason": "over_budget", "gate_node": "ingest",
            "summary": (f"Budget exhausted during ingest: "
                        f"${state.budget.spent_usd:.4f} of ${state.budget.budget_usd:.2f}."),
            "options": ["approve_override", "abort"], "default_option": "abort",
            "spent_usd": state.budget.spent_usd, "budget_usd": state.budget.budget_usd,
        }
        try:
            sup.update_state(config, {"phase": "surfaced", "pending_hitl": hitl,
                                      "budget_downgrade_active": True})
        except Exception as e:  # noqa: BLE001
            emit(project, wf, "ingest.persist_failed", {"error": str(e)})

    summary = {
        "items": [vars(it) for it in agg_items],
        "dispatched": [it.envelope_id for it in agg_items if it.status in ("done", "running")],
        "failed": [it.envelope_id for it in agg_items if it.status in ("failed", "surfaced")],
        "skipped_duplicate": [it.envelope_id for it in agg_items if it.status == "skipped_duplicate"],
        "deferred_to_host": [it.envelope_id for it in agg_items if it.status == "deferred_to_host"],
        "over_budget": over_budget,
        "spent_usd": state.budget.spent_usd,
        "budget_usd": state.budget.budget_usd,
    }
    emit(project, wf, "ingest.complete", summary)
    print(json.dumps({
        "workflow_id": wf, "ingested": True, **summary,
        "trace": str(trace_path(project, wf)),
    }, indent=2, default=str))
    return 0


def _attended_live_dispatcher(project: Path, verbose: bool = False):
    """Build the live MCP dispatcher attended mode drives (talks to pp_harness).
    Attended mode does NOT set drive_pp_loop — the host drives the stage steps."""
    from .dispatcher import MCPStdioDispatcher
    dispatcher = MCPStdioDispatcher(project, verbose=verbose)
    packs = discover_squads(project)
    if hasattr(dispatcher, "set_squad_packs"):
        dispatcher.set_squad_packs(packs)
    return dispatcher


def _next_engineering_task(state: HydraState):
    """First engineering task the host has not yet driven to a terminal attended
    outcome, or None when engineering is fully done.

    Completion is tracked via state.attended_completed_task_ids (a replace
    channel) rather than task.status, because the `tasks` channel's _append
    reducer makes an out-of-graph status flip impossible (it would duplicate)."""
    done = set(getattr(state, "attended_completed_task_ids", []) or [])
    for t in getattr(state, "tasks", []):
        if t.owner_squad == "engineering" and str(t.task_id) not in done:
            return t
    return None


def _next_nonengineering_attended_task(state: HydraState, packs: dict):
    """First pending non-engineering task whose squad uses claude-skill or
    agent-impersonation entrypoint, that the host has not yet completed.

    These tasks cannot be driven headlessly — they need a human-in-the-loop
    attended agent. We surface them in task-list order so the host can
    dispatch one at a time, mirroring the engineering attended flow."""
    done = set(getattr(state, "attended_completed_task_ids", []) or [])
    _NON_ENG_ENTRYPOINTS = frozenset({"claude-skill", "agent-impersonation"})
    for t in getattr(state, "tasks", []):
        if str(t.task_id) in done:
            continue
        if t.owner_squad == "engineering":
            continue
        pack = packs.get(t.owner_squad)
        if pack is None:
            continue
        if pack.entrypoint in _NON_ENG_ENTRYPOINTS:
            return t, pack
    return None, None


def _resolve_pack_lead_agent(pack) -> str:
    """Resolve the supervisor / lead agent slug for a squad pack.

    Priority: first agent with authority='gatekeeper', then first agent in the
    list, then 'general-purpose' as an absolute fallback.
    """
    agents = list(getattr(pack, "agents", []) or [])
    for a in agents:
        if getattr(a, "authority", "") == "gatekeeper":
            return a.slug
    if agents:
        return agents[0].slug
    return "general-purpose"


def _resolve_pack_cwd(pack, project: Path) -> str:
    """Resolve the on-disk directory for a squad pack.

    Searches the project-local squads/ directory and resolves symlinks so
    marketing packs (which are filesystem symlinks into MarketBliss) return
    their real path.  Falls back to the project root if not found.
    """
    from .squad_loader import SQUAD_DIR_NAMES, USER_SQUAD_DIR
    for dir_name in SQUAD_DIR_NAMES:
        candidate = project / dir_name / pack.slug
        if candidate.is_dir():
            try:
                return str(candidate.resolve())
            except Exception:  # noqa: BLE001
                return str(candidate)
    # user-global fallback
    candidate = USER_SQUAD_DIR / pack.slug
    if candidate.is_dir():
        try:
            return str(candidate.resolve())
        except Exception:  # noqa: BLE001
            return str(candidate)
    return str(project)


def _resolve_task_project_path(task, state: HydraState, project: Path) -> str:
    """Resolve the engineering target dir: an allow-listed repo id when the task
    (or the workflow, via `--repo`) targets one, else the workflow project root.

    Mirrors node_dispatch's precedence (supervisor.py): per-task target_repo_id
    wins, else the workflow-level state.target_repo_id set by intake from
    `--repo`. Without the state-level fallback, attended `step` would wrongly
    target the Hydra cwd for a `--repo`-scoped goal."""
    rid = getattr(task, "target_repo_id", None) or getattr(state, "target_repo_id", None)
    if rid:
        from .repo_registry import resolve_repo_project_path
        sub = (getattr(task, "target_repo_subpath", None)
               or getattr(state, "target_repo_subpath", None))
        p = resolve_repo_project_path(rid, sub)
        if sub:
            Path(p).mkdir(parents=True, exist_ok=True)
        return str(p)
    return str(project)


def _cmd_attended_step(args) -> int:
    """Attended (host-bridged) execution: open the next pending task stage and
    PAUSE for a visible host subagent (engineer for mcp squads; pack lead agent
    for claude-skill / agent-impersonation squads).

    Loads the workflow checkpoint (task ledger + budget), dispatches the
    appropriate cursor, and returns the first host_action. The host spawns the
    visible Agent and feeds the result back via ``hydra submit-host-result``.
    Requires the LangGraph/checkpoint path.

    Engineering tasks (mcp entrypoint): scaffolds a pp run, opens an attended
    code stage via host_bridge.begin_stage, worktree-isolated.

    Non-engineering tasks (claude-skill / agent-impersonation): creates a
    lightweight squad cursor via host_bridge.begin_squad_stage, no worktree
    isolation (these produce documents, not engine code)."""
    from . import host_bridge
    from .squad_loader import discover_squads as _discover
    project = Path(args.project) if args.project else Path.cwd()
    wf = str(args.workflow_id)
    if not _WORKFLOW_ID_RE.match(wf):
        print(json.dumps({"ok": False, "error": f"invalid workflow_id {wf!r}"}),
              file=sys.stderr)
        return 1

    lock_fd, lock_path = _acquire_resume_lock(project, wf)
    if lock_fd is None:
        print(json.dumps({"ok": False, "status": "resume_in_progress",
                          "lock": str(lock_path)}))
        return 0
    try:
        dispatcher = _attended_live_dispatcher(project, getattr(args, "verbose", False))
        from .supervisor import build_supervisor, _PurePythonRunner
        sup = build_supervisor(project_root=project, dispatcher=dispatcher)
        if isinstance(sup, _PurePythonRunner):
            print(json.dumps({"ok": False,
                              "error": "langgraph unavailable — attended step requires "
                                       "the checkpointing supervisor"}), file=sys.stderr)
            return 1
        config = {"configurable": {"thread_id": wf}}
        snap = sup.get_state(config)
        if snap is None or not snap.values:
            print(json.dumps({"ok": False, "error": "not_found",
                              "detail": "no checkpoint — run `hydra plan` first"}),
                  file=sys.stderr)
            return 1
        state = HydraState.model_validate(snap.values)

        # --- Engineering task (mcp entrypoint) ---
        task = _next_engineering_task(state)
        if task is not None:
            project_path = _resolve_task_project_path(task, state, project)
            request_text = task.description or state.root_goal

            # F27: preflight — verify the engineer agent file exists before
            # staging a host_action that references it. If absent, surface a
            # clear dependency error rather than emitting a broken host_action.
            _eng_agent_file = project / ".claude" / "agents" / "engineer.md"
            if not _eng_agent_file.exists():
                print(json.dumps({
                    "ok": False, "error": "missing_agent_dependency",
                    "detail": (
                        f"engineer agent file not found: {_eng_agent_file}. "
                        "Create .claude/agents/engineer.md (or symlink from "
                        "pair-programmer/.claude/agents/engineer.md) to enable "
                        "attended engineering."
                    ),
                }), file=sys.stderr)
                return 1

            start = dispatcher.call_mcp("pp_harness", "start_run", {
                "request_text": request_text, "project_path": project_path,
                "mode": "single"}, squad_id="engineering")
            inner = start.get("result", start) if isinstance(start, dict) else {}
            run_id = (inner or {}).get("run_id") if isinstance(inner, dict) else None
            if not run_id:
                print(json.dumps({"ok": False, "error": "start_run returned no run_id",
                                  "detail": str(start)[:500]}), file=sys.stderr)
                return 1

            # F28: ensure AGENTS.md / CLAUDE.md bootstrap in the target repo,
            # mirroring squad_node._via_mcp ~1764-1774. Fail-soft.
            try:
                dispatcher.call_mcp(
                    "pp_harness", "ensure_agents_md",
                    {"project_path": project_path}, squad_id="engineering")
            except Exception:  # noqa: BLE001
                pass
            try:
                from .squad_node import _maybe_write_claude_shim
                _maybe_write_claude_shim(project_path)
            except Exception:  # noqa: BLE001
                pass

            # Register the open pp run so postcheck/reap can finalize-abort it
            # if the workflow is abandoned mid-stage (run holds the .harness lock).
            state.open_pp_runs.append({"run_id": str(run_id), "project_path": project_path})

            res = host_bridge.begin_stage(
                dispatcher, workflow_id=wf, run_id=str(run_id),
                project_path=project_path, request_text=request_text,
                model_tier=getattr(task, "model_tier", None),
                project_root=project, task_id=str(task.task_id))

            try:
                # Only open_pp_runs (replace channel) is persisted — NOT `tasks`
                # (append reducer would duplicate). Completion is recorded via
                # attended_completed_task_ids by submit-host-result.
                sup.update_state(config, {"open_pp_runs": state.open_pp_runs})
            except Exception as e:  # noqa: BLE001
                emit(project, wf, "attended.persist_failed", {"error": str(e)})

            emit(project, wf, "attended.step", {"run_id": str(run_id),
                                                "task_id": str(task.task_id),
                                                "state": res.get("state")})
            print(json.dumps({"ok": True, **res}, indent=2, default=str))
            return 0

        # --- Non-engineering task (claude-skill / agent-impersonation) ---
        packs = _discover(project)
        ne_task, ne_pack = _next_nonengineering_attended_task(state, packs)
        if ne_task is not None:
            task_id = str(ne_task.task_id)
            request_text = ne_task.description or state.root_goal
            pack_cwd = _resolve_pack_cwd(ne_pack, project)
            lead_agent = _resolve_pack_lead_agent(ne_pack)

            res = host_bridge.begin_squad_stage(
                workflow_id=wf,
                task_id=task_id,
                squad_slug=ne_pack.slug,
                entrypoint=ne_pack.entrypoint,
                lead_agent=lead_agent,
                pack_cwd=pack_cwd,
                request_text=request_text,
                project_root=project,
            )
            emit(project, wf, "attended.step", {
                "run_id": task_id,
                "task_id": task_id,
                "squad_slug": ne_pack.slug,
                "state": res.get("state"),
            })
            print(json.dumps({"ok": True, **res}, indent=2, default=str))
            return 0

        # No pending tasks of any kind.
        print(json.dumps({"ok": True, "status": "no_pending_task",
                          "workflow_id": wf}))
        return 0
    finally:
        _release_resume_lock(lock_fd, lock_path)


def _cmd_attended_submit(args) -> int:
    """Feed a host subagent's result back into an attended stage and advance it
    one step. On stage completion, charge the accrued cost on the checkpointed
    HydraState budget (keeping the 80%/100% tripwires live) and record the task
    outcome — so attended execution is never budget-blind."""
    from . import host_bridge
    from .governance import charge_and_gate
    project = Path(args.project) if args.project else Path.cwd()
    wf = str(args.workflow_id)
    if not _WORKFLOW_ID_RE.match(wf):
        print(json.dumps({"ok": False, "error": f"invalid workflow_id {wf!r}"}),
              file=sys.stderr)
        return 1
    try:
        result = json.loads(Path(args.result).read_text(encoding="utf-8"))
        if not isinstance(result, dict):
            raise ValueError("result file must be a JSON object")
    except (OSError, ValueError, json.JSONDecodeError) as e:
        print(json.dumps({"ok": False, "error": f"could not read --result: {e}"}),
              file=sys.stderr)
        return 1

    lock_fd, lock_path = _acquire_resume_lock(project, wf)
    if lock_fd is None:
        print(json.dumps({"ok": False, "status": "resume_in_progress",
                          "lock": str(lock_path)}))
        return 0
    try:
        dispatcher = _attended_live_dispatcher(project, getattr(args, "verbose", False))
        cfile = host_bridge.cursor_path(project, wf, str(args.run_id))
        if not Path(cfile).exists():
            print(json.dumps({"ok": False, "error": "cursor_not_found",
                              "detail": str(cfile)}), file=sys.stderr)
            return 1
        res = host_bridge.submit_host_result(
            dispatcher, cursor_file=cfile, call_key=str(args.call_key), result=result)

        # On terminal: charge budget on the authoritative HydraState ledger and
        # record the task outcome into the checkpoint.
        # Rider (b): skip charge if already_charged=True (idempotency guard).
        if res.get("status") in ("complete", "surfaced", "aborted"):
            if res.get("already_charged"):
                # Idempotent re-submit: cursor was already charged on the first
                # terminal submit.  Return the cached result without re-billing.
                emit(project, wf, "attended.submit",
                     {"run_id": str(args.run_id), "call_key": str(args.call_key),
                      "status": res.get("status"), "already_charged": True})
                print(json.dumps({"ok": True, **res}, indent=2, default=str))
                return 0
            # Rider (b) recovery-safe ordering: mark cursor charged BEFORE the
            # budget write to the LangGraph checkpoint so that a crash between
            # here and the checkpoint persist is an under-charge (acceptable) rather
            # than a double-charge (unsafe).  Crash-ordering rationale:
            #   1. mark_charged(cfile)          ← cursor sidecar flagged first
            #   2. charge_and_gate(...)          ← HydraState.budget mutated in memory
            #   3. sup.update_state(...)         ← checkpoint persisted
            # If the process dies after (1) but before (3), the retry sees
            # already_charged=True and skips the charge → under-charge.
            # The opposite order (charge then mark) would re-charge on that crash
            # → double-charge, which burns real spend twice.
            host_bridge.mark_charged(cfile)
            from .supervisor import build_supervisor, _PurePythonRunner
            sup = build_supervisor(project_root=project, dispatcher=dispatcher)
            if not isinstance(sup, _PurePythonRunner):
                config = {"configurable": {"thread_id": wf}}
                snap = sup.get_state(config)
                if snap is not None and snap.values:
                    state = HydraState.model_validate(snap.values)
                    cost = float(res.get("cost_usd") or 0.0)
                    toks = int(res.get("tokens_in") or 0) + int(res.get("tokens_out") or 0)
                    block, downgrade = charge_and_gate(state, cost, toks)
                    # F34: budget_charge to eights (fail-soft; never blocks local work).
                    try:
                        from .eights.attestation import EightsAttestor as _EightsAttestor
                        _att = _EightsAttestor(dispatcher=dispatcher, workflow_id=wf)
                        _att.budget_charge(
                            workflow_id=wf, usd=cost, tokens=toks,
                            purpose="attended_submit",
                        )
                    except Exception:  # noqa: BLE001 — fail-soft per F34
                        pass
                    # Mark this engineering task attended-complete (replace
                    # channel) so the next `step` does not re-pick it. We do NOT
                    # flip task.status — the `tasks` channel's _append reducer
                    # would duplicate the task on update_state.
                    tid = res.get("task_id")
                    completed = list(state.attended_completed_task_ids)
                    if tid is not None and str(tid) not in completed:
                        completed.append(str(tid))
                    open_runs = [e for e in state.open_pp_runs
                                 if e.get("run_id") != res.get("run_id")]
                    res["budget_block"] = block
                    res["budget_downgrade"] = downgrade
                    res["spent_usd"] = state.budget.spent_usd
                    try:
                        sup.update_state(config, {
                            "attended_completed_task_ids": completed,
                            "open_pp_runs": open_runs,
                            "budget": state.budget.model_dump(mode="json"),
                            "budget_downgrade_active": bool(downgrade),
                        })
                    except Exception as e:  # noqa: BLE001
                        emit(project, wf, "attended.persist_failed", {"error": str(e)})

        emit(project, wf, "attended.submit", {"run_id": str(args.run_id),
                                              "call_key": str(args.call_key),
                                              "status": res.get("status")})
        print(json.dumps({"ok": True, **res}, indent=2, default=str))
        return 0
    finally:
        _release_resume_lock(lock_fd, lock_path)


_TERMINAL_PHASES = frozenset({"done", "surfaced"})


def _is_reapable(phase, has_pending_hitl: bool,
                 age_hours: float | None, older_than_hours: float) -> bool:
    """Pure predicate: is this workflow an abandoned non-terminal thread that
    should be swept to a terminal phase? Reapable iff non-terminal AND no
    pending HITL gate AND idle at least `older_than_hours` (unknown age, i.e.
    no checkpoint timestamp, counts as old enough to reap)."""
    if phase in _TERMINAL_PHASES:
        return False
    if has_pending_hitl:
        return False
    if age_hours is not None and age_hours < older_than_hours:
        return False
    return True


def _cmd_reap(args) -> int:
    """Garbage-collect abandoned non-terminal workflows.

    Why this exists: the supervisor runs in-session and `interrupt_before`
    approval/synthesis/judge_synthesis. A run that is never resumed (test /
    exploratory) or whose driving session dies leaves a non-terminal LangGraph
    checkpoint that nothing ever advances — so `workflows_list` reports it as
    "active" forever. There was no reaper. This sweeps such threads to the
    terminal `surfaced` phase (the same transition `resume --action reject`
    uses), recording a reap marker on `hitl_history` for audit.

    Safe by construction:
      - dry-run by default; only mutates with --apply
      - skips terminal phases (done / surfaced)
      - skips workflows with a pending HITL gate (genuinely awaiting a human)
      - skips workflows touched within --older-than-hours (may still be live)
      - per-workflow resume-lock so it never races an in-flight resume
    """
    import os
    import sqlite3
    from datetime import datetime, timezone

    project = Path(args.project) if args.project else Path.cwd()
    older_than_h = float(getattr(args, "older_than_hours", 24.0))
    do_apply = bool(getattr(args, "apply", False))

    from .supervisor import build_supervisor, _PurePythonRunner
    sup = build_supervisor(project_root=project, dispatcher=_NullDispatcher())
    if isinstance(sup, _PurePythonRunner):
        print(json.dumps({
            "error": "langgraph unavailable — reap requires the checkpointing supervisor",
        }), file=sys.stderr)
        return 1

    # Enumerate checkpoint threads from the SAME store build_supervisor binds.
    cp_db = Path(os.environ.get("HYDRA_CHECKPOINT_DB")
                 or (Path.home() / ".hydra" / "checkpoints.db"))
    if not cp_db.exists():
        print(json.dumps({"scanned": 0, "candidates": [], "reaped": [],
                          "reason": "no_checkpoint_db"}, indent=2))
        return 0
    conn = sqlite3.connect(f"file:{cp_db.as_posix()}?mode=ro", uri=True,
                           check_same_thread=False)
    try:
        thread_ids = [r[0] for r in conn.execute(
            "SELECT DISTINCT thread_id FROM checkpoints LIMIT 500").fetchall()]
    except Exception:  # noqa: BLE001 — schema absent / older langgraph layout
        thread_ids = []
    finally:
        conn.close()

    now = datetime.now(timezone.utc)
    candidates: list[dict] = []
    for wf in thread_ids:
        config = {"configurable": {"thread_id": wf}}
        try:
            snap = sup.get_state(config)
        except Exception:  # noqa: BLE001 — one bad thread must not abort the sweep
            continue
        if snap is None or not snap.values:
            continue
        v = snap.values
        phase = v.get("phase")
        ts = getattr(snap, "created_at", None)
        age_h = None
        if ts:
            try:
                dt = datetime.fromisoformat(str(ts).replace("Z", "+00:00"))
                age_h = (now - dt).total_seconds() / 3600.0
            except (TypeError, ValueError):
                age_h = None
        if not _is_reapable(phase, bool(v.get("pending_hitl")), age_h, older_than_h):
            continue
        candidates.append({
            "workflow_id": wf,
            "phase": phase,
            "age_hours": round(age_h, 1) if age_h is not None else None,
            "root_goal": (v.get("root_goal") or "")[:80],
        })

    reaped: list[str] = []
    if do_apply:
        for c in candidates:
            wf = c["workflow_id"]
            lock_fd, lock_path = _acquire_resume_lock(project, wf)
            if lock_fd is None:
                c["skipped"] = "resume_in_progress"
                continue
            try:
                config = {"configurable": {"thread_id": wf}}
                marker = {
                    "resolution": "reaped",
                    "reason": (f"abandoned: non-terminal '{c['phase']}', no pending "
                               f"gate, idle > {older_than_h}h"),
                    "reaped_at": now.isoformat(),
                }
                # Same terminal transition the reject action uses (no graph re-drive).
                sup.update_state(config, {"phase": "surfaced",
                                          "hitl_history": [marker]})
                emit(project, wf, "reaped", marker)
                reaped.append(wf)
            finally:
                _release_resume_lock(lock_fd, lock_path)

    print(json.dumps({
        "mode": "apply" if do_apply else "dry-run",
        "older_than_hours": older_than_h,
        "scanned": len(thread_ids),
        "candidate_count": len(candidates),
        "candidates": candidates,
        "reaped_count": len(reaped),
        "reaped": reaped,
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
        "RLM_GAMING_ROOT": str(hydra_root.parent / "RLM-Gaming"),
        "MB_ROOT": str(hydra_root.parent / "MarketBliss"),
        "XENIA_ROOT": str(hydra_root.parent / "Xenia"),
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

        # Existence check: prefer an explicit check_path_template (the pack ROOT),
        # since python pack-shims have args[0]=="-m" which is never a real path and
        # would otherwise always SKIP. Fall back to args[0]/cwd for legacy entries.
        if "check_path_template" in template:
            check_path = _interpolate(template["check_path_template"], default_paths)
        else:
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
    r.add_argument("--budget", type=float, default=None,
                   help="Workflow budget cap in USD (sets BudgetLedger.budget_usd).")
    r.add_argument("--risk", choices=["low", "medium", "high"], default=None,
                   help="Operator risk tolerance hint (recorded on the start event).")
    r.add_argument("--repo", default=None, metavar="ID",
                   help="Single allow-listed repo id for engineering targeting "
                        "(folded into the goal; resolved by hydra_core.repo_registry).")
    r.add_argument("--repos", default=None, metavar="ID,ID,...",
                   help="Comma-separated allow-listed repo ids for fleet mode "
                        "(>=2 distinct ids). Mutually exclusive with --repo.")
    r.add_argument("--subdir", default=None, metavar="PATH",
                   help="Repo-relative engineering target under --repo/--repos, "
                        "for example 'test-5' or 'games/minecraft-hd'.")
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
    pl = sub.add_parser("plan", help=(
        "Non-detaching planning surface for attended (host-bridged) execution: "
        "run intake+planner and return the TaskState plan WITHOUT dispatching."))
    pl.add_argument("goal")
    pl.add_argument("--squad", help="Comma-separated squad slugs to force-select")
    pl.add_argument("--budget", type=float, default=None,
                    help="Workflow budget cap in USD (sets BudgetLedger.budget_usd).")
    pl.add_argument("--repo", default=None, metavar="ID",
                    help="Single allow-listed repo id (folded into the goal).")
    pl.add_argument("--repos", default=None, metavar="ID,ID,...",
                    help="Comma-separated allow-listed repo ids for fleet mode.")
    pl.add_argument("--subdir", default=None, metavar="PATH",
                    help="Repo-relative engineering target under --repo/--repos.")
    pl.add_argument("--workflow-id", dest="workflow_id_override", default=None,
                    metavar="ID",
                    help="Pre-allocate the workflow id (threads plan->step->resume).")
    pl.add_argument("--risk", choices=["low", "medium", "high"], default=None,
                    help="Operator risk tolerance hint (recorded on the plan event).")

    stp = sub.add_parser("step", help=(
        "Attended mode: open the next engineering stage and pause for a visible "
        "host `engineer` subagent (returns a host_action)."))
    stp.add_argument("workflow_id")
    stp.add_argument("--verbose", action="store_true")

    shr = sub.add_parser("submit-host-result", help=(
        "Attended mode: feed a host subagent's result back into a stage and "
        "advance it one step (charges budget on stage completion)."))
    shr.add_argument("workflow_id")
    shr.add_argument("--run-id", dest="run_id", required=True)
    shr.add_argument("--call-key", dest="call_key", required=True)
    shr.add_argument("--result", required=True, metavar="FILE",
                     help="Path to a JSON file with the subagent's result object.")
    shr.add_argument("--verbose", action="store_true")

    s = sub.add_parser("status")
    s.add_argument("workflow_id", nargs="?")
    t = sub.add_parser("trace")
    t.add_argument("workflow_id")
    # Reaper: GC abandoned non-terminal workflows (stuck at approval/synthesis
    # because their session ended or they were never resumed past an interrupt).
    rp_reap = sub.add_parser("reap")
    rp_reap.add_argument("--older-than-hours", dest="older_than_hours",
                         type=float, default=24.0,
                         help="Only reap non-terminal workflows idle this long (default 24).")
    rp_reap.add_argument("--apply", action="store_true",
                         help="Actually transition stale workflows to 'surfaced' (default: dry-run).")
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

    # Continuation transport: inject host-completed skill envelopes into a
    # running workflow and dispatch engineering deterministically.
    ing = sub.add_parser("ingest", help=(
        "Inject host-completed skill envelopes (DEV_TASK/PRD/ARCH_RFC) into a "
        "workflow and dispatch the engineering leg through the pp stage loop."))
    ing.add_argument("workflow_id")
    ing.add_argument("--envelopes", required=True, metavar="PATH",
                     help="JSON file: a list of envelope dicts, or "
                          "{'envelopes': [...]} / {'emitted_envelopes': [...]}.")
    ing.add_argument("--live", action="store_true",
                     help="Use the live MCP dispatcher (drives real pp codegen+judge).")
    ing.add_argument("--verbose", action="store_true")

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
        "plan": _cmd_plan,
        "step": _cmd_attended_step,
        "submit-host-result": _cmd_attended_submit,
        "status": _cmd_status,
        "trace": _cmd_trace,
        "reap": _cmd_reap,
        # C2: approve == resume --action approve (the old stub printed a
        # plugin pointer and did nothing; resume is now first-class).
        "approve": lambda a: _cmd_resume(argparse.Namespace(
            project=a.project, workflow_id=a.workflow_id, action="approve",
            option=None, live=getattr(a, "live", False), verbose=False)),
        "resume": _cmd_resume,
        "ingest": _cmd_ingest,
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
