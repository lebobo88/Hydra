/**
 * Hydra Cockpit — Visual Regression Baseline Harness
 *
 * Usage:
 *   node tests/visual/capture.mjs baseline   # capture fresh baselines
 *   node tests/visual/capture.mjs current    # capture current screenshots
 *   node tests/visual/capture.mjs check      # capture current + diff against baselines
 *
 * Prerequisites:
 *   npx playwright install chromium
 *   Bridge (:8795) and Vite dev server (:5185) must be running, OR set VR_BASE env.
 *
 * Environment variables:
 *   VR_BASE        — base URL, default http://127.0.0.1:5185
 *   VR_WORKFLOW_ID — workflow id for /#/workflow/<id> route,
 *                    default 5ebd4268-5de0-4dbf-a82d-42c596d4818e
 *   VR_THRESHOLD   — pixel-change ratio threshold for "check" mode (0.0–1.0),
 *                    default 0.01 (1%)
 *   VR_SETTLE_MS   — settle delay after network-idle, default 800ms
 */

import { chromium } from '@playwright/test';
import { createReadStream, createWriteStream, existsSync, mkdirSync, readdirSync } from 'node:fs';
import { readFile, writeFile } from 'node:fs/promises';
import { fileURLToPath } from 'node:url';
import path from 'node:path';
import { PNG } from 'pngjs';
import pixelmatch from 'pixelmatch';

// ---------------------------------------------------------------------------
// Configuration
// ---------------------------------------------------------------------------

const __dirname = path.dirname(fileURLToPath(import.meta.url));

const BASE_URL    = process.env.VR_BASE        ?? 'http://127.0.0.1:5185';
const WORKFLOW_ID = process.env.VR_WORKFLOW_ID ?? '5ebd4268-5de0-4dbf-a82d-42c596d4818e';
const THRESHOLD   = parseFloat(process.env.VR_THRESHOLD ?? '0.01');
const SETTLE_MS   = parseInt(process.env.VR_SETTLE_MS   ?? '800', 10);

const VIEWPORT = { width: 1512, height: 900 };

// Routes to capture.  slug is used as the output filename (no slashes).
const ROUTES = [
  { slug: 'home',      hash: '/#/'                                          },
  { slug: 'launch',    hash: '/#/launch'                                    },
  { slug: 'squads',    hash: '/#/squads'                                    },
  { slug: 'campaigns', hash: '/#/campaigns'                                 },
  { slug: 'memory',    hash: '/#/memory'                                    },
  { slug: 'workflow',  hash: `/#/workflow/${encodeURIComponent(WORKFLOW_ID)}` },
];

// Selector that must be visible before we shoot (immortal head bar — always present)
const READY_SELECTOR = '[data-testid="immortal-head-bar"]';

// Directories (relative to this file)
const DIR_BASE    = path.join(__dirname, 'baseline');
const DIR_CURRENT = path.join(__dirname, 'current');
const DIR_DIFF    = path.join(__dirname, 'diff');

// ---------------------------------------------------------------------------
// Helpers
// ---------------------------------------------------------------------------

function ensureDir(dir) {
  if (!existsSync(dir)) mkdirSync(dir, { recursive: true });
}

function slugPath(dir, slug) {
  return path.join(dir, `${slug}.png`);
}

/** Sleep for ms milliseconds. */
function sleep(ms) {
  return new Promise((resolve) => setTimeout(resolve, ms));
}

/**
 * Read a PNG file and return { data, width, height }.
 */
async function readPng(filePath) {
  const buffer = await readFile(filePath);
  return new Promise((resolve, reject) => {
    const png = new PNG();
    png.parse(buffer, (err, img) => {
      if (err) reject(err);
      else resolve(img);
    });
  });
}

/**
 * Inject animation-disabling styles so screenshots are deterministic.
 * Also sets prefers-reduced-motion via emulateMedia (done on the page object).
 */
const DISABLE_ANIMATIONS_CSS = `
*,
*::before,
*::after {
  animation-duration: 0s !important;
  animation-delay: 0s !important;
  transition-duration: 0s !important;
  transition-delay: 0s !important;
  animation-iteration-count: 1 !important;
}
`;

// ---------------------------------------------------------------------------
// Core: capture screenshots for all routes
// ---------------------------------------------------------------------------

