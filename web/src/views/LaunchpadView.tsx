/**
 * Hydra Cockpit — Launchpad → THE CONSTELLATION (R2)
 *
 * Replaces the card grid with a bounded deterministic radial layout:
 *   - Spirit at center (spirit-core.png + spirit-pulse heartbeat)
 *   - Squad heads on crown-colored rings (Executive/Forge/Garland)
 *   - Head angle = stable hash of squad slug (deterministic, never reflows)
 *   - Necks (SVG paths) connect active workflow heads to the Spirit
 *   - Neck tension/sway encodes status per §8 (cubic-bezier control-point sway)
 *   - Active heads carry a flame (flame-sprite.png or CSS)
 *   - IAU "all-clear" idle formation when activeHeadCount === 0
 *   - Speak-intent affordance at the Spirit; recent-intent arc chips
 *   - Legion-vs-Pentecost divergence signal on the Spirit
 *   - 3% SVG turbulence grain overlay on constellation field
 *   - Full accessibility: every head/workflow reachable via accessible twin list
 *   - 8-state handling
 */

import { useCallback, useEffect, useMemo, useRef, useState } from 'react';
import {
  ErrorScreen,
  DegradedBanner,
  OfflineBanner,
} from '../components/StateScreens.tsx';
import { launchWorkflow, previewNonce, fetchSquads, fetchWorkflows } from '../api/client.ts';
import type { WorkflowSummary, SquadPack, LaunchResult } from '../api/client.ts';
import { ConfirmDialog } from '../components/ConfirmDialog.tsx';
import type { CockpitDialogState } from '../cockpit/types.ts';
import { crownOf, crownColorVar as crownColor } from '../cockpit/crowns.ts';
import type { CrownFamily } from '../cockpit/crowns.ts';
import {
  stableHash, RING_R, distributeHeads,
} from '../cockpit/constellation-layout.ts';

// Crown family → slug mapping and colours live in src/cockpit/crowns.ts; the
// deterministic, collision-free constellation geometry lives in
// src/cockpit/constellation-layout.ts (unit-tested in tests/ui/launchpad.test.tsx).

// ---------------------------------------------------------------------------
// Active phase detection
// ---------------------------------------------------------------------------

const ACTIVE_PHASES = new Set([
  'intake', 'planning', 'approval', 'dispatch', 'executing', 'judge', 'synthesis', 'postcheck',
]);

function isActive(wf: WorkflowSummary): boolean {
  return !!wf.phase && ACTIVE_PHASES.has(wf.phase);
}

function isRecent(wf: WorkflowSummary): boolean {
  return wf.phase === 'done' || wf.phase === 'surfaced';
}

// ---------------------------------------------------------------------------
// Recent-intent chips storage (sessionStorage for persistence)
// ---------------------------------------------------------------------------

const INTENT_HISTORY_KEY = 'hydra-launchpad-intent-history';
const INTENT_HISTORY_MAX = 5;

function loadIntentHistory(): string[] {
  try {
    return JSON.parse(sessionStorage.getItem(INTENT_HISTORY_KEY) ?? '[]') as string[];
  } catch {
    return [];
  }
}

function saveIntentHistory(history: string[]): void {
  try {
    sessionStorage.setItem(INTENT_HISTORY_KEY, JSON.stringify(history.slice(0, INTENT_HISTORY_MAX)));
  } catch { /* ignore */ }
}

function addToIntentHistory(intent: string): string[] {
  if (!intent.trim()) return loadIntentHistory();
  const existing = loadIntentHistory().filter((h) => h !== intent.trim());
  const next = [intent.trim(), ...existing].slice(0, INTENT_HISTORY_MAX);
  saveIntentHistory(next);
  return next;
}

// ---------------------------------------------------------------------------
// Neck path builder (cubic bezier)
// ---------------------------------------------------------------------------

interface NeckStatus {
  budgetPct: number;     // 0–1
  isBreach: boolean;     // budget exceeded
  isDivergent: boolean;  // workflow in divergence state
  isSynthesis: boolean;  // in synthesis phase (return flow)
  isDispatch: boolean;   // in dispatch phase (outgoing flow)
  hasPendingGate: boolean;
}

/**
 * Build a cubic bezier SVG path from Spirit center to a head position.
 * Control-point offset encodes neck status (sway + tension).
 * budgetPct ≥ 0.8 → amplitude escalates.
 */
function buildNeckPath(
  cx: number, cy: number,
  hx: number, hy: number,
  status: NeckStatus,
  phase: number, // per-neck phase offset for sway (0–1, from slug hash)
): string {
  const midX = (cx + hx) / 2;
  const midY = (cy + hy) / 2;

  // Sway amplitude: rest=8px, ≥80% budget=18px
  const amplitude = status.budgetPct >= 0.8 ? 18 : 8;
  const swayOffset = Math.sin(phase * Math.PI * 2) * amplitude;

  // Perpendicular to the neck direction
  const dx = hx - cx;
  const dy = hy - cy;
  const len = Math.sqrt(dx * dx + dy * dy) || 1;
  const perpX = -dy / len;
  const perpY = dx / len;

  const cp1x = midX + perpX * swayOffset;
  const cp1y = midY + perpY * swayOffset;
  const cp2x = midX + perpX * swayOffset * 0.5;
  const cp2y = midY + perpY * swayOffset * 0.5;

  return `M ${cx} ${cy} C ${cp1x} ${cp1y} ${cp2x} ${cp2y} ${hx} ${hy}`;
}

