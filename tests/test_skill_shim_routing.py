"""Phase-3 ingest-correctness tests: skill-shim routing (F6) + emitted-envelope
drop instrumentation (F8)."""
from __future__ import annotations

import logging
from uuid import uuid4

from hydra_core.squad_node import _resolve_skill_shim, _extract_emitted_envelopes
from hydra_core.schemas import CSuiteDecisionPacket
from hydra_core.state import HydraState


# --------------------------------------------------------------------------- #
# F6 — every claude-skill squad routes to the RIGHT shim server/prefix/path_key
# --------------------------------------------------------------------------- #
def test_customer_support_routes_to_xenia():
    shim = _resolve_skill_shim("customer-support")
    assert shim["server"] == "xenia"
    assert shim["prefix"] == "xenia"
    # xenia/server.py output_write honors ONLY `phase` (ignores `domain`).
    assert shim["path_key"] == "phase"


def test_marketing_squads_route_to_marketbliss():
    for slug in ("marketing-creative", "marketing-ops", "marketing-production",
                 "marketing-research", "marketing-strategy"):
        shim = _resolve_skill_shim(slug)
        assert shim["server"] == "marketbliss", slug
        assert shim["prefix"] == "mb", slug
        assert shim["path_key"] == "domain", slug


def test_known_skill_squads_unchanged():
    assert _resolve_skill_shim("garland")["server"] == "rlm_creative"
    assert _resolve_skill_shim("legal-compliance")["server"] == "senate"
    assert _resolve_skill_shim("rlm-gaming")["server"] == "rlm_gaming"


def test_unknown_squad_falls_back_to_garland_with_warning(caplog):
    with caplog.at_level(logging.WARNING):
        shim = _resolve_skill_shim("totally-unknown-squad")
    assert shim["server"] == "rlm_creative"  # garland fallback (no crash)
    assert any("no _SKILL_PACK_SHIMS entry" in r.message for r in caplog.records)


# --------------------------------------------------------------------------- #
# F8 — malformed emitted envelopes are COUNTED, not silently dropped
# --------------------------------------------------------------------------- #
def _inbound() -> CSuiteDecisionPacket:
    return CSuiteDecisionPacket(
        workflow_id=uuid4(), origin_squad="hydra", origin="BOARDROOM", objective="g")


def test_validation_drop_is_counted_and_logged(caplog):
    state = HydraState(root_goal="t")
    inbound = _inbound()
    # A DEV_TASK missing required fields (owner/repo/branch) fails validation.
    result = {"emitted_envelopes": [
        {"type": "DEV_TASK", "instructions": "do x"},          # invalid -> drop
        {"type": "DECISION_RECORD", "decision": "noted"},      # non-delegation -> skip_type
    ]}
    with caplog.at_level(logging.WARNING):
        out = _extract_emitted_envelopes(result, inbound, "rlm-gaming", state)
    assert out == []
    # The validation drop is counted; the benign type-filter is NOT.
    assert state.error_counters.get("emitted_envelope_drop") == 1
    assert any("FAILED validation" in r.message for r in caplog.records)


def test_valid_envelope_passes_without_drop():
    state = HydraState(root_goal="t")
    inbound = _inbound()
    result = {"emitted_envelopes": [{
        "type": "DEV_TASK", "owner": "backend", "repo": "hydra",
        "branch": "wf", "instructions": "add a file",
    }]}
    out = _extract_emitted_envelopes(result, inbound, "rlm-gaming", state)
    assert len(out) == 1
    assert out[0].type == "DEV_TASK"
    assert out[0].origin_squad == "rlm-gaming"  # forced to producer
    assert "emitted_envelope_drop" not in state.error_counters


# --------------------------------------------------------------------------- #
# F7 (LOCKED #1) — host-driven skill executor runs the skill, not host_pickup
# --------------------------------------------------------------------------- #
class _HostSkillDispatcher:
    def __init__(self, scripted, hosted):
        self.calls = []
        self._scripted = scripted
        self._hosted = hosted
        self.host_calls = []

    def call_mcp(self, server, tool, args, **_kw):
        self.calls.append((server, tool, args))
        key = (server, tool)
        if key in self._scripted:
            return {"status": "done", "tool": tool, "result": self._scripted[key]}
        return {"status": "failed", "error": "no script"}

    def spawn_subprocess(self, cmd, env=None):
        return {"status": "stub"}

    def emit_claude_prompt(self, prompt, agent=None):
        return {"status": "host_pickup_required"}

    def invoke_claude_skill(self, skill, args):
        raise AssertionError("host executor should run instead of host_pickup")

    def run_host_agent(self, agent_type, prompt, *, cwd=None, timeout_s=None):
        self.host_calls.append(agent_type)
        return self._hosted


def test_host_executor_runs_skill_and_surfaces_envelopes():
    from hydra_core.squad_node import _via_claude_skill
    from hydra_core.schemas import CreativeBrief, Constraints
    packs = __import__("hydra_core.squad_loader", fromlist=["discover_squads"]).discover_squads(
        __import__("pathlib").Path(__file__).resolve().parents[1])
    pack = packs["garland"]
    inbound = CreativeBrief(
        workflow_id=uuid4(), origin_squad="hydra", campaign_id=uuid4(),
        objective="teaser", target_audience="devs", constraints=Constraints())
    hosted = {
        "status": "done", "summary": "creative produced",
        "emitted_envelopes": [{
            "type": "CREATIVE_BRIEF", "campaign_id": str(uuid4()),
            "objective": "render shot", "target_audience": "devs",
            "constraints": {},
        }],
    }
    disp = _HostSkillDispatcher({
        ("rlm_creative", "rlm.command.list"): {"commands": [{"name": "rlm-team"}]},
        ("rlm_creative", "rlm.output.write"): {"relative": "RLM/output/draft/t.md"},
    }, hosted)

    result = _via_claude_skill(HydraState(root_goal="x"), pack, inbound, disp)

    # Host executor ran (not host_pickup); real envelopes surfaced.
    assert disp.host_calls and disp.host_calls[0].startswith("skill:")
    assert result.host_pickup_pending is False
    assert result.status == "done"
    # DecisionRecord + the emitted CREATIVE_BRIEF.
    assert any(getattr(e, "type", None) == "CREATIVE_BRIEF" for e in result.envelopes)