/**
 * @param {string} outDir  - directory to write PNGs into
 * @returns {{ slug: string, file: string, ok: boolean, error?: string }[]}
 */
async function captureAll(outDir) {
  ensureDir(outDir);

  const browser = await chromium.launch({ headless: true });
  const context = await browser.newContext({
    viewport: VIEWPORT,
    // Honour prefers-reduced-motion at the media-query level
    reducedMotion: 'reduce',
  });

  const results = [];

  for (const route of ROUTES) {
    const url = `${BASE_URL}${route.hash}`;
    const outFile = slugPath(outDir, route.slug);

    console.log(`  [capture] ${route.slug} → ${url}`);

    const page = await context.newPage();
    try {
      // Inject animation-kill stylesheet as early as possible
      await page.addInitScript(() => {
        const style = document.createElement('style');
        style.textContent = `
          *, *::before, *::after {
            animation-duration: 0s !important;
            animation-delay: 0s !important;
            transition-duration: 0s !important;
            transition-delay: 0s !important;
            animation-iteration-count: 1 !important;
          }
        `;
        document.head?.appendChild(style);
      });

      // Navigate and wait for load.
      // NOTE: networkidle is intentionally avoided — the cockpit makes continuous
      // polling requests (/api/health, /api/workflows every 8s) so the network
      // never goes idle. We rely on waitForSelector for the readiness signal instead.
      await page.goto(url, { waitUntil: 'load', timeout: 30_000 });

      // Wait for the immortal head bar (always rendered — if absent, app crashed)
      await page.waitForSelector(READY_SELECTOR, { timeout: 15_000 });

      // Also inject post-load (for any CSSOM the app replaces head content)
      await page.addStyleTag({ content: DISABLE_ANIMATIONS_CSS });

      // Fixed settle delay so any async paint/transition flushes
      await sleep(SETTLE_MS);

      // Screenshot
      await page.screenshot({ path: outFile, fullPage: false });
      console.log(`  [capture] ${route.slug} OK → ${outFile}`);
      results.push({ slug: route.slug, file: outFile, ok: true });
    } catch (err) {
      const msg = err instanceof Error ? err.message : String(err);
      console.error(`  [capture] ${route.slug} FAILED: ${msg}`);
      results.push({ slug: route.slug, file: outFile, ok: false, error: msg });
    } finally {
      await page.close();
    }
  }

  await context.close();
  await browser.close();
  return results;
}

// ---------------------------------------------------------------------------
// Diff: pixel-compare current vs baseline
// ---------------------------------------------------------------------------

/**
 * @returns {{ slug: string, ratio: number, diffFile: string, ok: boolean, error?: string }[]}
 */
async function diffAll() {
  ensureDir(DIR_DIFF);

  const diffResults = [];
  let anyExceeded = false;

  for (const route of ROUTES) {
    const baselineFile = slugPath(DIR_BASE, route.slug);
    const currentFile  = slugPath(DIR_CURRENT, route.slug);
    const diffFile     = slugPath(DIR_DIFF, route.slug);

    if (!existsSync(baselineFile)) {
      console.warn(`  [diff] ${route.slug}: no baseline at ${baselineFile} — skipping`);
      diffResults.push({ slug: route.slug, ratio: null, diffFile, ok: false, error: 'no baseline' });
      continue;
    }
    if (!existsSync(currentFile)) {
      console.warn(`  [diff] ${route.slug}: no current at ${currentFile} — skipping`);
      diffResults.push({ slug: route.slug, ratio: null, diffFile, ok: false, error: 'no current' });
      continue;
    }

    try {
      const [base, curr] = await Promise.all([readPng(baselineFile), readPng(currentFile)]);

      if (base.width !== curr.width || base.height !== curr.height) {
        const msg = `dimension mismatch: baseline ${base.width}x${base.height} vs current ${curr.width}x${curr.height}`;
        console.error(`  [diff] ${route.slug}: ${msg}`);
        diffResults.push({ slug: route.slug, ratio: 1.0, diffFile, ok: false, error: msg });
        anyExceeded = true;
        continue;
      }

      const diff = new PNG({ width: base.width, height: base.height });
      const changedPixels = pixelmatch(
        base.data, curr.data, diff.data,
        base.width, base.height,
        { threshold: 0.1, includeAA: false },
      );

      const totalPixels = base.width * base.height;
      const ratio = changedPixels / totalPixels;

      // Write diff PNG
      await new Promise((resolve, reject) => {
        const out = createWriteStream(diffFile);
        diff.pack().pipe(out);
        out.on('finish', resolve);
        out.on('error', reject);
      });

      const exceeded = ratio > THRESHOLD;
      if (exceeded) anyExceeded = true;

      const pct = (ratio * 100).toFixed(3);
      const status = exceeded ? 'FAIL' : 'OK';
      console.log(`  [diff] ${route.slug}: ${pct}% changed — ${status} (threshold ${(THRESHOLD * 100).toFixed(1)}%)`);
      diffResults.push({ slug: route.slug, ratio, diffFile, ok: !exceeded });
    } catch (err) {
      const msg = err instanceof Error ? err.message : String(err);
      console.error(`  [diff] ${route.slug}: ERROR ${msg}`);
      diffResults.push({ slug: route.slug, ratio: null, diffFile, ok: false, error: msg });
      anyExceeded = true;
    }
  }

  return { diffResults, anyExceeded };
}

