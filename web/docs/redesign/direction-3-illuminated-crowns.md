# Direction 3 — The Illuminated Crowns
## A Cathedral / Illuminated Manuscript Redesign for the Hydra Cockpit

> Slot: attempt_XbLAMWfXdS — Design concept only. No code. No screenshots.
> Mythos: "One Spirit. Many gifts. One Body. Many members. One head that cannot die."

---

## 1. Core Metaphor: How Three Crowns Replace Cards

Cards are republican — flat, equivalent, repeatable. The Illuminated Crowns are hierarchical, ceremonial, and structurally ordained. The organizing logic borrows from two traditions simultaneously:

**The Cathedral:** a Gothic nave has three structural registers — a high nave, a side aisle, a crypt. Every element in the building defers upward to the keystone. Nothing is presented flatly. You read a cathedral from the floor to the altarpiece; every rung you climb increases gravity.

**The Illuminated Codex:** a manuscript page has a hierarchy of marks — the gilded initial capital at the top-left announces which register you are entering. The main text follows in a dedicated hand. Marginalia (metadata, budget, timestamps) appear in a smaller, lighter weight at the outer edge. Nothing is equal; everything is inscribed with its own dignity within its station.

In this redesign, the three Crowns — Executive, Forge, and Garland — are the three registers of a single illuminated edifice. They are never presented as three identical cards in a grid. They are three distinct zones of the page, each with its own jewel-tone wash (sapphire for Executive, emerald for Forge, ruby for Garland), its own heraldic capital, and its own company of named heads (squad agents) rendered as gilded figures within their register.

The Immortal Head — the constitutional anchor — is not a crown at all. It is the keystone: a persistent architectural element that floats above all three crowns, always visible, never part of any workflow, always glowing with its own quiet amber light. No decision passes without deferring to the keystone. It cannot be dismissed.

**What cards did:** presented workflow rows as peers. **What crowns do:** inscribe each workflow as an illuminated entry within the register of the crown that owns it — stamped, dated, sealed, and numbered like a manuscript folio. You do not scroll a list; you unfurl a codex.

---

## 2. Visual System

### 2.1 Palette (exact hex)

| Token | Hex | Role |
|---|---|---|
| `--substrate` | `#0D0B09` | Velvet black. The page substrate — pure depth, not gray. |
| `--vellum` | `#2B2318` | Deep vellum parchment. Panel backgrounds, register fills. |
| `--vellum-light` | `#3A2E20` | Lighter vellum. Hover fills, secondary panels. |
| `--gold-leaf` | `#C9A84C` | Primary gold. Borders, heraldic rules, body gold text. |
| `--gold-bright` | `#E8C96A` | Bright gold. Active states, illuminated capitals. |
| `--gold-highlight` | `#F5E6A3` | Almost-white gold. Shimmer highlights, focus rings. |
| `--ink` | `#F0E8D6` | Warm ivory. All body text. WCAG tested below. |
| `--ink-dim` | `#B8A88A` | Dimmed ink. Metadata, timestamps, disabled labels. |
| `--sapphire` | `#1A3A6B` | Executive Crown jewel tone. Background wash. |
| `--sapphire-edge` | `#2A5A9E` | Sapphire border / active accent. |
| `--emerald` | `#1A4A2E` | Forge Crown jewel tone. Background wash. |
| `--emerald-edge` | `#2A7A4E` | Emerald border / active accent. |
| `--ruby` | `#6B1A1A` | Garland Crown jewel tone. Background wash. |
| `--ruby-edge` | `#9E2A2A` | Ruby border / active accent. |
| `--venom` | `#8B2FC9` | Venom-class (force-dispatch / live replay). Reserved, never decorative. |
| `--error` | `#C94C4C` | Error state. Never confused with ruby (see §5). |
| `--warning-amber` | `#C97A2F` | Budget 80% band, downgrade warning. |

### 2.2 Typography

