"""E2-31 — claude-skill packs resolve to a spawnable lead agent + real cwd.

`_resolve_pack_lead_agent` used to hand the host the bare squad.yaml slug
(e.g. `support-supervisor`) with `cwd=<Hydra>/squads/customer-support`. Neither
is real: the agent lives in the pack's own checkout under its frontmatter name,
and the squads/ dir holds only Hydra's overlay. These tests pin the fixed
contract.
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from hydra_core.cli import (
    _resolve_claude_skill_host_action,
    _resolve_pack_cwd,
    _resolve_pack_lead_agent,
)
from hydra_core.squad_loader import AgentSpec, SquadPack, discover_squads

HYDRA_ROOT = Path(__file__).resolve().parents[1]


def _make_pack_checkout(root: Path, *, with_plugin_json: bool) -> Path:
    """A fake pack checkout with a hestia agent, optionally plugin-installed."""
    checkout = root / "Xenia"
    (checkout / ".claude" / "agents").mkdir(parents=True)
    (checkout / ".claude" / "agents" / "hestia.md").write_text(
        "---\nname: hestia\ndescription: \"Support supervisor.\"\nmodel: opus\n"
        "tools:\n  - Read\n---\n\nBody.\n",
        encoding="utf-8",
    )
    if with_plugin_json:
        (checkout / ".claude-plugin").mkdir()
        (checkout / ".claude-plugin" / "plugin.json").write_text(
            json.dumps({"name": "xenia", "version": "1.0.0"}), encoding="utf-8")
    return checkout


def _fake_pack(checkout: Path) -> SquadPack:
    return SquadPack(
        slug="customer-support",
        name="Customer Support",
        description="fake pack",
        source_pack=str(checkout),
        entrypoint="claude-skill",
        agents=(
            AgentSpec(slug="support-supervisor", role="lead",
                      authority="gatekeeper",
                      agent_file=".claude/agents/hestia.md"),
            AgentSpec(slug="intake-router", role="router", authority="execute",
                      agent_file=".claude/agents/iris.md"),
        ),
        invoke={"command_hint": "/support-ticket"},
    )


class TestPluginInstalledPack:
    def test_lead_agent_is_plugin_qualified_frontmatter_name(self, tmp_path):
        pack = _fake_pack(_make_pack_checkout(tmp_path, with_plugin_json=True))
        assert _resolve_pack_lead_agent(pack) == "xenia:hestia"

    def test_host_action_carries_lead_agent_file_and_no_skill_shim(self, tmp_path):
        checkout = _make_pack_checkout(tmp_path, with_plugin_json=True)
        action = _resolve_claude_skill_host_action(_fake_pack(checkout))
        assert action["agent_type"] == "xenia:hestia"
        assert Path(action["lead_agent_file"]) == checkout / ".claude" / "agents" / "hestia.md"
        assert Path(action["lead_agent_file"]).is_file()
        # A plugin-loadable agent is spawned directly; no command indirection.
        assert "skill" not in action


class TestNonPluginPack:
    def test_falls_back_to_general_purpose_with_skill_and_tool_scope(self, tmp_path):
        checkout = _make_pack_checkout(tmp_path, with_plugin_json=False)
        action = _resolve_claude_skill_host_action(_fake_pack(checkout))
        assert action["agent_type"] == "general-purpose"
        assert action["skill"] == "/support-ticket"
        # customer-support's MCP shim prefix (hydra_core.squad_node._SKILL_PACK_SHIMS)
        assert action["tool_scope"] == "xenia"
        assert Path(action["lead_agent_file"]).is_file()

    def test_lead_agent_never_returns_the_bare_squad_slug(self, tmp_path):
        pack = _fake_pack(_make_pack_checkout(tmp_path, with_plugin_json=False))
        assert _resolve_pack_lead_agent(pack) != "support-supervisor"


class TestPackCwd:
    def test_cwd_is_the_pack_checkout_not_the_squads_overlay(self, tmp_path):
        checkout = _make_pack_checkout(tmp_path, with_plugin_json=True)
        cwd = Path(_resolve_pack_cwd(_fake_pack(checkout), HYDRA_ROOT))
        assert cwd == checkout.resolve()
        assert "squads" not in cwd.parts

    def test_unresolvable_checkout_falls_back_to_squads_dir(self, tmp_path):
        """A pack whose source_pack cannot be resolved still gets a usable cwd."""
        pack = SquadPack(
            slug="customer-support", name="cs", description="",
            source_pack="https://github.com/lebobo88/NoSuchRepoHere",
            entrypoint="claude-skill",
        )
        cwd = Path(_resolve_pack_cwd(pack, HYDRA_ROOT))
        assert cwd.is_dir()


class TestRealCustomerSupportPack:
    """Against the real registry: the resolved agent file must actually exist."""

    def test_customer_support_resolves_to_an_existing_agent_file(self):
        pack = discover_squads(HYDRA_ROOT).get("customer-support")
        if pack is None or pack.entrypoint != "claude-skill":
            pytest.skip("customer-support claude-skill pack not registered")
        action = _resolve_claude_skill_host_action(pack)
        if "lead_agent_file" not in action:
            pytest.skip("Xenia checkout not present in this environment")
        agent_file = Path(action["lead_agent_file"])
        assert agent_file.is_file(), f"lead agent file missing: {agent_file}"
        cwd = Path(_resolve_pack_cwd(pack, HYDRA_ROOT))
        assert agent_file.is_relative_to(cwd)
        if action["agent_type"] != "general-purpose":
            # Plugin-qualified: the suffix must be the file's frontmatter name.
            from hydra_core.cli import _agent_frontmatter_name
            expected = _agent_frontmatter_name(agent_file) or agent_file.stem
            assert action["agent_type"].endswith(f":{expected}")
        else:
            assert action["skill"] and action["tool_scope"]
