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
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Iterable
from uuid import UUID, uuid4

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


# ---------------------------------------------------------------------------
# E2-34: pack-envelope normalization
# ---------------------------------------------------------------------------
# A claude-skill pack (e.g. RLM-Gaming's Director) hand-writes a DEV_TASK from
# the prose contract, not from ``hydra_core.schemas.DevTask``. It emitted
# {type, origin_squad, target_squad, workflow_id, repo, pp_team, title,
# instructions, acceptance_criteria, budget_usd} — missing the REQUIRED
# ``owner`` and ``branch``, so validation failed and the delegation was dropped
# inside a top-level "complete" result (finding E2-34). We now fill safe
# defaults before validation and fold pack-only keys into real schema fields so
# nothing is silently lost.

#: The ``DevTask.owner`` literal set, in tie-break priority order.
DEV_TASK_OWNERS: tuple[str, ...] = ("frontend", "backend", "devops", "data", "fullstack")

#: Keyword -> owner inference table. Matched case-insensitively on word
#: boundaries against ``pp_team`` (weighted x2 — the most explicit signal),
#: ``title``, and ``instructions``. Highest score wins; ties break in
#: ``DEV_TASK_OWNERS`` order; no hit at all falls back to ``"fullstack"``.
OWNER_KEYWORDS: dict[str, tuple[str, ...]] = {
    "frontend": ("ui", "ux", "html", "css", "frontend", "front-end", "react",
                 "component", "stylesheet", "layout", "design-system", "widget"),
    "backend": ("api", "db", "database", "backend", "back-end", "server",
                "endpoint", "handler", "service", "sql", "rpc"),
    "devops": ("deploy", "deployment", "ci", "cd", "pipeline", "docker",
               "kubernetes", "infra", "infrastructure", "release", "runbook"),
    "data": ("data", "etl", "elt", "dataset", "warehouse", "analytics",
             "telemetry", "ingestion"),
}

_WORD_SPLIT_RE = re.compile(r"[^a-z0-9]+")
_SLUG_STRIP_RE = re.compile(r"^-+|-+$")


def _tokens(text: str) -> list[str]:
    return [t for t in _WORD_SPLIT_RE.split(str(text).lower()) if t]


def infer_dev_task_owner(pp_team: str | None, title: str | None,
                         instructions: str | None) -> str:
    """Infer a ``DevTask.owner`` literal from the pack's free-text fields.

    Deterministic: score each owner by keyword hits (``pp_team`` counts double),
    take the highest score, break ties in ``DEV_TASK_OWNERS`` order, and fall
    back to ``"fullstack"`` when nothing matches.
    """
    scores: dict[str, int] = {owner: 0 for owner in OWNER_KEYWORDS}
    for text, weight in ((pp_team, 2), (title, 1), (instructions, 1)):
        if not text:
            continue
        toks = set(_tokens(text))
        # Hyphenated keywords ("front-end") are also compared against the raw
        # lowercased text, since tokenizing splits them apart.
        raw = str(text).lower()
        for owner, words in OWNER_KEYWORDS.items():
            for word in words:
                if (word in toks) or ("-" in word and word in raw):
                    scores[owner] += weight
    best = max(scores.values())
    if best == 0:
        return "fullstack"
    for owner in DEV_TASK_OWNERS:
        if scores.get(owner) == best:
            return owner
    return "fullstack"  # pragma: no cover — unreachable; best came from scores


def slugify(text: str, *, max_words: int = 6, max_len: int = 48) -> str:
    """Lowercase hyphen-joined slug of at most ``max_words`` words."""
    words = _tokens(text)[:max_words]
    return _SLUG_STRIP_RE.sub("", "-".join(words)[:max_len])


def default_dev_task_branch(workflow_id: Any, title: str | None,
                            instructions: str | None) -> str:
    """``hydra/<workflow-short8>/<slug>`` — the branch a pack omitted."""
    wf_short = str(workflow_id or "unknown").replace("-", "")[:8] or "unknown"
    slug = slugify(title or "") or slugify(instructions or "") or "dev-task"
    return f"hydra/{wf_short}/{slug}"


#: Keys packs commonly emit that are NOT ``DevTask`` fields. Pydantic ignores
#: extras, so without this they would vanish. Each is folded into a real field.
_DEV_TASK_PACK_ONLY_KEYS: tuple[str, ...] = ("title", "acceptance_criteria", "budget_usd")


