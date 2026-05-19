"""Tests for judge policy loader + MCP client response normalization."""
from __future__ import annotations

from pathlib import Path

import pytest

from hydra_core.judge.policy import JudgePolicy, load_policy
from hydra_core.judge.mcp_client import MCPCritiqueClient, _normalize_pp_response


# ---------------- policy loader ----------------

def test_default_policy_loads_and_has_enabled_squads():
    p = load_policy(None)
    assert isinstance(p, JudgePolicy)
    # Built-in defaults include executive + creative.
    assert "executive" in p.enabled_squads
    assert "creative" in p.enabled_squads


def test_default_policy_hitl_severities_present():
    p = load_policy(None)
    assert "constitution-alignment@1" in p.hitl_on_fail
    assert "phi-redaction-completeness@1" in p.hitl_on_fail


def test_squad_enabled_when_empty_set():
    p = JudgePolicy(enabled_squads=set())
    assert p.squad_enabled("anything") is True


def test_squad_enabled_respects_allowlist():
    p = JudgePolicy(enabled_squads={"executive"})
    assert p.squad_enabled("executive") is True
    assert p.squad_enabled("sales-gtm") is False


def test_squad_enabled_none_treated_as_post_synthesis():
    p = JudgePolicy(enabled_squads={"executive"})
    assert p.squad_enabled(None) is True


def test_project_override_overlays_defaults(tmp_path: Path):
    (tmp_path / ".hydra").mkdir()
    (tmp_path / ".hydra" / "judge_policy.yaml").write_text(
        "enabled_squads: [research-ds]\n"
        "budget_cap_per_workflow_usd: 0.5\n",
        encoding="utf-8",
    )
    p = load_policy(tmp_path)
    assert p.enabled_squads == {"research-ds"}
    assert p.budget_cap_per_workflow_usd == 0.5
    # hitl_on_fail not overridden → keeps defaults
    assert "constitution-alignment@1" in p.hitl_on_fail


# ---------------- MCP response normalization ----------------

def test_normalize_pp_native_envelope_with_parsed_block():
    """Real PP CodexResult / GeminiResult shape — verdict lives in parsed."""
    raw = {
        "text": "raw stdout",
        "parsed": {
            "outcome": "revise",
            "critique_md": "needs more rigor",
            "score": {"clarity": 0.8, "rigor": 0.4},
        },
        "tokens_in": 100,
        "tokens_out": 200,
        "cost_usd": 0.001,
        "model": "gemini-2.5-pro",
    }
    out = _normalize_pp_response(raw)
    assert out == {
        "outcome": "revise",
        "critique_md": "needs more rigor",
        "score_json": {"clarity": 0.8, "rigor": 0.4},
    }


def test_normalize_pp_flat_legacy_shape():
    """Older / alt schema where verdict fields are flat at the top level."""
    raw = {
        "verdict": "revise",
        "critique": "needs more rigor",
        "scores": {"clarity": 3},
    }
    out = _normalize_pp_response(raw)
    assert out == {
        "outcome": "revise",
        "critique_md": "needs more rigor",
        "score_json": {"clarity": 3},
    }


def test_normalize_alternate_shape():
    raw = {
        "outcome": "pass",
        "critique_md": "ok",
        "score_json": {"a": 5},
    }
    out = _normalize_pp_response(raw)
    assert out["outcome"] == "pass"
    assert out["score_json"]["a"] == 5


def test_normalize_rejects_unknown_outcome():
    with pytest.raises(RuntimeError):
        _normalize_pp_response({"verdict": "maybe", "critique": "x"})


def test_normalize_rejects_non_dict():
    with pytest.raises(RuntimeError):
        _normalize_pp_response("not a dict")


# ---------------- MCPCritiqueClient round-trip ----------------

class _FakeDispatcher:
    def __init__(self, response: dict):
        self.response = response
        self.calls: list[dict] = []

    def call_mcp(self, *, server, tool, args):
        self.calls.append({"server": server, "tool": tool, "args": args})
        return {"status": "done", "result": self.response}


def test_mcp_client_routes_codex_to_pp_codex():
    fake = _FakeDispatcher({"verdict": "pass", "critique": "ok " * 30, "scores": {"x": 5}})
    client = MCPCritiqueClient(dispatcher=fake, cwd="/tmp")
    out = client.critique(vendor="codex", artifact_text="hi", rubric_md="rubric")
    assert fake.calls[0]["server"] == "pp-codex"
    assert fake.calls[0]["tool"] == "critique"
    assert out["outcome"] == "pass"


def test_mcp_client_routes_gemini_to_pp_gemini():
    fake = _FakeDispatcher({"verdict": "revise", "critique": "x", "scores": {}})
    client = MCPCritiqueClient(dispatcher=fake, cwd="/tmp")
    client.critique(vendor="gemini", artifact_text="hi", rubric_md="rubric")
    assert fake.calls[0]["server"] == "pp-gemini"


def test_mcp_client_raises_on_failed_status():
    class _BadDispatcher:
        def call_mcp(self, **kw):
            return {"status": "failed", "error": "boom"}

    client = MCPCritiqueClient(dispatcher=_BadDispatcher(), cwd="/tmp")
    with pytest.raises(RuntimeError):
        client.critique(vendor="codex", artifact_text="x", rubric_md="y")


def test_mcp_client_rejects_unsupported_vendor():
    client = MCPCritiqueClient(dispatcher=_FakeDispatcher({}), cwd="/tmp")
    with pytest.raises(RuntimeError):
        client.critique(vendor="claude", artifact_text="x", rubric_md="y")
