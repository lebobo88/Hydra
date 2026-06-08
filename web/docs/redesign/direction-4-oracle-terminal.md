# Direction 4 — The Oracle Terminal (Premium Mythic Console)

**Attempt slot:** SCy7ntcRKT  
**Author:** UX Design Agent (direction-4 of 5)  
**Date:** 2026-06-07

---

## 0. Taste Argument (read this first)

This is the direction that ships. Not because it compromises — because it earns its iconography. The other four directions may hit harder aesthetically; this one hits hardest *operationally*. It asks: what if the Bloomberg Terminal was built by people who actually believed in the Pentecost? The answer is an operator console that treats every workflow as a sacred act of discernment — surfaces that feel like brushed obsidian, type that feels like a command carved into stone, and exactly one moment of pyrotechnics: the Cerberus venom gate, which earns its drama by being the only thing on screen that glows.

The Oracle Terminal is awesome and premium-not-gimmicky because its restraint is the argument. Every serpentine connector, every molten-gold accent, every coiling progress ring — these are *functional* decisions first, mythic decisions second. The motifs earn their place by doing work. This is the direction that a CTO would demo to a board and a developer would actually use at 2 AM.

---

## 1. Core Metaphor & Editorial Layout System

### The Metaphor

The operator does not "manage workflows." The operator *consults the Oracle* — presents a question, waits for the gestalt voice, and then exercises discernment. The UI is built around this ceremony:

1. **Intake** — the question is formed (Launch Composer as confessional booth, not form wizard)
2. **Deliberation** — the machine works (Live Workflow as watch face, not dashboard)
3. **Declaration** — the voice speaks (Synthesis panel as the singular output, not a results card)
4. **Judgment** — the operator decides (Gate Cockpit as altar, not approval form)

### Layout System: The Three-Column Editorial Console

Cards are abolished. The layout is a **vertical editorial system** with three persistent columns and a hero zone:

```
┌─────────────────────────────────────────────────────────────────────────┐
│  HYDRA                            [nav: Launchpad · Squads · Memory]    │
│  ─────────────────────────────────────────────────────────────── snake  │
│                                                                          │
│  ┌─ RAIL ──────┐  ┌─ MAIN ──────────────────────────┐  ┌─ ORACLE ────┐ │
│  │             │  │                                  │  │             │ │
│  │  Context    │  │  Primary operating surface       │  │  VOICE OF   │ │
│  │  column     │  │  — workflow trace                │  │  HYDRA      │ │
│  │  (squads,   │  │  — phase machine                 │  │             │ │
│  │   budget,   │  │  — envelope stream               │  │  Gestalt    │ │
│  │   metadata) │  │  — gate cockpit                  │  │  synthesis  │ │
│  │             │  │                                  │  │  always     │ │
│  │  18% wide   │  │  58% wide                        │  │  visible    │ │
│  │             │  │                                  │  │  24% wide   │ │
│  └─────────────┘  └──────────────────────────────────┘  └─────────────┘ │
└─────────────────────────────────────────────────────────────────────────┘
```

**The Oracle column** (rightmost, 24% width) is the hero. It is always visible. It renders the current Hydra gestalt — the synthesis of what the machine knows right now: the active workflow's declared intent, the live phase, the most recent judge verdict, and (when synthesis is reached) the DECISION_RECORD as delivered text. This column never shows cards. It shows *lines of voice* — justified, editorial, alive.

**The Main column** (58%) holds the primary operating surface: phase machine, envelope stream, or gate cockpit depending on route. Not cards — a structured vertical document.

**The Rail column** (18%) holds persistent context: active squad list, budget bar, cycle count. Minimal. Scrolls independently.

### Navigation: Serpent Line

The top navigation is a single horizontal line. No sidebar. No icon grid. Logo (wordmark only) left-anchored. Nav items center-spaced in monospace. A single hairline rule runs full-width below the header, breaking into a serpentine wave at the active section — an SVG path, not a decoration, it is the connective tissue. On route change it animates from one serpent position to the next.

---

## 2. Visual System

### Palette

| Role | Hex | Name | Usage |
|---|---|---|---|
| Ground | `#09090B` | Void | App background, the abyss |
| Surface-1 | `#0F1013` | Obsidian | Main column background |
| Surface-2 | `#141619` | Shale | Rail + Oracle column backgrounds |
| Surface-3 | `#1C1F24` | Scale | Hover state lift, subtle separation |
| Venom | `#4AFF91` | Venom | Active state, live phase dot, SSE pulse, gate-approved accents |
| Venom-dim | `#1A6638` | Venom-shadow | Venom glows, progress ring fill |
| Gold | `#C8922A` | Molten | High-risk affordances, budget band 80%, synthesis voice, selected radio |
| Gold-pale | `#E8B86D` | Bone | Body text on dark surfaces (4.7:1 against Obsidian) |
| Ash | `#8A909A` | Ash | Secondary labels, timestamps, metadata |
| Chalk | `#D4D8E0` | Chalk | Primary text, headings (12:1 against Void) |
| Venom-gate | `#FF3B3B` | Venom-red | Cerberus gate, budget 100% band, force-dispatch |
| Venom-amber | `#E8A020` | Ember | Budget 80% band, degraded state |