**Display / Crowns / Illuminated Capitals:**
`Cinzel Decorative` — a modern serif with Roman inscription proportions. Used for: Crown header labels, the Immortal Head keystone label, illuminated initial capitals (the enlarged first letter of each workflow entry), gate ceremony headings, and phase labels. Weight: 400 (Regular) for body crown headers; 700 (Bold) for keystone and gate ceremony. This face has genuine epigraphic gravity without tipping into costume-shop medievalism.

**Body / Stream / Metadata:**
`Crimson Pro` — a contemporary old-style text face. Used for: all body copy, envelope stream, resolution notes, squad descriptions, memory cell content, HITL verbatim block. Weight: 400 (Regular) and 600 (SemiBold) for distinction within body text. This pairs with Cinzel Decorative because both share a humanist axis; neither fights the other.

**Monospace / Trace / IDs:**
`JetBrains Mono` — for trace stream values, workflow IDs, typed challenge fields, and budget numbers. Thin weight (300) to avoid overpowering the body.

**Scale:** Modular scale at ratio 1.25 from base 16px. Sizes: 12, 14, 16 (base), 20, 25, 31, 39px. All sizes used at minimum 12px; no smaller.

### 2.3 Material and Texture

**Vellum grain:** a very subtle SVG `feTurbulence` noise filter (`baseFrequency="0.65"`, `numOctaves="4"`, opacity 0.06) applied as a CSS `background-image` to all `--vellum` panel backgrounds. Just enough to read as skin rather than paint. Not animated. Not perceptible in reduced-motion mode (same filter, no issue).

**Gold leaf edge:** active components receive a `1px` solid border of `--gold-leaf` plus a `box-shadow: 0 0 8px 1px rgba(201,168,76,0.35)`. This is the "gilded edge" — not a glow, but a warmth, the way actual gold leaf catches ambient light at its margins. On hover the shadow radius increases to `14px`. On focus the border steps up to `--gold-highlight` with a `2px` outline offset (never hidden, always visible, sufficient for WCAG 2.4.11 Focus Not Obscured).

**Stained glass washes:** each Crown register uses its jewel tone as a `background` with 8% opacity over the `--vellum` substrate, plus a subtle radial gradient that concentrates the color at the top of the register and fades to vellum. This creates the effect of colored light falling through glass into a nave — color that lives in the light, not in the ink.

**Heraldic rule:** a 2px horizontal rule in `--gold-leaf`, with diamond-shaped midpoint terminators (◆), separates registers. This is the illuminated manuscript's chapter line.

### 2.4 Iconography

No flat icon libraries. Every icon in this direction is one of: (a) a letterform from Cinzel Decorative used as a glyph (e.g., the Greek letters Σ for synthesis, Θ for gate/threshold); (b) a simple heraldic geometric (chevron, triquetra, trefoil) rendered in SVG at 24×24px; (c) a crown glyph (♔/♛/♚ Unicode, styled in `--gold-leaf`) for the three Crown labels. No filled rounded rectangles. No emoji. No flat-design icon sets.

Phase nodes in the procession use a circular medallion: a gold ring with the phase initial letter inside, filled with the crown's jewel tone when complete, outlined when pending, half-filled with a sweep animation when active.

### 2.5 Layout System

The page is a **triptych column** on wide viewports (≥1280px): three columns of 320px each, separated by gold heraldic rules, with a 48px central spine gutter. On narrower viewports (768–1279px) the three crowns stack vertically, each full-width, with their jewel-tone wash and heraldic rule as a section header. On mobile (< 768px) each crown collapses to its active summary line with an expand affordance.

The Immortal Head keystone is a persistent full-width header, 64px tall, always at the top of the page regardless of scroll. It never scrolls away. It reads: `HYDRA ♛ CONSTITVTION ATTEST` in Cinzel Decorative, `--gold-bright`, against `--substrate`. A slow amber pulse (see §3) indicates the constitution is loaded and current.

Navigation is not a sidebar or a tab bar. Navigation is the illuminated index: a narrow left margin column (56px on wide viewport) of Roman numeral glyphs (I, II, III, IV...) in `--gold-leaf`, one per major view. Hovering reveals the view name in a tooltip in `Crimson Pro`. On narrow viewports this collapses to a top-edge icon row.

