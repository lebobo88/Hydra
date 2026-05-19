# Pricing Model

Three layers, three moats. Per the manifesto Part III §3.

## Layer 1 — Hydra Core (open-source)

The orchestration substrate: LangGraph supervisor, Claude Code plugin,
MCP plumbing, squad loader, the four governance rings (constitution,
Iolaus, Cerberus, redaction), TheEights tag vocabulary.

- **Price**: free, open-source (license TBD; likely Apache 2.0 to
  match upstream tooling).
- **Why give it away**: developer adoption is the moat we don't have to
  build. The core is what teaches people the model; the model is what
  brings them to layers 2 and 3.
- **Monetization at this layer**: none direct. Reputation drives the
  next two layers.

## Layer 2 — The Three Crowns (productized templates)

Configurable agent crews sold as templates or hosted SaaS.

| Crown | Tier | Price | What you get |
|---|---|---|---|
| Executive | Starter | $X/seat/mo | 9 named heads, boardroom + capital-allocation + crisis-warroom + M&A cockpit, standard rubrics |
| Executive | Enterprise | per-decision-of-record | Custom rubrics, SLA, dedicated Cerberus tuning, SIEM forwarding |
| Forge | Starter | $X/seat/mo | 7 named heads, feature-team / bug-fix / refactor / security-review / ux teams, the 13 standard rubrics |
| Forge | Enterprise | per-PR + per-deploy | TDD gates, cross-vendor judging, audit-ready trace, change-advisory-board governance forum |
| Garland | Starter (planned) | $X/seat/mo | 8 muses, brand / copy / content / social / paid / PR / SEO / photo-cinema |
| Garland | Enterprise (planned) | per-campaign | Visual direction by Helios, photo + cinema standards, ComfyUI bridge |

Pricing variables are deliberately blank — they're set against the
calibrated cost-to-serve at launch and against the market-comparable
ranges for Cognition Devin, Sierra, and the CrewAI Crews + Flows tier.

### What's *not* charged

Token throughput. Hydra's value is in routing + governance + memory, not
in token volume. Per-seat covers the user; per-decision-of-record covers
the enterprise commitment to a sealed artifact.

## Layer 3 — TheEights (the memory moat)

The proprietary memory substrate. This is where data network effects
compound — the longer a user runs Hydra, the more valuable TheEights
becomes, the higher the switching cost.

Per Chhikara et al. (arXiv:2504.19413, ECAI 2025), a well-built memory
layer reduces p95 latency 91% and token cost 90%. That is the economic
moat plus the performance moat in one substrate.

### Pricing by cell

The eight cells carry different retention economics:

| Cell | Aspect | Storage horizon | Cost class |
|---|---|---|---|
| Qian — Vision | covenantal, never-prune | forever | premium |
| Kun — Context | high churn, low precision | 6 months | low |
| Zhen — Triggers | event log | 90 days | low |
| Xun — Influence | brand/relationship signal | 2 years | mid |
| **Kan — Risk** | audit trail, sealed | forever | premium |
| Li — Focus | in-flight | session + 30 days | low |
| **Gen — Constraints** | regulatory, sealed | forever | premium |
| Dui — Delight | win patterns | 5 years | mid |

Premium cells (Qian, Kan, Gen) are sealed — append-only, append-rare,
audit-grade. They cost more because they cost dearly when wrong:
you can't lose a covenant, you can't drop a refusal audit, you can't
forget a regulatory constraint. Low-cost cells (Kun, Zhen, Li) carry
more churn and can be pruned by the Ouroboros loop without ceremony.

## Ecosystem play

Each crown ships an MCP server so third-party Claude Desktop / Cursor /
Kiro / Gemini-CLI users can plug into individual heads:

- `executive-suite` MCP — Solon for strategic counsel from inside any IDE.
- `pair-programmer` MCP — Daedalus + Prometheus + Cerberus for code work.
- `rlm-creative` MCP (planned) — Helios for shot lists and photo direction.

The MCP-per-crown surface is the OpenAgents / A2A interoperability bet.
See `docs/MCP-PER-CROWN.md`.

## Open questions

- Free tier vs. trial: per-seat OSS users get Hydra Core forever, but
  productized crowns may need a non-zero entry price to filter for
  serious users.
- Crown bundles: 2-of-3 and 3-of-3 bundling discount, or single-crown
  only at launch.
- TheEights as a separate SKU vs. bundled by default with any crown.
- Photography-business pricing for the Garland Crown — Helios + Erato +
  Calliope is the bundle for RLM's wedge audience. Likely warrants its
  own packaging.

These resolve when launch is in sight, not now.