// ---------------------------------------------------------------------------
// Grain overlay filter (3% turbulence — SVG defs)
// ---------------------------------------------------------------------------

function GrainFilter(): JSX.Element {
  return (
    <defs>
      <filter id="grain-noise" x="0%" y="0%" width="100%" height="100%" colorInterpolationFilters="sRGB">
        <feTurbulence type="fractalNoise" baseFrequency="0.65" numOctaves="3" stitchTiles="stitch" result="noise" />
        <feColorMatrix type="saturate" values="0" in="noise" result="grayNoise" />
        <feBlend in="SourceGraphic" in2="grayNoise" mode="overlay" result="blended" />
        <feComposite in="blended" in2="SourceGraphic" operator="in" />
      </filter>
    </defs>
  );
}

// ---------------------------------------------------------------------------
// Props
// ---------------------------------------------------------------------------

interface LaunchpadViewProps {
  live: boolean;
  offline: boolean;
  offlineSince?: number | undefined;
}

// ---------------------------------------------------------------------------
// Main Constellation component
// ---------------------------------------------------------------------------

export function LaunchpadView({ live, offline, offlineSince }: LaunchpadViewProps): JSX.Element {
  // ---- data state ----------------------------------------------------------
  const [workflows, setWorkflows] = useState<WorkflowSummary[] | null>(null);
  const [squads, setSquads] = useState<SquadPack[]>([]);
  const [loadingWf, setLoadingWf] = useState(true);
  const [loadingSquads, setLoadingSquads] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [degraded, setDegraded] = useState(false);
  const [degradedSources, setDegradedSources] = useState<string[]>([]);

  // ---- intent / speak state ------------------------------------------------
  const [intentText, setIntentText] = useState('');
  const [intentHistory, setIntentHistory] = useState<string[]>(loadIntentHistory);
  const [launching, setLaunching] = useState(false);
  const [launchError, setLaunchError] = useState<string | null>(null);
  const intentRef = useRef<HTMLTextAreaElement>(null);

  // ---- confirm dialog state (live launch nonce flow) ----------------------
  const [dialogState, setDialogState] = useState<CockpitDialogState | null>(null);
  const [confirmNonce, setConfirmNonce] = useState<string | null>(null);
  const [pendingLaunchGoal, setPendingLaunchGoal] = useState<string>('');
  const [launchMode, setLaunchMode] = useState<'dry' | 'live'>('dry');

  // ---- IAU idle formation state -------------------------------------------
  // phase offset for CSS sway (0–1), using a stable-ish timestamp seed
  const [swayPhase] = useState(() => Date.now() % 1000 / 1000);

  // ---- online ---------------------------------------------------------------
  const online = live && !offline;

  // ---- data fetching -------------------------------------------------------

  const loadWorkflows = useCallback(async (): Promise<void> => {
    try {
      const body = await fetchWorkflows(50);
      let wfs: WorkflowSummary[] = [];
      if (Array.isArray(body)) {
        wfs = body;
      } else if (body && typeof body === 'object' && 'workflows' in body) {
        const b = body as { workflows?: WorkflowSummary[]; degraded?: boolean; degradedReason?: string };
        wfs = b.workflows ?? [];
        if (b.degraded) {
          setDegraded(true);
          setDegradedSources(b.degradedReason ? [b.degradedReason] : ['hydra-mem']);
        }
      }
      setWorkflows(wfs);
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
    } finally {
      setLoadingWf(false);
    }
  }, []);

  const loadSquads = useCallback(async (): Promise<void> => {
    try {
      const body = await fetchSquads();
      let packs: SquadPack[] = [];
      if (Array.isArray(body)) {
        packs = body;
      } else if (body && typeof body === 'object' && 'squads' in body) {
        packs = (body as { squads?: SquadPack[] }).squads ?? [];
      }
      setSquads(packs);
    } catch {
      // squads unavailable — constellation still renders with workflow data
    } finally {
      setLoadingSquads(false);
    }
  }, []);

  useEffect(() => {
    void loadWorkflows();
    void loadSquads();
    const wfInterval = setInterval(() => { void loadWorkflows(); }, 8000);
    return () => clearInterval(wfInterval);
  }, [loadWorkflows, loadSquads]);

  // ---- derived data --------------------------------------------------------

  const activeWfs = useMemo(() => (workflows ?? []).filter(isActive), [workflows]);
  const recentWfs = useMemo(() => (workflows ?? []).filter(isRecent).slice(0, 5), [workflows]);

  // Build head map: slug → { squad, workflowIds, crown }
  const headMap = useMemo(() => {
    const map = new Map<string, {
      squad: SquadPack | null;
      workflows: WorkflowSummary[];
      crown: CrownFamily;
    }>();

    // Start with known squad packs
    squads.forEach((sq) => {
      map.set(sq.slug, { squad: sq, workflows: [], crown: crownOf(sq.slug) });
    });

    // Add heads from active workflows that aren't in the squad pack list
    activeWfs.forEach((wf) => {
      (wf.selected_squads ?? []).forEach((slug) => {
        if (!map.has(slug)) {
          map.set(slug, { squad: null, workflows: [], crown: crownOf(slug) });
        }
        map.get(slug)!.workflows.push(wf);
      });
    });

    // Wire workflows onto existing squad entries
    activeWfs.forEach((wf) => {
      (wf.selected_squads ?? []).forEach((slug) => {
        const entry = map.get(slug);
        if (entry && !entry.workflows.includes(wf)) {
          entry.workflows.push(wf);
        }
      });
    });

    return map;
  }, [squads, activeWfs]);

  const headSlugs = useMemo(() => Array.from(headMap.keys()), [headMap]);
  const activeHeadCount = useMemo(
    () => headSlugs.filter((s) => (headMap.get(s)?.workflows.length ?? 0) > 0).length,
    [headSlugs, headMap],
  );

  // Detect divergence: any active workflow in a divergence-like state
  const isDiverging = useMemo(
    () => activeWfs.some((wf) =>
      wf.phase === 'judge' &&
      (wf.has_pending_hitl === false) &&
      activeWfs.filter((w) => w.phase === 'judge').length > 1,
    ),
    [activeWfs],
  );

  // ---- SVG layout constants ------------------------------------------------
  const SVG_W = 420;
  const SVG_H = 420;
  const CX = SVG_W / 2;
  const CY = SVG_H / 2;
  const SPIRIT_R = 28;

  // ---- head positions (deterministic) ------------------------------------

  interface HeadPosition {
    slug: string;
    x: number;
    y: number;
    angle: number;
    crown: CrownFamily;
    isHeadActive: boolean;
    /** 0/1 alternating tier — pushes every other label further out so dense
        arcs (many garland/forge squads) keep their labels from colliding. */
    labelTier: number;
  }

  const headPositions = useMemo((): HeadPosition[] => {
    // Geometry (even per-sector distribution → collision-free) is computed by
    // the pure, unit-tested layout module; we only layer on live workflow
    // state here.
    return distributeHeads(headSlugs, CX, CY).map((g) => ({
      ...g,
      isHeadActive: (headMap.get(g.slug)?.workflows.length ?? 0) > 0,
    }));
  }, [headSlugs, headMap]);

  // ---- intent handling -----------------------------------------------------

  // Preview which heads would light up based on intent text
  const intentActiveSlugs = useMemo((): Set<string> => {
    if (!intentText.trim()) return new Set();
    const lower = intentText.toLowerCase();
    const active = new Set<string>();
    headSlugs.forEach((slug) => {
      const sq = headMap.get(slug)?.squad;
      const desc = (sq?.description ?? '') + ' ' + (sq?.name ?? '') + ' ' + slug;
      if (desc.toLowerCase().includes(lower) || lower.includes(slug)) {
        active.add(slug);
      }
    });
    return active;
  }, [intentText, headSlugs, headMap]);

  const handleIntentSubmit = useCallback(async (): Promise<void> => {
    const goal = intentText.trim();
    if (!goal || !online) return;

    if (launchMode === 'live') {
      // Fetch nonce for live launch
      setLaunching(true);
      setLaunchError(null);
      try {
        const nonce = await previewNonce('launch');
        setPendingLaunchGoal(goal);
        setConfirmNonce(nonce.nonce);
        setDialogState({
          kind: 'launch-live',
          title: 'Launch live workflow',
          verb: 'Launch live',
          lines: [
            `Goal: "${goal}"`,
            'This will execute real actions.',
          ],
          danger: true,
          payload: { goal, live: true, confirmNonce: nonce.nonce },
        });
      } catch (e) {
        setLaunchError(e instanceof Error ? e.message : String(e));
      } finally {
        setLaunching(false);
      }
    } else {
      // Dry-run: launch without nonce
      setLaunching(true);
      setLaunchError(null);
      try {
        const result: LaunchResult = await launchWorkflow({ goal, live: false });
        setIntentHistory(addToIntentHistory(goal));
        setIntentText('');
        // Navigate to the new workflow
        window.location.hash = `#/workflow/${encodeURIComponent(result.workflow_id)}`;
      } catch (e) {
        setLaunchError(e instanceof Error ? e.message : String(e));
      } finally {
        setLaunching(false);
      }
    }
  }, [intentText, online, launchMode]);

  const handleConfirmLaunch = useCallback(async (): Promise<void> => {
    if (!confirmNonce || !pendingLaunchGoal) return;
    setLaunching(true);
    setLaunchError(null);
    try {
      const result: LaunchResult = await launchWorkflow({
        goal: pendingLaunchGoal,
        live: true,
        confirmNonce,
      });
      setIntentHistory(addToIntentHistory(pendingLaunchGoal));
      setIntentText('');
      setDialogState(null);
      setConfirmNonce(null);
      window.location.hash = `#/workflow/${encodeURIComponent(result.workflow_id)}`;
    } catch (e) {
      setLaunchError(e instanceof Error ? e.message : String(e));
    } finally {
      setLaunching(false);
    }
  }, [confirmNonce, pendingLaunchGoal]);

  // Direct-jump S → focus speak-intent
  useEffect(() => {
    function onKeyDown(e: KeyboardEvent): void {
      const tag = (e.target as HTMLElement).tagName;
      if (['INPUT', 'TEXTAREA', 'SELECT'].includes(tag)) return;
      if (e.metaKey || e.ctrlKey || e.altKey) return;
      if (e.key === 'S') {
        e.preventDefault();
        intentRef.current?.focus();
      }
    }
    document.addEventListener('keydown', onKeyDown);
    return () => document.removeEventListener('keydown', onKeyDown);
  }, []);

  // ---- loading state -------------------------------------------------------

  if (loadingWf && loadingSquads && workflows === null) {
    return (
      <div className="constellation-field constellation-field--loading" data-testid="constellation-loading">
        <ConstellationSkeleton />
      </div>
    );
  }

  if (error && workflows === null) {
    return (
      <ErrorScreen
        message={`Failed to load constellation: ${error}`}
        onRetry={() => { setError(null); setLoadingWf(true); void loadWorkflows(); }}
      />
    );
  }

  // ---- SVG constellation label -------------------------------------------
  const svgAriaLabel = activeHeadCount > 0
    ? `Constellation — ${activeHeadCount} active head${activeHeadCount !== 1 ? 's' : ''}, ${activeWfs.length} workflow${activeWfs.length !== 1 ? 's' : ''}`
    : `Constellation — ${headSlugs.length} head${headSlugs.length !== 1 ? 's' : ''} in IAU idle formation`;

  // ---- render -------------------------------------------------------------

  return (
    <div
      className="constellation-view"
      data-testid="constellation-view"
      data-state={isDiverging ? 'diverging' : 'live'}
    >
      {/* ---- 8-state banners -------------------------------------------- */}
      {offline ? <OfflineBanner since={offlineSince} /> : null}
      {!offline && degraded ? (
        <DegradedBanner sources={degradedSources} message="Constellation data may be incomplete" />
      ) : null}

      {/* ---- Constellation field ---------------------------------------- */}
      <div
        className="constellation-field"
        data-iau-idle={activeHeadCount === 0 ? '' : undefined}
        data-testid="constellation-field"
      >
        {/* Grain overlay: 3% SVG turbulence, constellation field only */}
        <div className="constellation-grain" aria-hidden="true" />

        {/* ---- THE SVG CONSTELLATION ------------------------------------- */}
        <svg
          className="constellation-svg"
          viewBox={`0 0 ${SVG_W} ${SVG_H}`}
          aria-label={svgAriaLabel}
          role="img"
          aria-hidden="false"
          data-testid="constellation-svg"
        >
          {/* Grain filter defs */}
          <GrainFilter />

          {/* Background grain rect */}
          <rect
            x="0" y="0" width={SVG_W} height={SVG_H}
            fill="transparent"
            filter="url(#grain-noise)"
            opacity={0.03}
            aria-hidden="true"
          />

          {/* Crown ring guides (decorative, aria-hidden) */}
          <ConstellationRings cx={CX} cy={CY} />

          {/* Necks: Spirit ↔ active head */}
          {headPositions.map((hp) => {
            const headData = headMap.get(hp.slug);
            if (!headData || headData.workflows.length === 0) return null;
            const wf = headData.workflows[0];
            const status: NeckStatus = {
              budgetPct: wf.budget?.spent_usd && wf.budget.budget_usd
                ? wf.budget.spent_usd / wf.budget.budget_usd : 0,
              isBreach: !!(wf.budget?.spent_usd && wf.budget.budget_usd &&
                wf.budget.spent_usd >= wf.budget.budget_usd),
              isDivergent: isDiverging,
              isSynthesis: wf.phase === 'synthesis',
              isDispatch: wf.phase === 'dispatch',
              hasPendingGate: wf.has_pending_hitl ?? false,
            };
            const phase = (stableHash(hp.slug) % 100) / 100;
            return (
              <NeckLine
                key={`neck-${hp.slug}`}
                cx={CX} cy={CY}
                hx={hp.x} hy={hp.y}
                status={status}
                phase={phase}
                swayPhase={swayPhase}
                slug={hp.slug}
              />
            );
          })}

          {/* Head nodes */}
          {headPositions.map((hp) => (
            <HeadNode
              key={`head-${hp.slug}`}
              hp={hp}
              headData={headMap.get(hp.slug) ?? null}
              isIntentHover={intentActiveSlugs.has(hp.slug)}
              isAllIdle={activeHeadCount === 0}
            />
          ))}

          {/* Spirit core */}
          <SpiritNode
            cx={CX} cy={CY}
            r={SPIRIT_R}
            isDiverging={isDiverging}
          />
        </svg>

        {/* ---- Speak-intent affordance (below SVG, above chips) ---------- */}
        <SpeakIntent
          intentText={intentText}
          setIntentText={setIntentText}
          onSubmit={() => { void handleIntentSubmit(); }}
          online={online}
          offline={offline}
          launching={launching}
          launchError={launchError}
          launchMode={launchMode}
          setLaunchMode={setLaunchMode}
          intentRef={intentRef}
        />

        {/* ---- Recent-intent arc chips ----------------------------------- */}
        {intentHistory.length > 0 ? (
          <RecentIntentChips
            chips={intentHistory}
            onSelect={(text) => {
              setIntentText(text);
              intentRef.current?.focus();
            }}
          />
        ) : null}

        {/* ---- Empty state: no workflows yet ----------------------------- */}
        {workflows !== null && workflows.length === 0 && !degraded ? (
          <div className="constellation-empty" role="status" data-testid="constellation-empty">
            No workflows yet — speak the first intent
          </div>
        ) : null}

        {/* ---- Accessible twin: visually-hidden list of all heads + workflows */}
        <AccessibleConstellationTwin
          headPositions={headPositions}
          headMap={headMap}
          activeWfs={activeWfs}
          recentWfs={recentWfs}
          online={online}
        />
      </div>

      {/* ---- Confirm dialog for live launch -------------------------------- */}
      {dialogState ? (
        <ConfirmDialog
          state={dialogState}
          onConfirm={() => { void handleConfirmLaunch(); }}
          onCancel={() => {
            setDialogState(null);
            setConfirmNonce(null);
          }}
          busy={launching}
        />
      ) : null}
    </div>
  );
}

