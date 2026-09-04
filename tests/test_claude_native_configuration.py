"""Regression coverage for Hydra's Claude Code-native configuration."""
from __future__ import annotations

import json
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
PLUGIN_ROOT = ROOT / "plugins" / "hydra"


def test_plugin_registers_all_hydra_guard_hooks_without_project_duplicates() -> None:
    settings = json.loads((ROOT / ".claude" / "settings.json").read_text(encoding="utf-8"))
    hooks = json.loads((PLUGIN_ROOT / "hooks" / "hooks.json").read_text(encoding="utf-8"))["hooks"]

    assert settings["env"]["HYDRA_ENFORCE_ROUTING"] == "1"
    assert settings["env"]["HYDRA_DISABLE_CLAUDE_ENGINEER"] == "1"
    assert "hooks" not in settings
    # Tool search is already on by default. Do not force it through the Hydra
    # gateway because a proxy that does not forward tool_reference blocks must
    # be allowed to fall back to eager loading.
    assert "ENABLE_TOOL_SEARCH" not in settings["env"]
    assert "hydra-session-contract.ps1" in hooks["SessionStart"][0]["hooks"][1]["command"]
    assert "hydra-route-directive.ps1" in hooks["UserPromptSubmit"][0]["hooks"][1]["command"]

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
    engineer = (PLUGIN_ROOT / "agents" / "engineer.md").read_text(encoding="utf-8")
    same_vendor_judge = (PLUGIN_ROOT / "agents" / "judge-same-vendor.md").read_text(
        encoding="utf-8"
    )
    cross_vendor_judge = (PLUGIN_ROOT / "agents" / "judge-cross-vendor.md").read_text(
        encoding="utf-8"
    )

    assert "model: sonnet" in engineer
    assert "model: sonnet" in same_vendor_judge
    assert "model: sonnet" in cross_vendor_judge
    assert "<untrusted_content>" in engineer
    assert "<evidence_policy>" in same_vendor_judge


def test_claude_native_review_skill_is_read_only_and_governed() -> None:
    skill = (PLUGIN_ROOT / "skills" / "claude-native-review" / "SKILL.md").read_text(
        encoding="utf-8"
    )

    assert "context: fork" in skill
    assert "agent: Explore" in skill
    assert "allowed-tools: Read Glob Grep" in skill
    assert "Never propose changing `CONSTITUTION.md`" in skill
    assert "pair-programmer engineering stage" in skill


def test_router_prompt_matches_the_active_squad_registry() -> None:
    router = (PLUGIN_ROOT / "agents" / "hydra-router.md").read_text(encoding="utf-8")

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
    skills = PLUGIN_ROOT / "skills"
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
    agents = PLUGIN_ROOT / "agents"
    for name in ("hydra-router.md", "hydra-planner.md", "hydra-synthesizer.md"):
        body = (agents / name).read_text(encoding="utf-8")
        assert "<authority_boundary>" in body or "<evidence_policy>" in body
    assert "<output_contract>" in (agents / "hydra-planner.md").read_text(encoding="utf-8")


def test_native_operator_commands_delegate_to_hydra_authority() -> None:
    skills = PLUGIN_ROOT / "skills"
    status = (skills / "status" / "SKILL.md").read_text(encoding="utf-8")
    approve = (skills / "approve" / "SKILL.md").read_text(encoding="utf-8")
    resume = (skills / "resume" / "SKILL.md").read_text(encoding="utf-8")

    assert "<authority_boundary>" in status
    assert "hydra_core.cli status" in status
    assert "hydra.workflow.resume" in approve
    assert "Do not directly edit `HydraState`" in approve
    assert "hydra.workflow.resume" in resume
    assert "Do\nnot patch checkpoint state" in resume


def test_engineer_uses_hydra_provided_worktree_not_host_isolation() -> None:
    engineer = (PLUGIN_ROOT / "agents" / "engineer.md").read_text(encoding="utf-8")

    assert "host action is authoritative" in engineer
    assert "do not create, select, or request a separate" in engineer
    assert "isolation: worktree" not in engineer


