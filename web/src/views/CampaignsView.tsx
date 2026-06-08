/**
 * Hydra Cockpit — Campaigns view (#/campaigns).
 * Campaign rollups derived from /api/workflows (grouped by a campaign tag or
 * inferred from goal prefix). Shows per-phase budget vs eights cap.
 * 8-state handling.
 */

import { useEffect, useState } from 'react';
import { LoadingScreen, EmptyScreen, ErrorScreen, DegradedBanner, OfflineBanner } from '../components/StateScreens.tsx';
import { BudgetBar } from '../components/BudgetBar.tsx';
import type { WorkflowSummary } from '../api/client.ts';

interface Campaign {
  id: string;
  label: string;
  workflows: WorkflowSummary[];
  totalSpent: number;
  totalBudget: number;
}

/** Group workflows into campaigns by shared goal prefix (first 30 chars) or explicit campaign tag. */
function groupIntoCampaigns(workflows: WorkflowSummary[]): Campaign[] {
  const groups = new Map<string, WorkflowSummary[]>();
  for (const wf of workflows) {
    // Use first ~30 chars of goal as a rough campaign key
    const key = (wf.root_goal ?? '').slice(0, 30).trim() || wf.workflow_id.slice(0, 8);
    if (!groups.has(key)) groups.set(key, []);
    groups.get(key)!.push(wf);
  }
  const campaigns: Campaign[] = [];
  for (const [label, wfs] of groups) {
    const totalSpent = wfs.reduce((acc, w) => acc + (w.budget?.spent_usd ?? 0), 0);
    const totalBudget = wfs.reduce((acc, w) => acc + (w.budget?.budget_usd ?? 0), 0);
    campaigns.push({
      id: wfs[0]!.workflow_id.slice(0, 8),
      label,
      workflows: wfs,
      totalSpent,
      totalBudget,
    });
  }
  return campaigns;
}

interface CampaignsViewProps {
  online: boolean;
}

export function CampaignsView({ online }: CampaignsViewProps): JSX.Element {
  const [campaigns, setCampaigns] = useState<Campaign[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [degraded, setDegraded] = useState(false);

  async function load(): Promise<void> {
    setLoading(true);
    setError(null);
    try {
      const res = await fetch('/api/workflows?limit=200');
      if (!res.ok) throw new Error(`workflows error: ${res.status}`);
      const body = (await res.json()) as unknown;
      let wfs: WorkflowSummary[] = [];
      if (Array.isArray(body)) wfs = body as WorkflowSummary[];
      else if (body && typeof body === 'object') {
        const b = body as { workflows?: WorkflowSummary[]; degraded?: boolean };
        wfs = b.workflows ?? [];
        if (b.degraded) setDegraded(true);
      }
      setCampaigns(groupIntoCampaigns(wfs));
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
    } finally {
      setLoading(false);
    }
  }

  useEffect(() => { void load(); }, []);

  if (loading) return <LoadingScreen label="Loading campaigns…" />;
  if (error) return <ErrorScreen message={error} onRetry={() => { void load(); }} />;

  return (
    <div className="campaigns-view">
      <header className="view-header">
        <a href="#/" className="back-link">← Launchpad</a>
        <h1 className="view-title">Campaigns</h1>
      </header>

      {!online ? <OfflineBanner /> : null}
      {degraded ? (
        <DegradedBanner sources={['hydra-mem']} message="Campaign data may be incomplete" />
      ) : null}

      {campaigns.length === 0 ? (
        <EmptyScreen
          message={degraded
            ? 'Campaign data unavailable — source unreachable. An empty list is not evidence there are none.'
            : 'No campaigns yet. Launch a workflow to start one.'}
          action={
            <a href="#/launch" className="btn btn-primary">
              New Run
            </a>
          }
        />
      ) : (
        <div
          className="campaigns-list"
          role="list"
          aria-label={`${campaigns.length} campaign${campaigns.length !== 1 ? 's' : ''}`}
        >
          {campaigns.map((c) => (
            <CampaignCard key={c.id} campaign={c} online={online} />
          ))}
        </div>
      )}
    </div>
  );
}

function CampaignCard({ campaign, online }: { campaign: Campaign; online: boolean }): JSX.Element {
  const [expanded, setExpanded] = useState(false);
  const activeCount = campaign.workflows.filter((w) => w.phase && !['done', 'surfaced'].includes(w.phase)).length;
  const doneCount = campaign.workflows.filter((w) => w.phase === 'done').length;
  const surfacedCount = campaign.workflows.filter((w) => w.phase === 'surfaced').length;

  return (
    <article className="campaign-card" role="listitem" aria-label={`Campaign: ${campaign.label}`}>
      <header className="campaign-card-header">
        <div className="campaign-header-main">
          <span className="campaign-id mono text-sm">{campaign.id}</span>
          <span className="campaign-label">{campaign.label}</span>
        </div>
        <div className="campaign-stats text-sm">
          {activeCount > 0 ? (
            <span className="campaign-stat campaign-stat--active">
              {activeCount} active
            </span>
          ) : null}
          {doneCount > 0 ? (
            <span className="campaign-stat campaign-stat--done">
              {doneCount} done
            </span>
          ) : null}
          {surfacedCount > 0 ? (
            <span className="campaign-stat campaign-stat--surfaced">
              {surfacedCount} surfaced
            </span>
          ) : null}
          <span className="campaign-total text-muted">
            {campaign.workflows.length} workflow{campaign.workflows.length !== 1 ? 's' : ''}
          </span>
        </div>
      </header>

      {campaign.totalBudget > 0 ? (
        <div className="campaign-budget">
          <BudgetBar spent={campaign.totalSpent} budget={campaign.totalBudget} compact />
        </div>
      ) : null}

      <div className="campaign-actions">
        <button
          type="button"
          className="btn btn-ghost btn-sm"
          onClick={() => setExpanded((e) => !e)}
          aria-expanded={expanded}
          aria-controls={`campaign-wfs-${campaign.id}`}
        >
          {expanded ? 'Hide' : 'Show'} workflows ({campaign.workflows.length})
        </button>
        {online ? (
          <a
            href={`#/launch?goal=${encodeURIComponent(campaign.label)}`}
            className="btn btn-sm"
            aria-label={`Use campaign goal as hint for a new run`}
          >
            Use as hint ▸
          </a>
        ) : null}
      </div>

      {expanded ? (
        <ul
          id={`campaign-wfs-${campaign.id}`}
          className="campaign-workflow-list"
          aria-label={`Workflows in campaign ${campaign.label}`}
        >
          {campaign.workflows.map((wf) => (
            <li key={wf.workflow_id} className="campaign-wf-item">
              <span className="mono text-sm">{wf.workflow_id.slice(0, 8)}</span>
              <span className={`wf-phase-badge wf-phase--${wf.phase ?? 'unknown'}`}>
                {wf.phase ?? '?'}
              </span>
              <a
                href={`#/workflow/${encodeURIComponent(wf.workflow_id)}`}
                className="btn btn-ghost btn-sm"
                aria-label={`Open workflow ${wf.workflow_id.slice(0, 8)}`}
              >
                Open ▸
              </a>
            </li>
          ))}
        </ul>
      ) : null}
    </article>
  );
}
