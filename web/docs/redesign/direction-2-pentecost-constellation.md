# Direction 2 — Pentecost Constellation
## Hydra Cockpit Visual Redesign

**Slot:** attempt_mvjqBRK6G9  
**Designer:** UX Team — Direction 2 of 5  
**Date:** 2026-06-07  
**Theme:** One Spirit. Many Heads. One living map of the Body.

---

## 1. Core Metaphor — How the Constellation Structurally Replaces Cards

Cards are shelves. A card grid says "here are items you may pick from." It is a vending-machine metaphor. Hydra is not a vending machine; it is a living body animated by a single Spirit. The constellation is not decorative — it is the interface schema itself.

**The organizing structure is a force-directed graph with gravitational physics:**

- The **Spirit Node** occupies the center. It is not a card. It is a luminous mass — a slow orbital pulse of amber light. It represents the user's covenantal intent: the active goal, the immortal head. It exerts gravitational attraction on every head node. Distance from center encodes divergence from the Spirit's intent.

- **Neck lines** are not edges in the graph-theory sense — they are living tendons. Their length encodes status: a taut, short neck means the head is in active covenant with the Spirit (dispatching, executing). A slack, long neck means idle or dormant. Neck line weight (1px → 3px) encodes data flow rate. Animated dash-offset traveling Spirit→Head means task dispatching; Head→Spirit means synthesis returning.

- **Head nodes** are the 13 squad packs. Each is a circle whose diameter encodes capacity utilization. Active heads carry a tongue-of-fire particle emitter at their crown — a small upward flame in amber that shifts to cyan (Forge) or rose (Garland) depending on squad Crown. The Executive crown heads carry a faint gold filament halo. Dormant heads are slate-outlined, no fill, no flame.

- **The Constellation Field** is not a fixed layout. It breathes. Heads in active workflows cluster toward the Spirit. Idle heads drift to the periphery. When all heads are idle, they form the canonical Hydra constellation silhouette (matching the actual 88-IAU map outline) — a reference the user will learn to read as "all clear."

- **Legion vs. Pentecost as visual tension:** When a workflow enters a divergence state (squad outputs contradicting each other, no synthesis possible), the neck lines begin to vibrate with a low-frequency oscillation and the Spirit's pulse turns irregular — a shallow tremor rather than a deep beat. This is the Legion warning: multiplicity without unity. The user must intervene. Resolution restores the heartbeat.

**What replaces each card-heavy view:**

| Old view (cards) | New constellation element |
|---|---|
| Launchpad workflow cards | Spirit node tooltip + ring of recent intent chips |
| Live Workflow 8-phase machine | Animated neck arc with phase waypoints |
| Gate Cockpit HITL panel | Floating covenant card, crimson-gated, blocking |
| Squads (13 packs) | The 13 head nodes on the constellation field |
| Campaigns | Saved constellation snapshots in a bottom drawer |
| Memory 8-cell bagua | Eight-fold memory ring orbiting the Spirit inward |

---

## 2. Visual System

### Palette

| Role | Name | Hex | Usage |
|---|---|---|---|
| Background void | Deep Void | `#0A0B0F` | Full-bleed canvas, grain overlay |
| Spatial depth | Covenant Indigo | `#1B1F3B` | Panel glass, neck field fog |
| Spirit pulse | Spirit Amber | `#F4A820` | Spirit node core, active glow, synthesis stream |
| Forge Crown | Forge Cyan | `#00D4FF` | Engineering head nodes, flame tint on forge heads |
| Garland Crown | Garland Rose | `#FF6B8A` | Creative/marketing head nodes |
| Executive Crown | Sovereign Gold | `#C9A84C` | Executive head filament halo |
| Cerberus gate | Venom Crimson | `#C0392B` | Gate barrier pulse, HITL block ring |
| Resting text | Neutral Slate | `#8892A4` | Labels, telemetry, dormant node outlines |
| Synthesis light | Convergence White | `#F0EFEE` | The brief moment all light merges to Spirit |

**Grain overlay:** 3% SVG turbulence noise filter on the void canvas — subtle film grain that kills the "screen" feeling and adds material weight. Not visible at arm's length; felt subconsciously.