// ---------------------------------------------------------------------------
// Entry point
// ---------------------------------------------------------------------------

const MODE = process.argv[2] ?? 'baseline';

if (!['baseline', 'current', 'check'].includes(MODE)) {
  console.error(`Unknown mode: ${MODE}. Use: baseline | current | check`);
  process.exit(1);
}

(async () => {
  console.log(`\nHydra Cockpit — Visual Regression (mode: ${MODE})`);
  console.log(`  Base URL:    ${BASE_URL}`);
  console.log(`  Workflow ID: ${WORKFLOW_ID}`);
  console.log(`  Viewport:    ${VIEWPORT.width}x${VIEWPORT.height}`);
  console.log(`  Threshold:   ${(THRESHOLD * 100).toFixed(1)}%`);
  console.log(`  Settle:      ${SETTLE_MS}ms\n`);

  if (MODE === 'baseline') {
    console.log('Capturing baselines...');
    const results = await captureAll(DIR_BASE);
    const failed = results.filter((r) => !r.ok);
    if (failed.length > 0) {
      console.error(`\n${failed.length} route(s) failed to capture:`);
      failed.forEach((r) => console.error(`  ${r.slug}: ${r.error}`));
      process.exit(1);
    }
    console.log(`\nBaselines saved to: ${DIR_BASE}`);
    process.exit(0);
  }

  if (MODE === 'current') {
    console.log('Capturing current screenshots...');
    const results = await captureAll(DIR_CURRENT);
    const failed = results.filter((r) => !r.ok);
    if (failed.length > 0) {
      console.error(`\n${failed.length} route(s) failed to capture:`);
      failed.forEach((r) => console.error(`  ${r.slug}: ${r.error}`));
      process.exit(1);
    }
    console.log(`\nCurrent screenshots saved to: ${DIR_CURRENT}`);
    process.exit(0);
  }

  if (MODE === 'check') {
    console.log('Capturing current screenshots...');
    const captureResults = await captureAll(DIR_CURRENT);
    const captureFailed = captureResults.filter((r) => !r.ok);
    if (captureFailed.length > 0) {
      console.warn(`\nWarning: ${captureFailed.length} route(s) failed to capture (will skip diff for those):`);
      captureFailed.forEach((r) => console.warn(`  ${r.slug}: ${r.error}`));
    }

    console.log('\nDiffing against baselines...');
    const { diffResults, anyExceeded } = await diffAll();

    console.log('\n--- Visual Regression Report ---');
    for (const r of diffResults) {
      if (r.error) {
        console.log(`  ${r.slug.padEnd(12)} ERROR: ${r.error}`);
      } else {
        const pct = (r.ratio * 100).toFixed(3);
        const marker = r.ok ? 'PASS' : 'FAIL';
        console.log(`  ${r.slug.padEnd(12)} ${pct.padStart(8)}% changed  [${marker}]`);
      }
    }
    console.log('--------------------------------');

    if (anyExceeded) {
      console.error(`\nFAIL: one or more routes exceeded the ${(THRESHOLD * 100).toFixed(1)}% threshold.`);
      console.error(`Diff PNGs written to: ${DIR_DIFF}`);
      process.exit(1);
    } else {
      console.log(`\nPASS: all routes within threshold.`);
      process.exit(0);
    }
  }
})();
