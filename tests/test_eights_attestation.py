"""Tests for the eights-attestation adapter and supervisor wiring."""
from __future__ import annotations

from pathlib import Path
from uuid import uuid4

from hydra_core.eights.attestation import EightsAttestor, EIGHTS_MCP_SERVER
from hydra_core.immortal_head import load_constitution
from hydra_core.state import HydraState


HYDRA_ROOT = Path(__file__).resolve().parents[1]


class _NullDispatcher:
    def call_mcp(self, *a, **k):
        return {"status": "failed", "error": "no eights-daemon registered"}

    def spawn_subprocess(self, *a, **k): return {"status": "done", "stdout": "", "stderr": ""}
    def emit_claude_prompt(self, *a, **k): return {"status": "host_pickup_required"}
    def invoke_claude_skill(self, *a, **k): return {"status": "host_pickup_required"}


class _RecordingDispatcher:
    """Logs every eights MCP call; can replay scripted responses by tool name."""
    def __init__(self, scripted: dict[str, dict] | None = None):
        self.calls: list[dict] = []
        self.scripted = scripted or {}

    def call_mcp(self, server, tool, args, **_kw):
        self.calls.append({"server": server, "tool": tool, "args": args})
        if tool in self.scripted:
            return {"status": "done", "tool": tool, "result": self.scripted[tool]}
        return {"status": "done", "tool": tool, "result": {}}

    def spawn_subprocess(self, *a, **k): return {"status": "done", "stdout": "", "stderr": ""}
    def emit_claude_prompt(self, *a, **k): return {"status": "host_pickup_required"}
    def invoke_claude_skill(self, *a, **k): return {"status": "host_pickup_required"}


# ---------------- adapter ----------------

def test_attestor_noops_with_no_dispatcher():
    a = EightsAttestor()
    snap = load_constitution(HYDRA_ROOT)
    assert a.constitution_attest(snap) is None
    assert a.envelope_record({"id": "x", "type": "PRD"}) is None
    assert a.ceiling_tick(workflow_id="x", node="intake") is None
    assert a.redact_for_squad(text="hi", from_squad="a", to_squad="b") is None


def test_attestor_swallows_dispatcher_failures():
    a = EightsAttestor(dispatcher=_NullDispatcher())
    snap = load_constitution(HYDRA_ROOT)
    # Daemon returns failed → adapter returns None instead of raising.
    assert a.constitution_attest(snap) is None


def test_constitution_attest_call_shape():
    d = _RecordingDispatcher(scripted={
        "eights.constitution.attest": {
            "hash": "sha256:abc", "version": "1", "receipt": "rcpt-001"
        },
    })
    a = EightsAttestor(dispatcher=d, workflow_id="wf-test")
    snap = load_constitution(HYDRA_ROOT)
    out = a.constitution_attest(snap)
    assert out is not None
    assert out["receipt"] == "rcpt-001"
    assert d.calls[0]["server"] == EIGHTS_MCP_SERVER
    assert d.calls[0]["tool"] == "eights.constitution.attest"
    args = d.calls[0]["args"]
    assert args["consumer"] == "hydra"
    assert args["envelope"]["actor_id"] == "hydra.supervisor"
    assert args["envelope"]["trace_id"] == "wf-test"


def test_envelope_record_skips_when_id_missing():
    d = _RecordingDispatcher()
    a = EightsAttestor(dispatcher=d)
    assert a.envelope_record({}) is None
    assert d.calls == []


def test_envelope_record_call_shape():
    d = _RecordingDispatcher()
    a = EightsAttestor(dispatcher=d)
    env = {
        "id": "11111111-1111-1111-1111-111111111111",
        "type": "DECISION_RECORD",
        "workflow_id": "22222222-2222-2222-2222-222222222222",
        "origin_squad": "executive",
        "target_squad": "human",
        "parent_id": "33333333-3333-3333-3333-333333333333",
    }
    a.envelope_record(env)
    assert d.calls[0]["tool"] == "eights.hydra.envelope.record"
    args = d.calls[0]["args"]
    assert "envelope" in args
    hydra_env = args["hydra_envelope"]
    assert hydra_env["id"] == env["id"]
    assert hydra_env["type"] == "DECISION_RECORD"
    assert hydra_env["origin_squad"] == "executive"


def test_redact_for_squad_returns_redacted_text():
    d = _RecordingDispatcher(scripted={
        "eights.governance.redact_for_squad": {"redacted": "ssn [REDACTED]"},
    })
    a = EightsAttestor(dispatcher=d)
    out = a.redact_for_squad(text="ssn 123-45-6789", from_squad="executive", to_squad="garland")
    assert out == "ssn [REDACTED]"