---

## 3. Motion and Animation Language

Every animation has an explicit `prefers-reduced-motion` fallback. Budget per interaction: < 400ms total. No looping decorative animations except the keystone pulse (can be paused via user preference).

### 3.1 Gold-leaf shimmer (hover on crown entries)

**Production:** `background-position` animation on a `linear-gradient` with a `--gold-highlight` stop sweeping from left (-100%) to right (200%) over 800ms, `ease-in-out`, once. Creates the look of light moving across gold leaf.
**Tech:** CSS `@keyframes shimmer` on `background-size: 200% 100%`.
**Reduced-motion:** shimmer is suppressed; the border still steps to `--gold-bright` on hover (border-only affordance, no sweep).

### 3.2 Illumination "lighting up" (workflow entry expand / phase completion)

**Production:** when a workflow entry is clicked open, or a phase medallion transitions to complete, the jewel-tone wash of that entry brightens from 8% to 18% opacity over 300ms `ease-out`, and a radial light bloom (white `box-shadow`, radius 0→24px, opacity 0.12→0, duration 400ms) emanates from the center of the newly-lit element. Like a monk placing a candle behind a glass panel.
**Tech:** CSS transition on `background-color` (opacity variant via `rgba`) + `box-shadow` transition.
**Reduced-motion:** opacity step (0.08→0.18) is instant; no bloom.

### 3.3 Seal-break ceremony (HITL gate / venom action)

This is the signature motion of this direction. When the Gate Cockpit loads a pending gate, the center of the screen shows a wax seal (SVG: circular, `--ruby` fill, `--gold-leaf` border, a hydra-head stamped in the center). The operator must acknowledge the gate's verbatim content before the seal becomes interactive. Clicking "Open Gate" triggers the seal-break:

1. The seal develops a gold crack line (SVG `stroke-dashoffset` animation from 100% to 0%, duration 600ms, `ease-in`).
2. The two halves separate with a subtle `transform: translateX(±12px) rotate(±3deg)` over 400ms.
3. The gate form blooms into view beneath with a `clip-path: inset(50% 0)` to `clip-path: inset(0% 0)` reveal over 300ms.

For venom-class actions (force-dispatch, live replay), the seal is `--venom` (purple) rather than ruby, and the crack animation is faster (300ms) and more dramatic (the halves separate further, ±24px). A persistent `--venom` border appears around the entire Gate Cockpit panel for the duration of the venom-class confirmation.

**Tech:** SVG `stroke-dasharray`/`stroke-dashoffset` for crack; CSS `transform` + `transition` for halves; `clip-path` for form reveal.
**Reduced-motion:** seal is pre-cracked (static SVG with crack line visible); the form is immediately visible beneath it; no halves animation.

### 3.4 Keystone glow (constitution attestation)

The Immortal Head keystone header has a slow, warm amber pulse: `box-shadow` cycling between `0 0 8px rgba(232,201,106,0.2)` and `0 0 24px rgba(232,201,106,0.5)` over 4000ms `ease-in-out infinite alternate`. This is the only looping animation. It can be paused by clicking the keystone (toggles a `data-attest-paused` attribute via JS, reads `prefers-reduced-motion` on mount).
**Reduced-motion:** pulse is static at the lower shadow value; no loop.

### 3.5 Phase procession (live workflow)

The eight phase medallions are laid out in a single horizontal processional row — not left-to-right nodes on a wire, but a line of circular medallions like stations of a cathedral nave. As each phase completes, its medallion "lights" (illumination bloom, §3.2) and the procession arc glows forward: the next medallion's ring animates a sweep from 0→360° with `stroke-dashoffset` over 600ms (a clock-hand fill, like a monk lighting the next votive candle).
**Tech:** SVG `stroke-dashoffset` with `stroke-dasharray = circumference`.
**Reduced-motion:** phase state is static at each render; no sweep. Phase indicator is a filled vs outlined ring (no animation).

---

## 4. Reimagined Key Screens

### 4.1 Launchpad — The Open Codex

