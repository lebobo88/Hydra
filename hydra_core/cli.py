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
import threading
import warnings
from pathlib import Path
from typing import Any, Callable
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
from .strict_json import (
    dumps_strict,
    finite_float_arg,
    reject_non_finite,
    sanitize_non_finite,
)
from .state import (
    HydraState,
    PoisonedStateError,
    TaskState,
    plan_barrier_active,
    plan_deps_satisfied,
    plan_max_revisions,
    plan_revision_ceiling_reached,
)
from .telemetry import emit, trace_path

# ---------------------------------------------------------------------------
# `_cli_json_dumps` covers PRINTED command-result output ONLY -- CLI
# stdout/stderr is a machine boundary, uniformly, for that narrower set of
# call sites.
#
# Cross-vendor judge finding (REVISE round, HIGH): the previous version of
# this comment claimed "every `json.dumps(...)` call in this module" routes
# through this wrapper; that was false -- three sites (the backends.json
# export/setup and the ~/.claude.json rewrite in the gateway-* commands)
# write PERSISTED OPERATOR CONFIGURATION to disk, not a printed command
# result, and go through `dumps_strict` directly instead (see the
# PERSISTED-STATE rule in the module docstring, not this one). Sanitizing a
# config file the operator owns would silently rewrite their own data to
# `null`; refusing is correct there, exactly as for any other persisted
# state.
#
# For an actual PRINTED command-result dict (`{"ok": ...}`, `{"error": ...}`,
# a workflow/status/plan payload, ...): none is free-form human prose
# interpolated through `json.dumps` (prose in this file is printed directly,
# never JSON-encoded). `hydra status`/`hydra plan`/etc. output is documented
# and scripted against: a bare `NaN`/`Infinity` token would make that output
# invalid RFC 8259 JSON, so a conforming parser on the other end either
# rejects the whole document or misparses it -- the same defect the MCP
# gateway responses had (see `strict_json.dumps_tool_response_safe`), in
# different clothing.
#
# The policy for THAT set is therefore ONE policy: sanitize, never refuse. A
# CLI invocation must always print exactly ONE complete JSON document --
# raising instead (the `dumps_strict`/WRITE policy) would abort the process
# mid-print and hand the caller NO output and a traceback instead of a
# parseable result, which is strictly worse than one substituted field for a
# process whose entire contract with its caller is "print a JSON document
# and exit". This mirrors `dumps_tool_response_safe`'s reasoning for MCP tool
# responses exactly; `_cli_json_dumps` exists only because that helper's
# fixed `(payload, *, label)` signature does not forward the `indent=`/
# `default=` formatting kwargs this file's call sites already rely on --
# it is a local composition of the same exported primitives
# (`dumps_strict` / `sanitize_non_finite`), not a widened contract on either.
def _cli_json_dumps(payload: Any, **kwargs: Any) -> str:
    """``json.dumps`` for CLI stdout/stderr: sanitizes non-finite floats
    (and any other non-natively-JSON value) instead of raising, so a
    `hydra <cmd>` invocation always completes and prints one valid JSON
    document. See the module-level comment above for why this differs from
    `dumps_strict`'s refuse policy used on WRITE paths."""
    try:
        return dumps_strict(payload, label="cli_output", **kwargs)
    except (ValueError, TypeError, RecursionError):
        sanitized, fields = sanitize_non_finite(payload)
        if isinstance(sanitized, dict):
            marker_key = "_non_finite_fields_sanitized"
            if marker_key in sanitized:
                marker_key = "_hydra_non_finite_fields_sanitized"
                while marker_key in sanitized:
                    marker_key = f"_{marker_key}"
            sanitized = {**sanitized, marker_key: fields}
        else:
            sanitized = {
                "_value": sanitized,
                "_non_finite_fields_sanitized": fields,
            }
        return json.dumps(sanitized, **kwargs)


# ---------------------------------------------------------------------------
# MU1: MCP probe table for `hydra doctor`.
# Hoisted to module scope so tests can introspect and assert correct names.
# Each entry: server_key -> (tool_name, tool_args).
# Reachability semantics: the probe tools are real, zero-arg, and succeed on a
# healthy server, so reachable = call_mcp returns a {"status": "done"} envelope.
# A down/unregistered server surfaces as a {"status": "failed"} envelope (NOT a
# raised exception — call_mcp catches transport failures), so a bare
# `isinstance(res, dict)` check would false-positive "reachable". See _cmd_doctor.
# pp_harness:   budget_status (cheap read; returns budget rows)
# hydra_memory: hydra-mem.ping (no-arg liveness probe with DB check)
# Other pings:  leave as-is (consistent *.ping convention on those servers)
# ---------------------------------------------------------------------------
_DOCTOR_MCP_PROBES: dict[str, tuple[str, dict]] = {
    "pp_harness":      ("budget_status", {}),
    "hydra_memory":    ("hydra-mem.ping", {}),
    "executive_suite": ("es.ping", {}),
    "rlm_creative":    ("rlm.ping", {}),
    "senate":          ("senate.ping", {}),
}

# ---------------------------------------------------------------------------
# Supervisor symbols exposed at module scope for test patchability.
# unittest.mock.patch("hydra_core.cli.build_supervisor", ...) requires these
# names to live in the module's __dict__.  The try/except degrades gracefully
# when .supervisor can't be imported at module load time (e.g. missing deps).
# Commands that never need the supervisor (doctor, squads, verify) pay a small
# upfront import cost; the UserWarning from langchain_core is already silenced
# by the filter above, so hook stderr stays clean.
# ---------------------------------------------------------------------------
try:
    from .supervisor import build_supervisor, _PurePythonRunner
except Exception:  # noqa: BLE001 — degrade: missing deps at load time
    def build_supervisor(*args, **kwargs):  # type: ignore[misc]
        from .supervisor import build_supervisor as _real
        return _real(*args, **kwargs)

    class _PurePythonRunner:  # type: ignore[no-redef]  # noqa: N801
        """Sentinel; replaced when supervisor imports cleanly."""


class _NullDispatcher:
    """Inert dispatcher for the CLI smoke path. Real dispatchers come from
    the Claude Code plugin / MCP host.

    ``dry_run = True`` is an explicit, load-bearing marker: this dispatcher
    performs no I/O of any kind. Every "call" below is a fabricated stub
    response, and callers that key off this marker (e.g.
    ``EightsAttestor.replay_pending`` / ``replay_pending_async`` — see
    docs/audits/EIGHTS-RECORD-OUTCOME-RCA-2026-09-16.md §7 path S) treat it as
    a signal to skip any operation whose only purpose is to talk to a real
    daemon, such as draining the eights spool. Do not remove this attribute
    without auditing every ``getattr(dispatcher, "dry_run", False)`` call
    site."""
    dry_run = True

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


def _hitl_backlog_line(rows: list | None, threshold: int) -> str:
    """Render the doctor's TheEights HITL backlog line (E2-17).

    Pure so it is testable without a daemon. ``rows=None`` means the daemon
    did not answer — a WARN, never a FAIL (doctor degrades open on eights).
    """
    if rows is None:
        return ("WARN: eights HITL backlog unknown — daemon did not answer "
                "hitl.list (pending gates may be accumulating)")
    if len(rows) > threshold:
        return (f"WARN: eights HITL pending={len(rows)} exceeds "
                f"threshold={threshold} — run `hydra eights-hitl-reconcile` "
                "to see which are zombies, then --apply to close them")
    return f"OK:   eights HITL pending={len(rows)} (threshold={threshold})"


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

    # --- RA-7: eights spool depth check (cheap directory count) -----------
    # Runs in both quick and full mode (counting files is fast).  A large spool
    # means the daemon has been offline for a while; operator should run
    # `hydra eights-drain` or check the daemon health.
    try:
        from .eights.pending_spool import PendingSpool
        _spool_warn_thresh = int(
            os.environ.get("HYDRA_EIGHTS_SPOOL_WARN", "100")
        )
        _spool_root, _dead_root = _resolve_eights_spool_roots()
        _spool = PendingSpool(root=_spool_root, dead_letter_root=_dead_root)
        _spool_depth = _spool.count()
        if _spool_depth > _spool_warn_thresh:
            print(
                f"WARN: eights spool depth={_spool_depth} exceeds "
                f"threshold={_spool_warn_thresh} — "
                "run `hydra eights-drain` or check eights daemon health"
            )
        else:
            print(f"OK:   eights spool  depth={_spool_depth} "
                  f"(threshold={_spool_warn_thresh})")
        # E2-3: dead letters are records that were never delivered. Depth > 0
        # is always operator-actionable, independent of the pending depth.
        _dead_depth = _spool.dead_letter_count()
        if _dead_depth > 0:
            print(
                f"WARN: eights dead-letter depth={_dead_depth} — triage before "
                "replay (see docs/audits/EIGHTS-RECORD-OUTCOME-RCA-2026-09-16.md "
                "§7 path T); an unfiltered bulk replay is NOT recommended"
            )
        else:
            print(f"OK:   eights dead-letter  depth={_dead_depth}")
    except Exception as _spool_exc:
        print(f"WARN: eights spool check — {_spool_exc}")

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
    dispatcher = MCPStdioDispatcher(project)
    for server, (tool, tool_args) in _DOCTOR_MCP_PROBES.items():
        if server not in servers:
            print(f"WARN: {server} not registered at user scope (~/.claude.json)")
            continue
        try:
            res = dispatcher.call_mcp(server, tool, tool_args)
        except Exception as e:
            # call_mcp is not expected to raise (it catches transport failures
            # and returns a failed envelope), but guard anyway.
            print(f"WARN: {server} unreachable — {type(e).__name__}: {e}")
            continue
        # call_mcp normalizes every response to an envelope: a successful call
        # is {"status": "done", ...}; a transport/connect failure, a timeout, or
        # an MCP tool error is {"status": "failed", ...}. It returns a dict in
        # BOTH cases, so `isinstance(res, dict)` cannot distinguish reachable
        # from down — a genuinely-unreachable server would be mislabeled OK.
        # The probes above use real, zero-arg tools that succeed on a healthy
        # server, so status == "done" is the correct reachability signal.
        status = res.get("status") if isinstance(res, dict) else None
        if status == "done":
            print(f"OK:   {server} reachable")
        else:
            err = (res.get("error", "(no error field)")
                   if isinstance(res, dict) else f"non-dict {type(res).__name__}")
            print(f"WARN: {server} unreachable — {err}")

    # --- E2-17: TheEights HITL backlog (consumer hydra) ---------------------
    # Separate from the spool-depth line above: the spool counts calls Hydra
    # could not deliver, this counts gates the ledger still holds open.
    try:
        _hitl_thresh = int(os.environ.get("HYDRA_HITL_PENDING_THRESHOLD", "25"))
    except ValueError:
        _hitl_thresh = 25
    if "eights" in servers:
        try:
            from .eights.attestation import EightsAttestor
            _hitl_rows = EightsAttestor(dispatcher=dispatcher).hitl_list()
        except Exception as _hitl_exc:  # noqa: BLE001 — never crash doctor
            print(_hitl_backlog_line(None, _hitl_thresh)
                  + f" ({type(_hitl_exc).__name__}: {_hitl_exc})")
        else:
            print(_hitl_backlog_line(_hitl_rows, _hitl_thresh))
    else:
        print("WARN: eights not registered — skipping HITL backlog check")

    # --- RA-6: AgentSmith venom cross-check / smith→hydra back-channel ------
    # Informational only: surfaces the back-channel state (rationale field) but
    # NEVER fails the doctor — a missing or degraded AgentSmith is a WARN.
    # The "hydra-mcp-unavailable" rationale is the known live-deployment state
    # and must surface as a WARN (not an error) so operators know it is expected.
    if "agentsmith" in servers:
        try:
            _vs_res = dispatcher.call_mcp(
                "agentsmith",
                "agentsmith.hydra.venom_cross_check",
                {"capability": "ping"},
            )
            _vs_status = _vs_res.get("status") if isinstance(_vs_res, dict) else None
            if _vs_status == "done":
                # Unwrap result envelope (agentsmith returns {"status":"done","result":{...}})
                _inner = _vs_res.get("result", _vs_res) if isinstance(_vs_res, dict) else {}
                _rationale = (
                    _inner.get("rationale")
                    if isinstance(_inner, dict)
                    else None
                )
                if _rationale == "hydra-mcp-unavailable":
                    print(
                        "WARN: agentsmith venom_cross_check — smith→hydra back-channel "
                        f"unavailable (rationale={_rationale!r}); "
                        "check that hydra_gateway is registered in ~/.hydra/backends.json"
                    )
                else:
                    print(
                        f"OK:   agentsmith venom_cross_check reachable "
                        f"(back-channel rationale={_rationale!r})"
                    )
            else:
                _vs_err = (
                    _vs_res.get("error", "(no error field)")
                    if isinstance(_vs_res, dict)
                    else f"non-dict {type(_vs_res).__name__}"
                )
                print(f"WARN: agentsmith venom_cross_check — {_vs_err}")
        except Exception as _vs_exc:  # noqa: BLE001 — venom probe must never crash doctor
            print(
                f"WARN: agentsmith venom_cross_check raised "
                f"{type(_vs_exc).__name__}: {_vs_exc}"
            )
    else:
        print(
            "WARN: agentsmith not registered — skipping venom.cross_check probe "
            "(back-channel state unknown)"
        )

    # --- RA-9: WS-AUTH operator key probe -----------------------------------
    # Check whether an operator key is provisioned for Xenia's WS-AUTH capability
    # enforcement (send_response / execute_approved). Never fail the doctor, never
    # print the key or its length. OK when HYDRA_OPERATOR_KEY is non-empty in
    # os.environ OR present under any backend spec's env block in
    # ~/.hydra/backends.json (read-only parse, fail-soft on IO/parse errors).
    #
    # E2-5: a backends.json value may now be a "${HYDRA_OPERATOR_KEY}"
    # reference rather than the key itself. A reference that resolves reports
    # source=env (the key lives in the environment, not the file); one that does
    # not resolve leaves the probe unprovisioned. Only a literal value still
    # reports source=backends.json, and any inline 64-hex-shaped value in the
    # file draws a WARN naming its backend and key — never its content.
    try:
        from .backends_env import (
            expand_env_refs, has_env_ref, looks_like_inline_secret,
        )

        _wsauth_src: str | None = None
        # E2-5: names of backends whose env holds an inline-secret-shaped value.
        # Shape check only — no value is ever read into the message.
        _inline_hits: list[str] = []
        if os.environ.get("HYDRA_OPERATOR_KEY"):
            _wsauth_src = "env"
        try:
            _bj_path = Path.home() / ".hydra" / "backends.json"
            if _bj_path.exists():
                _bj = json.loads(_bj_path.read_text(encoding="utf-8"))
                if isinstance(_bj, dict):
                    for _bj_name, _bj_spec in _bj.items():
                        if not (
                            isinstance(_bj_spec, dict)
                            and isinstance(_bj_spec.get("env"), dict)
                        ):
                            continue
                        for _ek, _ev in _bj_spec["env"].items():
                            if looks_like_inline_secret(_ev):
                                _inline_hits.append(f"{_bj_name}.env.{_ek}")
                        _raw = _bj_spec["env"].get("HYDRA_OPERATOR_KEY")
                        if _wsauth_src or not _raw:
                            continue
                        if has_env_ref(_raw):
                            # E2-5: a ${VAR} reference — the key really lives in
                            # the environment, so report source=env when it
                            # resolves, and stay unprovisioned when it does not.
                            if expand_env_refs(_raw):
                                _wsauth_src = "env"
                        else:
                            _wsauth_src = "backends.json"
        except Exception:  # noqa: BLE001 — fail-soft on parse / IO errors
            pass
        if _inline_hits:
            print(
                "WARN: backends.json holds inline secret-shaped value(s) at "
                f"{', '.join(sorted(_inline_hits))} — replace with a "
                "${VAR} reference and export the variable instead "
                "(see docs/MCP_SETUP.md)"
            )
        if _wsauth_src:
            print(f"OK:   WS-AUTH operator key configured (source={_wsauth_src})")
        else:
            print(
                "WARN: WS-AUTH operator key unprovisioned - "
                "xenia send_response/execute_approved will reject all tokens (fail-closed)"
            )
    except Exception as _wsauth_exc:  # noqa: BLE001 — never crash doctor
        print(
            f"WARN: WS-AUTH key probe error "
            f"({type(_wsauth_exc).__name__}: {_wsauth_exc}) — key status unknown"
        )

    return 0 if fail_count == 0 else 1


