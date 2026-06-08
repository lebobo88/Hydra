/**
 * Hydra Cockpit — Pentecost Cockpit Shell (R1)
 * 3-region layout: IMMORTAL HEAD | THE BODY (left rail) | THE WORKING (center) | THE ORACLE (right)
 *
 * Preserves all C7 data wiring: API client, polling, CSRF, 7 routes, 8-state handling.
 * Adds: Spirit-pulse heartbeat, Body rail with Crown grouping, Oracle one-voice panel,
 *        budget sinew band, pending-gate beacon, click-to-pause sigil, keyboard direct-jumps.
 */

import { useCallback, useEffect, useRef, useState } from 'react';
import { parseView } from './cockpit/types.ts';
import { LaunchpadView } from './views/LaunchpadView.tsx';
import { LaunchComposerView } from './views/LaunchComposerView.tsx';
import { LiveWorkflowView } from './views/LiveWorkflowView.tsx';
import { GateCockpitView } from './views/GateCockpitView.tsx';
import { SquadsView } from './views/SquadsView.tsx';
import { CampaignsView } from './views/CampaignsView.tsx';
import { MemoryView } from './views/MemoryView.tsx';
import { SynthesisContext } from './cockpit/SynthesisContext.ts';

// ---------------------------------------------------------------------------
// Crown glyph SVGs (16px inline, aria-hidden)
// ---------------------------------------------------------------------------

function CrownExecGlyph(): JSX.Element {
  return (
    <svg width="14" height="14" viewBox="0 0 14 14" aria-hidden="true" focusable="false">
      <path d="M2 11 L7 4 L12 11 Z" fill="none" stroke="currentColor" strokeWidth="1.2" />
      <rect x="1" y="11" width="12" height="1.5" fill="currentColor" />
    </svg>
  );
}

function CrownForgeGlyph(): JSX.Element {
  return (
    <svg width="14" height="14" viewBox="0 0 14 14" aria-hidden="true" focusable="false">
      <rect x="2" y="4" width="10" height="7" rx="1" fill="none" stroke="currentColor" strokeWidth="1.2" />
      <line x1="5" y1="4" x2="5" y2="2" stroke="currentColor" strokeWidth="1.2" />
      <line x1="9" y1="4" x2="9" y2="2" stroke="currentColor" strokeWidth="1.2" />
    </svg>
  );
}

function CrownGarlandGlyph(): JSX.Element {
  return (
    <svg width="14" height="14" viewBox="0 0 14 14" aria-hidden="true" focusable="false">
      <path d="M2 10 Q3.5 4 7 4 Q10.5 4 12 10" fill="none" stroke="currentColor" strokeWidth="1.2" />
      <circle cx="2" cy="10" r="1.2" fill="currentColor" />
      <circle cx="12" cy="10" r="1.2" fill="currentColor" />
      <circle cx="7" cy="4" r="1.2" fill="currentColor" />
    </svg>
  );
}

// ---------------------------------------------------------------------------
// Crown registry — maps squad slugs to Crown family (extend as squads grow)
// ---------------------------------------------------------------------------

type CrownFamily = 'exec' | 'forge' | 'garland';

const CROWN_MAP: Record<string, CrownFamily> = {
  // Executive
  executive: 'exec',
  legal: 'exec',
  finance: 'exec',
  compliance: 'exec',
  // Forge
  engineering: 'forge',
  forge: 'forge',
  platform: 'forge',
  infra: 'forge',
  devops: 'forge',
  security: 'forge',
  // Garland
  garland: 'garland',
  marketing: 'garland',
  creative: 'garland',
  design: 'garland',
  product: 'garland',
  research: 'garland',
};

function crownOf(slug: string): CrownFamily {
  return CROWN_MAP[slug.toLowerCase()] ?? 'forge';
}

// ---------------------------------------------------------------------------
// Global bridge health + pending-gates state (top-bar probe)
// ---------------------------------------------------------------------------

interface BridgeStatus {
  live: boolean;
  offline: boolean;
  offlineSince: number | null;
  pendingGates: number;
  budgetPct: number;
}

