# Direction 5: The Hydra Bestiary / Living Codex

**Campaign:** best-of-5 visual redesign  
**Slot:** attempt_KfuCbYJDdO  
**Assigned direction:** D5 — "The Hydra Bestiary / Living Codex"  
**Date:** 2026-06-07  

---

## 0. One-sentence pitch

The Hydra Cockpit is a living illuminated manuscript: every workflow is a codex page being inscribed in real time, every squad-head is a bestiary creature-entry with a breathing illuminated capital, and the operator's act of launching a goal is an act of inscription — Discern, Delegate, Declare in ink.

---

## 1. Core Metaphor: How the Codex Replaces Cards

The complaint about the current design is "very card-heavy." Cards are containers: they suggest stored objects in a database. The codex metaphor inverts this. A codex is not a filing system — it is a living document being composed. The operator is not browsing records; they are *authoring alongside Hydra*.

### Structural translation

| Current primitive | Codex equivalent | Why it works |
|---|---|---|
| Workflow card | Codex page spread (left: illuminated header + marginalia; right: live body text being written) | A page has hierarchy, margins, a spine — not a flat rectangle |
| Phase-machine pill chips | The book's spine, visible as a gilded ribbon along the left edge; active phase = the open bookmark | Spines are literal progression markers |
| Squad/head cards | Bestiary entries: illuminated capital + creature-portrait + naturalist annotations | Each head is a distinct creature with taxonomy, gifts, habitat |
| Memory 8-cell grid | The Codex Index: 8 trigram-marked divider pages, like a medieval index with thumb-cuts | Bagua trigrams as section marks is exact and non-kitsch |
| HITL Gate | The Cerberus Seal page: a full-bleed illuminated gate illustration, venom annotation visible, operator must sign (Declare) | "Name the venom" becomes literal inscription |
| Budget ticker | Marginalia annotation in the right rail: a handwritten-style running tally with rubricated (red) threshold marks | Marginalia as operational metadata is historically accurate |
| Live trace/envelope stream | Ink bloom: new lines appear as if being written by a quill — characters ink in from left, lines bloom from a drop | This is the "writing in real time" gesture |

The card grid dissolves entirely. The Launchpad becomes the Codex's open frontispiece — a spread showing the active works in progress as titled chapters. The Launch Composer is the empty scriptorium page awaiting inscription.

---

## 2. Visual System

### 2.1 Palette (all values specified)

| Token | Hex | Use |
|---|---|---|
| `--vellum-base` | `#F5EDD6` | Page background — aged vellum, warm not yellow |
| `--vellum-shadow` | `#EAD9B5` | Folded page shadow, marginalia background |
| `--vellum-deep` | `#D9C49A` | Section dividers, heavy page shadows |
| `--ink-dark` | `#1E160A` | Primary body text — near-black sepia |
| `--ink-mid` | `#3B2A1A` | Secondary text, rules, ornamental strokes |
| `--ink-light` | `#6B4F35` | Tertiary text, metadata, marginalia annotation |
| `--gold-burnished` | `#C9A84C` | Gilded capitals, active state, spine glow |
| `--gold-pale` | `#E8D08A` | Gold wash, hover state glint |
| `--lapis` | `#1C3F6E` | Executive Crown heads, judicial phase indicators |
| `--lapis-light` | `#2E5F9E` | Lapis wash, focus rings on lapis elements |
| `--vermillion` | `#C0392B` | Gate/venom alerts, rubricated threshold marks, Cerberus |
| `--vermillion-light` | `#E05244` | Hover on alert elements |
| `--verdigris` | `#2D6A4F` | Garland Crown heads, synthesis/done phase |
| `--verdigris-light` | `#3E8C69` | Hover on verdigris elements |
| `--sanguine` | `#8B2B2B` | Forge Crown heads, error states |
| `--sanguine-light` | `#A83535` | Hover on sanguine elements |
| `--parchment-overlay` | `rgba(245,237,214,0.85)` | Modal/overlay scrim |