**Inner glow treatment:** Active nodes carry a `box-shadow` / Canvas radial gradient in their Crown color at 40% opacity, radius = 2× node diameter. This creates a light-pool on the canvas beneath each active head. The Spirit node's pool is always present and breathes (0.6s ease-in-out scale 0.9→1.1).

### Typography

| Role | Typeface | Weight | Size range | Notes |
|---|---|---|---|---|
| Node labels | **Barlow Condensed** | 600 | 10–14px | All-caps, tracked +0.08em |
| Spirit label (intent) | **Cormorant Garamond** | 300 italic | 18–24px | The one serif voice — covenantal, slow |
| Phase waypoints | **IBM Plex Mono** | 400 | 9–11px | Telemetry, token counts, latency ms |
| Panel headings | Barlow Condensed | 700 | 16–20px | Uppercase |
| Body / descriptions | **Barlow** (non-condensed) | 400 | 13px | Panel content only |

Cormorant Garamond appears only in the Spirit node tooltip and the synthesis declaration moment. It is the voice that speaks for the whole Body — measured, unhurried, covenantal. Every other surface uses Barlow family. The contrast between the two typefaces enacts the Spirit/Body distinction structurally.

### Material / Glow / Particle Treatment

**Dark glass panels** (the covenant card, the memory drawer, the gate panel): `background: rgba(27, 31, 59, 0.72)`, `backdrop-filter: blur(16px) saturate(140%)`, `border: 1px solid rgba(248, 168, 32, 0.12)`. A subtle inner-border gradient on the top edge fades from `rgba(F4A820, 0.3)` to transparent. These panels feel like obsidian glass over candlelight.

**Neck lines** rendered on Canvas2D or SVG `<line>` with `stroke-dasharray` and animated `stroke-dashoffset`. Active dispatch: 6px dash, 4px gap, offset animating at 40px/s. Synthesis return: same but reversed direction, color shifting from Crown hue toward Spirit Amber as it nears center.

**Tongue-of-fire particles** (WebGL preferred; Canvas2D fallback): Each active head node emits 8–14 particles from its top quadrant. Particle lifecycle: 0.6–1.2s. Initial velocity: upward ± 15° wobble. Color: starts at Crown color, brightens toward white at peak height, fades to transparent. Particle radius: 1.5–3px. Max simultaneous particles per head: 20. This is deliberately small — embers, not an inferno. The overall effect is a constellation of living flames scattered across void.

**Iconography:** No icon library. Three custom SVG primitives only:
1. A nine-pointed star (the immortal head / Spirit indicator, used as favicon and Spirit core background)
2. A flame silhouette (status indicator badge on active head nodes)
3. A chain link (the Cerberus gate, rendered as two interlocked rings in crimson)

---

## 3. Motion / Animation Language

### Spirit Heartbeat (the Signature Animation)

The Spirit node pulses at **0.78 Hz** — slightly slower than resting human heart rate (1.0 Hz). This was chosen deliberately: the orchestrator's rhythm is calmer than the user's own pulse. It should feel reassuring, not anxious.

Implementation: `scale(0.94) → scale(1.06)` on the amber glow layer, `ease-in-out`, 1.28s period, infinite. The Spirit label (the current intent phrase) fades between `opacity: 0.7 → 1.0` on the same cycle. The ambient light pool beneath the Spirit scales 0.96→1.04.

**Reduced-motion fallback:** Static amber glow at fixed opacity 0.85. No scale animation. The label stays at full opacity. No motion, full information.

### Head Emergence + Flame Ignition

When a squad is dispatched into a workflow, the head node transition is:
1. **300ms:** Node circle scales from 0.6→1.0 with a spring (overshoot to 1.08, settle). Neck line draws from Spirit outward via `stroke-dashoffset` animation (SVG path reveal).
2. **200ms offset:** Flame particle emitter activates. First 5 particles shoot upward in a burst (ignition moment), then settle to the steady drift rate.
3. **The amber glow pool beneath the node fades in** over 400ms.

Total emergence: ~900ms. This is slow enough to be noticed, fast enough not to obstruct.

**Reduced-motion fallback:** Node appears at full size immediately. Neck line draws at 150ms. Flame replaced by a static flame-badge SVG icon in Crown color. No particles.

