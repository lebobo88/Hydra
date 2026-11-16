"""In-process MCP tool catalog: the single source of truth for what the
``hydra_control`` and ``hydra_memory`` servers advertise.

DATA ONLY.  Each spec maps a tool name to ``{"description": str,
"inputSchema": dict}``.  Three consumers derive from it so they cannot drift
(Hydra#81, W0.3 of epic #78):

  * ``mcp_servers/hydra_control/server.py`` and
    ``mcp_servers/hydra_memory/server.py`` build their ``_TOOL_SCHEMAS`` from
    these specs (the handler registries stay in the servers);
  * ``hydra_core.toolshed`` derives ``HYDRA_CONTROL_TOOLS`` /
    ``HYDRA_MEMORY_TOOLS`` and the gateway fallback ``SCHEMA_OVERRIDES`` from
    the spec keys / ``inputSchema`` values;
  * ``tests/test_inproc_tool_catalog_parity.py`` asserts the server handler
    registries equal the spec keys.

Import discipline: standard library plus the package root ``hydra_core.eights``
(for ``ALL_CELLS``) ONLY.  No ``hydra_core.eights`` submodule, no
``hydra_core.memory``, no ``mcp_servers``, no MCP/provider SDK.  Importing
``hydra_core.toolshed`` must stay cheap and side-effect free.

Ordering: previously advertised tools first (in their historical toolshed
order), then tools newly advertised by Hydra#81.  repo register/unregister are
CLI-only and deliberately not listed.
"""
from __future__ import annotations

from typing import Any

from hydra_core.eights import ALL_CELLS

# ---------------------------------------------------------------- hydra_control

_RESUME_ACTIONS = ("approve", "reject", "modify-budget", "force-dispatch",
                   "change-squads", "recover-stalled-stage", "modify-plan")