**Color is never the sole signal.** Every state that uses venom/gold/red also uses iconography, text label, or spatial position as a second channel. This is required for WCAG 1.4.1 and enforced in the state matrix below.

### Typography

**Display:** `"Cormorant Garamond"` (Google Fonts, variable, 300–700) — a high-contrast editorial serif with Renaissance authority. Used for headings, the Oracle voice panel, gate titles, synthesis text. Not decorative: it is the voice of something older than the interface.

**Mono/UI:** `"JetBrains Mono"` (variable, subset for UI) — precise, technical, unhurried. Used for all data: workflow IDs, phase labels, envelope stream lines, budget figures, timestamps, the typed-challenge field. Never mixed with Cormorant in the same visual block — they occupy separate tiers.

**Scale (rem, base 16px):**
- `--t-oracle`: 1.5rem / 1.4 leading / Cormorant 300 — Oracle voice lines
- `--t-heading`: 1.125rem / 1.3 leading / Cormorant 600 — section headings
- `--t-label`: 0.75rem / 1.0 leading / JetBrains 400 uppercase tracked 0.12em — field labels
- `--t-data`: 0.875rem / 1.6 leading / JetBrains 400 — envelope lines, IDs
- `--t-micro`: 0.6875rem / 1.0 leading / JetBrains 400 — timestamps, Ash color

### Material & Texture

**Scale texture:** A subtle repeating SVG tile (32×32px, ~800 bytes) of overlapping rhombus outlines at 3° opacity on Surface-2 backgrounds — suggests serpent scales without naming them. Applied via `background-image: url('data:image/svg+xml,...')` as a CSS pattern. Not visible at arm's length; felt in peripheral vision.

