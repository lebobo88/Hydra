"""Unit tests for hydra_core.judge.dispatcher — mocked critique client."""
from __future__ import annotations

from uuid import uuid4

import pytest

from hydra_core.judge.dispatcher import (
    JudgeDispatchError,
    MIN_CRITIQUE_CHARS,
    NoOpCritiqueClient,
    dispatch_judge,
    dispatch_judge_with_fallback,
)


def _env() -> dict:
    return {
        "id": str(uuid4()),
        "type": "C_SUITE_DECISION_PACKET",
        "origin_squad": "executive",
        "workflow_id": str(uuid4()),
        "origin": "BOARDROOM",
        "objective": "fund Q3 plan",
    }


class _ScriptedClient:
    def __init__(self, response: dict, raises: Exception | None = None):
        self.response = response
        self.raises = raises
        self.calls: list[dict] = []

    def critique(self, *, vendor, artifact_text, rubric_md):
        self.calls.append({"vendor": vendor, "rubric_md": rubric_md, "artifact_text": artifact_text})
        if self.raises:
            raise self.raises
        return self.response


def test_noop_client_returns_pass_with_skeleton_marker():
    wf = uuid4()
    verdict = dispatch_judge(
        envelope=_env(),
        rubric_id="board-decision-quality@1",
        judge_vendor="agy",
        workflow_id=wf,
        client=NoOpCritiqueClient(),
    )
    # Pragmatic-pass guard: NoOp has _skeleton only, so substantive scores=0 →
    # downgrades to "revise".
    assert verdict.outcome == "revise"
    assert "pragmatic-pass guard tripped" in verdict.critique_md


def test_real_pass_with_substantive_scores_remains_pass():
    wf = uuid4()
    client = _ScriptedClient({
        "outcome": "pass",
        "critique_md": "Solid memo. " * 20,  # well over 80 chars
        "score_json": {"objective_clarity": 5, "risk_treatment": 4},
    })
    verdict = dispatch_judge(
        envelope=_env(),
        rubric_id="board-decision-quality@1",
        judge_vendor="agy",
        workflow_id=wf,
        client=client,
    )
    assert verdict.outcome == "pass"
    assert verdict.score_json["objective_clarity"] == 5


def test_short_critique_pass_gets_downgraded():
    wf = uuid4()
    client = _ScriptedClient({
        "outcome": "pass",
        "critique_md": "ok",  # < MIN_CRITIQUE_CHARS
        "score_json": {"objective_clarity": 5},
    })
    verdict = dispatch_judge(
        envelope=_env(),
        rubric_id="board-decision-quality@1",
        judge_vendor="agy",
        workflow_id=wf,
        client=client,
    )
    assert verdict.outcome == "revise"


def test_empty_scores_pass_gets_downgraded():
    wf = uuid4()
    client = _ScriptedClient({
        "outcome": "pass",
        "critique_md": "x" * (MIN_CRITIQUE_CHARS + 10),
        "score_json": {},
    })
    verdict = dispatch_judge(
        envelope=_env(),
        rubric_id="board-decision-quality@1",
        judge_vendor="agy",
        workflow_id=wf,
        client=client,
    )
    assert verdict.outcome == "revise"


def test_client_exception_surfaces_as_dispatch_error():
    wf = uuid4()
    client = _ScriptedClient({}, raises=RuntimeError("MCP unreachable"))
    with pytest.raises(JudgeDispatchError):
        dispatch_judge(
            envelope=_env(),
            rubric_id="board-decision-quality@1",
            judge_vendor="agy",
            workflow_id=wf,
            client=client,
        )


def test_rubric_body_is_passed_to_judge():
    wf = uuid4()
    client = _ScriptedClient({
        "outcome": "revise",
        "critique_md": "needs work " * 10,
        "score_json": {"x": 1},
    })
    dispatch_judge(
        envelope=_env(),
        rubric_id="constitution-alignment@1",
        judge_vendor="agy",
        workflow_id=wf,
        client=client,
    )
    assert len(client.calls) == 1
    assert "Constitution Alignment Rubric" in client.calls[0]["rubric_md"]
    assert "<untrusted-artifact>" in client.calls[0]["artifact_text"]


def test_unknown_rubric_raises():
    wf = uuid4()
    with pytest.raises(KeyError):
        dispatch_judge(
            envelope=_env(),
            rubric_id="not-a-real-rubric@99",
            judge_vendor="agy",
            workflow_id=wf,
            client=NoOpCritiqueClient(),
        )