def test_redact_returns_none_when_daemon_silent():
    d = _RecordingDispatcher()  # no scripted response → empty result
    a = EightsAttestor(dispatcher=d)
    out = a.redact_for_squad(text="hi", from_squad="a", to_squad="b")
    assert out is None  # caller falls back to local redaction


def test_prompt_get_returns_text():
    d = _RecordingDispatcher(scripted={
        "eights.prompt.get": {"text": "You are the boardroom facilitator."},
    })
    a = EightsAttestor(dispatcher=d)
    assert a.prompt_get(slug="boardroom") == "You are the boardroom facilitator."


def test_disabled_attestor_short_circuits():
    d = _RecordingDispatcher()
    a = EightsAttestor(dispatcher=d, enabled=False)
    a.envelope_record({"id": "x", "type": "PRD"})
    a.ceiling_tick(workflow_id="x", node="intake")
    assert d.calls == []


# ---------------- supervisor integration ----------------

class _PassClient:
    def critique(self, **k):
        return {"outcome": "pass", "critique_md": "ok " * 40, "score_json": {"x": 5}}


def test_supervisor_stamps_constitution_hash_at_intake():
    from hydra_core.supervisor import build_supervisor

    sup = build_supervisor(
        project_root=HYDRA_ROOT,
        dispatcher=_NullDispatcher(),
        critique_client=_PassClient(),
        force_pure_python=True,
    )
    state = HydraState(root_goal="quick refresh")
    final = sup.invoke(state)
    snap = load_constitution(HYDRA_ROOT)
    assert final.constitution_hash == snap.sha256


def test_supervisor_calls_eights_envelope_record():
    """Even with a null daemon, the eights adapter must be invoked."""
    from hydra_core.supervisor import build_supervisor

    d = _RecordingDispatcher()
    sup = build_supervisor(
        project_root=HYDRA_ROOT,
        dispatcher=d,
        critique_client=_PassClient(),
        force_pure_python=True,
    )
    state = HydraState(root_goal="quick refresh")
    sup.invoke(state)

    eights_calls = [c for c in d.calls if c["server"] == EIGHTS_MCP_SERVER]
    tools_called = {c["tool"] for c in eights_calls}
    assert "eights.constitution.attest" in tools_called
    assert "eights.governance.ceiling.tick" in tools_called
    assert "eights.hydra.envelope.record" in tools_called


# ---------------- memory federation ----------------

def test_memory_search_attaches_envelope_and_query():
    d = _RecordingDispatcher(scripted={"eights.memory.search": {"hits": [{"id": "m1"}]}})
    a = EightsAttestor(dispatcher=d, workflow_id="wf-1")
    res = a.memory_search("billing retries", top_k=5, fusion="hybrid")
    assert res == {"hits": [{"id": "m1"}]}
    call = next(c for c in d.calls if c["tool"] == "eights.memory.search")
    assert call["args"]["query"] == "billing retries"
    assert call["args"]["top_k"] == 5
    assert call["args"]["fusion"] == "hybrid"
    # Audit envelope is attached, same contract as every other eights call.
    assert call["args"]["envelope"]["trace_id"] == "wf-1"


def test_memory_search_workflow_id_is_per_call():
    # Per-call workflow_id stamps the envelope without mutating the shared
    # attestor — so concurrent callers can't cross-contaminate audit lineage.
    d = _RecordingDispatcher(scripted={"eights.memory.search": {"hits": []}})
    a = EightsAttestor(dispatcher=d, workflow_id="default-wf")
    a.memory_search("q1", workflow_id="call-wf")
    call = next(c for c in d.calls if c["tool"] == "eights.memory.search")
    assert call["args"]["envelope"]["trace_id"] == "call-wf"
    assert a.workflow_id == "default-wf"  # instance state untouched


def test_memory_search_noops_without_dispatcher():
    assert EightsAttestor().memory_search("x") is None


def test_memory_search_noops_on_empty_query():
    d = _RecordingDispatcher()
    a = EightsAttestor(dispatcher=d)
    assert a.memory_search("   ") is None
    assert d.calls == []  # never reaches the daemon


def test_memory_search_returns_none_on_daemon_failure():
    # Daemon returns status=failed → federation degrades to None (caller
    # falls back to local search).
    assert EightsAttestor(dispatcher=_NullDispatcher()).memory_search("q") is None
