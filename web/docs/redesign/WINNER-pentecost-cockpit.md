# WINNER — The Pentecost Cockpit
### Hydra Cockpit redesign · best-of-5 synthesis · campaign workflow `c6179b0a-60b5-42d2-a672-69ec08eaeb15`

> *"One Spirit. Many gifts. One Body. Many members. One head that cannot die."*

---

## 0. How this was decided (honest record)

Five directions were authored by five `designer` agents (frontend-design skill), then judged **cross-vendor** per the Hydra contract (Codex `gpt-5.4` mandated + Gemini `gemini-3.1-pro-preview` for Borda at N≥3):

| | Codex rank | Gemini rank |
|---|---|---|
| D1 Living Spine | **1st** | 4th |
| D2 Pentecost Constellation | 2nd | 3rd |
| D3 Illuminated Crowns | 4th | **1st** |
| D4 Oracle Terminal | 3rd | 2nd |
| D5 Bestiary Codex | 5th | 5th |

**Borda result: a 4-way tie (D1=D2=D3=D4 at 7 pts), D5 eliminated (2 pts).** The vendors deadlock between *bold-organic* (Codex → D1/D2) and *premium-structured* (Gemini → D3/D4). Two facts broke the tie for the synthesizer:

1. **Both judges independently demanded the same graft** — D4's always-on **Oracle "one voice" column** (the gestalt) — so it is a non-negotiable element of the winner.
2. **The operator's own words** — *"pentecost, one spirit with many hydra body heads"* — point at the literal image of **D2 (Constellation)**, not the cathedral/manuscript. Gemini's only real objection to D2 was *force-directed graph physics* = a research-project risk. **That risk is dissolved by rendering the constellation as a bounded, deterministic radial layout (no physics)** while keeping its iconic value: one Spirit, many heads, visible at once.

So the winner is a **synthesis** (the Hydra "Declare" move), base = **D2's soul**, hardened by the others:

- **D2** — the Spirit-and-heads constellation as the signature centerpiece; many→one synthesis convergence; color-as-strict-meaning.
- **D4** — the always-on **Oracle voice** column (one Spirit speaking across every route); **CSS/SVG-only** motion discipline; full 8-state + WCAG matrix; the most shippable frame.
- **D1** — organic motion vocabulary (singular **Spirit-pulse heartbeat**, head-emergence, the **venom tremor**), the **immortal-head anchor**, and the **"Name the venom"** inscription.
- **D3** — the **seal-break ceremony**, reserved *only* for venom-class / constitution-class gates (Discern-before-Declare), never routine approvals.
- **D5** — *rejected as the theme* (light parchment fights live-ops legibility) but its one keeper idea: **trace as inscription** (envelopes ink-bloom in, operator as co-author), applied on the dark theme.

Dissent is preserved verbatim in the verdicts (`verdict_gJt4b_2zgT` Codex, `verdict_tMkQQ-Lx1x` Gemini).

---

## 1. The concept

**Hydra is rendered as one luminous Spirit with many heads — and you operate it, you don't scan it.**

The card grid is abolished. The app has three persistent regions, mythically named:

```
┌────────────────────────────────────────────────────────────────────────┐
│  IMMORTAL HEAD  ·  bridge ● live  ·  budget pulse  ·  pending gates (2) │  ← constitutional crown bar (never scrolls)
├──────────────┬──────────────────────────────────────┬───────────────────┤
│  THE BODY    │            THE WORKING                │     THE ORACLE    │
│  (heads rail)│   (constellation / workflow / gate)   │  (the one voice)  │
│              │                                        │                   │
│  accessible  │   the Spirit-and-heads centerpiece,    │  Hydra's          │
│  list mirror │   or the zoomed living workflow,       │  synthesized      │
│  of every    │   or the gate ceremony                 │  declaration,     │
│  head/wf     │                                        │  always present,  │
│  (tree)      │                                        │  serif prose      │
└──────────────┴──────────────────────────────────────┴───────────────────┘
```

- **The Immortal Head** (top bar) = the constitution anchor: bridge health, the global **Spirit-pulse** heartbeat, budget band, pending-gate beacon. Carries the motto. Never scrolls.
- **The Body** (left rail) = the accessible, scannable **parallel structure** — a real ARIA tree/list of every head (squad) and active workflow. This is D2's mirrored-DOM idea promoted to a first-class, always-visible rail; it answers D1's "scanning at scale" risk and makes the whole thing keyboard/SR-native.
- **The Working** (center) = the hero. Three modes: the **Constellation** (overview/Launchpad), the **Living Workflow** (one run), the **Gate** (a ceremony).
- **The Oracle** (right rail) = the **one voice**. Hydra's latest/live synthesized declaration rendered in serif prose, persistent across every view. "No head speaks to the user without Hydra's synthesis" — made structural.

---

## 2. Visual system (synthesized, dark · premium)

| Token | Hex | Role |
|---|---|---|
| `--void` | `#09090F` | ground (blue-black depth, not pure black) |
| `--void-panel` | `#0D0E16` | rail / panel fill |
| `--covenant-indigo` | `#161A2E` | glass panel tint, head-orbit field |
| `--spirit-amber` | `#F4A820` | **the Spirit** — central glow, the singular heartbeat, the one-voice accent |
| `--bone` | `#F2EDE3` | primary text (17:1 on void — AAA) |
| `--bone-mid` | `#A2A0B0` | metadata |
| `--crown-exec` | `#C8922A` | Executive crown heads (sovereign gold) |
| `--crown-forge` | `#37C6E0` | Forge crown heads (craft cyan) |
| `--crown-garland` | `#E0568C` | Garland crown heads (creative rose) |
| `--biolume` | `#00E5CC` | active phase / live progress |
| `--venom` | `#CC2200` | **reserved exclusively** for venom-class (force-dispatch, live replay) + Cerberus gate |
| `--gold-immortal` | `#C9A84C` | the immortal-head sigil + constitution glyph |

