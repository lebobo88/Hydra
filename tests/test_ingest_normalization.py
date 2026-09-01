"""E2-34 — pack-emitted envelope normalization and rejection surfacing.

A claude-skill pack hand-writes its DEV_TASKs from the prose contract, which
never documented the REQUIRED ``owner`` / ``branch`` fields. On workflow
166fc7ee the rlm-gaming leg emitted such a DEV_TASK, ``submit_host_result``
returned top-level ``status="complete"``, and the engineering delegation was
dropped inside an embedded ``ingest`` detail with no trace event and no HITL.

These tests pin the two halves of the fix:

  - ``normalize_pack_envelope`` supplies safe defaults (owner inferred, branch
    synthesized, allow-listed ``repo`` mapped onto ``target_repo_id``) and folds
    pack-only keys (``title`` / ``acceptance_criteria`` / ``budget_usd``) into
    real schema fields so nothing is lost, emitting
    ``ingest.envelope_normalized``.
  - an envelope normalization cannot repair is REJECTED loudly:
    ``ingest.invalid_envelope`` on the trace, structured field errors on the
    item, and a terminal cursor that reports ``envelopes_rejected`` rather than
    ``complete``.
"""
from __future__ import annotations

import json
from pathlib import Path
from uuid import uuid4

import pytest

from hydra_core import host_bridge
from hydra_core.ingest import (
    default_dev_task_branch,
    dispatch_ingested_envelopes,
    infer_dev_task_owner,
    normalize_pack_envelope,
    slugify,
)
from hydra_core.schemas import validate_envelope
from hydra_core.squad_loader import discover_squads
from hydra_core.state import HydraState

from tests.test_hybrid_dispatch_e2e import _ScriptedDispatcher, _happy_responses

HYDRA_ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture
def packs():
    return discover_squads(HYDRA_ROOT)


@pytest.fixture(autouse=True)
def _no_git_harvest(monkeypatch):
    monkeypatch.setattr("hydra_core.squad_node.harvest_pp_run_artifacts",
                        lambda **_k: None)
    monkeypatch.setattr("hydra_core.squad_node._run_smoke",
                        lambda *_a, **_k: ("pass", "stub smoke pass"))


def _pack_dev_task(workflow_id: str, **over) -> dict:
    """The shape RLM-Gaming's Director actually emitted on workflow 166fc7ee:
    no ``owner``, no ``branch``, plus three keys DevTask does not define."""
    d = {
        "id": str(uuid4()),
        "type": "DEV_TASK",
        "origin_squad": "rlm-gaming",
        "target_squad": "engineering",
        "workflow_id": workflow_id,
        "repo": "hydra",
        "pp_team": "game-feature-team",
        "title": "Deterministic fog of war",
        "instructions": "Implement deterministic fog-of-war reveal in src/sim/fow.ts",
        "acceptance_criteria": ["reveal is seed-stable", "no per-frame allocation"],
        "budget_usd": 40,
    }
    d.update(over)
    return d


# --------------------------------------------------------------------------- #
# owner inference                                                             #
# --------------------------------------------------------------------------- #

@pytest.mark.parametrize("pp_team,title,instructions,expected", [
    (None, None, "Restyle the pause menu css and html", "frontend"),
    (None, "Ship the UI overlay", None, "frontend"),
    (None, None, "Add an api endpoint backed by the db", "backend"),
    (None, "Backend service handler", None, "backend"),
    (None, None, "Deploy the build through ci", "devops"),
    (None, None, "Add a docker image to the release pipeline", "devops"),
    (None, None, "Rebuild the etl over the analytics dataset", "data"),
    (None, None, "Make the thing nicer somehow", "fullstack"),
    (None, None, "", "fullstack"),
    # pp_team is weighted x2, so it wins over a single opposing body keyword.
    ("design-system-team", None, "touch the api once", "frontend"),
])
def test_owner_inference_table(pp_team, title, instructions, expected) -> None:
    assert infer_dev_task_owner(pp_team, title, instructions) == expected


def test_inferred_owner_is_always_a_schema_literal() -> None:
    from hydra_core.ingest import DEV_TASK_OWNERS
    from hydra_core.schemas import DevTask

    literals = DevTask.model_fields["owner"].annotation.__args__
    assert set(DEV_TASK_OWNERS) == set(literals)


