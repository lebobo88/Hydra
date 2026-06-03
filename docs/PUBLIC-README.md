# Hydra

> *Many heads. One heart.*

Hydra is an AI-agent orchestrator. It routes work across specialized agent
crews — strategy, engineering, marketing — under a single supervisor with
persistent memory, governance gates, and a constitution the user authors
once and the system never edits.

This is the public-facing README. For the philosophical charter that
shapes the architecture, see `docs/MANIFESTO.md`. Both are true.

## What Hydra does

Hydra is a LangGraph supervisor + Claude Code plugin that:

- Routes a user goal to one or more specialized **squads** (each squad is
  a self-contained agent pack with its own roster, tools, and entrypoint).
- Coordinates them through typed message envelopes
  (`CSuiteDecisionPacket`, `PRD`, `ArchRFC`, `DevTask`, `CreativeBrief`,
  `ShotList`, `AssetJob`, `HITLRequest`, `DecisionRecord`, `Handoff`).
- Pauses for human approval at HITL gates (`/hydra:approve`,
  `/hydra:resume`).
- Remembers every decision in a three-tier memory fabric (ephemeral /
  episodic / semantic).
- Synthesizes a single integrated answer back to the user.

Hydra does **not** do the squads' work — it routes, governs, synthesizes.

## The three crowns

Out of the box, Hydra ships with three productized crews:

| Crown | Squad | Source pack | What it does |
|---|---|---|---|
| Executive | `executive` | [ExecutiveSuite](https://github.com/lebobo88/ExecutiveSuite) | C-suite strategic decisions, boardroom, capital allocation, M&A, crisis response |
| Forge | `engineering` | [pair-programmer](https://github.com/lebobo88/pair-programmer) | spec-driven dev: PRD → ArchRFC → DevTask → tests → review → release |
| Garland | `garland` | [RLM-Creative](https://github.com/lebobo88/RLM-Creative) | brand, copy, content, social, paid, PR, SEO, visual direction |
| Curia | `legal-compliance` | [Senate](https://github.com/lebobo88/Senate) | contracts, regulatory, privacy, IP, M&A, litigation, governance, citation verification |

Plus a **Marketing crown** sourced from [MarketBliss](https://github.com/lebobo88/MarketBliss)
(5 squads: marketing-strategy, marketing-creative, marketing-research,
marketing-production, marketing-ops) and four stub crews scaffolded for
healthcare, sales-gtm, research-ds, and customer-support.
Drop a `squads/<slug>/squad.yaml` file and it appears in the registry — no
code changes.

## TheEights — persistent memory

The Hydra's memory layer. Three tiers:

- **Episodic** — append-only SQLite log of every envelope, tool call,
  and verdict.
- **Semantic** — pluggable vector index (Chroma/Qdrant/in-memory
  fallback).
- **Procedural** — self-rewriting routing heuristics, gated by the
  constitution.

The semantic layer carries an eight-cell tag vocabulary
(Vision / Context / Triggers / Influence / Risk / Focus / Constraints /
Delight). The cells are facets, not partitions — one storage layer, eight
ways to query it. Queries like *"what did the legal head flag the last
time we considered EU expansion?"* walk back through the Risk cell to the
originating episodic record.

## Governance

Hydra has four enforcement rings, ordered from outermost in:

1. **Constitution gate** — `CONSTITUTION.md` is the immortal head. Its
   SHA-256 is the law of the session. Refusals defined in Section IV of
   the constitution are checked on every postcheck, every procedural
   update, and every venom-class invocation.
2. **Iolaus the cauterizer** — squad version pinning, deprecation gate,
   per-workflow double-spawn ledger. The "cut one head, two grow back"
   failure mode is refused at the dispatch boundary.
3. **Cerberus the venom gate** — every dual-use capability (destructive
   shell, force-push, prod deploy, payments, autonomous email, browser
   on third-party accounts) is registered in `squads/engineering/cerberus.yaml`
   and must pass the gate. Refusals are audited to the Risk cell whether
   they pass or fail.
4. **Redaction + MCP-attack defense** — PII, prompt injection, lookalike
   tools, and cross-tool exfiltration shapes are neutralized at every
   squad boundary.

## Quickstart

```bash
# Discover the squads
python -m hydra_core.immortal_head verify       # prints constitution hash + summary
python - <<'PY'
from hydra_core.squad_loader import discover_squads
for slug, pack in discover_squads().items():
    print(f"{slug}: v{pack.version}  entrypoint={pack.entrypoint}")
PY

# Run the full test suite
python -m pytest tests/ -q
```

## Use from Claude Code

`/hydra:run "should we expand to EU next quarter?"` — convenes the
relevant crown, synthesizes counsel, surfaces dissents.

`/hydra:status` — lists workflows, shows the current phase.

`/hydra:approve <workflow_id>` / `/hydra:resume <workflow_id>` — release
a paused workflow past an HITL gate.

`/hydra:add-squad <slug>` — scaffold a new squad pack.

## Architecture (one-pager)

- **Supervisor** — `hydra_core/supervisor.py`. LangGraph state machine,
  8 nodes: intake → planner → approval → dispatch → judge_per_squad →
  synthesis → judge_synthesis → postcheck. Checkpointed to
  `~/.hydra/checkpoints.db`.
- **Schemas** — `hydra_core/schemas.py`. 10 typed envelopes, validated at
  every squad boundary.
- **Squad loader** — `hydra_core/squad_loader.py`. Discovers
  `squads/<slug>/squad.yaml` zero-code.
- **Memory** — `hydra_core/memory.py` + the hydra-memory MCP server.
- **Governance** — `hydra_core/governance.py`,
  `hydra_core/immortal_head.py`, `hydra_core/venom.py`,
  `hydra_core/iolaus.py`.
- **Cathedral renderers** — `hydra_core/heads.py`,
  `hydra_core/deliberation.py`. The user-facing voice layer.

## License

(TBD)

## Where to read more

- `docs/MANIFESTO.md` — the philosophical charter (cathedral register).
- `docs/ROADMAP-MANIFESTO.md` — the six-stage ingestion roadmap.
- `docs/BRAND.md` — sigil, loglines, voice register.
- `docs/VENOM.md` — Stage 5 policy on dual-use capabilities.
- `docs/PRICING.md` — three-tier monetization model.
- `docs/TRADEMARK-CLEARANCE.md` — name clearance status.
- `ARCHITECTURE.md` — engineer-facing detailed design.
- `CONTRIBUTING-SQUADS.md` — how to add a squad pack.
