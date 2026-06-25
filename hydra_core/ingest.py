"""Continuation transport — inject host-completed skill envelopes back into a
running workflow and dispatch the engineering leg deterministically.

Why this module exists
-----------------------
claude-skill squads (``rlm-gaming``, ``garland``) cannot run headlessly: the
live dispatcher returns ``{"status": "host_pickup_required"}`` for
``invoke_claude_skill`` (``dispatcher.py``). So a game design step runs inside
the Claude Code *host* (the model invoking the Skill), and the host captures the
skill's emitted ``DEV_TASK`` / ``PRD`` / ``ARCH_RFC`` envelopes. Those envelopes
then have nowhere to go: there was no surface to feed them back into the *same*
workflow so the deterministic Python engine forwards them to the ``engineering``
squad and runs the pair-programmer stage loop.

This module is that surface. It reuses the exact lower-level primitives the
in-graph forwarding sweep uses (``execute_squad`` -> ``_via_mcp`` ->
``_drive_pp_stage_loop``), so an ingested ``DEV_TASK`` is dispatched through the
full ``start_stage -> generate -> archive_artifact -> record_attempt ->
record_verdict -> finalize_stage -> finalize_run`` cycle with cross-vendor
judges — never written by the supervisor LLM.

Exactly-once / lock safety
--------------------------
The codex review flagged double-dispatch (and leaked ``.harness`` pp locks) as
the highest risk. We defend with two layers:

* **Dedup** — an envelope id already tied to a ``TaskState`` (``envelope_id``)
  or already recorded in the per-workflow ledger is skipped.
* **Claim-before-dispatch under the resume lock** — the CLI wrapper persists the
  ids to ``.hydra/<wf>/ingested.json`` *before* dispatching, while holding the
  same atomic resume lock ``hydra resume`` uses. A crash mid-dispatch therefore
  yields at-most-once dispatch; ``_via_mcp`` has already registered the pp run on
  ``state.open_pp_runs`` so ``node_postcheck`` / ``hydra reap`` can finalize-abort
  the lock.
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Iterable

from .governance import charge_and_gate, redact_for_squad_boundary
from .schemas import HydraEnvelope, validate_envelope
from .squad_node import execute_squad
from .state import HydraState, TaskState
# Reuse the in-graph routing + cost helpers so ingest and node_dispatch stay in
# lockstep. supervisor's langgraph import is guarded (pure-python fallback), so
# importing these module-level helpers is safe even without langgraph.
from .supervisor import (
    _FORWARD_TARGET_BY_TYPE,
    _extract_squad_cost,
    _resolve_forward_target,
)

# Text fields redacted at the squad boundary before engineering/garland sees the
# semi-trusted skill output. Mirrors supervisor._validate_and_redact_envelope.
_REDACT_TEXT_FIELDS = (
    "objective", "summary", "instructions", "decision",
    "rationale", "risk_assessment", "rollout_plan",
)


def _redact_envelope_dict(env_dict: dict) -> dict:
    """Validate + redact an envelope dict at a squad boundary (raises on
    invalid schema). Returns a new dict; the input is not mutated."""
    validate_envelope(env_dict)  # raises on bad schema — caller fails the item
    redacted = dict(env_dict)
    for fld in _REDACT_TEXT_FIELDS:
        if isinstance(redacted.get(fld), str):
            redacted[fld] = redact_for_squad_boundary(redacted[fld])
    return redacted


@dataclass
class IngestItemResult:
    envelope_id: str
    envelope_type: str | None
    target: str | None
    status: str          # done | failed | surfaced | running | skipped_duplicate | deferred_to_host | unknown_target
    run_id: str | None = None
    detail: str = ""


@dataclass
class IngestOutcome:
    items: list[IngestItemResult] = field(default_factory=list)
    new_tasks: list[TaskState] = field(default_factory=list)
    new_envelopes: list[dict] = field(default_factory=list)
    charged_usd: float = 0.0
    charged_tokens: int = 0
    # Budget gates (parity with node_dispatch): set when an ingested dispatch
    # pushes the workflow >= 80% (downgrade) / >= 100% (block). On `over_budget`
    # the loop stops and the wrapper surfaces an over_budget HITL.
    budget_downgrade: bool = False
    over_budget: bool = False

    @property
    def dispatched_ids(self) -> list[str]:
        """Ids that were actually handed to a squad (so the ledger claims them).

        A duplicate-skip or an unknown-target item is NOT claimed — only items
        that reached `execute_squad` (done/failed/surfaced/running) or were
        intentionally deferred to the host for a follow-up skill run."""
        return [
            it.envelope_id for it in self.items
            if it.status not in ("skipped_duplicate", "unknown_target")
        ]

    def summary(self) -> dict[str, Any]:
        return {
            "items": [vars(it) for it in self.items],
            "dispatched": [it.envelope_id for it in self.items if it.status in ("done", "running")],
            "failed": [it.envelope_id for it in self.items if it.status in ("failed", "surfaced")],
            "skipped_duplicate": [it.envelope_id for it in self.items if it.status == "skipped_duplicate"],
            "deferred_to_host": [it.envelope_id for it in self.items if it.status == "deferred_to_host"],
            "charged_usd": self.charged_usd,
            "charged_tokens": self.charged_tokens,
        }


def dispatch_ingested_envelopes(
    state: HydraState,
    raw_envelopes: Iterable[dict | HydraEnvelope],
    *,
    packs: dict[str, Any],
    dispatcher: Any,
    already_ingested: Iterable[str] = (),
    emit_fn: Callable[[str, dict], None] | None = None,
) -> IngestOutcome:
    """Deterministically dispatch host-completed skill envelopes.

    Pure of any filesystem/lock/checkpoint I/O so it is unit-testable with a
    fake dispatcher and an in-memory ``HydraState`` (see
    ``tests/test_hybrid_dispatch_e2e.py``). The CLI wrapper (``run_ingest``)
    layers the ledger, resume lock, and checkpoint persistence on top.

    Routing mirrors the in-graph forwarding sweep:
      * ``DEV_TASK`` / ``PRD`` / ``ARCH_RFC`` -> ``engineering`` (mcp; dispatched
        here through the pp stage loop).
      * ``CREATIVE_BRIEF`` / ``SHOT_LIST`` / ``ASSET_JOB`` -> ``garland``. garland
        is itself a claude-skill squad, so it cannot run headlessly — those
        items are returned with ``status="deferred_to_host"`` for the host to run
        as a follow-up skill, never silently dropped.

    Dedup: an envelope whose id is already a ``TaskState.envelope_id`` or in
    ``already_ingested`` (the ledger) is skipped (``skipped_duplicate``). Within
    a single call, a repeated id is also skipped.
    """
    def _emit(event: str, payload: dict) -> None:
        if emit_fn is not None:
            try:
                emit_fn(event, payload)
            except Exception:  # noqa: BLE001 — tracing must never break dispatch
                pass

    seen_existing: set[str] = {
        str(t.envelope_id) for t in state.tasks if t.envelope_id is not None
    } | {str(x) for x in already_ingested}

    outcome = IngestOutcome()

    for raw in raw_envelopes:
        # Normalise to a typed envelope (validate raw dicts; pass through
        # already-typed envelopes).
        try:
            env = raw if isinstance(raw, HydraEnvelope) else validate_envelope(dict(raw))
        except Exception as exc:  # noqa: BLE001 — bad envelope is an item failure, not a crash
            outcome.items.append(IngestItemResult(
                envelope_id=str((raw or {}).get("id", "?")) if isinstance(raw, dict) else "?",
                envelope_type=(raw or {}).get("type") if isinstance(raw, dict) else None,
                target=None, status="failed", detail=f"invalid envelope: {exc}",
            ))
            continue

        eid = str(env.id)
        etype = getattr(env, "type", None)

        if eid in seen_existing:
            outcome.items.append(IngestItemResult(
                envelope_id=eid, envelope_type=etype, target=None,
                status="skipped_duplicate", detail="already ingested / already a task",
            ))
            _emit("ingest.skip_duplicate", {"envelope_id": eid, "type": etype})
            continue
        seen_existing.add(eid)

        target = _resolve_forward_target(env, getattr(env, "origin_squad", "") or "")
        if target is None or target not in _FORWARD_TARGET_BY_TYPE.values():
            outcome.items.append(IngestItemResult(
                envelope_id=eid, envelope_type=etype, target=target,
                status="unknown_target",
                detail=f"no delegation target for type={etype!r}",
            ))
            _emit("ingest.unknown_target", {"envelope_id": eid, "type": etype})
            continue

        target_pack = packs.get(target)
        if target_pack is None:
            outcome.items.append(IngestItemResult(
                envelope_id=eid, envelope_type=etype, target=target,
                status="unknown_target", detail=f"target squad {target!r} not discovered",
            ))
            continue

        # Non-mcp targets (garland) cannot run headlessly — defer to the host.
        if getattr(target_pack, "entrypoint", None) != "mcp":
            outcome.items.append(IngestItemResult(
                envelope_id=eid, envelope_type=etype, target=target,
                status="deferred_to_host",
                detail=f"{target} is entrypoint={getattr(target_pack, 'entrypoint', '?')}; "
                       "run the skill in-host and re-ingest its emitted envelopes",
            ))
            _emit("ingest.deferred_to_host", {"envelope_id": eid, "target": target})
            continue

        # Redact at the boundary, then re-validate to a typed envelope so
        # execute_squad receives clean, schema-valid input (mirrors the sweep).
        try:
            safe_dict = _redact_envelope_dict(env.model_dump(mode="json"))
            safe_env = validate_envelope(safe_dict)
        except Exception as exc:  # noqa: BLE001
            outcome.items.append(IngestItemResult(
                envelope_id=eid, envelope_type=etype, target=target,
                status="failed", detail=f"redaction/validation failed: {exc}",
            ))
            continue

        task = TaskState(
            owner_squad=target,
            description=(
                getattr(env, "instructions", None)
                or getattr(env, "summary", None)
                or getattr(env, "objective", None)
                or f"ingested {etype} from {getattr(env, 'origin_squad', '?')}"
            ),
            envelope_id=env.id,
            target_repo_id=getattr(env, "target_repo_id", None) or state.target_repo_id,
            pp_team=getattr(env, "pp_team", None),
            pp_profile=getattr(env, "pp_profile", None),
        )

        try:
            result = execute_squad(state, target_pack, safe_env, dispatcher)
        except Exception as exc:  # noqa: BLE001 — one bad dispatch must not abort the batch
            task.status = "failed"
            outcome.new_tasks.append(task)
            outcome.items.append(IngestItemResult(
                envelope_id=eid, envelope_type=etype, target=target,
                status="failed", detail=f"execute_squad raised: {exc}",
            ))
            continue

        # Charge + gate through the SAME helper node_dispatch uses, so ingested
        # engineering honours the 80% downgrade tripwire and the >= 100% block —
        # not a budget-blind side door (codex review item 3).
        cost_usd, cost_tok = _extract_squad_cost(result)
        block, downgrade = charge_and_gate(state, cost_usd, cost_tok)
        outcome.charged_usd += cost_usd
        outcome.charged_tokens += cost_tok
        if downgrade:
            outcome.budget_downgrade = True
            state.budget_downgrade_active = True

        task.status = result.status
        outcome.new_tasks.append(task)

        run_id = None
        for produced in getattr(result, "envelopes", []):
            d = produced.model_dump(mode="json")
            # Tag so a later in-graph pass never re-forwards/re-judges these.
            d["_forwarded"] = True
            d["_ingested"] = True
            d["_task_id"] = str(task.task_id)
            if getattr(result, "host_pickup_pending", False):
                d["_host_pickup_pending"] = True
            if getattr(result, "pp_loop_terminal", False):
                d["_pp_loop_terminal"] = True
            outcome.new_envelopes.append(d)

        for art in getattr(result, "artifacts", []):
            if isinstance(art, dict) and art.get("kind") == "pp_run" and run_id is None:
                run_id = art.get("ref")

        outcome.items.append(IngestItemResult(
            envelope_id=eid, envelope_type=etype, target=target,
            status=result.status, run_id=run_id,
            detail=(getattr(result, "rationale", "") or "")[:200],
        ))
        _emit("ingest.dispatched", {
            "envelope_id": eid, "type": etype, "target": target,
            "status": result.status, "run_id": run_id,
        })

        if block:
            # >= 100%: stop dispatching the rest of the batch; the wrapper
            # surfaces an over_budget HITL (parity with node_dispatch).
            outcome.over_budget = True
            outcome.budget_downgrade = True
            state.budget_downgrade_active = True
            _emit("ingest.over_budget", {
                "spent_usd": state.budget.spent_usd,
                "budget_usd": state.budget.budget_usd,
                "last_envelope_id": eid,
            })
            break

    return outcome


# ---------------------------------------------------------------------------
# Per-workflow ingest ledger (exactly-once across retries / crashes)
# ---------------------------------------------------------------------------

def ingest_ledger_path(project_root: Path, workflow_id: str) -> Path:
    return Path(project_root) / ".hydra" / str(workflow_id) / "ingested.json"


def load_ingested_ids(project_root: Path, workflow_id: str) -> set[str]:
    p = ingest_ledger_path(project_root, workflow_id)
    if not p.exists():
        return set()
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return set()
    ids = data.get("ingested_ids") if isinstance(data, dict) else data
    return {str(x) for x in ids} if isinstance(ids, (list, set)) else set()


def _write_ledger(project_root: Path, workflow_id: str, ids: set[str]) -> None:
    p = ingest_ledger_path(project_root, workflow_id)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps({"ingested_ids": sorted(ids)}, indent=2), encoding="utf-8")


def claim_ingested_ids(project_root: Path, workflow_id: str, ids: Iterable[str]) -> set[str]:
    """Merge `ids` into the per-workflow ledger and persist. Returns the full
    set. Caller MUST hold the resume lock so this read-modify-write is atomic."""
    merged = load_ingested_ids(project_root, workflow_id) | {str(x) for x in ids}
    _write_ledger(project_root, workflow_id, merged)
    return merged


def release_ingested_ids(project_root: Path, workflow_id: str, ids: Iterable[str]) -> set[str]:
    """Remove `ids` from the ledger (un-claim). Used when a pre-claimed envelope
    never actually reached a squad (unknown_target / parse failure), so a
    corrected re-submit with the same id is NOT suppressed as a duplicate. Caller
    MUST hold the resume lock."""
    remaining = load_ingested_ids(project_root, workflow_id) - {str(x) for x in ids}
    _write_ledger(project_root, workflow_id, remaining)
    return remaining
