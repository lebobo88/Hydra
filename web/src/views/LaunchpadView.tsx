/**
 * Hydra Cockpit — Launchpad view (#/).
 * Active + recent workflows; phase chips; budget bars; gate badges.
 * A pending gate is the loudest element on the card.
 * 8-state handling: loading / empty / error / degraded / offline / partial / live / confirm
 */

import { useEffect, useState } from 'react';
import { LoadingScreen, ErrorScreen, DegradedBanner, OfflineBanner } from '../components/StateScreens.tsx';
import { PhaseMachine } from '../components/PhaseMachine.tsx';
import { BudgetBar } from '../components/BudgetBar.tsx';
import type { WorkflowSummary } from '../api/client.ts';

interface LaunchpadViewProps {
  live: boolean;
  offline: boolean;
  offlineSince?: number | undefined;
}

const ACTIVE_PHASES = new Set(['intake', 'planning', 'approval', 'dispatch', 'executing', 'judge', 'synthesis', 'postcheck']);

function isActive(wf: WorkflowSummary): boolean {
  return !!wf.phase && ACTIVE_PHASES.has(wf.phase);
}

function isRecent(wf: WorkflowSummary): boolean {
  return wf.phase === 'done' || wf.phase === 'surfaced';
}

function formatRelative(iso: string | undefined): string {
  if (!iso) return '';
  const then = new Date(iso).getTime();
  if (isNaN(then)) return iso;
  const diff = Math.floor((Date.now() - then) / 1000);
  if (diff < 60) return `${diff}s ago`;
  if (diff < 3600) return `${Math.floor(diff / 60)}m ago`;
  if (diff < 86400) return `${Math.floor(diff / 3600)}h ago`;
  return `${Math.floor(diff / 86400)}d ago`;
}

interface WorkflowCardProps {
  wf: WorkflowSummary;
  online: boolean;
}

function WorkflowCard({ wf, online }: WorkflowCardProps): JSX.Element {
  const hasPendingGate = wf.has_pending_hitl ?? false;
  const isTerminal = wf.phase === 'done' || wf.phase === 'surfaced';
  const shortId = wf.workflow_id.slice(0, 8);
  const squads = (wf.selected_squads ?? []).join(', ');

  const budget = wf.budget;
  const spentUsd = budget?.spent_usd ?? 0;
  const budgetUsd = budget?.budget_usd ?? 0;

  return (
    <article
      className={`wf-card${hasPendingGate ? ' wf-card--gate' : ''}${isTerminal ? ' wf-card--terminal' : ''}`}
      aria-label={`Workflow ${shortId}${hasPendingGate ? ', pending gate' : ''}`}
    >
      {hasPendingGate ? (
        <div className="gate-badge" role="status" aria-label="Pending HITL gate — action required">
          <span aria-hidden="true">⚠</span> GATE: requires action
        </div>
      ) : null}

      <header className="wf-card-header">
        <span className="wf-id mono">{shortId}</span>
        <span className="wf-goal">{wf.root_goal ?? '(no goal)'}</span>
        {wf.phase ? (
          <span className={`wf-phase-badge wf-phase--${wf.phase}`}>{wf.phase}</span>
        ) : null}
      </header>

      <div className="wf-card-body">
        <PhaseMachine
          currentPhase={wf.phase}
          terminalState={isTerminal ? (wf.phase as 'done' | 'surfaced') : null}
        />
        {squads ? <div className="wf-squads text-muted text-sm">{squads}</div> : null}
        {wf.updated_at ? (
          <div className="wf-updated text-muted text-sm">{formatRelative(wf.updated_at)}</div>
        ) : null}
        {budgetUsd > 0 ? (
          <BudgetBar spent={spentUsd} budget={budgetUsd} compact />
        ) : null}
      </div>

      <footer className="wf-card-footer">
        {hasPendingGate ? (
          <a
            href={`#/gate/${encodeURIComponent(wf.workflow_id)}`}
            className="btn btn-danger btn-sm"
            aria-label={`Open gate for workflow ${shortId}`}
          >
            Open gate ▸
          </a>
        ) : null}
        {isTerminal ? (
          <a
            href={`#/workflow/${encodeURIComponent(wf.workflow_id)}`}
            className="btn btn-ghost btn-sm"
            aria-label={`Open workflow ${shortId}`}
          >
            Open ▸
          </a>
        ) : (
          <a
            href={`#/workflow/${encodeURIComponent(wf.workflow_id)}`}
            className="btn btn-sm"
            aria-label={`Open workflow ${shortId}`}
          >
            Open ▸
          </a>
        )}
        {isTerminal && online ? (
          <a
            href={`#/workflow/${encodeURIComponent(wf.workflow_id)}`}
            className="btn btn-ghost btn-sm"
            aria-label={`Replay workflow ${shortId}`}
          >
            Replay ▸
          </a>
        ) : null}
      </footer>
    </article>
  );
}

