# Hydra Constellation — Web Research Notes (Phase 4)

Captured: 2026-05-19. Used to ground Act II/III with current external citations.

## LangGraph Supervisor Patterns (2026)

The supervisor pattern remains the most widely adopted multi-agent topology in production LangGraph deployments. Pattern characteristics:

- **One orchestrator does routing only**; specialist agents stay simple and single-purpose.
- **Checkpointing + HITL** built into the runtime — auditable, replayable.
- **Supervisor vs Swarm tradeoff:** Supervisor = easier to reason about, one routing node, clear control flow. Swarm = faster (direct agent-to-agent handoffs, no intermediary), but harder to audit. Recommendation: **start with supervisor; graduate to swarm when latency telemetry forces it.**
- Sits between the "network" and "hierarchical" patterns.

Hydra's choice of supervisor + typed envelopes maps directly to the dominant 2026 pattern and to the LangGraph checkpoint/HITL primitives.

Sources:
- [LangGraph Multi-Agent Orchestration — Official Guide 2026](https://www.lifetideshub.com/docs/langgraph-multi-agent-orchestration/)
- [LangGraph Supervisor Patterns 2026: Official Documentation Guide](https://www.lifetideshub.com/langgraph-supervisor-patterns-2026/)
- [Multi-Agent Orchestration in LangGraph: Supervisor vs Swarm (Focused.io)](https://focused.io/lab/multi-agent-orchestration-in-langgraph-supervisor-vs-swarm-tradeoffs-and-architecture)
- [LangGraph + MCP: Multi-Agent Workflows 2026 Guide](https://techbytes.app/posts/langgraph-mcp-multi-agent-workflow-guide-2026/)

## EU AI Act Article 50 — Status as of May 2026

- **Applies from 2 August 2026.** Digital Omnibus provisional agreement grants a transitional period to **2 December 2026** for generative AI systems already on the EU market.
- **8 May 2026:** European Commission published draft guidelines on Article 50 implementation, opened targeted consultation.
- **Code of Practice (marking/labelling of AI-generated content):** second draft published; final expected **June 2026**.
- **Scope:** Transparency obligations are NOT limited to "high-risk" systems — they apply to any AI system used in the four Art. 50 situations (direct user interaction; synthetic content generation; emotion recognition / biometric categorisation; deepfakes).

Sources:
- [Article 50: Transparency Obligations (EU AI Act portal)](https://artificialintelligenceact.eu/article/50/)
- [Draft Guidelines on Article 50 (European Commission)](https://digital-strategy.ec.europa.eu/en/library/draft-guidelines-implementation-transparency-obligations-certain-ai-systems-under-article-50-ai-act)
- [Code of Practice on marking and labelling of AI-generated content](https://digital-strategy.ec.europa.eu/en/policies/code-practice-ai-generated-content)
- [10 Takeaways: EC Draft Guidelines on AI Transparency (Global Policy Watch, May 2026)](https://www.globalpolicywatch.com/2026/05/10-takeaways-european-commission-draft-guidelines-on-ai-transparency-under-the-eu-ai-act/)
- [AI Act Update: EU Resolves to Change Rules and Extend Deadlines (Latham & Watkins)](https://www.lw.com/en/insights/ai-act-update-eu-resolves-to-change-rules-and-extend-deadlines)

## NIST AI RMF + Agentic Profile

- **AI RMF 1.0** released 26 January 2023.
- **NIST-AI-600-1, Generative AI Profile** released 26 July 2024 — extends the core framework with **12 GenAI-specific risks** (hallucination, data poisoning, prompt injection, IP, over-reliance, etc.).
- **NIST AI Agent Standards Initiative**, **February 2026** — formal declaration that AI standardization has entered the "agent era." Extension of NIST's continuous AI governance program since 2023.
- **NIST AI RMF Agentic Profile** (proposed via CSA Lab): structured RMF 1.0 extensions organized by Govern / Map / Measure / Manage for agents that acquire tool-use capabilities and execute autonomously.

Sources:
- [AI Risk Management Framework | NIST](https://www.nist.gov/itl/ai-risk-management-framework)
- [NIST AI 600-1 Generative AI Profile](https://www.nist.gov/publications/artificial-intelligence-risk-management-framework-generative-artificial-intelligence)
- [NIST AI RMF: Agentic Profile (CSA Labs)](https://labs.cloudsecurityalliance.org/agentic/agentic-nist-ai-rmf-profile-v1/)
- [NIST AI Agent Standards (Meta Intelligence)](https://www.meta-intelligence.tech/en/insight-nist-agent-standards)

## reveal.js for Self-Contained Offline Decks

- Pandoc supports `--self-contained` (or `--embed-resources --standalone` in current Pandoc) to emit a single-file HTML reveal.js deck with all CSS/JS/fonts/images base64-inlined.
- Quarto provides an end-to-end Revealjs format that handles offline embedding cleanly.
- Lazy-load pattern: change `src` → `data-src` on images/video/audio/iframe for in-deck media; reveal.js loads on slide focus.
- For large decks: consider [decktape](https://github.com/astefanutti/decktape) for PDF export when the HTML deck won't run.

Sources:
- [reveal.js](https://revealjs.com/)
- [reveal.js Config](https://revealjs.com/config/)
- [Revealjs Options (Quarto)](https://quarto.org/docs/reference/formats/presentations/revealjs.html)
- [markdeck (offline-ready reveal.js generator)](https://github.com/arnehilmann/markdeck)
- [Pandoc self-contained reveal.js (issue thread)](https://github.com/jgm/pandoc/issues/3915)

## Synthesis for Act III

- **Article 50 timeline pressure** is real and immediate (4 months to first deadline at time of writing).
- **NIST has formally entered the "agent era"** (Feb 2026) — Hydra is timed correctly to the standards wave.
- **Supervisor-pattern adoption** is the production default in 2026 LangGraph — Hydra is on-pattern, not contrarian.
- Reveal.js + Pandoc gives us a credible offline deck pipeline; we use inline `<style>` + `<script>` for true single-file delivery.
