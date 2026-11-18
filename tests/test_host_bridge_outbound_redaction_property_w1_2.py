"""W1.2 step C (Hydra#85): property-style sweep over every attended host_bridge
entry point, asserting a PII sentinel never survives raw outside the three
explicitly allow-listed locations.

Unlike ``test_host_bridge_outbound_redaction_w1_2.py`` (which pins specific
fields one at a time), this file drives each public entry point end-to-end and
scans EVERYTHING it could have touched: every file under the attended cursor
tree, the whole trace.jsonl, and ``json.dumps(default=str)`` of every value the
call returns. A leak through a field none of the targeted tests happened to
look at still fails here.

Hermetic: no network, no LLM, no live MCP transport — every dispatcher is the
in-process ``FakeDispatcher``/stub used across the host_bridge test suite, and
every filesystem path is under ``tmp_path`` (see ``tests/conftest.py`` for the
process-wide HYDRA_HOME/checkpoint/episodic-db redirection this still rides on
for anything that touches a module-level default).

Judge-payload decision (recorded here, not just in the commit message): the
``artifact_text`` host_bridge hands the host for `pp_codex.critique` /
`pp_agy.critique` (``_judge_pending_action``'s payload, host_action.
artifact_text) is REDACTED -- ``_outbound_text(judge_text, kind="judge")`` --
consistent with de5b028, which already redacted it before this stage ran.
Nothing in this file treats that payload as an allow-listed raw location;
``test_judge_success_redacts_artifact_text_everywhere`` below pins it.
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any
from uuid import uuid4

import pytest

from hydra_core import artifact_store, cli, governance, host_bridge
from hydra_core.host_bridge import (
    CURSOR_SCHEMA,
    _provision_worktree,
    cursor_path,
    save_cursor,
    sweep_stale_worktrees,
)
from hydra_core.state import HydraState
from tests.test_host_bridge import FakeDispatcher

# --------------------------------------------------------------------------- #
# One sentinel per test run (unique suffix -- never reused across runs, never
# collides with governance's own fixture samples in the sibling w1_2 file).
# --------------------------------------------------------------------------- #
_SUFFIX = f"{uuid4().int % 10000:04d}"
SENTINEL_SSN: str = f"931-27-{_SUFFIX}"
SENTINEL_EMAIL: str = f"sentinel.{_SUFFIX}@property-w1-2.test"
SENTINEL_PHONE: str = f"415-555-{_SUFFIX}"
SENTINEL_PARTS: list[str] = [SENTINEL_SSN, SENTINEL_EMAIL, SENTINEL_PHONE]
MARKER = "[REDACTED]"

# The ONLY three locations a raw sentinel part is ever permitted to land:
#   1. the generic attended artifact store file (write_attended_artifact),
#   2. a native pack's declared output-root file (write_native_artifact),
#   3. a pp archive_artifact payload, as captured by the stub dispatcher
#      (the engineer's own code/diff summary -- a different trust boundary
#      pp's own ledger owns, never redacted before being archived there).
ALLOWED_RAW_LOCATIONS: list[str] = [
    "attended_artifact_store_file",
    "native_pack_output_file",
    "pp_archive_artifact_payload",
]


def _free_text(suffix: str) -> str:
    """A sentence embedding all three sentinel parts, distinguishable per call
    site so a failed assertion names which input leaked."""
    return (f"re: {suffix} -- contact {SENTINEL_EMAIL}, ssn {SENTINEL_SSN}, "
            f"call {SENTINEL_PHONE}")


def _assert_clean(text: Any, where: str) -> None:
    s = str(text)
    for part in SENTINEL_PARTS:
        assert part not in s, f"sentinel part {part!r} leaked into {where}: {s!r}"


def _assert_json_clean(value: Any, where: str) -> None:
    _assert_clean(json.dumps(value, default=str), where)


def _assert_attended_dir_clean(root: Path, wf: str) -> None:
    """Every file under ``<root>/.hydra/<wf>/attended/`` except the generic
    attended-artifact-store leaf files (allow-listed location #1)."""
    base = Path(root) / ".hydra" / wf / "attended"
    if not base.exists():
        return
    allow_dir = (base / "artifacts").resolve()
    for f in base.rglob("*"):
        if f.is_dir():
            continue
        resolved = f.resolve()
        if allow_dir == resolved or allow_dir in resolved.parents:
            continue  # location #1 -- raw text belongs here
        _assert_clean(f.read_text(encoding="utf-8", errors="replace"), str(f))


def _assert_trace_clean(root: Path, wf: str) -> None:
    f = Path(root) / ".hydra" / wf / "trace.jsonl"
    if not f.exists():
        return
    text = f.read_text(encoding="utf-8")
    for line in text.splitlines():
        if not line.strip():
            continue
        _assert_clean(line, f"{f} line")


def _assert_everywhere_clean(root: Path, wf: str, *returned: Any) -> None:
    _assert_attended_dir_clean(root, wf)
    _assert_trace_clean(root, wf)
    for i, val in enumerate(returned):
        _assert_json_clean(val, f"returned value #{i}")


# =========================================================================== #
# Precondition: the sentinel is really matched by governance's own redactor.  #
# =========================================================================== #

def test_sentinel_precondition_is_really_redacted() -> None:
    for part in SENTINEL_PARTS:
        out = governance.redact_for_squad_boundary(
            f"x {part} y", scan_mcp_attacks=False)
        assert part not in out, f"{part!r} was not matched by _PII_PATTERNS"
        assert MARKER in out


# =========================================================================== #
# begin_stage                                                                 #
# =========================================================================== #

def test_begin_stage_sentinel_absent_everywhere(tmp_path: Path) -> None:
    disp = FakeDispatcher()
    wf = "wf-begin"
    block = f"## Hydra context\n{_free_text('context-block')}"
    res = host_bridge.begin_stage(
        disp, workflow_id=wf, run_id="run-1", project_path=str(tmp_path),
        request_text=_free_text("begin-stage-request"),
        project_root=str(tmp_path), hydra_context_block=block)
    _assert_everywhere_clean(tmp_path, wf, res)
    assert MARKER in res["host_action"]["prompt"]


# =========================================================================== #
# begin_squad_stage                                                           #
# =========================================================================== #

def test_begin_squad_stage_sentinel_absent_everywhere(tmp_path: Path) -> None:
    wf = "wf-begin-squad"
    res = host_bridge.begin_squad_stage(
        workflow_id=wf, task_id="task-1", squad_slug="marketing-strategy",
        entrypoint="claude-skill", lead_agent="brand-strategist",
        pack_cwd=str(tmp_path), project_root=str(tmp_path),
        request_text=_free_text("begin-squad-request"))
    _assert_everywhere_clean(tmp_path, wf, res)
    assert MARKER in res["host_action"]["prompt"]


def test_begin_squad_stage_allow_pii_keeps_sentinel_in_outbound_prompt(
        tmp_path: Path) -> None:
    """allow_pii=True is a deliberate opt-out of PII scrubbing for THIS
    squad's own outbound prompt -- confirms the opt-out still works post-fix,
    not merely that the default-False path redacts."""
    wf = "wf-begin-squad-allow"
    res = host_bridge.begin_squad_stage(
        workflow_id=wf, task_id="task-1", squad_slug="marketing-strategy",
        entrypoint="claude-skill", lead_agent="brand-strategist",
        pack_cwd=str(tmp_path), project_root=str(tmp_path),
        request_text=_free_text("allow-pii-request"), allow_pii=True)
    ha = res["host_action"]
    for part in SENTINEL_PARTS:
        assert part in ha["prompt"], f"{part!r} missing from allow_pii prompt"
        assert part in ha["task_description"]


# =========================================================================== #
# submit_host_result -- generate (success / rejection)                       #
# =========================================================================== #

def _begin_eng(disp: Any, tmp_path: Path, wf: str = "wf-gen") -> dict[str, Any]:
    return host_bridge.begin_stage(
        disp, workflow_id=wf, run_id="run-1", project_path=str(tmp_path),
        request_text="implement the thing", project_root=str(tmp_path))


@pytest.mark.parametrize("case", ["generate-success", "generate-rejected"],
                         ids=["generate-success", "generate-rejected"])
def test_submit_host_result_generate_sentinel_absent_everywhere(
        tmp_path: Path, case: str) -> None:
    wf = f"wf-{case}"
    if case == "generate-rejected":
        disp = _RejectingDispatcher(
            "record_attempt", f"ledger refused: {_free_text('record-attempt')}")
    else:
        disp = FakeDispatcher()
    res = _begin_eng(disp, tmp_path, wf=wf)
    cfile = res["cursor_path"]
    gen_text = _free_text("generate-summary")
    res2 = host_bridge.submit_host_result(
        disp, cursor_file=cfile, call_key="generate-0",
        result={"text": gen_text, "cost_usd": 0.05, "model": "m"})
    if case == "generate-success":
        assert res2["status"] == "awaiting_host"
        assert res2["state"] == "await_judge"
        # Allow-listed location #3: pp archive_artifact got the RAW text.
        archived = [a for _s, t, a, _q in disp.calls if t == "archive_artifact"]
        assert any(SENTINEL_EMAIL in str(a.get("bytes")) for a in archived), (
            "generate summary must reach pp archive_artifact RAW "
            "(allow-listed location, not a leak)")
        # But nothing else leaked.
        _assert_attended_dir_clean(tmp_path, wf)
        _assert_trace_clean(tmp_path, wf)
        _assert_json_clean(res2, "submit_host_result(generate-success) return")
    else:
        assert res2["status"] == "surfaced"
        _assert_everywhere_clean(tmp_path, wf, res2)
        assert MARKER in res2["error"]


class _RejectingDispatcher(FakeDispatcher):
    """FakeDispatcher whose one named tool returns a failure-shaped payload
    carrying free text (the sentinel)."""

    def __init__(self, tool: str, error: str, **kw: Any) -> None:
        super().__init__(**kw)
        self._reject_tool = tool
        self._reject_error = error

    def call_mcp(self, server: str, tool: str, args: dict, squad_id: str | None = None):
        if tool == self._reject_tool:
            self.calls.append((server, tool, dict(args), squad_id))
            return {"status": "failed", "error": self._reject_error}
        return super().call_mcp(server, tool, args, squad_id=squad_id)


# =========================================================================== #
# submit_host_result -- judge (success / rejection)                          #
# =========================================================================== #

def _drive_to_judge_local(disp: Any, tmp_path: Path, wf: str) -> tuple[str, dict]:
    res = _begin_eng(disp, tmp_path, wf=wf)
    cfile = res["cursor_path"]
    res = host_bridge.submit_host_result(
        disp, cursor_file=cfile, call_key="generate-0",
        result={"text": "edited foo.py", "cost_usd": 0.05, "model": "m"})
    return cfile, res


@pytest.mark.parametrize("case", ["judge-pass", "judge-fail"],
                         ids=["judge-pass", "judge-fail"])
def test_submit_host_result_judge_sentinel_absent_everywhere(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch, case: str) -> None:
    wf = f"wf-{case}"
    monkeypatch.setattr(host_bridge, "_judge_artifact_text",
                        lambda *a, **k: _free_text("judge-diff"))
    monkeypatch.setenv("HYDRA_ATTENDED_SMOKE_MODE", "sync")
    monkeypatch.setattr(host_bridge, "_run_smoke",
                        lambda *a, **k: ("pass", "fake smoke pass"))
    disp = FakeDispatcher(required_cross_vendor=False)
    cfile, res = _drive_to_judge_local(disp, tmp_path, wf)
    assert MARKER in res["host_action"]["artifact_text"]
    call_key = res["host_action"]["call_key"]
    outcome = "pass" if case == "judge-pass" else "fail"
    res2 = host_bridge.submit_host_result(
        disp, cursor_file=cfile, call_key=call_key,
        result={"outcome": outcome, "judge_producer": "claude",
                "cost_usd": 0.02,
                "critique_md": _free_text("critique"),
                "score_json": {"notes": _free_text("score-notes")}})
    _assert_everywhere_clean(tmp_path, wf, res, res2)
    # record_verdict's own call args (the dispatcher side of the boundary)
    # must never have carried the raw critique/score text either.
    rv_calls = [a for _s, t, a, _q in disp.calls if t == "record_verdict"]
    for a in rv_calls:
        _assert_json_clean(a, "record_verdict call args")


def test_judge_success_redacts_artifact_text_everywhere(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Pins the judge-payload decision: the diff text sent to pp's judge
    critique call is the REDACTED copy, not the raw one."""
    wf = "wf-judge-payload"
    monkeypatch.setattr(host_bridge, "_judge_artifact_text",
                        lambda *a, **k: _free_text("judge-artifact"))
    disp = FakeDispatcher(required_cross_vendor=False)
    cfile, res = _drive_to_judge_local(disp, tmp_path, wf)
    assert res["host_action"]["artifact_text"] != _free_text("judge-artifact")
    for part in SENTINEL_PARTS:
        assert part not in res["host_action"]["artifact_text"]
    assert MARKER in res["host_action"]["artifact_text"]


# =========================================================================== #
# submit_host_result -- smoke (pass / fail), sync mode                        #
# =========================================================================== #

@pytest.mark.parametrize("smoke_status", ["pass", "fail"],
                         ids=["smoke-pass", "smoke-fail"])
def test_submit_host_result_smoke_sentinel_absent_everywhere(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch, smoke_status: str) -> None:
    wf = f"wf-smoke-{smoke_status}"
    monkeypatch.setattr(host_bridge, "_judge_artifact_text",
                        lambda *a, **k: "clean diff, no pii")
    monkeypatch.setenv("HYDRA_ATTENDED_SMOKE_MODE", "sync")
    monkeypatch.setattr(
        host_bridge, "_run_smoke",
        lambda *a, **k: (smoke_status, _free_text(f"smoke-{smoke_status}")))
    disp = FakeDispatcher(required_cross_vendor=False)
    cfile, res = _drive_to_judge_local(disp, tmp_path, wf)
    call_key = res["host_action"]["call_key"]
    res2 = host_bridge.submit_host_result(
        disp, cursor_file=cfile, call_key=call_key,
        result={"outcome": "pass", "judge_producer": "claude",
                "cost_usd": 0.02, "critique_md": "fine"})
    _assert_everywhere_clean(tmp_path, wf, res, res2)
    smoke_calls = [a for _s, t, a, _q in disp.calls if t == "record_smoke_status"]
    for a in smoke_calls:
        _assert_json_clean(a, "record_smoke_status call args")
    cur = host_bridge.load_cursor(cfile)
    assert cur["smoke_status"] == smoke_status
    assert MARKER in cur["smoke_reason"]


# =========================================================================== #
# submit_host_result -- squad (success / rejection), native + non-native      #
# =========================================================================== #

def test_squad_non_native_success_sentinel_absent_and_store_file_raw(
        tmp_path: Path) -> None:
    wf = "wf-squad-nonnative-ok"
    res = host_bridge.begin_squad_stage(
        workflow_id=wf, task_id="task-1", squad_slug="healthcare",
        entrypoint="claude-skill", lead_agent="lead", pack_cwd=str(tmp_path),
        project_root=str(tmp_path), request_text="triage")
    disp = FakeDispatcher()
    text = _free_text("squad-nonnative")
    res2 = host_bridge.submit_host_result(
        disp, cursor_file=res["cursor_path"], call_key="squad-task-1-0",
        result={"text": text})
    assert res2["status"] == "complete"
    # Allow-listed location #1: the generic attended artifact store got the
    # RAW text.
    store_file = (tmp_path / ".hydra" / wf / "attended" / "artifacts"
                  / "task-1.md")
    assert store_file.read_text(encoding="utf-8") == text
    assert SENTINEL_EMAIL in store_file.read_text(encoding="utf-8")
    # The cursor/trace/return copy is the redacted one.
    cur = host_bridge.load_cursor(res["cursor_path"])
    for part in SENTINEL_PARTS:
        assert part not in cur["artifact_text"]
    assert MARKER in cur["artifact_text"]
    _assert_attended_dir_clean(tmp_path, wf)
    _assert_trace_clean(tmp_path, wf)
    _assert_json_clean(res2, "submit_host_result(squad-nonnative) return")


def test_squad_non_native_persist_failure_sentinel_absent_everywhere(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    wf = "wf-squad-nonnative-fail"
    res = host_bridge.begin_squad_stage(
        workflow_id=wf, task_id="task-1", squad_slug="healthcare",
        entrypoint="claude-skill", lead_agent="lead", pack_cwd=str(tmp_path),
        project_root=str(tmp_path), request_text="triage")

    def _boom(*_a: Any, **_k: Any) -> Any:
        raise OSError(f"disk is read-only near {_free_text('store-boom')}")

    monkeypatch.setattr(artifact_store, "write_attended_artifact", _boom)
    disp = FakeDispatcher()
    res2 = host_bridge.submit_host_result(
        disp, cursor_file=res["cursor_path"], call_key="squad-task-1-0",
        result={"text": _free_text("squad-nonnative-fail-text")})
    assert res2["status"] == "complete_unpersisted"
    _assert_everywhere_clean(tmp_path, wf, res2)


def test_squad_native_success_sentinel_absent_and_output_file_raw(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(artifact_store, "native_pack_root", lambda _slug: tmp_path)
    wf = "wf-squad-native-ok"
    res = host_bridge.begin_squad_stage(
        workflow_id=wf, task_id="task-1", squad_slug="executive",
        entrypoint="claude-native", lead_agent="executive-suite:boardroom",
        pack_cwd=str(tmp_path), project_root=str(tmp_path),
        request_text="frame the decision")
    disp = FakeDispatcher()
    text = _free_text("squad-native")
    res2 = host_bridge.submit_host_result(
        disp, cursor_file=res["cursor_path"], call_key="squad-task-1-0",
        result={"text": text})
    assert res2["status"] == "complete"
    assert res2["artifact_persisted_via"] == "native"
    # Allow-listed location #2: the native pack's declared output root got
    # the RAW text, written in-process by host_bridge (not by cli.py).
    out_file = tmp_path / "output" / "attended" / "task-1.md"
    assert out_file.read_text(encoding="utf-8") == text
    assert SENTINEL_EMAIL in out_file.read_text(encoding="utf-8")
    cur = host_bridge.load_cursor(res["cursor_path"])
    for part in SENTINEL_PARTS:
        assert part not in cur["artifact_text"]
    assert MARKER in cur["artifact_text"]
    _assert_attended_dir_clean(tmp_path, wf)
    _assert_trace_clean(tmp_path, wf)
    _assert_json_clean(res2, "submit_host_result(squad-native) return")


def test_squad_native_persist_failure_sentinel_absent_everywhere(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    def _boom(_slug: str, _relative: str, _content: str) -> Any:
        raise ValueError(f"not a text artifact near {_free_text('native-boom')}")

    monkeypatch.setattr(artifact_store, "write_native_artifact", _boom)
    wf = "wf-squad-native-fail"
    res = host_bridge.begin_squad_stage(
        workflow_id=wf, task_id="task-1", squad_slug="executive",
        entrypoint="claude-native", lead_agent="executive-suite:boardroom",
        pack_cwd=str(tmp_path), project_root=str(tmp_path),
        request_text="frame the decision")
    disp = FakeDispatcher()
    res2 = host_bridge.submit_host_result(
        disp, cursor_file=res["cursor_path"], call_key="squad-task-1-0",
        result={"text": _free_text("squad-native-fail-text")})
    assert res2["status"] == "complete_unpersisted"
    assert "artifact_persist_error" in res2
    _assert_everywhere_clean(tmp_path, wf, res2)


# =========================================================================== #
# PII-free squad text -> byte-identical artifact + DECISION_RECORD            #
# =========================================================================== #

def test_clean_squad_text_byte_identical_artifact_and_decision_record(
        tmp_path: Path) -> None:
    wf = "wf-squad-clean"
    clean_text = "# Clean triage report\nno PII in this one."
    res = host_bridge.begin_squad_stage(
        workflow_id=wf, task_id="task-1", squad_slug="healthcare",
        entrypoint="claude-skill", lead_agent="lead", pack_cwd=str(tmp_path),
        project_root=str(tmp_path), request_text="triage")
    disp = FakeDispatcher()
    res2 = host_bridge.submit_host_result(
        disp, cursor_file=res["cursor_path"], call_key="squad-task-1-0",
        result={"text": clean_text})
    assert res2["status"] == "complete"
    cur = host_bridge.load_cursor(res["cursor_path"])
    # Byte-identical: PII-free text passes through redaction unchanged.
    assert cur["artifact_text"] == clean_text
    store_file = (tmp_path / ".hydra" / wf / "attended" / "artifacts"
                  / "task-1.md")
    assert store_file.read_text(encoding="utf-8") == clean_text

    state = HydraState(root_goal="clean squad decision record")
    record = cli._attended_result_record(state, res2)
    assert record is not None
    state2 = HydraState(root_goal="clean squad decision record",
                        attended_results=[record])
    envelopes, artifacts = cli._materialize_attended_results(state2)
    assert len(envelopes) == 1
    assert envelopes[0]["artifacts"][0]["key"] == res2["artifact_ref"]["key"]
    assert artifacts[0]["ref"] == res2["artifact_ref"]["key"]


# =========================================================================== #
# _outbound_deep -- dict keys + tuple/set/frozenset traversal                #
# =========================================================================== #

def test_outbound_deep_redacts_dict_keys_and_preserves_tuple_shape() -> None:
    nested = {
        ("loc", SENTINEL_EMAIL): (SENTINEL_SSN, {SENTINEL_PHONE: 1}),
    }
    out = host_bridge._outbound_deep(nested, kind="engineer")
    flat = json.dumps(
        {str(k): v for k, v in out.items()}, default=str)
    for part in SENTINEL_PARTS:
        assert part not in flat, f"{part!r} leaked through _outbound_deep"
    # Container shapes are preserved: dict -> dict, tuple -> tuple.
    [key] = list(out.keys())
    assert isinstance(key, tuple)
    val = out[key]
    assert isinstance(val, tuple)
    assert isinstance(val[1], dict)


def test_outbound_deep_preserves_set_and_frozenset_types() -> None:
    s = {SENTINEL_EMAIL, "clean"}
    fs = frozenset({SENTINEL_PHONE, "clean2"})
    out_s = host_bridge._outbound_deep(s, kind="engineer")
    out_fs = host_bridge._outbound_deep(fs, kind="engineer")
    assert isinstance(out_s, set)
    assert isinstance(out_fs, frozenset)
    assert all(SENTINEL_EMAIL not in str(v) for v in out_s)
    assert all(SENTINEL_PHONE not in str(v) for v in out_fs)


# =========================================================================== #
# record_rejected_envelopes -- pydantic-error-shaped payload                  #
# =========================================================================== #

def test_record_rejected_envelopes_pydantic_shaped_tuple_loc_and_nested_key(
        tmp_path: Path) -> None:
    wf = "wf-rejected"
    cfile = cursor_path(str(tmp_path), wf, "run-rej")
    save_cursor(cfile, {
        "schema": CURSOR_SCHEMA, "workflow_id": wf, "run_id": "run-rej",
        "task_id": "task-rej", "squad_slug": "engineering", "state": "complete",
    })
    rejected = [{
        "envelope_id": "11111111-2222-3333-4444-555555555555",
        "status": "failed",
        "detail": _free_text("rejected-detail"),
        "errors": [{
            "loc": ("body", SENTINEL_EMAIL, 0),
            "msg": f"value error, got {SENTINEL_SSN}",
            "type": "value_error",
            "input": {SENTINEL_PHONE: "x", "nested": {"inner": SENTINEL_SSN}},
            "ctx": {"detail": _free_text("ctx-detail")},
        }],
    }]
    host_bridge.record_rejected_envelopes(cfile, rejected)
    cur = host_bridge.load_cursor(cfile)
    item = cur["rejected_envelopes"][0]
    assert item["envelope_id"] == "11111111-2222-3333-4444-555555555555"
    assert item["status"] == "failed"
    err = item["errors"][0]
    assert isinstance(err["loc"], list) or isinstance(err["loc"], tuple)
    assert err["type"] == "value_error"
    _assert_json_clean(cur["rejected_envelopes"], "rejected_envelopes")
    _assert_attended_dir_clean(tmp_path, wf)
    _assert_trace_clean(tmp_path, wf)


# =========================================================================== #
# recover_stalled_stage -- error path                                        #
# =========================================================================== #

def _write_recovery_cursor(tmp_path: Path, **over: Any) -> Path:
    payload: dict[str, Any] = {
        "attempt_id": "att-1", "outcome": "pass",
        "judge_producer": "codex", "judge_model_id": "codex-default",
        "critique_md": "looks good", "score_json": {},
        "rubric_id": "rfc-2119-normative", "idempotency_token": "judge-0",
    }
    cursor: dict[str, Any] = {
        "schema": CURSOR_SCHEMA, "kind": "engineering",
        "workflow_id": "wf-recover", "run_id": "run-recover", "stage_id": "stage-1",
        "attempt_id": "att-1", "project_path": str(tmp_path),
        "project_root": str(tmp_path), "repo_root": str(tmp_path),
        "branch": "attended/rec", "preserved_branch": "attended/rec",
        "state": "surfaced", "outcome": "pass", "final_status": "surfaced",
        "cost_usd": 0.1, "tokens_in": 1, "tokens_out": 1,
        "smoke_status": None, "finalized": True, "charged": True,
        "verdict_recorded_for": "judge-0",
        "pending_verdict_payload": payload,
    }
    cursor.update(over)
    cfile = tmp_path / ".hydra" / "wf-recover" / "attended" / "run-recover.json"
    save_cursor(cfile, cursor)
    return cfile


def test_recover_stalled_stage_merge_failure_sentinel_absent_everywhere(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    cfile = _write_recovery_cursor(tmp_path)
    monkeypatch.setattr(
        host_bridge, "_merge_branch_back",
        lambda repo, branch: {"merged": False, "sha": None, "base": None,
                              "error": f"merge_failed: {_free_text('merge')}"})
    disp = FakeDispatcher()
    rec = host_bridge.recover_stalled_stage(disp, cursor_file=cfile)
    assert rec["ok"] is False
    _assert_everywhere_clean(tmp_path, "wf-recover", rec)


# =========================================================================== #
# abort_stage                                                                #
# =========================================================================== #

def test_abort_stage_sentinel_absent_everywhere(tmp_path: Path) -> None:
    wf = "wf-abort"
    cfile = tmp_path / ".hydra" / wf / "attended" / "run-abort.json"
    save_cursor(cfile, {
        "schema": CURSOR_SCHEMA, "workflow_id": wf, "run_id": "run-abort",
        "stage_id": "stage-1", "state": "await_generate", "finalized": False,
        "pending_action": {"call_key": "generate-0"},
        "project_path": str(tmp_path), "project_root": str(tmp_path),
    })
    disp = FakeDispatcher()
    out = host_bridge.abort_stage(
        disp, cursor_file=cfile, reason=_free_text("operator-abort"))
    assert out["status"] == "aborted"
    _assert_everywhere_clean(tmp_path, wf, out)
    assert MARKER in out["error"]


# =========================================================================== #
# sweep_stale_worktrees -- error path                                        #
# =========================================================================== #

def _init_git_repo(path: Path) -> None:
    import subprocess

    def _git(args: list[str]) -> None:
        subprocess.run(["git", *args], cwd=str(path), capture_output=True,
                       text=True, check=False)

    path.mkdir(parents=True, exist_ok=True)
    _git(["init", "-q"])
    _git(["-c", "user.name=t", "-c", "user.email=t@t", "commit",
         "--allow-empty", "-q", "-m", "init"])


def test_sweep_stale_worktrees_error_path_sentinel_absent_everywhere(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from unittest.mock import MagicMock

    repo_root = tmp_path / "repo"
    _init_git_repo(repo_root)
    prov = _provision_worktree(str(repo_root), "runSweep")
    assert prov is not None
    wt_path, _branch = prov

    wf = "wf-sweep"
    cfile = cursor_path(str(repo_root), wf, "runSweep")
    save_cursor(cfile, {
        "schema": CURSOR_SCHEMA, "workflow_id": wf, "run_id": "runSweep",
        "state": "complete",
    })

    real_git = host_bridge._git

    def _fake_git(args: list[str], cwd: Any):
        if len(args) >= 2 and args[0] == "worktree" and args[1] == "remove":
            fake = MagicMock()
            fake.returncode = 128
            fake.stdout = ""
            fake.stderr = f"fatal: unable to remove worktree: {_free_text('sweep')}"
            return fake
        return real_git(args, cwd)

    monkeypatch.setattr(host_bridge, "_git", _fake_git)
    report = sweep_stale_worktrees(str(repo_root), str(repo_root))
    assert report["removed"] == []
    assert len(report["errors"]) == 1
    _assert_json_clean(report, "sweep_stale_worktrees report")
    assert MARKER in report["errors"][0]["error"]
    _assert_attended_dir_clean(repo_root, wf)
    _assert_trace_clean(repo_root, wf)
