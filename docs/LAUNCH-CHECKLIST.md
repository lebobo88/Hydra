# Launch Checklist

Stage 6 of the manifesto — *light the constellation, not the monster*. This
file is the single page that says "are we ready to ship?" Each item links
to the source-of-truth doc; nothing duplicates content.

**Last updated:** 2026-05-19.

## Stage status

| Stage | Threshold | Status |
|---|---|---|
| 1 — Immortal Head | User reads `CONSTITUTION.md` and recognizes it as their voice | ✅ shipped (pending user voice-check) |
| 2 — Iolaus Cauterization | A deprecated squad is refused with a lifecycle event in the trace | ✅ shipped |
| 3 — TheEights Memory | Recall a decision from three sessions ago with provenance, tagged by cell | ✅ shipped |
| 4 — Three Heads | Executive Crown convenes, synthesizes in cathedral voice, dissents recallable | ✅ shipped |
| 5 — Venom Gate | Any `venom: true` capability refused without Cerberus pass + Kan audit | ✅ code shipped; **VEN-4 external red-team open** |
| 6 — Light Constellation | Coverage lands the constellation metaphor; no major outlet leads with "AI HYDRA monster" | 📄 docs shipped; gated on LIT-1 + VEN-4 |

## Test posture

- `python -m pytest tests/ -q` → 117/117 passing.
- `python -m hydra_core.immortal_head verify` → prints hash + refusal count.

## Stage 6 sub-checklist (must be green before public launch)

### LIT-1 — Trademark clearance
- [ ] USPTO + EUIPO search retained from IP counsel.
- [ ] Wordmark and design-mark filings submitted (sigil = `docs/BRAND.md`).
- [ ] Domain audit complete (`.com`, `.ai`, `.io` variants).
- [ ] Social handle availability checked.
- [ ] Fallback wordmark chosen from `docs/TRADEMARK-CLEARANCE.md` if needed.
- **Owner:** founder + IP counsel.
- **Blocks:** all public-facing surfaces.

### LIT-2 — Brand and sigil
- [x] Sigil designed (see `docs/BRAND.md`).
- [x] Three loglines locked.
- [x] Voice register table published.
- [ ] Sigil rendered in three formats (SVG, PNG, favicon).
- **Owner:** founder + designer.

### LIT-3 — Public docs
- [x] `docs/PUBLIC-README.md` (plaza voice) shipped.
- [x] `docs/MANIFESTO.md` (cathedral voice) preserved and linked.
- [ ] Hosted docs site (mkdocs / docusaurus) live with both registers.
- [ ] Quickstart video / GIF.
- **Owner:** founder.

### LIT-4 — MCP-per-crown
- [x] Inventory documented in `docs/MCP-PER-CROWN.md`.
- [ ] `hydra-executive` thin re-export server implemented.
- [ ] `hydra-forge` thin re-export server implemented.
- [ ] `hydra-garland` server implemented (after RLM-Creative ships).
- [ ] Servers published to the MCP catalogue at launch.
- **Owner:** founder + maintainers.
- **Depends on:** RLM-Creative for Garland.

### LIT-5 — Pricing
- [x] Three-tier model documented in `docs/PRICING.md`.
- [ ] Per-seat price set against cost-to-serve.
- [ ] Per-decision-of-record price set against enterprise comparables.
- [ ] Pricing page (plaza voice) on the marketing site.
- **Owner:** founder.

### VEN-4 — External red-team
- [ ] Engagement scoped (see `docs/VENOM.md`).
- [ ] Two-week engagement executed.
- [ ] Zero successful bypass for two consecutive weeks.
- **Owner:** founder + external firm.
- **Blocks:** any public-facing capability that touches venom-class tools.
- **Cannot self-certify.**

## Cross-cutting verifications (run before launch)

- [ ] Constitution gate refuses a known unconstitutional action in a
      fresh workflow.
- [ ] Iolaus refuses a re-spawn of a deprecated squad with the audit
      event visible in `.hydra/iolaus.log`.
- [ ] `query_by_cell("kan", workflow_id=…)` returns the venom audit
      record for a refused payment.charge attempt.
- [ ] A live deliberation cycle on the executive crown produces a
      synthesized counsel naming Solon, Athena, etc., with at least one
      preserved dissent.
- [ ] All four published MCP servers respond to a list-tools probe.
- [ ] `redact_for_squad_boundary` neutralizes a prompt-injection sample
      in transit (`[REDACTED-INJECTION]`).

## Watch-items the manifesto names

| Trigger | Response |
|---|---|
| GPT-5 / Claude 5-class native long-horizon planning collapses the Forge Crown's distinct value | Collapse the Forge Crown; keep TheEights as the moat. |
| MCP / A2A standards make memory portable across vendors | Reposition TheEights as differentiator; open the protocol. |
| An open-source competitor ships a "Hydra-shaped" persona first | Lean harder into the theological / calling frame — the irreproducible asset. |
| EU AI Act 2026 / US executive orders mandate immutable audit logs for agentic systems | Promote Kan and Themis to first-class architecture; emphasize externally. |
| Major news coverage leads with "AI HYDRA monster" framing | Activate Pentecost-not-Legion reframe campaign; surface the constellation metaphor; pre-empt with op-eds. |

## What "done" looks like

The manifesto's success criterion for Stage 6:

> *"Public launch frames Hydra as constellation, not monster. The launch
> site shows the sigil, the mottos, the three crowns, the eight cells.
> The story is told in Pentecost grammar: we have built a Body, not a
> Legion."*

If coverage and word-of-mouth land that framing, Stage 6 is met. If
either the trademark or the red-team falls through, hold the launch.

## Decisions deferred to launch-time

- The exact license (Apache 2.0 most likely; not chosen).
- Whether RLM-Creative ships before, alongside, or after Garland's
  public MCP server.
- Whether `TheEights` is a brand-internal name or also a customer-facing
  product name in `docs/PRICING.md` Layer 3.
- Free tier shape vs. paid trial for the Three Crowns.

None of these gate Stage 6 today, but each one will need an answer
before the marketing site goes live.