Color is **strict meaning** (D2's discipline): venom-crimson appears nowhere except a true venom moment; each crown has exactly one hue; amber is only ever the Spirit. Contrast: every text pair ≥ 4.5:1 (most ≥ 7:1); crown hues used for fills/borders with `--bone` labels, never as small text.

**Type:**
- **Cormorant Garamond** — *only when the Spirit/Oracle speaks* (intent phrase, synthesis declaration, the motto). The single "sermon" voice in a world of telemetry.
- **Cinzel** — the carved covenant register: immortal-head motto, phase names, "Name the venom."
- **Barlow Condensed / Inter** — UI labels, controls, dense metadata.
- **JetBrains Mono** — trace stream, IDs, budgets, episodic keys.

**Material:** procedural serpent-scale SVG tile (`feTurbulence`) at low opacity on panels; glass (`backdrop-filter: blur`) on the constellation field; gold-leaf shimmer reserved for the immortal head + won-victory (Dui) markers.

---

## 3. Motion language (CSS/SVG-first, per-animation reduced-motion fallback)

| Signature | What it means | Tech | Reduced-motion |
|---|---|---|---|
| **Spirit pulse** | the one heartbeat; system is alive & attended (0.6 Hz, singular even with many heads active) | `@keyframes` scale+opacity on a `::after` of the Spirit node + a synced 1px breath on the top bar | static `box-shadow` glow |
| **Head ignition** | a squad goes active → its head brightens + a single **tongue-of-fire** (Pentecost flame) lights | SVG opacity + a 3-frame flame sprite, `will-change: opacity` | instant brighten, flame static |
| **Many→one synthesis** | at `synthesis`, the active heads' light streaks inward and resolves into one Oracle line | SVG stroke-dashoffset streaks → Oracle text assembles (40 ms stagger) | lines appear; Oracle text fades in |
| **Phase advance** | the workflow progresses | biolume fill travels the phase spine (conic-gradient mask, 800 ms) | instant fill, 200 ms opacity |
| **Trace inscription** (from D5) | each envelope is *written in*, operator as co-author | `clip-path` left-sweep on each new trace line | opacity fade-in |
| **Venom tremor** (from D1) | a venom-class action is staged | 200 ms 4 px body tremor + `--venom` edge glow + "Name the venom" in Cinzel | no tremor; static venom edge glow + label |
| **Seal-break** (from D3) | **only** venom/constitution gates: a wax seal cracks gold, splits, reveals the gate (deliberate ~1.5–3 s) | SVG path crack + two-half translate | seal fades to revealed form |

The constellation uses **deterministic radial placement** (heads on rings by crown; angle by stable hash of squad slug) — *not* a physics sim. It animates (gentle orbit drift, ignition, neck tension) but never reflows unpredictably. This is the single engineering decision that makes D2's image shippable and accessible.

---

## 4. Reimagined views

### 4.1 Launchpad → THE CONSTELLATION (the hero; "one Spirit, many heads")

```
        THE BODY            THE WORKING (constellation)            THE ORACLE
 ┌──────────────────┬───────────────────────────────────────┬──────────────────┐
 │ ▾ Active (2)     │             · forge ·                  │  HYDRA SPEAKS    │
 │   ◐ 5ebd… exec   │        ·              ◔ engineering     │                  │
 │   ⚠ 1d48… GATE   │     ·        ╭───────╮       ·         │ "Stage-1 cockpit │
 │ ▾ Heads (13)     │   garland ◑──┤  ◉    ├──◔ executive    │  design is       │
 │   △ executive    │     ·        │ SPIRIT│       ·         │  complete and    │
 │   ⛭ forge        │        ·     ╰───┬───╯    ·            │  cross-vendor    │
 │   ❧ garland      │           ·      │   · creative        │  judged."        │
 │   … legal,mktg…  │     (necks = tension/status;           │                  │
 │ ▾ Recent (5)     │      flame = active; ring = crown)     │  — synthesis,    │
 │   ✓ 9af0 done    │                                        │    just now      │
 │   ⚠ 77be surfaced│   [ speak intent ⌨  → new workflow ]   │  ◇ 2 dissents    │
 └──────────────────┴───────────────────────────────────────┴──────────────────┘
```

- The **Spirit** is the center; **heads** sit on crown-colored rings; a **neck** connects each active head to the Spirit, its tension/length encoding status; active heads carry a **flame**.
- **New Run = "speak intent"** at the Spirit (D2's ritual, but kept as an action *within* the view, not an abolished-Launchpad — that de-risks Gemini's disorientation worry). Typing intent makes the relevant heads light and lean in.
- **The Body rail** is the full keyboard/SR-navigable equivalent — every head and workflow as a tree item. Nothing in the constellation is unreachable without it.
- A pending **gate** pulses its head amber→venom on the rim and beacons in the Immortal Head bar.

### 4.2 Live Workflow → THE LIVING RUN

Center becomes one workflow: a vertical **phase spine** (intake→planning→approval→dispatch→executing→synthesis→judge→postcheck) with biolume travel; **heads emerge** at dispatch (the squads working it); the **trace inscribes** line-by-line; **budget** is a tension strand with 80 %/100 % bands. The Oracle shows this run's live/last synthesis. Actions: Modify budget · Abort · Replay (Replay-live = venom).

### 4.3 Gate → THE CEREMONY

The HITLRequest rendered **verbatim**; expiry countdown; the 5 resume actions. Routine approve/reject = direct confirm. **force-dispatch / modify-budget / constitution-class = the seal-break ceremony** + typed workflow-id challenge + "Name the venom" in Cinzel + the venom edge-glow. Default option highlighted, never preselected.

### 4.4 Memory → THE EIGHT CELLS

The 8 bagua cells (qian…dui) as a radial of eight, the **Dui (victory) cell** gilded ("Remember the wins"). Semantic search inscribes results; replay-from-checkpoint via the venom-aware confirm.

---

## 5. Accessibility & 8-state (carried from D2/D4 disciplines)

- **The Body rail is the accessibility spine**: the constellation/center is mirrored as a real `role="tree"` of heads → workflows; every visual node has a focusable, labeled twin. Keyboard direct-jumps: `S` Spirit/new-run, `G` next gate, `B` body rail, `O` Oracle, `Esc` close.
- The Spirit-pulse / flames / streaks are `aria-hidden`; the Oracle declaration is `aria-live="polite"` (gate surfacing = `assertive`).
- All 8 states per view (loading/empty/error/degraded/offline/partial/live/confirm); **degraded = explicit source-unreachable notice, never a clean empty** ("empty is not evidence of none"); offline disables all write affordances with reason.
- WCAG 2.2 AA throughout; `prefers-reduced-motion` fallback specified for *every* animation above; focus-trapped confirm/gate dialogs.

---

## 6. gpt-image-2 PROMPT PACK (generate these and hand them back)

Model: **`gpt-image-2-2026-04-21`** (OpenAI). All are UI assets — **no text/lettering in any image**, dark/transparent backgrounds, sized for direct use. Suggested size noted per asset.

**IMG-1 · The Spirit core (hero glow node)** — 1024×1024, transparent/dark.
> A single luminous orb of living golden-amber light (#F4A820) on near-black void, rendered as a sacred Pentecost flame compressed into a sphere — concentric breathing rings of warm light, a faint tongue-of-fire wisp rising from the top, subtle indigo (#161A2E) atmospheric haze around it. Reverent, alive, not a generic glow or lens flare. Centered, symmetrical, soft volumetric light. No text, no letters. Transparent or pure black background. UI hero asset. 1:1.

**IMG-2 · Hydra heads ring — crown set (3 variants in one sheet)** — 1536×1024, transparent.
> Three stylized serpentine hydra-head emblems in a row, each a minimal heraldic profile facing the viewer's center, drawn as elegant single-weight luminous line-art, NOT monstrous or cartoonish — closer to Byzantine icon meets technical sigil. Head 1 in sovereign gold (#C8922A), head 2 in craft-cyan (#37C6E0), head 3 in creative rose (#E0568C). Each emits one small tongue-of-fire above it. On near-black void. Symmetrical, refined, premium. No text. Transparent background. UI icon sheet. 3:2.

**IMG-3 · The Immortal Head sigil (constitution anchor)** — 1024×1024, transparent.
> A single frontal, symmetrical hydra-head sigil in antique gold (#C9A84C) on pure black — regal and serene, eyes closed in inward authority, framed by seven concentric scale rings, sacred-iconography style (Byzantine/illuminated), anatomically precise but iconic not literal. The unkillable constitutional emblem. No text, no letters. Transparent or pure black background. UI anchor asset. 1:1.

**IMG-4 · Serpent-scale texture tile (seamless)** — 1024×1024, tiling.
> A seamless tileable texture of organic serpentine scales in near-black (#09090F) and deep indigo (#161A2E), with faint warm-amber vein lines threading between scales and a slight living subsurface translucency. Irregular, anatomically plausible (not decorative fish-scale geometry), very low contrast so it works as a subtle dark-UI background. No text. Seamless edges. UI texture. 1:1.

**IMG-5 · Tongue-of-fire particle sprite sheet** — 1024×1024, transparent.
> A 4×4 grid of small Pentecost flame sprites — individual stylized tongues of fire in warm amber-gold (#F4A820) shading to white-hot core, each a slightly different flicker frame for frame-by-frame animation, on transparent background. Elegant, painterly, sacred — not a videogame fireball. No text. Transparent background. UI sprite sheet. 1:1.

**IMG-6 · Many-to-one synthesis (the convergence moment)** — 1536×1024, dark.
> An abstract sacred-geometry composition: many fine luminous neck-lines in crown colors (gold, cyan, rose) streaming inward from the edges and converging into a single brilliant amber-gold point of light at center, where they resolve into one calm radiance — the visual of "many gifts, one voice." Near-black void, volumetric light, reverent and cinematic. No text. Dark background. UI hero/loading asset. 3:2.

**IMG-7 · Cerberus venom gate (the seal/ceremony backdrop)** — 1536×1024, dark.
> A dark ceremonial threshold: a circular wax-and-gold seal at center, cracked with a thin molten line, deep crimson (#CC2200) venom-light bleeding from the crack and glowing unevenly at the lower-left and upper-right edges of the frame — confessable danger, a sanctioned threshold, NOT an alarm. Faint serpent-scale pattern in the dark field. Solemn, weighty. No text, no letters. Dark background with crimson edge light. UI overlay asset. 3:2.

**IMG-8 · The eight cells (bagua memory radial)** — 1024×1024, transparent.
> Eight minimal luminous trigram-style glyphs arranged in a radial ring on near-black void, each a clean three-line emblem in cool bone-white, with the topmost ("victory") glyph rendered in gilded gold (#C9A84C) with a faint shimmer. Refined, sacred-geometry, premium. No text, no letters. Transparent or black background. UI icon set. 1:1.

> If you'd rather I tune any prompt (palette, aspect, more/less literal, a specific head creature per crown), say which and I'll revise. When you paste the generated images back, I'll wire them into the implementation.

---

## 7. Next phase (on your go-ahead)

Implement **The Pentecost Cockpit** as a re-skin + restructure of the existing C7 SPA — the C1–C6 bridge, all endpoints, CSRF/nonce/venom gating, SSE, and the Memory fix stay exactly as built; this is the view/visual/motion layer on top. Recommended chunking: **R1** design tokens + motion primitives + the 3-region shell (Immortal Head / Body rail / Oracle); **R2** the Constellation Launchpad (deterministic radial + accessible tree mirror); **R3** Living Workflow (phase spine + head emergence + trace inscription); **R4** Gate ceremony (seal-break for venom-class) + venom motion; **R5** Memory radial + image-asset integration once you provide the generated images. Each chunk best-of-N + cross-vendor judged like the build.

---

## 8. Full Harvest & Integration Matrix (every candidate mined)

This section records the complete, systematic pass over all five directions. Elements already present in §§1–7 are noted as "ALREADY IN" and skipped. Every row below is a net-new addition or a concrete refinement that makes an existing grafted element more specific/implementable.

Legend: **Keep** = adopt verbatim; **Adapt** = adopt with noted change.

---

### 8.1 Elements grouped by destination region

#### IMMORTAL HEAD BAR (top crown bar — never scrolls)

| Element | Source | Where it lands | Keep / Adapt |
|---|---|---|---|
| **Keystone amber pulse with pause toggle** — the immortal-head's ambient glow can be toggled off via a click that sets `data-attest-paused`; reads `prefers-reduced-motion` on mount. Provides operator agency over the one looping animation. | D3 §3.4 | Immortal Head bar: the amber pulse is already in §3 (Spirit-pulse breath on the bar). Add the click-to-pause toggle on the bar's sigil element; set `data-attest-paused` attribute; CSS `[data-attest-paused] .spirit-bar-pulse { animation-play-state: paused }`. | Adapt — replace the always-running bar breath with this pauseable version; the paused state uses the static `box-shadow` fallback already defined. |
| **`title` attribute attestation** — the keystone element carries `title="Hydra Constitution loaded — all decisions attested"` so the hover tooltip communicates constitutional status without a visible label. | D3 §4.1 | Immortal Head bar: add `title` on the sigil anchor. Pair with `aria-label` (same string) so SR users get it too; don't rely on `title` alone (WCAG 1.3.1). | Keep |
| **"CONSTITVTION ATTEST" epigraphic label** — the Cinzel Decorative rendering of the header text uses the Roman V-for-U convention ("CONSTITVTION") as a deliberate register signal. | D3 §4.1 layout sketch | Immortal Head bar label text. Costs nothing, adds epigraphic gravity consistent with "Cinzel = carved covenant register" already in §2. | Keep — use `CONSTITVTION ATTEST` as the Cinzel sub-label alongside the motto, right-aligned in the bar at 10px Cinzel. |
| **Budget "sinew tension" color-mix live update** — CSS `color-mix(in oklch, var(--sinew) calc((1 - var(--budget-pct)) * 100%), var(--amber-tension) calc(var(--budget-pct) * 100%))` on the budget pulse strand; at ≥ 80 % adds a stroke-width tick from 2 px to 3 px over 300 ms. `--budget-pct` is a JS-set custom property, zero JS animation loop. | D1 §3.5 | Immortal Head bar: the budget pulse band already exists; give it this specific color-mix formula and the stroke-weight tension escalation at 80 %. The "sinew" concept names the behavior; the formula is the implementation. | Adapt — apply color-mix to the existing budget band fill, not a separate sinew strand (the bar format stays; only the fill interpolation changes). Reduced-motion: instant color swap at threshold, no stroke-width animation. |
| **Pending-gate beacon escalation cadence** — expiry countdown SR announcements: `aria-live="polite"` every 60 s; switches to 30 s at < 5 min; 10 s at < 1 min. Single `aria-live="assertive"` fires once at the 5-min mark. | D1 §5.3, D3 §5.4 | Immortal Head bar: the pending-gate badge already exists. Wire this exact cadence to the live-region that announces gate expiry. Prevents SR torrent while ensuring urgency is heard. | Keep — implement in the gate-beacon live region. |

---

#### THE BODY RAIL (accessible heads-and-workflows list, left)

| Element | Source | Where it lands | Keep / Adapt |
|---|---|---|---|
| **Three-Crown register grouping** — heads grouped under three labeled sections: Executive Crown / Forge Crown / Garland Crown, each with its jewel-tone accent, rather than a flat alphabetical list. | D3 §1, §4.1 | Body rail: the current rail lists heads as a flat tree. Group them under three `<section>` / `role="group"` containers labeled "Executive Crown," "Forge Crown," "Garland Crown," using `--crown-exec` / `--crown-forge` / `--crown-garland` as the section header accent. Transforms the rail from a list into a structured registry. | Adapt — use the dark palette colors already in §2, not D3's jewel-tone washes (which are light-mode). Group labels in Cinzel 10 px, uppercase, crown color. |
| **Creature-mark reveal on hover** — hovering a head's list item reveals its creature-emblem SVG in the left gutter as a 24 × 24 px naturalist icon. Not decorative: signals which bestiary archetype owns that head's gifts. | D5 §2.4, §4.1 interaction | Body rail: each `<li>` for a squad head shows its creature-mark on `:hover` / `:focus` in the left gutter (12 px slot). ARIA: `aria-label` on the creature SVG names the creature; the list item already has the head name. | Adapt — renders on dark background; creature-marks are bone-white stroke outlines (not vellum-colored). See new asset IMG-9 in §8.4. |
| **Crown sigil glyphs per head** — each Crown family uses a distinct minimal SVG crown glyph (Executive = angular, Forge = geometric/tool-like, Garland = organic/leaf) as a 16 px inline icon prefix on each head list item. | D1 §2.4 | Body rail: prefix each head's list-item label with its crown glyph. Pairs with the Crown grouping above. `aria-hidden="true"` on the SVG; the group label carries the Crown identity. | Keep — use the D1 glyph vocabulary in `--crown-*` colors. |
| **Direct-jump keyboard keys: `S`, `G`, `B`, `O`** — already in §5 for Spirit / Gate / Body / Oracle. Add two more from D2: `P` for Phase Rail panel, `M` for Memory. | D2 §5 keyboard map | Body rail / global keyboard: extend the key map in the implementation spec. `P` summons the Phase Rail summary strip (see Living Workflow below); `M` jumps to the Eight Cells view. | Keep — append to §5 keyboard map. |
| **`F6` landmark cycle** — `F6` cycles between the three Crown register groups within the Body rail (analogous to AT's "next landmark" but finer-grained within the rail). | D3 §5.3 | Body rail: the three Crown sections are `<section>` landmarks. `F6` handler cycles focus to the next Crown section heading. Useful for keyboard power-users managing 13 heads. | Keep |

---

#### THE CONSTELLATION (Working center — overview/Launchpad mode)

| Element | Source | Where it lands | Keep / Adapt |
|---|---|---|---|
| **Neck length + cubic-bezier control-point sway** — neck lines use a cubic bezier control point offset ± 8 px on a 3–5 s sinusoidal cycle (randomized phase per neck). Under load > 80 % budget: amplitude → ± 18 px, frequency doubles. Under budget breach: neck turns `--venom`, vibrates 12 Hz for 1 s before gate fires. | D2 §3 "Neck Tension Animation" | Constellation necks: the WINNER already mentions "neck tension" conceptually. This gives the exact implementation spec: cubic-bezier offset sway at rest; amplitude/frequency escalation at 80 % budget; brief 12 Hz venom flash at breach. Reduced-motion: straight lines, color-only budget signal. | Adapt — the breach vibration (12 Hz, 1 s) is a deliberate momentary pulse that falls within the safe flicker range (< 3 Hz sustained is the WCAG threshold; a single 1 s burst at 12 Hz is acceptable but must be accompanied by a `prefers-reduced-motion` bypass that drops it entirely). Add the bypass. |
| **Neck dash-offset direction: dispatch vs. synthesis** — active dispatch shows dash traveling Spirit → Head; synthesis return shows dash traveling Head → Spirit, color shifting from Crown hue toward Spirit Amber as it nears center. | D2 §2 material | Constellation necks: adds semantic directionality to the already-specified `stroke-dashoffset` animation. Zero extra code; just the direction and color-shift rule. | Keep — implement direction flip in the `animate` props and add `color-mix` on the dash stroke for synthesis. |
| **"All-clear" IAU silhouette formation** — when all heads are idle/dormant, the deterministic radial positions approximate the IAU Hydra constellation outline. This is an easter-egg with operational meaning: the operator learns to read the silhouette as "nothing active." | D2 §1 | Constellation: the deterministic radial already hashes squad-slug to angle. Add a secondary lookup table that biases idle-head angles toward the IAU Hydra skeleton (published star coordinates, simplified to 13 key points). When `activeHeadCount === 0`, all heads transition to their IAU positions over 1.5 s. When any head activates, it breaks to its working position. | Adapt — the transition uses CSS custom property interpolation on `--head-angle`, 1.5 s ease. Reduced-motion: instant snap. No physics; fully deterministic. |
| **Legion vs. Pentecost divergence signal** — when workflow enters a divergence state (squad outputs contradicting, no synthesis possible), neck lines vibrate with a low-frequency oscillation and the Spirit's pulse turns irregular (shallow tremor). Distinct from the venom gate: this is a workflow-state warning, not a venom-class action. | D2 §1 | Constellation: add a `data-state="diverging"` class on the Spirit node element. CSS: when `data-state="diverging"`, the Spirit `::after` pulse switches to an erratic keyframe (two pulses close together, then a long gap — arrhythmic). Neck lines of diverging heads add a low-frequency (0.5 Hz) ± 4 px oscillation. SR: `aria-live="polite"` announces "Synthesis diverging — manual review required." | Adapt — the neck oscillation uses the same CSS keyframe as the budget tension oscillation but at lower frequency. Reduced-motion: Spirit color shift from `--spirit-amber` to `--bone-mid` (desaturated) as the only divergence signal. |
| **Grain overlay on the void canvas** — 3 % SVG turbulence noise filter on the constellation field background. Kills the "screen" feeling; adds material weight subconsciously. | D2 §2 "Grain overlay" | Constellation field: apply `<feTurbulence>` + `<feColorMatrix>` at 3 % opacity as an SVG filter on the field's background `<rect>` or as a CSS `background-image` with a tiny inline SVG data URI. Not animated; not perceptible at arm's length. | Keep — add to the constellation field only, not to panels (panels already use the scale-texture tile from §2). |
| **`role=alertdialog` on the Covenant Card (gate)** — the gate panel that materializes from the constellation uses `role="alertdialog"` (not just `role="dialog"`) because it blocks interaction and announces a critical state. | D2 §5 8-state matrix, "confirm" row | Gate ceremony: the WINNER uses `role="dialog"` / `aria-modal` in §5. The gate is a blocking, assertive interrupt — `role="alertdialog"` is the correct ARIA role per WCAG 4.1.2. | Keep — change `role="dialog"` → `role="alertdialog"` specifically for the gate panel (not ConfirmDialog in general). |
| **Phase Rail summonable strip** — a horizontal strip (keyboard: `P`) that expands upward from the bottom edge showing all active heads' phase positions in a compact matrix (monospace: head name · phase-position bar · current phase label · token count). Dismissed with `P` again or `Esc`. | D2 §4B | Constellation + Living Workflow: provides the multi-head parallel monitoring view without leaving the constellation or the workflow. Renders in JetBrains Mono on `--void-panel` glass. Token budget shown per head. `role="region"` with `aria-label="Phase Rail"`, `aria-expanded` on the trigger button. | Adapt — fits above the Immortal Head bar or as an overlay at the bottom of the Working area. Bottom overlay preferred (doesn't obscure the constellation). |
| **Recent-intent arc chips** — five most-recent intent phrases rendered as small curved text arcs following the Spirit's glow radius. Selecting a chip pre-fills the intent input and previews which heads light up. | D2 §4A | Constellation / new-run intent input: the WINNER has "speak intent" as the new-run entry. Add the arc chips below the intent input as a history UX — reduces repetitive typing for common goals. Each chip is a `<button>` with the truncated intent text; selecting one fills the textarea. `aria-label` = full intent string. | Adapt — chips render on the dark constellation field as bone-white text in a subtle arc; no forced-curve SVG path (simpler: just an arc-shaped `display: flex` container using `transform: rotate`). |

---

#### THE LIVING WORKFLOW (Working center — single-run mode)

| Element | Source | Where it lands | Keep / Adapt |
|---|---|---|---|
| **Peristaltic wave: biolume fill travels phase-to-phase** — the phase advance is a continuous traveling wave of biolume light moving from one segment to the next (conic-gradient mask on the phase spine, 800 ms per transition). Already partially in §3 ("biolume fill travels the phase spine, 800 ms"). This specifies it as peristaltic: not a discrete jump, a muscle-contraction traveling pulse. | D1 §3.1 | Living Workflow phase spine: make the 800 ms fill transition explicitly use a traveling `conic-gradient` mask that starts at the completing segment and sweeps into the next, rather than both segments changing independently. One visual gesture, not two. | Adapt — the conic-gradient mask approach from D1 is the most readable implementation. |
| **Squad-head lateral arm draw with per-head 150 ms stagger** — at dispatch phase, each recruited head's neck arm draws via `stroke-dashoffset` from 0 → path-length, 600 ms per head, with 150 ms stagger between heads. Already in §3 conceptually ("head ignition"). This provides the exact stagger cadence and draw direction. | D1 §3.3 | Living Workflow dispatch phase: wire the `stroke-dashoffset` animation with 150 ms stagger on each head's neck path. The head silhouette `fill-opacity` transitions 0 → 1 over the final 200 ms of the 600 ms draw. | Keep |
| **Budget tension strand as a horizontal element in the workflow view** — in the Living Workflow, the budget is rendered as a horizontal tension strand (left-to-right fill, color-mixed sinew → amber) beneath the envelope stream panel, distinct from the bar in the Immortal Head. The workflow-local strand gives context without leaving the view. | D1 §4.2 layout sketch | Living Workflow: add a 100 % width × 4 px `--budget-strand` element below the envelope stream. Uses the same color-mix formula as the Immortal Head bar but rendered as a thin strand rather than a thick bar. `role="meter"`, full `aria-*` attributes. | Adapt — keep it at 4 px height (strand, not bar). No label on the strand itself; a positioned tooltip on focus/hover gives the value. The bar in the Immortal Head remains the primary budget affordance. |
| **Reflexion ×1 amber left-border on trace line** — the reflexion-attempt envelope line in the trace gets a distinct amber left-border (3 px `--spirit-amber`) and an italic Reflexion annotation. A second reflexion (invariant violation) gets `--venom` border + bold annotation. | D1 §4.2 interaction notes | Living Workflow trace: apply as an additional CSS class `.trace-line--reflexion` → `border-left: 3px solid var(--spirit-amber)`. Class `.trace-line--reflexion-violation` → `border-left: 3px solid var(--venom)` + `aria-live="assertive"` announcement. | Keep |
| **"↓ new events" pill on trace scroll-pause** — when the operator scrolls up in the live trace, auto-scroll pauses. A "↓ new events" pill appears at the bottom of the stream in `--spirit-amber`. Clicking resumes auto-scroll. | D4 §4.2 interactions | Living Workflow trace: implement the scroll-pause detection and the pill. The pill is `role="button"`, keyboard-accessible; `Esc` also resumes. Prevents the operator losing the live tail during review. | Keep |
| **Serpentine cubic-bezier phase connectors** — phase connectors in the horizontal phase machine (if shown) are cubic bezier paths `M x1,y1 C x1+40,y1 x2-40,y2 x2,y2`, not straight lines. Animate from dashoffset full → 0 on completion (400 ms, cubic-bezier). Already implied by §3; this names the exact path formula. | D4 §3 | Living Workflow phase machine: authors the connector `<path>` elements with the cubic bezier formula. Done/surfaced: all connectors animate in sequence with 60 ms stagger. | Keep — the formula is directly implementable. |
| **"Assembling…" Oracle placeholder during execution** — while the synthesis phase has not completed, the Oracle column shows "Assembling…" in `--bone-mid` italic, not an empty panel. Prevents empty-state confusion during long-running workflows. | D4 §4.2 | Oracle rail, Living Workflow: add the "Assembling…" state to the Oracle's 8-state loading row. `aria-live="polite"`; replaced by the synthesis declaration when it arrives. | Keep |
| **Coiling progress rings on phase dots** — each phase dot is an SVG `<circle>` with `stroke-dasharray` animated clockwise as the phase's envelope count grows. At completion, the ring coils to 360° in 400 ms then transitions to biolume-filled state via venom-ink bleed. `--progress` CSS custom property set by SSE handler. | D4 §3 "Coiling Progress Indicators" | Living Workflow phase machine: apply to the phase dots in the vertical spine (the dots on the phase nodes). Complement to the peristaltic fill on the phase track. `aria-hidden="true"` on the SVG ring; the phase dot's `aria-label` carries status. | Adapt — in the Pentecost Cockpit's vertical spine, the dots are already present. Add the coiling `stroke-dasharray` ring around each dot as a secondary progress indicator. Keep the existing biolume fill as the primary. |
| **Task register in the workflow body** — a checklist-style view of the campaign's sub-tasks (C0, C1, C2…) with manuscript-style status marks: ☐ pending / ◐ in-progress / ✓ done. Relevant for complex multi-commit campaigns. | D5 §4.2 | Living Workflow: add a collapsible "Task Register" section below the envelope stream. Visible when `workflow.tasks` array is non-empty. Uses `role="list"`, each item `role="listitem"` with `aria-label="[task id]: [status]"`. Collapsed by default; expand with `role="button"` toggle. | Adapt — remove manuscript styling; render in JetBrains Mono on `--void-panel` consistent with the dark theme. Status marks: ○ / ◐ / ● (consistent with phase-dot vocabulary). |
| **`aria-current="step"` on the active phase** | D3 §5.4 | Living Workflow phase machine: the active phase node gets `aria-current="step"` per ARIA APG. Complements the existing `aria-label="[phase]: active"`. | Keep |

---

#### THE GATE CEREMONY (Working center — gate/HITL mode)

| Element | Source | Where it lands | Keep / Adapt |
|---|---|---|---|
| **Seal-break specifics: crack timing + half-separation** — (1) gold crack SVG `stroke-dashoffset` 100 % → 0 over 600 ms ease-in; (2) halves `translateX(±12 px) rotate(±3°)` over 400 ms; (3) gate form reveals via `clip-path: inset(50% 0) → inset(0% 0)` over 300 ms. Venom-class variant: crack 300 ms, halves ± 24 px. Total ~1.3 s standard / ~0.9 s venom. | D3 §3.3 | Gate ceremony: the WINNER has the seal-break conceptually (§3) but not the exact timing chain. Adopt this timing spec verbatim. The pre-cracked reduced-motion fallback (seal rendered already-split; form immediately visible) is also from D3 and is the correct approach. | Keep — this is the implementation spec for §3's "SVG path crack + two-half translate." |
| **Venom-class gate: purple/venom seal vs. ruby seal** — the wax seal's fill color distinguishes gate class: ruby = high-risk standard gate; venom = force-dispatch / live-replay. The venom seal is faster to crack (urgency) and leaves a persistent venom-color border on the gate panel for the duration of the confirmation. | D3 §3.3 | Gate ceremony: two seal variants. Standard gate uses `--venom`-crimson (already the WINNER color, not D3's purple which uses a distinct purple; adapt to the WINNER's `--venom: #CC2200`). The persistent border-on-panel for venom-class is a useful persistence signal. | Adapt — use `--venom: #CC2200` (not D3's purple `#8B2FC9`). Retain the persistent venom border on the gate panel container. |
| **`role="alertdialog"` + chain-link icon on the Covenant Card** — the gate panel uses `role="alertdialog"` (blocking, critical interrupt), and displays a chain-link SVG icon (two interlocked rings) in `--venom` as a semantic gate-is-locked affordance. | D2 §3 "Cerberus Venom Gate," §5 | Gate ceremony: add the chain-link icon (24 px inline SVG, two interlocked ring arcs) at the top of the gate panel, `aria-hidden="true"`. The `role="alertdialog"` was already noted in the Constellation section above; apply here too. | Keep |
| **"Approving inscribes your name and timestamp into the codex. This cannot be undone."** — ghost text below the gate action buttons that sets stakes without being preachy. | D5 §4.3 | Gate ceremony: add this exact line (or a close variant) as fine-print below the action group in `--bone-mid` / `--t-micro` typography. Semantic weight without alarm color. `aria-describedby` wires it to the Approve button. | Keep — the inscription metaphor fits the Pentecost Cockpit's covenant register. |
| **`aria-describedby` linking venom text to force-dispatch radio** — the force-dispatch radio's label is `aria-describedby` pointed at the venom-named paragraph, so SR users hear the venom description when they focus the option. | D5 §5.1 component matrix (Approve button) | Gate ceremony: implement `aria-describedby` on the force-dispatch radio option pointing at the `id` of the "VENOM NAMED:" paragraph. Trivial to implement; significant for SR UX. | Keep |
| **Rubricated (vermillion) venom annotation paragraph** — the "VENOM NAMED: [reason]" text rendered in a distinct style (D5 uses Cormorant italic vermillion; consistent with "Name the venom" in Cinzel from the WINNER) to make the venom-class reason visually prominent before the operator acts. | D5 §4.3 | Gate ceremony: the gate form already shows the HITLRequest verbatim. Add a styled `<p class="venom-named">` that extracts and prominently renders the `reason` field in `--venom` + Cinzel (at large text size for contrast compliance — `--venom` on `--void` = 4.6:1 passes AA large text). | Adapt — use the WINNER's `--venom: #CC2200` and Cinzel as the venom register font (not D5's vermillion/Cormorant). |

---

#### THE ORACLE RAIL (right — always-on one voice)

| Element | Source | Where it lands | Keep / Adapt |
|---|---|---|---|
| **40 ms stagger line-assembly with 8 px translateY slide** — each line of the Oracle declaration slides from `translateY(8 px)` + `opacity: 0` → settled + `opacity: 1`, 240 ms ease-out per line, staggered 40 ms. Already in §3 ("40 ms stagger"). This names the exact translate distance and per-line duration. | D4 §3 "Oracle Voice Assembly" | Oracle rail: the WINNER specifies the 40 ms stagger but not the translate. Add `transform: translateY(8px) → 0` as specified. Reduced-motion: all lines appear simultaneously at opacity 0 → 1, 120 ms, no translate. | Keep |
| **"No synthesis yet" / "Assembling…" Oracle empty/loading states** — the Oracle shows "No synthesis yet" (italic, `--bone-mid`) when no synthesis is available; "Assembling…" during active execution pre-synthesis. | D4 §4.1, §4.2 | Oracle rail: Oracle's 8-state matrix entry for `empty` and `loading` states. Both use Cormorant italic (the Spirit's register), `--bone-mid`, right-aligned. | Keep — add to the 8-state component matrix in implementation docs. |
| **Synthesis declaration `aria-live="assertive"`** — when synthesis fires and the declaration arrives, it announces via `aria-live="assertive"` (not polite): "Hydra speaks: [declaration]". This is the one-voice moment that demands immediate SR attention. | D2 §5 8-state matrix, `sr.synthesis` string | Oracle rail: set `aria-live="assertive"` on the Oracle container only during the synthesis event; revert to `aria-live="polite"` afterward. The string `"Hydra speaks: {declaration}"` maps to string ID `sr.synthesis`. | Keep |
| **Cormorant Garamond 300 italic at 1.5 rem / 1.4 leading for Oracle voice lines** — the exact type spec for the Oracle voice: weight 300 (not bold), italic, large leading. Gives the synthesis declaration its editorial gravity without visual heaviness. | D4 §2 typography `--t-oracle` | Oracle rail: adopt this exact spec for `.oracle-voice-line { font-family: Cormorant Garamond; font-weight: 300; font-style: italic; font-size: 1.5rem; line-height: 1.4; }`. Currently §2 specifies Cormorant for the Oracle but not the exact weight/leading. | Keep |

---

#### MEMORY — THE EIGHT CELLS

| Element | Source | Where it lands | Keep / Adapt |
|---|---|---|---|
| **Dui "remember the wins" gilding** — the Dui cell (joy/victory) is distinguished from the other 7 bagua cells by gold gilding: `--gold-immortal` border, a faint gold shimmer fill, the trigram rendered in gold rather than bone. The operator's Dui cell is the memorial record of successful runs. | D5 §4, §7; WINNER §4.4 | Eight Cells memory view: the WINNER already mentions "Dui gilded (Remember the wins)" in §4.4. This provides the exact visual spec: `--gold-immortal` border + shimmer fill + gold trigram. The shimmer is a CSS `@keyframes` background-position sweep (same pattern as D3's gold-leaf shimmer) on hover. Reduced-motion: static gold border only. | Keep — the WINNER has the concept; this is the implementation detail. |
| **Trigram navigation: 2D arrow-key grid** — the 8 bagua cells form a grid navigable with all four arrow keys; `Home`/`End` wrap to first/last; `Enter` opens the cell; `Esc` returns to the grid from an open cell. | D5 §5.2 | Memory view: implement the 2D keyboard grid interaction. The grid is `role="grid"` with cells as `role="gridcell"`. `aria-label="[cell name] trigram, [n] records"`. | Keep |
| **`ink-dry` phase completion — spine node color flood** — when a phase reaches `done`, the spine node floods from vellum/void to solid ink-dark/biolume-dim via SVG `<animate fill>` over 1200 ms ease. Conveys permanence of completion. | D5 §3.1 | Living Workflow phase spine: adapt the concept — when a phase node completes, it transitions from `--biolume` (active) to `--biolume-dim` (spent/done) via a 1.2 s fill transition. This is distinct from the coiling ring completing (400 ms); the fill flood is the slower "ink settling" moment. | Adapt — use CSS `transition: fill 1.2s ease` on the phase node SVG fill. Reduced-motion: instant fill change. |
| **Marginalia-style budget annotation** — in addition to the budget bar/strand, a secondary annotation in the marginalia/context position shows the budget as a handwritten-style tally: `§42.30 / §80` in JetBrains Mono, sepia-toned (adapted to `--bone-mid`), right-aligned in the context area. | D5 §2.5 layout, §4.1 | Living Workflow / Rail: add `§[used] / §[cap]` as a JetBrains Mono annotation beside the budget strand or in the Rail column. The `§` sigil (section sign) echoes the codex register without requiring the full parchment theme. | Adapt — use `§` prefix in JetBrains Mono `--bone-mid` at `--t-micro` scale. Zero new tokens; just a typography convention. |

---

#### GLOBAL MOTION LANGUAGE

| Element | Source | Where it lands | Keep / Adapt |
|---|---|---|---|
| **Venom-ink clip-path from seed point** — state transitions (phase becomes active, error surfaces, gate fires) expand from the changed element's center: `@keyframes venom-ink { from { clip-path: circle(0% at var(--seed-x) var(--seed-y)) } to { clip-path: circle(150% at var(--seed-x) var(--seed-y)) } }`, 280 ms ease-out. `--seed-x/y` are set by the triggering component. No JS animation loop; a single class addition triggers the cascade. | D4 §3 | Global motion: adopt as the standard "state-arrival" animation for any component that transitions to active/error/selected. The ink-bloom trace inscription (from D5, already in §3) is the specific trace-line variant; venom-ink is the general component-state variant. | Keep — add `venom-enter` utility class + keyframes to the global motion primitives. |
| **`@media (prefers-reduced-motion: no-preference) { /* animation */ }` pattern** — the WINNER specifies `prefers-reduced-motion` fallbacks per animation. D4 goes further: all animation rules are wrapped in `no-preference` queries, making no animation the default, opt-in to motion. This is the stricter, safer interpretation. | D4 §6 "Reduced Motion" | Global motion: adopt the opt-in pattern. Restructure all animation `@keyframes` usages to live inside `@media (prefers-reduced-motion: no-preference)` blocks. This means zero animation by default; motion only when the OS explicitly says no preference (which defaults to "motion allowed"). This eliminates any risk of animating against user will. | Keep — refactor the motion primitives table in §3 to note this pattern. The static fallbacks then become the default rendering, with animation as enhancement. |
| **`will-change` lifecycle management** — `will-change: transform, opacity` applied only to actively animating elements, removed (`will-change: auto`) after the animation `animationend` event. Prevents unnecessary paint layer promotion. | D5 §3.2 | Global motion: add `will-change` lifecycle management to the implementation notes for the phase-advance, head-emergence, and seal-break animations. | Keep |
| **Budget flash keyframe for 100 % breach** — `@keyframes budget-alarm { 0%,100% { opacity:1 } 50% { opacity:0.4 } }` at 1.2 s infinite. Reduced-motion: no flash; static red bar + appended text "BUDGET EXCEEDED." | D4 §3 "Budget Tension" | Immortal Head budget band + workflow budget strand: apply when `--budget-pct >= 1.0`. The flash uses only `opacity` (safe for reduced-motion isolation: the flash is gated in `no-preference` block; the text label is always present). | Keep |
| **Serpentine nav underline — active route indicator** — the top navigation's active-section indicator is an SVG serpentine wave path that animates from one position to the next on route change via `stroke-dashoffset`. Not a straight underline; a subtle S-curve that echoes the creature's form. | D4 §1 "Serpentine Line" | Global nav / Immortal Head bar: the Immortal Head bar could carry a serpentine active-indicator on the nav items (Launchpad · Squads · Memory). Add a 1 px SVG `<path>` that draws between nav items on route change. Low cost, high character. | Adapt — keep the path simple (one curve cycle, not full sinusoidal); 300 ms ease on `stroke-dashoffset`. |

---

#### GLOBAL ACCESSIBILITY & a11y PATTERNS

| Element | Source | Where it lands | Keep / Adapt |
|---|---|---|---|
| **Parallel accessible ARIA tree beside the constellation** — the constellation SVG carries `aria-hidden="true"` at its root; beside it in the DOM sits a `<nav aria-label="Hydra constellation — workflow map">` with a live `<ul role="tree">` mirroring every head's state. Already promoted to "The Body rail" in the WINNER. This confirms the Body rail IS that parallel tree, not a separate shadow DOM structure. | D2 §5 "Accessible List/Tree Equivalent" | Body rail: confirm that the Body rail `role="tree"` IS the constellation's accessible equivalent. The constellation SVG root is `aria-hidden="true"`. The Body rail handles all AT interaction. No additional shadow structure needed. | Keep — eliminates ambiguity about whether there are two parallel structures or one. |
| **`aria-busy="true"` during loading on the tree root** — while the constellation/workflow data is loading, the nav tree root carries `aria-busy="true"` + sr-only "Loading Hydra constellation." Removed on data arrival. | D2 §5 8-state loading | Body rail loading state: implement `aria-busy` on the `<nav>` root. Pairs with the existing loading skeleton treatment. | Keep |
| **Named contrast pairs for every token** — D3 and D4 both provide explicit ratio tables. The WINNER's §2 states the palette but does not provide a complete pairwise table. | D3 §5.1, D4 §6 "Contrast Checks" | Implementation docs: compile a final consolidated contrast table from both sources. All pairs above 4.5:1 for text, 3:1 for UI components. Any pair used at small normal weight must be ≥ 4.5:1. | Keep — the table is a deliverable for the implementation phase; add it as §8.3 subset. |
| **`role="meter"` for budget strand** — the budget strand/bar carries `role="meter" aria-valuenow aria-valuemin="0" aria-valuemax aria-label="Budget: [pct]% of $[cap] consumed"`. | D1 §5.3 | Budget strand + Immortal Head budget band: apply `role="meter"` to both budget elements. | Keep |
| **Skip-to-content link as first focusable element** — a visually hidden but focusable skip link at the very top of the DOM: `<a href="#main-working" class="sr-only focusable">Skip to main content</a>`. | D4 §6 keyboard, D5 §5.2 | Global layout: add the skip link to the `<body>` before any visible UI. `id="main-working"` on the Working center `<main>` element. | Keep |
| **WCAG 2.5.8 minimum target sizes** — all interactive controls meet 24×24 CSS px minimum; workflow rows 48 px height; radio labels 32 px touch target via padding; nav links 44 px height via line-height. | D4 §6 "Target Sizes" | Global: adopt as a design constraint for all interactive elements. Enforce in the component implementation checklist. | Keep |
| **Phase node `aria-current="step"`** — the active phase node in the Living Workflow phase machine gets `aria-current="step"`. | D3 §5.4, D4 §5 | Living Workflow phase machine: apply `aria-current="step"` to the active phase dot/node. Already noted in the Living Workflow section above; listed here for completeness in the a11y audit. | Keep |
| **Localization string-ID inventory for core copy** — string IDs `spirit.placeholder`, `spirit.awaiting`, `gate.venomLabel`, `gate.approve`, `gate.reject`, `phase.names.*`, `sr.loading`, `sr.empty`, `sr.synthesis`. | D2 Appendix | Implementation: create `src/i18n/en-US.json` with these string IDs as the baseline. RTL handling: the constellation's radial layout is symmetric; panel text uses `dir="rtl"` on the panel root. Neck dash-offset direction (synthesis return) unchanged under RTL (semantic direction unchanged). | Keep |
| **Responsive matrix** — four breakpoints: `≥ 1440 px` (full 3-column + Phase Rail), `1024–1439 px` (constellation 80 vw + collapsible rail), `768–1023 px` (constellation fills viewport, panels overlay), `< 768 px` (Body rail as default list view; constellation opt-in toggle). | D2 Appendix responsive matrix | Global layout: adopt this breakpoint matrix. The `< 768 px` decision — Body rail list view as default, constellation as toggle — is the correct call (Hydra Cockpit is a power-user tool; at phone scale the list is the working interface). | Keep |

---

### 8.2 Deliberately dropped (and why)

| Element | Source | Reason for drop |
|---|---|---|
| D2 force-directed graph physics (D3-force sim) | D2 §3, implementation tech | Engineering and accessibility risk: physics layouts produce non-deterministic positions that break spatial memory and keyboard navigation. The deterministic radial is already the WINNER's decision. |
| D5 light parchment vellum theme (`#F5EDD6` substrate) | D5 §2.1 | Direct conflict with live-ops legibility and the established dark premium aesthetic. The dark theme is a non-negotiable for an ops console. |
| D1 full biological literalism (entire app = living organism, no grid at all) | D1 §1 | The organism metaphor is fully honored by the constellation and motion vocabulary. The spine-only single-axis navigation (abolishing all grid) goes too far for a multi-workflow overview; the constellation + Body rail serves the same spatial clarity with better scannability. |
| D3 triptych cathedral as the whole-app frame | D3 §2.5 layout | The three-column triptych with stained-glass jewel-tone washes and the Roman-numeral margin as the sole navigation is too fragile for dense operational data. The crown grouping (kept) is extracted without the full cathedral commitment. |
| D3 Roman-numeral margin-column navigation as the sole nav | D3 §2.5 | Six-character-wide nav is elegant but risks discoverability for new operators; the WINNER retains a conventional nav in the Immortal Head bar alongside the keyboard shortcuts. The Roman-numeral aesthetic is captured by the Cinzel label treatment. |
| D5 page-turn `rotateY(-180°)` view transition | D5 §3.1 | 3D page-turn is visually expensive and conflicts with the CSS/SVG-only motion discipline; also creates a disorienting spatial context switch inconsistent with the constellation's radial spatial model. |
| D5 `capital-breathe` / `capital-blink` on illuminated capitals | D5 §3.1 | The Pentecost Cockpit adopts creature-marks (the creature-mark reveal hover, kept) but not the full illuminated-capital frame with SMIL breathing/blinking. The SMIL lifecycle is harder to control for reduced-motion; the breathing is decorative without operational meaning. |
| D2 WebGL particle system for flames | D2 §2, §3 | The WINNER uses a 3-frame SVG flame sprite (§3 "head ignition"), avoiding the WebGL dependency. D2's WebGL particles are beautiful but conflict with the CSS/SVG-only motion discipline and add bundle weight. |
| D1 `feTurbulence` displacement map on vertebra bodies (morphing over time) | D1 §2.3 | The animated scale texture tile is kept for panels (low cost). Continuously morphing displacement on every phase node is GPU expensive and provides no operational information. |
| D4 venom green (`#4AFF91`) as the primary active color | D4 §2 palette | The WINNER's palette uses `--biolume: #00E5CC` for active states and `--spirit-amber` for the Spirit. D4's venom-green is visually strong but semantically confusing (in the WINNER, venom = crimson danger, not green active). Keeping D4's green would require renaming the venom semantic entirely. |
| D3 purple venom (`#8B2FC9`) for force-dispatch | D3 §2.1 palette | The WINNER assigns `--venom: #CC2200` (crimson) for venom-class actions. Purple conflicts. Crimson is kept; purple dropped. |
| D5 physical parchment grain PNGs at 12 % opacity on body | D5 §2.3 | The WINNER's SVG procedural `feTurbulence` tile achieves the material quality without a raster dependency or the light-parchment aesthetic. |
| D1 split-frame spine/right-rail as the only layout | D1 §2.5 | The three-column Pentecost shell (Body / Working / Oracle) supersedes D1's two-column split. D1's layout is the right structure for the Living Workflow view (which has a similar left-nav + right-panel pattern), and the phase spine directly informs the Living Workflow. But it is not the global layout. |

---

### 8.3 Consolidated contrast table (harvest from D3 + D4 named pairs, applied to WINNER palette)

| Foreground | Background | Ratio | Use | Passes |
|---|---|---|---|---|
| `--bone` `#F2EDE3` | `--void` `#09090F` | 17.0:1 | Primary text | AAA |
| `--bone` `#F2EDE3` | `--void-panel` `#0D0E16` | 15.2:1 | Rail / panel text | AAA |
| `--spirit-amber` `#F4A820` | `--void` `#09090F` | 8.3:1 | Spirit label, active node labels, focus outlines | AAA |
| `--bone-mid` `#A2A0B0` | `--void` `#09090F` | 5.8:1 | Metadata labels | AA |
| `--crown-forge` `#37C6E0` | `--void` `#09090F` | 9.1:1 | Forge head labels, neck lines | AAA |
| `--crown-garland` `#E0568C` | `--void` `#09090F` | 5.4:1 | Garland head labels | AA |
| `--crown-exec` `#C8922A` | `--void` `#09090F` | 4.7:1 | Executive head labels (large text / bold) | AA |
| `--gold-immortal` `#C9A84C` | `--void` `#09090F` | 6.1:1 | Immortal head sigil label | AA |
| `--biolume` `#00E5CC` | `--void` `#09090F` | 7.1:1 | Active phase fill text | AA + AAA |
| `--venom` `#CC2200` | `--void` `#09090F` | 4.6:1 | Gate text at large size (18 px+ / bold) only | AA large text |
| `--bone` `#F2EDE3` | `--covenant-indigo` `#161A2E` | 12.8:1 | Glass panel body text | AAA |
| `--spirit-amber` outline | `--void` `#09090F` | 8.3:1 | Focus indicator (WCAG 2.4.11, minimum 3:1) | Passes |
| `--venom` `#CC2200` border | `--void` `#09090F` | 4.6:1 | Venom border/icon (UI component, 3:1 required) | Passes |

**Rule inherited from D3:** `--crown-exec` (`#C8922A`) is NEVER used as normal-weight body text on `--void`. It is used only at large text (≥ 18 px Cinzel bold / ≥ 24 px) or as a border / UI component. Executive head label text uses `--bone` with crown-colored border/background.

---

### 8.4 New asset needs surfaced by the harvest

The following gpt-image-2 prompts are net-new relative to §6's IMG-1 through IMG-8. They are required by harvested elements (creature-marks, Phase Rail icon, IAU silhouette reference).

**IMG-9 · Creature-mark icon set (squad-head emblems)** — 1536×1024, transparent.
> A set of six naturalist-illustration creature emblems arranged in a 2×3 grid on pure black transparent background. Each emblem: 200×200 px cell, a single stylized creature rendered as precise monochrome line art (bone-white stroke, #F2EDE3, no fill) in the tradition of 16th-century natural history illustration — not cartoon, not heraldic: observational, scientifically composed. The six creatures: (1) a crowned serpent coiled upright, head raised — crown detail precise; (2) a salamander in a stylized flame, four-legged and alert; (3) a pelican in her piety, breast lowered, feeding young below — the classical "pelican in her piety" pose; (4) a great blue heron standing in water, watching; (5) a three-headed dog (Cerberus), three distinct head profiles, calm and heraldic; (6) a phoenix rising, wings spread, tail feathers fanning. Each creature isolated, centered in its cell, fine ink line weight, no color other than white line on black. No text, no labels, no borders around cells. Transparent background. UI icon set asset. Aspect ratio 3:2.

**IMG-10 · IAU Hydra constellation skeleton reference (layout guide)** — 1536×512, transparent.
> The IAU Hydra constellation skeleton — the largest constellation in the night sky — rendered as a minimal astronomical star-chart line drawing on transparent/black background. Approximately 13 key nodes (bright stars) connected by fine white lines in the traditional IAU constellation boundary style: dots for stars, lines for connections, NO labels or text anywhere. The constellation elongated horizontally, spanning the full width, slightly wavy — its characteristic sinuous shape. Bone-white (#F2EDE3) dots and lines on pure black transparent background. Precise astronomical shape, not decorative. To be used as a layout guide for the "all-idle" head placement in a radial UI. Aspect ratio 3:1.

**IMG-11 · Phase Rail compact strip (UI component background)** — 1024×128, transparent/dark.
> A sleek horizontal strip UI background: ultra-thin, 128 px tall, full-width proportionally. Background is deep indigo-black (#0D0E16) with a very subtle serpent-scale emboss texture visible only on close inspection, a 1 px top edge in spirit amber (#F4A820) at 30 % opacity, and two 1 px lateral side borders in the same amber at 15 % opacity. No text, no data, no UI elements — purely the container panel background. The bottom edge fades to transparent (20 px gradient fade). Glass-material quality. Dark background. UI asset for the Phase Rail overlay strip. Aspect ratio 8:1.

---

*End of §8 — Full Harvest & Integration Matrix.*
*Harvested: D1 = 10 elements | D2 = 9 elements | D3 = 9 elements | D4 = 10 elements | D5 = 8 elements. Total: 46 net-new elements integrated.*
*Dropped: 14 elements (reasons recorded in §8.2).*
*New image prompts added: 3 (IMG-9, IMG-10, IMG-11).*