HYDRA_CONTROL_TOOL_SPECS: dict[str, dict[str, Any]] = {
    "hydra.control.ping": {
        "description": ("No-arg liveness probe: returns ok + hydra root. "
                        "Used by AgentMesh's mcp-tool-call healthProbe."),
        "inputSchema": {"type": "object", "properties": {}},
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
                "budget": {"type": "number", "description": "Budget cap in USD (optional). Must be finite: NaN/Infinity are rejected."},
                "workflow_id": {"type": "string",
                                "description": "Pre-allocated workflow id (optional)."},
                "risk": {"type": "string", "enum": ["low", "medium", "high"],
                         "description": "Operator risk tolerance hint forwarded as --risk to the CLI (optional)."},
                "repo": {"type": "string",
                         "description": ("Single allow-listed repo id for engineering targeting "
                                        "(forwarded as --repo; pre-seeded onto HydraState.target_repo_id, "
                                        "validated at intake). Mutually exclusive with repos.")},
                "repos": {"type": "string",
                          "description": ("Comma-separated allow-listed repo ids for fleet mode, "
                                         ">=2 distinct ids (forwarded as --repos). Mutually exclusive with repo.")},
                "repo_subpath": {"type": "string",
                                 "description": ("Repo-relative engineering target under repo/repos "
                                                "(forwarded as --subdir).")},
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
                "budget": {"type": "number", "description": "Budget cap in USD (optional). Must be finite: NaN/Infinity are rejected."},
                "workflow_id": {"type": "string",
                                "description": "Pre-allocated workflow id (optional)."},
                "risk": {"type": "string", "enum": ["low", "medium", "high"],
                         "description": "Operator risk tolerance hint forwarded as --risk to the CLI (optional)."},
                "rigor": {"type": "string", "enum": ["trivial", "standard", "major"],
                          "description": ("Operator override of node_planner's computed plan_rigor, "
                                         "forwarded as --rigor to the CLI (optional). Wins over the "
                                         "computed value; a downgrade from the computed value is "
                                         "recorded as a hitl_history event.")},
                "repo": {"type": "string",
                         "description": ("Single allow-listed repo id for engineering targeting "
                                        "(forwarded as --repo; pre-seeded onto HydraState.target_repo_id, "
                                        "validated at intake). Mutually exclusive with repos.")},
                "repos": {"type": "string",
                          "description": ("Comma-separated allow-listed repo ids for fleet mode, "
                                         ">=2 distinct ids (forwarded as --repos). Mutually exclusive with repo.")},
                "repo_subpath": {"type": "string",
                                 "description": ("Repo-relative engineering target under repo/repos "
                                                "(forwarded as --subdir).")},
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
    "hydra.workflow.finalize": {
        "description": (
            "Attended execution: close the workflow through the ENGINE. Call it "
            "once hydra.workflow.step returns {status:'ready_to_finalize'}. "
            "Materialises every attended task result (engineering stages and "
            "claude-skill/impersonation squad cursors) into squad DECISION_RECORD "
            "envelopes + artifact rows, then resumes the graph so synthesis -> "
            "judge_synthesis -> postcheck run over the real outputs: the engine "
            "DECISION_RECORD is persisted to episodic memory (RA-8) and the phase "
            "becomes terminal. Returns {ok:true, status:'finalized', "
            "decision_record_id, phase, artifact_refs} or {ok:false, "
            "status:'tasks_pending', pending:[task_id,...]} when the attended loop "
            "is not done. Idempotent: a second call returns "
            "{ok:true, status:'already_finalized', decision_record_id}."),
        "inputSchema": {
            "type": "object",
            "properties": {"workflow_id": {"type": "string"}},
            "required": ["workflow_id"],
        },
    },
    "hydra.workflow.resume": {
        "description": (
            "Resolve a pending HITL gate. Transport depends on "
            "HYDRA_ALLOW_DETACHED: with the gate set (automation), launches a "
            "DETACHED `hydra resume --live` CLI subprocess and returns "
            "immediately ({ok, launched, pid, log}). Without it (the normal "
            "interactive session), runs `hydra resume --gate-only` "
            "SYNCHRONOUSLY in-process and returns the resolved gate's result "
            "in the same call ({ok, resumed: false, gate_only: true, "
            "graph_reentered: false, phase, pending_hitl, ...}) — the gate "
            "clears (lock, operator-capability mint+verify, spool prune, "
            "state patch) but the compiled graph is NEVER re-entered (no "
            "sup.invoke, no node_dispatch, no squad runs); the caller's "
            "existing step/submit loop continues the workflow from its "
            "cursor. Refuses with {ok: false, error: "
            "\"operator_identity_required\"} before touching any state if "
            "the operator identity is unknown or the minted capability is "
            "degraded -- this check covers EVERY action that can mutate "
            "checkpoint state or the spool, including 'reject' (not just "
            "'approve'/'force-dispatch'/'modify-budget'/'change-squads'/"
            "'modify-plan'). action='recover-stalled-stage' is the ONE "
            "exception: it is a LIVE operation (can replay a pp verdict, "
            "run smoke/finalize, and merge code), so it is refused outright "
            "on this attended route ({ok: false, error: "
            "\"recovery_is_live_operation\"}) before any subprocess runs; "
            "only the detached `hydra resume --live --action "
            "recover-stalled-stage` route (HYDRA_ALLOW_DETACHED=1) may run "
            "it. Idempotent at the CLI layer — no pending gate means "
            "no-op, and a retry after an interrupted gate-only resume "
            "reconciles any stale spooled HITL request for the "
            "already-resolved gate. WRITE tool: only reachable via meshd's "
            "sanctioned write path."),
        "inputSchema": {
            "type": "object",
            "properties": {
                "workflow_id": {"type": "string"},
                "action": {"type": "string", "enum": list(_RESUME_ACTIONS)},
                "option": {"type": "string"},
                "critique_ref": {
                    "type": "string",
                    "description": (
                        "modify-plan only: a file path or repo:artifact:<path> "
                        "MemoryRef key naming the operator's revision critique. "
                        "The critique text itself must never be passed via "
                        "'option' -- that field is character- and length-bounded."
                    ),
                },
            },
            "required": ["workflow_id", "action"],
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
    "hydra.repo.list": {
        "description": (
            "READ-ONLY: list the repo allow-list -- built-in ids plus operator-"
            "registered ids from ~/.hydra/repos.json. Registration itself is "
            "CLI-only (`hydra repo register`) and NOT exposed over MCP, since "
            "the registry IS the allow-list. Returns {ok, builtin, registered}."),
        "inputSchema": {"type": "object", "properties": {}},
    },
    "hydra.repo.resolve": {
        "description": (
            "READ-ONLY: resolve an allow-listed repo_id to its absolute path, "
            "with a real git-root verification (same check used at dispatch "
            "time). Returns {ok:true, repo_id, path} or {ok:false, error}."),
        "inputSchema": {
            "type": "object",
            "properties": {
                "repo_id": {"type": "string", "description": "Allow-listed repo id to resolve."},
            },
            "required": ["repo_id"],
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
                        "Must be a finite, non-negative number (NaN/Infinity are "
                        "rejected)."
                    ),
                },
            },
        },
    },
    "hydra.worktree.sweep_status": {
        "description": (
            "READ-ONLY preview of the attended-worktree janitor "
            "(host_bridge.sweep_stale_worktrees): reports, per worktree, "
            "its cursor state and the decision that would be made -- "
            "'removed'/'would-remove', 'skipped-non-terminal', "
            "'skipped-no-cursor', or 'error' -- without deleting anything. "
            "Always runs dry-run regardless of any input; the deleting form "
            "(`hydra sweep-worktrees --apply`) is CLI-only and NOT exposed "
            "over MCP, same allow-list-authorship boundary as `hydra repo "
            "register` (see hydra.repo.list above). Never deletes a git "
            "branch. Returns {ok, mode, project, count_removed, "
            "count_skipped, count_errors, entries}."),
        "inputSchema": {
            "type": "object",
            "properties": {
                "project": {
                    "type": "string",
                    "description": "Project root to scan (defaults to this Hydra checkout).",
                },
            },
        },
    },
}