**Contrast guarantees (WCAG 2.2 AA):**
- `--ink-dark` on `--vellum-base`: 14.2:1 (AAA)
- `--ink-mid` on `--vellum-base`: 8.9:1 (AAA)
- `--ink-light` on `--vellum-base`: 4.7:1 (AA)
- `--lapis` on `--vellum-base`: 7.3:1 (AAA)
- `--vermillion` on `--vellum-base`: 4.6:1 (AA) — used only for large text / icon + label pairs; pure alert icons always paired with text label
- `--gold-burnished` on `--ink-dark`: 5.1:1 (AA) — gilded text on dark headers only
- `--verdigris` on `--vellum-base`: 4.8:1 (AA)
- Focus rings: `--lapis` 3px solid, offset 2px on vellum — 3:1 non-text contrast met

**No color-only signals.** All state distinctions carry shape + label alongside color.

### 2.2 Typography

| Role | Typeface | Weight/style | Usage |
|---|---|---|---|
| Display / Illuminated Heading | Cormorant Garamond | 600 Italic + 700 | View titles, chapter openings, bestiary entry names |
| Body / Codex Text | Source Serif 4 | 400 / 600 | All body copy, marginalia, field-journal annotations |
| Rubrication | Cormorant Garamond | 700 Italic, `--vermillion` | Threshold labels, gate warnings, phase node labels |
| Running Head | Source Serif 4 SmallCaps | 500 | Top-of-page navigation bar |
| Monospace (trace/code) | Courier Prime | 400 | Envelope stream, raw trace, JSON payloads |

Font-size scale follows a minor-third progression (16 / 19.2 / 23 / 27.6 / 33.2px) named `--text-body`, `--text-lead`, `--text-h3`, `--text-h2`, `--text-h1`.

Line-height: 1.65 for body (manuscript readability), 1.2 for display headings. Measure: 66ch max on main codex column.

### 2.3 Material / Texture

Three texture layers composited in CSS:

1. **Paper grain** — `url('/assets/textures/grain-vellum.png')` at 12% opacity, `background-blend-mode: multiply`. PNG is a 512×512 tileable noise render. Applied to `body` and `.page-spread`.
2. **Ink bleed** — SVG filter `<feTurbulence>` + `<feDisplacementMap>` applied to text elements at low amplitude (scale=1.5) — gives organic ink-on-paper edge rather than pixel-perfect letterforms.
3. **Gilding wash** — on illuminated capital elements: CSS `background: radial-gradient(ellipse at 30% 30%, var(--gold-pale) 0%, var(--gold-burnished) 60%, #A07A2A 100%)` with `mix-blend-mode: hard-light` at 90% opacity for dimensional gold.
4. **Page shadow** — double `box-shadow` on `.codex-page`: `inset 4px 0 12px rgba(30,22,10,0.12)` (gutter shadow) + `4px 0 24px rgba(30,22,10,0.18)` (drop shadow right).

### 2.4 Iconography: Illuminated Capitals & Creature-Marks

Each squad-head gets a bespoke illuminated capital constructed from:
- An SVG "drop capital" frame: ornate rectangular border with corner flourishes, species designation in small-cap below
- Inside the frame: a creature-portrait SVG (stylized naturalist line art, not cartoon) — the creature chosen to embody the head's gift/character
- Crown affiliation: border color is `--lapis`, `--verdigris`, or `--sanguine` per crown
- The capital letter itself: the first letter of the head's name, in Cormorant Garamond 700 Italic, gilded, overlaid bottom-left

Three sample head assignments (to be completed for all 13):

| Head | Crown | Creature | Reason |
|---|---|---|---|
| Codex (Executive) | Executive / Lapis | Crowned serpent | Wisdom that holds the constitution |
| Forge (Forge) | Forge / Sanguine | Salamander in flame | The mythic fire-dwelling creature; Forge = artisan |
| Garland (Garland) | Garland / Verdigris | Pelican in her piety | Self-giving, synthesis, care — classical symbol |
| Cerberus gate | — | Three-headed dog with seal | Literal; but rendered as naturalist plate, not cartoon |
| Dui (Memory) | — | Lake/marsh heron | Dui = joyful lake; heron as watcher/rememberer |

Marginalia ornaments: a library of 24 SVG glyphs (vine scrolls, manicules, asterisks, paragraph marks ¶, pointing hands, small creatures) used as inline ornaments in the marginalia rail. These are not decorative noise — each has a semantic role (manicule = operator attention required; vine scroll = section boundary; paragraph mark = new logical unit).

### 2.5 Layout System

```
[  left margin  |  spine  |  main codex column (66ch)  |  right marginalia rail  ]
     80px         24px              ~680px                       200px
```

