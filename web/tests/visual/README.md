# Hydra Cockpit — Visual Regression Baseline Harness

## Why this harness exists

The pp_harness `visual_regression` MCP tool is NON-FUNCTIONAL in this environment.
It produces 1-pixel / 68-byte PNGs because its Playwright backend has no provisioned
browser. This self-contained harness replaces it with a real Playwright + pixelmatch
pipeline that the operator runs directly.

---

## Prerequisites

1. Install the Playwright Chromium browser (one-time per machine, ~120 MB):

   ```
   npx playwright install chromium
   ```

   This downloads a pinned Chromium build into `~/.cache/ms-playwright/` (Linux/macOS)
   or `%LOCALAPPDATA%\ms-playwright\` (Windows). The browser binary is NOT committed.

2. The **bridge** and the **Vite dev server** must be running before you shoot:

   - Bridge: `npm run bridge` (listens on :8795)
   - Dev server: `npm run dev` (listens on :5185)

   Or set `VR_BASE` to point at any running instance (e.g. a staging URL).

---

## Generating baselines

```
npm run visual:baseline
```

Captures all six routes at 1512x900 and saves them to `tests/visual/baseline/`.
These PNGs are committed to git so every subsequent run has a known-good reference.

Run this whenever you intentionally change the cockpit's visual appearance. After
regenerating, review the diffs with a PNG viewer and commit the new baseline.

---

## Checking for regressions

```
npm run visual:check
```

1. Captures fresh screenshots into `tests/visual/current/` (ignored by git).
2. Pixel-diffs each against the committed baseline using **pixelmatch**.
3. Writes annotated diff PNGs to `tests/visual/diff/` (ignored by git).
4. Prints a per-route changed-pixel percentage and exits non-zero if any route
   exceeds the threshold.

---

## Routes captured

| Slug        | Hash route                          |
|-------------|-------------------------------------|
| `home`      | `/#/`                               |
| `launch`    | `/#/launch`                         |
| `squads`    | `/#/squads`                         |
| `campaigns` | `/#/campaigns`                      |
| `memory`    | `/#/memory`                         |
| `workflow`  | `/#/workflow/<VR_WORKFLOW_ID>`       |

Default workflow ID: `5ebd4268-5de0-4dbf-a82d-42c596d4818e`.
Override via `VR_WORKFLOW_ID=<uuid>`.

---

## Environment variables

| Variable        | Default                           | Description                                        |
|-----------------|-----------------------------------|----------------------------------------------------|
| `VR_BASE`       | `http://127.0.0.1:5185`           | Base URL of the running cockpit                    |
| `VR_WORKFLOW_ID`| `5ebd4268-5de0-4dbf-a82d-42c596d4818e` | Workflow UUID for the `/workflow/<id>` route  |
| `VR_THRESHOLD`  | `0.01`                            | Max allowed pixel-change ratio (1%) before failure |
| `VR_SETTLE_MS`  | `800`                             | Extra settle delay (ms) after network-idle         |

---

## Determinism: how animations are suppressed

The cockpit includes an `AmbientField` (drifting embers) and several CSS transitions
that would produce non-deterministic screenshots. This harness applies two layers of
suppression:

1. **`reducedMotion: 'reduce'`** on the Playwright browser context — Chromium honours
   the `prefers-reduced-motion: reduce` media query. The `AmbientField` component
   already checks for this and stops its RAF loop.

2. **Injected stylesheet** via `addInitScript` (before page load) and `addStyleTag`
   (after load, for CSSOM overwrites):

   ```css
   *, *::before, *::after {
     animation-duration: 0s !important;
     animation-delay: 0s !important;
     transition-duration: 0s !important;
     transition-delay: 0s !important;
     animation-iteration-count: 1 !important;
   }
   ```

3. A fixed **settle delay** (`VR_SETTLE_MS`, default 800 ms) after page `load`
   to allow React to flush any async paint.

   Note: `networkidle` is intentionally NOT used. The cockpit polls `/api/health`
   and `/api/workflows` every 8 seconds, so the network never goes idle. The
   harness uses `waitUntil: 'load'` + `waitForSelector('[data-testid="immortal-head-bar"]')`
   as the composite readiness signal instead.

The harness waits for `[data-testid="immortal-head-bar"]` before shooting — this
selector is always present; its absence means the app crashed before render.

---

## Threshold

Default: **1%** of pixels may change (`VR_THRESHOLD=0.01`).

This tolerates sub-pixel anti-aliasing differences across OS/GPU/font-rendering
combinations while catching genuine regressions (layout shifts, missing components,
colour changes, broken transitions).

Raise the threshold (`VR_THRESHOLD=0.03`) for routes with inherently dynamic content
(e.g. the workflow route if it shows live timestamps).

---

## Directory layout

```
tests/visual/
  capture.mjs       # the harness script
  README.md         # this file
  baseline/         # committed — reference screenshots
  current/          # gitignored — last "check" run captures
  diff/             # gitignored — annotated diff PNGs
```

---

## CI integration

Add to your CI pipeline after the bridge + dev server are up:

```yaml
- run: npx playwright install --with-deps chromium
- run: npm run visual:check
  env:
    VR_BASE: http://localhost:5185
    VR_WORKFLOW_ID: ${{ vars.TEST_WORKFLOW_ID }}
```

Exit code 0 = all routes within threshold. Exit code 1 = regression detected.
