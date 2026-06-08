/**
 * Hydra Cockpit — App shell.
 * Top bar: HYDRA COCKPIT logo, nav links, live/offline pulse, pending-gates counter, New Run CTA.
 * Hash routing across 7 views. No router library — hash-based parseView.
 */

import { useCallback, useEffect, useState } from 'react';
import { parseView } from './cockpit/types.ts';
import { LaunchpadView } from './views/LaunchpadView.tsx';
import { LaunchComposerView } from './views/LaunchComposerView.tsx';
import { LiveWorkflowView } from './views/LiveWorkflowView.tsx';
import { GateCockpitView } from './views/GateCockpitView.tsx';
import { SquadsView } from './views/SquadsView.tsx';
import { CampaignsView } from './views/CampaignsView.tsx';
import { MemoryView } from './views/MemoryView.tsx';

// ---------------------------------------------------------------------------
// Global bridge health + pending-gates state (lightweight top-bar probe)
// ---------------------------------------------------------------------------

interface BridgeStatus {
  live: boolean;
  offline: boolean;
  offlineSince: number | null;
  pendingGates: number;
}

function useBridgeStatus(): BridgeStatus {
  const [status, setStatus] = useState<BridgeStatus>({
    live: true,
    offline: false,
    offlineSince: null,
    pendingGates: 0,
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
// App shell
// ---------------------------------------------------------------------------

export function App(): JSX.Element {
  const { live, offline, offlineSince: offlineSinceRaw, pendingGates } = useBridgeStatus();
  const offlineSince = offlineSinceRaw ?? undefined;
  const parsed = useHashView();

  const isOnline = live && !offline;

  // Build nav link className helper
  function navClass(view: string): string {
    return `nav-link${parsed.view === view ? ' nav-link--active' : ''}`;
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

  return (
    <div className={`cockpit-shell${offline ? ' cockpit-shell--offline' : ''}`} data-testid="cockpit-shell">
      {/* Top bar */}
      <header className="top-bar" role="banner">
        <div className="top-bar-brand">
          <span className="brand-logo" aria-label="Hydra Cockpit">
            HYDRA <span className="brand-accent">COCKPIT</span>
          </span>
        </div>

        <nav className="top-bar-nav" aria-label="Main navigation">
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

        <div className="top-bar-right">
          {/* Live/offline pulse */}
          <span
            className={`bridge-pulse ${live ? 'pulse--live' : 'pulse--offline'}`}
            role="status"
            aria-live="polite"
            aria-label={live ? 'Bridge online' : 'Bridge offline — write actions disabled'}
            data-testid="bridge-pulse"
          >
            {live ? '● live' : '⊗ offline'}
          </span>

          {/* Pending gates counter */}
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
            className={`btn btn-primary${!isOnline ? ' btn-disabled' : ''}`}
            aria-label="Start a new workflow run"
            aria-disabled={!isOnline}
            data-testid="new-run-cta"
          >
            + New Run
          </a>
        </div>
      </header>

      {/* Main content */}
      <main className="cockpit-main" id="main-content" tabIndex={-1}>
        {renderView()}
      </main>
    </div>
  );
}