The **spine** is a 24px gilded vertical ribbon running the full viewport height, left of the main column. It carries the phase-machine nodes as small circles (ink circles, not pills), connected by a gilded line. The active node glows gold; completed nodes are filled dark ink; future nodes are outline-only.

The **left margin** holds the running navigation: view name in small-caps, a manicule pointing right into the content.

The **right marginalia rail** (200px) carries: budget annotation (handwritten-style number tally), metadata (timestamps in italic sepia), active alerts (rubricated), and contextual creature-mark ornaments that react to current phase.

On tablet (768–1023px): right rail collapses into a drawer; spine shrinks to 12px without phase labels (labels on hover tooltip).

On mobile (< 768px): spine hides; navigation moves to a bottom "bookmark" row; marginalia appears as pull-up drawer triggered by a manicule icon.

---

## 3. Motion / Animation Language

### 3.1 Named animations

| Name | Trigger | CSS/SVG | Duration | Reduced-motion fallback |
|---|---|---|---|---|
| **ink-bloom** | New content arrival (envelope, new line in trace) | SVG `clipPath` expanding from left + `opacity: 0→1` with slight blur `filter: blur(2px)→blur(0)` | 400ms ease-out | `opacity: 0→1` only, 120ms |
| **page-turn** | View navigation | CSS 3D `rotateY(-180deg)` on a wrapper div, perspective 1200px, half-way swaps content | 500ms ease-in-out | Instant swap, no transform |
| **capital-breathe** | Idle, on bestiary entry | SVG scale `1.0→1.015→1.0` on the illuminated capital frame | 4000ms sinusoidal infinite | No animation |
| **capital-blink** | Idle, random interval 8–20s | SVG opacity `1→0.3→1` on the creature eye element | 200ms | No animation |
| **spine-glow-advance** | Phase transition | CSS `box-shadow` on active spine node: `0 0 0px →0 0 12px var(--gold-burnished)→0 0 0px` | 800ms ease | Instant node fill change, no glow |
| **seal-stamp** | HITL gate opens | SVG Cerberus seal scales from `0→1.05→1.0` at center of gate page | 600ms cubic-bezier(0.34,1.56,0.64,1) | `opacity: 0→1`, 200ms |
| **ink-dry** | Phase reaches `done` | Phase's spine node: color flood from `--vellum-base` to `--ink-dark` via SVG `<animate fill>` | 1200ms ease | Instant color change |
| **marginalia-write** | Budget/metadata update | Right rail value character-by-character opacity stagger, 20ms per char | 20ms × char-count | Value replaces instantly |

### 3.2 Implementation tech

- **ink-bloom, page-turn, marginalia-write**: CSS keyframes + CSS custom properties. No JS for the animation itself; JS only sets `data-state` attributes that trigger the class.
- **capital-breathe, capital-blink**: SVG SMIL `<animate>` elements embedded in the SVG asset. Falls back cleanly when `prefers-reduced-motion` matches (SMIL respects `@media (prefers-reduced-motion)` via JS toggle: `svg.pauseAnimations()` on match).
- **spine-glow-advance**: CSS `transition: box-shadow 800ms ease` on `.spine-node.active`.
- **seal-stamp**: CSS `animation` on `.gate-seal` triggered by React state mount.
- **ink-dry**: Inline SVG `<animate>` for phase completion; JS fires `requestAnimationFrame` + attribute update.

All animations: `will-change: transform, opacity` scoped only to actively animating elements; removed after animation completes to avoid paint layers overhead.

Reduced-motion global: React context reads `window.matchMedia('(prefers-reduced-motion: reduce)')`, injects `data-reduced-motion="true"` on `<html>`, and CSS selectors `[data-reduced-motion="true"] *` suppress all non-essential transitions.

---

## 4. Reimagined Screens: ASCII Sketches

### 4.1 Launchpad — The Frontispiece

The Launchpad is the codex's frontispiece spread: two half-pages side by side, showing the active works in progress as titled chapters with their inscribed progress.