# --------------------------------------------------------------------------- #
# branch + slug                                                               #
# --------------------------------------------------------------------------- #

def test_default_branch_uses_workflow_short_and_title_slug() -> None:
    wf = "166fc7ee-1111-4222-8333-444455556666"
    assert default_dev_task_branch(wf, "Deterministic Fog of War!", None) == \
        "hydra/166fc7ee/deterministic-fog-of-war"


def test_default_branch_falls_back_to_instructions_then_placeholder() -> None:
    wf = "166fc7ee-1111-4222-8333-444455556666"
    assert default_dev_task_branch(wf, None, "one two three four five six seven") == \
        "hydra/166fc7ee/one-two-three-four-five-six"
    assert default_dev_task_branch(wf, None, None) == "hydra/166fc7ee/dev-task"


def test_slugify_caps_words() -> None:
    assert slugify("a b c d e f g h") == "a-b-c-d-e-f"


# --------------------------------------------------------------------------- #
# normalization                                                               #
# --------------------------------------------------------------------------- #

def test_pack_dev_task_normalizes_and_validates() -> None:
    wf = str(uuid4())
    raw = _pack_dev_task(wf)
    out = normalize_pack_envelope(raw)

    fields = out.pop("_normalized_fields")
    assert "owner" in fields and "branch" in fields

    env = validate_envelope(out)
    # Nothing in pp_team/title/instructions matches a keyword — safe default.
    assert env.owner == "fullstack"
    assert env.branch == f"hydra/{wf.replace('-', '')[:8]}/deterministic-fog-of-war"
    # `repo` names an allow-listed id, so it is mirrored to target_repo_id.
    assert env.target_repo_id == "hydra"
    # Nothing the pack sent was lost.
    assert env.test_plan == ["reveal is seed-stable", "no per-frame allocation"]
    assert env.constraints.budget_usd == 40.0
    assert "Deterministic fog of war" in env.instructions
    # The input dict is not mutated.
    assert raw["title"] == "Deterministic fog of war"


def test_normalization_does_not_override_explicit_fields() -> None:
    raw = _pack_dev_task(str(uuid4()), owner="devops", branch="feature/mine",
                         target_repo_id="hydra")
    out = normalize_pack_envelope(raw)
    assert out["owner"] == "devops"
    assert out["branch"] == "feature/mine"
    assert "owner" not in out.get("_normalized_fields", [])


def test_unknown_repo_is_not_mapped_to_target_repo_id() -> None:
    out = normalize_pack_envelope(
        _pack_dev_task(str(uuid4()), repo="definitely-not-registered-repo"))
    assert out.get("target_repo_id") is None
    assert out["repo"] == "definitely-not-registered-repo"


def test_unknown_extra_keys_are_folded_into_instructions() -> None:
    out = normalize_pack_envelope(
        _pack_dev_task(str(uuid4()), estimated_hours=12))
    assert "estimated_hours" not in out
    assert "estimated_hours: 12" in out["instructions"]
    env = validate_envelope({k: v for k, v in out.items() if k != "_normalized_fields"})
    assert "estimated_hours: 12" in env.instructions


def test_non_dev_task_envelope_passes_through_unchanged() -> None:
    src = {"type": "PRD", "origin_squad": "hydra", "workflow_id": str(uuid4()),
           "source_goal_id": str(uuid4()), "summary": "s"}
    out = normalize_pack_envelope(src)
    assert out == src
    assert out is not src


# --------------------------------------------------------------------------- #
# dispatch: normalized envelope reaches engineering                           #
# --------------------------------------------------------------------------- #

def test_pack_shaped_dev_task_dispatches_after_normalization(packs) -> None:
    state = HydraState(root_goal="game build")
    disp = _ScriptedDispatcher(_happy_responses("pass"), drive=True)
    events: list[tuple[str, dict]] = []

    outcome = dispatch_ingested_envelopes(
        state, [_pack_dev_task(str(state.workflow_id))],
        packs=packs, dispatcher=disp,
        emit_fn=lambda e, p: events.append((e, p)),
    )

    assert [it.status for it in outcome.items] == ["done"]
    assert [t.owner_squad for t in outcome.new_tasks] == ["engineering"]
    assert not outcome.rejected

    normalized = [p for (e, p) in events if e == "ingest.envelope_normalized"]
    assert len(normalized) == 1
    assert "owner" in normalized[0]["fields_defaulted"]
    assert "branch" in normalized[0]["fields_defaulted"]
    assert normalized[0]["type"] == "DEV_TASK"


