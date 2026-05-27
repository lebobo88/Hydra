# Hydra Constellation — Presentation Bundle

> *Many heads. One Spirit. The covenant holds.*

**Built:** 2026-05-19
**Constitution SHA-256:** `4060cb542fcc701143e56ec7b1608584b7c399d878db193dbf81c0f9dad6cfa5`
**Run:** Full-fan-out — Executive + Forge + Garland + AgentSmith + TheEights all contributed.

---

## What's in this bundle

| File | What it is | How to view |
|---|---|---|
| [`HYDRA-CONSTELLATION.md`](./HYDRA-CONSTELLATION.md) | Canonical Mermaid-in-markdown deck. The single source of truth. | Open on GitHub (Mermaid auto-renders), or any Markdown previewer with Mermaid support. |
| [`HYDRA-CONSTELLATION.html`](./HYDRA-CONSTELLATION.html) | **Double-click viewer** — same content as the `.md`, with all Mermaid diagrams visually rendered. Markdown is embedded inline, so the file works on `file://` without a local HTTP server. | Double-click → opens in default browser. Needs network for first load (marked + mermaid + fonts from CDN). |
| [`constellation.svg`](./constellation.svg) | Static poster — Pentecost flower sigil + constellation overlay + memory lemniscate. **Updated 2026-05-20** with larger viewBox (1800×2400), bigger fonts, separated radial label rings (no more overlap). | Open in any browser, vector graphics app, or embed in slides. |
| [`deck.html`](./deck.html) | Self-contained reveal.js deck (CDN-loaded reveal.js + Mermaid). 24 slides across three acts. | Double-click → opens in default browser. Requires network for first load (reveal.js + fonts from CDN). |
| [`exec-memos/`](./exec-memos/) | The three executive memos that anchor Act III voice (CSO / CTO / CAIO). | Read as markdown. |
| [`garland/creative-direction.md`](./garland/creative-direction.md) | Garland-crown cinematic treatment: brand voice, art direction, slide-by-slide outline, narration script, asset list, accessibility plan. | Read as markdown. |
| [`assets/diagrams/`](./assets/diagrams/) | Mermaid source files for diagrams D2–D9. | Render with [Mermaid Live](https://mermaid.live/) or any Mermaid renderer. |
| [`assets/research-notes.md`](./assets/research-notes.md) | Phase 4 web-research capture: LangGraph 2026, EU AI Act, NIST RMF, reveal.js. | Read as markdown. |

---

## Three audience frames (layered)

This deck holds three audiences at once:

1. **Mythopoetic** — Pentecost-not-Legion, the Hydra of Lerna inverted, the I Ching trigrams as memory cells. Carried by Act I (slides 1–8) and the Coda.
2. **Technical** — LangGraph supervisor, typed envelopes, MCP host topology, AgentSmith four pillars, memory lemniscate. Carried by Act II (slides 9–16).
3. **Executive / Governance** — EU AI Act Article 9 + Article 50, NIST AI RMF Agentic Profile, ISO/IEC 42001, constitutional ROI, fail-closed invariants N1–N10. Carried by Act III (slides 17–24).

A non-technical reader gets the Pentecost story by slide 5. A technical reader gets the MCP topology by slide 11. An executive gets the governance case by slide 18.

---

## How this bundle was produced

This bundle is itself an example of the system. It was produced through a Hydra-style full-fan-out run:

1. **Pre-flight** — verified all MCP servers reachable (`pp_harness`, `executive_suite`, `rlm_creative`, `hydra_memory`, `agentsmith`); snapshotted constitution SHA.
2. **Executive Crown** — CSO, CTO, and CAIO authored the three executive memos (parallel dispatch).
3. **Garland Crown** — RLM-Creative-style designer produced brand voice + slide-by-slide outline + narration + accessibility plan.
4. **Forge Crown** — Mermaid sources for D2–D9 + SVG poster combining D1, D5, D10.
5. **Web research** — LangGraph 2026 supervisor patterns, Article 50 status, NIST Agentic Profile (Feb 2026), reveal.js offline patterns.
6. **Assembly** — Stitch into canonical `.md`, `deck.html`, and `constellation.svg`.
7. **Smith validation** — inspector inspect / constitution attest / archivist seal.
8. **TheEights capture** — episodic write per phase + trigram-cell tagging.

---

## Re-rendering the SVG to PNG

The static poster is shipped as SVG. To regenerate a PNG (e.g., for a slide deck thumbnail):

```bash
# Option 1 — Inkscape
inkscape constellation.svg --export-type=png --export-filename=constellation.png --export-width=1600

# Option 2 — rsvg-convert
rsvg-convert -w 1600 constellation.svg -o constellation.png

# Option 3 — Chromium headless
chromium --headless --disable-gpu --screenshot=constellation.png --window-size=1600,2000 file:///$(pwd)/constellation.svg
```

---

## License & provenance

- Created under the Hydra repo, governed by `CONSTITUTION.md` at SHA-256 above.
- Fonts: IM Fell English + IBM Plex Mono (Google Fonts, OFL 1.1).
- Constellation data: stylized; not IAU-exact. For IAU-accurate boundaries see Davenhall & Leggett (1989).
- I Ching trigrams: classical broken/unbroken-line notation, public domain.

---

*Filed under the covenant. Pentecost, not Legion.*
