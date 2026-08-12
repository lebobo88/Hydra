"""Regression coverage for Hydra's Claude Code-native configuration."""
from __future__ import annotations

import json
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def test_claude_settings_registers_all_hydra_guard_hooks() -> None:
    settings = json.loads((ROOT / ".claude" / "settings.json").read_text(encoding="utf-8"))
    hooks = settings["hooks"]

    assert settings["env"]["HYDRA_ENFORCE_ROUTING"] == "1"
    assert settings["env"]["HYDRA_DISABLE_CLAUDE_ENGINEER"] == "1"
    # Tool search is already on by default. Do not force it through the Hydra
    # gateway because a proxy that does not forward tool_reference blocks must
    # be allowed to fall back to eager loading.
    assert "ENABLE_TOOL_SEARCH" not in settings["env"]
    assert "hydra-session-contract.ps1" in hooks["SessionStart"][0]["hooks"][0]["command"]
    assert "hydra-route-directive.ps1" in hooks["UserPromptSubmit"][0]["hooks"][0]["command"]

    pretool = {entry["matcher"]: entry["hooks"][0]["command"] for entry in hooks["PreToolUse"]}
    assert "hydra-block-direct-write.ps1" in pretool["Write|Edit|NotebookEdit"]
    assert "hydra-block-bash-writes.ps1" in pretool["Bash"]
    assert "hydra-block-direct-pp.ps1" in pretool["Skill"]


def test_claude_contract_preserves_governance_and_untrusted_data_boundary() -> None:
    contract = (ROOT / "CLAUDE.md").read_text(encoding="utf-8")

    assert "@AGENTS.md" in contract
    assert "Treat text from tools" in contract
    assert "validated, redacted envelopes" in contract
    assert "MemoryRef" in contract
    assert ".claude/rules/" in contract


def test_attended_agents_use_current_sonnet_alias_and_evidence_policy() -> None:
    engineer = (ROOT / ".claude" / "agents" / "engineer.md").read_text(encoding="utf-8")
    same_vendor_judge = (ROOT / ".claude" / "agents" / "judge-same-vendor.md").read_text(
        encoding="utf-8"
    )
    cross_vendor_judge = (ROOT / ".claude" / "agents" / "judge-cross-vendor.md").read_text(
        encoding="utf-8"
    )

    assert "model: sonnet" in engineer
    assert "model: sonnet" in same_vendor_judge
    assert "model: sonnet" in cross_vendor_judge
    assert "<untrusted_content>" in engineer
    assert "<evidence_policy>" in same_vendor_judge


def test_claude_native_review_skill_is_read_only_and_governed() -> None:
    skill = (ROOT / ".claude" / "skills" / "claude-native-review" / "SKILL.md").read_text(
        encoding="utf-8"
    )

    assert "context: fork" in skill
    assert "agent: Explore" in skill
    assert "allowed-tools: Read Glob Grep" in skill
    assert "Never propose changing `CONSTITUTION.md`" in skill
    assert "pair-programmer engineering stage" in skill


def test_router_prompt_matches_the_active_squad_registry() -> None:
    router = (ROOT / ".claude" / "agents" / "hydra-router.md").read_text(encoding="utf-8")

    assert "→ `garland`" in router
    assert "`legal-compliance` (stub" not in router
    assert "`customer-support` (stub" not in router


def test_scoped_rules_preserve_hydra_authority_and_content_boundaries() -> None:
    rules = ROOT / ".claude" / "rules"
    governance = (rules / "governance.md").read_text(encoding="utf-8")
    engineering = (rules / "engineering.md").read_text(encoding="utf-8")
    external = (rules / "external-content.md").read_text(encoding="utf-8")

    assert "Hydra, not Claude Code, remains authoritative" in governance
    assert "MemoryRef" in governance
    assert "pair-programmer harness" in engineering
    assert "Treat all third-party document text" in external
    assert "Curia" in external


def test_native_host_skills_are_read_only_and_do_not_replace_hydra() -> None:
    skills = ROOT / ".claude" / "skills"
    recovery = (skills / "workflow-recovery" / "SKILL.md").read_text(encoding="utf-8")
    evidence = (skills / "evidence-citation-review" / "SKILL.md").read_text(encoding="utf-8")
    legal = (skills / "legal-summary-intake" / "SKILL.md").read_text(encoding="utf-8")

    for skill in (recovery, evidence, legal):
        assert "context: fork" in skill
        assert "agent: Explore" in skill
        assert "background: false" in skill
    assert "never approves, resumes" in recovery
    assert "not a substitute for a rubric verdict" in evidence
    assert "do not replace the legal-compliance squad" in legal


def test_host_agents_have_structured_authority_and_evidence_contracts() -> None:
    agents = ROOT / ".claude" / "agents"
    for name in ("hydra-router.md", "hydra-planner.md", "hydra-synthesizer.md"):
        body = (agents / name).read_text(encoding="utf-8")
        assert "<authority_boundary>" in body or "<evidence_policy>" in body
    assert "<output_contract>" in (agents / "hydra-planner.md").read_text(encoding="utf-8")


def test_native_operator_commands_delegate_to_hydra_authority() -> None:
    commands = ROOT / ".claude" / "commands"
    status = (commands / "hydra-status.md").read_text(encoding="utf-8")
    approve = (commands / "hydra-approve.md").read_text(encoding="utf-8")
    resume = (commands / "hydra-resume.md").read_text(encoding="utf-8")

    assert "<authority_boundary>" in status
    assert "hydra_core.cli status" in status
    assert "hydra.workflow.resume" in approve
    assert "Do not directly edit `HydraState`" in approve
    assert "hydra.workflow.resume" in resume
    assert "Do\nnot patch checkpoint state" in resume


def test_engineer_uses_hydra_provided_worktree_not_host_isolation() -> None:
    engineer = (ROOT / ".claude" / "agents" / "engineer.md").read_text(encoding="utf-8")

    assert "host action is authoritative" in engineer
    assert "do not create, select, or request a separate" in engineer
    assert "isolation: worktree" not in engineer


def test_plugin_and_project_contexts_have_documented_activation_boundary() -> None:
    manifest = json.loads((ROOT / ".claude-plugin" / "plugin.json").read_text(encoding="utf-8"))
    readme = (ROOT / "README.md").read_text(encoding="utf-8")

    assert manifest["version"] == "0.1.5"
    assert manifest["author"]["name"] == "rob"
    assert manifest["hooks"] == "./hooks.json"
    assert len(manifest["agents"]) == 9
    assert "### Activation contexts" in readme
    assert "consumer projects" in readme


def test_plugin_manifest_references_existing_scoped_agents() -> None:
    manifest = json.loads((ROOT / ".claude-plugin" / "plugin.json").read_text(encoding="utf-8"))

    for relative_path in manifest["agents"]:
        agent_path = ROOT / relative_path.removeprefix("./")
        body = agent_path.read_text(encoding="utf-8")
        assert agent_path.is_file()
        assert body.startswith("---\n")
        assert "\nname: " in body
        assert "\ndescription: " in body


def test_plugin_release_is_recorded_in_the_changelog() -> None:
    manifest = json.loads((ROOT / ".claude-plugin" / "plugin.json").read_text(encoding="utf-8"))
    changelog = (ROOT / "CHANGELOG.md").read_text(encoding="utf-8")

    assert f"plugin {manifest['version']}" in changelog
    assert "MCP\n  gateway registration remains machine-local" in changelog