function useBridgeStatus(): BridgeStatus {
  const [status, setStatus] = useState<BridgeStatus>({
    live: true,
    offline: false,
    offlineSince: null,
    pendingGates: 0,
    budgetPct: 0,
  });

  const probe = useCallback(async () => {
    try {
      const [healthRes, hitlRes] = await Promise.allSettled([
        fetch('/api/health'),
        fetch('/api/hitl'),
      ]);

      const healthOk = healthRes.status === 'fulfilled' && healthRes.value.ok;

      let gates = 0;
      if (hitlRes.status === 'fulfilled' && hitlRes.value.ok) {
        const body = (await hitlRes.value.json()) as unknown;
        if (Array.isArray(body)) gates = body.length;
        else if (body && typeof body === 'object' && 'items' in body) {
          gates = ((body as { items?: unknown[] }).items ?? []).length;
        } else if (body && typeof body === 'object' && 'count' in body) {
          gates = Number((body as { count?: number }).count ?? 0);
        }
      }

      setStatus((prev) => ({
        live: healthOk,
        offline: !healthOk,
        offlineSince: healthOk ? null : (prev.offlineSince ?? Date.now()),
        pendingGates: gates,
        budgetPct: prev.budgetPct,
      }));
    } catch {
      setStatus((prev) => ({
        ...prev,
        live: false,
        offline: true,
        offlineSince: prev.offlineSince ?? Date.now(),
      }));
    }
  }, []);

  useEffect(() => {
    void probe();
    const interval = setInterval(() => { void probe(); }, 8000);
    return () => clearInterval(interval);
  }, [probe]);

  return status;
}

// ---------------------------------------------------------------------------
// Hash routing hook
// ---------------------------------------------------------------------------

function useHashView() {
  const [parsed, setParsed] = useState(() => parseView(window.location.hash));
  useEffect(() => {
    function onHash(): void {
      setParsed(parseView(window.location.hash));
    }
    window.addEventListener('hashchange', onHash);
    return () => window.removeEventListener('hashchange', onHash);
  }, []);
  return parsed;
}

// ---------------------------------------------------------------------------
// Gate expiry SR beacon (§8 pending-gate cadence)
// ---------------------------------------------------------------------------

interface GateSRBeaconProps {
  pendingGates: number;
}

function GateSRBeacon({ pendingGates }: GateSRBeaconProps): JSX.Element {
  const [announcement, setAnnouncement] = useState('');
  const [assertiveMsg, setAssertiveMsg] = useState('');
  const intervalRef = useRef<ReturnType<typeof setInterval> | null>(null);
  const fiveMinFiredRef = useRef(false);

  useEffect(() => {
    if (pendingGates === 0) {
      if (intervalRef.current) clearInterval(intervalRef.current);
      fiveMinFiredRef.current = false;
      return;
    }

    // Announce every 60s; ramp at <5 min and <1 min (simulated from pendingGates presence)
    const cadenceMs = 60000;
    function announce(): void {
      setAnnouncement(`${pendingGates} pending gate${pendingGates !== 1 ? 's' : ''} requiring action.`);
    }

    if (intervalRef.current) clearInterval(intervalRef.current);
    intervalRef.current = setInterval(announce, cadenceMs);

    // One-time assertive at 5-min mark — simplified: fire once on mount when gates present
    if (!fiveMinFiredRef.current) {
      setAssertiveMsg(`Gate action required: ${pendingGates} pending gate${pendingGates !== 1 ? 's' : ''}.`);
      fiveMinFiredRef.current = true;
      setTimeout(() => setAssertiveMsg(''), 5000);
    }

    return () => {
      if (intervalRef.current) clearInterval(intervalRef.current);
    };
  }, [pendingGates, assertiveMsg]);

  return (
    <>
      <span
        aria-live="polite"
        className="sr-only"
        aria-atomic="true"
        data-testid="gate-sr-polite"
      >
        {announcement}
      </span>
      <span
        aria-live="assertive"
        className="sr-only"
        aria-atomic="true"
        data-testid="gate-sr-assertive"
      >
        {assertiveMsg}
      </span>
    </>
  );
}

// ---------------------------------------------------------------------------
// Body Rail component
// ---------------------------------------------------------------------------

interface Workflow {
  workflow_id: string;
  phase?: string;
  root_goal?: string;
  has_pending_hitl?: boolean;
  selected_squads?: string[];
}

interface BodyRailProps {
  currentWorkflowId?: string | undefined;
  online: boolean;
}

