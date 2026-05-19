"""hydra — local CLI for the Enterprise Agent Mesh.

Subcommands:
  hydra doctor                       — health check (squads, langgraph, mcp)
  hydra squads                       — list discovered squad packs
  hydra run "<goal>" [--squad slug]  — start a workflow
  hydra status [<workflow_id>]       — list runs / show a run
  hydra approve <workflow_id>        — resume an HITL-paused run
  hydra trace <workflow_id>          — tail the JSONL trace
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
    packs = discover_squads(Path(args.project) if args.project else None)
    if not packs:
        print("FAIL: no squads discovered. Expected squads/<name>/squad.yaml.")
        return 1
    print(f"OK: {len(packs)} squad(s) discovered:")
    for slug, p in packs.items():
        status = p.entrypoint
        marker = "[active]" if status != "stub" else "[ stub ]"
        print(f"  {marker} {slug:20s}  entrypoint={status:22s}  agents={len(p.agents)}  industries={list(p.industries)[:3]}")
    try:
        import langgraph  # type: ignore  # noqa
        print("OK: langgraph installed")
    except ImportError:
        print("WARN: langgraph not installed — supervisor will use pure-python fallback")
    try:
        import pydantic  # type: ignore  # noqa
        print(f"OK: pydantic available")
    except ImportError:
        print("FAIL: pydantic missing")
        return 1
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

    args = ap.parse_args(argv)
    return {
        "doctor": _cmd_doctor,
        "squads": _cmd_squads,
        "run": _cmd_run,
        "status": _cmd_status,
        "trace": _cmd_trace,
        "approve": lambda a: (print("approval pathway lives in the Claude Code plugin (/hydra:approve)"), 0)[1],
    }[args.cmd](args)


if __name__ == "__main__":                                                  # pragma: no cover
    sys.exit(main())