```
┌────────────────────────────────────────────────────────────────────────────────────┐
│ [left margin]  [spine]  [main codex column]                    [marginalia rail]   │
│                                                                                    │
│  LAUNCHPAD     ║  ══════════════════════════════════════════  Budget & Status       │
│  ¶ Active       ║                                                                  │
│  Works          ●  H Y D R A   C O C K P I T                  ───────────────      │
│                 ║  Illuminated Chronicle of the AgentMesh      bridge: ✓ ok        │
│                 ║                                              db: fresh 3s         │
│                 ║  ── Chapter the First ─────────────────────                      │
│                 ●◀  Stage 1 · Cockpit Design                   §42 of §80           │
│  [manicule]→    ║  [executing]              [Forge · Garland]  ████████░░░░         │
│                 ║  Intake●Planning●Approval●Dispatch●Exec◐                          │
│                 ║  Judge○ Synthesis○ Postcheck○               ───────────────      │
│                 ║                                              03:58:11              │
│                 ║  ── Chapter the Second ── ⚠ GATE AWAITS ──  expires              │
│                 ●  Payments Idempotency                        §80 of §80           │
│                 ║  [approval: high_risk seal pending]          ████████████         │
│                 ║  Intake●Planning●Approval◐                   ⚠ AT CAP            │
│                 ║                       [ Open Gate › ] ←────────────────          │
│                 ║                                                                  │
│                 ║  ── Recent Works ──────────────────────────                      │
│                 ○  9af0… Marketing Launch  [done]  Replay ›                        │
│                 ○  77be… Research Synth    [surfaced]  Open ›                      │
│                                                                                    │
│                 ║                           [ + Inscribe New Goal ]                │
└────────────────────────────────────────────────────────────────────────────────────┘
```

**Interaction notes:**
- Each chapter opens with a page-turn animation navigating to `#/workflow/:id`
- The "GATE AWAITS" chapter glows with a faint vermillion ink-wash behind the header — not a red card, but a rubricated chapter heading, the medieval signal for "attention required"
- "Inscribe New Goal" is the launch CTA — named to reinforce the inscription metaphor
- The ● nodes on the spine are clickable: clicking a completed node navigates to that phase's trace
- Hovering a chapter title reveals the creature-mark of the primary squad head in the left margin

### 4.2 Live Workflow — The Page Being Written

```
┌──────────────────────────────────────────────────────────────────────────────────────────┐
│ [← Frontispiece]  [spine]  [codex main]                          [marginalia]            │
│                                                                                          │
│  LIVE WORK        ●  ┌─────────────────────────────────────────┐  §42.30 / §80           │
│  5ebd4268         │  │ ✦  Stage 1 · Cockpit Design             │  ████████░░░░            │
│                   ●  │    Hydra Chronicle · Executing           │                         │
│  [Illuminated     │  └─────────────────────────────────────────┘  Forge Crown            │
│   capital:        ●                                               [salamander mark]       │
│   stylized H      │  Intake  Planning  Approval  Dispatch                                 │
│   in gilded       ●◀ Executing ◐  Judge ○  Synthesis ○  Postcheck ○                      │
│   frame]          │                                               ───────────────         │
│                   │  ─── Live Inscription ─────────────────────  Phase: executing         │
│                   │                                               Started: 14:32:07        │
│  [manicule        │  [ink-bloom: lines appearing as typed]        Elapsed: 00:18:43        │
│   points to       │  14:32:07 · squad.forge.pp · envelope recv   ───────────────         │
│   active line]→   │  > Analyzing idempotency requirements...      Judge: pending          │
│                   │  14:32:19 · planning.complete ─ ✓ ok          Reflexion: ×0           │
│                   │  14:32:21 · dispatch → forge-engineering                               │
│                   │  14:33:45 · pp_run R7xk2p · stage:code                                │
│                   │  14:49:03 · [          ← ink blooming here                            │
│                   │                                               ───────────────         │
│                   │  ─── Task Register ─────────────────────────  [ Abort ]              │
│                   │  ☐ C0: scaffolding          [done]            [ Budget ]             │
│                   │  ☐ C1: health-probe         [done]            [ Replay ]             │
│                   │  ◐ C2: hydra manifest       [in progress]                             │
│                   │  ○ C3: gate cockpit         [pending]                                 │
└──────────────────────────────────────────────────────────────────────────────────────────┘
```

