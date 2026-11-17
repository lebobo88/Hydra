"""Opt-in negation + constraint-field exclusion + merged-file factual check
for the constitution postcheck (Hydra#163 step 1). Hermetic: no network, no
LLM, no reads of anything under .hydra.
"""
from __future__ import annotations

import copy

import pytest

from hydra_core.cli import _attended_result_record
from hydra_core.governance import enforce_governance
from hydra_core.immortal_head import _BREACH_PATTERNS, verify_intent
from hydra_core.state import HydraState

# Verbatim from a real attended request (see Hydra#163).
S1 = "Do not edit CONSTITUTION.md."
S2 = "Editing CONSTITUTION.md or any rubric in hydra_core/judge/registry."

FILE_PATTERN_NAME = _BREACH_PATTERNS[0][0]
FILE = "CONSTITUTION.md"


def _state(**kw) -> HydraState:
    kw.setdefault("root_goal", "ship the feature")
    return HydraState(**kw)


def _gov(state: HydraState):
    return enforce_governance(state, {})


def _dev_task(**fields) -> dict:
    env = {"envelope_type": "DEV_TASK", "title": "t", "instructions": "do the thing"}
    env.update(fields)
    return env


# ---- verify_intent -----------------------------------------------------------

def test_p1_default_path_unchanged():
    # Values observed at the stage base (d5df1fd).
    for s in (S1, S2):
        v = verify_intent(s)
        assert v.aligned is False
        assert v.breached_refusals == ("rewrite_constitution",)


def test_n1_negated_sentence_aligned():
    assert verify_intent(S1, negation_aware=True).aligned is True


@pytest.mark.parametrize("text", [
    f"never edit {FILE}",
    f"Please work without editing {FILE}",
    f"We cannot overwrite {FILE}",
    f"You must not delete {FILE}",
])
def test_n2_negators_aligned(text):
    assert verify_intent(text, negation_aware=True).aligned is True


def test_n3_unnegated_imperative_breaches():
    v = verify_intent(f"edit {FILE} now", negation_aware=True)
    assert v.aligned is False
    assert v.breached_refusals == (FILE_PATTERN_NAME,)


def test_n4_negated_first_verb_unnegated_second_breaches():
    text = f"never edit it, then delete {FILE}"
    assert verify_intent(text, negation_aware=True).aligned is False


def test_negation_does_not_cross_clause_boundary():
    text = f"Do not worry. Edit {FILE}"
    assert verify_intent(text, negation_aware=True).aligned is False


def test_scan_payload_only_affects_constitution_pattern():
    # constitution_scan_payload narrows only the file pattern.
    v = verify_intent(
        {"a": "rm -rf /", "b": S2}, constitution_scan_payload={"a": "rm -rf /"})
    assert v.breached_refusals == ("unguarded_venom",)


# ---- enforce_governance: text check -------------------------------------------

def test_g1_negated_goal_not_surfaced():
    v = _gov(_state(root_goal=S1))
    assert v.surfaced is False, v.reason


def test_g2_constraint_fields_excluded_but_instructions_scanned():
    for field in ("test_plan", "non_goals"):
        s = _state()
        s.envelopes.append(_dev_task(**{field: [S2]}))
        v = _gov(s)
        assert v.surfaced is False, (field, v.reason)
    s = _state()
    s.envelopes.append(_dev_task(instructions=S2))
    v = _gov(s)
    assert v.surfaced is True
    assert v.reason.startswith("constitution_breach:")


def test_g2_steps_items_stripped_and_originals_untouched():
    env = {"envelope_type": "PLAN", "steps": [
        {"step_id": "s1", "description": "x", "acceptance_criteria": [S2],
         "do_not_touch": [S2], "risks": [S2]}]}
    s = _state()
    s.envelopes.append(env)
    before = copy.deepcopy(s.envelopes)
    assert _gov(s).surfaced is False
    assert s.envelopes == before


def test_g3_other_patterns_not_narrowed():
    s = _state()
    s.envelopes.append(_dev_task(test_plan=["run rm -rf / to clean"]))
    assert _gov(s).surfaced is True


def test_g4_flattened_rationale():
    ok = {"envelope_type": "DECISION_RECORD", "decision": "d", "rationale": S1}
    bad = {"envelope_type": "DECISION_RECORD", "decision": "d",
           "rationale": f"Proposal: edit {FILE} to relax the rule."}
    s = _state()
    s.envelopes.append(copy.deepcopy(ok))
    assert _gov(s).surfaced is False
    assert s.envelopes[0]["rationale"] == S1
    s = _state()
    s.envelopes.append(copy.deepcopy(bad))
    assert _gov(s).surfaced is True
    assert s.envelopes[0]["rationale"] == bad["rationale"]


def test_g5_plan_ref_is_not_scanned():
    # TASK 0: the ingest PLAN branch stores the PLAN in state.plan_ref only;
    # enforce_governance's payload is goal/envelopes/last_event.
    s = _state(plan_ref={"steps": [], "non_goals": [S2]})
    assert _gov(s).surfaced is False
    s = _state(plan_ref={"steps": [], "goal": f"edit {FILE}"})
    assert _gov(s).surfaced is False


# ---- enforce_governance: factual check -----------------------------------------

def _attended(**kw) -> dict:
    rec = {"task_id": "t1", "owner_squad": "engineering", "status": "complete",
           "changed_paths": [FILE]}
    rec.update(kw)
    return rec


def test_f1_merged_constitution_change_surfaces():
    s = _state()
    s.attended_results = [_attended(merged=True)]
    v = _gov(s)
    assert v.surfaced is True
    assert v.reason.startswith("constitution_breach:")
    assert FILE in v.reason


@pytest.mark.parametrize("path", [
    "CONSTITUTION.md", ".\\CONSTITUTION.md", "./CONSTITUTION.md",
    "constitution.md", "./Constitution.MD",
])
def test_f2_path_variants(path):
    s = _state()
    s.attended_results = [_attended(merged=True, changed_paths=["a.py", path])]
    assert _gov(s).surfaced is True


@pytest.mark.parametrize("extra", [
    {"merged": False},
    {},
    {"merged": False, "final_status": "surfaced"},
    {"merged": False, "final_status": "failed"},
    {"stage_outcome": "pass_unlanded"},
    {"merged": "yes"},
])
def test_f3_not_merged_never_triggers(extra):
    s = _state()
    s.attended_results = [_attended(**extra)]
    assert _gov(s).surfaced is False


def test_f3_merged_other_file_does_not_trigger():
    s = _state()
    s.attended_results = [_attended(merged=True, changed_paths=["docs/CONSTITUTION.md.bak", "x.py"])]
    assert _gov(s).surfaced is False


def test_f4_negation_never_applies_to_factual_check():
    s = _state(root_goal=S1)
    s.envelopes.append({"envelope_type": "DECISION_RECORD", "decision": "d", "rationale": S1})
    s.attended_results = [_attended(merged=True, final_status="surfaced")]
    assert _gov(s).surfaced is True


# ---- cli record ------------------------------------------------------------------

def test_r1_attended_result_record_merged_flag():
    s = _state()
    base = {"task_id": "t1", "squad_slug": "engineering"}
    assert _attended_result_record(s, {**base, "merge": {"merged": True, "sha": "x"}})["merged"] is True
    assert _attended_result_record(s, {**base, "merge": {"merged": False}})["merged"] is False
    assert _attended_result_record(s, dict(base))["merged"] is False
    assert _attended_result_record(s, {**base, "merge": None})["merged"] is False
    assert _attended_result_record(s, {**base, "merged": True})["merged"] is True