// ---------------------------------------------------------------------------
// Sub-components
// ---------------------------------------------------------------------------

// ---- Constellation skeleton (loading state) --------------------------------

function ConstellationSkeleton(): JSX.Element {
  return (
    <svg
      className="constellation-svg constellation-svg--skeleton"
      viewBox="0 0 420 420"
      aria-label="Loading constellation…"
      role="img"
    >
      <circle cx="210" cy="210" r="28" fill="none" stroke="var(--bone-mid)" strokeWidth="1.5" opacity="0.3" />
      <circle cx="210" cy="210" r="100" fill="none" stroke="var(--crown-exec)" strokeWidth="0.5" opacity="0.2" strokeDasharray="4 4" />
      <circle cx="210" cy="210" r="140" fill="none" stroke="var(--crown-forge)" strokeWidth="0.5" opacity="0.2" strokeDasharray="4 4" />
      <circle cx="210" cy="210" r="175" fill="none" stroke="var(--crown-garland)" strokeWidth="0.5" opacity="0.2" strokeDasharray="4 4" />
    </svg>
  );
}

// ---- Crown ring guides -------------------------------------------------------

interface ConstellationRingsProps { cx: number; cy: number; }

function ConstellationRings({ cx, cy }: ConstellationRingsProps): JSX.Element {
  return (
    <g aria-hidden="true" className="constellation-rings">
      {Object.entries(RING_R).map(([crown, r]) => (
        <circle
          key={crown}
          cx={cx} cy={cy} r={r}
          fill="none"
          stroke={crownColor(crown as CrownFamily)}
          strokeWidth="0.5"
          opacity="0.18"
          strokeDasharray="3 5"
        />
      ))}
    </g>
  );
}

