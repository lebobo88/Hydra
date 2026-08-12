---
name: claude-native-review
description: "Read-only review of Hydra's Claude Code configuration, prompts, hooks, and MCP integration. Use when auditing Claude-native adoption or preparing a governed modernization plan."
context: fork
agent: Explore
background: false
allowed-tools: Read Glob Grep
---

# Claude-native Hydra review

Review `$ARGUMENTS` without editing files.

<scope>
Inspect `CLAUDE.md`, `.claude/settings.json`, `.claude/agents/`,
`.claude/commands/`, `.claude/skills/`, `.claude/hooks/`, and relevant tests.
Compare them with current official Anthropic and Claude Code documentation.
</scope>

<invariants>
Never propose changing `CONSTITUTION.md`, bypassing HITL, transferring raw
blobs between squads, or replacing the pair-programmer engineering stage
protocol with direct subagents, agent teams, worktrees, or local edits.
</invariants>

<method>
1. Identify Claude-native capabilities that are already in use, missing, or
   unsuitable because of Hydra's governance model.
2. Support every finding with exact file paths and line references.
3. Distinguish stable capabilities from experimental ones.
4. Produce P0/P1/P2 findings, an implementation plan, and testable acceptance
   criteria. State uncertainty explicitly rather than guessing.
</method>

<output_contract>
Return a concise Markdown report with sections: `Evidence`, `Findings`,
`Recommended changes`, `Acceptance tests`, and `Do not adopt`.
</output_contract>