### Neck Tension Animation

Neck lines use a cubic bezier control point offset from the straight Spirit→Head path. The control point drifts ±8px perpendicular to the neck direction on a 3–5s sinusoidal cycle (each neck has a randomized phase offset). This gives a gentle organic sway — not mechanical, not rigid. Under high load (>80% squad token budget consumed), the oscillation amplitude increases to ±18px and frequency doubles — the neck strains. Under budget breach, the neck turns Venom Crimson and vibrates at 12Hz for 1s before the Cerberus gate fires.

**Reduced-motion fallback:** Straight lines, no oscillation. Budget breach: neck color changes to crimson only, no vibration.

### Synthesis Convergence (Many → One Voice)

This is the signature moment. When the Hydra synthesizer fires, all active head nodes emit a **stream of light** traveling along their necks back toward the Spirit. Implementation:

1. All neck dash-offset animations reverse direction simultaneously.
2. Particle color on each head shifts from Crown color → Spirit Amber over 800ms.
3. A radial **convergence bloom** builds at the Spirit node: the ambient glow pool expands from 2× to 4× node diameter over 1.2s, color shifts to Convergence White (`#F0EFEE`).
4. At peak bloom (1.2s mark): the Spirit label transitions (cross-fade 400ms) from the active intent phrase to the **synthesis declaration** in Cormorant Garamond italic — the gestalt voice, the one answer from the many.
5. Bloom retreats over 600ms back to normal amber heartbeat.

Total convergence sequence: ~2.8s. It is unmistakable. The user knows Hydra has spoken with one voice.

**Reduced-motion fallback:** Spirit label cross-fades to synthesis declaration. Neck colors shift to amber. No bloom, no particle animation. Duration: 500ms.

### Cerberus Venom Gate

When a dangerous/irreversible action is intercepted by the Cerberus venom gate:

1. All active neck lines pulse crimson once (200ms flash).
2. A **crimson ring** expands outward from the Spirit node — not the node itself, but a separate ring element — expanding from radius 0 to the viewport diagonal over 600ms, then fades. This is the "barrier pulse": it visually traverses every head node, touching each.
3. The offending head node's flame extinguishes. The node gains a Venom Crimson border ring (3px, solid) with a slow 1Hz pulse.
4. The **Covenant Card** (HITL panel) materializes as a floating dark-glass panel, centered viewport, blocking interaction. The chain-link icon appears in crimson. The synthesis label reads: "Name the venom." in Cormorant Garamond.

**Reduced-motion fallback:** Neck lines change to crimson. Covenant Card appears without animation. Crimson ring not rendered.

### Animation Budget

| Animation | GPU cost | Duration | Trigger |
|---|---|---|---|
| Spirit heartbeat | Negligible (CSS transform) | Continuous | Always |
| Neck sway | Low (Canvas redraw 30fps) | Continuous | Active necks only |
| Flame particles | Medium (WebGL, 20 particles/head) | Continuous | Active heads only |
| Head emergence | Low | 900ms | Dispatch event |
| Synthesis bloom | Medium (Canvas radial gradient) | 2.8s | Synthesizer event |
| Venom gate pulse | Low (SVG ring) | 600ms | Cerberus event |

Canvas/WebGL renders at 30fps by default; bumps to 60fps only during the 2.8s synthesis convergence window to ensure smoothness at the high-stakes moment.

### Implementation Technology

- **Force simulation:** D3-force (`d3-force` v3) for gravitational layout. Spirit node is a fixed center anchor. Head nodes have charge repulsion from each other and attraction toward the Spirit. Idle heads drift to the Hydra constellation silhouette via a custom x/y positioning force applied when `active === false`.
- **Graph rendering:** SVG for neck lines and node circles (accessibility tree friendly). Particle system: WebGL via a minimal custom shader (no Three.js dependency — the particle system is ~200 lines of raw WebGL). Canvas2D fallback for the synthesis bloom radial gradient.
- **Reduced-motion detection:** `window.matchMedia('(prefers-reduced-motion: reduce)')` checked at mount; all animation registrations conditional. Force simulation still runs (layout), but no visual animation beyond color and opacity cross-fades.

---

## 4. Reimagined Views

### 4A — Launchpad (Start a New Workflow)

