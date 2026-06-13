# Hydra Manifesto Ingestion Roadmap

Distilled from the approved manifesto-ingestion plan and the manifesto's own Stage 1–6 recommendations. Stages are ordered as the manifesto requires (immortal head before cauterization, cauterization before memory, memory before heads, heads before venom, venom before launch).

**Locked decisions** (from clarifying Q&A on 2026-05-19):
- Scope: full six-stage roadmap; Stage 1 shipped in initial run.
- Garland Crown: sibling project at [`RLM-Creative`](https://github.com/lebobo88/RLM-Creative). RLM-CLI-Starter contributes only `copilot-content`, `copilot-image`, `copilot-frontend-designer`, and ComfyUI integration.
- Voice: dual register — cathedral in `CONSTITUTION.md` / manifesto / `docs/MANIFESTO.md`; plaza in `squad.yaml`, schemas, code. Mythic head names attach via alias overlay.
- TheEights: generic semantic graph as storage; eight cells implemented as a tag/facet vocabulary, not a hard partition.

---

## Stage 1 — Forge the Immortal Head ✅ shipped 2026-05-19

| Artifact | Status |
|---|---|
| `CONSTITUTION.md` (repo root) | created |
| `hydra_core/immortal_head.py` | created |
| `hydra_core/governance.py` — `enforce_constitution()` | wired |
| `hydra_core/supervisor.py` — snapshot loaded at build | wired |
| `hydra_core/schemas.py` — `HITLRequest.reason` += `constitution_breach` | added |
| `AGENTS.md` — Constitution Gate hard rule | added |
| `tests/test_immortal_head.py` (14 cases) | passing |

**Verification:** `python -m hydra_core.immortal_head verify` prints hash + refusal count; `pytest tests/test_immortal_head.py` is green; full suite 30/30.

**Open item before Stage 2:** Rob reads `CONSTITUTION.md` aloud and confirms it sounds like his voice. If it doesn't, iterate the text before any other stage begins. The manifesto's Stage 1 threshold is not satisfied by mechanical correctness alone.

---

## Stage 2 — Cauterize Before You Spawn (Iolaus) ✅ shipped 2026-05-19

**Goal:** an agent can be deprecated and verifiably not respawn.

### Tickets
- `IOL-1` Create `hydra_core/iolaus.py` with `pre_dispatch(envelope)` and `post_dispatch(envelope, result)` hooks. `pre_dispatch` reads `squads/<slug>/squad.yaml#version` and rejects deprecated slugs unless `allow_archived=True`.
- `IOL-2` Create `hydra_core/version.py` with squad version-pin + deprecation registry. New exception `SquadDeprecated`.
- `IOL-3` Extend `squad_loader.py` schema with `version: str` (default `1.0.0`) and optional `deprecated_after: date`.
- `IOL-4` Update each existing `squads/*/squad.yaml` to set `version: 1.0.0` explicitly.
- `IOL-5` Add Claude Code hooks at `.claude-plugin/hooks/pre-tool-use.py` + `post-tool-use.py` to refuse re-spawn of deprecated sub-agents within a session.
- `IOL-6` `tests/test_iolaus.py` — deprecation refuses dispatch; pin survives restart; double-spawn refused.

**Threshold:** delete a squad → invoke it → see refusal in trace with the lifecycle event recorded.

---

## Stage 3 — Plant the Lemniscate (TheEights memory substrate) ✅ shipped 2026-05-19

**Goal:** Hydra recalls a decision from three sessions ago with provenance, tagged by the eight cells.

### Tickets
- `EIG-1` Create `hydra_core/eights/__init__.py` exporting `Cell = Literal["qian","kun","zhen","xun","kan","li","gen","dui"]` with docstring mapping each to Vision / Context / Triggers / Influence / Risk / Focus / Constraints / Delight.
- `EIG-2` Extend `MemoryRef` in `hydra_core/schemas.py` with `cells: list[Cell]` (default `[]`). Backwards compatible.
- `EIG-3` `hydra_core/memory.py` — add `query_by_cell(cell, window, limit)` to episodic + semantic stores.
- `EIG-4` `hydra_core/eights/classifier.py` — rules-first classifier (regex over envelope `type` + `origin_squad`); LLM fallback for ambiguous writes, with each LLM choice gated through `enforce_constitution`.
- `EIG-5` Extend `mcp_servers/hydra-memory/` with `query_eights(cell, window, limit)` and `tag_memory(ref_id, cells)`.
- `EIG-6` `hydra_core/procedural.py` — procedural-memory spine. Proposed updates queue + immortal-head veto gate (reuses Stage 1's `enforce_constitution`).
- `EIG-7` `hydra_core/reflection.py` — Ouroboros loop: every N decisions, re-read episodic outcomes, update semantic confidences, propose procedural updates.
- `EIG-8` `tests/test_eights.py` — write/read across cells, constitution gate refuses misaligned procedural update, three-session recall test.

**Threshold:** answer *"what did Themis flag the last time we considered X?"* via Gen-cell query with provenance back to the originating episodic record.

---

## Stage 4 — Grow Three Heads, Not Nine (head-naming overlay) ✅ shipped 2026-05-19

**Goal:** Executive Crown + Forge Crown wired with mythic aliases; Garland Crown stubbed.

### Tickets
- `HED-1` `hydra_core/heads.py` — `HEAD_ALIASES` registry. Each entry: plaza slug → mythic name + register + refusal pattern + sigil hint.
- `HED-2` `squads/executive/heads.yaml` — Solon=CEO, Athena=CSO, Hermes=CMO, Hephaestus=CTO, Demeter=CFO, Hestia=COO, Themis=CLO, Asclepius=CPO, Iris=Board. Bind to ExecutiveSuite's existing personas.
- `HED-3` `squads/engineering/heads.yaml` — Daedalus=architect, Prometheus=engineer, Argus=reviewer, Hygeia=test-strategist, Cerberus=security-reviewer, Charon=ops-author, Mnemosyne=docs-author.
- `HED-4` `squads/garland/squad.yaml` — stub pointing at [`RLM-Creative`](https://github.com/lebobo88/RLM-Creative) with `entrypoint: stub` until that project ships.
- `HED-5` `hydra_core/deliberation.py` — Society-of-Mind cycle for Executive Crown: independent drafts → cross-critique → Iris devil's-advocate → Hydra synthesizes. Dissents persisted to TheEights Kan cell (substantive disagreements) or Dui cell (validated patterns).
- `HED-6` Update synthesis renderer in `supervisor.py` to use cathedral names in user-facing output, plaza slugs in envelopes.

**Threshold:** `/hydra:run "should we expand to EU?"` → Executive Crown convenes → synthesized answer signed by mythic names → dissents recallable.

---

## Stage 5 — Pour the Venom (dual-use security) ✅ shipped 2026-05-19 (VEN-4 red-team open by design)

**Goal:** every irreversible capability is named, logged, traceable, and gated through Cerberus.

### Tickets
- `VEN-1` `hydra_core/venom.py` — venom registry. `register_venom(capability, owner_squad, refusal_pattern, audit_sink)`. Capabilities of class `venom` route through Cerberus before execution.
- `VEN-2` `squads/engineering/cerberus.yaml` — split Cerberus out of generic `security-reviewer`. Owns prompt-injection defenses, MCP allow-lists, exfiltration tripwires, tool-permission scoping, audit logging to TheEights Kan cell.
- `VEN-3` Extend `governance.redact_for_squad_boundary` to detect prompt-injection patterns, lookalike-tool names, cross-tool exfiltration combinations (per April 2025 MCP security analysis).
- `VEN-4` Schedule external red-team engagement. Stage 5 cannot self-certify; threshold is two weeks of attempted breach without success.
- `VEN-5` `tests/test_venom.py` — every registered venom requires Cerberus pass; Kan-cell audit log entry created; absence of either refuses execution.

**Threshold:** any capability marked `venom: true` cannot be invoked without Cerberus pass and Kan-cell audit entry. External red team has tried and failed for two weeks.

---

## Stage 6 — Light the Constellation (public launch) 📄 docs shipped 2026-05-19; launch gated on LIT-1 + VEN-4

**Goal:** launch as *constellation, not monster.*

### Tickets
- `LIT-1` Trademark clearance for "Hydra" in AI/orchestration class. Manifesto caveat #1 is real (Marvel HYDRA, Lockheed HYDRA prior art). Fallback: keep "Hydra" as internal persona, pick customer-facing wordmark.
- `LIT-2` Marketing site frames Hydra as constellation. Three loglines from manifesto Part III §2. Sigil (nine-headed serpent in lemniscate, crowned central head, octagonal frame).
- `LIT-3` Public developer docs in plaza register; cathedral docs (manifesto + CONSTITUTION.md) linked for those who want them.
- `LIT-4` Open MCP servers per crown so third-party Claude Desktop / Cursor / Kiro users can plug into individual heads (e.g., `Solon` for strategic counsel from IDE).
- `LIT-5` Pricing: per-seat for users; per-decision-of-record for enterprises; TheEights cells priced by retention horizon (Dui low, Gen premium).

**Threshold:** public launch lands the *constellation* metaphor in coverage; no major news outlet leads with "AI HYDRA monster" framing.

---

## Parallel Track — RLM-Creative (Garland Crown's home)

Sibling project at [`RLM-Creative`](https://github.com/lebobo88/RLM-Creative). Not in any of the six Hydra stages; runs in parallel.

### From RLM-CLI-Starter (pull, do not duplicate)
- `copilot-content`, `copilot-image`, `copilot-frontend-designer`
- ComfyUI integration

### Native to RLM-Creative
Eight Garland heads:

| Mythic | Plaza slug | Scope |
|---|---|---|
| Calliope | brand-strategist | Narrative architecture, positioning, voice |
| Erato | copywriter | Long-form, short-form, headline craft |
| Polyhymnia | content-strategist | Editorial calendar, pillar content, repurposing |
| Terpsichore | social-community | Platform-native voice, community rhythm |
| Euterpe | paid-acquisition | Performance creative, channel arbitrage |
| Clio | pr-earned | Story angles, press kits |
| Urania | seo-discovery | Schema, technical SEO, semantic clustering |
| Helios | photo-cinema | Visual direction, shot lists, color science |

### Pattern
CrewAI-style role-based crew (manifesto Part II §2). Hermes (Executive Crown CMO) translates strategic intent into `CreativeBrief` envelope → routed to Calliope as crew lead → fans out.

### Initial files to plan (separate run)
`squad.yaml`, `README.md`, `heads.yaml`, eight per-head subagent definitions, three orchestrator skills (`/creative-campaign`, `/photo-direction`, `/brand-refresh`), ComfyUI bridge config.

---

## Decisions Deferred

- Kan vs Dui split for deliberation dissents (Stage 4 implementation will decide based on outcome qualifier).
- User-approval vs Iris-automated for procedural-memory updates (Stage 3 default: user-approval; revisit at Stage 6).
- RLM-Creative repository topology — monorepo with `squad.yaml` or proper sibling project. Decide when that track begins.