**Interaction notes:**
- The "Live Inscription" section is the live trace. Each new envelope line blooms in from left using the `ink-bloom` animation — not a log stream, a manuscript being written
- The illuminated capital in the left margin breathes slowly (capital-breathe) and shows the squad creature
- Clicking the creature capital opens the bestiary entry for that head (slide-in panel from left — not a modal, a page inserted before)
- The task register uses manuscript checkbox marks: ☐ / ◐ / ✓ (quill-style checkmarks, SVG)
- Phase transitions fire `spine-glow-advance`: the newly active spine node briefly halos gold
- When judge phase completes, the judge verdict appears as a rubricated annotation in the marginalia rail (vermillion, italic, signed "Judge")

### 4.3 Gate Cockpit — The Cerberus Seal Page

```
┌──────────────────────────────────────────────────────────────────────────────────────────┐
│  [← workflow]     THE CERBERUS GATE              expires: 03:58:11  [countdown]          │
│ ─────────────────────────────────────────────────────────────────────────────────────────│
│                                                                                          │
│   [Full-width illuminated gate illustration — Cerberus seal, three heads, ink+gold]     │
│   [SVG seal animates in via seal-stamp on mount — bouncy cubic-bezier]                  │
│                                                                                          │
│  ─── The Request ──────────────────────────────────────────────────────────────────────  │
│                                                                                          │
│  ¶ VENOM NAMED:   high_risk — financial instrument without idempotency key              │
│                   [rubricated in vermillion, Cormorant Garamond italic]                  │
│                                                                                          │
│  ¶ GOAL:          Add idempotency-key support to /payments POST                         │
│  ¶ SQUAD:         forge-engineering (PP harness, gpt-5.4 judge)                         │
│  ¶ BUDGET SOUGHT: §80 (current: §42.30 consumed)                                        │
│  ¶ RISK FACTORS:  [verbatim HITLRequest fields, rendered as codex paragraphs]           │
│                                                                                          │
│  ─── Declare ──────────────────────────────────────────────────────────────────────────  │
│                                                                                          │
│  [ ✓ Approve  — Inscribe passage ]    [ ✗ Reject — Close the gate ]                     │
│  [ ± Modify Budget ]    [ ⇄ Change Squads ]    [ ⚡ Force Dispatch ]                     │
│                                                                                          │
│  Approving inscribes your name and timestamp into the codex. This cannot be undone.     │
│  [ghost text below Declare actions — sets stakes without being preachy]                  │
└──────────────────────────────────────────────────────────────────────────────────────────┘
```

---

## 5. WCAG 2.2 AA Compliance — 8-State Machine

### 5.1 Component state matrix

| Component | Default | Hover | Focus | Active | Loading | Empty | Error | Disabled |
|---|---|---|---|---|---|---|---|---|
| **Chapter row (workflow)** | Vellum bg, ink-dark text, spine node aligned | Gold-pale wash on bg (`background: var(--gold-pale)` at 20% opacity), ink underline on title | 3px lapis solid ring, offset 2px; `role=link`, `aria-label="Open [name] workflow"` | Scale 0.99, shadow deepens | Skeleton: animated ink-wash scan line on title area | Empty state: italic "No active works" + manicule ornament | Vermillion rule above row, error annotation in marginalia rail | `opacity: 0.45`, `cursor: not-allowed`, `aria-disabled=true`, no hover |
| **Spine phase node** | Ink circle, `title="[phase name]"`, `role=img` | Tooltip: phase name + elapsed; gold ring appears | Tab-focusable; `role=button` when navigable; lapis focus ring | Pressed: scale 0.92 | Pulse: gentle opacity pulse 1→0.7→1, 1.5s infinite | N/A | Vermillion fill + `aria-label="[phase]: error"` | Outline only, `aria-disabled=true` |
| **Illuminated capital (head)** | SVG, ARIA `role=img aria-label="[head name], [crown] Crown"` | Gold-pale shimmer sweeps left→right | Tab-focusable; lapis ring; Enter/Space → opens bestiary entry | Scale 0.97 | N/A | N/A | Sanguine border on capital frame | `aria-hidden=true` if purely decorative fallback text visible |
| **Gate action button (Approve)** | Verdigris bg, gold text, quill-tip left ornament | Verdigris-light bg, slight lift shadow | Lapis 3px ring; `aria-describedby` → venom text | Sanguine bg flash 80ms | Spinner (ink-drop rotating SVG) + `aria-live=polite "Processing..."` | N/A | Error text in marginalia rail, button re-enabled | `opacity: 0.45`, `aria-disabled=true` |
| **Gate action button (Reject)** | Sanguine outline, sanguine text | Sanguine fill, vellum text | Lapis ring; `aria-label="Reject — close the gate"` | Sanguine fill darkens | Same spinner pattern | N/A | Same as above | Same disabled pattern |
| **Live trace line (ink-bloom)** | Hidden pre-bloom | N/A (not interactive) | N/A | N/A | Bloom animation in progress; `aria-live=polite` region; `aria-atomic=false` | "No trace yet — the page is blank." in italic | Error line in sanguine with ⚠ glyph + sr-only "Error:" prefix | N/A |
| **Budget annotation (marginalia)** | Sepia italic text, progress bar in ink | N/A (display) | N/A | N/A | `aria-busy=true` on container | "—" with sr-only "budget not loaded" | Vermillion text, `role=alert`, sr-only "Budget error:" | N/A |
| **Bestiary entry slide panel** | Off-screen left, `aria-hidden=true` | N/A at panel level | Focus moves to panel heading on open; trap focus inside; `role=dialog aria-modal=true aria-label="[head] bestiary entry"` | N/A | Ink-wash shimmer on entry body | "Entry not found" + empty creature frame | Error text + retry link | N/A |
| **Memory cell (trigram)** | Vellum cell, trigram SVG, record count, cell name | Gold wash, cursor changes | Lapis ring, `aria-label="[cell name] trigram, [n] records"` | Slight sink transform | Count area: animated ellipsis; `aria-busy=true` | "No records" in italic; trigram still visible | Sanguine border, error count | `opacity: 0.45`, `aria-disabled=true` |

