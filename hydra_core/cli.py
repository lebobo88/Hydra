"""hydra — local CLI for the Enterprise Agent Mesh.

Subcommands:
  hydra doctor                       — health check (constitution, squads,
                                       venom, overlays, eights, langgraph, mcp)
  hydra verify                       — print constitution hash + refusal count
  hydra squads                       — list discovered squad packs (JSON)
  hydra run "<goal>" [--squad slug]  — start a workflow
  hydra status [<workflow_id>]       — list runs / show a run
  hydra approve <workflow_id>        — resume an HITL-paused run
  hydra trace <workflow_id>          — tail the JSONL trace
  hydra memory query <cell>          — query TheEights by cell
  hydra memory tag <key> --cells …   — attach cells to an episodic row
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from uuid import uuid4

from .squad_loader import discover_squads
from .state import HydraState
from .supervisor import build_supervisor
from .telemetry import emit, trace_path


class _NullDispatcher:
    """Inert dispatcher for the CLI smoke path. Real dispatchers come from
    the Claude Code plugin / MCP host."""
    def call_mcp(self, server, tool, args):
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
        ("pp-daemon", "ping", {}),
        ("hydra-memory", "list_tools", {}),
        ("executive-suite", "es.ping", {}),
        ("rlm-creative", "rlm.ping", {}),
    ]
    dispatcher = MCPStdioDispatcher(project)
    for server, tool, tool_args in probes:
        if server not in servers:
            print(f"WARN: {server} not in .mcp.json")
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
    workflow_id = uuid4()
    initial = HydraState(workflow_id=workflow_id, root_goal=args.goal)
    if args.squad:
        initial.selected_squads = [s.strip() for s in args.squad.split(",") if s.strip()]
    if args.live:
        from .dispatcher import MCPStdioDispatcher
        dispatcher = MCPStdioDispatcher(project, verbose=args.verbose)
    else:
        dispatcher = _NullDispatcher()
    sup = build_supervisor(
        project_root=project,
        dispatcher=dispatcher,
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


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="hydra", description="Enterprise Agent Mesh supervisor")
    ap.add_argument("--project", help="Project root (defaults to cwd)")
    sub = ap.add_subparsers(dest="cmd", required=True)

    sub.add_parser("doctor")
    sub.add_parser("verify")
    sub.add_parser("squads")
    r = sub.add_parser("run")
    r.add_argument("goal")
    r.add_argument("--squad", help="Comma-separated squad slugs to force-select")
    r.add_argument("--live", action="store_true", help="Use the live MCP dispatcher (talks to pp-daemon etc.)")
    r.add_argument("--verbose", action="store_true", help="Verbose MCP tool list / errors")
    s = sub.add_parser("status")
    s.add_argument("workflow_id", nargs="?")
    t = sub.add_parser("trace")
    t.add_argument("workflow_id")
    sub.add_parser("approve").add_argument("workflow_id")

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
        "approve": lambda a: (print("approval pathway lives in the Claude Code plugin (/hydra:approve)"), 0)[1],
    }[args.cmd](args)


if __name__ == "__main__":                                                  # pragma: no cover
    sys.exit(main())