**Venom ink:** State transitions do not use fades. They use an ink-bleed effect: a radial gradient that expands from a seed point (the changed element's center) using a CSS `@keyframes` on `clip-path: circle()`. Duration 280ms, ease-out. The "ink enters the surface" idiom.

**Brushed metal rail:** The Rail column has a 1px right border in `#2A2D33` with a subtle vertical gradient from `#1C1F24` top to `#141619` bottom. No texture tile — the gradient is the material.

**Serpentine lines:** SVG `<path>` elements with `stroke-dasharray` and `stroke-dashoffset` animation (CSS or GSAP-lite). Phase connectors in the Live Workflow view are serpentine cubic bezier paths, not straight lines. The stroke animates from the left node's exit point to the right node's entry point as the phase completes.

### Iconography

A custom 24×24 serpentine line-set. No filled icons. All stroke, 1.5px, round-linecap. Key marks:

- **Phase dot:** concentric ellipse (not a circle — slightly oblate, like a scale)
- **Gate:** a closed-loop coil (snake consuming its tail / ouroboros, minimal — two arcs)
- **Venom:** a drop with a serif tail — not a skull, not a warning triangle — something old and specific
- **Live:** a small serpentine wave (2 cycles), animated in place
- **Gold (high-risk):** a fleur / flame — one central upright stroke with two curved strokes flanking it
- **Synthesis:** converging lines meeting at a point (many-to-one)
- **Budget:** a column of horizontal bars, the top bar shorter — a ledger

All icons available as React components (`<Icon name="gate" size={16} />`) with `aria-hidden` by default; when used alone, caller provides `aria-label`.

### Layout Grid

- **Max content width:** 1440px, centered
- **Columns:** Rail (18%) / Gutter (1.5%) / Main (57%) / Gutter (1.5%) / Oracle (22%) — sums to 100%
- **Base unit:** 8px
- **Section spacing:** `--space-section: 40px` between major blocks
- **Vertical rhythm:** all body text on a 24px baseline grid; headers on 32px

---

## 3. Motion & Animation Language

### Philosophy

One animation per moment. No ambient shimmer. Motion is reserved for: state change, phase transition, Cerberus gate event, Oracle voice assembly. Everything else is instantaneous with a 120ms ease-out opacity cross-fade (non-distracting, accessible).

### Coiling Progress Indicators

Phase dots in the Live Workflow phase machine are not filled circles — they are **coiling progress rings**: an SVG `<circle>` with `stroke-dasharray` animated to fill clockwise as a phase's envelope count grows toward its expected budget. When a phase completes, the ring completes its coil (360°) in 400ms ease-in-out, then the dot transitions to the venom-filled "complete" state via venom-ink bleed. 

Implementation: CSS custom property `--progress` on each ring element, updated by the SSE handler. The coil is `stroke-dashoffset: calc(157 - (157 * var(--progress)))` on a `r=25` circle. No canvas.

### Venom-Ink State Transitions

When a phase becomes active, errors surface, or a gate fires:

```css
@keyframes venom-ink {
  from { clip-path: circle(0% at var(--seed-x) var(--seed-y)); opacity: 0.7; }
  to   { clip-path: circle(150% at var(--seed-x) var(--seed-y)); opacity: 1; }
}
.venom-enter { animation: venom-ink 280ms ease-out forwards; }
```

`--seed-x` and `--seed-y` are set by the component triggering the transition (the phase dot's center coordinates). This creates the "ink enters from the changed point" effect. No JavaScript animation libraries required for the standard case.

**REDUCED-MOTION fallback:** `@media (prefers-reduced-motion: reduce)` collapses all transition durations to `1ms` and replaces `clip-path` animations with a simple `opacity: 0 → 1` cross-fade at the same timing. All semantic state changes still occur; only the kinematics are removed.

### Serpentine Connector Flow

When the Live Workflow phase machine renders and a phase completes, the SVG connector path between the completed phase and the next phase animates:

```
stroke-dashoffset: <path-length> → 0  (400ms, cubic-bezier(0.4, 0, 0.2, 1))
```

The path itself is a cubic bezier: `M x1,y1 C x1+40,y1 x2-40,y2 x2,y2` — a serpentine horizontal flow, not a straight line. When a workflow is done/surfaced, all connectors animate in sequence with 60ms stagger.

**REDUCED-MOTION:** connectors appear instantly (no dashoffset animation); path is drawn in full opacity.

### Oracle Voice Assembly

When the synthesis phase completes and the DECISION_RECORD arrives, the Oracle column animates the gestalt text in:

- Each line appears with a staggered 40ms delay (lines 1, 2, 3...) using `animation-delay`
- Each line slides from 8px below its final position to 0 (`transform: translateY(8px) → 0`) + opacity `0 → 1`
- Duration per line: 240ms, ease-out
- No typewriter effect (too slow, too cute) — the lines *arrive*, as if read aloud

**REDUCED-MOTION:** all lines appear simultaneously, no translate, just opacity cross-fade 120ms.

### Cerberus Venom-Gate Moment

The force-dispatch / venom-class gate is the one moment of pyrotechnics. The Gate Cockpit's `force-dispatch` radio selection triggers:

1. The gate cockpit border transitions from `--surface-3` to `--venom-gate` (1px → 2px, 200ms)
2. A `box-shadow: 0 0 0 1px var(--venom-gate), 0 0 32px 0 rgba(255,59,59,0.15)` appears (280ms ease-out)
3. The venom-ink animation fills the background of the gate cockpit section from the radio button center (360ms)
4. The typed-challenge field appears via venom-ink from the challenge label's position

This sequence is CSS-only (class addition triggers the cascade). No canvas. No JavaScript animation loop.

**REDUCED-MOTION:** border color changes instantly, box-shadow appears instantly, typed-challenge field appears via 120ms opacity cross-fade only.

### Budget Tension

The budget ticker bar uses a CSS gradient that updates via `--pct` custom property. At 80% the amber band appears via `background-image` change (no animation — instant, high-attention). At 100% the bar flashes using:

```css
@keyframes budget-alarm { 0%,100% { opacity:1; } 50% { opacity:0.4; } }
.budget-alarm { animation: budget-alarm 1.2s ease-in-out infinite; }
```

**REDUCED-MOTION:** no flash — bar changes to venom-red with a static text label "BUDGET EXCEEDED" appended.

---

## 4. Reimagined Views

### 4.1 Launchpad

```
╔═══════════════════════════════════════════════════════════════════════╗
║  HYDRA                         Launchpad · Squads · Memory     + Run ║
║  ─────────────────────────────────────────────────────────────────── ║
║                                                                       ║
║  ┌─ RAIL ──────────┐  ┌─ MAIN ─────────────────────────┐  ┌─ ORACLE ┐ ║
║  │                 │  │ ACTIVE                          │  │         │ ║
║  │  LIVE     2     │  │ ─────                           │  │ VOICE   │ ║
║  │  GATE     1     │  │                                 │  │ OF      │ ║
║  │  DONE    14     │  │ 5ebd4268                        │  │ HYDRA   │ ║
║  │                 │  │ Stage 1 cockpit design          │  │         │ ║
║  │  ─────────────  │  │ executing · eng                 │  │ Last    │ ║
║  │  SQUADS         │  │ ●●●●◐○○○  $42/$80              │  │ synth   │ ║
║  │  engineering    │  │ ─────────────────────────────── │  │ speaks  │ ║
║  │  executive      │  │                                 │  │ here in │ ║
║  │  research-ds    │  │ 1d48bb4d                        │  │ Cormo-  │ ║
║  │  +10 more       │  │ payments idempotency            │  │ rant    │ ║
║  │                 │  │ ⊙ GATE: high_risk  03:57:44 ▾   │  │ serif   │ ║
║  │  ─────────────  │  │ ─ ─ ─ ─ ─ ─ ─ ─ ─ ─ ─ ─ ─ ─  │  │         │ ║
║  │  BUDGET TODAY   │  │ open gate ▸             $80/$80 │  │ ─────── │ ║
║  │  $122 / $200    │  │                                 │  │ RECENT  │ ║
║  │  [████████░░░]  │  │ RECENT                          │  │ CONTEXT │ ║
║  │                 │  │ ─────                           │  │         │ ║
║  │                 │  │ 9af0  marketing launch   done   │  │         │ ║
║  └─────────────────┘  │ 77be  research synth  surfaced  │  └─────────┘ ║
║                       └────────────────────────────────┘               ║
╚═══════════════════════════════════════════════════════════════════════╝
```

**Key design decisions vs. cards:**

- Workflow rows are *document lines* not cards. No border-radius. No box-shadow. A full-width 1px `--surface-3` rule separates workflows — like ledger entries.
- The GATE row (`1d48bb4d`) is the loudest element — not through a colored card, but through spatial expansion: when a gate is pending, that workflow row expands to show the gate summary inline and the countdown in `--venom-gate` (red) monospace. It is the only element with a border accent (left 3px `--venom-gate` bar).
- Phase dots are the coiling progress rings described above. No labels on dots — hover reveals a tooltip `role="tooltip"` with the phase name.
- `+ Run` is a gold (`--gold`) text button, not a primary CTA card. Authority without decoration.
- The Oracle column on Launchpad shows the last completed synthesis from the most recent workflow — or "No synthesis yet" in Ash italic. It reminds the operator what Hydra last said.

**Interactions:**
- Click anywhere in a workflow row → `#/workflow/:id`
- Click "open gate" link → `#/gate/:hitl_id`
- `+ Run` → `#/launch`
- Row is keyboard-focusable (`role="row"`, `tabindex=0`); Enter/Space navigates
- Gate countdown uses `aria-live="assertive"` on the seconds field (updates every 10s to avoid over-announcement); full countdown string announced on mount

### 4.2 Live Workflow

```
╔═══════════════════════════════════════════════════════════════════════╗
║  ← Launchpad   WORKFLOW 5ebd4268   executing   ● live (SSE)           ║
║  ─────────────────────────────────────────────────────────────────── ║
║                                                                       ║
║  ┌─ RAIL ──────────┐  ┌─ MAIN ─────────────────────────┐  ┌─ ORACLE ┐ ║
║  │                 │  │ PHASE MACHINE                   │  │         │ ║
║  │  SQUADS         │  │                                 │  │ VOICE   │ ║
║  │  engineering    │  │  intake ──● planning ──● approv │  │ OF      │ ║
║  │                 │  │  ●done     ●done        ●done   │  │ HYDRA   │ ║
║  │  BUDGET         │  │      ╲serpentine╱              │  │         │ ║
║  │  $42 / $80      │  │  dispatch──◐ executing ○ judge  │  │ "The    │ ║
║  │  [████████░░░░] │  │  ●done      ◐active    ○wait   │  │ plan is │ ║
║  │  52%            │  │              ↑ now              │  │ sound.  │ ║
║  │  ─────────────  │  │                                 │  │ The     │ ║
║  │  JUDGE          │  │  ○ synthesis   ○ postcheck      │  │ risk is │ ║
║  │  codex (cross)  │  │  ○wait         ○wait            │  │ named.  │ ║
║  │  last: approve  │  │ ─────────────────────────────── │  │ Proceed │ ║
║  │  ─────────────  │  │ BUDGET  $42/$80  52%            │  │ with    │ ║
║  │  [Modify budget]│  │ [████████████░░░░░░░░░░░░░░░░░] │  │ discer- │ ║
║  │  [Abort]        │  │    ^80% downgrade  ^100% HITL   │  │ nment." │ ║
║  │                 │  │ ─────────────────────────────── │  │         │ ║
║  │                 │  │ ENVELOPE STREAM                 │  │ ─────── │ ║
║  │                 │  │                                 │  │ Assemb- │ ║
║  │                 │  │ 13:42:21  DEV_TASK  dispatched  │  │ ling... │ ║
║  │                 │  │ 13:42:48  judge codex  revise   │  │         │ ║
║  │                 │  │ 13:42:49  reflexion ×1          │  │         │ ║
║  │                 │  │ 13:43:02  judge codex  approve✓ │  │         │ ║
║  │                 │  │ 13:43:10  DECISION_RECORD →     │  │         │ ║
║  └─────────────────┘  └────────────────────────────────┘  └─────────┘ ║
╚═══════════════════════════════════════════════════════════════════════╝
```

**Key design decisions:**

- Phase machine is an inline horizontal document section — not a separate pane or modal. Phases are oblate dots connected by serpentine SVG paths. Completed phases show coil-completed rings. Active phase shows an animated partial coil in venom. Future phases show empty rings in Ash.
- The envelope stream is a `<pre>`-like mono log — no row borders, no table. A thin left margin with a venom vertical line marks the currently active phase's envelopes. The Reflexion ×1 marker appears as a parenthetical `(×1)` in Ash italic — subtle, but visible. A second reflexion (violation) would appear in venom-red with a `!` prefix.
- Budget ticker: a 100% width bar with CSS gradient. The 80% band boundary is marked by a short vertical tick in Ember. The 100% boundary tick is in venom-red. No labels cluttering the bar — hover reveals tooltip.
- Oracle column during execution shows "Assembling..." in Ash italic until synthesis phase completes, then the DECISION_RECORD arrives and the voice-assembly animation plays (lines slide in with 40ms stagger).
- `[Modify budget]` and `[Abort]` in the Rail are ghost buttons (1px border, no fill). They are the least visually prominent elements by design — power is not for casual clicking.

**Interactions:**
- Envelope stream auto-scrolls to tail while live; operator scroll-up pauses auto-scroll; a "↓ new events" pill appears in venom at bottom of stream (click resumes)
- Phase dots: keyboard-reachable in sequence; Enter opens a tooltip with phase details
- SSE error → polling fallback → `aria-live="polite"` announcement "Connection degraded, polling for updates"

---

## 5. Screen-State Matrix (8 States × Key Components)

| Component | default | hover | focus | active | loading | empty | error | disabled |
|---|---|---|---|---|---|---|---|---|
| Workflow row (Launchpad) | Document line, Chalk on Obsidian, left 0px border | Scale background lift (`--surface-3`), 120ms ease | 2px venom-green outline, offset 2px (WCAG 2.4.11) | Pressed scale (`scale: 0.998`), 60ms | Skeleton: 48px height, `--surface-3` animated pulse | "No workflows yet" Ash italic, "+ New Run" gold link | "Bridge error" inline banner, red left border, retry link | n/a (rows are always navigate-only) |
| Gate row accent | No accent | No change | Same focus ring | n/a | n/a | n/a | n/a | n/a |
| Gate row (pending) | 3px venom-red left border, countdown in red mono, row expanded | Surface-3 lift, red border unchanged | 2px venom outline, offset 2px | n/a | Skeleton pulse | n/a | Countdown shows "EXPIRED" in red; all actions disabled, `aria-disabled="true"` on all buttons | Row read-only when offline; "Resume offline — disabled" tooltip |
| Phase dot (ring) | Empty ring, Ash stroke, `aria-label="[phase]: pending"` | Tooltip appears: phase name + entry time, 200ms delay | Visible focus ring (2px gold offset 2px) when keyboard-navigating | n/a | Partial coil, venom stroke, animated | n/a | Error phase: ring fill red, `!` icon inside, `aria-label="[phase]: error"` | n/a |
| Phase dot (active) | Partial coil animating, venom stroke, pulse dot at tip | Tooltip: phase name + duration-so-far | Focus ring | n/a | n/a | n/a | n/a | n/a |
| Phase dot (complete) | Full coil, venom fill, `aria-label="[phase]: complete"` | Tooltip: phase name + duration | Focus ring | n/a | n/a | n/a | n/a | n/a |
| Serpentine connector | Ash stroke, `stroke-dashoffset` = full path length (invisible) | n/a (not interactive) | n/a | n/a | n/a | n/a | n/a | n/a |
| Serpentine connector (animating) | Dashoffset animating 0→full, venom stroke | n/a | n/a | n/a | n/a | n/a | n/a | n/a |
| Oracle column (voice) | Cormorant 300, bone color, 1.5rem, justified | n/a (not interactive) | n/a | n/a | "Assembling..." Ash italic, no animation | "No synthesis yet" Ash italic | "Synthesis unavailable" + error detail in mono | n/a |
| Budget ticker bar | CSS gradient left-fill via `--pct`; no text on bar | Tooltip: "USD used / cap (pct%)" | n/a (not interactive) | n/a | 0% fill, pulse animation on bar track | 0% fill, no pulse | n/a | n/a |
| Budget ticker (80%+) | Amber left-border tick, amber text cap label | Same | n/a | n/a | n/a | n/a | n/a | n/a |
| Budget ticker (100%) | Red tick, red bar fill, flashing (reduced-motion: static red) | Same | n/a | n/a | n/a | n/a | n/a | n/a |
| Resume action radio | 1px Ash circle, JetBrains label, Chalk | Gold border-color on radio circle, 120ms | 2px venom outline on label wrapper, offset 2px | Selected: gold filled circle, gold label | n/a | n/a | n/a | `opacity: 0.38`, `aria-disabled="true"`, pointer-events: none (gate expired or offline) |
| Resume action (default option) | Gold label, "(default)" suffix in Ash, gold circle border (not filled) | Gold fill on circle (preview) | 2px venom outline | n/a | n/a | n/a | n/a | Same as radio disabled |
| Typed challenge field | `--surface-2` bg, 1px Ash border, JetBrains, placeholder in Ash | `--surface-3` bg, Chalk border | 2px venom outline | Character typed: border Chalk | n/a | Empty on submit: venom-red border + "Workflow ID required" below, `role="alert"` | Mismatch: venom-red border + "ID does not match" below, `role="alert"` | `opacity: 0.38`, `aria-disabled="true"` (not force-dispatch context) |
| Force-dispatch radio + Cerberus gate | Standard radio appearance | Gold border preview | 2px venom outline | Selected: venom-gate red border + glow; background venom-ink fill animation; typed challenge appears | n/a | n/a | n/a | Disabled when gate expired |
| Resume button (Med risk) | 1px Chalk border, Chalk text, `--surface-1` bg | `--surface-3` bg, Gold text, Gold border | 2px venom outline, offset 2px | Scale 0.97, 60ms | Spinner (coil icon animated), disabled | n/a | Error toast inline: "Resume failed — [reason]", `role="alert"` | `opacity: 0.38`, `aria-disabled="true"` (offline or expired) |
| Resume button (High risk) | 1px Gold border, Gold text, `--surface-1` bg | Gold glow `box-shadow: 0 0 12px rgba(200,146,42,0.25)` | 2px venom outline | Scale 0.97 | Spinner, disabled | n/a | Same as Med + retry link | Same opacity+aria |
| Resume button (venom-class) | 1px venom-red border, venom-red text | Venom-red glow | 2px venom outline | Scale 0.97, border pulses once | Spinner, disabled | n/a | Same + Cerberus note | Disabled when gate expired |
| `+ New Run` button | No fill, no border — Gold text only, tracked mono | Underline, 120ms | 2px venom outline | Scale 0.98 | n/a | n/a | n/a | Ash text, no underline on hover (offline) |
| Envelope stream line | JetBrains `--t-data`, Ash timestamp, Chalk kind, Ash detail | Chalk background row highlight | Focus on row (keyboard nav): venom outline | n/a | Skeleton: two lines, pulse | "No envelopes yet — awaiting phase dispatch" Ash italic | SSE error inline: red icon + "Stream interrupted" + fallback notice | n/a |
| Reflexion ×1 marker | Ash italic `(reflexion ×1)` inline with the envelope line | Tooltip: "One reflexion allowed per stage" | n/a | n/a | n/a | n/a | Second reflexion (violation): venom-red `! ×2 INVARIANT VIOLATION`, `aria-live="assertive"` | n/a |
| Nav link | JetBrains mono, Ash | Chalk, 120ms | 2px venom outline | Chalk, scale 0.97 | n/a | n/a | n/a | n/a |
| Nav active link | Chalk, serpentine underline SVG animates in | n/a | Same focus ring | n/a | n/a | n/a | n/a | n/a |

---

## 6. WCAG 2.2 AA + Keyboard / SR-Only Plan

### Contrast Checks

| Pair | Ratio | Passes |
|---|---|---|
| Chalk (`#D4D8E0`) on Void (`#09090B`) | 13.8:1 | AA + AAA |
| Bone (`#E8B86D`) on Obsidian (`#0F1013`) | 7.1:1 | AA |
| Venom (`#4AFF91`) on Obsidian | 9.8:1 | AA + AAA |
| Gold (`#C8922A`) on Obsidian | 4.6:1 | AA (text) |
| Ash (`#8A909A`) on Void | 5.4:1 | AA |
| Venom-red (`#FF3B3B`) on Obsidian | 5.1:1 | AA |
| UI component: Venom outline on Obsidian | 9.8:1 | AA (3:1 required) |
| Ember (`#E8A020`) on Void | 6.3:1 | AA |

### Keyboard Navigation

```
Tab order per view:
  Global header: [Logo (skip-link target)] → [Nav links ×3] → [+ New Run]
  
  Launchpad:
    Workflow rows (tabindex=0, role=row) in source order →
    Within expanded gate row: [Open gate link] →
    End of list: [Load more] if paginated

  Live Workflow:
    Phase dots (tabindex=0) in phase order →
    [Modify budget] → [Abort] (Rail) →
    Envelope stream rows (tabindex=0, keyboard scroll with arrow keys) →
    Oracle column (tabindex=-1, not in flow — screen reader can reach via browse mode)

  Gate Cockpit:
    HITL verbatim block (role=region, aria-label="HITL Request") →
    Resume action radios (role=radiogroup, arrow key navigation) →
    Budget field / squad field (conditionally shown) →
    Resolution note textarea →
    Typed challenge field (conditionally shown) →
    [Cancel] → [Resume]

  Skip links: "Skip to main content" (first focusable in <head>-rendered portal, WCAG 2.4.1)
```

### Focus Management

- `ConfirmDialog` (typed-challenge modal): on open, focus moves to first form field. On close (Cancel or success), focus returns to the trigger button. `aria-modal="true"`, `role="dialog"`.
- Gate countdown expiry: when the countdown reaches zero, `aria-live="assertive"` announces "Gate expired. All actions disabled." All interactive elements inside the Gate Cockpit receive `aria-disabled="true"` and `tabindex="-1"`.
- SSE degraded notice: announced via `aria-live="polite"` on degradation; does not interrupt current task.
- Error states: use `role="alert"` for inline errors (field validation, resume failure). No screen-shake or visual-only indicators.

### SR-Only Text

- Phase machine: each phase dot has `aria-label="[phase name]: [status]"` (e.g., "executing: active")
- Serpentine connectors: `aria-hidden="true"` (decorative SVG)
- Coiling progress rings: `aria-hidden="true"` on the SVG; the phase dot's label carries the semantic
- Scale texture tiles: `aria-hidden="true"`
- Budget bar: `role="progressbar"`, `aria-valuenow`, `aria-valuemin="0"`, `aria-valuemax`, `aria-label="Budget: $42 of $80 (52%)"`
- Venom-gate glow effect: `aria-hidden="true"` on the glow pseudo-element; the radio label carries "force-dispatch (venom-class — Cerberus-gated)"
- Live indicator (SSE pulse): SR-only text `<span class="sr-only">Live connection active</span>` alongside the visual pulse dot

### Target Sizes

All interactive controls meet WCAG 2.5.8: minimum 24×24 CSS px hit area. Workflow rows: minimum 48px height. Radio labels: 32px minimum touch target via padding. Navigation links: 44px height via line-height.

### Reduced Motion

All animations gated behind:
```css
@media (prefers-reduced-motion: no-preference) { /* animation rules */ }
```
Default is no animation; opt-in to motion. This is the stricter interpretation and ensures zero animation for users who have not explicitly set a preference.

---

## 7. Image Generation Prompts (gpt-image-2)

**Prompt 1 — Serpent scale texture tile (surface material)**
> "A seamless tileable texture of overlapping geometric scales, each scale a slightly irregular hexagonal-rhombus facet with a subtle beveled edge. Ultra-dark background near-black (#09090B). Scales rendered in very subtle relief — only the edges catch light in dark ash tones. No color variation, no iridescence, no fantasy. Pure surface material, like matte obsidian or anodized metal with a scale-pattern emboss. Micro detail visible at 2x. UI asset, tileable 256x256px, transparent dark background, no text in image, no symbols. Lighting: single cold directional light from upper-left, very low contrast — barely-there texture."

**Prompt 2 — Venom and gold hero motif (synthesis panel background wash)**
> "An abstract editorial wash — ink dispersing into dark water. Two inks: a cold near-ultraviolet venom green (#4AFF91) and a warm molten gold (#C8922A). They meet at a vanishing point slightly right of center without mixing, each preserving its character. The background is near-black void. No geometry, no symbols, no text. The feeling is: two truths held in tension, luminous. Aspect ratio 3:1 landscape. Photographic quality ink dispersion, macro photography style, dark background, UI decorative asset. No text in image."

**Prompt 3 — Serpentine iconography sheet (functional icon set)**
> "A sheet of 12 minimalist serpentine line icons on a near-black background, arranged in a 4x3 grid with generous spacing. Each icon: 48x48px cell, 1.5px stroke weight, round linecaps, pure white stroke (#D4D8E0). Icons (labeled below each in small sans): phase-dot (oblate ellipse), gate (ouroboros coil, two arcs), venom (elongated drop with serif tail), live (2-cycle sine wave), synthesis (three lines converging to point), budget (ledger bars), squad (three small dots in triangle), replay (curved arrow with coil), warning (minimal angular mark — not triangle), check (serpentine checkmark), arrow-right (flowing rightward arc), connect (two points joined by cubic bezier). No fill, outlines only. No decorative borders. Dark background, UI asset sheet, no text labels in image itself."

**Prompt 4 — Cerberus gate moment (hero illustration for the venom-gate state)**
> "A single commanding graphic for an operator decision moment: a stylized closed gate rendered in architectural line work. Three vertical bars — three heads, three guardians — rendered as elongated geometric forms, not literal dogs. Cold dark background. The gate bars are edged in dim molten gold. A thin horizontal threshold line across the bottom. Above the threshold: venom-red light (#FF3B3B) seeps upward from below, as if seen through a crack — controlled, not explosive. No faces, no mythology literal, no text. The feeling: weight, consequence, deliberation. Aspect ratio 4:3. Dramatic chiaroscuro. Editorial illustration quality, line-work dominant, transparent/dark background."

---

## 8. Implementation Technology Summary

| Concern | Technology | Notes |
|---|---|---|
| Phase rings | SVG `<circle>` + CSS custom properties | No canvas; `--progress` updated by SSE handler |
| Serpentine connectors | SVG `<path>` + CSS `stroke-dashoffset` animation | Cubic bezier paths authored per phase pair |
| Venom-ink transitions | CSS `clip-path: circle()` keyframes | `--seed-x/y` set by component, no JS animation loop |
| Oracle voice assembly | CSS `animation-delay` stagger on flex children | Each line a `<p>` with delay index |
| Cerberus gate glow | CSS `box-shadow` + border-color transition + class toggle | One class addition triggers full cascade |
| Scale texture | SVG data-URI in `background-image` | ~800 bytes, cached |
| Budget ticker | CSS gradient + `--pct` custom property | React updates the property, no JS animation |
| Budget flash (100%) | CSS `@keyframes` infinite | Removed via reduced-motion media query |
| Serpentine nav underline | Inline SVG per active nav item | Animates on route change with `stroke-dashoffset` |

The entire motion system is CSS + SVG. No GSAP. No Framer Motion. No Canvas 2D. This keeps the bundle lean and the reduced-motion fallback complete.

---

## 9. Permission-Aware UX

For actions touching role × resource authority:

| Action | Risk class | Visible affordance | Condition for enable | Keyboard path |
|---|---|---|---|---|
| Launch (live) | High | Gold border button, "(live)" label, ConfirmDialog | `mode === 'live'` radio selected | Tab to button, Enter opens dialog |
| Approve | Med | Chalk border button, gold confirm nonce dialog | Gate not expired, online | Radio → Tab → Tab (resolution note) → Tab → Enter |
| Reject | Low | Chalk border button | Gate not expired | Same radio flow |
| Modify-budget | High | Gold border button, typed challenge in dialog | Gate not expired, `modify-budget` in gate options, online | Radio flow + budget field + dialog |
| Force-dispatch | Venom | Red border button, Cerberus note, typed challenge unconditional | Gate not expired, `force-dispatch` in gate options, online | Radio → venom-ink fill → typed challenge → Tab × 2 → Enter |
| Replay --live | Venom | Gold + venom note, ConfirmDialog with venom warning | Workflow done/surfaced | Memory view or workflow header |
| Tag memory | Low | Ghost button | Online | Tab to button, Enter |

All disabled states use `aria-disabled="true"` + `tabindex="-1"` + tooltip on focus explaining the reason. The tabindex removal is paired with a focus-visible sibling trigger so keyboard users are never lost.

---

## Appendix: Why This Direction is the Right Ship

The Oracle Terminal earns the mythos structurally. The *three-column editorial system* is Discern / Delegate / Declare made spatial: Rail (context) / Main (action) / Oracle (voice). The serpentine connectors do not decorate the phase machine — they *are* the phase machine's grammar, showing flow and completion as a single continuous gesture. The venom-ink transition is "name the venom" rendered as a UI primitive: dangerous state change arrives with weight and traceable origin. The Oracle voice panel is the gestalt made persistent — one synthesizing voice that is always present, never fragmented into cards.

The restraint is the argument for production-readiness. The only moment of pyrotechnics (Cerberus gate) earns its drama because everything else is controlled. A developer inheriting this codebase sees CSS custom properties, SVG paths, and `@keyframes` — no bespoke animation runtime to learn or debug. The design is implementable in one sprint by the same team that built the current card UI, and it will still be recognizably Hydra when the sixth view is added.

This is not fantasy mythology applied to enterprise software. It is enterprise software that takes its own principles seriously enough to let the design language *embody* them.