def test_plugin_and_project_contexts_have_documented_activation_boundary() -> None:
    manifest = json.loads((PLUGIN_ROOT / ".claude-plugin" / "plugin.json").read_text(encoding="utf-8"))
    readme = (ROOT / "README.md").read_text(encoding="utf-8")

    assert manifest["version"] == "0.1.7"
    assert manifest["author"]["name"] == "rob"
    assert "hooks" not in manifest
    assert len(manifest["agents"]) == 11
    assert "### Activation contexts" in readme
    assert "consumer projects" in readme


def test_plugin_manifest_references_existing_scoped_agents() -> None:
    manifest = json.loads((PLUGIN_ROOT / ".claude-plugin" / "plugin.json").read_text(encoding="utf-8"))

    for relative_path in manifest["agents"]:
        agent_path = PLUGIN_ROOT / relative_path.removeprefix("./")
        body = agent_path.read_text(encoding="utf-8")
        assert agent_path.is_file()
        assert body.startswith("---\n")
        assert "\nname: " in body
        assert "\ndescription: " in body


def test_plugin_release_is_recorded_in_the_changelog() -> None:
    manifest = json.loads((PLUGIN_ROOT / ".claude-plugin" / "plugin.json").read_text(encoding="utf-8"))
    changelog = (ROOT / "CHANGELOG.md").read_text(encoding="utf-8")

    assert f"plugin {manifest['version']}" in changelog
    assert "MCP\n  gateway registration remains machine-local" in changelog


def test_operator_skills_use_canonical_namespace_without_project_duplicates() -> None:
    expected = {
        "add-squad", "approve", "budget", "campaign", "drive",
        "replay", "resume", "run", "squads", "status",
    }
    actual = {path.parent.name for path in (PLUGIN_ROOT / "skills").glob("*/SKILL.md")}
    assert expected <= actual

    # Git does not track empty directories, so a fresh checkout of `.claude/`
    # legitimately has NO `agents`/`commands`/`skills`/`hooks` directory at
    # all after 41993f2 moved these operator artifacts into the plugin
    # (plugins/hydra/). Absence satisfies the invariant at least as strongly
    # as emptiness does -- do not "restore" these as empty dirs / .gitkeep,
    # that would silently reintroduce the project-scope duplicates this
    # assertion exists to forbid.
    offenders: dict[str, list[str]] = {}
    for directory in ("agents", "commands", "skills", "hooks"):
        path = ROOT / ".claude" / directory
        if not path.exists():
            continue
        entries = sorted(entry.name for entry in path.iterdir())
        if entries:
            offenders[directory] = entries
    assert not offenders, (
        "Project-scope .claude/ directories must not duplicate the plugin "
        f"namespace, but found entries: {offenders}"
    )
    for skill in expected:
        body = (PLUGIN_ROOT / "skills" / skill / "SKILL.md").read_text(encoding="utf-8")
        assert "disable-model-invocation: true" in body


@pytest.mark.parametrize("agent_filename", ["hydra.md", "hydra-plaza.md"])
def test_hydra_persona_agent_adds_voice_without_duplicating_the_contract(
    agent_filename: str,
) -> None:
    body = (PLUGIN_ROOT / "agents" / agent_filename).read_text(encoding="utf-8")

    assert "Hard Rule" not in body
    assert "/pp:" not in body
    # No routing table (the squad registry table lives in AGENTS.md, not here).
    assert "| Slug | Source pack | Entrypoint |" not in body

    # Frontmatter must omit tools:/model:/permissionMode: -- each omission is
    # deliberate: `["*"]` is not a documented file form; a `model:` would
    # force that model on every session in every project with the plugin
    # enabled, overriding the operator's --model; permissionMode is ignored
    # for plugin agents.
    frontmatter = body.split("---\n", 2)[1]
    assert "tools:" not in frontmatter
    assert "model:" not in frontmatter
    assert "permissionMode:" not in frontmatter


def test_plugin_settings_scope_agent_to_plugin_namespace() -> None:
    settings = json.loads((PLUGIN_ROOT / "settings.json").read_text(encoding="utf-8"))

    # Scoped ("hydra:hydra"), not a bare "hydra" -- a bare name would not
    # resolve for a plugin agent.
    assert settings == {"agent": "hydra:hydra"}