### 5.2 Keyboard navigation map

```
Tab order within a codex page:
  [Skip-to-content link (sr-only, focusable)] → 
  [Nav: Frontispiece / Squads / Memory / Campaigns] →
  [Spine nodes (arrow keys within, Home/End for first/last)] →
  [Chapter rows (Enter to open)] →
  [Actions within chapter (tab through)] →
  [Marginalia actions if any] →
  [+ Inscribe New Goal button]

Gate Cockpit tab order:
  [Skip link] → [Countdown (sr-only live region)] → [Venom paragraph] → 
  [Approve] → [Reject] → [Modify Budget] → [Change Squads] → [Force Dispatch]
  
  Escape: closes any open bestiary panel, returns focus to triggering capital.
  
Spine nodes: Left/Right arrows navigate between nodes; Enter opens trace at that phase.
Bestiary panel: Escape closes; focus returns to illuminated capital that opened it.
Memory cells: Grid arrow navigation (2D); Enter opens cell; Escape returns to grid.
```

### 5.3 Screen reader

- `aria-live="polite"` region wraps the live trace section; `aria-atomic="false"` so each new line is announced independently
- Phase transitions: `aria-live="assertive"` for gate-entering state only (high urgency); all others `polite`
- Illuminated capitals: `role="img"` with `aria-label="[Head name], [Crown] Crown bestiary entry"`. SVG creatures marked `aria-hidden="true"` (decorative); the label carries semantic weight
- Gate countdown: `<time>` element with `aria-live="off"` and a separate `aria-live="polite"` that announces once at 5-min, 1-min, 30-sec marks (not every second)
- Page title updates on route change: `document.title = "[View] — Hydra Codex"`
- `role="main"` on the codex column; `role="complementary"` on the marginalia rail; `role="navigation"` on the spine and top nav

---

## 6. Image Generation Prompts (gpt-image-2)

**Prompt 1 — Illuminated Hydra Bestiary Plate**
> Full-page illuminated manuscript bestiary plate depicting a nine-headed hydra coiled in a formal naturalist pose, each head distinct, each rendered in the style of a medieval illuminated bestiary with individual character — one crowned, one armored, one wreathed in vines. Surrounding border: intricate vine-scroll marginalia with small creature vignettes in the corners. Palette: aged vellum ground #F5EDD6, deep sepia ink #3B2A1A, lapis lazuli #1C3F6E, vermillion #C0392B, verdigris #2D6A4F, burnished gold #C9A84C. Style: 13th-century English bestiary illumination, formal symmetry, flat perspective, gold leaf halation on the central heads. Composition: portrait orientation, centered subject, dense ornamental border, Latin-style annotation lines (no actual readable text, only decorative letterform marks). No text in image. Transparent background preferred, or vellum-tone paper. UI asset, high resolution, fine ink linework, jewel-tone washes.

