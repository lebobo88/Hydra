"""Governance invariants flagged by the adversarial review's gap analysis
(Senate legal pre-dispatch HITL; legal placeholder never finalizes as a real
answer; Xenia WS-AUTH tool privilege). Hydra-side, deterministic."""
from __future__ import annotations

from pathlib import Path
from uuid import uuid4

from hydra_core.squad_loader import discover_squads
from hydra_core.squad_node import _via_claude_skill
from hydra_core.schemas import CSuiteDecisionPacket
from hydra_core.state import HydraState

HYDRA_ROOT = Path(__file__).resolve().parents[1]


class _FakeDispatcher:
    """call_mcp + the headless skill stubs, but NO run_host_agent (so the skill
    route degrades to host_pickup — the executor seam is absent)."""

    def __init__(self, scripted: dict[tuple[str, str], dict] | None = None):
        self.calls: list[tuple[str, str, dict]] = []
        self._scripted = scripted or {}

    def call_mcp(self, server, tool, args, **_kw):
        self.calls.append((server, tool, args))
        if (server, tool) in self._scripted:
            return {"status": "done", "tool": tool, "result": self._scripted[(server, tool)]}
        return {"status": "done", "tool": tool, "result": {}}

    def spawn_subprocess(self, cmd, env=None):
        return {"status": "stub"}

    def emit_claude_prompt(self, prompt, agent=None):
        return {"status": "host_pickup_required"}

    def invoke_claude_skill(self, skill, args):
        return {"status": "host_pickup_required", "summary": "deferred to host"}


def _packs():
    return discover_squads(HYDRA_ROOT)


# --- Senate: legal-compliance has a pre-dispatch HITL gate ------------------- #
def test_legal_compliance_declares_hitl_gates():
    """The pre-dispatch HITL fires because the legal-compliance pack declares
    hitl_required gates (the Tribune's Veto at the routing boundary). Without a
    single hitl_required gate, `_squad_has_hitl_gate` would never trip and a
    legal answer could dispatch with no human in the loop."""
    pack = _packs()["legal-compliance"]
    hitl_gates = [g for g in pack.gates if getattr(g, "hitl_required", False)]
    assert hitl_gates, "legal-compliance MUST declare >=1 hitl_required gate"
    # The always-on compliance + citation integrity gates are among them.
    rubric_ids = {g.rubric_id for g in hitl_gates}
    assert any("citation-integrity" in r or "compliance-coverage" in r for r in rubric_ids)


# --- Senate: a legal placeholder is never a finalized real answer ------------ #
def test_legal_placeholder_is_host_pickup_not_a_real_answer():
    """A headless legal-compliance dispatch (no host executor) returns a
    host_pickup placeholder marked host_pickup_pending — the judge plane skips
    host_pickup_pending results, so a contentless placeholder can NEVER be scored
    or finalized as a real legal answer."""
    pack = _packs()["legal-compliance"]
    inbound = CSuiteDecisionPacket(
        workflow_id=uuid4(), origin_squad="hydra", origin="BOARDROOM",
        objective="Is this data-processing clause GDPR Art. 28 compliant?")
    disp = _FakeDispatcher({("senate", "senate.command.list"): {"commands": []}})

    result = _via_claude_skill(HydraState(root_goal="x"), pack, inbound, disp)

    assert result.host_pickup_pending is True
    # F11: 'host_pickup_required' is normalised to 'deferred_to_host' so
    # governance can block workflows whose pack work never executed.
    assert result.status == "deferred_to_host"


# --- Xenia: WS-AUTH ticket bridge is write-privileged + approval-gated -------- #
def test_customer_support_ticket_bridge_is_write_privileged():
    """The customer-support → xenia-tickets bridge must be declared WRITE
    privilege (deny-by-default on monetary/irreversible actions; server-side
    WS-AUTH enforces capability). A read/missing privilege would silently drop
    the approval contract."""
    pack = _packs()["customer-support"]
    bridge = next((t for t in pack.tools if getattr(t, "mcp_server", None) == "xenia-tickets"), None)
    assert bridge is not None, "customer-support MUST bind the xenia-tickets bridge"
    assert getattr(bridge, "privilege", None) == "write"