// ---- Spirit node ------------------------------------------------------------

interface SpiritNodeProps {
  cx: number; cy: number; r: number;
  isDiverging: boolean;
}

function SpiritNode({ cx, cy, r, isDiverging }: SpiritNodeProps): JSX.Element {
  return (
    <g
      className={`spirit-node${isDiverging ? ' spirit-node--diverging' : ''}`}
      data-testid="spirit-node"
      aria-hidden="true"
    >
      {/* Deep ambient glow — layered soft radiance */}
      <circle
        cx={cx} cy={cy} r={r + 28}
        fill="none"
        stroke="var(--spirit-amber)"
        strokeWidth="0.3"
        opacity="0.06"
      />
      <circle
        cx={cx} cy={cy} r={r + 20}
        fill="none"
        stroke="var(--spirit-amber)"
        strokeWidth="0.5"
        opacity="0.1"
      />
      {/* Corona ring — slow-rotating dashed ring (CSS animation applied) */}
      <circle
        cx={cx} cy={cy} r={r + 14}
        fill="none"
        stroke="var(--spirit-amber)"
        strokeWidth="0.8"
        opacity="0.22"
        strokeDasharray="2 6"
        className="spirit-corona"
        style={{ transformOrigin: `${cx}px ${cy}px` } as React.CSSProperties}
      />
      {/* Second corona — opposite-phase rotation for depth */}
      <circle
        cx={cx} cy={cy} r={r + 8}
        fill="none"
        stroke="var(--spirit-amber)"
        strokeWidth="0.5"
        opacity="0.3"
        strokeDasharray="4 4"
        className="spirit-corona"
        style={{ transformOrigin: `${cx}px ${cy}px`, animationDirection: 'reverse', animationDuration: '16s' } as React.CSSProperties}
      />
      {/* Outer glow ring */}
      <circle
        cx={cx} cy={cy} r={r + 6}
        fill="none"
        stroke="var(--spirit-amber)"
        strokeWidth="0.5"
        opacity={isDiverging ? 0.25 : 0.18}
        className="spirit-glow-ring"
      />
      {/* Main Spirit body */}
      <circle
        cx={cx} cy={cy} r={r}
        fill="var(--covenant-indigo)"
        stroke="var(--spirit-amber)"
        strokeWidth="1.5"
        className="spirit-core-circle spirit-pulse-host"
      />
      {/* Spirit image — slow counter-rotation for depth illusion */}
      <g
        className="spirit-image-rotating"
        style={{ transformOrigin: `${cx}px ${cy}px` } as React.CSSProperties}
      >
        <image
          href="/images/chosen/spirit-core.png"
          x={cx - r} y={cy - r}
          width={r * 2} height={r * 2}
          preserveAspectRatio="xMidYMid meet"
          aria-hidden="true"
        />
      </g>
      {/* Divergence indicator ring */}
      {isDiverging ? (
        <circle
          cx={cx} cy={cy} r={r + 6}
          fill="none"
          stroke="var(--bone-mid)"
          strokeWidth="1"
          opacity="0.5"
          className="spirit-diverge-ring"
          strokeDasharray="2 4"
        />
      ) : null}
    </g>
  );
}

