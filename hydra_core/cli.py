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
        if old in raw and new not in raw:
            occurrences = raw.count(old)
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

    sub.add_parser("doctor")
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
    s = sub.add_parser("status")
    s.add_argument("workflow_id", nargs="?")
    t = sub.add_parser("trace")
    t.add_argument("workflow_id")
    sub.add_parser("approve").add_argument("workflow_id")

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
        "approve": lambda a: (print("approval pathway lives in the Claude Code plugin (/hydra:approve)"), 0)[1],
        "gateway-backup": _cmd_gateway_backup,
        "gateway-export-backends": _cmd_gateway_export_backends,
        "gateway-migrate-hooks": _cmd_gateway_migrate_hooks,
        "gateway-remove-old-backends": _cmd_gateway_remove_old_backends,
        "gateway-rollback": _cmd_gateway_rollback,
        "gateway-setup": _cmd_gateway_setup,
    }[args.cmd](args)


if __name__ == "__main__":                                                  # pragma: no cover
    sys.exit(main())
