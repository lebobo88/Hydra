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

# (server, tool) pairs the live engineering path in squad_node._via_mcp invokes
# with squad_id="engineering". Every one of these MUST authorize or the
# bootstrap/drive path is RBAC-rejected mid-run.
LOOP_TOOLS = [
    ("pp_harness", "start_run"),
    ("pp_harness", "ensure_agents_md"),
    ("pp_harness", "start_stage"),
    ("pp_harness", "archive_artifact"),
    ("pp_harness", "record_attempt"),
    ("pp_harness", "record_verdict"),
    ("pp_harness", "finalize_stage"),
    ("pp_harness", "finalize_run"),
    ("pp_harness", "record_smoke_status"),
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


# ── RA-3: claude-skill shim RBAC auto-authorization ──────────────────────────

# Shim tool pairs for each claude-skill squad that appears in _SKILL_PACK_SHIMS.
# Parametrize against the squads present in THIS worktree's squads/ directory
# (marketing-* are symlinks from MarketBliss; only test what discover_squads returns).
_CLAUDE_SKILL_SHIM_CASES: list[tuple[str, str, str]] = [
    # (squad_slug, shim_server, shim_prefix)
    ("garland",          "rlm_creative", "rlm"),
    ("legal-compliance", "senate",       "senate"),
    ("rlm-gaming",       "rlm_gaming",   "rlmgaming"),
    ("customer-support", "xenia",        "xenia"),
]

_SHIM_TOOL_PAIRS = [
    (slug, server, prefix, f"{prefix}.command.list")
    for slug, server, prefix in _CLAUDE_SKILL_SHIM_CASES
] + [
    (slug, server, prefix, f"{prefix}.output.write")
    for slug, server, prefix in _CLAUDE_SKILL_SHIM_CASES
]


@pytest.mark.parametrize(
    "slug,server,prefix,tool",
    _SHIM_TOOL_PAIRS,
    ids=[f"{slug}/{tool}" for slug, server, prefix, tool in _SHIM_TOOL_PAIRS],
)
def test_claude_skill_own_shim_pair_is_authorized(
    slug: str, server: str, prefix: str, tool: str,
) -> None:
    """RA-3 (a): every claude-skill squad's own shim tool pair must authorize
    against the real dispatcher — verified against _SKILL_PACK_SHIMS entries.
    No MCP connection is made; only the pure _check_tool_rbac method runs.
    """
    packs = discover_squads(REPO_ROOT)
    # If the squad isn't in this worktree's registry, skip rather than fail
    # (marketing-* are MarketBliss symlinks absent from this checkout).
    if slug not in packs:
        pytest.skip(f"{slug!r} not in discover_squads — likely a symlink squad")

    pack = packs[slug]
    assert pack.entrypoint == "claude-skill", (
        f"{slug!r} must be a claude-skill squad but is {pack.entrypoint!r}"
    )
    disp = MCPStdioDispatcher(project_root=REPO_ROOT)
    disp.set_squad_packs(packs)
    rejection = disp._check_tool_rbac(server, tool, squad_id=slug)
    assert rejection is None, (
        f"{slug!r} should be auto-authorized for its own shim tool {server}/{tool} "
        f"but RBAC rejected it: {rejection}"
    )


def test_cross_squad_shim_call_is_denied() -> None:
    """RA-3 (b): a squad must NOT be authorized to call another squad's shim
    tools. customer-support owns xenia.* tools; it must be denied for
    mb.output.write (MarketBliss shim) on marketbliss server."""
    from hydra_core.squad_loader import SquadPack

    # Build a minimal customer-support pack (no declared tools → only shim pair
    # auto-authorization applies, which is scoped to the squad's OWN server).
    cs_pack = SquadPack(
        slug="customer-support",
        name="customer-support",
        description="test — cross-squad shim denial",
        entrypoint="claude-skill",
        agents=(),
        tools=(),
    )
    disp = MCPStdioDispatcher(project_root=REPO_ROOT)
    disp.set_squad_packs({"customer-support": cs_pack})

    # customer-support's shim is xenia / xenia.*, NOT marketbliss / mb.*
    rejection = disp._check_tool_rbac("marketbliss", "mb.output.write",
                                      squad_id="customer-support")
    assert rejection is not None, (
        "customer-support must NOT be authorized for mb.output.write on marketbliss "
        "(that is a MarketBliss shim, not the Xenia shim)"
    )
    assert "not authorized" in rejection.lower() or "RBAC" in rejection


def test_unknown_slug_resolve_shim_returns_none_and_via_claude_skill_surfaces() -> None:
    """RA-3 (c): _resolve_skill_shim returns None for unknown slug (fail-CLOSED),
    and _via_claude_skill surfaces a clear error SquadResult instead of routing
    to the wrong pack store."""
    import logging
    from hydra_core.squad_node import _resolve_skill_shim, _via_claude_skill, SquadResult
    from hydra_core.squad_loader import SquadPack
    from hydra_core.state import HydraState
    from hydra_core.schemas import CSuiteDecisionPacket
    from uuid import uuid4

    # (c-i) _resolve_skill_shim returns None for unknown slug
    result_shim = _resolve_skill_shim("totally-unknown-slug-xyz")
    assert result_shim is None, (
        "_resolve_skill_shim must return None for unknown slugs (fail-CLOSED)"
    )

    # (c-ii) _via_claude_skill surfaces with a clear error rationale
    unknown_pack = SquadPack(
        slug="totally-unknown-slug-xyz",
        name="unknown",
        description="test — unknown shim slug",
        entrypoint="claude-skill",
        agents=(),
        tools=(),
    )
    inbound = CSuiteDecisionPacket(
        workflow_id=uuid4(), origin_squad="hydra",
        origin="BOARDROOM", objective="test",
    )
    state = HydraState(root_goal="test unknown slug surface")

    class _NullDispatcher:
        def call_mcp(self, *a, **kw):
            return {"status": "done"}
        def invoke_claude_skill(self, *a, **kw):
            return {"status": "done"}

    sr = _via_claude_skill(state, unknown_pack, inbound, _NullDispatcher())
    assert isinstance(sr, SquadResult)
    assert sr.status == "surfaced", (
        f"Expected status='surfaced' for unknown shim slug, got {sr.status!r}"
    )
    assert "no entry in _SKILL_PACK_SHIMS" in sr.rationale or "_SKILL_PACK_SHIMS" in sr.rationale, (
        f"rationale should mention missing shim: {sr.rationale!r}"
    )