function BodyRail({ currentWorkflowId, online }: BodyRailProps): JSX.Element {
  const [workflows, setWorkflows] = useState<Workflow[]>([]);
  const [loading, setLoading] = useState(true);

  useEffect(() => {
    let active = true;
    async function load(): Promise<void> {
      try {
        const res = await fetch('/api/workflows?limit=50');
        if (!res.ok) throw new Error('failed');
        const body = (await res.json()) as unknown;
        let wfs: Workflow[] = [];
        if (Array.isArray(body)) wfs = body as Workflow[];
        else if (body && typeof body === 'object' && 'workflows' in body) {
          wfs = ((body as { workflows?: Workflow[] }).workflows) ?? [];
        }
        if (active) { setWorkflows(wfs); setLoading(false); }
      } catch {
        if (active) setLoading(false);
      }
    }
    void load();
    const t = setInterval(() => { void load(); }, 8000);
    return () => { active = false; clearInterval(t); };
  }, []);

  // Group active workflows by crown
  const active = workflows.filter((w) => !['done', 'surfaced'].includes(w.phase ?? ''));
  const recent = workflows.filter((w) => ['done', 'surfaced'].includes(w.phase ?? '')).slice(0, 5);

  // Collect unique squads from all workflows
  const allSquads = new Set<string>();
  workflows.forEach((w) => (w.selected_squads ?? []).forEach((s) => allSquads.add(s)));
  // Always show canonical Crown sections even if empty
  const execSquads = [...allSquads].filter((s) => crownOf(s) === 'exec');
  const forgeSquads = [...allSquads].filter((s) => crownOf(s) === 'forge');
  const garlandSquads = [...allSquads].filter((s) => crownOf(s) === 'garland');

  // Keyboard: F6 cycles Crown sections
  const execRef = useRef<HTMLElement>(null);
  const forgeRef = useRef<HTMLElement>(null);
  const garlandRef = useRef<HTMLElement>(null);
  useEffect(() => {
    const sections = [execRef, forgeRef, garlandRef];
    let idx = 0;
    function onKeyDown(e: KeyboardEvent): void {
      if (e.key === 'F6' && (e.target as HTMLElement)?.closest?.('.body-rail')) {
        e.preventDefault();
        idx = (idx + 1) % sections.length;
        (sections[idx].current as HTMLElement | null)?.focus();
      }
    }
    document.addEventListener('keydown', onKeyDown);
    return () => document.removeEventListener('keydown', onKeyDown);
  }, []);

  function phaseIcon(phase?: string): string {
    if (!phase) return '○';
    if (phase === 'done') return '✓';
    if (phase === 'surfaced') return '⚠';
    if (['approval', 'judge'].includes(phase)) return '⚠';
    return '◐';
  }

  return (
    <nav
      className="body-rail"
      aria-label="Hydra constellation — workflow map"
      aria-busy={loading}
      data-testid="body-rail"
      role="navigation"
    >
      {/* Active workflows */}
      {active.length > 0 ? (
        <section className="crown-section" aria-label="Active workflows">
          <div className="crown-section-header">
            <span className="crown-glyph" aria-hidden="true">◎</span>
            <span>Active ({active.length})</span>
          </div>
          <ul className="body-tree" role="tree" aria-label="Active workflows">
            {active.map((wf) => (
              <li key={wf.workflow_id} role="treeitem" aria-label={`Workflow ${wf.workflow_id.slice(0, 8)}, phase ${wf.phase ?? 'unknown'}`}>
                <a
                  href={wf.has_pending_hitl
                    ? `#/gate/${encodeURIComponent(wf.workflow_id)}`
                    : `#/workflow/${encodeURIComponent(wf.workflow_id)}`}
                  className={`body-tree-item${currentWorkflowId === wf.workflow_id ? ' body-tree-item--active' : ''}${wf.has_pending_hitl ? ' body-tree-item--gate' : ''}`}
                  aria-current={currentWorkflowId === wf.workflow_id ? 'page' : undefined}
                >
                  <span aria-hidden="true">{phaseIcon(wf.phase)}</span>
                  <span className="mono" style={{ fontSize: '10px' }}>{wf.workflow_id.slice(0, 6)}…</span>
                  {wf.has_pending_hitl ? <span className="sr-only">— gate pending</span> : null}
                </a>
              </li>
            ))}
          </ul>
        </section>
      ) : null}

      <div className="body-rail-divider" role="separator" />

      {/* Executive Crown */}
      <section
        className="crown-section"
        aria-label="Executive Crown"
        aria-labelledby="crown-exec-heading"
        tabIndex={-1}
        ref={execRef as React.RefObject<HTMLElement>}
      >
        <div
          id="crown-exec-heading"
          className="crown-section-header crown-section-header--exec"
          aria-hidden="false"
        >
          <span className="crown-glyph" style={{ color: 'var(--crown-exec)' }} aria-hidden="true">
            <CrownExecGlyph />
          </span>
          Executive Crown
        </div>
        {execSquads.length > 0 ? (
          <ul className="body-tree" role="tree" aria-label="Executive Crown heads">
            {execSquads.map((s) => (
              <li key={s} role="treeitem">
                <span className="body-tree-item" style={{ color: 'var(--crown-exec)' }}>
                  <span className="sr-only">Executive head:</span>{s}
                </span>
              </li>
            ))}
          </ul>
        ) : (
          <div className="body-rail-status" aria-hidden="true">—</div>
        )}
      </section>

      {/* Forge Crown */}
      <section
        className="crown-section"
        aria-label="Forge Crown"
        aria-labelledby="crown-forge-heading"
        tabIndex={-1}
        ref={forgeRef as React.RefObject<HTMLElement>}
      >
        <div
          id="crown-forge-heading"
          className="crown-section-header crown-section-header--forge"
        >
          <span className="crown-glyph" style={{ color: 'var(--crown-forge)' }} aria-hidden="true">
            <CrownForgeGlyph />
          </span>
          Forge Crown
        </div>
        {forgeSquads.length > 0 ? (
          <ul className="body-tree" role="tree" aria-label="Forge Crown heads">
            {forgeSquads.map((s) => (
              <li key={s} role="treeitem">
                <span className="body-tree-item" style={{ color: 'var(--crown-forge)' }}>
                  <span className="sr-only">Forge head:</span>{s}
                </span>
              </li>
            ))}
          </ul>
        ) : (
          <div className="body-rail-status" aria-hidden="true">—</div>
        )}
      </section>

      {/* Garland Crown */}
      <section
        className="crown-section"
        aria-label="Garland Crown"
        aria-labelledby="crown-garland-heading"
        tabIndex={-1}
        ref={garlandRef as React.RefObject<HTMLElement>}
      >
        <div
          id="crown-garland-heading"
          className="crown-section-header crown-section-header--garland"
        >
          <span className="crown-glyph" style={{ color: 'var(--crown-garland)' }} aria-hidden="true">
            <CrownGarlandGlyph />
          </span>
          Garland Crown
        </div>
        {garlandSquads.length > 0 ? (
          <ul className="body-tree" role="tree" aria-label="Garland Crown heads">
            {garlandSquads.map((s) => (
              <li key={s} role="treeitem">
                <span className="body-tree-item" style={{ color: 'var(--crown-garland)' }}>
                  <span className="sr-only">Garland head:</span>{s}
                </span>
              </li>
            ))}
          </ul>
        ) : (
          <div className="body-rail-status" aria-hidden="true">—</div>
        )}
      </section>

      <div className="body-rail-divider" role="separator" />

      {/* Recent workflows */}
      {recent.length > 0 ? (
        <section className="crown-section" aria-label="Recent workflows">
          <div className="crown-section-header">
            <span aria-hidden="true">◇</span>
            <span>Recent ({recent.length})</span>
          </div>
          <ul className="body-tree" role="tree" aria-label="Recent workflows">
            {recent.map((wf) => (
              <li key={wf.workflow_id} role="treeitem">
                <a
                  href={`#/workflow/${encodeURIComponent(wf.workflow_id)}`}
                  className={`body-workflow-item body-workflow-item--${wf.phase === 'done' ? 'done' : 'active'}`}
                  aria-label={`Workflow ${wf.workflow_id.slice(0, 8)}, ${wf.phase}`}
                >
                  <span aria-hidden="true">{wf.phase === 'done' ? '✓' : '⚠'}</span>
                  <span className="mono" style={{ fontSize: '10px' }}>{wf.workflow_id.slice(0, 6)}…</span>
                </a>
              </li>
            ))}
          </ul>
        </section>
      ) : null}

      {/* Offline indicator */}
      {!online ? (
        <div className="body-rail-status" role="status" aria-live="polite">
          Bridge offline
        </div>
      ) : null}
    </nav>
  );
}