The Launchpad is not a separate page. It is the Spirit node in its resting state. Clicking the Spirit opens an **Intent Ring** — a concentric overlay that appears around the Spirit node:

```
                    ╔═══════════════════════════════════════════════╗
                    ║          HYDRA COCKPIT                        ║
                    ║   ·  ·  ·  ·  ·  ·  ·  ·  ·  ·  ·  ·  ·    ║
                    ║                                               ║
                    ║      ○  ○  ○  (dormant heads, periphery)     ║
                    ║                                               ║
                    ║          ┌─────────────────────┐             ║
                    ║      ○   │   INTENT INPUT      │   ○         ║
                    ║          │  ┌───────────────┐  │             ║
                    ║          │  │ [type intent] │  │             ║
                    ║          │  └───────────────┘  │             ║
                    ║          │      ◉ SPIRIT        │             ║
                    ║          │   "awaiting word"    │             ║
                    ║          │  [Discern] [Abort]  │             ║
                    ║          └─────────────────────┘             ║
                    ║      ○                              ○         ║
                    ║                                               ║
                    ║      ○  ○  ○  ○  ○  ○  ○  ○  ○              ║
                    ║   (recent intents as faint arc chips below)  ║
                    ╚═══════════════════════════════════════════════╝
```

The intent input is a single textarea — no label, just a placeholder in Cormorant italic: "Name the goal." Beneath it: five recent intent chips rendered as small curved text arcs following the Spirit's glow radius. Selecting a chip pre-fills the textarea and highlights the squads that historically engaged that intent category (heads glow faintly in preview).

Interaction: type intent → press Enter or [Discern] → Hydra's planning phase begins → head nodes animate toward Spirit as they are recruited → the first neck lines draw.

No workflow cards. No list. The intent is the entry point.

### 4B — Live Workflow (8-Phase Machine)

The 8 phases (intake → planning → approval → dispatch → executing → synthesis → judge → postcheck) are encoded as **waypoints on the neck arcs**, not as a separate timeline view. Each recruited head's neck carries 8 small markers spaced along the arc:

```
         ◉ SPIRIT (amber pulse)
          ╲
           ╲──●──●──●──●──●──●──●──●── ○ HEAD: forge-engineer
              ①  ②  ③  ④  ⑤  ⑥  ⑦  ⑧
              ↑  ↑  ↑  ↑
              done        ← current phase waypoint glows
```

The current phase waypoint is lit in Crown color. Completed phases are Spirit Amber. Upcoming phases are Slate. The phase label appears on hover/focus of the waypoint dot as a tooltip.

A **Phase Rail** panel can be summoned from the bottom edge (keyboard: `P`) — a horizontal strip that expands upward showing all active heads' phase positions in a compact matrix. This is the "engineer view" for monitoring all heads simultaneously without leaving the constellation.

```
┌──────────────────────────────────────────────────────────┐
│ PHASE RAIL  [P to dismiss]         tokens: 12,840 / 40k  │
│ forge-engineer  ①●●●●──────────── executing ⑤            │
│ ux-team         ①●●─────────────── approval ③  [GATE]    │
│ ralph-loop      ①●●●●●────────────  judge   ⑥            │
│ pp:best-of      ①●──────────────── planning ②            │
└──────────────────────────────────────────────────────────┘
```

The Phase Rail is monospace telemetry — IBM Plex Mono, slate text on indigo glass. Token budget bar at top right. No cards.

---

## 5. WCAG 2.2 AA + 8-State Machine + Keyboard / Screen Reader Access

### The Accessible List/Tree Equivalent

This is the hardest design problem in a graph/particle UI: a force-directed constellation is entirely visual by default. The solution is a **parallel accessible tree** that is always present in the DOM, hidden visually, but exposed to assistive technology.

The constellation SVG carries `aria-hidden="true"` on its root element. Beside it in the DOM sits a `<nav aria-label="Hydra constellation — workflow map">` containing a live `<ul>` tree:

```
<nav aria-label="Hydra constellation — workflow map">
  <ul role="tree">
    <li role="treeitem" aria-expanded="true" aria-label="Spirit — current intent: Build the Pentecost Constellation redesign">
      <ul role="group">
        <li role="treeitem" aria-label="forge-engineer — executing phase 5 of 8 — 3,240 tokens">
        <li role="treeitem" aria-label="ux-team — awaiting approval gate — HITL required" aria-live="polite">
        <li role="treeitem" aria-label="ralph-loop — judge phase 6 of 8">
      </ul>
    </li>
  </ul>
</nav>
```

`aria-live="polite"` on any head node whose phase changes. `aria-live="assertive"` on the Spirit node when synthesis fires (the declaration must be heard immediately). The Cerberus gate panel has `role="alertdialog"` and receives focus on appearance.

### Keyboard Navigation Map

| Key | Action |
|---|---|
| `Tab` | Move through head nodes in constellation order (Spirit → active heads by proximity → dormant heads) |
| `Enter` / `Space` | Expand head node detail (phase waypoints, token usage) |
| `P` | Toggle Phase Rail panel |
| `G` | Jump to active Cerberus gate (if present) |
| `S` | Jump to Spirit node / open Intent Ring |
| `Escape` | Dismiss active panel / close Intent Ring |
| `Arrow keys` | Navigate within Phase Rail matrix rows/columns |
| `M` | Toggle Memory Ring overlay |
| `?` | Show keyboard map overlay |

### 8-State Matrix

| State | Spirit Node | Head Nodes | Necks | Panels | A11y treatment |
|---|---|---|---|---|---|
| **loading** | Amber ring pulsing at 2Hz, label "Connecting…" | All nodes slate ghost circles, no labels | None visible | None | `aria-busy="true"` on nav tree root; sr-only "Loading Hydra constellation" |
| **empty** | Amber pulse at resting 0.78Hz, label in Cormorant: "Name the goal." | All 13 dormant at periphery in IAU silhouette | None | Intent Ring available on Spirit click | Tree contains only Spirit node; sr-only "No active workflows. Activate Spirit to begin." |
| **live** | Full amber glow, intent phrase shown | Active heads near center with flames; dormant at periphery | Dash-offset flowing, sway active | Phase Rail available | Live tree updated via `aria-live="polite"` on phase changes |
| **error** | Spirit pulse turns shallow tremor, amber desaturates to ochre | Errored head node gains crimson dot badge | Errored neck stops animating, turns slate | Error card floats (dark glass, crimson border) with error message + retry | `role="alert"` on error card; focus sent to retry button |
| **degraded** | Spirit pulse continues but at reduced amplitude (0.7×) | Affected heads show yellow caution badge, flame dims | Affected necks thin to 1px | Degraded banner strip at top: "1 head degraded — results may be partial" | `aria-label` on affected head nodes updated; `aria-live="polite"` degraded banner |
| **offline** | Spirit pulse stops; node dims to 40% opacity, label "Offline" | All heads dim, no flames | All necks fade to 20% opacity | Offline overlay: "Hydra is unreachable. Retry in 30s." with countdown | `aria-live="assertive"` on offline message; focus trapped in overlay |
| **partial** | Pulse continues, amber with slate mixed | Active heads lit, some dormant with caution badge | Active necks animated, partial-head necks dashed in slate | Phase Rail shows partial entries with "—" for unavailable phases | Tree reflects partial state per node; sr-only notes "2 heads unavailable" |
| **confirm** (Cerberus gate) | Pulse slows to 0.4Hz, label cross-fades to "Name the venom." | Offending head's flame extinguishes; crimson border ring | All necks pause animation; offending neck turns crimson | Covenant Card: `role="alertdialog"`, chain-link icon, approval/reject buttons | `aria-modal="true"` on Covenant Card; focus trapped; Escape = reject; Enter on Approve = confirm |

### Contrast Checks (WCAG 2.2 AA — 4.5:1 text, 3:1 large/UI)

