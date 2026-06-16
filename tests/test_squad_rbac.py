"""RBAC regression tests for the REAL engineering squad pack.

The existing RBAC coverage in ``tests/test_fleet.py`` uses a hand-rolled
``_FakeRbacDispatcher`` with a synthetic pack. Nothing loaded the *actual*
``squads/engineering/squad.yaml`` through the *real*
``MCPStdioDispatcher._check_tool_rbac`` — so the day-one bug where the squad
declared dotted tool names (``pp.harness.start_run``) that match neither RBAC
branch went undetected until the first live ``hydra run --live`` dispatch.

These tests load the real pack via ``discover_squads`` and assert every tool the
live drive loop in ``squad_node._via_mcp`` calls authorizes for the engineering
squad, while an undeclared tool and the historical dotted form are rejected.

``_check_tool_rbac`` is a pure method (no MCP connection needed), so we
instantiate the dispatcher and call it directly.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from hydra_core.dispatcher import MCPStdioDispatcher
from hydra_core.squad_loader import discover_squads

REPO_ROOT = Path(__file__).resolve().parents[1]

# (server, tool) pairs the live drive loop in squad_node._via_mcp invokes with
# squad_id="engineering". Every one of these MUST authorize or the loop is
# RBAC-rejected mid-run.
LOOP_TOOLS = [
    ("pp_harness", "start_run"),
    ("pp_harness", "start_stage"),
    ("pp_harness", "archive_artifact"),
    ("pp_harness", "record_attempt"),
    ("pp_harness", "record_verdict"),
    ("pp_harness", "finalize_stage"),
    ("pp_harness", "finalize_run"),
    ("pp_codex", "generate"),
    ("pp_gemini", "critique"),
]


@pytest.fixture()
def dispatcher_with_real_packs() -> MCPStdioDispatcher:
    """A real dispatcher with the real discovered squad packs injected.

    No MCP servers are contacted — only the pure RBAC method is exercised.
    """
    packs = discover_squads(REPO_ROOT)
    assert "engineering" in packs, "engineering squad must be discoverable"
    disp = MCPStdioDispatcher(project_root=REPO_ROOT)
    disp.set_squad_packs(packs)
    return disp


@pytest.mark.parametrize("server,tool", LOOP_TOOLS)
def test_engineering_authorizes_every_drive_loop_tool(
    dispatcher_with_real_packs: MCPStdioDispatcher, server: str, tool: str
) -> None:
    """Each tool the live drive loop calls must authorize (return None)."""
    rejection = dispatcher_with_real_packs._check_tool_rbac(
        server, tool, squad_id="engineering"
    )
    assert rejection is None, (
        f"engineering should be authorized for {server}.{tool} but RBAC "
        f"rejected it: {rejection}"
    )


def test_engineering_rejects_undeclared_tool(
    dispatcher_with_real_packs: MCPStdioDispatcher,
) -> None:
    """A tool the squad never declares (e.g. force_unlock) must be rejected."""
    rejection = dispatcher_with_real_packs._check_tool_rbac(
        "pp_harness", "force_unlock", squad_id="engineering"
    )
    assert rejection is not None
    assert "force_unlock" in rejection


def test_historical_dotted_form_would_be_rejected() -> None:
    """Lock in the regression: a pack declaring the historical dotted name
    ``pp.harness.start_run`` (dot in the server segment) must NOT authorize a
    live ``pp_harness/start_run`` call. This is the exact day-one bug."""
    from hydra_core.squad_loader import SquadPack, ToolSpec

    broken = SquadPack(
        slug="engineering",
        name="engineering",
        description="broken (historical dotted form)",
        entrypoint="mcp",
        agents=(),
        tools=(ToolSpec(name="pp.harness.start_run", mcp_server="pp_harness"),),
    )
    disp = MCPStdioDispatcher(project_root=REPO_ROOT)
    disp.set_squad_packs({"engineering": broken})
    rejection = disp._check_tool_rbac("pp_harness", "start_run", squad_id="engineering")
    assert rejection is not None, (
        "the historical dotted 'pp.harness.start_run' must be rejected — it "
        "matches neither tool_key 'pp_harness.start_run' nor bare 'start_run'"
    )


def test_exact_dotted_key_form_authorizes() -> None:
    """Sanity check on the RBAC rule itself: a CORRECTLY formed dotted key
    equal to f'{server}.{tool}' (underscore server) DOES authorize via branch
    (a) — proving bare names are a choice, not the only working form."""
    from hydra_core.squad_loader import SquadPack, ToolSpec

    pack = SquadPack(
        slug="engineering",
        name="engineering",
        description="exact dotted key form",
        entrypoint="mcp",
        agents=(),
        tools=(ToolSpec(name="pp_harness.start_run", mcp_server="pp_harness"),),
    )
    disp = MCPStdioDispatcher(project_root=REPO_ROOT)
    disp.set_squad_packs({"engineering": pack})
    assert disp._check_tool_rbac("pp_harness", "start_run", squad_id="engineering") is None