def _normalize_envelope_id(out: dict, defaulted: list[str]) -> None:
    """Guarantee ``out["id"]`` is a UUID string, preserving a non-UUID original.

    ``HydraEnvelope.id`` is a ``UUID``. A host-run pack labels its envelopes
    however it likes — the observed case was ``"devtask-hydra-heads-166fc7ee"``,
    which ``hydra.workflow.submit_envelopes`` accepted and the detached ingest
    then rejected with "id Input should be a valid UUID", visible only in
    ``ingest.log``. We mint a UUID4 and keep the original in ``external_id`` so
    the pack's own reference still resolves.

    A MISSING id is synthesized too: the dedup ledger keys on the id, so an
    envelope without one would bypass ``processed`` entirely.
    """
    raw_id = out.get("id")
    if raw_id is not None and str(raw_id).strip():
        try:
            UUID(str(raw_id))
            return
        except (ValueError, AttributeError, TypeError):
            if not out.get("external_id"):
                out["external_id"] = str(raw_id)
                defaulted.append("external_id")
    out["id"] = str(uuid4())
    defaulted.append("id")


def normalize_pack_envelope(env: dict) -> dict:
    """Fill safe defaults on a pack-emitted envelope before schema validation.

    ``id`` is normalized for EVERY envelope type (see ``_normalize_envelope_id``);
    the remaining repairs apply to ``DEV_TASK``, the type packs hand-write most
    often. The returned dict carries ``_normalized_fields`` — the list of fields
    defaulted or folded — which the caller emits as
    ``ingest.envelope_normalized``. Pydantic treats that key as an extra and
    drops it at validation.

    Never raises: a shape this cannot repair falls through to validation, which
    then reports the real error.
    """
    if not isinstance(env, dict):
        return env
    out = dict(env)
    defaulted: list[str] = []
    _normalize_envelope_id(out, defaulted)
    if out.get("type") != "DEV_TASK":
        if defaulted:
            out["_normalized_fields"] = defaulted
        return out

    title = out.get("title") if isinstance(out.get("title"), str) else None
    instructions = out.get("instructions") if isinstance(out.get("instructions"), str) else None
    pp_team = out.get("pp_team") if isinstance(out.get("pp_team"), str) else None

    # --- owner -------------------------------------------------------------
    if not out.get("owner"):
        out["owner"] = infer_dev_task_owner(pp_team, title, instructions)
        defaulted.append("owner")

    # --- branch ------------------------------------------------------------
    if not out.get("branch"):
        out["branch"] = default_dev_task_branch(out.get("workflow_id"), title, instructions)
        defaulted.append("branch")

    # --- repo / target_repo_id ---------------------------------------------
    repo = out.get("repo")
    if isinstance(repo, str) and repo and not out.get("target_repo_id"):
        try:
            from .repo_registry import is_known_repo
            if is_known_repo(repo):
                out["target_repo_id"] = repo
                defaulted.append("target_repo_id")
        except Exception:  # noqa: BLE001 — registry trouble must not block ingest
            pass
    if not out.get("repo"):
        # ``repo`` is required by the schema; fall back to the resolved repo id
        # so a pack that only set target_repo_id still validates.
        out["repo"] = str(out.get("target_repo_id") or ".")
        defaulted.append("repo")

    # --- fold pack-only keys into real fields so nothing is lost ------------
    tail_lines: list[str] = []

    acceptance = out.pop("acceptance_criteria", None)
    if acceptance:
        items = acceptance if isinstance(acceptance, list) else [acceptance]
        existing = list(out.get("test_plan") or [])
        for item in items:
            text = str(item).strip()
            if text and text not in existing:
                existing.append(text)
        out["test_plan"] = existing
        defaulted.append("test_plan<-acceptance_criteria")

    budget = out.pop("budget_usd", None)
    if budget is not None:
        constraints = dict(out.get("constraints") or {})
        folded = False
        if constraints.get("budget_usd") is None:
            try:
                constraints["budget_usd"] = float(budget)
                out["constraints"] = constraints
                defaulted.append("constraints.budget_usd<-budget_usd")
                folded = True
            except (TypeError, ValueError):
                folded = False
        if not folded:
            tail_lines.append(f"budget_usd: {budget}")
    if title:
        out.pop("title", None)
        tail_lines.insert(0, f"Title: {title}")

    # Any remaining key that is neither a DevTask field nor an internal ingest
    # marker is appended verbatim so the receiving squad still sees it.
    from .schemas import DevTask as _DevTask
    known = set(_DevTask.model_fields)
    for key in sorted(k for k in list(out) if k not in known
                      and not k.startswith("_") and k not in _DEV_TASK_PACK_ONLY_KEYS):
        tail_lines.append(f"{key}: {out.pop(key)!r}")

    if tail_lines:
        tail = "\n".join(tail_lines)
        base = out.get("instructions")
        out["instructions"] = (
            f"{base}\n\n[from originating pack]\n{tail}"
            if isinstance(base, str) and base
            else f"[from originating pack]\n{tail}"
        )
        defaulted.append("instructions<-pack_only_keys")

    if defaulted:
        out["_normalized_fields"] = defaulted
    return out