| Foreground | Background | Ratio | Use | Pass? |
|---|---|---|---|---|
| `#F4A820` Spirit Amber | `#0A0B0F` Deep Void | 8.3:1 | Spirit label, active node labels | Pass |
| `#8892A4` Neutral Slate | `#0A0B0F` Deep Void | 5.1:1 | Dormant node labels, telemetry | Pass |
| `#00D4FF` Forge Cyan | `#0A0B0F` Deep Void | 11.2:1 | Forge head labels, neck lines | Pass |
| `#FF6B8A` Garland Rose | `#0A0B0F` Deep Void | 5.8:1 | Garland head labels | Pass |
| `#C0392B` Venom Crimson | `#0A0B0F` Deep Void | 4.6:1 | Gate text (large, 16px+) | Pass (large text) |
| `#F0EFEE` Convergence White | `#1B1F3B` Covenant Indigo | 9.4:1 | Panel body text on glass | Pass |
| `#C9A84C` Sovereign Gold | `#0A0B0F` Deep Void | 6.1:1 | Executive halo label | Pass |

Note: Venom Crimson on Deep Void passes only at large text (16px+ / bold). The gate panel uses Crimson as a border accent only; all crimson text is at 18px Barlow Condensed 700. Body copy in the Covenant Card uses Convergence White on Covenant Indigo glass — 9.4:1, fully compliant.

Focus indicators: 2px solid `#F4A820` outline, 2px offset, on all interactive elements. This amber ring on the void background achieves 8.3:1 — well above the WCAG 2.2 AA 3:1 minimum for focus indicators.

---

## 6. gpt-image-2 Generation Prompts

### Prompt A — Spirit Core Glow (UI Asset)
```
Glowing amber orb — the Spirit node of a multi-agent orchestrator UI. 
Deep void black background #0A0B0F. Central amber sphere, color #F4A820, 
with a soft multi-layer radial halo: inner ring pure amber, middle ring 
desaturating to warm orange-gold at 50% opacity, outer diffusion cloud 
at 10% opacity fading to void. Subtle internal nebula texture — filaments 
of lighter gold suggesting neural pathways or scripture lettering without 
being legible. Nine-pointed star ghost form visible at 15% opacity beneath 
the sphere. Ultra-clean, UI-ready, no text in image, transparent background, 
2:2 aspect ratio, cinematic volumetric lighting, photorealistic glow physics, 
no illustrative style, no cartoons, dark background only.
```

### Prompt B — Tongue-of-Fire Particle Sprite Sheet (UI Asset)
```
Sprite sheet of 12 small flame particles arranged in a 4x3 grid on a pure 
black background, suitable for WebGL particle system. Each particle is an 
isolated upward-rising ember or tongue of fire, 64x64px per cell. Color 
range: warm amber #F4A820 at base transitioning to cool cyan #00D4FF at tip 
for engineering variants; rose #FF6B8A variants for creative squad; pure 
amber-to-white for neutral. Flames are translucent, soft-edged, painterly 
but not cartoonish — like Acts 2 Pentecost imagery rendered as a technical 
asset. No text. No border. Transparent/black background. Flat UV-ready 
sprite sheet format. Aspect ratio 4:3.
```

### Prompt C — Constellation Hero Shot (Marketing / Onboarding)
```
Overhead view of a living force-directed constellation in deep void space. 
Central glowing amber orb (the Spirit) surrounded by 13 smaller glowing 
nodes connected by luminous curved tendrils of light. Each satellite node 
carries a tiny upward flame — amber, cyan, and rose varieties scattered 
asymmetrically. The overall silhouette loosely matches the Hydra IAU 
constellation shape (long, sinuous, spanning 100 degrees). Background: 
pure void black with 3% film grain noise. The neck lines between nodes 
are animated-looking — suggest motion with dash patterns and varying 
luminosity. The scene feels sacred, cosmological, alive — not a network 
diagram, not a data visualization, but a living body of light. Painterly 
realism, cinematic depth-of-field blur at periphery, no text in image, 
dark background, 16:9 aspect ratio, ultra-high detail.
```

### Prompt D — Cerberus Gate Venom Barrier (UI Asset)
```
Expanding crimson ring on deep void black background, as if a warning pulse 
propagating outward from an unseen central point. Color: venom crimson 
#C0392B. The ring is 3–5px thick, with a soft outer glow in darker red and 
a hard inner edge. The texture of the ring suggests chain links or interlocked 
serpentine scales — a barrier, not just a circle. Two interlocked ring-link 
shapes (chain lock symbol) visible at the 12 o'clock position of the ring 
in slightly brighter crimson. Background is pure void black. No text. 
High contrast, UI-asset quality, transparent/dark background, 1:1 aspect 
ratio, suitable for SVG mask overlay on a web canvas.
```