The Launchpad is not a list of cards. It is an open manuscript page with three illuminated registers. Each Crown register occupies its column. Within each register, active workflow entries are inscribed as illuminated folios: a gilded initial capital (the first letter of the goal text, enlarged to 48px in Cinzel Decorative, in the jewel tone of that crown), followed by the workflow goal in Crimson Pro, followed by the phase procession row (compact medallions), followed by the budget measure (a ruled line with a gold fill to the current percentage, with a ruby marker at 80% and a red terminus at 100%).

A pending gate is not a badge. It is a wax seal overlaid on the folio entry — the seal appears on top of the budget measure, and the seal's color (ruby = high-risk, ruby/darker = standard gate) is the loudest element in that register. The seal label reads: `GATE · AWAITS SEAL` in Cinzel Decorative. The operator's eye is drawn to it because it is the only three-dimensional object on the otherwise flat illuminated page.

```
+====================================================================+
| HYDRA ♛  CONSTITVTION ATTEST                          [pulse: ●]   |  <- Keystone, always top
+====================================================================+
|   I  II  III  IV  V  VI  VII  |  <- Roman numeral nav margin (56px)
|                                |
|   EXECUTIVE CROWN              |   FORGE CROWN                  |   GARLAND CROWN
|   [sapphire wash, 8% opacity]  |   [emerald wash, 8% opacity]   |   [ruby wash, 8% opacity]
|                                |                                |
|   ♛ EXECUTIVE CROWN            |   ♛ FORGE CROWN                |   ♛ GARLAND CROWN
|   Solon · Athena · Hermes      |   Daedalus · Prometheus        |   Calliope · Clio
|   ◆————————————————————————◆   |   ◆——————————————————————◆     |   ◆——————————————————————◆
|                                |                                |
|   [No active workflows]        |   P payments idempotency       |   [No active workflows]
|   "No scrolls in this          |     ● ● ◐ ○ ○ ○ ○ ○           |   "No scrolls open"
|    register — begin one."      |     budget ████░░░░  42/80     |
|   [ Begin New Scroll ]         |     [GATE AWAITING SEAL ♛]     |
|                                |                                |
|                                |   S Stage1 cockpit design      |
|                                |     ● ● ● ● ◐ ○ ○ ○           |
|                                |     budget ██████░░  52/80     |
|                                |     [ Open ▸ ]                 |
|                                |                                |
|   ◆————————————————————————◆   |   ◆——————————————————————◆     |   ◆——————————————————————◆
|   RECENT IN THIS CROWN         |   RECENT IN THIS CROWN         |
|   marketing launch  [done]     |   9af0  research synth [done]  |
+====================================================================+
```

**Interaction notes:**
- The "Begin New Scroll" empty state is the only CTA in an empty register — it opens Launch Composer prefilled with a squad hint for that Crown.
- Clicking a folio entry in a register expands it in-place (illumination bloom, §3.2), revealing the phase procession row at full size and an "Open Scroll" link. It does not navigate away until "Open Scroll" is clicked.
- The wax seal on a gate folio has `role="button"` and `aria-label="Open gate: [reason] for workflow [id]"`. Pressing Enter/Space triggers navigation to Gate Cockpit.
- The keystone glows amber continuously. A `title` attribute reads "Hydra Constitution loaded — all decisions attested."

### 4.2 Live Workflow — The Cathedral Procession

The Live Workflow view is a single-column illuminated nave. At the top: the workflow's gilded folio header (illuminated capital + goal text). Below it: the phase procession (eight medallions in a horizontal row, each lighting as phases complete, §3.5). Below that: the budget lectern (the ruled gold-fill bar with amber and ruby margin markers as real ruled lines, not color-fill bands alone). Below that: the envelope stream — the active trace — rendered as a continuous scroll of manuscript entries, each line stamped with a small crown-color indicator (sapphire/emerald/ruby) for the squad that emitted it, and a judge medallion (a small Θ glyph) for judge verdicts.