// ---- Head node --------------------------------------------------------------

interface HeadNodeProps {
  hp: { slug: string; x: number; y: number; crown: CrownFamily; isHeadActive: boolean; angle: number; labelTier: number };
  headData: { workflows: WorkflowSummary[]; squad: SquadPack | null } | null;
  isIntentHover: boolean;
  isAllIdle: boolean;
}

function HeadNode({ hp, headData, isIntentHover, isAllIdle }: HeadNodeProps): JSX.Element {
  const isActive = (headData?.workflows.length ?? 0) > 0;
  const hasPendingGate = headData?.workflows.some((w) => w.has_pending_hitl) ?? false;
  const r = 12;
  const crown = hp.crown;

  // Crown color selection
  const strokeColor = crownColor(crown);
  const fillColor = isActive
    ? `color-mix(in srgb, ${strokeColor} 25%, var(--void))`
    : 'var(--void)';

  // Per-head drift period and phase derived from slug hash for deterministic diversity
  const slugHash = stableHash(hp.slug);
  const driftPeriod = 5 + (slugHash % 30) / 10; // 5–8s
  const driftPhase = -((slugHash % 30) / 10);   // 0 to -3s offset

  return (
    <g
      className={`head-node${isActive ? ' head-node--active' : ''}${isIntentHover ? ' head-node--intent' : ''}${isAllIdle ? ' head-node--iau-idle' : ''}`}
      data-slug={hp.slug}
      data-crown={crown}
      aria-hidden="true"
      style={{
        '--drift-period': `${driftPeriod}s`,
        '--drift-phase': `${driftPhase}s`,
      } as React.CSSProperties}
    >
      {/* Head circle */}
      <circle
        cx={hp.x} cy={hp.y} r={r}
        fill={fillColor}
        stroke={hasPendingGate ? 'var(--venom)' : strokeColor}
        strokeWidth={isActive ? 1.5 : 1}
        opacity={isActive || isIntentHover ? 1 : 0.5}
        className={`head-circle${isActive ? ' head-ignite' : ''}`}
        style={{ '--head-angle': hp.angle } as React.CSSProperties}
      />
      {/* Hover bloom ring — expands outward and fades */}
      {(isActive || isIntentHover) ? (
        <circle
          cx={hp.x} cy={hp.y} r={r}
          fill="none"
          stroke={strokeColor}
          strokeWidth="1"
          opacity="0"
          className="head-bloom-ring"
          aria-hidden="true"
        />
      ) : null}
      {/* Head label — full slug (only truncated past 14 chars, with ellipsis);
          even-distribution placement keeps neighbouring labels from colliding. */}
      <text
        x={hp.x} y={hp.y + r + 10 + (hp.labelTier ? 11 : 0)}
        textAnchor="middle"
        fontSize="8"
        fill={hasPendingGate ? 'var(--venom)' : strokeColor}
        opacity={isActive ? 1 : 0.72}
        fontFamily="var(--font-mono)"
        aria-hidden="true"
        className="head-label"
      >
        {hp.slug.length > 14 ? hp.slug.slice(0, 13) + '…' : hp.slug}
      </text>
      {/* Flame image (active heads — primary) */}
      {isActive ? (
        <image
          href="/images/chosen/flame-sprite.png"
          x={hp.x - 8} y={hp.y - r - 16}
          width="16" height="16"
          preserveAspectRatio="xMidYMid meet"
          aria-hidden="true"
          className="head-flame flame-enter"
        />
      ) : null}
      {/* Flame lick particles — tiny SVG ellipses licking upward from active heads */}
      {isActive ? (
        <>
          <ellipse
            cx={hp.x - 2} cy={hp.y - r - 4}
            rx="1.5" ry="3"
            fill="var(--spirit-amber)"
            opacity="0.8"
            aria-hidden="true"
            className="flame-lick"
            style={{ transformOrigin: `${hp.x - 2}px ${hp.y - r - 4}px` } as React.CSSProperties}
          />
          <ellipse
            cx={hp.x + 2} cy={hp.y - r - 6}
            rx="1" ry="2.5"
            fill="var(--spirit-amber)"
            opacity="0.6"
            aria-hidden="true"
            className="flame-lick"
            style={{ transformOrigin: `${hp.x + 2}px ${hp.y - r - 6}px` } as React.CSSProperties}
          />
          <ellipse
            cx={hp.x} cy={hp.y - r - 8}
            rx="0.8" ry="2"
            fill="var(--bone)"
            opacity="0.7"
            aria-hidden="true"
            className="flame-lick"
            style={{ transformOrigin: `${hp.x}px ${hp.y - r - 8}px` } as React.CSSProperties}
          />
        </>
      ) : null}
      {/* Pending gate pulse ring */}
      {hasPendingGate ? (
        <circle
          cx={hp.x} cy={hp.y} r={r + 5}
          fill="none"
          stroke="var(--venom)"
          strokeWidth="1"
          opacity="0.6"
          className="gate-pulse-ring"
          aria-hidden="true"
        />
      ) : null}
    </g>
  );
}