// ---------------------------------------------------------------------------
// Oracle Rail component — always-on one voice
// ---------------------------------------------------------------------------

interface OracleRailProps {
  latestSynthesis: string | null;
  isExecuting: boolean;
}

function OracleRail({ latestSynthesis, isExecuting }: OracleRailProps): JSX.Element {
  const [assertive, setAssertive] = useState('');
  const prevSynthesis = useRef<string | null>(null);

  // Switch aria-live to assertive on new synthesis arrival, then revert to polite
  useEffect(() => {
    if (latestSynthesis && latestSynthesis !== prevSynthesis.current) {
      setAssertive(`Hydra speaks: ${latestSynthesis}`);
      prevSynthesis.current = latestSynthesis;
      const t = setTimeout(() => setAssertive(''), 3000);
      return () => clearTimeout(t);
    }
    return undefined;
  }, [latestSynthesis]);

  const lines = latestSynthesis ? latestSynthesis.split('. ').filter(Boolean) : [];

  return (
    <aside
      className="oracle-rail"
      aria-label="The Oracle — Hydra's synthesized declaration"
      data-testid="oracle-rail"
    >
      {/* Assertive SR-only announcement on new synthesis */}
      <span aria-live="assertive" className="sr-only" aria-atomic="true">
        {assertive}
      </span>

      <header className="oracle-header">
        <span
          className="oracle-spirit-dot spirit-pulse-host"
          aria-hidden="true"
          data-testid="oracle-spirit-dot"
        />
        <span className="oracle-header-label">The Oracle</span>
      </header>

      <div
        className="oracle-declaration"
        aria-live="polite"
        aria-label="Hydra's latest synthesis declaration"
        data-testid="oracle-declaration"
      >
        {isExecuting && !latestSynthesis ? (
          <p className="oracle-assembling" aria-label="Synthesis assembling">
            Assembling…
          </p>
        ) : lines.length > 0 ? (
          lines.map((line, i) => (
            <p
              key={i}
              className="oracle-voice-line oracle-line"
              style={{ '--line-index': i } as React.CSSProperties}
            >
              {line}{i < lines.length - 1 ? '.' : ''}
            </p>
          ))
        ) : (
          <p className="oracle-placeholder">
            No synthesis yet.
          </p>
        )}
      </div>

      {latestSynthesis ? (
        <div className="oracle-dissents" aria-label="Dissents">
          <span className="oracle-dissent-icon" aria-hidden="true">◇</span>
          <span className="sr-only">Dissents available</span>
        </div>
      ) : null}
    </aside>
  );
}