def _cmd_verify(args) -> int:
    from .immortal_head import load_constitution

    project = Path(args.project) if args.project else None
    try:
        snap = load_constitution(project)
    except FileNotFoundError as e:
        print(f"FAIL: {e}", file=sys.stderr)
        return 1
    print(_cli_json_dumps({
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
        print(_cli_json_dumps({"error": f"invalid cell {args.cell!r}",
                          "valid": list(ALL_CELLS)}), file=sys.stderr)
        return 1
    rows = query_by_cell(args.cell, limit=int(args.limit),
                         workflow_id=args.workflow_id)
    print(_cli_json_dumps({"cell": args.cell, "count": len(rows), "rows": rows},
                     default=str, indent=2))
    return 0


def _cmd_memory_tag(args) -> int:
    from .memory import tag_episodic

    cells = [c.strip() for c in (args.cells or "").split(",") if c.strip()]
    if not cells:
        print(_cli_json_dumps({"error": "no cells supplied"}), file=sys.stderr)
        return 1
    merged = tag_episodic(args.key, cells, replace=bool(args.replace))
    # MU11: tag_episodic returns an error dict when the key does not exist.
    if isinstance(merged, dict) and "error" in merged:
        print(_cli_json_dumps(merged, indent=2))
        return 1
    print(_cli_json_dumps({"key": args.key, "cells": merged}, indent=2))
    return 0


def _cmd_squads(args) -> int:
    packs = discover_squads(Path(args.project) if args.project else None)
    print(_cli_json_dumps({
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


def _cmd_repo(args) -> int:
    """`hydra repo register|unregister|list` — WS1-C self-service admin of
    ~/.hydra/repos.json. CLI-only by design; see repo_registry.register_repo."""
    from .repo_registry import (
        list_registered_repos,
        register_repo,
        unregister_repo,
    )
    if args.repocmd == "register":
        try:
            result = register_repo(
                args.repo_id, args.path, force=args.force, init=args.init,
            )
        except (ValueError, TimeoutError) as exc:
            print(_cli_json_dumps({"ok": False, "error": str(exc)}), file=sys.stderr)
            return 1
        print(_cli_json_dumps({"ok": True, **result}, indent=2))
        return 0
    if args.repocmd == "unregister":
        try:
            result = unregister_repo(args.repo_id)
        except (ValueError, TimeoutError) as exc:
            print(_cli_json_dumps({"ok": False, "error": str(exc)}), file=sys.stderr)
            return 1
        print(_cli_json_dumps({"ok": True, **result}, indent=2))
        return 0
    if args.repocmd == "list":
        print(_cli_json_dumps({"ok": True, "repos": list_registered_repos()}, indent=2))
        return 0
    print(_cli_json_dumps({"ok": False, "error": f"unknown repocmd {args.repocmd!r}"}), file=sys.stderr)
    return 1


def _cmd_run(args) -> int:
    project = Path(args.project) if args.project else Path.cwd()
    # WS1 retry (finding 1): --repo and --repos are mutually exclusive on the
    # CLI transport too. The MCP transport already rejects this combination
    # (mcp_servers/hydra_control/server.py: _extract_repo_params), but the CLI
    # path pre-seeds target_repo_id/target_repo_ids directly onto HydraState
    # (see WS1-B below) — that bypasses node_intake's goal-text ambiguity
    # guard entirely (it only compares ids parsed OUT of root_goal), so an
    # operator passing both flags here would otherwise silently pick whichever
    # field a downstream branch happens to prefer. Fail fast instead.
    if getattr(args, "repo", None) and getattr(args, "repos", None):
        print(
            _cli_json_dumps({"error": "--repo and --repos are mutually exclusive"}),
            file=sys.stderr,
        )
        return 1
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
    # WS1-B: --repo/--repos/--subdir are structured argparse flags, so they are
    # pre-seeded directly onto HydraState (target_repo_id / target_repo_ids /
    # target_repo_subpath) rather than folded into the goal text. root_goal
    # stays exactly what the operator typed. node_intake validates whatever
    # arrives here (goal-text token OR pre-seeded field) through the same
    # allow-list check and HITL shape either way. parse_repo_arg /
    # parse_repos_arg are unchanged and remain solely for operator-typed goal
    # text (e.g. a goal pasted into a chat UI with no separate --repo flag).
    _goal = args.goal
    initial = HydraState(workflow_id=workflow_id, root_goal=_goal)
    if getattr(args, "repo", None):
        initial.target_repo_id = str(args.repo).strip().lower()
        initial.target_repo_source = "explicit_param"
    if getattr(args, "repos", None):
        initial.target_repo_ids = [
            p.strip().lower() for p in str(args.repos).split(",") if p.strip()
        ]
        initial.target_repo_source = "explicit_param"
    if getattr(args, "subdir", None):
        from .repo_registry import normalize_repo_subpath
        initial.target_repo_subpath = normalize_repo_subpath(str(args.subdir))
    if args.squad:
        initial.selected_squads = [s.strip() for s in args.squad.split(",") if s.strip()]
    # --budget: set the workflow budget cap (the genuinely-missing wire — the
    # slash commands advertise it but the CLI run parser never accepted it).
    if getattr(args, "budget", None) is not None:
        initial.budget.budget_usd = float(args.budget)
    # --risk: recorded on the start event for audit AND (P3) pre-seeded onto
    # HydraState.risk_tolerance, where node_planner's plan-rigor triage reads it.
    _risk = getattr(args, "risk", None)
    if _risk:
        initial.risk_tolerance = _risk
    critique_client = None
    if args.live:
        from .dispatcher import MCPStdioDispatcher
        from .judge import MCPCritiqueClient
        dispatcher = MCPStdioDispatcher(project, verbose=args.verbose)
        # Reuse the same dispatcher for cross-vendor judge calls; pp_codex /
        # pp_agy servers must be registered at user scope (~/.claude.json).
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
        # P5a: `--live` is the detached path (hydra.workflow.launch detaches
        # exactly this); `--no-checkpoint` is the pure-python runner. Neither
        # has an attended host cursor for the claude-native planning squad to
        # defer to, so both must force plan_rigor to "trivial" or a plan-phase
        # run would seed a planning task and park forever.
        force_trivial_plan_rigor=bool(args.live) or bool(getattr(args, "no_checkpoint", False)),
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
    print(_cli_json_dumps({
        "workflow_id": str(workflow_id),
        "phase": getattr(final, "phase", "?"),
        "selected_squads": getattr(final, "selected_squads", []),
        "tasks": [{"squad": t.owner_squad, "status": t.status} for t in getattr(final, "tasks", [])],
        "trace": str(trace_path(project, workflow_id)),
    }, indent=2))
    return 0


def _build_resolved_target_view(state) -> dict | None:
    """WS1-E ergonomic: what engineering-target resolved, and from where.

    Surfaced on `hydra.workflow.plan` / `hydra.workflow.step` output (and
    rendered on the approval HITL) so an operator sees the target BEFORE any
    work happens, instead of discovering a wrong-repo diff at merge time.
    Returns None when nothing has resolved (the missing-target HITL in
    node_planner covers that case for engineering dispatch)."""
    if getattr(state, "target_repo_ids", None):
        return {
            "mode": "fleet",
            "repo_ids": list(state.target_repo_ids),
            "source": getattr(state, "target_repo_source", None) or "unknown",
        }
    if getattr(state, "target_repo_id", None):
        return {
            "mode": "single",
            "repo_id": state.target_repo_id,
            "subpath": getattr(state, "target_repo_subpath", None),
            "source": getattr(state, "target_repo_source", None) or "unknown",
        }
    return None


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

    # WS1 retry-2 (finding B): --repo and --repos are mutually exclusive here
    # too, mirroring _cmd_run's guard above. Without it, `hydra plan --repo X
    # --repos Y,Z` pre-seeds target_repo_id AND target_repo_ids independently
    # (see WS1-B below) and node_intake's goal-text ambiguity guard never
    # fires for this structured path (it only compares ids parsed OUT of
    # root_goal, both empty here) -- so the conflict is silently resolved by
    # whichever branch happens to run instead of being rejected.
    if getattr(args, "repo", None) and getattr(args, "repos", None):
        print(
            _cli_json_dumps({"error": "--repo and --repos are mutually exclusive"}),
            file=sys.stderr,
        )
        return 1

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

    # WS1-B: structured flags pre-seed HydraState directly; see _cmd_run for
    # the full rationale. root_goal stays exactly what the operator typed.
    _goal = args.goal
    initial = HydraState(workflow_id=workflow_id, root_goal=_goal)
    if getattr(args, "repo", None):
        initial.target_repo_id = str(args.repo).strip().lower()
        initial.target_repo_source = "explicit_param"
    if getattr(args, "repos", None):
        initial.target_repo_ids = [
            p.strip().lower() for p in str(args.repos).split(",") if p.strip()
        ]
        initial.target_repo_source = "explicit_param"
    if getattr(args, "subdir", None):
        from .repo_registry import normalize_repo_subpath
        initial.target_repo_subpath = normalize_repo_subpath(str(args.subdir))
    if args.squad:
        initial.selected_squads = [s.strip() for s in args.squad.split(",") if s.strip()]
    if getattr(args, "budget", None) is not None:
        initial.budget.budget_usd = float(args.budget)
    if getattr(args, "risk", None):
        initial.risk_tolerance = args.risk
    # --rigor: operator override of node_planner's computed plan_rigor, pre-
    # seeded onto state the way --squad pre-seeds selected_squads. node_planner
    # still computes the auto-triage value (to detect + record a downgrade)
    # but the override wins and plan_rigor_source becomes "operator_flag".
    if getattr(args, "rigor", None):
        initial.plan_rigor_override = args.rigor

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
        print(_cli_json_dumps({
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
    print(_cli_json_dumps({
        "ok": True,
        "workflow_id": str(workflow_id),
        "phase": getattr(final, "phase", "?"),
        "selected_squads": list(getattr(final, "selected_squads", [])),
        "requires_human_approval": bool(getattr(final, "requires_human_approval", False)),
        "tasks": [_task_view(t) for t in getattr(final, "tasks", [])],
        "pending_hitl": pending if isinstance(pending, dict) else None,
        "resolved_target": _build_resolved_target_view(final),
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


def _reconcile_attestor(project: Path):
    """Build an EightsAttestor bound to the live MCP dispatcher (E2-17).

    Its own seam so terminal-transition reconciliation is stubbable in tests
    without spawning the eights daemon.
    """
    from .dispatcher import MCPStdioDispatcher
    from .eights.attestation import EightsAttestor
    return EightsAttestor(dispatcher=MCPStdioDispatcher(project))


def _make_phase_lookup(project: Path) -> Callable[[str], str | None]:
    """Return ``workflow_id -> phase | None`` over Hydra's checkpoint store.

    ``None`` means Hydra has no state for that workflow — an orphan row from
    a wiped checkpoint db or a foreign run id. Results are memoized because
    a reconcile sweep sees many rows per workflow.
    """
    from .supervisor import build_supervisor, _PurePythonRunner
    sup = build_supervisor(project_root=project, dispatcher=_NullDispatcher())
    if isinstance(sup, _PurePythonRunner):
        # No checkpointer → every workflow is "unknown"; the reconcile caller
        # still resolves those rows (they can never be advanced again).
        return lambda _wf: None

    cache: dict[str, str | None] = {}

    def _lookup(workflow_id: str) -> str | None:
        if workflow_id in cache:
            return cache[workflow_id]
        phase: str | None = None
        try:
            snap = sup.get_state({"configurable": {"thread_id": workflow_id}})
            if snap is not None and snap.values:
                phase = snap.values.get("phase")
        except Exception:  # noqa: BLE001 — one bad thread never aborts a sweep
            phase = None
        cache[workflow_id] = phase
        return phase

    return _lookup


def _resolve_eights_hitl_for_workflow(
    project: Path, workflow_id: str, *, note: str, decision: str = "rejected",
    gate_node: str | None = None, dispatcher=None,
) -> dict:
    """Close a workflow's pending TheEights HITL rows. Never raises (E2-17).

    ``dispatcher`` reuses a caller's already-built transport instead of
    spawning a second eights connection. A caller holding a null dispatcher
    (``hydra resume`` without ``--live``) therefore no-ops here rather than
    starting the daemon mid-resume; those rows are swept by
    ``hydra eights-hitl-reconcile`` / ``hydra reap --apply`` instead.
    """
    try:
        from .eights.hitl_reconcile import resolve_for_workflow
        if dispatcher is not None:
            from .eights.attestation import EightsAttestor
            attestor = EightsAttestor(dispatcher=dispatcher, workflow_id=workflow_id)
        else:
            attestor = _reconcile_attestor(project)
        return resolve_for_workflow(
            attestor, workflow_id,
            note=note, decision=decision, gate_node=gate_node,
        )
    except Exception as exc:  # noqa: BLE001 — reconciliation is never a gate
        return {"pending": 0, "resolved": 0, "failed": 0, "unavailable": True,
                "error": f"{type(exc).__name__}: {exc}"}


def _build_gate_only_eights_client(project: Path, wf: str):
    """Factory for the attended gate-only resume route's narrow live
    TheEights client (operator decision 2). A DEDICATED minimal client, not
    `EightsAttestor`: `EightsAttestor.hitl_resolve` routes through `_call`,
    which spools `eights.governance.hitl.resolve` on ANY failure
    (`_SPOOLABLE_TOOLS` in hydra_core/eights/attestation.py) so a later
    `replay_pending` can retry it — exactly the durability behaviour this
    route forbids (a failed resolve here must report `unavailable`, never
    queue a retry). `GateOnlyHitlClient` calls `dispatcher.call_mcp` directly
    and never imports `PendingSpool`/`replay_pending`/`replay_pending_async`
    — there is nothing in it to spool with.

    The dispatcher itself is a real, live `MCPStdioDispatcher`: it reads the
    SAME backend registry (`~/.hydra/backends.json` / `HYDRA_BACKENDS`) and
    `.mcp.json` every other Hydra dispatcher reads (`hydra_core.dispatcher.
    _load_mcp_config`), and its `call_mcp` refuses to open a stdio session at
    all when `HYDRA_TEST_NO_DAEMONS=1` (the hermetic suite's conftest sets
    this for every test) — see `hydra_core.dispatcher.daemons_disabled()`,
    checked at the very top of `call_mcp` before any subprocess work. So even
    a test that does NOT inject a stub client here can never fork a real
    `node TheEights/daemon/dist/index.js` child.

    Tests inject a stub in-process by monkeypatching this factory directly
    (`hydra_core.cli._build_gate_only_eights_client`) rather than touching
    global env, so "reachable"/"unreachable" TheEights can be simulated
    deterministically without relying on the daemon kill-switch.
    """
    from .dispatcher import MCPStdioDispatcher
    from .eights.attestation import GateOnlyHitlClient
    dispatcher = MCPStdioDispatcher(project, verbose=False)
    return GateOnlyHitlClient(dispatcher, workflow_id=wf)


# Cross-vendor finding 1a (HIGH): the bounded inner deadline for the whole
# gate-only TheEights round trip (connect + list + resolve). This is
# DELIBERATELY far below the transport's own worst-case connect budget (3
# attempts x 2 x HYDRA_DISPATCH_CONNECT_TIMEOUT_S=20s = up to 120s, see
# MCPStdioDispatcher._get_or_connect_pooled_session) and below the tool-call
# timeout (HYDRA_DISPATCH_TOOL_TIMEOUT_S=120s default) -- the whole point of
# this wrapper is to never let the transport's own timeouts govern how long
# a gate-only resume can block. HYDRA_RESUME_TIMEOUT_S (mcp_servers/
# hydra_control/server.py, default 45s) is the OUTER hard kill on the whole
# `hydra resume --gate-only` child process; this inner budget must clear
# with margin: 8s (inner) + up to ~1.5s (the deadline-only best-effort
# session close, `_best_effort_close_gate_only_dispatcher`) + ~1s (identity
# check, checkpoint patch, spool prune, JSON serialize -- no other live I/O
# on this route) + margin (~33.5s) < 45s (outer).
_GATE_ONLY_EIGHTS_TIMEOUT_S = float(
    os.environ.get("HYDRA_GATE_ONLY_EIGHTS_TIMEOUT_S", "8"))


def _resolve_eights_hitl_gate_only(
    project: Path, workflow_id: str, *, note: str, decision: str,
    gate_node: str | None = None, reconcile: bool = False,
    client: Any = None,
) -> dict:
    """Operator decision 2 (RESOLVE-GATE-ONLY follow-up): on the attended
    gate-only resume route, resolve TheEights' matching pending HITL ticket
    NOW, with exactly ONE narrow live round trip: list the workflow's
    pending `hydra_gate` tickets, then resolve the one(s) matching this
    gate. Never constructs a supervisor or dispatch, never calls
    `replay_pending`/`replay_pending_async`/any spool drain, and never
    spools anything on failure (see `_build_gate_only_eights_client`'s
    docstring for why `GateOnlyHitlClient`, not `EightsAttestor`, is used
    here). Returns ``{"eights_resolution": "resolved"|"unavailable"|
    "none_pending", "reason": <str, when unavailable>, "resolved": <int,
    when resolved>}``; an unreachable daemon or a failed call reports
    "unavailable" with a reason and never claims "resolved".

    Callers use this DIRECTLY (never call it in-thread without a deadline):
    see `_resolve_eights_hitl_gate_only_bounded` below, which is the only
    call site this module uses on the live resume paths.

    ``reconcile=True`` (cross-vendor finding 1b): this is a RETRY on the
    no-pending-gate route, not the original resolution. TheEights showing no
    matching ticket here means the gate was ALREADY resolved (by this
    function's own earlier, possibly-abandoned attempt, or by a prior
    successful call) -- that is success, not failure, so it is reported as
    `"none_pending"` rather than `"unavailable"`. A genuinely unreachable
    daemon or a failed list/resolve call still reports `"unavailable"`
    either way.

    ``client``, when given (cross-vendor finding 2, RESOLVE-GATE-ONLY
    follow-up), is a pre-built client to use instead of constructing a new
    one here. `_resolve_eights_hitl_gate_only_bounded` builds the client on
    the CALLING thread (fast, no I/O) and passes it in so it retains a
    reference to it even if the worker thread running this function is
    later abandoned at the inner deadline -- otherwise there would be no
    way to attempt closing the dispatcher the abandoned attempt was using.
    """
    if client is None:
        try:
            client = _build_gate_only_eights_client(project, workflow_id)
        except Exception as exc:  # noqa: BLE001 — never block the gate-only return
            return {"eights_resolution": "unavailable",
                    "reason": f"client_build_failed: {type(exc).__name__}: {exc}"}
    try:
        rows = client.hitl_list()
    except Exception as exc:  # noqa: BLE001
        return {"eights_resolution": "unavailable",
                "reason": f"list_failed: {type(exc).__name__}: {exc}"}
    if rows is None:
        return {"eights_resolution": "unavailable", "reason": "eights_unreachable"}
    from .eights.hitl_reconcile import row_workflow_id, row_gate_node
    wf = str(workflow_id)
    matched = [r for r in rows if row_workflow_id(r) == wf]
    if gate_node:
        matched = [r for r in matched if row_gate_node(r) == gate_node]
    if not matched:
        if reconcile:
            return {"eights_resolution": "none_pending"}
        return {"eights_resolution": "unavailable", "reason": "no_matching_ticket"}
    resolved = 0
    last_reason = "resolve_failed"
    for row in matched:
        request_id = row.get("request_id")
        if not request_id:
            continue
        try:
            out = client.hitl_resolve(
                request_id=str(request_id), decision=decision, note=note)
        except Exception as exc:  # noqa: BLE001
            out = None
            last_reason = f"{type(exc).__name__}: {exc}"
        if out is not None:
            resolved += 1
    if resolved == 0:
        return {"eights_resolution": "unavailable", "reason": last_reason}
    return {"eights_resolution": "resolved", "resolved": resolved}


def _resolve_eights_hitl_gate_only_bounded(
    project: Path, workflow_id: str, *, note: str, decision: str,
    gate_node: str | None = None, reconcile: bool = False,
    timeout_s: float | None = None,
) -> dict:
    """Cross-vendor finding 1a: wall-clock bound around
    `_resolve_eights_hitl_gate_only`'s ENTIRE connect+list+resolve round
    trip, enforced independently of any timeout inside the MCP transport
    (dispatcher.py's connect/tool-call timeouts bound individual ops, not
    the retry loop around them -- see `_GATE_ONLY_EIGHTS_TIMEOUT_S` above
    for the arithmetic against the outer child deadline).

    Runs the resolution on a DAEMON thread and joins with `timeout_s`. If it
    has not finished by the deadline, returns `"unavailable"` (reason=
    "deadline") immediately WITHOUT waiting further -- the caller (this
    gate-only resume) proceeds and returns to the host on schedule. The
    abandoned thread cannot write any HYDRA state afterward: the gate-only
    route's only local state mutation (`sup.update_state`, the spool prune)
    already happened BEFORE this call runs (see the call sites), and this
    function's own thread touches nothing but TheEights' remote ledger over
    MCP -- there is no Hydra-local write left for it to race. Being a daemon
    thread also means the abandoned attempt can never keep the CLI's own
    process alive past this function's return: when `hydra resume
    --gate-only` finishes printing its JSON body and exits, Python's
    interpreter shutdown does not wait for daemon threads, so the process
    exits (and, on Windows, `subprocess.run`'s own outer timeout in
    mcp_servers/hydra_control/server.py additionally SIGKILLs/TerminateProcess
    it if it somehow didn't).

    Cross-vendor finding 2 (RESOLVE-GATE-ONLY follow-up): on the deadline
    path, this also makes a best-effort, time-bounded attempt to close the
    live MCP session the abandoned attempt was using (see
    `_best_effort_close_gate_only_dispatcher` below) so a slow-but-not-
    wedged TheEights daemon this call spawned does not leak past the
    resume. That close attempt is capped separately and can add up to
    roughly its own cap on top of `timeout_s` before this function returns
    -- see `_GATE_ONLY_EIGHTS_TIMEOUT_S`'s arithmetic comment above, which
    accounts for it.
    """
    if timeout_s is None:
        # Read the env var at CALL time (not at module import) so tests --
        # and operators -- can override it per-invocation via
        # monkeypatch/env without needing to reload this module.
        timeout_s = float(os.environ.get(
            "HYDRA_GATE_ONLY_EIGHTS_TIMEOUT_S", str(_GATE_ONLY_EIGHTS_TIMEOUT_S)))
    # Built on THIS (calling) thread -- fast, no I/O (see the docstring on
    # `client=` above) -- so a reference to it survives even if the worker
    # below is abandoned at the deadline.
    try:
        client = _build_gate_only_eights_client(project, workflow_id)
    except Exception as exc:  # noqa: BLE001 — never block the gate-only return
        return {"eights_resolution": "unavailable",
                "reason": f"client_build_failed: {type(exc).__name__}: {exc}"}
    _box: list[dict] = []

    def _worker() -> None:
        try:
            _box.append(_resolve_eights_hitl_gate_only(
                project, workflow_id, note=note, decision=decision,
                gate_node=gate_node, reconcile=reconcile, client=client,
            ))
        except Exception as exc:  # noqa: BLE001 — never raise off-thread
            _box.append({"eights_resolution": "unavailable",
                        "reason": f"{type(exc).__name__}: {exc}"})

    thread = threading.Thread(target=_worker, daemon=True)
    thread.start()
    thread.join(timeout_s)
    if _box:
        return _box[0]
    _best_effort_close_gate_only_dispatcher(client)
    return {"eights_resolution": "unavailable", "reason": "deadline"}


def _best_effort_close_gate_only_dispatcher(
    client: Any, *, timeout_s: float = 1.5,
) -> None:
    """Cross-vendor finding 2 (RESOLVE-GATE-ONLY follow-up): when the
    gate-only TheEights call's inner deadline fires
    (`_resolve_eights_hitl_gate_only_bounded`), the abandoned worker thread
    can still hold a live MCP stdio session to a TheEights daemon process it
    spawned. `MCPStdioDispatcher.close_pooled_sessions` (hydra_core/
    dispatcher.py) is a best-effort close of that session; this wrapper
    runs it on its OWN daemon thread and joins with `timeout_s`, so a lock
    held by the still-running abandoned attempt (see
    `close_pooled_sessions`'s own docstring) can never block the gate-only
    resume's return by more than `timeout_s` -- if the close hasn't
    finished by then, this simply returns anyway and leaves that thread to
    finish (or not) on its own, exactly like the abandoned resolution
    attempt itself.

    Never raises. A no-op when `client` has no `dispatcher` attribute (e.g.
    a test double) or `dispatcher` has no `close_pooled_sessions` method.

    KNOWN LIMITATION: even when the close succeeds, the underlying
    TheEights child process can still be alive afterward on Windows (see
    `close_pooled_sessions`'s docstring) -- this is a best-effort mitigation
    of session leakage, not a guarantee against an orphaned process. No
    process-tree sweep is attempted here (or anywhere in this fix) because
    a prior attempt at that elsewhere in this ecosystem had to be withdrawn:
    it could kill an unrelated process that later reused the same pid.
    """
    dispatcher = getattr(client, "dispatcher", None)
    close = getattr(dispatcher, "close_pooled_sessions", None)
    if not callable(close):
        return

    def _closer() -> None:
        try:
            close(timeout_s=timeout_s)
        except Exception:  # noqa: BLE001 — best-effort only
            pass

    closer_thread = threading.Thread(target=_closer, daemon=True)
    closer_thread.start()
    closer_thread.join(timeout_s)


def _eights_decision_for_history_entry(entry: dict | None) -> str:
    """Cross-vendor finding 1b: map a `hitl_history` entry back to the
    TheEights resolve `decision` it was originally recorded with (`resolved`
    key is `"resolution"`/`"option"` -- see the `resolution` dict built
    above). Mirrors `_terminal_resolution`'s reject-or-abort test at the
    original resolve call site so a retry reconciliation resolves the SAME
    decision the original attempt would have."""
    if not isinstance(entry, dict):
        return "approved"
    if entry.get("resolution") == "reject" or entry.get("option") == "abort":
        return "rejected"
    return "approved"


def _eights_resolution_fields(gate_only: bool, result: dict) -> dict:
    """Additive JSON fields for a gate-only response body (operator decision
    2): {} when not gate_only, otherwise the honest `eights_resolution`
    outcome from `_resolve_eights_hitl_gate_only` -- "resolved" or
    "unavailable" (never a hardcoded "deferred")."""
    if not gate_only:
        return {}
    out: dict = {"eights_resolution": result.get("eights_resolution", "unavailable")}
    if result.get("reason"):
        out["eights_resolution_reason"] = result["reason"]
    if result.get("resolved"):
        out["eights_resolved_count"] = result["resolved"]
    return out


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


def _plan_artifact_relpath(location: str | None) -> str | None:
    """Extract the repo-relative path from a `plan_artifact_location`
    MemoryRef key (``repo:artifact:<relpath>``, the shape
    `hydra_core.artifact_store.write_repo_artifact` returns). Returns None
    for any other shape (a checkpoint predating P2/P5b, an unset location,
    or a `--critique-ref` that is simply a plain file path rather than a
    MemoryRef key) -- callers treat that as "not a repo-artifact MemoryRef",
    never raise.
    """
    if not location or not location.startswith("repo:artifact:"):
        return None
    return location[len("repo:artifact:"):]


def _append_plan_governance_note(
    project: Path, wf: str, plan_artifact_location: str | None, note: str,
) -> None:
    """Best-effort: append ``note`` to the tracked plan artifact's Governance
    Notes section (see `hydra_core.plan_artifact.append_governance_note`).

    Fail-soft by design: a missing, unreadable, or unwritable plan artifact
    must never block the operator action that triggered this note (a
    force-dispatch past `plan_gate` has already proceeded regardless — see
    Task 1). A workflow whose plan was never actually committed to disk (or
    whose artifact write failed earlier) still gets a real `policy_override`
    trace event and `hitl_history` entry; only the artifact-side note is
    skipped.

    Fail-soft does NOT mean fail-silent (cross-vendor judge finding, P5c
    revise round): a `policy_override` event and a `plan_gate_bypassed`
    hitl_history entry both claim the bypass was recorded, but if THIS
    write fails, the artifact note never landed and nothing anywhere said
    so -- the audit trail implies a completeness it does not have. Emit
    `plan_governance_note_failed` on every failure path (containment
    refusal included) so a consumer tailing the trace can tell the
    difference between "no note was needed" (no `plan_artifact_location`,
    the common non-plan-gate case -- no event either way) and "a note was
    owed and silently did not happen".

    Read-before-write is validated through the SAME containment check
    `write_repo_artifact` itself uses
    (`hydra_core.artifact_store.resolve_repo_artifact_path`) -- this
    function used to build `Path(project) / relpath` and call `read_text()`
    on it directly, validating only on the LATER `write_repo_artifact` call,
    by which point the (potentially path-escaping) read had already
    happened. One containment check, shared with the write path, not a
    second hand-rolled one (see `_read_plan_critique`'s sibling comment).
    """
    relpath = _plan_artifact_relpath(plan_artifact_location)
    if relpath is None:
        return
    try:
        from .artifact_store import resolve_repo_artifact_path, write_repo_artifact
        from .plan_artifact import append_governance_note
        full = resolve_repo_artifact_path(project, relpath)
        existing = full.read_text(encoding="utf-8") if full.is_file() else ""
        updated = append_governance_note(existing, note)
        write_repo_artifact(project, relpath, updated)
    except Exception as exc:  # noqa: BLE001 — fail-soft (never block the resume), but say so
        try:
            emit(project, wf, "plan_governance_note_failed", {
                "plan_artifact_location": plan_artifact_location,
                "error": f"{type(exc).__name__}: {exc}",
            })
        except Exception:  # noqa: BLE001 — even the failure trace must never block the resume
            pass


class _PlanCritiqueError(ValueError):
    """Raised by `_read_plan_critique` for a `--critique-ref` that cannot be
    resolved to real critique text (missing --critique-ref, unreadable file,
    or empty content)."""


def _read_plan_critique(ref: str, project: Path) -> str:
    """Resolve `--critique-ref` (a file path or a `repo:artifact:<path>`
    MemoryRef key) to the operator's full, untruncated revision critique.

    P5c Task 2: the critique text reaches this CLI via `--critique-ref`,
    never via `--option`. `_OPTION_RE`
    (mcp_servers/hydra_control/server.py) caps `option` at 200 characters of
    ``[A-Za-z0-9 ,._-]`` — real prose critique (parentheses, colons,
    quotation marks, more than 200 characters) would be rejected or
    truncated by that boundary, which exists to guard a string that reaches
    a subprocess argv, not to carry free text. `critique_ref` is validated
    by the wider `_CRITIQUE_REF_RE` at the MCP boundary instead (still no
    shell metacharacters, no leading `-`); this function then reads the
    FULL file content it names, so punctuation and length survive intact.

    Containment, mirroring `hydra_core.artifact_store.write_repo_artifact`'s
    discipline for the READ side of this same feature: `critique_ref` is not
    operator-only, it is a parameter on the `hydra.workflow.resume` MCP verb
    (`_CRITIQUE_REF_RE` guards shell metacharacters and argv-flag confusion,
    never path containment — it deliberately allows `/`/`\\`/`:` so real
    paths pass), so an absolute path or a `..`-escaping relative path here is
    an arbitrary-file-read reachable by any caller of that verb, not just a
    human operator with their own filesystem access. The resolved candidate
    MUST land under the resolved project root — this also defeats a symlink
    that sits inside the project but points outside it, since resolving
    before comparing follows the symlink to its real target. Refusal is
    always `_PlanCritiqueError`, never a bare OSError/ValueError leaking the
    filesystem's own message.
    """
    ref = (ref or "").strip()
    if not ref:
        raise _PlanCritiqueError("empty --critique-ref")
    relpath = _plan_artifact_relpath(ref)
    if relpath is not None:
        raw_candidate = Path(project) / relpath
    else:
        raw_candidate = Path(ref)
        if not raw_candidate.is_absolute():
            raw_candidate = Path(project) / raw_candidate

    try:
        project_root = Path(project).resolve()
    except OSError as exc:
        raise _PlanCritiqueError(f"could not resolve project root: {exc}") from exc
    try:
        candidate = raw_candidate.resolve()
    except OSError as exc:
        raise _PlanCritiqueError(
            f"could not resolve --critique-ref {ref!r}: {exc}"
        ) from exc
    if not candidate.is_relative_to(project_root):
        raise _PlanCritiqueError(
            f"--critique-ref {ref!r} resolves outside the project root — refused"
        )

    try:
        text = candidate.read_text(encoding="utf-8")
    except OSError as exc:
        raise _PlanCritiqueError(
            f"could not read --critique-ref {ref!r}: {exc}"
        ) from exc
    text = text.strip()
    if not text:
        raise _PlanCritiqueError(f"--critique-ref {ref!r} is empty")
    return text


def _resolve_operator_capability_for_resume(
    args, wf: str, action: str, pending: dict | None, *, gate_only: bool,
) -> tuple[dict | None, str, dict | None]:
    """Mint + verify an operator capability for a resume action that is
    about to mutate checkpointed state.

    Returns ``(capability_patch, operator, refusal)``:
    - ``capability_patch`` is the minted token dict (or ``None`` if mint was
      skipped/failed and the caller is allowed to proceed anyway — the
      legacy non-gate_only warn-and-proceed posture).
    - ``operator`` is the resolved operator id string (``"unknown"`` when
      unidentified), for callers that embed it in an emitted event.
    - ``refusal`` is ``None`` when the caller may proceed, or a JSON-ready
      dict the caller must ``print(..., file=sys.stderr)`` and return 1 for.

    Single source of truth for identity verification, called from every
    place in `_cmd_resume_locked` that is about to run `sup.update_state`
    (cross-vendor finding 2): the bare-interrupt reject path AND the general
    per-action path below (widened from the historical
    `_MUTATING_RESUME_ACTIONS` allow-list to `action in
    _MUTATING_RESUME_ACTIONS or gate_only`, so `reject` — previously
    unchecked — is covered whenever `gate_only` is set). Operator decision B:
    under `gate_only`, an unknown or degraded operator identity refuses
    BEFORE any mutation; the legacy non-gate_only CLI path keeps the
    original warn-and-proceed posture (WS-AUTH run-A) unchanged.
    """
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
    _UNKNOWN_OPERATORS = {"", "unknown"}
    _force_degraded = _operator.strip() in _UNKNOWN_OPERATORS

    # Operator decision B (RESOLVE-GATE-ONLY, finding 2): on the attended
    # gate_only route, an unknown operator identity must REFUSE before
    # clearing the gate — never mint a degraded token and warn-proceed (the
    # run-A posture below, kept for the non-gate_only/legacy CLI path).
    # Nothing has been mutated yet at this point (no mint, no verify, no
    # state patch, no spool prune) — this is a pure refusal.
    if gate_only and _force_degraded:
        return None, _operator, {
            "ok": False,
            "error": "operator_identity_required",
            "workflow_id": wf,
            "action": action,
            "message": (
                "attended gate-only resume requires a known operator "
                "identity to mint a verifiable capability for a "
                "state-mutating action; set the HYDRA_OPERATOR_ID "
                "environment variable to identify the caller (there is no "
                "--operator CLI flag). Nothing was changed."
            ),
        }

    if _force_degraded:
        _log_cli.warning(
            "operator identity unknown for action=%r; capability degraded — "
            "set the HYDRA_OPERATOR_ID environment variable to a real "
            "operator id to issue a verifiable capability token",
            action,
        )
        # Use a sentinel actor_id for the degraded token payload so the wire
        # format is consistent; the sig.value=None marks it unusable.
        _operator = _operator or "unknown"

    operator_capability_patch: dict | None = None
    try:
        from .auth.capability import mint_for_approval
        if _force_degraded:
            # Force degraded by temporarily unsetting the key env var. We do
            # this in a narrow scope to avoid races; the key is restored
            # immediately after the call returns.
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
            if gate_only:
                # Decision B: a degraded token (missing HYDRA_OPERATOR_KEY)
                # refuses on the gate_only route exactly like an unknown
                # operator — no state has been touched yet (mint is pure).
                return None, _operator, {
                    "ok": False,
                    "error": "operator_identity_required",
                    "workflow_id": wf,
                    "action": action,
                    "message": (
                        "attended gate-only resume requires a verifiable "
                        "operator capability; the minted token is degraded "
                        "(no HYDRA_OPERATOR_KEY configured). Set "
                        "HYDRA_OPERATOR_ID and HYDRA_OPERATOR_KEY to "
                        "identify and authenticate the caller. Nothing "
                        "was changed."
                    ),
                }
            _log_cli.warning(
                "operator capability degraded (no HYDRA_OPERATOR_KEY); "
                "gated consumers will reject — set HYDRA_OPERATOR_KEY to enable "
                "cryptographic proof of approval"
            )
    except Exception as _cap_exc:  # noqa: BLE001 — never block an approval on mint failure
        if gate_only:
            # Decision B: mint failing outright means we cannot verify
            # operator identity at all — refuse rather than proceed without
            # a capability token.
            return None, _operator, {
                "ok": False,
                "error": "operator_identity_required",
                "workflow_id": wf,
                "action": action,
                "message": (
                    "attended gate-only resume requires a verifiable "
                    f"operator capability; mint failed ({type(_cap_exc).__name__}: "
                    f"{_cap_exc}). Set HYDRA_OPERATOR_ID and "
                    "HYDRA_OPERATOR_KEY to identify and authenticate the "
                    "caller. Nothing was changed."
                ),
            }
        _log_cli.warning(
            "mint_for_approval raised %s: %s — approval proceeds without capability token",
            type(_cap_exc).__name__, _cap_exc,
        )

    # M3: verify the just-minted capability before applying the patch.
    # Fail-closed on a tampered/invalid token; warn-and-continue on a
    # degraded token (no key or unknown operator — already warned at mint)
    # -- but ONLY on the legacy non-gate_only CLI path. Cross-vendor
    # finding 4: the substring-based degrade-and-proceed posture below is
    # NOT safe under gate_only, where operator decision B demands failing
    # CLOSED on any verification result whose `valid` is not exactly
    # `True`, and on any verifier exception -- a "degraded"/"no operator
    # key"/"no key" substring in the failure reason must not be treated as
    # a green light for an unauthenticated resume.
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
            if _m3_result.get("valid") is not True:
                _m3_reason = _m3_result.get("reason", "unknown")
                _m3_sig = (operator_capability_patch.get("sig") or {})
                _m3_is_degraded = (
                    _m3_sig.get("degraded") is True
                    or _m3_sig.get("value") is None
                    or "degraded" in _m3_reason
                    or "no operator key" in _m3_reason
                    or "no key" in _m3_reason
                )
                if gate_only:
                    # Decision B: fail closed, unconditionally -- no
                    # substring carve-out for "degraded"/"no operator
                    # key"/"no key" is allowed to proceed under gate_only.
                    # Nothing has been mutated yet. A previously-degraded
                    # reason now surfaces the identity-required shape (it
                    # used to warn-and-proceed, the gap this closes); a
                    # non-degraded reason keeps the pre-existing
                    # `capability_verify_failed` shape, which already
                    # refused on gate_only before this fix.
                    if _m3_is_degraded:
                        return None, _operator, {
                            "ok": False,
                            "error": "operator_identity_required",
                            "workflow_id": wf,
                            "action": action,
                            "message": (
                                "attended gate-only resume requires a "
                                "verifiable operator capability; "
                                f"verification failed ({_m3_reason}). Set "
                                "HYDRA_OPERATOR_ID and HYDRA_OPERATOR_KEY to "
                                "identify and authenticate the caller. "
                                "Nothing was changed."
                            ),
                        }
                    return None, _operator, {
                        "error": f"capability_verify_failed: {_m3_reason}",
                        "workflow_id": wf,
                    }
                # Legacy non-gate_only path: degrade-warn for cases where no
                # key was configured or the token is intentionally degraded
                # (foundation run posture); fail closed on anything else.
                if _m3_is_degraded:
                    _log_cli.warning(
                        "capability verify: degraded (%s) — approval proceeds "
                        "(set HYDRA_OPERATOR_KEY to enable cryptographic enforcement)",
                        _m3_reason,
                    )
                else:
                    # This refusal shape (bare "capability_verify_failed", no
                    # "ok"/"operator_identity_required" wrapper) predates
                    # gate_only and is unchanged for the legacy transport.
                    return None, _operator, {
                        "error": f"capability_verify_failed: {_m3_reason}",
                        "workflow_id": wf,
                    }
        except Exception as _m3_exc:  # noqa: BLE001
            if gate_only:
                # Decision B: a verifier exception under gate_only must
                # refuse, not warn-and-proceed -- an exception is not proof
                # of a valid capability.
                return None, _operator, {
                    "ok": False,
                    "error": "operator_identity_required",
                    "workflow_id": wf,
                    "action": action,
                    "message": (
                        "attended gate-only resume requires a verifiable "
                        "operator capability; verification raised "
                        f"{type(_m3_exc).__name__}: {_m3_exc}. Nothing was "
                        "changed."
                    ),
                }
            _log_cli.warning(
                "verify_operator_capability raised %s: %s — approval proceeds",
                type(_m3_exc).__name__, _m3_exc,
            )

    return operator_capability_patch, _operator, None


def _precheck_operator_identity_gate_only(args) -> dict | None:
    """Operator decision 1: on the attended gate-only resume route, verify a
    real, non-degraded operator identity is even POSSIBLE before the resume
    lock is acquired or any workflow state is touched — so an unauthenticated
    call writes NOTHING at all: no `.hydra/<workflow>/` directory (created by
    `_acquire_resume_lock`'s `lock_dir.mkdir(...)`, the very first side effect
    on the old path), no `resume.lock`, no checkpoint database (opened by
    `build_supervisor`), no telemetry.

    This is a PURE, state-free check (env/args only — no checkpoint read, no
    workflow lookup of any kind, since none exists yet at this point in
    `_cmd_resume`): the operator id must be known and non-empty, AND a
    signing key must be present so a later mint (`mint_for_approval`, which
    DOES need the loaded pending-gate state and so cannot run this early)
    could plausibly produce a non-degraded capability. This does not mint or
    verify anything itself — it only rules out the two conditions that would
    make the later mint degraded/refused regardless of which gate is
    eventually loaded. The real mint+verify
    (`_resolve_operator_capability_for_resume`, called from
    `_cmd_resume_locked` immediately after the checkpoint load) is UNCHANGED
    and still runs -- it binds the token to the actual pending gate, which
    this pre-lock check cannot see yet.

    Returns a refusal dict (caller prints it and returns 1) when identity is
    missing/unknown or no signing key is configured; ``None`` to proceed.
    Only applies to the gate-only route — the legacy non-gate_only CLI path
    keeps its original warn-and-proceed posture (WS-AUTH run-A), unchanged,
    and is not called here.
    """
    operator = (
        getattr(args, "operator", None)
        or os.environ.get("HYDRA_OPERATOR_ID", "")
        or ""
    ).strip()
    if not operator or operator == "unknown":
        return {
            "ok": False,
            "error": "operator_identity_required",
            "message": (
                "attended gate-only resume requires a known operator "
                "identity BEFORE the resume lock is acquired; set the "
                "HYDRA_OPERATOR_ID environment variable to identify the "
                "caller (there is no --operator CLI flag). Nothing was "
                "created or changed."
            ),
        }
    if not os.environ.get("HYDRA_OPERATOR_KEY"):
        return {
            "ok": False,
            "error": "operator_identity_required",
            "message": (
                "attended gate-only resume requires a signing key to mint a "
                "verifiable operator capability; HYDRA_OPERATOR_KEY is not "
                "set. Nothing was created or changed."
            ),
        }
    return None


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

    # Cross-vendor finding 2 (HIGH): recover-stalled-stage is refused
    # unconditionally on the gate-only route (`_cmd_resume_locked` already
    # does this below, at ~1732, as defence in depth for direct callers of
    # that function) -- but that refusal used to run AFTER
    # `_acquire_resume_lock` had already created `.hydra/<workflow>/` and
    # written `resume.lock` (its `lock_dir.mkdir(...)` is the very first
    # side effect on this route). A gate-only route that promises "never
    # writes" for a refused action must not write a lock file first. Refuse
    # HERE, before the lock is even attempted, and before the pre-lock
    # identity precheck below (this action is refused regardless of who is
    # asking, not based on identity -- same reasoning the precheck-skip
    # comment below already documents).
    if (bool(getattr(args, "gate_only", False))
            and action == "recover-stalled-stage"):
        print(_cli_json_dumps({
            "ok": False,
            "error": "recovery_is_live_operation",
            "workflow_id": wf,
            "action": action,
            "message": (
                "recover-stalled-stage can replay a pp verdict and run "
                "live squad/engineering work (smoke, finalize, merge); "
                "it is refused on the attended gate-only resume route. "
                "Use the detached CLI (`hydra resume --live --action "
                "recover-stalled-stage --option <run_id>`, requires "
                "HYDRA_ALLOW_DETACHED=1) to run this recovery. Nothing "
                "was created or changed."
            ),
        }), file=sys.stderr)
        return 1

    # Operator decision 1: on the gate-only route, refuse an unauthenticated
    # caller BEFORE the resume lock exists and BEFORE build_supervisor opens
    # the checkpoint database -- this check needs no workflow state (it runs
    # ahead of the lock/checkpoint on purpose) so it can sit here, first.
    #
    # Scoped to every action EXCEPT recover-stalled-stage: that action is
    # refused unconditionally under gate_only regardless of who is asking
    # (cross-vendor finding 1 -- it is a LIVE operation, the opposite of what
    # gate-only promises, and `_cmd_resume_locked` already refuses it before
    # validating --option or looking for a cursor file). Requiring identity
    # first would just replace one pre-lock, no-state-touched refusal with
    # another, more specific one -- and would mask
    # `recovery_is_live_operation` behind `operator_identity_required` for an
    # action that was never going to run regardless of identity.
    if (bool(getattr(args, "gate_only", False))
            and action != "recover-stalled-stage"):
        _pre_refusal = _precheck_operator_identity_gate_only(args)
        if _pre_refusal is not None:
            print(_cli_json_dumps({
                **_pre_refusal, "workflow_id": wf, "action": action,
            }), file=sys.stderr)
            return 1

    # Atomic claim BEFORE reading gate state (claim-then-check): the loser of
    # a concurrent double-resume must never observe the still-uncleared gate.
    lock_fd, lock_path = _acquire_resume_lock(project, wf)
    if lock_fd is None:
        print(_cli_json_dumps({
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
    # RESOLVE-GATE-ONLY (operator decision A, EIGHTS-RECORD-OUTCOME-RCA-2026-09-16
    # §7 path K follow-up): the attended MCP route (`_run_resume_attended` in
    # mcp_servers/hydra_control/server.py) always passes `--gate-only`. When set,
    # this function resolves the pending gate (lock already held by the caller,
    # operator-capability mint+verify, spool prune, state patch clearing
    # pending_hitl, hitl_history) and returns WITHOUT ever calling `sup.invoke` —
    # no node_dispatch, no squad of any kind runs on the stub dispatcher. The
    # host's own step/submit loop (hydra.workflow.step / submit_host_result)
    # continues the workflow from its cursor. This is a narrower, explicit CLI
    # mode -- NOT a change to node_dispatch's non-live deferral filter, which is
    # untouched (and still applies to the ordinary --live-less resume below when
    # gate_only is False, e.g. direct CLI usage).
    #
    # This read MUST happen before the recover-stalled-stage branch immediately
    # below -- that branch is the one action here that is NOT gate-only-safe
    # (cross-vendor finding 1) and needs the flag to refuse.
    gate_only = bool(getattr(args, "gate_only", False))

    # Cross-vendor finding 3 (defence in depth -- argparse's mutually_exclusive_group
    # on the `resume` subparser already rejects this combination before any code
    # here runs; this guard covers any other caller of `_cmd_resume_locked`, e.g. a
    # future subcommand or a test that builds `args` directly). Checked BEFORE any
    # side effect of any kind -- no dispatcher construction, no spool drain, no
    # checkpoint load, nothing.
    if gate_only and getattr(args, "live", False):
        print(_cli_json_dumps({
            "ok": False,
            "error": "gate_only_live_conflict",
            "workflow_id": wf,
            "action": action,
            "message": (
                "--gate-only and --live are mutually exclusive: --gate-only "
                "never spawns a live MCP dispatcher or drains the eights "
                "spool. Use --gate-only alone for the attended host loop, or "
                "--live alone for a detached resume. Nothing was changed."
            ),
        }), file=sys.stderr)
        return 1

    # W2-4: recover-stalled-stage does not touch the LangGraph checkpoint
    # interrupt machinery the actions below use -- a stranded attended stage
    # is a host_bridge cursor whose pp-ledger call never landed, not an
    # HITL-paused graph. Route it separately, still under the same
    # claim-and-resume lock `_cmd_resume` already acquired above (governance:
    # a paused/stranded workflow resumes only via approve/resume).
    #
    # Cross-vendor finding 1 (CRITICAL): unlike every other action in this
    # function, `_cmd_recover_stalled_stage` builds a LIVE `MCPStdioDispatcher`
    # (`_attended_live_dispatcher`) and can replay a pp verdict, run
    # smoke/finalization, merge code, and mark an engineering task complete
    # (host_bridge.recover_stalled_stage). That is exactly the "live work" the
    # attended gate-only route (operator decision A) promises never to run --
    # "never touches sup.invoke, only sup.update_state" is true but irrelevant:
    # the live dispatcher itself runs squad/engineering recovery before this
    # function ever calls `sup.update_state`. Refuse it here (defence in
    # depth; the MCP server also refuses before ever invoking this CLI --
    # see `_run_resume_attended` in mcp_servers/hydra_control/server.py) so a
    # gate-only caller can never reach it, regardless of how the CLI is
    # invoked directly. The DETACHED route (`hydra resume --live
    # --action recover-stalled-stage`, requires HYDRA_ALLOW_DETACHED=1) is the
    # only route that may run this recovery; that behaviour is unchanged.
    if action == "recover-stalled-stage":
        if gate_only:
            print(_cli_json_dumps({
                "ok": False,
                "error": "recovery_is_live_operation",
                "workflow_id": wf,
                "action": action,
                "message": (
                    "recover-stalled-stage can replay a pp verdict and run "
                    "live squad/engineering work (smoke, finalize, merge); "
                    "it is refused on the attended gate-only resume route. "
                    "Use the detached CLI (`hydra resume --live --action "
                    "recover-stalled-stage --option <run_id>`, requires "
                    "HYDRA_ALLOW_DETACHED=1) to run this recovery. Nothing "
                    "was changed."
                ),
            }), file=sys.stderr)
            return 1
        return _cmd_recover_stalled_stage(args, project, wf, option)

    critique_client = None
    if getattr(args, "live", False):
        from .dispatcher import MCPStdioDispatcher
        from .judge import MCPCritiqueClient
        dispatcher = MCPStdioDispatcher(project, verbose=getattr(args, "verbose", False))
        critique_client = MCPCritiqueClient(dispatcher=dispatcher, cwd=project)
        # Resume re-enters dispatch — drive pp to real codegen on the live path.
        dispatcher.drive_pp_loop = True
        # RA-7: fail-soft background spool drain on every lifecycle resume entry.
        # node_intake (supervisor) calls replay_pending_async on full-run intake
        # but resume skips node_intake, so 6 000+ spooled entries since 2026-07-03
        # were never drained.  Non-blocking (replay_pending_async spawns a daemon
        # thread); failed files stay in the spool for the next attempt.
        try:
            from .eights.attestation import EightsAttestor
            EightsAttestor(dispatcher=dispatcher).replay_pending_async()
        except Exception:  # noqa: BLE001 — spool drain must never block resume
            pass
    else:
        dispatcher = _NullDispatcher()

    from .supervisor import build_supervisor, _PurePythonRunner
    sup = build_supervisor(
        project_root=project,
        dispatcher=dispatcher,
        critique_client=critique_client,
    )
    if isinstance(sup, _PurePythonRunner):
        print(_cli_json_dumps({
            "error": "langgraph unavailable — resume requires the checkpointing supervisor",
        }), file=sys.stderr)
        return 1

    config = {"configurable": {"thread_id": wf}}
    snap = sup.get_state(config)
    if snap is None or not snap.values:
        print(_cli_json_dumps({"workflow_id": wf, "error": "not_found"}))
        return 1
    values = snap.values
    pending = values.get("pending_hitl")

    # RESOLVE-GATE-ONLY single auth gate (cross-vendor finding 1, CRITICAL):
    # resolve the operator capability EXACTLY ONCE here, immediately after
    # the checkpoint is loaded and BEFORE ANY side effect below -- no
    # telemetry write (`emit(...)`), no spool prune/reconcile
    # (`_prune_spooled_hitl_requests`), no `sup.update_state`, no
    # `hitl_history` append, no graph re-entry (`sup.invoke`), and no
    # early-return branch of any kind that writes anything. Every gate_only
    # branch further down (bare-interrupt approve/force-dispatch, bare-
    # interrupt reject, the no-pending-gate branch, and the general
    # pending-gate path) reuses this single result instead of re-resolving
    # or, worse, skipping the check entirely (the CRITICAL gap: every
    # no-pending gate_only branch used to prune the spool with zero identity
    # verification). An unresolved refusal here changes NOTHING -- the
    # checkpoint was only read (`sup.get_state`), never written.
    operator_capability_patch: dict | None = None
    _operator = ""
    if gate_only:
        operator_capability_patch, _operator, _refusal = (
            _resolve_operator_capability_for_resume(
                args, wf, action, pending, gate_only=gate_only))
        if _refusal is not None:
            print(_cli_json_dumps(_refusal), file=sys.stderr)
            return 1

    if not pending:
        # MU7: inspect snap.next to distinguish a bare LangGraph interrupt
        # (no pending_hitl but graph paused before synthesis/judge_synthesis)
        # from a genuinely terminal state (snap.next empty).
        _snap_next = getattr(snap, "next", ()) or ()
        if _snap_next and action in ("approve", "force-dispatch"):
            # Bare interrupt + approve/force-dispatch: continue the graph --
            # UNLESS gate_only, in which case there is nothing to clear (no
            # pending_hitl exists here at all) and the graph must not be
            # re-entered either; just report the bare interrupt honestly so
            # the host knows to call step, exactly like every other gate_only
            # exit.
            emit(project, wf, "hitl_resumed", {
                "action": action,
                "option": option,
                "gate_node": None,
                "bare_interrupt": list(_snap_next),
                "gate_only": gate_only,
            })
            if gate_only:
                # Cross-vendor finding 3: under gate_only, `sup.invoke` is
                # NEVER called, so `snap.next` never advances past the
                # original interrupt -- a genuine retry of an already-
                # resolved gate-only action ALWAYS lands HERE (bare
                # interrupt, pending_hitl already None), not in the deeper
                # `no_pending_gate` branch below. THIS is therefore the
                # branch that must reconcile a spool prune that a prior call
                # (killed between its checkpoint patch and its spool prune)
                # left stranded, or `retry_after_partial_gate_only_is_safe`
                # (mcp_servers/hydra_control/server.py) is false in
                # practice. Idempotent: 0 pruned when there is nothing
                # stale, or `hitl_history` is empty (no gate ever resolved).
                _last_hist = values.get("hitl_history") or []
                _last_gate_node = None
                _last_hist_entry: dict | None = None
                if _last_hist and isinstance(_last_hist[-1], dict):
                    _last_hist_entry = _last_hist[-1]
                    _last_gate_node = _last_hist_entry.get("gate_node")
                _pruned_stale = _prune_spooled_hitl_requests(wf, _last_gate_node)
                _reconcile_out: dict = {}
                if _last_gate_node:
                    _reconcile_result = _resolve_eights_hitl_gate_only_bounded(
                        project, wf,
                        note=f"hydra resume retry-reconcile: {action}",
                        decision=_eights_decision_for_history_entry(_last_hist_entry),
                        gate_node=_last_gate_node,
                        reconcile=True,
                    )
                    _reconcile_out = _eights_resolution_fields(True, _reconcile_result)
                print(_cli_json_dumps({
                    "workflow_id": wf,
                    "ok": True,
                    "resumed": False,
                    "gate_only": True,
                    "graph_reentered": False,
                    "action": action,
                    "interrupted_before": list(_snap_next),
                    "gate_node": None,
                    "phase": values.get("phase"),
                    "status": values.get("phase"),
                    "pending_hitl": None,
                    "pruned_spooled_hitl_requests": _pruned_stale,
                    "note": ("bare interrupt observed, no pending_hitl gate to "
                             "clear; graph not re-entered — call "
                             "hydra.workflow.step to continue"),
                    **_reconcile_out,
                }))
                return 0
            final_dict = sup.invoke(None, config=config)
            _phase = (final_dict.get("phase") if isinstance(final_dict, dict)
                      else getattr(final_dict, "phase", "?"))
            print(_cli_json_dumps({
                "workflow_id": wf,
                "ok": True,
                "resumed": True,
                "action": action,
                "continued_bare_interrupt": True,
                "interrupted_before": list(_snap_next),
                "gate_node": None,
                "phase": _phase,
                "status": _phase,
                "pending_hitl": (final_dict.get("pending_hitl")
                                 if isinstance(final_dict, dict) else None),
                "trace": str(trace_path(project, wf)),
            }))
            return 0
        if _snap_next and action == "reject":
            # Bare interrupt + reject: park the workflow surfaced without
            # continuing (mirrors the real-gate reject path).
            #
            # Cross-vendor finding 2: this branch mutates checkpoint state
            # (`sup.update_state` below) with no pending_hitl gate at all --
            # it must pass through the SAME identity check as every other
            # mutating branch under gate_only. That check now runs exactly
            # ONCE, at the top of this function (finding 1) -- a refusal
            # there already returned before this branch could ever be
            # reached, so there is nothing further to verify here.
            sup.update_state(config, {"phase": "surfaced"})
            print(_cli_json_dumps({
                "workflow_id": wf,
                "ok": True,
                "resumed": False,
                "action": "reject",
                "phase": "surfaced",
                "status": "surfaced",
                "gate_node": None,
                "pending_hitl": None,
                "continued_bare_interrupt": False,
            }))
            return 0
        # All other cases (snap.next empty, or other actions with a bare
        # interrupt): frozen no_pending_gate contract.  Other actions
        # (modify-budget, change-squads) need a real gate — hint the operator
        # that approve would work when a bare interrupt is pending.
        _no_gate_out: dict = {
            "workflow_id": wf,
            "ok": True,
            "resumed": False,
            "reason": "no_pending_gate",
            "phase": values.get("phase"),
            "status": values.get("phase"),
            "gate_node": None,
            "pending_hitl": values.get("pending_hitl"),
        }
        if _snap_next:
            _no_gate_out["hint"] = "bare_interrupt_pending"
            _no_gate_out["interrupted_before"] = list(_snap_next)
        if gate_only:
            # Cross-vendor finding 3: the mutating path below writes the
            # checkpoint patch (pending_hitl=None, hitl_history append)
            # BEFORE pruning the spool. A child killed between those two
            # writes leaves a spooled HITL request for a gate that is
            # ALREADY resolved on the checkpoint; a retry lands HERE (no
            # pending_hitl) and, before this fix, never pruned it -- yet the
            # MCP server reports `retry_after_partial_gate_only_is_safe:
            # true` (mcp_servers/hydra_control/server.py). Make that claim
            # true: idempotently reconcile the spool for the most recently
            # resolved gate (the last `hitl_history` entry's `gate_node`) on
            # every gate-only no-pending-gate return. A no-op (0 pruned)
            # when nothing is spooled, already pruned, or `hitl_history` is
            # empty (workflow never had a gate at all).
            _last_hist = values.get("hitl_history") or []
            _last_gate_node = None
            _last_hist_entry = None
            if _last_hist and isinstance(_last_hist[-1], dict):
                _last_hist_entry = _last_hist[-1]
                _last_gate_node = _last_hist_entry.get("gate_node")
            _no_gate_out["pruned_spooled_hitl_requests"] = (
                _prune_spooled_hitl_requests(wf, _last_gate_node))
            # Cross-vendor finding 1b: same bounded reconciliation as the
            # bare-interrupt gate_only branch above -- a retry that lands
            # HERE (checkpoint already shows no pending gate) must also
            # reconcile TheEights' ledger for the most recently resolved
            # gate, not just the local spool.
            if _last_gate_node:
                _reconcile_result = _resolve_eights_hitl_gate_only_bounded(
                    project, wf,
                    note=f"hydra resume retry-reconcile: {action}",
                    decision=_eights_decision_for_history_entry(_last_hist_entry),
                    gate_node=_last_gate_node,
                    reconcile=True,
                )
                _no_gate_out.update(
                    _eights_resolution_fields(True, _reconcile_result))
        print(_cli_json_dumps(_no_gate_out))
        return 0

    from datetime import datetime, timezone
    resolution = {
        **(pending if isinstance(pending, dict) else {}),
        "resolution": action,
        "option": option,
        "resolved_at": datetime.now(timezone.utc).isoformat(),
    }

    # WS-AUTH run-A / cross-vendor finding 2 (RESOLVE-GATE-ONLY): mint +
    # verify an operator-capability token before this function's FIRST state
    # mutation (`sup.update_state(config, patch)` below). Historically this
    # only ran for `_MUTATING_RESUME_ACTIONS`; `reject` was NOT a member, so
    # a gate-only reject cleared pending_hitl with no identity check at all.
    #
    # Under gate_only, `operator_capability_patch`/`_operator` were ALREADY
    # resolved exactly once at the top of this function (finding 1) -- a
    # refusal there would have returned before reaching this point, so
    # nothing further needs to happen here and the resolved values must NOT
    # be re-initialised/overwritten. Only the legacy non-gate_only CLI path
    # (direct `hydra resume` without --gate-only) still mints here, scoped to
    # its original narrower allow-list (WS-AUTH run-A warn-and-proceed
    # posture, unchanged).
    _MUTATING_RESUME_ACTIONS = frozenset({"approve", "force-dispatch",
                                          "modify-budget", "change-squads",
                                          "modify-plan"})
    if not gate_only and action in _MUTATING_RESUME_ACTIONS:
        operator_capability_patch, _operator, _refusal = (
            _resolve_operator_capability_for_resume(
                args, wf, action, pending, gate_only=gate_only))
        if _refusal is not None:
            print(_cli_json_dumps(_refusal), file=sys.stderr)
            return 1

    patch: dict = {"pending_hitl": None, "hitl_history": [resolution]}
    if operator_capability_patch is not None:
        patch["operator_capability"] = operator_capability_patch

    if action == "change-squads":
        if not option:
            print(_cli_json_dumps({"error": "change-squads needs --option \"squad-a,squad-b\""}),
                  file=sys.stderr)
            return 1
        patch["selected_squads"] = [s.strip() for s in option.split(",") if s.strip()]
    if action == "modify-budget":
        try:
            new_budget_usd = float(option)
            # Cross-vendor judge finding (this round, CRITICAL): this was
            # the ONE `modify-budget` entry that wrote `option` straight
            # into the checkpoint's `budget.budget_usd` with no finiteness
            # check at all (not even the non-negative check `--set`/MCP
            # `set_budget` had) -- `hydra resume <id> --action modify-budget
            # --option nan` poisoned an ALREADY-RUNNING workflow's budget
            # via `update_state` below. Same shared validator as the
            # argparse `--budget` flags, `hydra budget --set`, and the MCP
            # tools (see `strict_json.reject_non_finite`'s docstring).
            reject_non_finite(new_budget_usd, flag="--option")
            budget = values.get("budget")
            b = dict(budget) if isinstance(budget, dict) else (
                budget.model_dump(mode="json") if hasattr(budget, "model_dump") else {})
            b["budget_usd"] = new_budget_usd
            patch["budget"] = b
        except (TypeError, ValueError) as e:
            detail = str(e) if "finite number" in str(e) else (
                f"modify-budget needs a numeric --option, got {option!r}"
            )
            print(_cli_json_dumps({"error": detail}), file=sys.stderr)
            return 1

    # P5c Task 2: --modify-plan. Validated and prepared here (alongside the
    # other per-action patch blocks); the actual graph re-entry happens
    # further down, in its own early-return branch next to reject/abort --
    # `sup.invoke(None, config=config)` at the bottom of this function would
    # resume the graph from wherever it is genuinely parked (`plan_gate`),
    # re-running `node_plan_gate` against the OLD `plan_ref` and materialising
    # the WRONG plan's steps. `_modify_plan_task`/`_modify_plan_new_revision`
    # are consumed by that later branch.
    _modify_plan_task: TaskState | None = None
    _modify_plan_new_revision: int | None = None
    _modify_plan_prior_envelope_id = None
    if action == "modify-plan":
        if resolution.get("gate_node") != "plan_gate":
            print(_cli_json_dumps({
                "error": "modify-plan is only valid at the plan_gate",
                "gate_node": resolution.get("gate_node"),
            }), file=sys.stderr)
            return 1
        _critique_ref = getattr(args, "critique_ref", None)
        if not _critique_ref:
            print(_cli_json_dumps({
                "error": "modify-plan needs --critique-ref <path-or-memoryref> "
                         "(the critique text itself never travels as --option)",
            }), file=sys.stderr)
            return 1
        try:
            _critique_text = _read_plan_critique(_critique_ref, project)
        except _PlanCritiqueError as exc:
            print(_cli_json_dumps({"error": str(exc)}), file=sys.stderr)
            return 1
        _cur_revision = int(values.get("plan_revision") or 0)
        _max_revisions = plan_max_revisions()
        if plan_revision_ceiling_reached(_cur_revision, _max_revisions):
            print(_cli_json_dumps({
                "error": "revision_ceiling_reached",
                "plan_revision": _cur_revision,
                "max_revisions": _max_revisions,
            }), file=sys.stderr)
            return 1
        _modify_plan_new_revision = _cur_revision + 1
        _modify_plan_prior_envelope_id = values.get("plan_envelope_id")
        _modify_plan_task = TaskState(
            owner_squad="planning",
            description=(
                f"Revise the {values.get('plan_rigor') or 'standard'}-rigor plan "
                f"(revision {_modify_plan_new_revision}) for: "
                f"{values.get('root_goal', '')}"
            ),
            priority="P2",
            # Deliberately non-zero (unlike node_planner's P5a seed, which
            # leaves this at the TaskState default 0) -- this task's own
            # plan_revision is stamped to the NEW revision it is authoring,
            # so if a later modify-plan supersedes it before it is ever
            # dispatched, the stale-revision filters the four selectors
            # already apply (cli.py, node_dispatch's sequential loop) skip
            # it exactly like they skip a superseded plan STEP task.
            plan_revision=_modify_plan_new_revision,
            plan_critique=_critique_text,
            supersedes_plan_envelope_id=(
                str(_modify_plan_prior_envelope_id)
                if _modify_plan_prior_envelope_id else None
            ),
        )

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

    # P5c Task 1: force-dispatch is a governance event, not a synonym for
    # approve -- two runbooks (plugins/hydra/skills/hitl-protocol/SKILL.md,
    # plugins/hydra/skills/resume/SKILL.md) already promise a `policy_override`
    # audit trail for `--force-dispatch`; the engine never actually emitted
    # one anywhere. This closes that gap for EVERY force-dispatch, not only
    # at plan_gate — a pre-existing defect on every other gate too, now
    # visible to any consumer tailing the trace for the first time.
    if action == "force-dispatch":
        _fd_gate_node = resolution.get("gate_node")
        emit(project, wf, "policy_override", {
            "gate_node": _fd_gate_node,
            "gate_reason": _gate_reason,
            "option": option,
            "operator": _operator,
        })
        if _fd_gate_node == "plan_gate":
            # `bypassed` is NOT a member of `_PLAN_BARRIER_STATES` (state.py)
            # -- this write cannot raise the plan barrier, unlike the
            # `rejected` write below, which the reject path scopes to
            # plan_gate for exactly that reason. Still scoped here too, so a
            # force-dispatch past a DIFFERENT gate never touches plan_status.
            patch["plan_status"] = "bypassed"
            _bypass_note = {
                "event": "plan_gate_bypassed",
                "workflow_id": wf,
                "note": "dispatch proceeded without plan approval (force-dispatch)",
                "resolved_at": resolution["resolved_at"],
            }
            patch["hitl_history"] = [resolution, _bypass_note]
            _append_plan_governance_note(
                project, wf, values.get("plan_artifact_location"),
                "dispatch proceeded without plan approval (force-dispatch, "
                f"resolved_at={resolution['resolved_at']})",
            )

    sup.update_state(config, patch)

    # C3: prevent a later spool replay from filing a ticket for this
    # now-resolved gate (late-spool orphan reconciliation, gate-identity-keyed).
    pruned_spool = _prune_spooled_hitl_requests(wf, resolution.get("gate_node"))

    # E2-17: the gate is now resolved on the Hydra side — close the matching
    # row in TheEights' shared ledger too, or it stays pending forever. Scoped
    # to the resolved gate identity (same key the spool prune uses), so another
    # open gate in this workflow survives.
    #
    # Operator decision 2: under gate_only there is no live dispatcher at all
    # (gate_only forbids --live -- see the mutual-exclusion guard above), so
    # the legacy `_resolve_eights_hitl_for_workflow(dispatcher=dispatcher)`
    # call below would only ever run against `_NullDispatcher`, which cannot
    # reach TheEights. Resolve TheEights NOW instead, with the dedicated
    # narrow live client (`_resolve_eights_hitl_gate_only` -- never spools,
    # never replays). The legacy call (fail-soft, spools on failure) stays
    # for the non-gate_only CLI path, unchanged.
    _terminal_resolution = action == "reject" or option == "abort"
    if gate_only:
        _eights_gate_only = _resolve_eights_hitl_gate_only_bounded(
            project, wf,
            note=("workflow terminal: surfaced" if _terminal_resolution
                  else f"hydra resume: {action}"),
            decision="rejected" if _terminal_resolution else "approved",
            gate_node=resolution.get("gate_node") or None,
        )
        _eights_hitl = {"resolved": _eights_gate_only.get("resolved", 0)}
    else:
        _eights_gate_only = {}
        _eights_hitl = _resolve_eights_hitl_for_workflow(
            project, wf,
            note=("workflow terminal: surfaced" if _terminal_resolution
                  else f"hydra resume: {action}"),
            decision="rejected" if _terminal_resolution else "approved",
            gate_node=resolution.get("gate_node") or None,
            dispatcher=dispatcher,
        )
    emit(project, wf, "hitl_resumed", {
        "action": action,
        "option": option,
        "gate_node": resolution.get("gate_node"),
        "pruned_spooled_hitl_requests": pruned_spool,
        "eights_hitl_resolved": _eights_hitl.get("resolved", 0),
    })

    # F10: abort option → park the workflow surfaced without resuming the graph.
    # Handled AFTER the patch+spool-prune+emit so the gate resolution is fully
    # recorded before returning (mirrors the reject path).
    if option == "abort":
        sup.update_state(config, {"phase": "surfaced"})
        print(_cli_json_dumps({
            "workflow_id": wf,
            "ok": True,
            "resumed": False,
            "action": "abort_option",
            "phase": "surfaced",
            "status": "surfaced",
            "gate_node": resolution.get("gate_node"),
            "pending_hitl": None,
            # abort never re-entered the graph even before gate_only existed
            # -- that BEHAVIOR is identical either way (operator decision A).
            # The JSON body itself is NOT byte-for-byte identical to the
            # pre-gate_only output, though: `gate_only` is a new field
            # present on every call (False on the legacy path), and
            # `eights_resolution` is a new, additive key that only appears
            # when gate_only is set (cross-vendor finding 5) -- a consumer
            # that treated the old body as a closed/fixed key set would see
            # an unfamiliar key, not the same document.
            "gate_only": gate_only,
            "graph_reentered": False,
            **_eights_resolution_fields(gate_only, _eights_gate_only),
        }, indent=2))
        return 0

    if action == "reject":
        # A rejected gate does NOT continue the graph; the workflow stays
        # parked as 'surfaced' with the resolution on record. Deliberately
        # NO automatic re-plan: an engine that authors another plan the
        # moment one is rejected is a loop the operator cannot stop. The
        # rejected plan stays on disk marked rejected.
        _reject_patch: dict[str, Any] = {"phase": "surfaced"}
        # P5c Task 3: `rejected` IS a member of `_PLAN_BARRIER_STATES`
        # (state.py) -- writing it RAISES the plan barrier. This handler
        # runs for EVERY gate rejection (budget, high_risk, constitution,
        # plan_gate, ...), so the write must be scoped to plan_gate: an
        # unscoped write here would raise a barrier that, with
        # HYDRA_PLAN_PHASE off, no flag-gated code could ever clear —
        # exactly the total-dispatch-freeze class of bug the flag exists to
        # prevent, reachable from an ordinary operator reject. See the
        # `bypassed` write above (Task 1) for the safe-by-construction
        # counterpart, and state.py's `_PLAN_BARRIER_STATES` comment.
        if resolution.get("gate_node") == "plan_gate":
            _reject_patch["plan_status"] = "rejected"
        sup.update_state(config, _reject_patch)
        print(_cli_json_dumps({
            "workflow_id": wf,
            "ok": True,
            "resumed": False,
            "action": "reject",
            "phase": "surfaced",
            "status": "surfaced",
            "gate_node": resolution.get("gate_node"),
            "pending_hitl": None,
            # reject never re-entered the graph even before gate_only existed
            # -- that BEHAVIOR is identical either way (operator decision A).
            # As with the abort-option body above, the JSON itself is
            # ADDITIVE, not byte-for-byte identical to the pre-gate_only
            # output: `gate_only` is now always present, and
            # `eights_resolution` is a new key added only when gate_only is
            # set (cross-vendor finding 5).
            "gate_only": gate_only,
            "graph_reentered": False,
            **_eights_resolution_fields(gate_only, _eights_gate_only),
        }, indent=2))
        return 0

    if action == "modify-plan":
        # P5c Task 2: re-enter the graph the same way the ingest PLAN branch
        # does (`_reenter_graph_after_dispatch`, P5b Task 3) -- as_node=
        # "dispatch" makes the graph believe dispatch just finished so
        # after_dispatch's conditional edge fires fresh against the NEW
        # plan_status ("authoring", not "drafted"), routing to "await_host"
        # (-> END) rather than re-running the still-parked `plan_gate`
        # interrupt node against the OLD `plan_ref`. Reusing this exact
        # primitive (rather than writing a second copy) is deliberate — see
        # this function's brief on hand-duplicated decisions.
        assert _modify_plan_task is not None and _modify_plan_new_revision is not None
        # RESOLVE-GATE-ONLY: the state mutation (new revision task, plan_status
        # "authoring") is applied either way; only the invoke loop that would
        # actually run the graph is skipped under gate_only (see
        # `_reenter_graph_after_dispatch`'s gate_only docstring).
        parked_at = _reenter_graph_after_dispatch(sup, config, {
            "plan_status": "authoring",
            "plan_revision": _modify_plan_new_revision,
            "tasks": [_modify_plan_task],
        }, gate_only=gate_only)
        emit(project, wf, "plan_modify_requested", {
            "prior_plan_envelope_id": (
                str(_modify_plan_prior_envelope_id)
                if _modify_plan_prior_envelope_id else None
            ),
            "plan_revision": _modify_plan_new_revision,
            "gate_only": gate_only,
        })
        _modify_plan_out: dict = {
            "workflow_id": wf,
            "ok": True,
            "resumed": not gate_only,
            "action": "modify-plan",
            "plan_status": "authoring",
            "status": "authoring",
            "plan_revision": _modify_plan_new_revision,
            "plan_parked_at": parked_at,
            "gate_node": resolution.get("gate_node"),
            "pending_hitl": None,
        }
        if gate_only:
            _modify_plan_out["gate_only"] = True
            _modify_plan_out["graph_reentered"] = False
            _modify_plan_out.update(_eights_resolution_fields(gate_only, _eights_gate_only))
            _modify_plan_out["note"] = (
                "plan revision task recorded, graph not re-entered — call "
                "hydra.workflow.step to continue"
            )
        print(_cli_json_dumps(_modify_plan_out, indent=2))
        return 0

    if gate_only:
        # RESOLVE-GATE-ONLY (decision A): every remaining action here
        # (approve, force-dispatch, modify-budget, change-squads) has already
        # had its gate resolved above -- pending_hitl cleared, hitl_history
        # recorded, per-action patch applied (budget/squads/reflexion-override/
        # policy_override), spool pruned, TheEights resolution attempted.
        # Stop here: never call `sup.invoke` -- no node_dispatch, no squad of
        # any kind runs on the stub. Re-read the checkpoint (not `values`,
        # which is the PRE-patch snapshot) so phase/pending_hitl reflect what
        # was actually just written.
        _post_snap = sup.get_state(config)
        _post_values = _post_snap.values if _post_snap is not None and _post_snap.values else {}
        _post_phase = _post_values.get("phase", values.get("phase"))
        print(_cli_json_dumps({
            "workflow_id": wf,
            "ok": True,
            "resumed": False,
            "gate_only": True,
            "graph_reentered": False,
            "action": action,
            "phase": _post_phase,
            "status": _post_phase,
            "gate_node": resolution.get("gate_node"),
            "pending_hitl": _post_values.get("pending_hitl"),
            **_eights_resolution_fields(gate_only, _eights_gate_only),
            "note": (
                "gate resolved without re-entering the graph — call "
                "hydra.workflow.step to continue"
            ),
        }, indent=2))
        return 0

    final_dict = sup.invoke(None, config=config)
    phase = final_dict.get("phase") if isinstance(final_dict, dict) else getattr(final_dict, "phase", "?")
    _resulting_pending = (final_dict.get("pending_hitl")
                          if isinstance(final_dict, dict) else None)
    print(_cli_json_dumps({
        "workflow_id": wf,
        "ok": True,
        "resumed": True,
        "action": action,
        "phase": phase,
        "status": phase,
        "gate_node": resolution.get("gate_node"),
        "pending_hitl": _resulting_pending,
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
        print(_cli_json_dumps({"error": f"invalid workflow_id {wf!r}"}), file=sys.stderr)
        return 1

    try:
        envelopes = _load_envelopes_file(Path(args.envelopes))
    except (OSError, ValueError) as e:
        print(_cli_json_dumps({"error": f"could not read --envelopes: {e}"}), file=sys.stderr)
        return 1
    if not envelopes:
        print(_cli_json_dumps({"workflow_id": wf, "ingested": False,
                          "reason": "no_envelopes"}))
        return 0

    lock_fd, lock_path = _acquire_resume_lock(project, wf)
    if lock_fd is None:
        print(_cli_json_dumps({"workflow_id": wf, "ingested": False,
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
        # RA-7: fail-soft background spool drain (mirrors _cmd_resume_locked).
        try:
            from .eights.attestation import EightsAttestor
            EightsAttestor(dispatcher=dispatcher).replay_pending_async()
        except Exception:  # noqa: BLE001
            pass
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
        print(_cli_json_dumps({
            "workflow_id": wf, "ingested": False,
            "error": "langgraph unavailable — ingest requires the checkpointing supervisor",
        }), file=sys.stderr)
        return 1
    snap = sup.get_state(config)
    if snap is None or not snap.values:
        print(_cli_json_dumps({"workflow_id": wf, "ingested": False, "error": "not_found",
                          "detail": "no checkpoint for this workflow_id"}), file=sys.stderr)
        return 1
    try:
        state = HydraState.model_validate(snap.values)
    except Exception as e:  # noqa: BLE001
        print(_cli_json_dumps({"workflow_id": wf, "ingested": False,
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
    from .ingest import IngestItemResult, normalize_for_ingest, release_ingested_ids
    # The claim/release decision is `_ingest_item_should_release_claim`
    # (defined above in this module) — the SAME function `_cmd_attended_
    # submit`'s emitted-envelopes loop calls. P5b revise round item 3: this
    # loop used to carry its own hand-duplicated copy of the decision (a
    # local `_NOT_DISPATCHED = {"unknown_target"}` plus an inline
    # `failed`-with-errors check) that agreed with the extracted function on
    # every case except `plan_phase_disabled` -- added to the extracted
    # function for item 2, but never to this copy, leaving a PLAN submitted
    # through `hydra ingest` with the flag off permanently claimed. This is
    # the third time this feature produced that exact defect shape (a rule
    # duplicated by hand, then updated in one copy only); collapsing to one
    # call is the fix, not adding the missing case here too.
    processed: set[str] = set(load_ingested_ids(project, wf))
    agg_items: list = []
    over_budget = False
    for env_dict in envelopes:
        # E2-34: normalize first so a missing or non-UUID pack id becomes a real
        # UUID before it is used as the ledger key — otherwise such an envelope
        # bypasses `processed` and can dispatch twice.
        try:
            env_dict = normalize_for_ingest(env_dict, _emit_ingest)
        except ValueError as exc:
            # b1baf30 revise round item 6: normalize_for_ingest raises when a
            # pack-supplied budget_usd could not be converted to a finite
            # float. Report it as a real failed item instead of crashing the
            # whole submit batch.
            bad_id = str(env_dict.get("id", "?"))
            errors = [{"field": "budget_usd", "msg": str(exc)}]
            agg_items.append(IngestItemResult(
                envelope_id=bad_id, envelope_type=env_dict.get("type"), target=None,
                status="failed", detail=f"invalid envelope: {exc}", errors=errors,
            ))
            _emit_ingest("ingest.invalid_envelope", {
                "envelope_id": bad_id, "type": env_dict.get("type"), "errors": errors,
            })
            continue
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
        # Un-claim if this envelope never reached a squad (wrong type/parse
        # fail/flag-refused) so it can be re-submitted after correction;
        # otherwise mark it processed. `dispatch_ingested_envelopes` was
        # called above with a SINGLE-element `[env_dict]`, so `out_i.items`
        # holds at most one entry for this envelope — `[-1]` is "the one
        # item for this envelope" (or None if dispatch produced nothing,
        # which the `last_item is not None` check below treats as "keep
        # claimed", matching this loop's prior behaviour). Same shape as
        # `_cmd_attended_submit`'s per-envelope loop, which also dispatches
        # one envelope at a time and reads `outcome.items[-1]` the same way
        # — that symmetry is why calling the one shared decision function
        # here is faithful to what THIS loop iterates, not just convenient.
        last_item = out_i.items[-1] if out_i.items else None
        if eid and last_item is not None and _ingest_item_should_release_claim(last_item):
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
    print(_cli_json_dumps({
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
    for t in getattr(state, "tasks", []):
        if str(t.task_id) in done:
            continue
        if t.owner_squad == "engineering":
            continue
        pack = packs.get(t.owner_squad)
        if pack is None:
            continue
        if pack.entrypoint in _NON_ENG_ATTENDED_ENTRYPOINTS:
            return t, pack
    return None, None


_NON_ENG_ATTENDED_ENTRYPOINTS = frozenset(
    {"claude-skill", "claude-native", "agent-impersonation"})


def _next_attended_task(state: HydraState, packs: dict):
    """First attended-eligible task in ``state.tasks`` list order.

    E2-23: selection must follow the planner's task order, not drain
    engineering first.  ``/hydra:campaign`` pre-wires executive -> creative ->
    engineering; picking the engineering task first inverted that DAG and ran
    the engineer without its upstream envelopes.

    Returns ``(task, kind, pack)`` where ``kind`` is ``"engineering"`` (pack is
    None — engineering opens a pp run cursor) or ``"squad"`` (pack is the
    non-engineering squad pack whose entrypoint is host-attended).  Returns
    ``(None, None, None)`` when nothing is pending.  Tasks already recorded in
    ``attended_completed_task_ids`` are skipped, and non-engineering tasks
    whose squad is unknown or headless-dispatchable are passed over (they are
    not host-attended) so engineering behind them is still reachable.

    P1: the squad only decides WHICH cursor is opened — it must not reorder
    the planner's dependency chain. Order still comes from the planner's
    ``state.tasks`` list; two gates now additionally hold a candidate back
    without reordering anything: while a plan barrier is active
    (``plan_barrier_active``) only a ``planning``-owned task is selectable,
    and independently of the barrier, a task whose ``depends_on`` is not yet
    satisfied (``plan_deps_satisfied``) is skipped so a later, ready task can
    be picked instead. Both are no-ops while ``plan_status == "none"`` and no
    task carries ``depends_on``.
    """
    done = set(getattr(state, "attended_completed_task_ids", []) or [])
    barrier = plan_barrier_active(state)
    for t in getattr(state, "tasks", []):
        if str(t.task_id) in done:
            continue
        if getattr(t, "plan_revision", 0) and t.plan_revision != state.plan_revision:
            continue
        if barrier and t.owner_squad != "planning":
            continue
        # Deliberately unconditional (NOT `if barrier and not
        # plan_deps_satisfied(...)`): approval sets plan_status="approved",
        # which is intentionally not a barrier state, so a barrier-conditional
        # check would stop honoring an approved plan's step dependencies the
        # instant the plan was approved -- destroying the DAG ordering this
        # feature exists to provide. Do not "fix" this into a barrier-gated
        # check; empty `depends_on` (today's default for every task) always
        # satisfies, so this stays a no-op until a planner populates it.
        if not plan_deps_satisfied(state, t):
            continue
        if t.owner_squad == "engineering":
            return t, "engineering", None
        pack = (packs or {}).get(t.owner_squad)
        if pack is None:
            continue
        if pack.entrypoint in _NON_ENG_ATTENDED_ENTRYPOINTS:
            return t, "squad", pack
    return None, None, None


def _attended_task_gate_type(task, state: HydraState) -> str | None:
    """B9: best-effort real pp gate_type for an attended engineering
    ``TaskState``, derived from the actual Hydra envelope that triggered it
    (PRD/ARCH_RFC/DEV_TASK/HANDOFF via ``squad_node._gate_type_for_envelope``)
    rather than a hardcoded literal.

    ``TaskState`` itself carries no envelope type, only ``envelope_id``; the
    triggering envelope's raw dict (with its real ``type``) lives in
    ``state.envelopes``. Returns ``None`` (falls through to host_bridge's
    documented code_style DEFAULT) when the task has no envelope_id or no
    matching envelope is found — e.g. a planner-synthesised default task with
    no originating PRD/ARCH_RFC/DEV_TASK envelope.
    """
    eid = str(getattr(task, "envelope_id", "") or "")
    if not eid:
        return None
    for env in getattr(state, "envelopes", None) or []:
        if isinstance(env, dict) and str(env.get("id") or "") == eid:
            from .squad_node import _gate_type_for_envelope
            return _gate_type_for_envelope(env.get("type"))
    return None


def _pack_lead_agent_spec(pack):
    """The pack's lead AgentSpec: first gatekeeper, else first agent, else None."""
    agents = list(getattr(pack, "agents", []) or [])
    for a in agents:
        if getattr(a, "authority", "") == "gatekeeper":
            return a
    return agents[0] if agents else None


def _skill_pack_checkout(pack) -> Path | None:
    """Real on-disk checkout of a claude-skill pack, or None.

    ``squads/<slug>/`` holds only Hydra's squad.yaml overlay -- the agents and
    the pack's slash command live in the sibling repo named by ``source_pack``
    (a repo URL, or a filesystem path for a locally vendored pack). The repo id
    is the URL's last segment, resolved through the allow-listed registry.
    """
    src = str(getattr(pack, "source_pack", None) or "").strip()
    if not src:
        return None
    if "://" not in src:
        p = Path(src).expanduser()
        if p.is_dir():
            return p.resolve()
    repo_id = src.rstrip("/").replace("\\", "/").rsplit("/", 1)[-1]
    if repo_id.endswith(".git"):
        repo_id = repo_id[:-4]
    if not repo_id:
        return None
    try:
        from .repo_registry import resolve_repo_path
        return resolve_repo_path(repo_id.lower())
    except Exception:  # noqa: BLE001 -- unregistered/absent checkout is not fatal
        return None


def _agent_frontmatter_name(agent_path: Path) -> str | None:
    """Top-level ``name:`` from a Claude Code agent file's YAML frontmatter."""
    try:
        text = agent_path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return None
    if not text.startswith("---"):
        return None
    end = text.find("\n---", 3)
    block = text[3:end] if end != -1 else text[3:]
    for line in block.splitlines():
        if line.startswith("name:"):  # top-level key only (no indent)
            return line[len("name:"):].strip().strip("\"'") or None
    return None


def _resolve_claude_skill_host_action(pack) -> dict:
    """Host-action fields for a claude-skill pack's attended lead agent.

    E2-31: the bare squad.yaml slug (e.g. ``support-supervisor``) is NOT a
    spawnable agent type -- pack agents live in the sibling checkout under their
    own frontmatter names. Resolve a spawnable identity instead:

    * pack installed as a Claude Code plugin (``.claude-plugin/plugin.json``)
      -> ``"<plugin-name>:<agent frontmatter name>"``;
    * otherwise -> ``"general-purpose"`` plus ``skill`` (the pack's slash
      command) and ``tool_scope`` (its MCP shim prefix) so the host drives the
      pack through its declared command and MCP tools.

    ``lead_agent_file`` is always included when resolvable, for transparency.
    """
    action: dict = {"agent_type": "general-purpose"}
    checkout = _skill_pack_checkout(pack)
    spec = _pack_lead_agent_spec(pack)

    agent_path: Path | None = None
    if checkout is not None and spec is not None and getattr(spec, "agent_file", None):
        from .squad_loader import resolve_agent_file_path
        try:
            agent_path = resolve_agent_file_path(checkout, spec.agent_file)
        except FileNotFoundError:
            candidate = checkout / spec.agent_file
            agent_path = candidate if candidate.is_file() else None
    if agent_path is not None:
        action["lead_agent_file"] = str(agent_path)

    plugin_name: str | None = None
    if checkout is not None:
        manifest = checkout / ".claude-plugin" / "plugin.json"
        if manifest.is_file():
            try:
                plugin_name = (json.loads(manifest.read_text(encoding="utf-8"))
                               .get("name") or None)
            except (OSError, ValueError):
                plugin_name = None

    if plugin_name and agent_path is not None:
        agent_name = _agent_frontmatter_name(agent_path) or agent_path.stem
        action["agent_type"] = f"{plugin_name}:{agent_name}"
        return action

    # Not plugin-loadable: drive the pack's slash command through its MCP shim.
    from .squad_node import _SKILL_PACK_SHIMS
    shim = _SKILL_PACK_SHIMS.get(pack.slug) or {}
    invoke = getattr(pack, "invoke", None) or {}
    command = invoke.get("command_hint") or shim.get("default_cmd")
    if command:
        action["skill"] = command
    if shim.get("prefix"):
        action["tool_scope"] = shim["prefix"]
    return action


def _next_stub_attended_task(state: HydraState, packs: dict):
    """E2-32: first pending task (task-list order) owned by a squad whose pack
    entrypoint is ``stub``, that the host has not yet completed.

    A stub squad has no host action to spawn and no MCP leg to drive — its
    whole contract is the canned ``[STUB]`` DecisionRecord that
    ``squad_node._stub`` returns. Before this, ``_next_engineering_task`` and
    ``_next_nonengineering_attended_task`` both skipped stub tasks, so an
    attended workflow whose only task was a stub squad reported
    ``no_pending_task`` forever and never advanced.

    P1: while a plan barrier is active, a stub task cannot jump ahead of the
    planning task — this selector is consulted BEFORE ``_next_attended_task``
    in ``_cmd_attended_step``, so without this guard a pre-seeded stub task
    ordered ahead of the planning task would be driven regardless of
    ``plan_status``. A stale-revision stub task (superseded by a replan) is
    also skipped. Both are no-ops while ``plan_status == "none"``.
    """
    done = set(getattr(state, "attended_completed_task_ids", []) or [])
    barrier = plan_barrier_active(state)
    for t in getattr(state, "tasks", []):
        if str(t.task_id) in done:
            continue
        if getattr(t, "plan_revision", 0) and t.plan_revision != state.plan_revision:
            continue
        if barrier and t.owner_squad != "planning":
            continue
        pack = packs.get(t.owner_squad)
        if pack is not None and getattr(pack, "entrypoint", None) == "stub":
            return t, pack
    return None, None


def _task_precedes(state: HydraState, task, other) -> bool:
    """True when `task` comes before `other` in the workflow's task list.
    A None `other` (no competing candidate) means `task` wins by default."""
    if other is None:
        return True
    order = [str(t.task_id) for t in getattr(state, "tasks", [])]
    try:
        return order.index(str(task.task_id)) < order.index(str(other.task_id))
    except ValueError:  # pragma: no cover — both ids come from state.tasks
        return True


def _drive_stub_task(sup, config: dict, project: Path, wf: str,
                     state: HydraState, task, pack) -> dict:
    """E2-32: run a stub squad's in-process path and record it as the task
    result, with the same checkpoint bookkeeping ``submit-host-result`` does
    for a squad result.

    Idempotent with the in-graph path: when a headless dispatch pass already
    produced this squad's ``[STUB]`` DecisionRecord (RA-5 keeps stubs OUT of
    the live-defer pre-filter, so ``node_dispatch`` runs ``_stub`` itself), the
    existing envelope is reused instead of emitting a second one. The task is
    marked attended-complete but deliberately NOT added to
    ``attended_done_task_ids`` — a stub is `surfaced`, not `done`, and
    ``enforce_governance`` must keep surfacing the workflow for human
    follow-up.
    """
    from .squad_node import _stub
    from .schemas import CSuiteDecisionPacket

    task_id = str(task.task_id)
    existing: dict | None = None
    for e in getattr(state, "envelopes", []) or []:
        if not isinstance(e, dict):
            continue
        if (e.get("origin_squad") == pack.slug
                and "[STUB]" in str(e.get("decision") or "")):
            existing = e
            break

    new_envelopes: list[dict] = []
    if existing is None:
        inbound = CSuiteDecisionPacket(
            workflow_id=state.workflow_id,
            origin_squad="hydra",
            target_squad=pack.slug,
            origin="BOARDROOM",
            objective=task.description or state.root_goal,
        )
        result = _stub(pack, inbound)
        from .ingest import _redact_envelope_dict
        for produced in result.envelopes:
            d = _redact_envelope_dict(produced.model_dump(mode="json"))
            d["_task_id"] = task_id
            new_envelopes.append(d)
        existing = new_envelopes[0] if new_envelopes else None

    completed = list(getattr(state, "attended_completed_task_ids", []) or [])
    if task_id not in completed:
        completed.append(task_id)
    patch: dict = {"attended_completed_task_ids": completed}
    if new_envelopes:
        # `envelopes` uses an append reducer — emit only the NEW records.
        patch["envelopes"] = new_envelopes
    try:
        sup.update_state(config, patch)
    except Exception as e:  # noqa: BLE001
        emit(project, wf, "attended.persist_failed", {"error": str(e)})

    envelope_id = (str(existing.get("id"))
                   if isinstance(existing, dict) and existing.get("id") is not None
                   else None)
    emit(project, wf, "dispatch.stub_surfaced", {
        "task_id": task_id,
        "squad_slug": pack.slug,
        "entrypoint": "stub",
        "envelope_id": envelope_id,
        "reused_in_graph_record": not new_envelopes,
    })
    return {
        "status": "stub_surfaced",
        "task_id": task_id,
        "squad_slug": pack.slug,
        "envelope_id": envelope_id,
        "decision_record": existing,
    }


def _run_first_step_dispatch_pass(sup, config: dict, project: Path, wf: str,
                                  snap, state: HydraState) -> bool:
    """E2-32: run the headless dispatch pass `hydra approve` runs, on the FIRST
    attended step of a workflow that has no approval gate.

    ``hydra plan`` adds ``dispatch`` to ``interrupt_before``, so every planned
    workflow parks at a bare interrupt. A gated workflow leaves that interrupt
    via ``/hydra:approve``; a workflow whose planner set
    ``requires_human_approval=False`` had NO caller for that pass at all, so
    claude-skill / agent-impersonation tasks never got their
    ``dispatch.deferred_to_host`` marking and the phase never left ``dispatch``.

    Guards (all required): a bare LangGraph interrupt is pending, no HITL gate
    is open, the planner did not require approval, and nothing has been driven
    attended yet (this is a first-step bootstrap, not a per-step resume).
    A pending engineering task suppresses the pass — the attended engineering
    cursor below owns that dispatch and engineering deferral semantics are out
    of scope here (fix-E2-22).

    Returns True when the pass ran, so the caller re-reads the checkpoint.
    """
    if getattr(state, "requires_human_approval", False):
        return False
    if state.pending_hitl:
        return False
    if getattr(state, "attended_completed_task_ids", None):
        return False
    if not (getattr(snap, "next", ()) or ()):
        return False
    # P1: under an active plan barrier, a pending engineering task must NOT
    # suppress this bootstrap pass — the planning task needs its
    # dispatch.deferred_to_host marking, and _next_engineering_task knows
    # nothing about plan_status, so without this the workflow would hang
    # waiting on an engineering task the barrier is holding back anyway.
    # Deliberately NOT added to _next_engineering_task itself: this guard is
    # local to the bootstrap-pass suppressor, and filtering inside that
    # predicate would change its meaning for every other caller.
    if _next_engineering_task(state) is not None and not plan_barrier_active(state):
        return False
    emit(project, wf, "attended.no_approval_dispatch_pass", {
        "interrupted_before": list(getattr(snap, "next", ()) or ()),
    })
    try:
        sup.invoke(None, config=config)
    except Exception as e:  # noqa: BLE001 — the attended cursor still proceeds
        emit(project, wf, "attended.dispatch_pass_failed", {"error": str(e)})
        return False
    return True


def _attended_result_record(state: HydraState, res: dict) -> dict | None:
    """Project a terminal attended cursor result into the durable record
    `hydra finalize` later materialises into a squad DECISION_RECORD (E2-30).

    Returns None when the result carries no task_id (nothing to attribute).
    The record is deliberately small and JSON-only: the checkpoint is the
    authoritative store and must stay serialisable.
    """
    tid = res.get("task_id")
    if tid is None:
        return None
    tid = str(tid)
    owner = str(res.get("squad_slug") or "")
    if not owner:
        for t in getattr(state, "tasks", []):
            if str(t.task_id) == tid:
                owner = t.owner_squad
                break
    record: dict[str, object] = {
        "task_id": tid,
        "owner_squad": owner or "engineering",
        "run_id": str(res.get("run_id") or tid),
        "status": str(res.get("status") or "complete"),
        "final_status": str(res.get("final_status") or res.get("status") or ""),
        "stage_id": res.get("stage_id"),
        "summary": (str(res.get("stage_outcome") or "") or None),
        "changed_paths": list(res.get("changed_paths") or [])[:50],
        "cost_usd": float(res.get("cost_usd") or 0.0),
    }
    if isinstance(res.get("artifact_ref"), dict):
        record["artifact_ref"] = res["artifact_ref"]
    if res.get("error"):
        record["error"] = str(res["error"])[:500]
    return record


def _merge_attended_result(existing: list[dict], record: dict | None) -> list[dict]:
    """Upsert an attended result by task_id (last terminal outcome wins)."""
    if record is None:
        return list(existing or [])
    out = [r for r in (existing or []) if str(r.get("task_id")) != record["task_id"]]
    out.append(record)
    return out


def _attended_pending_task_ids(state: HydraState, packs: dict | None = None) -> list[str]:
    """Task ids the attended loop has NOT driven to a terminal outcome.

    A task counts as attended-done when its id is in
    ``attended_completed_task_ids`` (the same signal `_next_engineering_task`
    and `_next_nonengineering_attended_task` honour), OR when the in-graph
    dispatch already carried it to a terminal status.

    P1: a task whose ``plan_revision`` is stale (superseded by a replan) is
    excluded too. ``state.tasks`` is append-only, so without this a
    superseded plan's step tasks would sit "pending" forever and block
    finalize (``tasks_pending``) even though no selector will ever dispatch
    them again. No-op while nothing sets a non-zero ``plan_revision``.
    """
    done = set(getattr(state, "attended_completed_task_ids", []) or [])
    pending: list[str] = []
    for t in getattr(state, "tasks", []):
        tid = str(t.task_id)
        if tid in done:
            continue
        if t.status in ("done", "failed", "cancelled"):
            continue
        if getattr(t, "plan_revision", 0) and t.plan_revision != state.plan_revision:
            continue
        pending.append(tid)
    return pending


def _materialize_attended_results(state: HydraState) -> tuple[list[dict], list[dict]]:
    """Turn attended task records into the state shape `node_synthesis` reads.

    `node_synthesis` groups `state.envelopes` by `origin_squad` and mints one
    MemoryRef per `state.artifacts` entry. Attended tasks never went through
    `node_dispatch`, so neither channel holds their output. This builds, per
    attended result, one squad-origin DECISION_RECORD envelope plus one
    artifact row keyed by the persisted MemoryRef (native pack artifact) or the
    pp run id (engineering stage).

    A task whose `owner_squad` is a RESERVED_META_SQUAD (e.g. "planning") still
    gets its DECISION_RECORD envelope here like any other squad — this
    function does not special-case it. `node_synthesis`'s `origin_squad`
    grouping loop is the one place that excludes RESERVED_META_SQUADS from
    becoming a squad "voice" (the plan is the frame of the record, not a
    voice within it); that single filter covers an envelope regardless of
    which of the two emission points produced it (an ordinary in-graph
    dispatch envelope, or this function).
    """
    from .schemas import DecisionRecord, MemoryRef

    envelopes: list[dict] = []
    artifacts: list[dict] = []
    seen_envelope_tasks = {
        str(e.get("_task_id")) for e in (state.envelopes or []) if e.get("_task_id")
    }
    for rec in (getattr(state, "attended_results", []) or []):
        tid = str(rec.get("task_id"))
        if tid in seen_envelope_tasks:
            continue  # an in-graph / ingest envelope already represents this task
        squad = str(rec.get("owner_squad") or "engineering")
        ref_obj = rec.get("artifact_ref") if isinstance(rec.get("artifact_ref"), dict) else None
        ref_key = str((ref_obj or {}).get("key") or rec.get("run_id") or tid)
        kind = "attended_squad_artifact" if ref_obj else "attended_pp_run"
        status = str(rec.get("final_status") or rec.get("status") or "complete")
        detail = rec.get("summary") or rec.get("error") or ""
        record = DecisionRecord(
            workflow_id=state.workflow_id,
            origin_squad=squad,
            target_squad="hydra",
            decision=(f"Attended {squad} task {tid} finished: {status}"),
            rationale=(
                f"Driven in-context by the attended host loop (run {rec.get('run_id')}). "
                f"outcome={status}; artifact={ref_key}"
                + (f"; detail={str(detail)[:300]}" if detail else "")
            ),
            artifacts=[MemoryRef(tier="episodic", key=ref_key, summary=kind)],
            sealed=(status == "complete"),
        )
        env = record.model_dump(mode="json")
        env["_task_id"] = tid
        env["_attended"] = True
        envelopes.append(env)
        artifacts.append({
            "kind": kind,
            "ref": ref_key,
            "task_id": tid,
            "squad": squad,
            "status": status,
        })
    return envelopes, artifacts


def _resolve_pack_lead_agent(pack) -> str:
    """Resolve the supervisor / lead agent slug for a squad pack.

    Priority: first agent with authority='gatekeeper', then first agent in the
    list, then 'general-purpose' as an absolute fallback.
    """
    if getattr(pack, "entrypoint", None) == "claude-native":
        from .native_packs import native_pack
        return native_pack(pack.slug).qualified_lead_agent
    if getattr(pack, "entrypoint", None) == "claude-skill":
        return _resolve_claude_skill_host_action(pack)["agent_type"]
    spec = _pack_lead_agent_spec(pack)
    return spec.slug if spec is not None else "general-purpose"


def _resolve_pack_cwd(pack, project: Path) -> str:
    """Resolve the on-disk directory for a squad pack.

    Searches the project-local squads/ directory and resolves symlinks so
    marketing packs (which are filesystem symlinks into MarketBliss) return
    their real path.  Falls back to the project root if not found.
    """
    if getattr(pack, "entrypoint", None) == "claude-native":
        from .native_packs import native_pack_root
        return str(native_pack_root(pack.slug))
    if getattr(pack, "entrypoint", None) == "claude-skill":
        # E2-31: squads/<slug>/ is only Hydra's overlay -- the agents, skills and
        # slash command live in the pack's own checkout.
        checkout = _skill_pack_checkout(pack)
        if checkout is not None:
            return str(checkout)
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
    (or the workflow, via `--repo`) targets one.

    Mirrors node_dispatch's precedence (supervisor.py): per-task target_repo_id
    wins, else the workflow-level state.target_repo_id set by intake from
    `--repo`.

    WS1-E: raises `MissingEngineeringTargetError` when neither resolves --
    engineering dispatch must not silently fall back to the Hydra cwd. The
    primary gate is node_planner's HITL (fires before this is ever reached
    for a fresh plan); this is defense-in-depth for a checkpoint written
    before that gate existed."""
    rid = getattr(task, "target_repo_id", None) or getattr(state, "target_repo_id", None)
    if rid:
        from .repo_registry import resolve_repo_project_path
        sub = (getattr(task, "target_repo_subpath", None)
               or getattr(state, "target_repo_subpath", None))
        p = resolve_repo_project_path(rid, sub)
        if sub:
            Path(p).mkdir(parents=True, exist_ok=True)
        return str(p)
    raise MissingEngineeringTargetError(
        "engineering dispatch has no resolved target repo (no --repo/--repos "
        "and no target_repo_id on the task or workflow). This checkpoint "
        "predates the WS1-E target gate, or was resumed after it was "
        "bypassed -- register the intended repo and relaunch with an "
        "explicit --repo/--repos."
    )


class MissingEngineeringTargetError(RuntimeError):
    """Raised by `_resolve_task_project_path` when an engineering task has no
    resolved target repo. WS1-E defense-in-depth: the primary gate is
    node_planner's HITL, which stops a fresh plan before any worktree is cut
    or stage started; this exception covers a checkpoint from before that
    gate existed reaching `hydra attended step` directly."""


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
        print(_cli_json_dumps({"ok": False, "error": f"invalid workflow_id {wf!r}"}),
              file=sys.stderr)
        return 1

    lock_fd, lock_path = _acquire_resume_lock(project, wf)
    if lock_fd is None:
        print(_cli_json_dumps({"ok": False, "status": "resume_in_progress",
                          "lock": str(lock_path)}))
        return 0
    try:
        dispatcher = _attended_live_dispatcher(project, getattr(args, "verbose", False))
        from .supervisor import build_supervisor, _PurePythonRunner
        sup = build_supervisor(project_root=project, dispatcher=dispatcher)
        if isinstance(sup, _PurePythonRunner):
            print(_cli_json_dumps({"ok": False,
                              "error": "langgraph unavailable — attended step requires "
                                       "the checkpointing supervisor"}), file=sys.stderr)
            return 1
        config = {"configurable": {"thread_id": wf}}
        snap = sup.get_state(config)
        if snap is None or not snap.values:
            print(_cli_json_dumps({"ok": False, "error": "not_found",
                              "detail": "no checkpoint — run `hydra plan` first"}),
                  file=sys.stderr)
            return 1
        state = HydraState.model_validate(snap.values)

        # E2-32: no-approval workflows have no `approve` caller to leave the
        # plan_only dispatch interrupt — run that same pass here, once.
        if _run_first_step_dispatch_pass(sup, config, project, wf, snap, state):
            snap = sup.get_state(config)
            if snap is not None and snap.values:
                state = HydraState.model_validate(snap.values)

        # E2-23: pick the next attended task in task-list order.  The squad
        # only decides WHICH cursor is opened (pp run vs squad stage) — it must
        # not reorder the planner's dependency chain.
        packs = _discover(project)
        _sel_task, _sel_kind, _sel_pack = _next_attended_task(state, packs)

        # --- Engineering task (mcp entrypoint) ---
        if _sel_kind == "engineering":
            task = _sel_task
            try:
                project_path = _resolve_task_project_path(task, state, project)
            except MissingEngineeringTargetError as _missing_target_err:
                from .repo_registry import unknown_repo_hitl_fields
                print(_cli_json_dumps({
                    "ok": False,
                    "error": "missing_engineering_target",
                    "detail": str(_missing_target_err),
                    "resolved_target": None,
                    **unknown_repo_hitl_fields("<repo-id>"),
                }, indent=2), file=sys.stderr)
                return 1
            request_text = task.description or state.root_goal

            # F27: preflight — verify ALL THREE agent files exist before
            # staging host_actions that reference them.  If any is absent,
            # surface a clear dependency error rather than a broken host_action.
            _agents_dir = project / "plugins" / "hydra" / "agents"
            _required_agents = {
                "engineer.md": "code generator",
                "judge-cross-vendor.md": "cross-vendor judge",
                "judge-same-vendor.md": "same-vendor judge",
            }
            _missing_agents = [
                name for name in _required_agents
                if not (_agents_dir / name).exists()
            ]
            if _missing_agents:
                print(_cli_json_dumps({
                    "ok": False, "error": "missing_agent_dependency",
                    "detail": (
                        f"attended engineering requires agent stubs: "
                        f"{', '.join(_missing_agents)}. "
                        "Install the Hydra plugin assets under plugins/hydra/agents/ "
                        "to enable "
                        "attended engineering."
                    ),
                    "missing": _missing_agents,
                }), file=sys.stderr)
                return 1

            # RA-12b (attended path): thread Hydra provenance into pp's start_run
            # so every attended run row carries hydra_workflow_id for cost
            # attribution (MU16 gate) and eights provenance writes.  Mirrors
            # squad_node._via_mcp — same optional key names, same semantics.
            _start_run_args: dict = {
                "request_text": request_text,
                "project_path": project_path,
                "mode": "single",
                "hydra_workflow_id": wf,
            }
            _env_id = getattr(task, "envelope_id", None)
            if _env_id is not None:
                _start_run_args["hydra_envelope_id"] = str(_env_id)
            _origin_squad = getattr(task, "origin_squad", None)
            if _origin_squad:
                _start_run_args["hydra_origin_squad"] = str(_origin_squad)
            _env_type = getattr(task, "envelope_type", None)
            if _env_type:
                _start_run_args["hydra_envelope_type"] = str(_env_type)
            start = dispatcher.call_mcp("pp_harness", "start_run",
                                        _start_run_args, squad_id="engineering")
            inner = start.get("result", start) if isinstance(start, dict) else {}
            run_id = (inner or {}).get("run_id") if isinstance(inner, dict) else None
            _hydra_context_block: str | None = (
                (inner or {}).get("hydra_context_block")
                if isinstance(inner, dict) else None
            )
            if not run_id:
                print(_cli_json_dumps({"ok": False, "error": "start_run returned no run_id",
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
            try:
                from .squad_node import ensure_target_repo_ignores, ensure_target_repo_test_excludes
                ensure_target_repo_ignores(project_path)
                ensure_target_repo_test_excludes(project_path)
            except Exception:  # noqa: BLE001
                pass

            # Register the open pp run so postcheck/reap can finalize-abort it
            # if the workflow is abandoned mid-stage (run holds the .harness lock).
            state.open_pp_runs.append({"run_id": str(run_id), "project_path": project_path})

            res = host_bridge.begin_stage(
                dispatcher, workflow_id=wf, run_id=str(run_id),
                project_path=project_path, request_text=request_text,
                model_tier=getattr(task, "model_tier", None),
                project_root=project, task_id=str(task.task_id),
                hydra_context_block=_hydra_context_block,
                # B9: real gate_type derived from the task's triggering
                # envelope, not a hardcoded literal.
                gate_type=_attended_task_gate_type(task, state))

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
            _resolved_target = _build_resolved_target_view(state)
            if _resolved_target is None:
                # Single-target, per-task override (fleet-degrade case etc.) --
                # task.target_repo_id resolved even though state-level fields
                # didn't. Surface it the same way for the step response.
                _tr_id = getattr(task, "target_repo_id", None)
                if _tr_id:
                    _resolved_target = {
                        "mode": "single",
                        "repo_id": _tr_id,
                        "subpath": getattr(task, "target_repo_subpath", None),
                        "source": "task_override",
                    }
            print(_cli_json_dumps({"ok": True, "resolved_target": _resolved_target, **res},
                             indent=2, default=str))
            return 0

        # --- Non-engineering task (claude-skill / agent-impersonation) ---
        # E2-32: a stub squad has no host action to spawn — drive it in-process.
        # Resolved against the E2-23 attended selection in TASK-LIST ORDER:
        # whichever candidate appears first in state.tasks wins, so a stub
        # never jumps the queue and is never skipped forever.
        stub_task, stub_pack = _next_stub_attended_task(state, packs)
        if stub_task is not None and _task_precedes(state, stub_task, _sel_task):
            res = _drive_stub_task(sup, config, project, wf, state,
                                   stub_task, stub_pack)
            print(_cli_json_dumps({"ok": True, **res}, indent=2, default=str))
            return 0

        if _sel_kind == "squad":
            ne_task, ne_pack = _sel_task, _sel_pack
            task_id = str(ne_task.task_id)
            request_text = ne_task.description or state.root_goal
            pack_cwd = _resolve_pack_cwd(ne_pack, project)
            action_extras = None
            if ne_pack.entrypoint == "claude-skill":
                action_extras = _resolve_claude_skill_host_action(ne_pack)
                lead_agent = action_extras.pop("agent_type")
            else:
                lead_agent = _resolve_pack_lead_agent(ne_pack)

            # E2-28: upstream context as HANDLES only (never raw artifact text
            # across a squad boundary) — completed tasks' envelope ids plus the
            # workflow's episodic MemoryRef keys.
            _done_ids = set(getattr(state, "attended_completed_task_ids", []) or [])
            _upstream_refs: list[str] = []
            for _t in getattr(state, "tasks", []):
                if str(_t.task_id) not in _done_ids:
                    continue
                _rid = getattr(_t, "result_envelope_id", None) or getattr(_t, "envelope_id", None)
                if _rid:
                    _upstream_refs.append(f"envelope:{_rid} ({_t.owner_squad})")
            _upstream_refs.extend(
                f"episodic:{k}" for k in (getattr(state, "episodic_refs", []) or []))

            res = host_bridge.begin_squad_stage(
                action_extras=action_extras,
                workflow_id=wf,
                task_id=task_id,
                squad_slug=ne_pack.slug,
                entrypoint=ne_pack.entrypoint,
                lead_agent=lead_agent,
                pack_cwd=pack_cwd,
                request_text=request_text,
                project_root=project,
                goal=state.root_goal,
                envelope_id=(str(ne_task.envelope_id)
                             if getattr(ne_task, "envelope_id", None) else None),
                upstream_refs=_upstream_refs,
                budget_usd=state.budget.budget_usd,
                budget_remaining_usd=state.budget.usd_remaining,
                risk=("high" if getattr(state, "requires_human_approval", False)
                      else "normal"),
                priority=getattr(ne_task, "priority", None),
                acceptance_criteria=getattr(ne_task, "acceptance_criteria", None),
            )
            emit(project, wf, "attended.step", {
                "run_id": task_id,
                "task_id": task_id,
                "squad_slug": ne_pack.slug,
                "state": res.get("state"),
            })
            print(_cli_json_dumps({"ok": True, **res}, indent=2, default=str))
            return 0

        # P1: awaiting_plan_approval — a plan-gate HITL is open. Distinct from
        # ready_to_finalize so the host waits on /hydra:approve instead of
        # calling finalize against an unapproved plan. No-op unless something
        # files a pending_hitl with gate_node == "plan_gate".
        _pending_hitl = getattr(state, "pending_hitl", None)
        if isinstance(_pending_hitl, dict) and _pending_hitl.get("gate_node") == "plan_gate":
            print(_cli_json_dumps({"ok": True, "status": "awaiting_plan_approval",
                              "pending_hitl": _pending_hitl,
                              "workflow_id": wf}, indent=2, default=str))
            return 0

        # P1: blocked_on_failed_dependency — at least one not-done task has
        # unsatisfied dependencies and nothing else is selectable. Without
        # this distinct terminal, the host would see ready_to_finalize, call
        # finalize, get tasks_pending back, and silently drop half a plan.
        # No-op unless a task carries a depends_on that never resolves.
        _blocked_deps = [
            str(t.task_id) for t in getattr(state, "tasks", [])
            if getattr(t, "status", None) not in ("done", "failed", "cancelled")
            and str(t.task_id) not in set(getattr(state, "attended_completed_task_ids", []) or [])
            and not plan_deps_satisfied(state, t)
        ]
        if _blocked_deps:
            print(_cli_json_dumps({"ok": True, "status": "blocked_on_failed_dependency",
                              "blocked_task_ids": _blocked_deps,
                              "workflow_id": wf}, indent=2, default=str))
            return 0

        # No pending tasks of any kind. E2-30: this is not the end of the
        # workflow — the attended results still have to go through
        # synthesis/judge_synthesis/postcheck. Tell the host to call
        # `hydra finalize` (status), keeping `no_pending_task` as a
        # compatibility alias for hosts pinned to the old contract.
        print(_cli_json_dumps({"ok": True, "status": "ready_to_finalize",
                          "no_pending_task": True,
                          "next_action": "hydra.workflow.finalize",
                          "workflow_id": wf}))
        return 0
    finally:
        _release_resume_lock(lock_fd, lock_path)


def _cmd_recover_stalled_stage(args, project: Path, wf: str, option) -> int:
    """``hydra resume <workflow_id> --action recover-stalled-stage --option <run_id>``.

    W2-4: the sanctioned entry point for ``host_bridge.recover_stalled_stage``.
    ``--option`` carries the stranded attended cursor's ``run_id`` (the same
    id ``hydra attended step`` printed as ``run_id`` when the stage began).
    Mirrors ``_cmd_attended_submit``'s post-terminal budget-charge block so a
    recovered stage is charged exactly once: ``already_charged`` (read off the
    cursor's persisted ``charged`` flag) gates the charge exactly as it does
    for a normal retried submit.
    """
    from . import host_bridge
    if not option:
        print(_cli_json_dumps({"ok": False,
                          "error": "recover-stalled-stage needs --option <run_id>"}),
              file=sys.stderr)
        return 1
    run_id = str(option)
    dispatcher = _attended_live_dispatcher(project, getattr(args, "verbose", False))
    cfile = host_bridge.cursor_path(project, wf, run_id)
    if not Path(cfile).exists():
        print(_cli_json_dumps({"ok": False, "error": "cursor_not_found", "detail": str(cfile)}),
              file=sys.stderr)
        return 1

    res = host_bridge.recover_stalled_stage(dispatcher, cursor_file=cfile)
    if not res.get("ok", True):
        print(_cli_json_dumps(res, indent=2, default=str), file=sys.stderr)
        return 1

    if res.get("status") in ("complete", "surfaced", "aborted") and not res.get("already_charged"):
        host_bridge.mark_charged(cfile)
        from .governance import charge_and_gate
        from .supervisor import build_supervisor, _PurePythonRunner
        sup = build_supervisor(project_root=project, dispatcher=dispatcher)
        if not isinstance(sup, _PurePythonRunner):
            config = {"configurable": {"thread_id": wf}}
            snap = sup.get_state(config)
            if snap is not None and snap.values:
                state = HydraState.model_validate(snap.values)
                cost = float(res.get("cost_usd") or 0.0)
                toks = int(res.get("tokens_in") or 0) + int(res.get("tokens_out") or 0)
                # B8: host_bridge tags the stage's cost provenance in
                # "cost_source" ("measured"/"estimated"/"unmeasured") — an
                # unreporting host no longer charges as free.
                cost_source = str(res.get("cost_source") or "measured")
                # Fix (mixed-provenance estimated_usd): "cost_source" is a
                # single collapsed label for the whole (possibly mixed)
                # stage, so it cannot be used to size the estimated_usd
                # credit for a stage that mixed a measured and an estimated
                # component. Pass the per-component figure host_bridge
                # tracked separately instead.
                estimated_component = float(res.get("estimated_cost_usd") or 0.0)
                block, downgrade = charge_and_gate(
                    state, cost, toks, source=cost_source,
                    estimated_usd=estimated_component,
                )
                if cost_source == "unmeasured":
                    emit(project, wf, "attended.cost_unmeasured",
                         {"stage_id": res.get("stage_id"), "run_id": res.get("run_id")})
                tid = res.get("task_id")
                completed = list(state.attended_completed_task_ids)
                if tid is not None and str(tid) not in completed:
                    completed.append(str(tid))
                done_ids = list(getattr(state, "attended_done_task_ids", []) or [])
                if (res.get("status") == "complete" and tid is not None
                        and str(tid) not in done_ids):
                    done_ids.append(str(tid))
                open_runs = [e for e in state.open_pp_runs
                             if e.get("run_id") != res.get("run_id")]
                res["budget_block"] = block
                res["budget_downgrade"] = downgrade
                res["spent_usd"] = state.budget.spent_usd
                attended_results = _merge_attended_result(
                    state.attended_results, _attended_result_record(state, res))
                try:
                    sup.update_state(config, {
                        "attended_completed_task_ids": completed,
                        "attended_done_task_ids": done_ids,
                        "attended_results": attended_results,
                        "open_pp_runs": open_runs,
                        "budget": state.budget.model_dump(mode="json"),
                        "budget_downgrade_active": bool(downgrade),
                    })
                except Exception as e:  # noqa: BLE001
                    emit(project, wf, "attended.persist_failed", {"error": str(e)})

    emit(project, wf, "attended.recovery.resume",
         {"run_id": run_id, "status": res.get("status")})
    print(_cli_json_dumps({"ok": True, **res}, indent=2, default=str))
    return 0


def _reenter_graph_after_dispatch(
    sup: Any, config: dict, patch: dict[str, object], *, max_iterations: int = 6,
    target_next: tuple[str, ...] = ("plan_gate",), gate_only: bool = False,
) -> list[str]:
    """P5b Task 3: re-enter the compiled graph as if `dispatch` just finished.

    `hydra_core.ingest` never calls `build_supervisor`/`update_state`/`invoke`
    itself (see its module docstring) — setting `plan_status="drafted"` on the
    checkpoint alone cannot reach `plan_judge`, because `after_dispatch` is a
    conditional edge evaluated only when the `dispatch` node finishes.
    `as_node="dispatch"` makes the graph believe dispatch just finished so
    that edge fires; the loop then drives `invoke(None)` until the graph
    parks at ``target_next`` or has nothing left to run (``next`` empty).

    Bounded at ``max_iterations`` — mirrors `_cmd_finalize`'s identical
    `for _ in range(6)` idiom. A stuck graph (a routing bug that never
    reaches ``target_next`` and never empties ``next``) must be a loud,
    bounded no-op here, not a hang.

    Returns the final ``next`` tuple as a list (JSON-friendly), for the
    caller to report back to the operator.

    ``gate_only`` (RESOLVE-GATE-ONLY, resume --gate-only's modify-plan branch):
    the ``update_state(..., as_node="dispatch")`` call is a pure checkpoint
    mutation -- it stamps the new plan task/revision onto the state but does
    NOT execute any graph node. The ``sup.invoke(None, ...)`` calls in the loop
    below are what actually re-enter the graph (running `after_dispatch` and
    whatever it routes to). When ``gate_only`` is set, apply the state
    mutation and return immediately without ever invoking -- the host's
    step/submit loop picks up the newly-authored plan-revision task from its
    own cursor exactly like a fresh planner task.
    """
    sup.update_state(config, patch, as_node="dispatch")
    if gate_only:
        return list(getattr(sup.get_state(config), "next", None) or [])
    for _ in range(max_iterations):
        parked_at = getattr(sup.get_state(config), "next", None)
        if not parked_at or tuple(parked_at) == target_next:
            break
        sup.invoke(None, config=config)
    return list(getattr(sup.get_state(config), "next", None) or [])


def _ingest_item_should_release_claim(item: Any) -> bool:
    """Whether a `dispatch_ingested_envelopes` item result means the claimed
    envelope_id (the dedup ledger claim `_cmd_attended_submit` AND
    `_cmd_ingest_locked` both take before dispatching) must be released so a
    retry under the SAME id is possible, rather than being silently skipped
    forever as `skipped_duplicate`. The ONE decision function both callers
    use — P5b revise round item 3 found a hand-duplicated second copy in
    `_cmd_ingest_locked` that had drifted out of sync with this one (see that
    call site's comment); do not let a THIRD copy happen — extend this
    function, never inline a new condition at a call site.

    Three cases release the claim: the envelope never reached a squad
    because no delegation target exists (`unknown_target`); it failed
    schema validation (`failed` WITH structured `errors`) (E2-34) -- a bare
    `failed` with no structured errors does NOT qualify, because that can be
    a post-`start_run` drive-loop abort that already registered an open pp
    run, and un-claiming it would make an at-most-once dispatch
    re-dispatchable (a double run); or a PLAN was refused because
    `HYDRA_PLAN_PHASE` is off (`plan_phase_disabled`, P5b revise round item
    2) -- this last one matters most at the exact moment the flag flips ON
    and the operator resubmits, under the same id, the plan that was just
    refused. Every other status (done/drafted/deferred_to_host/
    skipped_duplicate/surfaced/running) keeps the claim, because the
    envelope genuinely reached (or is queued for) real work.
    """
    return (
        item.status == "unknown_target"
        or item.status == "plan_phase_disabled"
        or (item.status == "failed" and bool(item.errors))
    )


def _apply_plan_reentry(
    sup: Any, config: dict, project: Path, wf: str,
    plan_reentry_patch: dict[str, object], plan_reentry_envelope_id: str | None,
    res: dict[str, object], *, emit_fn: Any, release_fn: Any,
) -> None:
    """Drive `_reenter_graph_after_dispatch` and report the OUTCOME, not an
    optimistic guess, into `res` (mutated in place).

    Cross-vendor judge finding (P5b revise round): `_reenter_graph_after_
    dispatch`'s very first statement is `sup.update_state(...)`, which is
    fallible. The envelope_id was already claimed in the dedup ledger by the
    time this runs (`_cmd_attended_submit`'s per-envelope loop, above), so on
    failure the checkpoint may never have advanced while the id stays
    claimed — a retry would be silently skipped as `skipped_duplicate` and
    the plan would become permanently unreachable with no error surfaced
    anywhere. Follow the two patterns `_cmd_attended_submit` already uses for
    exactly this shape of problem instead of inventing a third: release the
    claim (mirrors the unknown_target/failed release in the per-envelope
    loop) and flip a caller-visible top-level status (mirrors
    `envelopes_rejected` below it) rather than reporting the optimistic
    "drafted" set before the fallible call ran.
    """
    try:
        parked_at = _reenter_graph_after_dispatch(sup, config, plan_reentry_patch)
    except Exception as exc:  # noqa: BLE001
        emit_fn(project, wf, "attended.plan_reentry_failed", {"error": str(exc)})
        if plan_reentry_envelope_id is not None:
            release_fn(project, wf, [plan_reentry_envelope_id])
        res["status"] = "plan_reentry_failed"
        res["plan_status"] = "plan_reentry_failed"
        res["plan_reentry_error"] = str(exc)
    else:
        res["plan_status"] = plan_reentry_patch.get("plan_status")
        res["plan_parked_at"] = parked_at


def _apply_rejected_envelopes(
    res: dict[str, object], rejected: list[dict[str, object]], *,
    record_fn: Any, emit_fn: Any, project: Path, wf: str, cfile: Any, run_id: str,
) -> None:
    """Surface a batch's schema-rejected delegation envelopes (mutates `res`
    in place). The engineering task itself stays attended-complete (it is
    already in `attended_done_task_ids`); what is NOT complete is the
    delegation it emitted, so this parks it on the cursor for `step`/
    `finalize` to render and always records it under `res["rejected_envelopes"]`.

    P5b revise round item 1: `res["status"]` used to be overwritten
    UNCONDITIONALLY to `"envelopes_rejected"` here, which clobbered a
    `"plan_reentry_failed"` status `_apply_plan_reentry` may have just set
    when the SAME batch also carried a PLAN whose re-entry raised.
    `plan_status`/`plan_reentry_error` survive under their own keys either
    way, but a caller that branches only on the single top-level `status`
    field would be told "envelopes_rejected" and act on that alone, never
    learning the checkpoint may not have advanced. `rejected_envelopes` is
    always recorded regardless of which status wins, so that signal is never
    lost — only the single top-level `status` string has to pick one.
    Deliberate choice: a failed plan re-entry wins, because it can leave the
    graph checkpoint mid-transition with a claimed-but-unreachable envelope
    id, which is a worse-to-miss failure than a rejected delegation (already
    safely un-claimed and retryable on its own).
    """
    res["rejected_envelopes"] = rejected
    record_fn(cfile, rejected)
    emit_fn(project, wf, "attended.envelopes_rejected", {
        "run_id": run_id, "rejected_count": len(rejected),
    })
    if res.get("status") != "plan_reentry_failed":
        res["status"] = "envelopes_rejected"


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
        print(_cli_json_dumps({"ok": False, "error": f"invalid workflow_id {wf!r}"}),
              file=sys.stderr)
        return 1
    try:
        result = json.loads(Path(args.result).read_text(encoding="utf-8"))
        if not isinstance(result, dict):
            raise ValueError("result file must be a JSON object")
        # Cross-vendor judge finding (this round, HIGH): this is the INPUT
        # boundary for an untrusted host-subagent result (e.g. `cost_usd`,
        # priced downstream via `_priced_cost` into `float(reported)` with
        # no finiteness check of its own). `json.loads` here ACCEPTS a bare
        # NaN/Infinity token (Python's json module is permissive by
        # default), and everything downstream of this parse --
        # `host_bridge.submit_host_result` -> `_apply_generate` ->
        # `record_attempt` (a pp-harness LEDGER side effect) -- runs BEFORE
        # `submit_host_result`'s own `save_cursor` (which IS strict) is ever
        # reached. Rejecting here, before the lock is even acquired and
        # before `submit_host_result` is called at all, means no ledger
        # call and no cursor write can happen with a poisoned cost. Reuses
        # the same `find_non_finite_field` walker `_cmd_attended_finalize`
        # already uses for the analogous attended-result boundary above,
        # rather than inventing a second non-finite check.
        from .strict_json import find_non_finite_field
        bad_field = find_non_finite_field(result)
        if bad_field is not None:
            raise ValueError(
                f"--result contains a non-finite value at {bad_field}; "
                "refusing before any ledger/cursor side effect"
            )
        # Cross-vendor judge finding (follow-up round, HIGH): the walk above
        # only recognizes an actual `float` NaN/Infinity -- a JSON STRING
        # like `"cost_usd": "NaN"` is ordinary, valid JSON (not a defect in
        # itself) and is invisible to it, yet is still coerced to a real
        # non-finite float downstream by `host_bridge._priced_cost`'s
        # `coerce_untrusted_cost` cast and `_apply_generate`/`_apply_judge`'s
        # token accumulation. This check is COMPLEMENTARY, not redundant:
        # `find_non_finite_field` catches a genuine float NaN/Infinity
        # before any side effect; this one catches a string that only
        # BECOMES one at the cast, by validating the value the SAME way the
        # cast site now does, at the same coercion-first-then-check
        # boundary, before the resume lock or `submit_host_result` (and, in
        # turn, the ledger) is ever reached.
        # Cross-vendor judge finding (follow-up round, HIGH): the walk above
        # only recognizes an actual `float` NaN/Infinity -- a JSON STRING
        # like `"cost_usd": "NaN"` is ordinary, valid JSON (not a defect in
        # itself) and is invisible to it, yet is still coerced to a real
        # non-finite float downstream by `host_bridge._priced_cost`'s
        # `coerce_untrusted_cost` cast and `_apply_generate`/`_apply_judge`'s
        # token accumulation. This check is COMPLEMENTARY, not redundant:
        # `find_non_finite_field` catches a genuine float NaN/Infinity
        # before any side effect; this one catches a string that only
        # BECOMES one at the cast, by validating the value the SAME way the
        # cast site now does, at the same coercion-first-then-check
        # boundary, before the resume lock or `submit_host_result` (and, in
        # turn, the ledger) is ever reached.
        from .squad_node import coerce_untrusted_cost
        if result.get("cost_usd") is not None:
            _, _cost_src = coerce_untrusted_cost(result["cost_usd"])
            if _cost_src == "unmeasured":
                raise ValueError(
                    f"--result.cost_usd {result['cost_usd']!r} does not coerce "
                    "to a finite number; refusing before any ledger/cursor "
                    "side effect"
                )
        for _tok_field in ("tokens_in", "tokens_out"):
            _raw_tok = result.get(_tok_field)
            if _raw_tok is not None:
                _, _tok_src = coerce_untrusted_cost(_raw_tok)
                if _tok_src == "unmeasured":
                    raise ValueError(
                        f"--result.{_tok_field} {_raw_tok!r} does not coerce "
                        "to a finite number; refusing before any "
                        "ledger/cursor side effect"
                    )
    except (OSError, ValueError, json.JSONDecodeError) as e:
        print(_cli_json_dumps({"ok": False, "error": f"could not read --result: {e}"}),
              file=sys.stderr)
        return 1

    lock_fd, lock_path = _acquire_resume_lock(project, wf)
    if lock_fd is None:
        print(_cli_json_dumps({"ok": False, "status": "resume_in_progress",
                          "lock": str(lock_path)}))
        return 0
    try:
        dispatcher = _attended_live_dispatcher(project, getattr(args, "verbose", False))
        cfile = host_bridge.cursor_path(project, wf, str(args.run_id))
        if not Path(cfile).exists():
            print(_cli_json_dumps({"ok": False, "error": "cursor_not_found",
                              "detail": str(cfile)}), file=sys.stderr)
            return 1
        res = host_bridge.submit_host_result(
            dispatcher, cursor_file=cfile, call_key=str(args.call_key), result=result)

        # On terminal: charge budget on the authoritative HydraState ledger and
        # record the task outcome into the checkpoint.
        # Rider (b): skip charge if already_charged=True (idempotency guard).
        # E2-35: "complete_unpersisted" is terminal too — the pack agent ran and
        # spent real money, so it must charge like any other terminal outcome.
        if res.get("status") in ("complete", "complete_unpersisted",
                                 "surfaced", "aborted"):
            if res.get("already_charged"):
                # Idempotent re-submit: cursor was already charged on the first
                # terminal submit.  Return the cached result without re-billing.
                emit(project, wf, "attended.submit",
                     {"run_id": str(args.run_id), "call_key": str(args.call_key),
                      "status": res.get("status"), "already_charged": True})
                print(_cli_json_dumps({"ok": True, **res}, indent=2, default=str))
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
            # The host returns the native pack artifact as text. Persist it only
            # through the declared pack output root; the helper rejects escapes
            # and non-text artifacts before producing the MemoryRef returned to
            # the operator and telemetry.
            squad_slug = str(res.get("squad_slug") or "")
            artifact_text = str(res.get("artifact_text") or "")
            # E2-35: host_bridge already persisted non-native squad artifacts
            # (generic store + claude-skill shim) and recorded the outcome on
            # the cursor. Only squads it left alone — the native packs — reach
            # the native output-root writer here.
            already_handled = (res.get("artifact_ref") is not None
                               or res.get("artifact_persist_error") is not None)
            if squad_slug and artifact_text and not already_handled:
                try:
                    from .artifact_store import write_native_artifact
                    ref = write_native_artifact(
                        squad_slug, f"attended/{res.get('task_id') or args.run_id}.md",
                        artifact_text,
                    )
                    res["artifact_ref"] = ref.model_dump(mode="json")
                    emit(project, wf, "attended.native_artifact_persisted", {
                        "squad_slug": squad_slug, "memory_ref": ref.key,
                    })
                except (ValueError, OSError) as exc:
                    res["artifact_persist_error"] = str(exc)
                    emit(project, wf, "attended.native_artifact_persist_failed", {
                        "squad_slug": squad_slug, "error": str(exc),
                    })
            if not isinstance(sup, _PurePythonRunner):
                config = {"configurable": {"thread_id": wf}}
                snap = sup.get_state(config)
                if snap is not None and snap.values:
                    state = HydraState.model_validate(snap.values)
                    cost = float(res.get("cost_usd") or 0.0)
                    toks = int(res.get("tokens_in") or 0) + int(res.get("tokens_out") or 0)
                    # B8: see the recover-stalled-stage twin above — an
                    # unreporting host is estimated/unmeasured, never free.
                    cost_source = str(res.get("cost_source") or "measured")
                    # Fix (mixed-provenance estimated_usd): see the twin
                    # comment above — credit only the per-component estimated
                    # figure, not the whole (possibly mixed) stage total.
                    estimated_component = float(res.get("estimated_cost_usd") or 0.0)
                    block, downgrade = charge_and_gate(
                        state, cost, toks, source=cost_source,
                        estimated_usd=estimated_component,
                    )
                    if cost_source == "unmeasured":
                        emit(project, wf, "attended.cost_unmeasured",
                             {"stage_id": res.get("stage_id"), "run_id": res.get("run_id")})
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
                    # MU15: record complete-only outcomes in attended_done_task_ids
                    # so enforce_governance can skip the deferred_to_host / surfaced
                    # check for tasks the host successfully drove to completion.
                    # Only 'complete' enters this list — surfaced/aborted outcomes
                    # intentionally stay out so governance still surfaces those.
                    done_ids = list(getattr(state, "attended_done_task_ids", []) or [])
                    if (res.get("status") == "complete"
                            and tid is not None
                            and str(tid) not in done_ids):
                        done_ids.append(str(tid))
                    open_runs = [e for e in state.open_pp_runs
                                 if e.get("run_id") != res.get("run_id")]
                    res["budget_block"] = block
                    res["budget_downgrade"] = downgrade
                    res["spent_usd"] = state.budget.spent_usd
                    # E2-30: persist the attended outcome so `hydra finalize`
                    # can materialise it into a squad result envelope for
                    # node_synthesis (the in-graph dispatch never ran here).
                    attended_results = _merge_attended_result(
                        state.attended_results, _attended_result_record(state, res))
                    try:
                        sup.update_state(config, {
                            "attended_completed_task_ids": completed,
                            "attended_done_task_ids": done_ids,
                            "attended_results": attended_results,
                            "open_pp_runs": open_runs,
                            "budget": state.budget.model_dump(mode="json"),
                            "budget_downgrade_active": bool(downgrade),
                        })
                    except Exception as e:  # noqa: BLE001
                        emit(project, wf, "attended.persist_failed", {"error": str(e)})

                    # A native pack may emit typed work for a sibling squad.
                    # Route it through the same boundary validation, redaction,
                    # claim-before-dispatch ledger, and checkpoint persistence
                    # used by `hydra workflow submit-envelopes`.  This closes
                    # the attended-host gap without creating an ungoverned
                    # direct Agent fan-out.
                    emitted = res.get("emitted_envelopes") or []
                    if emitted:
                        from .ingest import (
                            claim_ingested_ids,
                            dispatch_ingested_envelopes,
                            load_ingested_ids,
                            normalize_for_ingest,
                            release_ingested_ids,
                        )
                        packs = discover_squads(project)
                        if hasattr(dispatcher, "set_squad_packs"):
                            dispatcher.set_squad_packs(packs)
                        outcomes: list[dict[str, object]] = []
                        # E2-34: envelopes that never reached a squad because
                        # they failed schema validation. A non-empty list flips
                        # the top-level status to "envelopes_rejected" so the
                        # delegation is never dropped inside a "complete".
                        rejected: list[dict[str, object]] = []
                        # P5b Task 3: the state patch a PLAN item produced
                        # (set by dispatch_ingested_envelopes on
                        # outcome.plan_patch), applied via the graph re-entry
                        # idiom AFTER this loop. Last-one-wins is fine — a
                        # single attended submit ingesting more than one PLAN
                        # is not a real scenario the host produces.
                        plan_reentry_patch: dict[str, object] | None = None
                        plan_reentry_envelope_id: str | None = None
                        processed = load_ingested_ids(project, wf)
                        for raw in emitted:
                            if not isinstance(raw, dict):
                                bad = {"status": "failed", "detail": "non-object envelope",
                                       "errors": [{"field": "", "msg": "non-object envelope"}]}
                                outcomes.append(bad)
                                rejected.append(bad)
                                emit(project, wf, "ingest.invalid_envelope",
                                     {"envelope_id": None, "type": None,
                                      "errors": bad["errors"]})
                                continue
                            # E2-34: normalize BEFORE reading the id. A pack may
                            # omit `id` or use a non-UUID label; keying dedup on
                            # the raw value would let such an envelope bypass
                            # `processed` and dispatch twice.
                            try:
                                raw = normalize_for_ingest(
                                    raw,
                                    lambda event, payload: emit(project, wf, event, payload),
                                )
                            except ValueError as exc:
                                # b1baf30 revise round item 6: a pack-supplied
                                # budget_usd that could not be converted to a
                                # finite float. Real failed item, not a crash.
                                bad_id = raw.get("id")
                                bad_errors = [{"field": "budget_usd", "msg": str(exc)}]
                                bad = {"envelope_id": str(bad_id) if bad_id is not None else "?",
                                       "status": "failed", "detail": f"invalid envelope: {exc}",
                                       "errors": bad_errors}
                                outcomes.append(bad)
                                rejected.append(bad)
                                emit(project, wf, "ingest.invalid_envelope",
                                     {"envelope_id": bad.get("envelope_id"),
                                      "type": raw.get("type"), "errors": bad_errors})
                                continue
                            envelope_id = raw.get("id")
                            if envelope_id is not None and str(envelope_id) in processed:
                                outcomes.append({"envelope_id": str(envelope_id),
                                                 "status": "skipped_duplicate"})
                                continue
                            if envelope_id is not None:
                                claim_ingested_ids(project, wf, [str(envelope_id)])
                            outcome = dispatch_ingested_envelopes(
                                state, [raw], packs=packs, dispatcher=dispatcher,
                                already_ingested=processed,
                                emit_fn=lambda event, payload: emit(project, wf, event, payload),
                            )
                            item = outcome.items[-1] if outcome.items else None
                            if envelope_id is not None and item is not None:
                                # A schema-rejected envelope never reached a
                                # squad, so un-claim it: the host can re-submit
                                # a corrected envelope under the same id without
                                # being suppressed as a duplicate (E2-34).
                                if _ingest_item_should_release_claim(item):
                                    release_ingested_ids(project, wf, [str(envelope_id)])
                                else:
                                    processed.add(str(envelope_id))
                            try:
                                sup.update_state(config, {
                                    "tasks": outcome.new_tasks,
                                    "envelopes": outcome.new_envelopes,
                                    "open_pp_runs": state.open_pp_runs,
                                    "budget": state.budget.model_dump(mode="json"),
                                })
                            except Exception as exc:  # noqa: BLE001
                                emit(project, wf, "attended.emitted_persist_failed",
                                     {"error": str(exc)})
                            if outcome.plan_patch:
                                plan_reentry_patch = dict(outcome.plan_patch)
                                # Tracked separately from `processed`/the
                                # ledger so a re-entry failure below can
                                # release exactly this claim without touching
                                # any other envelope_id this loop processed.
                                plan_reentry_envelope_id = (
                                    str(envelope_id) if envelope_id is not None else None
                                )
                            outcomes.extend(vars(it) for it in outcome.items)
                            rejected.extend(vars(it) for it in outcome.rejected)
                        res["ingest"] = outcomes
                        if plan_reentry_patch:
                            _apply_plan_reentry(
                                sup, config, project, wf,
                                plan_reentry_patch, plan_reentry_envelope_id, res,
                                emit_fn=emit, release_fn=release_ingested_ids,
                            )
                        if rejected:
                            _apply_rejected_envelopes(
                                res, rejected,
                                record_fn=host_bridge.record_rejected_envelopes,
                                emit_fn=emit, project=project, wf=wf,
                                cfile=cfile, run_id=str(args.run_id),
                            )

        emit(project, wf, "attended.submit", {"run_id": str(args.run_id),
                                              "call_key": str(args.call_key),
                                              "status": res.get("status")})
        print(_cli_json_dumps({"ok": True, **res}, indent=2, default=str))
        return 0
    finally:
        _release_resume_lock(lock_fd, lock_path)


def _cmd_finalize(args) -> int:
    """``hydra finalize <workflow_id>`` — close an attended workflow properly.

    E2-30: the attended loop (`hydra step` / `hydra submit-host-result`) marks
    tasks attended-done but never re-enters the graph, so an interactive
    workflow died at `phase="synthesis"` with no engine DECISION_RECORD, no
    judge_synthesis verdict, no postcheck governance pass and no RA-8 episodic
    row. This command is the missing leg: it materialises the attended results
    into the state shape `node_synthesis` expects and resumes the graph so
    `synthesis -> judge_synthesis -> postcheck` run over real squad output.

    Contract:
      * any task still pending -> ``{ok:false, status:"tasks_pending", pending}``
      * already finalized      -> ``{ok:true, status:"already_finalized",
                                     decision_record_id}`` (idempotent)
      * otherwise              -> ``{ok:true, status:"finalized",
                                     decision_record_id, phase, artifact_refs}``
    """
    project = Path(args.project) if args.project else Path.cwd()
    wf = str(args.workflow_id)
    if not _WORKFLOW_ID_RE.match(wf):
        print(_cli_json_dumps({"ok": False, "error": f"invalid workflow_id {wf!r}"}),
              file=sys.stderr)
        return 1

    lock_fd, lock_path = _acquire_resume_lock(project, wf)
    if lock_fd is None:
        print(_cli_json_dumps({"ok": False, "status": "resume_in_progress",
                          "lock": str(lock_path)}))
        return 0
    try:
        from .supervisor import build_supervisor, _PurePythonRunner
        dispatcher = _attended_live_dispatcher(project, getattr(args, "verbose", False))
        sup = build_supervisor(project_root=project, dispatcher=dispatcher)
        if isinstance(sup, _PurePythonRunner):
            print(_cli_json_dumps({
                "ok": False,
                "error": "langgraph unavailable — finalize requires the checkpointing supervisor",
            }), file=sys.stderr)
            return 1
        config = {"configurable": {"thread_id": wf}}
        snap = sup.get_state(config)
        if snap is None or not snap.values:
            print(_cli_json_dumps({"ok": False, "workflow_id": wf, "error": "not_found"}),
                  file=sys.stderr)
            return 1
        try:
            state = HydraState.model_validate(snap.values)
        except Exception as e:  # noqa: BLE001
            print(_cli_json_dumps({"ok": False, "workflow_id": wf,
                              "error": f"checkpoint_invalid: {e}"}), file=sys.stderr)
            return 1

        # Idempotent: a second call never re-synthesizes (that would duplicate
        # the episodic rows RA-8 writes inside node_synthesis).
        if state.attended_finalized_record_id:
            print(_cli_json_dumps({
                "ok": True, "status": "already_finalized", "workflow_id": wf,
                "decision_record_id": state.attended_finalized_record_id,
                "phase": state.phase,
            }, indent=2, default=str))
            return 0

        pending = _attended_pending_task_ids(state)
        if pending:
            print(_cli_json_dumps({
                "ok": False, "status": "tasks_pending", "workflow_id": wf,
                "pending": pending,
                "detail": ("attended tasks still open — drive them with "
                           "`hydra step` / `hydra submit-host-result` first"),
            }, indent=2, default=str))
            return 0

        envelopes, artifacts = _materialize_attended_results(state)
        patch: dict[str, object] = {
            "phase": "synthesis",
            "pending_hitl": None,
            "hitl_return_node": None,
        }
        if envelopes:
            patch["envelopes"] = envelopes
        if artifacts:
            patch["artifacts"] = artifacts
        # Cross-vendor judge finding (this round, item 2 HIGH): `as_node=
        # "judge_per_squad"` below re-enters the graph via `after_judge_per_
        # squad`'s conditional edge WITHOUT ever running `node_judge_per_
        # squad` -- so its unconditional non-finite scan (over `state.
        # envelopes`/`state.verdicts`) never executes for the envelopes/
        # artifacts this function just materialized from attended results.
        # A non-finite value injected here (e.g. via a corrupted attended
        # result payload) would reach `synthesis` untouched; `synthesis`'s
        # own strict-serialization failure is caught and REDACTED further
        # downstream, so a fresh, finite DecisionRecord silently replaces
        # the broken data instead of surfacing it. Scan the exact payload
        # about to be checkpointed, here, before the mutation, and refuse
        # with the same field-naming shape `node_judge_per_squad` uses.
        from .strict_json import find_non_finite_field
        bad_field = find_non_finite_field({"envelopes": envelopes, "artifacts": artifacts})
        if bad_field is not None:
            print(_cli_json_dumps({
                "ok": False, "status": "unjudgeable", "workflow_id": wf,
                "field": bad_field,
                "detail": (
                    "attended result data contains a non-finite value at "
                    f"{bad_field}; refusing to finalize into synthesis. Fix "
                    "the offending attended result at source and re-run "
                    "finalize."
                ),
            }, indent=2, default=str))
            return 0

        emit(project, wf, "finalize.materialized", {
            "envelopes": len(envelopes), "artifacts": len(artifacts),
            "attended_results": len(state.attended_results or []),
        })
        # as_node="judge_per_squad": its conditional edge routes to `synthesis`
        # for a non-surfaced phase, so the graph re-enters exactly where the
        # attended loop left off.
        sup.update_state(config, patch, as_node="judge_per_squad")

        # `synthesis` and `judge_synthesis` are interrupt_before nodes; drive
        # the tail deterministically until the graph has no next task (END) or
        # it parks on a HITL gate.
        for _ in range(6):
            cur = sup.get_state(config)
            if not getattr(cur, "next", None):
                break
            sup.invoke(None, config=config)
            after = sup.get_state(config)
            if (after.values or {}).get("pending_hitl") and                     (after.values or {}).get("phase") == "surfaced":
                break

        final_snap = sup.get_state(config)
        final_state = HydraState.model_validate(final_snap.values)
        record = next(
            (e for e in reversed(final_state.envelopes)
             if e.get("type") == "DECISION_RECORD" and e.get("origin_squad") == "hydra"),
            None,
        )
        record_id = str((record or {}).get("id") or "")
        if record_id:
            try:
                sup.update_state(config, {"attended_finalized_record_id": record_id})
            except Exception as e:  # noqa: BLE001
                emit(project, wf, "finalize.persist_failed", {"error": str(e)})
        emit(project, wf, "finalize.complete", {
            "decision_record_id": record_id or None,
            "phase": final_state.phase,
        })
        payload = {
            "ok": bool(record_id),
            "status": "finalized" if record_id else "no_decision_record",
            "workflow_id": wf,
            "decision_record_id": record_id or None,
            "phase": final_state.phase,
            "artifact_refs": [a.get("key") for a in ((record or {}).get("artifacts") or [])],
            "dissent_count": len((record or {}).get("dissenting_opinions") or []),
            "sealed": (record or {}).get("sealed"),
            "pending_hitl": final_state.pending_hitl,
            "trace": str(trace_path(project, wf)),
        }
        print(_cli_json_dumps(payload, indent=2, default=str))
        return 0 if record_id else 1
    except PoisonedStateError as e:
        # Choke-point catch (see `state.make_checkpoint_serde`): the
        # checkpoint this workflow already holds — before any attended
        # result is even materialized — carries a non-finite value
        # somewhere in `verdicts`/`envelopes`/`artifacts`/`plan_ref`/
        # `attended_results`/etc. Surface the SAME `unjudgeable` shape the
        # rest of the codebase uses and never touch the checkpoint.
        print(_cli_json_dumps({
            "ok": False, "status": "unjudgeable", "workflow_id": wf,
            "field": e.field,
            "detail": (
                "the stored checkpoint contains a non-finite value at "
                f"{e.field}; refusing to resume/finalize this workflow. "
                "This is a data defect in previously persisted state, not "
                "a defect in this finalize call. There is no in-place "
                "repair for `finalize`. Use `hydra replay --sanitize-non-"
                f"finite {wf}` to replay anyway (every substituted field "
                "is reported and the source checkpoint is left unchanged), "
                "or start a new workflow."
            ),
        }, indent=2, default=str))
        return 0
    finally:
        _release_resume_lock(lock_fd, lock_path)


def _cmd_budget(args) -> int:
    """Show or set the budget ledger for Hydra workflows.

    hydra budget
        List all known workflows latest-first (by checkpoint mtime) with a
        summary row: workflow_id, phase, budget_usd, spent_usd, spent_tokens.

    hydra budget <workflow_id>
        Full budget ledger for that workflow: budget_usd, spent_usd, remaining,
        spent_tokens, utilization_pct, repo_budgets, repo_spend.

    hydra budget <workflow_id> --set <USD>
        Update the budget ceiling in the LangGraph checkpoint to USD. The
        change is durable (persisted via update_state) and emits a trace event.
        Equivalent to `hydra resume --action modify-budget --option USD` but
        without graph re-invocation.
    """
    import os
    import sqlite3

    project = Path(args.project) if args.project else Path.cwd()
    wf_arg = getattr(args, "workflow_id", None)
    set_usd = getattr(args, "set_usd", None)

    # --set is a mutation and MUST target a specific workflow. Without a
    # workflow_id it would otherwise fall through to the list-all path and be
    # silently discarded, leaving the operator believing a cap was written.
    if set_usd is not None and not wf_arg:
        print(_cli_json_dumps({
            "error": "--set requires a workflow_id (e.g. `hydra budget <id> --set 250`)",
        }), file=sys.stderr)
        return 1

    from .supervisor import build_supervisor, _PurePythonRunner
    sup = build_supervisor(project_root=project, dispatcher=_NullDispatcher())
    if isinstance(sup, _PurePythonRunner):
        print(_cli_json_dumps({
            "error": "langgraph unavailable — budget requires the checkpointing supervisor",
        }), file=sys.stderr)
        return 1

    # ---- Single-workflow path (detail or --set) --------------------------------
    if wf_arg:
        if not _WORKFLOW_ID_RE.match(wf_arg):
            print(_cli_json_dumps({"error": f"invalid workflow_id {wf_arg!r}"}),
                  file=sys.stderr)
            return 1
        config = {"configurable": {"thread_id": wf_arg}}
        snap = sup.get_state(config)
        if snap is None or not snap.values:
            print(_cli_json_dumps({"workflow_id": wf_arg, "error": "not_found"}),
                  file=sys.stderr)
            return 1
        try:
            state = HydraState.model_validate(snap.values)
        except Exception as e:  # noqa: BLE001
            print(_cli_json_dumps({"workflow_id": wf_arg,
                              "error": f"checkpoint_invalid: {e}"}),
                  file=sys.stderr)
            return 1

        b = state.budget

        if set_usd is not None:
            try:
                new_usd = float(set_usd)
            except (TypeError, ValueError):
                print(_cli_json_dumps({"error": (
                    f"--set requires a numeric USD value, got {set_usd!r}"
                )}), file=sys.stderr)
                return 1
            # Cross-vendor judge finding (this round, CRITICAL): `--set`
            # only checked non-negative, so `--set nan`/`--set inf` slipped
            # a non-finite budget straight into the checkpoint mutation
            # below via `update_state`. Same shared validator as the
            # argparse `--budget` flags and the MCP tool (see
            # `strict_json.reject_non_finite`'s docstring).
            try:
                reject_non_finite(new_usd, flag="--set")
            except ValueError as e:
                print(_cli_json_dumps({"error": str(e)}), file=sys.stderr)
                return 1
            if new_usd < 0:
                print(_cli_json_dumps({"error": "budget_usd must be non-negative"}),
                      file=sys.stderr)
                return 1

            # M3 mutating-action capability verification (mirrors _cmd_resume_locked).
            # budget_set is a state-mutating action; gate it through the same
            # verify_operator_capability path as approve/force-dispatch/change-squads.
            import logging as _logging_bs
            _log_bs = _logging_bs.getLogger(__name__)
            _operator_bs = (
                getattr(args, "operator", None)
                or os.environ.get("HYDRA_OPERATOR_ID", "")
                or "unknown"
            )
            _pending_for_mint: dict = {
                "capability": "budget_set",
                "gate_node": "budget",
                "reason": "budget_modification",
                "workflow_id": wf_arg,
                "resource_id": wf_arg,
            }
            _cap_token_bs: dict | None = None
            # _force_degraded_bs: True when HYDRA_OPERATOR_KEY is absent/empty.
            # KEY presence — not operator-id — is the authoritative signal for whether
            # cryptographic enforcement is configured.  With no key we intentionally
            # produce a degraded (unsigned) capability token and warn-and-proceed
            # regardless of HYDRA_OPERATOR_ID (documented WS-AUTH run-A / foundation
            # posture).  When a key IS present, any mint/verify exception FAILS CLOSED
            # (return 1, no patch) to prevent unsigned state mutations.
            _force_degraded_bs = not bool(os.environ.get("HYDRA_OPERATOR_KEY", "").strip())
            try:
                from .auth.capability import mint_for_approval as _mint_bs
                if _force_degraded_bs:
                    _log_bs.warning(
                        "HYDRA_OPERATOR_KEY not configured for budget_set; capability degraded — "
                        "set HYDRA_OPERATOR_KEY to enable cryptographic enforcement",
                    )
                    _saved_key_bs = os.environ.pop("HYDRA_OPERATOR_KEY", None)
                    try:
                        _cap_token_bs = _mint_bs(
                            workflow_id=wf_arg,
                            pending_hitl=_pending_for_mint,
                            operator=_operator_bs,
                        )
                    finally:
                        if _saved_key_bs is not None:
                            os.environ["HYDRA_OPERATOR_KEY"] = _saved_key_bs
                else:
                    _cap_token_bs = _mint_bs(
                        workflow_id=wf_arg,
                        pending_hitl=_pending_for_mint,
                        operator=_operator_bs,
                    )
            except Exception as _mint_exc_bs:  # noqa: BLE001
                if _force_degraded_bs:
                    # Degraded path: mint raised on a no-key run — warn and proceed.
                    _log_bs.warning(
                        "mint_for_approval raised %s (degraded path) — budget_set proceeds",
                        type(_mint_exc_bs).__name__,
                    )
                else:
                    # Key IS configured but mint failed — fail closed.
                    print(_cli_json_dumps({
                        "error": (
                            f"capability_mint_failed: {type(_mint_exc_bs).__name__}: "
                            f"{_mint_exc_bs}"
                        ),
                        "workflow_id": wf_arg,
                    }), file=sys.stderr)
                    return 1
            if _cap_token_bs is not None:
                try:
                    from .auth.capability import verify_operator_capability as _verify_bs
                    _vr_bs = _verify_bs(
                        _cap_token_bs,
                        expected_capability="budget_set",
                        expected_workflow_id=wf_arg,
                        expected_resource_id=wf_arg,
                    )
                    if not _vr_bs.get("valid"):
                        _m3_reason_bs = _vr_bs.get("reason", "unknown")
                        _m3_sig_bs = (_cap_token_bs.get("sig") or {})
                        _is_degraded_bs = (
                            _m3_sig_bs.get("degraded") is True
                            or _m3_sig_bs.get("value") is None
                            or "degraded" in _m3_reason_bs
                            or "no key" in _m3_reason_bs
                        )
                        if _is_degraded_bs:
                            _log_bs.warning(
                                "capability verify: degraded (%s) — budget_set proceeds "
                                "(set HYDRA_OPERATOR_KEY to enable cryptographic enforcement)",
                                _m3_reason_bs,
                            )
                        else:
                            print(_cli_json_dumps({
                                "error": f"capability_verify_failed: {_m3_reason_bs}",
                                "workflow_id": wf_arg,
                            }), file=sys.stderr)
                            return 1
                except Exception as _v_exc_bs:  # noqa: BLE001
                    if _force_degraded_bs:
                        # Degraded path: verify raised on a no-key run — warn and proceed.
                        _log_bs.warning(
                            "verify_operator_capability raised %s (degraded path) — proceeds",
                            type(_v_exc_bs).__name__,
                        )
                    else:
                        # Key IS configured but verify raised — fail closed.
                        print(_cli_json_dumps({
                            "error": (
                                f"capability_verify_exception: {type(_v_exc_bs).__name__}: "
                                f"{_v_exc_bs}"
                            ),
                            "workflow_id": wf_arg,
                        }), file=sys.stderr)
                        return 1

            b.budget_usd = new_usd
            try:
                patch_bs: dict = {"budget": b.model_dump(mode="json")}
                if _cap_token_bs is not None:
                    patch_bs["operator_capability"] = _cap_token_bs
                sup.update_state(config, patch_bs)
            except Exception as e:  # noqa: BLE001
                print(_cli_json_dumps({"error": f"checkpoint update failed: {e}"}),
                      file=sys.stderr)
                return 1
            emit(project, wf_arg, "budget.set",
                 {"workflow_id": wf_arg, "budget_usd": new_usd,
                  "spent_usd": b.spent_usd, "operator": _operator_bs})
            print(_cli_json_dumps({
                "workflow_id": wf_arg,
                "set": True,
                "budget_usd": new_usd,
                "spent_usd": round(b.spent_usd, 6),
                "remaining_usd": round(max(new_usd - b.spent_usd, 0.0), 6),
                "capability_degraded": (
                    _cap_token_bs is None
                    or (_cap_token_bs.get("sig") or {}).get("degraded", False)
                ),
            }, indent=2))
            return 0

        # Detail view
        print(_cli_json_dumps({
            "workflow_id": wf_arg,
            "phase": getattr(state, "phase", "?"),
            "root_goal": (getattr(state, "root_goal", "") or "")[:80],
            "budget_usd": b.budget_usd,
            "spent_usd": round(b.spent_usd, 6),
            "remaining_usd": round(max(b.budget_usd - b.spent_usd, 0.0), 6),
            "spent_tokens": b.spent_tokens,
            "utilization_pct": round(b.percent_consumed * 100, 1),
            "repo_budgets": b.repo_budgets,
            "repo_spend": {k: round(v, 6) for k, v in b.repo_spend.items()},
        }, indent=2))
        return 0

    # ---- List all workflows ---------------------------------------------------
    cp_db = Path(
        os.environ.get("HYDRA_CHECKPOINT_DB")
        or str(Path.home() / ".hydra" / "checkpoints.db")
    )
    if not cp_db.exists():
        print(_cli_json_dumps({"workflows": [], "reason": "no_checkpoint_db"}, indent=2))
        return 0

    conn = sqlite3.connect(
        f"file:{cp_db.as_posix()}?mode=ro", uri=True, check_same_thread=False
    )
    try:
        thread_ids = [
            r[0]
            for r in conn.execute(
                "SELECT DISTINCT thread_id FROM checkpoints LIMIT 500"
            ).fetchall()
        ]
    except Exception:  # noqa: BLE001 — schema absent / older langgraph layout
        thread_ids = []
    finally:
        conn.close()

    rows: list[dict] = []
    for wf in thread_ids:
        config = {"configurable": {"thread_id": wf}}
        try:
            snap = sup.get_state(config)
        except Exception:  # noqa: BLE001
            continue
        if snap is None or not snap.values:
            continue
        try:
            state = HydraState.model_validate(snap.values)
        except Exception:  # noqa: BLE001
            continue
        b = state.budget
        # Checkpoint mtime: use the trace JSONL file as a proxy (written at
        # every emit call throughout the workflow run).
        tp = trace_path(project, wf)
        mtime: float = 0.0
        try:
            if tp.exists():
                mtime = tp.stat().st_mtime
        except OSError:
            pass
        rows.append({
            "_mtime": mtime,
            "workflow_id": wf,
            "phase": getattr(state, "phase", "?"),
            "goal": (getattr(state, "root_goal", "") or "")[:60],
            "budget_usd": b.budget_usd,
            "spent_usd": round(b.spent_usd, 6),
            "spent_tokens": b.spent_tokens,
            "utilization_pct": round(b.percent_consumed * 100, 1),
        })

    # Sort latest-first; remove the sort key before printing.
    rows.sort(key=lambda r: r["_mtime"], reverse=True)
    for r in rows:
        del r["_mtime"]

    print(_cli_json_dumps({"count": len(rows), "workflows": rows}, indent=2))
    return 0


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
        print(_cli_json_dumps({
            "error": "langgraph unavailable — reap requires the checkpointing supervisor",
        }), file=sys.stderr)
        return 1

    # Enumerate checkpoint threads from the SAME store build_supervisor binds.
    cp_db = Path(os.environ.get("HYDRA_CHECKPOINT_DB")
                 or (Path.home() / ".hydra" / "checkpoints.db"))
    if not cp_db.exists():
        print(_cli_json_dumps({"scanned": 0, "candidates": [], "reaped": [],
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

    # E2-17: a reaped workflow is terminal — close its pending rows in
    # TheEights' shared ledger. One hitl.list for the whole sweep; fail-soft
    # (an unreachable daemon leaves the rows and spools nothing to lose).
    eights_hitl = {"resolved": 0, "failed": 0, "pending": 0}
    if do_apply and reaped:
        try:
            from .eights.hitl_reconcile import resolve_for_workflow
            attestor = _reconcile_attestor(project)
            rows = attestor.hitl_list()
            if rows is None:
                eights_hitl["unavailable"] = True
            else:
                for wf in reaped:
                    s = resolve_for_workflow(
                        attestor, wf, rows=rows,
                        note="workflow terminal: surfaced",
                    )
                    for k in ("resolved", "failed", "pending"):
                        eights_hitl[k] += s.get(k, 0)
        except Exception as exc:  # noqa: BLE001 — never fail a reap on this
            eights_hitl["error"] = f"{type(exc).__name__}: {exc}"

    print(_cli_json_dumps({
        "mode": "apply" if do_apply else "dry-run",
        "older_than_hours": older_than_h,
        "eights_hitl": eights_hitl,
        "scanned": len(thread_ids),
        "candidate_count": len(candidates),
        "candidates": candidates,
        "reaped_count": len(reaped),
        "reaped": reaped,
    }, indent=2))
    return 0


def _cmd_sweep_worktrees(args) -> int:
    """`hydra sweep-worktrees [--project PATH] [--apply]` — operator entry
    point for `host_bridge.sweep_stale_worktrees` (the attended-worktree
    janitor). See that function's docstring for the safety invariants this
    command must never weaken: cursor-terminality gated, fails toward keeping
    on a corrupt/unreadable/missing cursor, never deletes a git branch, and
    per-entry failures are reported rather than silently dropped.

    Dry-run by default — reports what WOULD be removed without touching disk.
    Pass --apply to actually delete. This mirrors `reap`'s dry-run-by-default
    convention above.
    """
    from .host_bridge import sweep_stale_worktrees

    project = str(Path(args.project) if args.project else Path.cwd())
    do_apply = bool(getattr(args, "apply", False))

    report = sweep_stale_worktrees(project, project_root=project, dry_run=not do_apply)

    # Normalize into one flat, auditable per-entry list: worktree path,
    # cursor state (where known), and WHY each decision was made — a bare
    # count would hide exactly the information an operator needs before
    # trusting a destructive sweep.
    entries: list[dict[str, Any]] = []
    for e in report.get("removed", []):
        entries.append({
            "worktree": e.get("worktree"),
            "run_id": e.get("run_id"),
            "root": e.get("root"),
            "cursor_state": e.get("state"),
            "decision": "would-remove" if e.get("dry_run") else "removed",
        })
    for e in report.get("skipped", []):
        reason = str(e.get("reason") or "")
        if reason == "no_cursor_found":
            decision, cursor_state = "skipped-no-cursor", None
        elif reason.startswith("non_terminal_state:"):
            decision, cursor_state = "skipped-non-terminal", reason.split(":", 1)[1]
        else:
            decision, cursor_state = "skipped", None
        entries.append({
            "worktree": e.get("worktree"),
            "run_id": e.get("run_id"),
            "root": e.get("root"),
            "cursor_state": cursor_state,
            "decision": decision,
            "reason": reason,
        })
    for e in report.get("errors", []):
        entries.append({
            "worktree": e.get("worktree"),
            "run_id": e.get("run_id"),
            "root": e.get("root"),
            "cursor_state": None,
            "decision": "error",
            "error": e.get("error"),
        })

    print(_cli_json_dumps({
        "mode": "apply" if do_apply else "dry-run",
        "project": project,
        "count_removed": sum(1 for e in entries if e["decision"] in ("removed", "would-remove")),
        "count_skipped": sum(1 for e in entries if e["decision"].startswith("skipped")),
        "count_errors": sum(1 for e in entries if e["decision"] == "error"),
        "entries": entries,
    }, indent=2))
    return 0


def _cmd_status(args) -> int:
    """List recent workflows (latest-first) or show a specific workflow's state.

    No arg  → JSON list of {workflow_id, phase, pending_hitl, root_goal}
              sorted by trace mtime LATEST-FIRST. Phase is read from the
              LangGraph checkpoint when available; "?" otherwise.

    With id → structured JSON view: tasks table (task_id[:8], owner_squad,
              status), pending HITL summary, budget (budget_usd, spent_usd,
              usd_remaining). NOT a raw trace.jsonl dump.
    """
    project = Path(args.project) if args.project else Path.cwd()
    base = project / ".hydra"

    if args.workflow_id:
        # --- Specific workflow: structured view (checkpoint preferred) ---
        wf = str(args.workflow_id)
        try:
            from .supervisor import build_supervisor, _PurePythonRunner
            _sup = build_supervisor(project_root=project, dispatcher=_NullDispatcher())
            if not isinstance(_sup, _PurePythonRunner):
                _config = {"configurable": {"thread_id": wf}}
                _snap = _sup.get_state(_config)
                if _snap is not None and _snap.values:
                    _state = HydraState.model_validate(_snap.values)
                    # MU15d: tasks whose id is in attended_done_task_ids show
                    # "done (attended)" — the checkpoint status stays
                    # "deferred_to_host" (append-reducer cannot update it in
                    # place) but the attended host's completion signal is the
                    # authoritative override for display.
                    _done_task_ids = set(
                        getattr(_state, "attended_done_task_ids", []) or []
                    )
                    tasks_view = [
                        {
                            "task_id": str(t.task_id)[:8],
                            "owner_squad": t.owner_squad,
                            "status": (
                                "done (attended)"
                                if str(t.task_id) in _done_task_ids
                                else t.status
                            ),
                        }
                        for t in getattr(_state, "tasks", [])
                    ]
                    _b = _state.budget
                    _pending = _state.pending_hitl
                    _pending_summary = None
                    if isinstance(_pending, dict):
                        _pending_summary = {
                            "reason": _pending.get("reason"),
                            "gate_node": _pending.get("gate_node"),
                            "summary": (_pending.get("summary", "") or "")[:120],
                        }
                    print(_cli_json_dumps({
                        "workflow_id": wf,
                        "phase": _state.phase,
                        "root_goal": (_state.root_goal or "")[:120],
                        "tasks": tasks_view,
                        "pending_hitl": _pending_summary,
                        "budget": {
                            "budget_usd": _b.budget_usd,
                            "spent_usd": round(_b.spent_usd, 6),
                            "usd_remaining": round(_b.usd_remaining, 6),
                            "percent_consumed": round(_b.percent_consumed * 100, 1),
                        },
                    }, indent=2))
                    return 0
        except PoisonedStateError as e:
            # Choke-point catch (see `state.make_checkpoint_serde`): surface
            # the SAME `unjudgeable` shape `_cmd_finalize` uses instead of
            # falling through to the trace-view fallback below, which would
            # tell the operator "checkpoint unavailable" and hide WHY. This
            # read never mutates the checkpoint, so — like `_cmd_finalize` —
            # exit 0: the CLI call itself succeeded at reporting the
            # workflow's true (refused) state; it is not a call failure.
            # Recovery: `hydra replay` reads through the SAME scanning serde
            # (verified in tests/test_replay_poisoned_state.py), so it also
            # refuses this checkpoint by default — it is NOT a working
            # recovery path on its own. The only way to proceed is the
            # explicit opt-in `hydra replay --sanitize-non-finite <id>`,
            # which substitutes every non-finite value with null, reports
            # each substituted field, and never touches this checkpoint
            # (only the freshly minted replay workflow is persisted).
            print(_cli_json_dumps({
                "workflow_id": wf,
                "status": "unjudgeable",
                "field": e.field,
                "detail": (
                    "the stored checkpoint contains a non-finite value at "
                    f"{e.field}; refusing to display this workflow's state. "
                    "This is a data defect in previously persisted state. "
                    "Recovery: there is no in-place repair, and `hydra "
                    "replay` also refuses this checkpoint by default (same "
                    "scan). Use `hydra replay --sanitize-non-finite " + wf +
                    "` to replay anyway (every substituted field is "
                    "reported; the source checkpoint is left unchanged), or "
                    "quarantine this workflow_id and start a new one."
                ),
            }, indent=2, default=str))
            return 0
        except Exception:  # noqa: BLE001 — fall back to trace view
            pass

        # Fall back: structured view of the most recent trace events (NOT raw dump).
        p = trace_path(project, wf)
        if not p.exists():
            print(_cli_json_dumps({"error": f"no trace for workflow_id={wf!r}"}))
            return 1
        lines = [ln for ln in p.read_text(encoding="utf-8").splitlines() if ln.strip()]
        recent_events: list[dict] = []
        for line in lines[-30:]:
            try:
                recent_events.append(json.loads(line))
            except json.JSONDecodeError:
                pass
        print(_cli_json_dumps({
            "workflow_id": wf,
            "note": "checkpoint unavailable — showing last 30 trace events",
            "events": recent_events,
        }, indent=2, default=str))
        return 0

    # --- No arg: list workflows, latest-first by trace mtime ---
    if not base.exists():
        print(_cli_json_dumps({"workflows": []}, indent=2))
        return 0

    wf_dirs = [d for d in base.iterdir() if d.is_dir()]

    def _trace_mtime(d: Path) -> float:
        tf = d / "trace.jsonl"
        try:
            return tf.stat().st_mtime if tf.exists() else d.stat().st_mtime
        except OSError:
            return 0.0

    wf_dirs.sort(key=_trace_mtime, reverse=True)

    # Try building the supervisor once (to get rich checkpoint state per workflow).
    _list_sup = None
    try:
        from .supervisor import build_supervisor, _PurePythonRunner
        _sup_cand = build_supervisor(project_root=project, dispatcher=_NullDispatcher())
        if not isinstance(_sup_cand, _PurePythonRunner):
            _list_sup = _sup_cand
    except Exception:  # noqa: BLE001 — langgraph absent; degrade gracefully
        pass

    rows: list[dict] = []
    for d in wf_dirs:
        row: dict = {"workflow_id": d.name, "phase": "?", "pending_hitl": None}
        if _list_sup is not None:
            try:
                _cfg = {"configurable": {"thread_id": d.name}}
                _sn = _list_sup.get_state(_cfg)
                if _sn is not None and _sn.values:
                    _st = HydraState.model_validate(_sn.values)
                    row["phase"] = _st.phase
                    row["root_goal"] = (_st.root_goal or "")[:80]
                    _ph = _st.pending_hitl
                    if isinstance(_ph, dict):
                        row["pending_hitl"] = {
                            "reason": _ph.get("reason"),
                            "gate_node": _ph.get("gate_node"),
                        }
            except PoisonedStateError as e:
                # Report the poisoned row explicitly rather than leaving it
                # at phase "?" indistinguishable from an ordinary/unreadable
                # checkpoint. Other workflows in the list are unaffected —
                # one bad row must not abort the listing.
                row["status"] = "unjudgeable"
                row["field"] = e.field
                row["detail"] = (
                    f"non-finite value at {e.field}; no in-place repair, and "
                    "plain `hydra replay` also refuses (same scan) — use "
                    "`hydra replay --sanitize-non-finite <id>` to replay "
                    "anyway (every substitution reported, source checkpoint "
                    "unchanged), or quarantine this workflow_id"
                )
            except Exception:  # noqa: BLE001 — one bad checkpoint must not abort listing
                pass
        if "root_goal" not in row:
            # Try to extract goal from the first workflow_start trace event.
            tf = d / "trace.jsonl"
            if tf.exists():
                try:
                    lines = tf.read_text(encoding="utf-8").splitlines()
                    for line in lines[:10]:
                        if not line.strip():
                            continue
                        try:
                            ev = json.loads(line)
                            if ev.get("kind") == "workflow_start":
                                _g = (ev.get("payload") or {}).get("goal", "")
                                if _g:
                                    row["root_goal"] = _g[:80]
                                    break
                        except json.JSONDecodeError:
                            pass
                except Exception:  # noqa: BLE001
                    pass
        rows.append(row)

    print(_cli_json_dumps({"workflows": rows}, indent=2))
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
        print(_cli_json_dumps({
            "error": f"invalid workflow_id {source_wf!r}",
            "detail": "must match ^[A-Za-z0-9][A-Za-z0-9\\-_]{{0,63}}$",
        }), file=sys.stderr)
        return 1

    from_phase = getattr(args, "from_phase", None) or "intake"
    # Validate --from-phase against known phases
    if from_phase not in _KNOWN_PHASES:
        print(_cli_json_dumps({
            "error": f"invalid --from-phase {from_phase!r}",
            "valid": sorted(_KNOWN_PHASES),
        }), file=sys.stderr)
        return 1

    swap_model = getattr(args, "swap_model", None)
    if swap_model is not None and not _MODEL_ID_RE.match(swap_model):
        print(_cli_json_dumps({
            "error": f"invalid --swap-model {swap_model!r}",
            "detail": "must match ^[A-Za-z0-9][A-Za-z0-9\\-_./:]{{0,127}}$",
        }), file=sys.stderr)
        return 1

    live = getattr(args, "live", False)
    sanitize = getattr(args, "sanitize_non_finite", False)

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
        print(_cli_json_dumps({
            "error": "langgraph unavailable — replay requires the checkpointing supervisor",
        }), file=sys.stderr)
        return 1

    # Load source checkpoint
    source_config = {"configurable": {"thread_id": source_wf}}
    sanitized_fields: list[str] = []
    try:
        snap = sup.get_state(source_config)
    except PoisonedStateError as e:
        # Verified end-to-end (tests/test_replay_poisoned_state.py): this
        # `get_state` call routes through the SAME scanning serde
        # (`state.make_checkpoint_serde`) as every other checkpoint read, so
        # replaying a poisoned checkpoint refuses here exactly the way
        # `hydra status`/`hydra finalize` do -- it is NOT a working recovery
        # path by default. `--sanitize-non-finite` is the only opt-in way to
        # proceed anyway (see the local `_read_raw_checkpoint_for_sanitize`
        # closure defined below for why bypassing the scanning serde is safe
        # ONLY here: the sanitized values are used solely to seed the
        # freshly-minted `replay_wf` thread_id below, never written back
        # over `source_wf`).
        if not sanitize:
            print(_cli_json_dumps({
                "source_workflow_id": source_wf,
                "ok": False,
                "status": "unjudgeable",
                "field": e.field,
                "detail": (
                    "the stored checkpoint contains a non-finite value at "
                    f"{e.field}; refusing to replay. This is a data defect "
                    "in previously persisted state, not a defect in this "
                    "replay call. There is no in-place repair. Retry with "
                    "`--sanitize-non-finite` to replay anyway (every "
                    "substituted field is reported and the source "
                    "checkpoint is left unchanged), or start a new workflow."
                ),
            }, indent=2, default=str))
            return 0
        from .strict_json import sanitize_non_finite

        def _read_raw_checkpoint_for_sanitize(wf_id: str) -> dict | None:
            """Read a checkpoint's ``channel_values`` WITHOUT the
            non-finite-scanning serde (see ``state.make_checkpoint_serde``),
            for the sole purpose of ``hydra replay --sanitize-non-finite``.

            This is the ONLY sanctioned reason to bypass the scanning
            choke point: every other reader (``build_supervisor``'s
            checkpointer, ``hydra_memory._load_state_values``) must keep
            refusing by default. This closure is deliberately NOT a
            module-level name — it exists only inside this
            ``--sanitize-non-finite`` branch of ``_cmd_replay``, so there is
            nothing importable elsewhere in the codebase to accidentally
            call and bypass the choke point with. The caller is
            responsible for running the result through
            ``strict_json.sanitize_non_finite`` before treating any field
            as trusted, and MUST NOT write it back to this same ``wf_id``
            thread_id — ``_cmd_replay`` only ever persists it under a
            freshly minted ``replay_wf`` thread_id, leaving the source
            checkpoint byte-for-byte unchanged, exactly like an ordinary
            (unpoisoned) replay already does.
            """
            import sqlite3

            from langgraph.checkpoint.serde.jsonplus import JsonPlusSerializer
            from langgraph.checkpoint.sqlite import SqliteSaver

            from .state import BudgetLedger

            cp_db = Path(
                os.environ.get("HYDRA_CHECKPOINT_DB")
                or str(Path.home() / ".hydra" / "checkpoints.db")
            )
            if not cp_db.exists():
                return None
            conn = sqlite3.connect(str(cp_db), check_same_thread=False)
            try:
                raw_serde = JsonPlusSerializer(
                    allowed_msgpack_modules=[HydraState, TaskState, BudgetLedger],
                )
                saver = SqliteSaver(conn, serde=raw_serde)
                tup = saver.get_tuple({"configurable": {"thread_id": wf_id}})
                if tup is None:
                    return None
                return (tup.checkpoint or {}).get("channel_values") or {}
            finally:
                conn.close()

        raw_values = _read_raw_checkpoint_for_sanitize(source_wf)
        if raw_values is None:
            print(_cli_json_dumps({
                "source_workflow_id": source_wf,
                "error": "checkpoint_not_found",
                "detail": f"No checkpoint for workflow_id={source_wf!r}. "
                          "Run `hydra status` to list known workflows.",
            }), file=sys.stderr)
            return 1
        values, sanitized_fields = sanitize_non_finite(raw_values)
        if not isinstance(values, dict):
            values = {}
    else:
        if snap is None or not snap.values:
            print(_cli_json_dumps({
                "source_workflow_id": source_wf,
                "error": "checkpoint_not_found",
                "detail": f"No checkpoint for workflow_id={source_wf!r}. "
                          "Run `hydra status` to list known workflows.",
            }), file=sys.stderr)
            return 1
        values = dict(snap.values)

    # Reconstruct state at the requested phase boundary
    current_phase = values.get("phase", "intake")

    # Reset state to the from_phase starting point:
    # keep the root_goal, selected_squads, budget; clear runtime artifacts.
    replay_initial = HydraState(
        workflow_id=replay_wf,
        root_goal=values.get("root_goal", ""),
        phase=from_phase,
        selected_squads=values.get("selected_squads", []),
    )
    # WS1 retry (finding 5): carry the source run's repo-targeting fields
    # forward. Without this, a replay of a --repo/--repos-targeted run
    # silently drops back to intake's cwd-fallback path (the original bug
    # this stage exists to fix) -- landing in a DIFFERENT repo than the run
    # being replayed, which also breaks the determinism replay exists to
    # provide. These are the exact three fields node_intake persists onto
    # state for repo targeting (see node_intake's `update` dict above).
    if values.get("target_repo_id") is not None:
        replay_initial.target_repo_id = values["target_repo_id"]
    if values.get("target_repo_ids"):
        replay_initial.target_repo_ids = list(values["target_repo_ids"])
    if values.get("target_repo_subpath") is not None:
        replay_initial.target_repo_subpath = values["target_repo_subpath"]
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

    # Record the replay provenance in the trace (source id, phase, swap_model).
    # `sanitized_fields` is non-empty ONLY when `--sanitize-non-finite` was
    # passed AND the source checkpoint was actually poisoned -- recorded here
    # so the sanitization is never silent, per-field, in the durable trace.
    emit(project, replay_wf, "replay_start", {
        "source_workflow_id": source_wf,
        "source_phase": current_phase,
        "from_phase": from_phase,
        "swap_model": swap_model,
        "live": live,
        "sanitized_non_finite_fields": sanitized_fields or None,
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

    print(_cli_json_dumps({
        "source_workflow_id": source_wf,
        "replay_workflow_id": str(replay_wf),
        "from_phase": from_phase,
        "swap_model": swap_model,
        "live": live,
        "phase": phase,
        "sanitized_non_finite_fields": sanitized_fields or None,
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


def _redact_inline_secrets(spec: dict) -> list[str]:
    """Rewrite inline-secret-shaped env values in ``spec`` to ``${KEY}`` refs.

    Mutates ``spec["env"]`` in place and returns the sorted list of env keys
    that were rewritten. Detection is a shape check only
    (``backends_env.looks_like_inline_secret``); the value is never printed,
    logged, or returned.
    """
    from .backends_env import looks_like_inline_secret

    if not isinstance(spec, dict):
        return []
    env = spec.get("env")
    if not isinstance(env, dict):
        return []
    rewritten: list[str] = []
    for key, value in list(env.items()):
        if looks_like_inline_secret(value):
            env[key] = "${" + str(key) + "}"
            rewritten.append(str(key))
    return sorted(rewritten)


def _cmd_gateway_export_backends(args) -> int:
    """Export mcpServers block from ~/.claude.json to ~/.hydra/backends.json."""
    from .dispatcher import _load_user_scope_mcp, BACKEND_REGISTRY
    servers = _load_user_scope_mcp()
    if not servers:
        print("No mcpServers found in ~/.claude.json")
        return 1
    # E2-5: never copy inline secret material into backends.json. Any env value
    # that looks like a 64-hex key is rewritten to a ${VAR} reference resolved
    # at dispatch time from the launching environment. The value itself is
    # neither printed nor retained.
    for _name, _spec in servers.items():
        _redacted = _redact_inline_secrets(_spec)
        for _key in _redacted:
            print(
                f"  NOTE: {_name}.env.{_key} rewritten to ${{{_key}}} — "
                f"export {_key} in the environment that launches Hydra."
            )
    BACKEND_REGISTRY.parent.mkdir(parents=True, exist_ok=True)
    # Strict, not the stdout sanitize policy: this is PERSISTED OPERATOR
    # CONFIGURATION (backends.json is live operator state carrying
    # HYDRA_OPERATOR_ID, read back as config on every dispatch), not a
    # printed command result -- see the PERSISTED-STATE rule, not the CLI
    # stdout policy. A non-finite value here must refuse rather than
    # silently rewrite a field the operator owns to `null`.
    BACKEND_REGISTRY.write_text(
        dumps_strict(servers, label="backends.json export", indent=2, default=str),
        encoding="utf-8",
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
        ("mcp__pp_agy__", "mcp__hydra_gateway__pp_agy__"),
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
    # Strict: this rewrites the operator's OWN ~/.claude.json whole -- the
    # same PERSISTED-STATE rule as the backends.json export above, not the
    # stdout policy. Refuse rather than silently substitute a field in a
    # file we don't own the full schema of.
    claude_json.write_text(
        dumps_strict(raw, label="claude.json rewrite", indent=2, default=str),
        encoding="utf-8",
    )
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
    # Strict: persisted operator configuration, same as the export site
    # above -- not a printed command result.
    BACKEND_REGISTRY.write_text(
        dumps_strict(backends, label="backends.json setup", indent=2),
        encoding="utf-8",
    )
    print(f"\nWrote {len(backends)} backends to {BACKEND_REGISTRY}")
    return 0


def _interpolate(template: str, values: dict[str, str]) -> str:
    result = template
    for key, val in values.items():
        result = result.replace(f"{{{key}}}", val)
    return result


def _resolve_eights_spool_roots() -> tuple[Path, Path]:
    """Resolve (spool_root, dead_letter_root), honouring the env overrides.

    ``HYDRA_EIGHTS_SPOOL`` / ``HYDRA_EIGHTS_DEAD_LETTER`` let tests (and an
    operator inspecting a copied spool) point the drain + doctor checks at a
    scratch directory instead of ``~/.hydra/eights-pending``.
    """
    from .eights.pending_spool import DEFAULT_SPOOL_ROOT, DEFAULT_DEAD_LETTER_ROOT

    spool_root = Path(
        os.environ.get("HYDRA_EIGHTS_SPOOL") or DEFAULT_SPOOL_ROOT
    )
    dead_root = Path(
        os.environ.get("HYDRA_EIGHTS_DEAD_LETTER") or DEFAULT_DEAD_LETTER_ROOT
    )
    return spool_root, dead_root


def _cmd_eights_drain(args) -> int:
    """Drain the eights-pending spool in a bounded batch.

    Re-issues up to --limit (default 500) spooled calls to the eights daemon
    via the live MCPStdioDispatcher.  Also removes stale .partial staging
    files older than 1 hour (these are crash residue from interrupted writes).

    Prints JSON: {drained, failed, skipped, dead_lettered, dead_letter_depth,
    remaining, partial_removed, spool_root, dead_letter_root}.
    ``drained`` = successfully re-sent; ``failed`` = attempted but daemon
    rejected; ``skipped`` = corrupt / concurrently-removed / TTL-expired;
    ``dead_lettered`` = moved to the dead-letter dir on THIS run;
    ``dead_letter_depth`` = total entries sitting in the dead-letter dir.

    E2-3: entries older than --max-age-hours are dead-lettered WITHOUT a
    replay attempt.  That used to be invisible, so an eights outage longer
    than the 24h default silently converted the whole backlog into
    unreplayable dead letters.  A WARN now goes to stderr whenever an entry
    is dead-lettered for age, and `--replay-dead-letter` re-queues them.

    If the eights daemon is not reachable the replay attempts all fail; spool
    entries remain in place for the next run (fail-soft, never destructive).
    """
    import time as _time
    from .eights.pending_spool import PendingSpool

    limit = int(getattr(args, "limit", 500))
    max_age_hours = float(getattr(args, "max_age_hours", 24.0))
    replay_dead_letter = bool(getattr(args, "replay_dead_letter", False))
    project = Path(args.project) if args.project else Path.cwd()
    spool_root, dead_root = _resolve_eights_spool_roots()

    # Remove stale .partial files (crash residue from interrupted spool writes).
    partial_removed = 0
    if spool_root.is_dir():
        stale_cutoff = _time.time() - 3600.0  # 1 hour
        for _pf in spool_root.glob("*.partial"):
            try:
                if _pf.stat().st_mtime < stale_cutoff:
                    _pf.unlink()
                    partial_removed += 1
            except OSError:
                pass

    spool = PendingSpool(root=spool_root, dead_letter_root=dead_root)

    # E2-3: --replay-dead-letter moves dead letters back into the pending
    # spool (attempts reset) and drains THAT pass with the age check off —
    # otherwise every re-queued entry would immediately expire again.
    requeued = 0
    if replay_dead_letter:
        requeued = spool.requeue_dead_letters(limit=limit)
        max_age_hours = 0.0

    drained = 0
    failed = 0
    skipped = 0
    dead_lettered = 0
    dead_lettered_expired = 0
    drain_error: str | None = None

    try:
        from .dispatcher import MCPStdioDispatcher
        from .eights.attestation import EightsAttestor
        dispatcher = MCPStdioDispatcher(project)
        attestor = EightsAttestor(dispatcher=dispatcher, spool=spool)
        summary = attestor.replay_pending(
            max_replays=limit, max_age_hours=max_age_hours
        )
        drained = summary.get("sent", 0)
        failed = summary.get("failed", 0)
        skipped = summary.get("skipped", 0)
        dead_lettered = summary.get("dead_lettered", 0)
        dead_lettered_expired = summary.get("dead_lettered_expired", 0)
    except Exception as exc:  # noqa: BLE001 — dispatcher not available → partial drain report
        drain_error = f"{type(exc).__name__}: {exc}"

    remaining = spool.count()
    dead_letter_depth = spool.dead_letter_count()
    out: dict = {
        "drained": drained,
        "failed": failed,
        "skipped": skipped,
        "dead_lettered": dead_lettered,
        "dead_letter_depth": dead_letter_depth,
        "remaining": remaining,
        "partial_removed": partial_removed,
        "spool_root": str(spool_root),
        "dead_letter_root": str(dead_root),
    }
    if replay_dead_letter:
        out["requeued_from_dead_letter"] = requeued
    if drain_error is not None:
        out["error"] = drain_error

    if dead_lettered_expired > 0:
        print(
            f"WARN: {dead_lettered_expired} spool entr"
            f"{'y' if dead_lettered_expired == 1 else 'ies'} dead-lettered for "
            f"age (older than --max-age-hours={max_age_hours}). They were NOT "
            f"replayed. dead_letter_depth={dead_letter_depth} — triage each "
            "entry before replaying it (see "
            "docs/audits/EIGHTS-RECORD-OUTCOME-RCA-2026-09-16.md §7 path T); "
            "an unfiltered bulk replay is NOT recommended. Raising "
            "--max-age-hours before the next drain is a separate, unrelated "
            "knob.",
            file=sys.stderr,
        )

    print(_cli_json_dumps(out, indent=2))
    return 0


def _cmd_eights_hitl_reconcile(args) -> int:
    """`hydra eights-hitl-reconcile [--apply] [--limit N]` (E2-17).

    Lists TheEights' pending `hydra_gate` requests, matches each row's
    workflow_id against Hydra's checkpoint store, and resolves the rows whose
    workflow is terminal (done/surfaced) or unknown to Hydra. Rows for an
    ACTIVE workflow are never touched — those are real gates awaiting a human.

    Dry-run by default (mirrors `reap`); prints a JSON summary either way.
    """
    project = Path(args.project) if args.project else Path.cwd()
    do_apply = bool(getattr(args, "apply", False))
    limit = getattr(args, "limit", None)

    from .eights.hitl_reconcile import reconcile
    try:
        attestor = _reconcile_attestor(project)
        lookup = _make_phase_lookup(project)
    except Exception as exc:  # noqa: BLE001 — report, never traceback
        print(_cli_json_dumps({"error": f"{type(exc).__name__}: {exc}"}, indent=2),
              file=sys.stderr)
        return 1

    summary = reconcile(attestor, lookup, apply=do_apply,
                        limit=int(limit) if limit is not None else None)
    summary["mode"] = "apply" if do_apply else "dry-run"
    print(_cli_json_dumps(summary, indent=2))
    return 0


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
    r.add_argument("--budget", type=finite_float_arg("--budget"), default=None,
                   help="Workflow budget cap in USD (sets BudgetLedger.budget_usd). "
                        "Must be a finite number: NaN/Infinity are rejected.")
    r.add_argument("--risk", choices=["low", "medium", "high"], default=None,
                   help="Operator risk tolerance hint (recorded on the start event).")
    r.add_argument("--repo", default=None, metavar="ID",
                   help="Single allow-listed repo id for engineering targeting "
                        "(pre-seeded onto HydraState.target_repo_id; resolved by "
                        "hydra_core.repo_registry).")
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
    pl.add_argument("--budget", type=finite_float_arg("--budget"), default=None,
                    help="Workflow budget cap in USD (sets BudgetLedger.budget_usd). "
                         "Must be a finite number: NaN/Infinity are rejected.")
    pl.add_argument("--repo", default=None, metavar="ID",
                    help="Single allow-listed repo id (pre-seeded onto "
                         "HydraState.target_repo_id).")
    pl.add_argument("--repos", default=None, metavar="ID,ID,...",
                    help="Comma-separated allow-listed repo ids for fleet mode.")
    pl.add_argument("--subdir", default=None, metavar="PATH",
                    help="Repo-relative engineering target under --repo/--repos.")
    pl.add_argument("--workflow-id", dest="workflow_id_override", default=None,
                    metavar="ID",
                    help="Pre-allocate the workflow id (threads plan->step->resume).")
    pl.add_argument("--risk", choices=["low", "medium", "high"], default=None,
                    help="Operator risk tolerance hint (recorded on the plan event).")
    pl.add_argument("--rigor", choices=["trivial", "standard", "major"], default=None,
                    help="Operator override of the computed plan_rigor. Wins over "
                         "node_planner's triage; a downgrade from the computed value "
                         "is recorded as a hitl_history event.")

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

    fin = sub.add_parser("finalize", help=(
        "Attended mode: materialise the attended task results and resume the "
        "graph through synthesis -> judge_synthesis -> postcheck (idempotent)."))
    fin.add_argument("workflow_id")
    fin.add_argument("--verbose", action="store_true")

    s = sub.add_parser("status")
    s.add_argument("workflow_id", nargs="?")
    t = sub.add_parser("trace")
    t.add_argument("workflow_id")
    # Reaper: GC abandoned non-terminal workflows (stuck at approval/synthesis
    # because their session ended or they were never resumed past an interrupt).
    rp_reap = sub.add_parser("reap")
    rp_reap.add_argument("--older-than-hours", dest="older_than_hours",
                         type=finite_float_arg("--older-than-hours"), default=24.0,
                         help="Only reap non-terminal workflows idle this long (default 24). "
                              "Must be a finite number: NaN/Infinity are rejected (a NaN "
                              "threshold makes `_is_reapable`'s `age_hours < older_than_hours` "
                              "comparison fail open, marking every non-terminal workflow "
                              "reapable regardless of actual age).")
    rp_reap.add_argument("--apply", action="store_true",
                         help="Actually transition stale workflows to 'surfaced' (default: dry-run).")

    # Attended-worktree janitor operator entry point (host_bridge.sweep_stale_worktrees).
    sw = sub.add_parser("sweep-worktrees", help=(
        "Remove attended-run worktrees whose Hydra cursor has reached a "
        "terminal state (complete/surfaced/aborted). Never deletes a git "
        "branch. Dry-run by default; per-entry reasons are always reported."))
    sw.add_argument("--apply", action="store_true",
                    help="Actually remove eligible worktrees (default: dry-run, deletes nothing).")

    ap_approve = sub.add_parser("approve")
    ap_approve.add_argument("workflow_id")
    ap_approve.add_argument("--live", action="store_true",
                            help="Continue with the live MCP dispatcher")
    # C2 (mesh-console-unification): real HITL resume from checkpoint.
    rs = sub.add_parser("resume")
    rs.add_argument("workflow_id")
    rs.add_argument("--action", required=True,
                    choices=["approve", "reject", "modify-budget",
                             "force-dispatch", "change-squads",
                             "recover-stalled-stage", "modify-plan"])
    rs.add_argument("--option", help=(
        "Action argument: chosen option label, new budget USD for "
        "modify-budget, comma-separated squads for change-squads, or the "
        "stalled attended cursor's run_id for recover-stalled-stage"))
    rs.add_argument("--critique-ref", dest="critique_ref", metavar="PATH_OR_MEMORYREF",
                    help=(
                        "modify-plan only: a file path or repo:artifact:<path> "
                        "MemoryRef key naming the operator's revision critique. "
                        "Never pass the critique text itself via --option -- "
                        "that channel is character- and length-bounded."))
    # Cross-vendor finding 3: --gate-only and --live are MUTUALLY EXCLUSIVE,
    # not merely "informative" of one another -- --live constructs a live
    # MCPStdioDispatcher and starts a background eights spool drain before
    # any gate-only logic runs; that is exactly the live side effect the
    # attended gate-only route promises never to trigger. Enforced two ways:
    # (1) an argparse mutually-exclusive group rejects the combination before
    # any code in this module runs at all; (2) `_cmd_resume_locked` also
    # checks explicitly (defence in depth for any non-argparse caller).
    _rs_live_gate = rs.add_mutually_exclusive_group()
    _rs_live_gate.add_argument("--live", action="store_true",
                    help="Continue with the live MCP dispatcher (talks to pp_harness etc.). "
                         "Mutually exclusive with --gate-only.")
    _rs_live_gate.add_argument("--gate-only", dest="gate_only", action="store_true",
                    help=(
                        "Resolve the pending HITL gate (lock, operator-capability "
                        "mint+verify -- covering EVERY action that can mutate "
                        "checkpoint state or the spool, including reject -- spool "
                        "prune, state patch) WITHOUT re-entering the compiled graph "
                        "(no sup.invoke, no node_dispatch, no squad of any kind "
                        "runs). The attended MCP route (hydra.workflow.resume when "
                        "detached launch is not allowed) passes this flag for every "
                        "action EXCEPT recover-stalled-stage, which it refuses "
                        "outright before ever invoking this CLI (recovery is a live "
                        "operation, not a gate resolution -- use the detached "
                        "--live route for it instead); the host's existing "
                        "step/submit loop continues the workflow from its cursor. "
                        "Mutually EXCLUSIVE with --live: gate-only never spawns the "
                        "live MCP dispatcher and refuses outright if --live is also "
                        "given."))
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
    rp.add_argument(
        "--sanitize-non-finite",
        dest="sanitize_non_finite",
        action="store_true",
        help=(
            "Opt-in recovery for a poisoned source checkpoint (a persisted "
            "NaN/Infinity/-Infinity — see PoisonedStateError): loads the "
            "checkpoint through a sanitizing pass that replaces every "
            "non-finite value with null, reports every substituted field, "
            "and proceeds. Never the default; never writes the sanitized "
            "state back over the source checkpoint. Without this flag, "
            "replay of a poisoned checkpoint refuses exactly like `hydra "
            "status`/`hydra finalize` do."
        ),
    )
    rp.add_argument("--verbose", action="store_true")

    # budget: show or set workflow budget ledger
    bg = sub.add_parser("budget", help=(
        "Show or set workflow budget ledger. "
        "No args = list all workflows latest-first. "
        "With id = full ledger. "
        "--set USD = update the budget ceiling in the checkpoint."))
    bg.add_argument(
        "workflow_id", nargs="?", default=None,
        help="Workflow id to inspect or modify (omit to list all workflows).")
    bg.add_argument(
        "--set", dest="set_usd", metavar="USD", default=None,
        help=(
            "Update the budget cap for the workflow to this USD value. "
            "Persisted into the LangGraph checkpoint and traced. "
            "Requires a workflow_id argument."
        ),
    )

    # RA-7: eights spool drain subcommand
    ed = sub.add_parser(
        "eights-drain",
        help=(
            "Drain the eights-pending spool in a bounded batch. "
            "Re-issues spooled calls to the eights daemon and removes stale "
            ".partial files older than 1 hour. "
            "Prints JSON: {drained, failed, skipped, dead_lettered, "
            "dead_letter_depth, remaining}."
        ),
    )
    ed.add_argument(
        "--limit",
        type=int,
        default=500,
        metavar="N",
        help=(
            "Maximum number of spool entries to attempt in this run "
            "(default 500). Remaining entries stay in the spool for the next "
            "call."
        ),
    )
    ed.add_argument(
        "--max-age-hours",
        dest="max_age_hours",
        type=finite_float_arg("--max-age-hours"),
        default=24.0,
        metavar="H",
        help=(
            "Dead-letter spool entries older than H hours WITHOUT attempting "
            "a replay (default 24, kept for backwards compatibility; 0 "
            "disables the age check entirely). Entries dead-lettered for age "
            "emit a WARN on stderr — recover them with --replay-dead-letter."
        ),
    )
    ed.add_argument(
        "--replay-dead-letter",
        dest="replay_dead_letter",
        action="store_true",
        help=(
            "Move up to --limit entries from the dead-letter directory back "
            "into the pending spool (attempts reset to 0) and drain them with "
            "the age check disabled for that pass."
        ),
    )

    # E2-17: HITL lifecycle reconciliation against TheEights' shared ledger.
    ehr = sub.add_parser(
        "eights-hitl-reconcile",
        help=(
            "Reconcile TheEights' pending HITL requests against Hydra "
            "workflow state. Resolves rows whose workflow is terminal or "
            "unknown; leaves active gates alone. Dry-run unless --apply. "
            "Prints JSON: {pending, terminal, unknown, active, resolved}."
        ),
    )
    ehr.add_argument(
        "--apply", action="store_true",
        help="Actually resolve the zombie rows (default: dry-run report only).",
    )
    ehr.add_argument(
        "--limit", type=int, default=None, metavar="N",
        help="Examine at most N pending rows (default: all).",
    )

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

    # WS1-C: `hydra repo register|unregister|list` — self-service ~/.hydra/repos.json
    # administration. CLI-ONLY (never exposed as a writable MCP tool — see
    # repo_registry.register_repo's docstring for the security rationale: the
    # registry IS the allow-list, so a freely-callable MCP register tool would
    # put allow-list authorship inside the supervisor LLM's reach).
    repo_p = sub.add_parser("repo", help="Administer ~/.hydra/repos.json (operator-registered repo ids).")
    repo_sub = repo_p.add_subparsers(dest="repocmd", required=True)
    rreg = repo_sub.add_parser("register", help="Register (or overwrite) a repo id.")
    rreg.add_argument("repo_id")
    rreg.add_argument("path", help="Absolute path to the repo.")
    rreg.add_argument("--init", action="store_true",
                       help="Create the directory / run `git init` if it isn't a git repo yet.")
    rreg.add_argument("--force", action="store_true",
                       help="Allow shadowing a built-in repo id.")
    rureg = repo_sub.add_parser("unregister", help="Remove a registered repo id.")
    rureg.add_argument("repo_id")
    repo_sub.add_parser("list", help="List operator-registered repo ids.")

    args = ap.parse_args(argv)

    if args.cmd == "memory":
        memcmds = {"query": _cmd_memory_query, "tag": _cmd_memory_tag}
        return memcmds[args.memcmd](args)

    if args.cmd == "repo":
        return _cmd_repo(args)

    dispatch = {
        "doctor": _cmd_doctor,
        "verify": _cmd_verify,
        "squads": _cmd_squads,
        "run": _cmd_run,
        "plan": _cmd_plan,
        "step": _cmd_attended_step,
        "submit-host-result": _cmd_attended_submit,
        "finalize": _cmd_finalize,
        "status": _cmd_status,
        "trace": _cmd_trace,
        "budget": _cmd_budget,
        "reap": _cmd_reap,
        "sweep-worktrees": _cmd_sweep_worktrees,
        # C2: approve == resume --action approve (the old stub printed a
        # plugin pointer and did nothing; resume is now first-class).
        "approve": lambda a: _cmd_resume(argparse.Namespace(
            project=a.project, workflow_id=a.workflow_id, action="approve",
            option=None, live=getattr(a, "live", False), verbose=False)),
        "resume": _cmd_resume,
        "ingest": _cmd_ingest,
        "replay": _cmd_replay,
        "eights-drain": _cmd_eights_drain,
        "eights-hitl-reconcile": _cmd_eights_hitl_reconcile,
        "gateway-backup": _cmd_gateway_backup,
        "gateway-export-backends": _cmd_gateway_export_backends,
        "gateway-migrate-hooks": _cmd_gateway_migrate_hooks,
        "gateway-remove-old-backends": _cmd_gateway_remove_old_backends,
        "gateway-rollback": _cmd_gateway_rollback,
        "gateway-setup": _cmd_gateway_setup,
    }
    try:
        return dispatch[args.cmd](args)
    except PoisonedStateError as e:
        # Defense-in-depth catch-all (see `state.make_checkpoint_serde` and
        # `_cmd_finalize`'s dedicated handler above): every OTHER command
        # that touches a checkpoint (`step`, `submit-host-result`, `status`,
        # `budget`, `resume`, ...) routes through this single dispatch call,
        # so one handler here covers all of them without editing each command
        # individually — the choke point itself already did the only work
        # that matters (refusing to deserialize); this just keeps the CLI's
        # exit contract (`ok: false` JSON, not a bare traceback) uniform.
        print(_cli_json_dumps({
            "ok": False, "status": "unjudgeable",
            "workflow_id": getattr(args, "workflow_id", None),
            "field": e.field,
            "detail": (
                "the stored checkpoint contains a non-finite value at "
                f"{e.field}; refusing to read/advance this workflow."
            ),
        }, indent=2, default=str))
        return 0


if __name__ == "__main__":                                                  # pragma: no cover
    sys.exit(main())