// ---- Neck line --------------------------------------------------------------

interface NeckLineProps {
  cx: number; cy: number;
  hx: number; hy: number;
  status: NeckStatus;
  phase: number;
  swayPhase: number;
  slug: string;
}

function NeckLine({ cx, cy, hx, hy, status, phase, swayPhase, slug }: NeckLineProps): JSX.Element {
  // Combine neck phase with sway phase for gentle offset diversity
  const combinedPhase = (phase + swayPhase) % 1;
  const pathD = buildNeckPath(cx, cy, hx, hy, status, combinedPhase);

  // Color logic: synthesis = Head→Spirit (blend toward amber); dispatch = Spirit→Head (crown color)
  // The animation direction is encoded via the CSS class and stroke-dashoffset direction
  const isSynthesis = status.isSynthesis;
  const isBreach = status.isBreach;
  const isDivergent = status.isDivergent;

  const strokeColor = isBreach
    ? 'var(--venom)'
    : isDivergent
      ? 'var(--bone-mid)'
      : isSynthesis
        ? 'var(--spirit-amber)'
        : crownColor(crownOf(slug));

  return (
    <g aria-hidden="true" className="neck-group">
      {/* Base neck line */}
      <path
        d={pathD}
        fill="none"
        stroke={strokeColor}
        strokeWidth={isBreach ? 2 : 1}
        opacity={isDivergent ? 0.4 : 0.7}
        className={`neck-line${isBreach ? ' neck-line--breach' : ''}${isDivergent ? ' neck-line--divergent' : ''}`}
        data-testid={`neck-${slug}`}
      />
      {/* Animated dash (dispatch or synthesis) */}
      <path
        d={pathD}
        fill="none"
        stroke={isSynthesis ? 'var(--spirit-amber)' : strokeColor}
        strokeWidth="1.5"
        opacity="0.9"
        strokeDasharray="6 8"
        className={`neck-dash${isSynthesis ? ' neck-dash--synthesis' : ' neck-dash--dispatch'}`}
        aria-hidden="true"
      />
    </g>
  );
}