// ---------------------------------------------------------------------------
// App shell
// ---------------------------------------------------------------------------

export function App(): JSX.Element {
  const { live, offline, offlineSince: offlineSinceRaw, pendingGates, budgetPct } = useBridgeStatus();
  const offlineSince = offlineSinceRaw ?? undefined;
  const parsed = useHashView();

  const isOnline = live && !offline;

  // Click-to-pause sigil toggle (§8 IMMORTAL HEAD BAR)
  const [attestPaused, setAttestPaused] = useState(false);

  // Synthesis state — lifted from LiveWorkflowView via SynthesisContext (R3)
  const [latestSynthesis, setLatestSynthesisState] = useState<string | null>(null);
  const [isExecuting, setIsExecutingState] = useState(false);

  // Stable setSynthesis callback for the context value
  const setSynthesis = useCallback((text: string | null, executing: boolean) => {
    setLatestSynthesisState(text);
    setIsExecutingState(executing);
  }, []);

  // Direct-jump keyboard shortcuts (§5, §8)
  useEffect(() => {
    function onKeyDown(e: KeyboardEvent): void {
      // Don't intercept when typing in inputs
      const tag = (e.target as HTMLElement).tagName;
      if (['INPUT', 'TEXTAREA', 'SELECT'].includes(tag)) return;
      if (e.metaKey || e.ctrlKey || e.altKey) return;

      switch (e.key) {
        case 'S': // Spirit / Launchpad
          e.preventDefault();
          window.location.hash = '#/';
          break;
        case 'G': // Next Gate
          e.preventDefault();
          if (pendingGates > 0) window.location.hash = '#/';
          break;
        case 'B': { // Body rail focus
          e.preventDefault();
          const rail = document.querySelector<HTMLElement>('[data-testid="body-rail"]');
          rail?.focus();
          break;
        }
        case 'O': { // Oracle focus
          e.preventDefault();
          const oracle = document.querySelector<HTMLElement>('[data-testid="oracle-rail"]');
          oracle?.setAttribute('tabindex', '-1');
          oracle?.focus();
          break;
        }
        case 'P': { // Phase Rail (stub — R3)
          e.preventDefault();
          // Phase Rail implementation in R3
          break;
        }
        case 'M': // Memory
          e.preventDefault();
          window.location.hash = '#/memory';
          break;
      }
    }
    document.addEventListener('keydown', onKeyDown);
    return () => document.removeEventListener('keydown', onKeyDown);
  }, [pendingGates]);

  // Build nav link className helper
  function navClass(view: string): string {
    return `immortal-nav-link${parsed.view === view ? ' immortal-nav-link--active' : ''}`;
  }

  function handleLaunched(workflowId: string): void {
    window.location.hash = `#/workflow/${encodeURIComponent(workflowId)}`;
  }

  // Render the active view
  function renderView(): JSX.Element {
    switch (parsed.view) {
      case 'launchpad':
        return <LaunchpadView live={live} offline={offline} offlineSince={offlineSince} />;

      case 'launch': {
        const goal = parsed.params.get('goal') ?? '';
        const squadsParam = parsed.params.get('squads') ?? '';
        const squads = squadsParam ? squadsParam.split(',').map((s) => s.trim()).filter(Boolean) : [];
        return (
          <LaunchComposerView
            initialGoal={goal}
            initialSquads={squads}
            online={isOnline}
            onLaunched={handleLaunched}
          />
        );
      }

      case 'workflow':
        if (!parsed.id) {
          return (
            <div className="state-screen state-error" role="alert">
              No workflow ID in URL. <a href="#/">Go to Launchpad</a>
            </div>
          );
        }
        return <LiveWorkflowView workflowId={parsed.id} online={isOnline} />;

      case 'gate':
        if (!parsed.id) {
          return (
            <div className="state-screen state-error" role="alert">
              No workflow ID in URL for gate. <a href="#/">Go to Launchpad</a>
            </div>
          );
        }
        return <GateCockpitView workflowId={parsed.id} online={isOnline} />;

      case 'squads':
        return <SquadsView online={isOnline} />;

      case 'campaigns':
        return <CampaignsView online={isOnline} />;

      case 'memory':
        return <MemoryView online={isOnline} />;

      default:
        return <LaunchpadView live={live} offline={offline} offlineSince={offlineSince} />;
    }
  }

  const budgetFillClass = budgetPct >= 1.0
    ? 'immortal-budget-fill immortal-budget-fill--critical'
    : budgetPct >= 0.8
      ? 'immortal-budget-fill immortal-budget-fill--warn'
      : 'immortal-budget-fill';

  const synthesisContextValue = { latestSynthesis, isExecuting, setSynthesis };

  return (
    <SynthesisContext.Provider value={synthesisContextValue}>
    <div
      className={`cockpit-shell${offline ? ' cockpit-shell--offline' : ''}`}
      data-testid="cockpit-shell"
      data-attest-paused={attestPaused ? '' : undefined}
    >
      {/* Skip-to-content — first focusable element */}
      <a href="#main-working" className="skip-link">
        Skip to main content
      </a>

      {/* SR beacon for gate expiry cadence */}
      <GateSRBeacon pendingGates={pendingGates} />

      {/* ----------------------------------------------------------------
          IMMORTAL HEAD — constitutional crown bar (never scrolls)
      ---------------------------------------------------------------- */}
      <header
        className="immortal-head-bar"
        data-testid="immortal-head-bar"
      >
        {/* Sigil anchor — click-to-pause Spirit pulse */}
        <button
          className="immortal-sigil spirit-pulse-host"
          onClick={() => setAttestPaused((p) => !p)}
          aria-label={
            attestPaused
              ? 'Hydra Constitution loaded — pulse paused. Click to resume.'
              : 'Hydra Constitution loaded — all decisions attested. Click to pause pulse.'
          }
          aria-pressed={attestPaused}
          title="Hydra Constitution loaded — all decisions attested"
          data-testid="immortal-sigil"
        >
          <img
            src="/images/chosen/immortal-head.png"
            alt=""
            aria-hidden="true"
          />
        </button>

        {/* Motto */}
        <div className="immortal-motto">
          <span className="immortal-motto-text">One Spirit. Many gifts.</span>
          <span className="immortal-motto-sub">CONSTITVTION ATTEST</span>
        </div>

        {/* Navigation */}
        <nav className="immortal-nav" aria-label="Main navigation">
          <a href="#/" className={navClass('launchpad')} aria-label="Launchpad — active workflows">
            Launchpad
          </a>
          <a href="#/launch" className={navClass('launch')} aria-label="Launch Composer — new workflow">
            Launch
          </a>
          <a href="#/squads" className={navClass('squads')} aria-label="Squads — squad packs">
            Squads
          </a>
          <a href="#/campaigns" className={navClass('campaigns')} aria-label="Campaigns — workflow rollups">
            Campaigns
          </a>
          <a href="#/memory" className={navClass('memory')} aria-label="Memory — episodic grid and search">
            Memory
          </a>
        </nav>

        {/* Right cluster */}
        <div className="immortal-right">
          {/* Bridge health dot */}
          <span
            className={`bridge-health ${live ? 'bridge-health--live' : 'bridge-health--offline'}`}
            role="status"
            aria-live="polite"
            aria-label={live ? 'Bridge online' : 'Bridge offline — write actions disabled'}
            data-testid="bridge-pulse"
          >
            <span className="bridge-health-dot" aria-hidden="true" />
            {live ? 'live' : 'offline'}
          </span>

          {/* Budget sinew band */}
          <div
            className="immortal-budget-band"
            aria-hidden="true"
            title="Global budget utilization"
          >
            <div
              className="immortal-budget-track"
              role="meter"
              aria-valuenow={Math.round(budgetPct * 100)}
              aria-valuemin={0}
              aria-valuemax={100}
              aria-label={`Budget: ${Math.round(budgetPct * 100)}% consumed`}
              style={{ '--budget-pct': budgetPct } as React.CSSProperties}
            >
              <div
                className={`${budgetFillClass}${budgetPct >= 1.0 ? ' budget-alarm' : ''}`}
                style={{ width: `${Math.min(100, budgetPct * 100)}%` }}
              />
            </div>
          </div>

          {/* Pending gate beacon */}
          {pendingGates > 0 ? (
            <a
              href="#/"
              className="pending-gates-badge"
              aria-label={`${pendingGates} pending gate${pendingGates !== 1 ? 's' : ''} — action required`}
              data-testid="pending-gates-badge"
            >
              <span aria-hidden="true">⚠</span>
              {pendingGates} gate{pendingGates !== 1 ? 's' : ''}
            </a>
          ) : null}

          {/* New Run CTA */}
          <a
            href={isOnline ? '#/launch' : undefined}
            className={`new-run-cta${!isOnline ? ' new-run-cta--disabled' : ''}`}
            aria-label="Start a new workflow run"
            aria-disabled={!isOnline}
            data-testid="new-run-cta"
          >
            + New Run
          </a>
        </div>
      </header>

      {/* ----------------------------------------------------------------
          THREE-COLUMN BODY
      ---------------------------------------------------------------- */}
      <div className="cockpit-body">
        {/* THE BODY — left rail: accessible heads + workflows tree */}
        <BodyRail
          currentWorkflowId={parsed.id}
          online={isOnline}
        />

        {/* THE WORKING — center router outlet */}
        <main
          className="working-center"
          id="main-working"
          tabIndex={-1}
          aria-label="The Working — main content"
          data-testid="working-center"
        >
          <div className="working-inner">
            {renderView()}
          </div>
        </main>

        {/* THE ORACLE — right rail: always-on one voice */}
        <OracleRail
          latestSynthesis={latestSynthesis}
          isExecuting={isExecuting}
        />
      </div>
    </div>
    </SynthesisContext.Provider>
  );
}