export function LaunchpadView({ live, offline, offlineSince }: LaunchpadViewProps): JSX.Element {
  const [workflows, setWorkflows] = useState<WorkflowSummary[] | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [degraded, setDegraded] = useState(false);
  const [degradedSources, setDegradedSources] = useState<string[]>([]);

  async function load(): Promise<void> {
    setLoading(true);
    setError(null);
    try {
      const res = await fetch('/api/workflows?limit=50');
      if (!res.ok) throw new Error(`bridge error: ${res.status}`);
      const body = (await res.json()) as unknown;
      // Normalize: may be { workflows: [...] } or [...] depending on hydra-mem version
      let wfs: WorkflowSummary[] = [];
      if (Array.isArray(body)) {
        wfs = body as WorkflowSummary[];
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
      setLoading(false);
    }
  }

  useEffect(() => {
    void load();
    const interval = setInterval(() => { void load(); }, 8000);
    return () => clearInterval(interval);
  // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  if (loading && workflows === null) return <LoadingScreen label="Loading workflows…" />;

  if (error && workflows === null) {
    return <ErrorScreen message={`Failed to load workflows: ${error}`} onRetry={() => { void load(); }} />;
  }

  const active = (workflows ?? []).filter(isActive);
  const recent = (workflows ?? []).filter(isRecent);
  const online = live && !offline;

  return (
    <div className="launchpad-view">
      {offline ? <OfflineBanner since={offlineSince ?? undefined} /> : null}
      {!offline && degraded ? (
        <DegradedBanner sources={degradedSources} message="Workflow list may be incomplete" />
      ) : null}

      <section className="wf-section" aria-labelledby="active-heading">
        <h2 id="active-heading" className="section-heading">
          Active ({active.length})
        </h2>
        {active.length === 0 ? (
          <div className="empty-hint">
            {degraded
              ? 'Active workflows cannot be confirmed — source unreachable. An empty list is not evidence of none.'
              : 'No active workflows.'}
          </div>
        ) : (
          <div className="wf-card-grid" role="list" aria-label="Active workflows">
            {active.map((wf) => (
              <div key={wf.workflow_id} role="listitem">
                <WorkflowCard wf={wf} online={online} />
              </div>
            ))}
          </div>
        )}
      </section>

      <section className="wf-section" aria-labelledby="recent-heading">
        <h2 id="recent-heading" className="section-heading">
          Recent ({recent.length})
        </h2>
        {recent.length === 0 ? (
          <div className="empty-hint text-muted text-sm">No completed workflows.</div>
        ) : (
          <div className="recent-list" role="list" aria-label="Recent workflows">
            {recent.slice(0, 10).map((wf) => (
              <div key={wf.workflow_id} className="recent-item" role="listitem">
                <span className="mono text-sm">{wf.workflow_id.slice(0, 8)}…</span>
                <span className="recent-goal">{wf.root_goal ?? '(no goal)'}</span>
                <span className={`wf-phase-badge wf-phase--${wf.phase}`}>{wf.phase}</span>
                {wf.updated_at ? (
                  <span className="text-muted text-sm">{formatRelative(wf.updated_at)}</span>
                ) : null}
                <a
                  href={`#/workflow/${encodeURIComponent(wf.workflow_id)}`}
                  className="btn btn-ghost btn-sm"
                  aria-label={`Open workflow ${wf.workflow_id.slice(0, 8)}`}
                >
                  Open ▸
                </a>
              </div>
            ))}
          </div>
        )}
      </section>

      {error && workflows !== null ? (
        <div className="inline-error text-sm" role="alert">
          Refresh failed: {error}.{' '}
          <button className="btn-link" type="button" onClick={() => { void load(); }}>
            Retry
          </button>
        </div>
      ) : null}
    </div>
  );
}