// ---- Speak-intent affordance ------------------------------------------------

interface SpeakIntentProps {
  intentText: string;
  setIntentText: (t: string) => void;
  onSubmit: () => void;
  online: boolean;
  offline: boolean;
  launching: boolean;
  launchError: string | null;
  launchMode: 'dry' | 'live';
  setLaunchMode: (m: 'dry' | 'live') => void;
  intentRef: React.RefObject<HTMLTextAreaElement>;
}

function SpeakIntent({
  intentText, setIntentText, onSubmit, online, offline, launching,
  launchError, launchMode, setLaunchMode, intentRef,
}: SpeakIntentProps): JSX.Element {
  return (
    <div
      className="speak-intent"
      data-testid="speak-intent"
      role="region"
      aria-label="Speak intent — launch a new workflow"
    >
      {offline ? (
        <div className="speak-intent-offline" aria-live="polite">
          Bridge offline — speak-intent disabled
        </div>
      ) : null}

      <div className="speak-intent-inner">
        <label htmlFor="constellation-intent" className="speak-intent-label sr-only">
          Speak your intent to launch a new workflow
        </label>
        <textarea
          id="constellation-intent"
          ref={intentRef}
          className="speak-intent-textarea"
          placeholder="Speak your intent… (S to focus)"
          value={intentText}
          onChange={(e) => setIntentText(e.target.value)}
          disabled={!online || launching}
          rows={2}
          aria-label="Workflow intent — describe what Hydra should do"
          data-testid="intent-textarea"
          onKeyDown={(e) => {
            if (e.key === 'Enter' && (e.ctrlKey || e.metaKey)) {
              e.preventDefault();
              onSubmit();
            }
          }}
        />

        {/* Mode selection */}
        <div className="speak-intent-mode" role="radiogroup" aria-label="Launch mode">
          <label className="speak-intent-mode-label">
            <input
              type="radio"
              name="constellation-launch-mode"
              value="dry"
              checked={launchMode === 'dry'}
              onChange={() => setLaunchMode('dry')}
              disabled={!online}
              aria-label="Dry-run — validate routing without real dispatch"
            />
            <span>Dry-run</span>
          </label>
          <label className="speak-intent-mode-label">
            <input
              type="radio"
              name="constellation-launch-mode"
              value="live"
              checked={launchMode === 'live'}
              onChange={() => setLaunchMode('live')}
              disabled={!online}
              aria-label="Live — real dispatch (requires confirmation)"
            />
            <span className="speak-intent-live-label">Live</span>
          </label>
        </div>

        <button
          type="button"
          className="speak-intent-submit"
          onClick={onSubmit}
          disabled={!online || !intentText.trim() || launching}
          aria-label={launchMode === 'live'
            ? 'Launch live workflow — real dispatch'
            : 'Launch dry-run — validate routing'}
          data-testid="intent-submit"
          aria-busy={launching}
        >
          {launching ? 'Launching…' : launchMode === 'live' ? 'Launch live →' : 'Speak →'}
        </button>
      </div>

      {launchError ? (
        <div className="speak-intent-error" role="alert" data-testid="intent-error">
          {launchError}
        </div>
      ) : null}
    </div>
  );
}