# --------------------------------------------------------------------------- #
# rejection surfacing                                                         #
# --------------------------------------------------------------------------- #

def test_unrepairable_envelope_is_rejected_with_trace_event(packs) -> None:
    state = HydraState(root_goal="x")
    disp = _ScriptedDispatcher(_happy_responses("pass"), drive=True)
    events: list[tuple[str, dict]] = []

    # An `owner` outside the literal set: normalization never overrides an
    # explicit value, so this must fail validation loudly, not be dropped.
    hopeless = _pack_dev_task(str(state.workflow_id), owner="wizard")

    outcome = dispatch_ingested_envelopes(
        state, [hopeless], packs=packs, dispatcher=disp,
        emit_fn=lambda e, p: events.append((e, p)),
    )

    assert [it.status for it in outcome.items] == ["failed"]
    assert outcome.rejected and outcome.rejected[0].envelope_id == hopeless["id"]
    assert any(e["field"] == "owner" for e in outcome.rejected[0].errors)
    # Nothing was dispatched.
    assert not disp.calls
    assert not outcome.new_tasks

    invalid = [p for (e, p) in events if e == "ingest.invalid_envelope"]
    assert len(invalid) == 1
    assert invalid[0]["type"] == "DEV_TASK"
    assert any(err["field"] == "owner" for err in invalid[0]["errors"])


def test_unknown_envelope_type_is_rejected(packs) -> None:
    state = HydraState(root_goal="x")
    disp = _ScriptedDispatcher(_happy_responses("pass"), drive=True)
    events: list[tuple[str, dict]] = []

    outcome = dispatch_ingested_envelopes(
        state, [{"id": str(uuid4()), "type": "NOT_A_TYPE"}],
        packs=packs, dispatcher=disp,
        emit_fn=lambda e, p: events.append((e, p)),
    )
    assert [it.status for it in outcome.items] == ["failed"]
    assert outcome.rejected
    assert any(e == "ingest.invalid_envelope" for (e, _p) in events)


def test_terminal_cursor_with_rejections_reports_envelopes_rejected(tmp_path) -> None:
    """The cursor half of the CLI surfacing: a stage that completed but whose
    emitted envelope was rejected must NOT read back as `complete`."""
    cursor_file = tmp_path / "cursor.json"
    cursor = {
        "schema": host_bridge.CURSOR_SCHEMA,
        "workflow_id": str(uuid4()),
        "run_id": "run_T",
        "state": "complete",
        "final_status": "complete",
        "project_path": str(tmp_path),
        "kind": "squad",
    }
    cursor_file.write_text(json.dumps(cursor), encoding="utf-8")

    assert host_bridge._step_result(
        host_bridge.load_cursor(cursor_file), cursor_file)["status"] == "complete"

    rejected = [{"envelope_id": "e1", "envelope_type": "DEV_TASK",
                 "status": "failed",
                 "errors": [{"field": "owner", "msg": "Field required"}]}]
    host_bridge.record_rejected_envelopes(cursor_file, rejected)

    res = host_bridge._step_result(host_bridge.load_cursor(cursor_file), cursor_file)
    assert res["status"] == "envelopes_rejected"
    assert res["final_status"] == "complete"      # the stage itself did finish
    assert res["rejected_envelopes"] == rejected


def test_record_rejected_envelopes_is_a_noop_on_empty(tmp_path) -> None:
    cursor_file = tmp_path / "cursor.json"
    cursor_file.write_text(json.dumps({"schema": host_bridge.CURSOR_SCHEMA,
                                       "state": "complete"}), encoding="utf-8")
    before = cursor_file.read_text(encoding="utf-8")
    host_bridge.record_rejected_envelopes(cursor_file, [])
    assert cursor_file.read_text(encoding="utf-8") == before
