"""E2-25 — versioned rubric resolution, gate-typed defaults, explicit fallback.

The attended bridge used to hand the judge the unversioned id
``rfc-2119-normative`` (a *spec* rubric, on a *code* stage), fail to resolve its
body, silently substitute a generic one-liner, and still record
``rubric_id=rfc-2119-normative`` in the pp ledger. These tests pin the four
properties that fix requires:

1. an unversioned base resolves to the highest ``@N`` a registry serves;
2. an already-versioned id passes through untouched (replay determinism);
3. a registry miss surfaces ``rubric_fallback`` in the judge host_action and in
   the recorded verdict metadata, instead of being swallowed;
4. a code stage defaults to the code rubric, never the spec rubric.
"""
from __future__ import annotations

import subprocess

import pytest

from hydra_core import host_bridge
from hydra_core.judge import rubric_resolution as rr


@pytest.fixture(autouse=True)
def _clear_rubric_cache():
    rr.reset_rubric_cache()
    yield
    rr.reset_rubric_cache()


class _ListDispatcher:
    """Serves a canned pp ``list_rubrics`` / ``get_rubric`` pair."""

    def __init__(self, rows, bodies=None):
        self.rows = rows
        self.bodies = bodies or {}
        self.calls: list[str] = []

    def call_mcp(self, server, tool, args, squad_id=None):
        self.calls.append(tool)
        if tool == "list_rubrics":
            return {"status": "done", "result": list(self.rows)}
        if tool == "get_rubric":
            body = self.bodies.get(args.get("id"))
            return {"status": "done",
                    "result": {"markdown": body} if body else {}}
        return {"status": "done", "result": {}}


# --------------------------------------------------------------------------- #
# resolve_rubric_id                                                            #
# --------------------------------------------------------------------------- #

def test_unversioned_base_resolves_to_highest_version():
    disp = _ListDispatcher([
        {"id": "web-runtime-validation@1", "kind": "contract"},
        {"id": "web-runtime-validation@2", "kind": "contract"},
        {"id": "rfc-2119-normative@1", "kind": "spec"},
    ])
    assert rr.resolve_rubric_id(disp, "web-runtime-validation") == \
        "web-runtime-validation@2"
    assert rr.resolve_rubric_id(disp, "rfc-2119-normative") == \
        "rfc-2119-normative@1"


def test_already_versioned_id_passes_through_unchanged():
    disp = _ListDispatcher([{"id": "rfc-2119-normative@9", "kind": "spec"}])
    # A caller that pinned @1 keeps @1 even though @9 exists — past verdicts
    # replay against the exact rubric body they were judged with.
    assert rr.resolve_rubric_id(disp, "rfc-2119-normative@1") == \
        "rfc-2119-normative@1"
    assert disp.calls == []   # no registry round-trip needed


def test_registry_miss_returns_base_and_reports_unresolved():
    disp = _ListDispatcher([{"id": "rfc-2119-normative@1", "kind": "spec"}])
    seen: list[str] = []
    out = rr.resolve_rubric_id(disp, "no-such-rubric", on_unresolved=seen.append)
    assert out == "no-such-rubric"
    assert seen == ["no-such-rubric"]


def test_local_registry_resolves_without_a_dispatcher():
    # The Hydra-local code rubric resolves even when pp is unreachable.
    assert rr.resolve_rubric_id(None, "code-change-quality") == \
        "code-change-quality@1"


def test_list_rubrics_is_cached_per_process():
    disp = _ListDispatcher([{"id": "rfc-2119-normative@1", "kind": "spec"}])
    rr.resolve_rubric_id(disp, "rfc-2119-normative")
    rr.resolve_rubric_id(disp, "rfc-2119-normative")
    assert disp.calls.count("list_rubrics") == 1


def test_pp_outage_is_not_cached_as_an_empty_registry():
    class _Broken:
        def __init__(self):
            self.n = 0

        def call_mcp(self, server, tool, args, squad_id=None):
            self.n += 1
            raise RuntimeError("pp daemon down")

    disp = _Broken()
    assert rr.resolve_rubric_id(disp, "rfc-2119-normative") == "rfc-2119-normative"
    rr.resolve_rubric_id(disp, "rfc-2119-normative")
    assert disp.n == 2   # retried rather than pinned to an empty index


# --------------------------------------------------------------------------- #
# rubric_body                                                                  #
# --------------------------------------------------------------------------- #

def test_body_comes_from_the_local_registry_without_fallback():
    body, fallback = rr.rubric_body("code-change-quality@1")
    assert fallback is False
    assert "requirement_fidelity" in body


def test_body_falls_back_to_pp_get_rubric():
    disp = _ListDispatcher([], bodies={"rfc-2119-normative@1": "# RFC 2119\nMUST"})
    body, fallback = rr.rubric_body("rfc-2119-normative@1", disp)
    assert fallback is False
    assert "RFC 2119" in body


def test_unknown_rubric_yields_generic_body_and_flags_fallback():
    disp = _ListDispatcher([])
    body, fallback = rr.rubric_body("rfc-2119-normative", disp)
    assert fallback is True
    assert body == rr.GENERIC_FALLBACK_BODY


# --------------------------------------------------------------------------- #
# gate-typed defaults                                                          #
# --------------------------------------------------------------------------- #

def test_code_gate_default_is_not_the_spec_rubric():
    for gate_type in ("code_style", "lint_class"):
        chosen = rr.default_rubric_id(gate_type)
        assert chosen == rr.CODE_RUBRIC_BASE
        assert chosen != "rfc-2119-normative"


def test_spec_gate_keeps_the_rfc_2119_rubric():
    assert rr.default_rubric_id("spec") == "rfc-2119-normative"