// ---- Recent-intent arc chips ------------------------------------------------

interface RecentIntentChipsProps {
  chips: string[];
  onSelect: (text: string) => void;
}

function RecentIntentChips({ chips, onSelect }: RecentIntentChipsProps): JSX.Element {
  return (
    <div
      className="recent-intent-chips"
      data-testid="recent-intent-chips"
      role="region"
      aria-label="Recent intents — click to prefill"
    >
      {chips.map((chip, i) => (
        <button
          key={i}
          type="button"
          className="intent-chip"
          onClick={() => onSelect(chip)}
          aria-label={chip}
          title={chip}
          data-testid={`intent-chip-${i}`}
        >
          {chip.length > 32 ? chip.slice(0, 32) + '…' : chip}
        </button>
      ))}
    </div>
  );
}

// ---- Accessible twin: visually-hidden list ---------------------------------

interface AccessibleConstellationTwinProps {
  headPositions: Array<{ slug: string; crown: CrownFamily; isHeadActive: boolean }>;
  headMap: Map<string, { workflows: WorkflowSummary[]; squad: SquadPack | null }>;
  activeWfs: WorkflowSummary[];
  recentWfs: WorkflowSummary[];
  online: boolean;
}

function AccessibleConstellationTwin({
  headPositions, headMap, activeWfs, recentWfs,
}: AccessibleConstellationTwinProps): JSX.Element {
  return (
    <div
      className="constellation-accessible-twin"
      data-testid="constellation-accessible-twin"
      aria-label="Constellation accessible structure"
    >
      {/* Active workflows list */}
      {activeWfs.length > 0 ? (
        <section aria-labelledby="acc-active-heading">
          <h2 id="acc-active-heading" className="sr-only">Active workflows</h2>
          <ul role="list" aria-label="Active workflows">
            {activeWfs.map((wf) => (
              <li key={wf.workflow_id} role="listitem">
                <a
                  href={wf.has_pending_hitl
                    ? `#/gate/${encodeURIComponent(wf.workflow_id)}`
                    : `#/workflow/${encodeURIComponent(wf.workflow_id)}`}
                  className="acc-workflow-link"
                  aria-label={`Workflow ${wf.workflow_id.slice(0, 8)}, phase ${wf.phase ?? 'unknown'}${wf.has_pending_hitl ? ', gate pending — action required' : ''}`}
                >
                  <span aria-hidden="true">{wf.has_pending_hitl ? '⚠' : '◐'}</span>
                  <span>{wf.workflow_id.slice(0, 8)} — {wf.root_goal ?? '(no goal)'}</span>
                  <span>{wf.phase}</span>
                  {wf.has_pending_hitl ? <span className="sr-only">Gate pending — action required</span> : null}
                </a>
              </li>
            ))}
          </ul>
        </section>
      ) : (
        <p className="sr-only">No active workflows — constellation in idle formation.</p>
      )}

      {/* Squad heads list */}
      <section aria-labelledby="acc-heads-heading">
        <h2 id="acc-heads-heading" className="sr-only">Squad heads in constellation</h2>
        <ul role="list" aria-label="Squad heads">
          {headPositions.map((hp) => {
            const data = headMap.get(hp.slug);
            const wfs = data?.workflows ?? [];
            return (
              <li key={hp.slug} role="listitem">
                <span
                  className="acc-head-item"
                  aria-label={`${hp.slug} — ${hp.crown} crown — ${wfs.length > 0 ? `${wfs.length} active workflow${wfs.length !== 1 ? 's' : ''}` : 'idle'}`}
                >
                  {hp.slug}
                  {wfs.length > 0 ? ` (${wfs.length} workflow${wfs.length !== 1 ? 's' : ''})` : ''}
                </span>
              </li>
            );
          })}
        </ul>
      </section>

      {/* Recent workflows */}
      {recentWfs.length > 0 ? (
        <section aria-labelledby="acc-recent-heading">
          <h2 id="acc-recent-heading" className="sr-only">Recent workflows</h2>
          <ul role="list" aria-label="Recent workflows">
            {recentWfs.map((wf) => (
              <li key={wf.workflow_id} role="listitem">
                <a
                  href={`#/workflow/${encodeURIComponent(wf.workflow_id)}`}
                  className="acc-workflow-link"
                  aria-label={`Workflow ${wf.workflow_id.slice(0, 8)}, ${wf.phase}`}
                >
                  <span>{wf.workflow_id.slice(0, 8)} — {wf.root_goal ?? '(no goal)'}</span>
                  <span>{wf.phase}</span>
                </a>
              </li>
            ))}
          </ul>
        </section>
      ) : null}
    </div>
  );
}