# ---------------------------------------------------------------- hydra_memory

_CELL_ENUM = sorted(ALL_CELLS)

HYDRA_MEMORY_TOOL_SPECS: dict[str, dict[str, Any]] = {
    "hydra-mem.write_episodic": {
        "description": "Append a payload to episodic memory; returns a MemoryRef key.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "workflow_id": {"type": "string"},
                "kind": {"type": "string", "default": "note"},
                "payload": {"type": "object"},
                "key": {"type": "string"},
                "cells": {"type": "array", "items": {"type": "string",
                                                     "enum": _CELL_ENUM}},
                "origin_squad": {"type": "string"},
            },
            "required": ["workflow_id"],
        },
    },
    "hydra-mem.read_episodic": {
        "description": "Resolve an episodic key to its full row.",
        "inputSchema": {
            "type": "object",
            "properties": {"key": {"type": "string"}},
            "required": ["key"],
        },
    },
    "hydra-mem.list_workflow": {
        "description": "List every episodic row for a workflow_id.",
        "inputSchema": {
            "type": "object",
            "properties": {"workflow_id": {"type": "string"}},
            "required": ["workflow_id"],
        },
    },
    "hydra-mem.semantic_search": {
        "description": ("Full-text search over episodic memory by query "
                        "(LIKE across payload/kind/key). Optional workflow_id "
                        "and cell narrow the scan."),
        "inputSchema": {
            "type": "object",
            "properties": {
                "query": {"type": "string"},
                "k": {"type": "integer", "minimum": 1, "maximum": 50,
                      "default": 5},
                "workflow_id": {"type": "string"},
                "cell": {"type": "string", "enum": _CELL_ENUM},
                "index": {"type": "string"},
                "embedding": {"type": "array", "items": {"type": "number"}},
            },
        },
    },
    "hydra-mem.query_eights": {
        "description": "Query episodic rows tagged with a TheEights cell.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "cell": {"type": "string", "enum": _CELL_ENUM},
                "limit": {"type": "integer", "minimum": 1, "default": 50},
                "workflow_id": {"type": "string"},
            },
            "required": ["cell"],
        },
    },
    "hydra-mem.tag_memory": {
        "description": "Attach TheEights cells to an existing episodic row.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "key": {"type": "string"},
                "cells": {"type": "array", "items": {"type": "string",
                                                     "enum": _CELL_ENUM}},
                "replace": {"type": "boolean", "default": False},
            },
            "required": ["key"],
        },
    },
    "hydra-mem.ping": {
        "description": ("No-arg liveness probe: returns ok + episodic DB path. "
                        "Used by AgentMesh's mcp-tool-call healthProbe."),
        "inputSchema": {
            "type": "object",
            "properties": {},
        },
    },
    "hydra-mem.workflows_list": {
        "description": ("Read-only list of workflows from the LangGraph "
                        "checkpoint store (phase, squads, pending-HITL flag), "
                        "newest first."),
        "inputSchema": {
            "type": "object",
            "properties": {
                "limit": {"type": "integer", "minimum": 1, "maximum": 200,
                          "default": 50},
            },
        },
    },
    "hydra-mem.workflow_status": {
        "description": ("Read-only live state of one workflow: phase, budget, "
                        "tasks, pending_hitl, envelope/verdict counts."),
        "inputSchema": {
            "type": "object",
            "properties": {"workflow_id": {"type": "string"}},
            "required": ["workflow_id"],
        },
    },
    "hydra-mem.squad_list": {
        "description": ("Read-only discovered squad packs: roster, gates, "
                        "accepts/emits envelope types, best_of_n."),
        "inputSchema": {
            "type": "object",
            "properties": {},
        },
    },
    "hydra-mem.hitl_pending": {
        "description": ("Read-only pending HITL gates across all workflows "
                        "(workflow_id + gate_node + the HITL payload). The "
                        "live-truth complement to TheEights' hitl_queue."),
        "inputSchema": {
            "type": "object",
            "properties": {},
        },
    },
}