def test_dispatch_judge_refuses_to_serialize_non_finite_envelope():
    """Cross-vendor judge finding (b1baf30 revise round, item 1/2): the live
    PLAN-judging path hands a `model_dump(mode="json")` dict (e.g.
    `state.plan_ref`) straight to `dispatch_judge`, not a validated `Plan`
    instance -- so a legacy plan that predates the non-finite-budget schema
    guard can still carry a NaN/Infinity value here. `_envelope_to_text` must
    refuse before ever calling the critique client, naming the offending
    field, not silently emit the bare `NaN` token into the judge prompt.

    Cross-vendor judge finding (further revise round, item 1/4): a bare
    `ValueError` escaping `dispatch_judge` is an UNHANDLED abort at the
    supervisor `_judge_envelope` boundary, which tolerates only
    `JudgeDispatchError` -- exactly the regression that made a legacy
    checkpoint's resume crash instead of degrading. The refusal must
    therefore surface as a `JudgeDispatchError` (the type the fallback loop
    already handles), still naming the offending field, still never
    reaching the critique client.
    """
    wf = uuid4()
    hostile = _env()
    hostile["constraints"] = {"budget_usd": float("nan")}
    client = _ScriptedClient({
        "outcome": "pass", "critique_md": "x" * 100, "score_json": {"a": 1},
    })
    with pytest.raises(JudgeDispatchError, match="constraints.budget_usd") as exc_info:
        dispatch_judge(
            envelope=hostile,
            rubric_id="constitution-alignment@1",
            judge_vendor="agy",
            workflow_id=wf,
            client=client,
        )
    assert exc_info.value.reason == "non_finite_envelope"
    assert exc_info.value.retryable is False
    # Never reached the client -- the refusal happens before dispatch.
    assert client.calls == []


def test_dispatch_judge_with_fallback_degrades_non_finite_envelope_to_recorded_unjudgeable():
    """The end-to-end resume path: `supervisor._judge_envelope` calls
    `dispatch_judge_with_fallback`, which converts every `JudgeDispatchError`
    (infra/auth/quota/timeout) into an honest `skip` verdict -- EXCEPT a
    non-finite-envelope failure, which is a genuine DATA DEFECT (deterministic
    across every vendor, since it happens before any client is invoked), not
    a transient outage. A legacy checkpoint envelope with a non-finite budget
    must therefore resume and be judged (never abort), with the problem
    RECORDED in the verdict's critique_md and score_json under the DISTINCT
    `unjudgeable` outcome -- never folded into `skip`, which every
    verdict-consuming call site (supervisor.py's `node_judge_per_squad`,
    `node_judge_synthesis`, `node_plan_judge`; `best_of_n.judge_and_rank`)
    treats as "no signal, nothing to block on."

    Cross-vendor judge finding (item 1/6, CRITICAL): before this fix, this
    scenario produced an ordinary `skip` verdict that every downstream site
    accepted as judged -- silently advancing to synthesis and potentially
    marking the workflow `done` despite the envelope never having been
    evaluated.

    Mutation proof (restore the ValueError abort -- revert immediately):
    if `dispatch_judge` raises bare `ValueError` again instead of
    `JudgeDispatchError`, this call raises out of
    `dispatch_judge_with_fallback` (which only catches `JudgeDispatchError`)
    and the test fails with an unhandled `ValueError`.
    """
    wf = uuid4()
    hostile = _env()
    hostile["constraints"] = {"budget_usd": float("nan")}
    client = _ScriptedClient({
        "outcome": "pass", "critique_md": "x" * 100, "score_json": {"a": 1},
    })
    verdict, attempts = dispatch_judge_with_fallback(
        envelope=hostile,
        rubric_id="constitution-alignment@1",
        judge_vendors=["agy", "codex"],
        workflow_id=wf,
        client=client,
    )
    assert verdict.outcome == "unjudgeable"
    assert "constraints.budget_usd" in verdict.critique_md
    assert verdict.score_json.get("_unjudgeable") is True
    assert all(a.get("reason") == "non_finite_envelope" for a in attempts)
    # Never reached the client for either vendor -- both attempts refused
    # before dispatch, and that refusal is recorded per-vendor.
    assert client.calls == []


def test_dispatch_judge_with_fallback_still_produces_ordinary_skip_for_infra_outage():
    """Companion to the unjudgeable test above: an ordinary vendor/infra
    failure (NOT `non_finite_envelope`) must still degrade to the honest
    `skip` outcome exactly as before -- the new `unjudgeable` marker is
    additive, not a replacement for the existing infra-outage handling."""
    wf = uuid4()
    client = _ScriptedClient({}, raises=RuntimeError("MCP unreachable"))
    verdict, attempts = dispatch_judge_with_fallback(
        envelope=_env(),
        rubric_id="constitution-alignment@1",
        judge_vendors=["agy", "codex"],
        workflow_id=wf,
        client=client,
    )
    assert verdict.outcome == "skip"
    assert verdict.score_json.get("_infra") is True
    assert all(a.get("reason") != "non_finite_envelope" for a in attempts)


def test_dispatch_judge_allow_nan_true_would_have_leaked_nan_into_prompt():
    """Mutation proof (revert immediately): show plain `json.dumps` (the
    pre-fix behaviour, before `_envelope_to_text` routed through
    `strict_json.dumps_strict`) would have silently written the literal
    `NaN` token into the judge-facing artifact text instead of refusing.
    """
    import json as _json
    hostile = _env()
    hostile["constraints"] = {"budget_usd": float("nan")}
    text = _json.dumps(hostile, indent=2, default=str, sort_keys=True)
    assert "NaN" in text