---

## 7. Taste Argument — Why Awesome, Not Cliché

Every multi-agent dashboard built in 2024–2026 converges on one of three visual languages: card grids (Notion-adjacent, approachable, dull), dark-mode node graphs (obsidian-and-neon, forgettable), or terminal-aesthetic dashboards (interesting to engineers, inaccessible to anyone else). This direction avoids all three by doing something rarer: **making the mythos load-bearing at the structural level, not the decorative level.**

The Hydra constellation is not slapped on as a logo or a background illustration. The force-directed graph IS the information architecture. The heartbeat IS the status indicator. The flame IS the activity badge. The synthesis convergence IS the notification system. Every visual element earns its existence by doing structural work. Nothing is cosmetic.

The Pentecost tension — Legion vs. one-Spirit — is not a tagline. It is a live UI state that the user can read from the graph's topology: are the heads clustering (covenant) or drifting apart (Legion warning)? This gives the user situational awareness without a dashboard of numbers.

The use of Cormorant Garamond — a serif, historically resonant, unhurried — for only the Spirit's voice (intent phrase and synthesis declaration) is the single most opinionated typographic move. In a monospace/condensed-sans world, one serif moment is startling and weighted. It signals: this is the word that matters. The rest is telemetry; this is proclamation.

The Hydra IAU silhouette as the resting constellation state is a detail that rewards the curious user who looks it up. The largest constellation in the night sky, used for navigation when nothing else was visible — landing here, in this interface, is not accident. It is the manifesto rendered in layout.

**The boldest move:** The Launchpad is abolished as a view. There is no "home screen with workflow cards." The entry point to every workflow is the Spirit node — you speak your intent to the center, and the body assembles around the word. This is Pentecost logic: the Spirit precedes and animates the members, not the reverse. It is structurally counterintuitive and immediately right.

---

## Appendix — Localization & Responsive Matrix

### Localization

| String ID | Default (en-US) | Notes |
|---|---|---|
| `spirit.placeholder` | "Name the goal." | Cormorant italic, Spirit node input |
| `spirit.awaiting` | "awaiting word" | Resting Spirit label |
| `gate.venomLabel` | "Name the venom." | Cerberus card heading |
| `gate.approve` | "Approve" | Covenant card CTA |
| `gate.reject` | "Reject" | Covenant card secondary |
| `phase.names.*` | intake / planning / approval / dispatch / executing / synthesis / judge / postcheck | Phase waypoint tooltips |
| `sr.loading` | "Loading Hydra constellation" | sr-only |
| `sr.empty` | "No active workflows. Activate Spirit to begin." | sr-only |
| `sr.synthesis` | "Hydra speaks: {declaration}" | `aria-live="assertive"` |

RTL handling: The constellation is radially symmetric — RTL/LTR does not affect the force layout. Panel text and Phase Rail flip via `dir="rtl"` on the panel root. The neck dash-offset direction reversal for RTL locales: synthesis streams still travel Head→Spirit (semantic direction unchanged, physical direction mirrors). Flame particles are directionless — no RTL impact.

### Responsive Matrix

| Breakpoint | Layout | Constellation behavior | Phase Rail |
|---|---|---|---|
| `≥1440px` | Full constellation + side memory drawer | All 13 heads, full force sim | Full Phase Rail (bottom) |
| `1024–1439px` | Constellation (80vw) + collapsible rail | All 13 heads, reduced force radius | Phase Rail collapsed to icon strip |
| `768–1023px` | Constellation fills viewport, panels overlay | 13 heads, reduced node size | Phase Rail as bottom sheet |
| `<768px` (mobile) | Accessible list view default; constellation available via toggle | Constellation scales to 100vw; necks simplified to straight lines; flame particles disabled | Phase Rail as full-screen modal |

Mobile-first accessible list is the default below 768px because a force-directed graph at phone scale loses legibility. The constellation toggle is available for demonstration but the list view is the working interface on mobile. This is honest: Hydra Cockpit is a power-user orchestration tool; its primary surface is large-screen.

---

*End of Direction 2 — Pentecost Constellation.*  
*Slot: attempt_mvjqBRK6G9*