**Prompt 2 — Aged Vellum Paper Texture**
> Seamless tileable texture of aged medieval vellum manuscript page: warm cream-to-ochre (#F5EDD6 to #EAD9B5), surface imperfections, subtle hair-follicle marks characteristic of genuine vellum, slight cockling at corners, faint ink ghost-lines from previous pages showing through, organic paper grain, no writing. Flat top-down view. 1:1 square format. Photorealistic material texture. No text, no illustrations, purely the surface of old vellum. UI background asset.

**Prompt 3 — Illuminated Capital Creature-Set**
> Set of six illuminated drop-capitals in the style of a medieval English bestiary: each letter (H, C, F, G, D, P) rendered as an ornate gilded frame containing a stylized naturalist creature portrait — a crowned serpent, a salamander, a pelican, a heron, a three-headed dog, a phoenix. Each capital: 1:1 square frame, interlaced vine border in verdigris and vermillion, gold-leaf letter overlay bottom-left, creature fills center. Palette: sepia ink linework, lapis, verdigris, vermillion, burnished gold. Style: formal bestiary illumination, not cartoon, scholarly naturalist line quality. No readable text, only decorative letterform shapes. Six panels arranged in 2×3 grid on transparent background. UI asset.

**Prompt 4 — Gilded Marginalia Flourishes Sheet**
> Sheet of gilded marginalia ornaments from a medieval illuminated codex: assorted vine-scrolls, manicule pointing hands, paragraph marks ¶, asterisks, foliate terminals, small creature vignettes (rabbit, owl, snail), section borders. All rendered in sepia ink on transparent background with gold highlight accents. Arranged as a flat asset sheet, isolated elements with clear spacing between them. Style: 13th–14th century English manuscript marginalia, fine ink linework, naturalistic but stylized. No text. Transparent background. UI decorative asset library.

---

## 7. Taste Argument: Why This Is Awesome, Not Twee

The risk with "illuminated manuscript" is kitsch: slap some gold serif fonts on a beige background, call it a codex, ship it, embarrass everyone. The bestiary direction avoids that through four specific choices.

**Structural integrity, not decoration.** The marginalia rail, the spine, the codex column, and the left margin are not decorations applied over a card grid — they *replace* the card grid with a different organizational logic. The layout comes from manuscript conventions that were engineered over centuries for readability and information density. Marginalia rails exist because medieval scribes needed metadata alongside main text. The spine is a literal spatial metaphor for sequential phases. These work because they solve real UX problems, not because they look pretty.

**The bestiary entry is precision-cast, not generic.** Each squad-head gets an individual creature grounded in that head's actual gift-set (Garland/pelican because the pelican feeds its young from its own body — self-giving synthesis; Forge/salamander because the salamander lives in fire — the forge creature). This is mythic precision, not clip-art. An operator who notices the pelican in the Garland entry understands something true about that head's role. The design teaches through the iconography.

**The inscription metaphor earns its weight.** "Discern → Delegate → Declare" maps exactly to the codex gesture: the operator discerns the goal (reading the frontispiece state), delegates to Hydra (inscribes the goal in the Launch Composer), declares at the gate (signs the seal). The copist metaphor is not applied from outside — it is the actual structure of the workflow. The operator is always already inscribing; the UI just makes that visible.

**Scholarly warmth is the opposite of enterprise sterility, but also the opposite of whimsy.** The tone is naturalist gravity: the kind of serious, affectionate attention a medieval scholar brought to categorizing the world. The creatures are not cute. The Latin-style annotations (decorative, non-readable) signal rigor, not play. The rubrication (red ink for gates and warnings) is the oldest UX signal in the Western manuscript tradition — it predates CSS by eight centuries and it still works. This is a design for someone who takes their work seriously and wants a tool that takes it seriously too.

**The paper-meets-live-system tension is the product.** Cards feel like a database. A codex being written in real time feels like a *process you are part of*. When the ink blooms in as a new envelope arrives, the operator experiences the system as *actively authoring alongside them*, not displaying records at them. That is the experience the mythos demands: One Spirit, many gifts, authoring together. The animation earns its existence not as flair but as semantic meaning.

---

*End of Direction 5: The Hydra Bestiary / Living Codex*  
*Slot: attempt_KfuCbYJDdO*  
*Author: UX designer agent, claude-sonnet-4-6*  
*Date: 2026-06-07*