def test_unknown_gate_type_defaults_to_the_code_rubric():
    assert rr.default_rubric_id(None) == rr.CODE_RUBRIC_BASE
    assert rr.default_rubric_id("something-new") == rr.CODE_RUBRIC_BASE


# --------------------------------------------------------------------------- #
# host_bridge wiring                                                           #
# --------------------------------------------------------------------------- #

def _git(args, cwd):
    return subprocess.run(["git", *args], cwd=str(cwd), capture_output=True,
                          text=True, check=False)


def _init_repo(path):
    _git(["init"], path)
    _git(["config", "user.email", "t@t.test"], path)
    _git(["config", "user.name", "Test"], path)
    _git(["config", "commit.gpgsign", "false"], path)
    (path / "README.md").write_text("base\n", encoding="utf-8")
    _git(["add", "-A"], path)
    _git(["commit", "-m", "base", "--no-verify"], path)


class _BridgeDispatcher:
    """Minimal pp stand-in for begin_stage → submit(generate) → judge action."""

    def __init__(self, *, gate_rubric=None, rubric_rows=(), bodies=None):
        self.calls: list[tuple[str, dict]] = []
        self._gate_rubric = gate_rubric
        self._rows = list(rubric_rows)
        self._bodies = bodies or {}

    def call_mcp(self, server, tool, args, squad_id=None):
        self.calls.append((tool, dict(args)))
        if tool == "start_stage":
            return {"status": "done", "result": {"stage_id": "stage-1"}}
        if tool == "record_attempt":
            return {"status": "done", "result": {"attempt_id": "att-1"}}
        if tool == "gate_eligible_judges":
            res = {"required_cross_vendor": True}
            if self._gate_rubric is not None:
                res["rubric_id"] = self._gate_rubric
            return {"status": "done", "result": res}
        if tool == "list_rubrics":
            return {"status": "done", "result": list(self._rows)}
        if tool == "get_rubric":
            body = self._bodies.get(args.get("id"))
            return {"status": "done",
                    "result": {"markdown": body} if body else {}}
        return {"status": "done", "result": {}}

    def args_for(self, tool):
        return [a for t, a in self.calls if t == tool]


def _run_to_judge(disp, tmp_path):
    _init_repo(tmp_path)
    res = host_bridge.begin_stage(
        disp, workflow_id="wf-1", run_id="run-1", project_path=str(tmp_path),
        request_text="implement the thing", project_root=str(tmp_path))
    return host_bridge.submit_host_result(
        disp, cursor_file=res["cursor_path"], call_key="generate-0",
        result={"text": "edited foo.py", "cost_usd": 0.0,
                "tokens_in": 1, "tokens_out": 1, "model": "claude-opus-4-8"})


def test_attended_code_stage_judges_on_the_code_rubric(tmp_path):
    # pp returns no rubric for a code_style gate (gates.ts pickDefaultRubric ->
    # null), so the stage default decides — and it must be the code rubric.
    disp = _BridgeDispatcher(gate_rubric=None)
    res = _run_to_judge(disp, tmp_path)
    action = res["host_action"]
    assert action["rubric_id"] == "code-change-quality@1"
    assert action["rubric_fallback"] is False
    assert "requirement_fidelity" in action["rubric_md"]


def test_attended_unversioned_gate_rubric_is_resolved(tmp_path):
    disp = _BridgeDispatcher(
        gate_rubric="rfc-2119-normative",
        rubric_rows=[{"id": "rfc-2119-normative@1", "kind": "spec"}],
        bodies={"rfc-2119-normative@1": "# RFC 2119 (v1)\nMUST/SHOULD/MAY"})
    res = _run_to_judge(disp, tmp_path)
    action = res["host_action"]
    assert action["rubric_id"] == "rfc-2119-normative@1"
    assert action["rubric_fallback"] is False
    assert "RFC 2119" in action["rubric_md"]


def test_attended_registry_miss_flags_fallback_in_action_and_verdict(tmp_path):
    # No registry serves this id: the judge gets the generic body, and both the
    # host_action and the recorded verdict must say so.
    disp = _BridgeDispatcher(gate_rubric="ghost-rubric")
    res = _run_to_judge(disp, tmp_path)
    action = res["host_action"]
    assert action["rubric_id"] == "ghost-rubric"
    assert action["rubric_fallback"] is True
    assert action["rubric_md"] == rr.GENERIC_FALLBACK_BODY

    host_bridge.submit_host_result(
        disp, cursor_file=res["cursor_path"], call_key=action["call_key"],
        result={"outcome": "revise", "critique_md": "needs work",
                "judge_producer": "codex", "judge_model_id": "gpt-x",
                "score_json": {"correctness": 3}, "cost_usd": 0.0})
    verdicts = disp.args_for("record_verdict")
    assert verdicts, "record_verdict was never called"
    score = verdicts[-1]["score_json"]
    assert verdicts[-1]["rubric_id"] == "ghost-rubric"
    assert score["_rubric_id"] == "ghost-rubric"
    assert score["_rubric_fallback"] is True


def test_attended_resolved_verdict_carries_no_fallback_flag(tmp_path):
    disp = _BridgeDispatcher(gate_rubric=None)
    res = _run_to_judge(disp, tmp_path)
    host_bridge.submit_host_result(
        disp, cursor_file=res["cursor_path"],
        call_key=res["host_action"]["call_key"],
        result={"outcome": "revise", "critique_md": "needs work",
                "judge_producer": "codex", "judge_model_id": "gpt-x",
                "score_json": {}, "cost_usd": 0.0})
    score = disp.args_for("record_verdict")[-1]["score_json"]
    assert score["_rubric_id"] == "code-change-quality@1"
    assert "_rubric_fallback" not in score