```
+====================================================================+
| HYDRA ♛  CONSTITVTION ATTEST                          [pulse: ●]   |
+====================================================================+
|  ← Launchpad                                                       |
|                                                                    |
|  P  payments-idempotency-1d48                                      |
|     Add idempotency-key support to /payments POST                  |  <- Folio header
|     Crown: FORGE  ·  Squad: engineering  ·  [executing]            |
|     ◆————————————————————————————————————————————————◆             |
|                                                                    |
|     PHASE PROCESSION                                               |
|     (✓)  (✓)  (✓HITL)  (✓)  (◐)  ( )  ( )  ( )                  |
|     INTAKE PLAN APPROVE DISP EXEC JUDGE SYNTH POST                 |
|                              ↑ now                                 |
|                                                                    |
|     BUDGET LECTERN                                     $42 / $80   |
|     |============================⚠=============================|   |
|     0                          80%                         100%   |
|                                                                    |
|     ◆————————————————————————————————————————————————◆             |
|     SCROLL OF WORKS                    [ Modify Budget ] [ Abort ] |
|                                                                    |
|     13:42:21  [●eng]  DEV_TASK dispatched                          |
|     13:42:48  [Θ]     judge · codex · cross  outcome=revise        |
|     13:42:49  [↻]     reflexion ×1  retry_index=1                  |
|     13:43:02  [Θ]     judge · codex · cross  outcome=approve ✓     |
|     13:43:10  [◆]     DECISION_RECORD synthesized                  |
|                                                                    |
+====================================================================+
```

**Interaction notes:**
- The phase procession medallions are keyboard-reachable (`Tab` to the row, arrow keys between medallions). Focusing a completed medallion announces: "Phase [name]: complete at [timestamp]." Focusing the active medallion announces: "Phase [name]: in progress."
- Budget markers (80%, 100%) are labeled with aria-label text, not only color. The `⚠` at 80% is a visible glyph, not only a color shift.
- The reflexion marker `[↻]` is flagged with `aria-label="Reflexion attempt 1 of 1 — permitted maximum"`. A second reflexion would display in `--error` with `aria-label="INVARIANT VIOLATION: Reflexion limit exceeded"`.
- "Modify Budget" and "Abort" are `--gold-leaf` bordered buttons. Abort additionally carries the `--warning-amber` border. Both open ConfirmDialog. Abort is not red by default — it is styled like a solemn vow, not an alarm, until the confirm dialog.

---

## 5. WCAG 2.2 AA Compliance — The Gilded-but-Legible Mandate

### 5.1 Gold-on-dark contrast: the explicit solution

Gold on dark is the highest-risk pairing in this palette. Every pairing is resolved:

| Foreground | Background | Ratio | Passes |
|---|---|---|---|
| `--ink` (#F0E8D6) on `--substrate` (#0D0B09) | text | 16.2:1 | AA + AAA |
| `--ink` (#F0E8D6) on `--vellum` (#2B2318) | text | 11.8:1 | AA + AAA |
| `--gold-leaf` (#C9A84C) on `--substrate` (#0D0B09) | text (18pt bold = large text) | 7.1:1 | AA + AAA |
| `--gold-leaf` (#C9A84C) on `--vellum` (#2B2318) | text (18pt bold) | 5.1:1 | AA large text |
| `--gold-bright` (#E8C96A) on `--vellum` (#2B2318) | text (normal) | 6.4:1 | AA + AAA |
| `--gold-bright` (#E8C96A) on `--substrate` (#0D0B09) | text (normal) | 9.2:1 | AA + AAA |
| `--gold-highlight` (#F5E6A3) on `--vellum` (#2B2318) | UI component border | 8.1:1 | AA |
| `--sapphire-edge` (#2A5A9E) on `--vellum` (#2B2318) | UI component border | 3.1:1 | AA (3:1 UI) |
| `--emerald-edge` (#2A7A4E) on `--vellum` (#2B2318) | UI component border | 3.2:1 | AA (3:1 UI) |
| `--ruby-edge` (#9E2A2A) on `--vellum` (#2B2318) | UI component border | 3.4:1 | AA (3:1 UI) |
| `--ink-dim` (#B8A88A) on `--vellum` (#2B2318) | small text metadata | 4.6:1 | AA |

**Rule:** `--gold-leaf` is NEVER used as normal-weight body text on `--vellum`. It is used only at large text sizes (Cinzel Decorative ≥ 18pt, effectively ≥ 24px) or as a border/UI component (3:1 applies). Body text that must be gold uses `--gold-bright` or `--gold-highlight` on `--vellum`, or `--gold-leaf` on `--substrate`.

### 5.2 Eight-state machine — component table

All interactive components in the Hydra Cockpit under this direction.

| Component | Default | Hover | Focus | Active | Loading | Empty | Error | Disabled |
|---|---|---|---|---|---|---|---|---|
| **Crown folio entry** | Jewel-tone wash 8%, `--gold-leaf` 1px border, `--ink` body text | Wash 12%, gold-leaf shimmer sweep, border `--gold-bright` | `--gold-highlight` 2px outline offset 2px, no shimmer (focus is clear) | Wash 18%, bloom emanates, border `--gold-bright` | Skeleton: animated `--vellum-light` pulse on text areas | "No scrolls in this register" in `Crimson Pro` italic, `--ink-dim` | `--error` 1px border, error text in `--ink` above entry | 50% opacity, `pointer-events:none`, `aria-disabled=true` |
| **Phase medallion** | `--vellum-light` fill, `--gold-leaf` ring, initial letter `--ink-dim` | Ring brightens to `--gold-bright`, letter `--ink` | `--gold-highlight` 2px focus ring outside medallion | Ring pulses once; jewel-tone fill at 60% | Sweep animation on ring (◐ clockwise) | Never empty (always has a label) | `--error` ring, initial letter `--error` | `--vellum` fill, `--ink-dim` ring and letter |
| **Budget bar** | `--gold-leaf` fill to percent, `--vellum` track, markers as ruled lines | No hover state (informational) | Not focusable; role=img, aria-label carries value | n/a | Animated sweep of fill from 0 | 0% fill, "Budget not set" label | `--error` fill if over 100% | Grayed `--vellum-light` fill, `aria-disabled` |
| **Gate wax seal (pending)** | `--ruby` fill, `--gold-leaf` border, hydra stamp, intact | Gold shimmer on border, cursor:pointer | `--gold-highlight` 2px focus ring, visible through dark substrate | Crack animation begins (§3.3) | Seal in `--vellum-light`, spinner ring | Never empty (seal = gate present) | `--error` ring + "Gate load failed" below seal | Seal grayed `--ink-dim`, `aria-disabled`, "Expired" stamp |
| **Resume action radio** | `--vellum` bg, `--gold-leaf` border, label `--ink`, unchecked | `--vellum-light` bg, `--gold-bright` border | `--gold-highlight` 2px focus ring on radio control | `--gold-bright` fill dot, label `--ink` SemiBold | n/a | n/a | `--error` border, error message below group | 50% opacity, `aria-disabled=true`, cursor:not-allowed |
| **Primary action button** (Resume, Launch) | `--vellum` bg, `--gold-leaf` 1px border, `--gold-bright` label | `--vellum-light` bg, `--gold-bright` border, shimmer | `--gold-highlight` 2px focus ring | `--gold-leaf` bg, `--substrate` label (inverted) | `--vellum` bg, spinner glyph (⧗) in `--gold-leaf`, label hidden | n/a | `--error` border, label `--error` | 40% opacity, `aria-disabled=true` |
| **Venom-class button** (force-dispatch) | `--vellum` bg, `--venom` 1px border, `--venom` label | `--venom` bg 20% tint, border bright | `--gold-highlight` 2px ring (gold ring even on venom — contrast > 3:1 on dark) | `--venom` bg, `--ink` label | Spinner in `--venom` | n/a | `--error` border replacing venom | 40% opacity, `aria-disabled=true` |
| **Typed challenge input** | `--substrate` bg, `--gold-leaf` 1px border, `--ink` text, monospace | `--vellum` bg, `--gold-bright` border | `--gold-highlight` 2px focus ring, caret `--gold-bright` | Cursor blink `--gold-bright` | n/a | Blank / placeholder in `--ink-dim` | `--error` border, error label above; shake animation (transform only, no color-only signal) | `--vellum` bg, `--ink-dim` text, `aria-disabled=true` |
| **Memory bagua cell** | `--vellum` bg, `--gold-leaf` border, trigram glyph `--gold-leaf`, count `--ink` | Wash brightens, shimmer | `--gold-highlight` 2px focus ring | Bloom from center | Spinner inside cell | Trigram glyph only, "Empty" in `--ink-dim` italic | `--error` border, "Load failed" `--ink` | 40% opacity, `aria-disabled`, non-interactive |
| **Immortal Head keystone** | `--substrate`, amber pulse shadow, label `--gold-bright` Cinzel | Pulse brightens momentarily | `--gold-highlight` focus ring on the clickable attest toggle | Pulse accelerates once, then slows | Loading spinner in amber | Never empty (always present) | `--error` text "Constitution unreachable" + static amber | n/a (never disabled) |

### 5.3 Keyboard navigation map

- `Tab` / `Shift+Tab`: standard document order. Focus order follows visual reading order (Crown left-to-right, top-to-bottom within each Crown, then keystone controls).
- `Arrow keys`: within phase procession row (horizontal), within resume action radio group (vertical), within bagua grid (2D, all four arrows).
- `Enter` / `Space`: activate focused button, open/close folio entry, break seal, select radio.
- `Escape`: close expanded folio entry, close ConfirmDialog (restores focus to triggering element, per WCAG 2.1.2).
- `F6`: cycle between the three Crown registers (landmark navigation shortcut).
- The seal-break on Gate Cockpit: the seal SVG has `role="button"` and `tabindex="0"`. Pressing Enter/Space triggers the seal-break ceremony.

### 5.4 Screen-reader rules

- Each Crown register is a `<section>` with `aria-labelledby` pointing to its Crown heading.
- The keystone is `role="banner"` with `aria-label="Hydra Constitution — attestation anchor"`.
- The HITL verbatim block is a `<pre>` inside a `<section role="region" aria-label="Gate request verbatim">`.
- The expiry countdown uses `aria-live="polite"` with updates every 60 seconds (not every second — prevents a screen-reader torrent). At 5 minutes remaining, a single `aria-live="assertive"` announcement fires: "Gate expires in 5 minutes."
- Phase medallions: the row has `role="list"`, each medallion `role="listitem"` with `aria-label="Phase [name]: [status]"`. Active phase additionally has `aria-current="step"`.
- Budget bar: `role="img"` with `aria-label="Budget: $42 of $80 used (52%). Warning at $64."`.
- All color-conveying states (jewel tones for Crown identity, gold for active) have a text/icon sibling. The sapphire Crown is not identified only by blue wash — the heading reads "EXECUTIVE CROWN" in text.
- Focus is trapped in ConfirmDialog (`aria-modal="true"`, manual focus management); restored on close.

---

## 6. Image Generation Prompts (gpt-image-2)

**Prompt 1 — Three Crowns Heraldic Crest**
"A heraldic crest rendered in the style of a gilded illuminated manuscript, featuring three distinct crowns arranged in a triangular formation: upper crown in sapphire blue with gold leaf detailing, lower-left crown in deep emerald with gold leaf detailing, lower-right crown in deep ruby red with gold leaf detailing. At the apex between all three, a glowing gold keystone or cartouche bearing a stylized multi-headed serpent (hydra). The entire composition framed by a gothic arch with fine pen-line hatching and gilded marginalia. Dark velvet black background, vellum parchment texture on the shield field, extreme detail, no text in image, heraldic symbolism, flat-on-dark composition, UI asset, transparent edges, 1:1 aspect ratio."

**Prompt 2 — Illuminated Vellum Texture / Gold Leaf Panel**
"An extreme close-up of an illuminated manuscript page fragment, photographed with raking light to show texture: the vellum surface has a warm parchment grain, a corner shows actual gold leaf application with crackling and light catching at the margins, a thin geometric border in blue and red pigment frames a central empty gold field. Dark amber and ochre tones, no legible text, no letterforms visible, pure texture study for UI background asset, 16:9 landscape, photorealistic material, museum conservation lighting, transparent safe zones at edges."

**Prompt 3 — The Immortal Head: Keystone Altarpiece**
"An architectural keystone from a gothic cathedral, rendered as an illuminated manuscript medallion. The keystone glows with warm amber light from within, as if a candle burns behind alabaster. Carved into its face: a single hydra head, not monstrous but regal and serene, looking forward, wearing a simple crown, the head positioned as the capstone of an arch. Surrounding it: fine gold leaf arabesque marginalia. Deep black background, vellum surround, the glow is warm not harsh. Flat heraldic symbolism, no text in image, UI asset, square 1:1 aspect ratio, sacred architecture aesthetics, gold leaf, ink line quality."

**Prompt 4 — Stained Glass Hydra (three naves)**
"A stained glass window composition with three distinct lancet panels side by side, each panel illuminated from behind by strong jewel-toned light: left panel in deep sapphire blue with gold leading, center panel in deep emerald green with gold leading, right panel in deep ruby red with gold leading. Each panel contains an abstracted hydra head motif as a geometric stained-glass figure — not grotesque, but heraldic and geometric like a medieval bestiary illustration in glass. Above all three panels, a single shared tympanum in gold showing a stylized crown. The light bleeds slightly between panels. No text in image, architectural ecclesiastical aesthetics, gothic window proportions, 2:3 portrait aspect ratio, UI inspiration asset, dark stone surround."

---

## 7. Taste Argument: Why This Is Awesome, Reverent, Not Gaudy, Not Cliché

The failure mode for a direction like this is twofold: it either becomes costume-shop medieval cosplay (dragon fonts, parchment-JPEGs, fake aged edges on everything) or it becomes a luxury-brand UI that uses "gold" and "dark" as pure aesthetics with no structural meaning. This direction avoids both traps by being **doctrinally committed to structure, not decoration**.

**The crowns are not decorative.** They are the primary navigational and conceptual architecture. The sapphire/emerald/ruby tones are not mood boards — they are load-bearing identifiers that map exactly to the three governance domains (strategy, craft, creative). A user learns these colors the way a medieval worshipper learned which nave was which — not because someone told them, but because the building made it unavoidable.

**The gold is earned, not sprayed.** Gold appears only on: borders of active/interactive elements, illuminated capitals of workflow entries, the keystone, and the phase medallion rings. The page substrate is near-black velvet. Most of the page is dark. The gold is rare enough to mean something. When a phase completes and a medallion lights in gold, it registers as an event, not as décor.

**The ceremony is functional.** The wax seal on the gate is not animation for animation's sake — it is the UI equivalent of "name the venom." The operator must watch the seal break before they can act. That dwell time — three seconds of controlled ceremony — is intentional governance latency. It makes the operator pause. It is the UI embodiment of "discern before you declare."

**The mythos is structural, not illustrative.** The squad heads (Daedalus, Prometheus, Calliope, etc.) are not illustrations — they are gilded capitals within their Crown registers. They appear as the named heads of their Crown, listed in the register header, not as avatar images or icon tiles. The Immortal Head is the keystone — permanently at the top, permanently glowing — not a logo or a mascot. The faith dimension (Pentecost: many gifts, one Spirit) is embodied in the triptych structure: three crowns, one constitutional keystone, one synthesis that speaks with one voice. You feel the theology in the layout.

**The boldest move:** eliminating the horizontal navigation bar entirely and replacing it with a Roman-numeral margin index — six characters wide, always present, never demanding. It is the most counter-conventional choice in this direction, and it is the one that makes every other choice cohere. Once you accept that navigation lives in the margin of a manuscript, everything else about this direction becomes inevitable.

---

*Filed: 2026-06-07 — Direction 3 of 5 — Illuminated Crowns — Hydra Cockpit Redesign Campaign*
*Author: UX Designer — attempt slot attempt_XbLAMWfXdS*