def normalize_for_ingest(env: dict,
                         emit_fn: Callable[[str, dict], None] | None = None) -> dict:
    """``normalize_pack_envelope`` + the ``ingest.envelope_normalized`` trace.

    Returns the normalized dict with the ``_normalized_fields`` marker stripped,
    so callers can hand it straight to ``validate_envelope`` or persist it.
    Idempotent: normalizing an already-normalized envelope defaults nothing and
    emits nothing, which is what lets the CLI normalize once for dedup and still
    pass the dict through ``dispatch_ingested_envelopes``.
    """
    if not isinstance(env, dict):
        return env
    out = normalize_pack_envelope(env)
    fields_defaulted = out.pop("_normalized_fields", None)
    if fields_defaulted and emit_fn is not None:
        try:
            emit_fn("ingest.envelope_normalized", {
                "envelope_id": str(out.get("id", "?")),
                "external_id": out.get("external_id"),
                "type": out.get("type"),
                "fields_defaulted": list(fields_defaulted),
            })
        except Exception:  # noqa: BLE001 — tracing must never break ingest
            pass
    return out


def validation_error_details(exc: Exception) -> list[dict[str, Any]]:
    """Render a pydantic ValidationError as a JSON-safe field/message list."""
    errors = getattr(exc, "errors", None)
    if callable(errors):
        try:
            return [
                {"field": ".".join(str(p) for p in e.get("loc", ())),
                 "msg": str(e.get("msg", ""))}
                for e in errors()
            ]
        except Exception:  # noqa: BLE001
            pass
    return [{"field": "", "msg": str(exc)}]


@dataclass
class IngestItemResult:
    envelope_id: str
    envelope_type: str | None
    target: str | None
    status: str          # done | failed | surfaced | running | skipped_duplicate | deferred_to_host | unknown_target
    run_id: str | None = None
    detail: str = ""
    # E2-34: structured pydantic errors for a `failed` validation item, so the
    # host sees WHICH fields were wrong instead of one flattened string.
    errors: list[dict[str, Any]] = field(default_factory=list)


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

    @property
    def rejected(self) -> list[IngestItemResult]:
        """Items that never reached a squad because they failed validation.

        E2-34: the attended submit path turns a non-empty list into a top-level
        ``status="envelopes_rejected"`` instead of reporting ``complete`` with
        the failure buried in the ``ingest`` detail."""
        return [it for it in self.items if it.status == "failed" and it.errors]

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
        # already-typed envelopes). E2-34: a hand-written pack envelope first
        # goes through normalize_pack_envelope, which supplies the required
        # fields the prose contract never documented (owner/branch) and folds
        # pack-only keys into real schema fields.
        normalized = normalize_for_ingest(raw, _emit) if isinstance(raw, dict) else raw
        try:
            env = (normalized if isinstance(normalized, HydraEnvelope)
                   else validate_envelope(dict(normalized)))
        except Exception as exc:  # noqa: BLE001 — bad envelope is an item failure, not a crash
            bad = normalized if isinstance(normalized, dict) else {}
            errors = validation_error_details(exc)
            outcome.items.append(IngestItemResult(
                envelope_id=str(bad.get("id", "?")),
                envelope_type=bad.get("type"),
                target=None, status="failed", detail=f"invalid envelope: {exc}",
                errors=errors,
            ))
            _emit("ingest.invalid_envelope", {
                "envelope_id": str(bad.get("id", "?")),
                "type": bad.get("type"),
                "errors": errors,
            })
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
            errors = validation_error_details(exc)
            outcome.items.append(IngestItemResult(
                envelope_id=eid, envelope_type=etype, target=target,
                status="failed", detail=f"redaction/validation failed: {exc}",
                errors=errors,
            ))
            _emit("ingest.invalid_envelope", {
                "envelope_id": eid, "type": etype, "errors": errors,
            })
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
        # F34: budget_charge to eights (fail-soft; never blocks local work).
        try:
            from .eights.attestation import EightsAttestor as _EightsAttestor
            _att = _EightsAttestor(
                dispatcher=dispatcher,
                workflow_id=str(state.workflow_id),
            )
            _att.budget_charge(
                workflow_id=str(state.workflow_id),
                usd=cost_usd, tokens=cost_tok,
                purpose="ingest_dispatch",
            )
        except Exception:  # noqa: BLE001 — fail-soft per F34
            pass
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
