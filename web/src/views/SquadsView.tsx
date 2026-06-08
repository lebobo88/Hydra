/**
 * Hydra Cockpit — Squads view (#/squads).
 * 13-pack grid from /api/squads. use-as-hint → prefilled #/launch.
 * 8-state handling.
 */

import { useEffect, useState } from 'react';
import { LoadingScreen, EmptyScreen, ErrorScreen, DegradedBanner, OfflineBanner } from '../components/StateScreens.tsx';
import { ViewHeader } from '../components/ViewHeader.tsx';
import type { SquadPack } from '../api/client.ts';

interface SquadsViewProps {
  online: boolean;
}

export function SquadsView({ online }: SquadsViewProps): JSX.Element {
  const [squads, setSquads] = useState<SquadPack[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [degraded, setDegraded] = useState(false);

  async function load(): Promise<void> {
    setLoading(true);
    setError(null);
    try {
      const res = await fetch('/api/squads');
      if (!res.ok) throw new Error(`squads error: ${res.status}`);
      const body = (await res.json()) as unknown;
      let packs: SquadPack[] = [];
      if (Array.isArray(body)) packs = body as SquadPack[];
      else if (body && typeof body === 'object') {
        const b = body as { squads?: SquadPack[]; degraded?: boolean };
        packs = b.squads ?? [];
        if (b.degraded) setDegraded(true);
      }
      setSquads(packs);
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
    } finally {
      setLoading(false);
    }
  }

  useEffect(() => { void load(); }, []);

  if (loading) return <LoadingScreen label="Loading squads…" />;
  if (error) return <ErrorScreen message={error} onRetry={() => { void load(); }} />;

  return (
    <div className="squads-view">
      <ViewHeader title="Squads" />

      {!online ? <OfflineBanner /> : null}
      {degraded ? (
        <DegradedBanner sources={['squad-registry']} message="Squad registry may be stale" />
      ) : null}

      {squads.length === 0 ? (
        <EmptyScreen
          message={degraded
            ? 'Squad list unavailable — source unreachable. An empty list is not evidence there are none.'
            : 'No squads discovered. Register squad packs via the Hydra CLI.'
          }
        />
      ) : (
        <div
          className="squads-grid"
          role="list"
          aria-label={`${squads.length} squad pack${squads.length !== 1 ? 's' : ''}`}
        >
          {squads.map((sq) => (
            <SquadCard key={sq.slug} squad={sq} online={online} />
          ))}
        </div>
      )}
    </div>
  );
}

function SquadCard({ squad, online }: { squad: SquadPack; online: boolean }): JSX.Element {
  const launchHref = `#/launch?squads=${encodeURIComponent(squad.slug)}`;
  return (
    <article className="squad-card" role="listitem" aria-label={`Squad pack: ${squad.slug}`}>
      <header className="squad-card-header">
        <span className="squad-slug mono">{squad.slug}</span>
        {squad.name ? <span className="squad-name">{squad.name}</span> : null}
        {squad.version ? <span className="squad-version text-muted text-sm">v{squad.version}</span> : null}
      </header>

      {squad.description ? (
        <p className="squad-description text-sm">{squad.description}</p>
      ) : null}

      {squad.industries && squad.industries.length > 0 ? (
        <div className="squad-meta-row">
          <span className="squad-meta-label text-muted text-sm">Industries:</span>
          <div className="squad-tag-list">
            {squad.industries.map((ind) => (
              <span key={ind} className="squad-tag">{ind}</span>
            ))}
          </div>
        </div>
      ) : null}

      {squad.accepts && squad.accepts.length > 0 ? (
        <div className="squad-meta-row">
          <span className="squad-meta-label text-muted text-sm">Accepts:</span>
          <span className="text-sm mono">{squad.accepts.join(', ')}</span>
        </div>
      ) : null}

      {squad.emits && squad.emits.length > 0 ? (
        <div className="squad-meta-row">
          <span className="squad-meta-label text-muted text-sm">Emits:</span>
          <span className="text-sm mono">{squad.emits.join(', ')}</span>
        </div>
      ) : null}

      {squad.agents && squad.agents.length > 0 ? (
        <div className="squad-agents">
          <span className="squad-meta-label text-muted text-sm">Agents ({squad.agents.length}):</span>
          <ul className="squad-agent-list" aria-label={`Agents in ${squad.slug}`}>
            {squad.agents.map((agent) => (
              <li key={agent.slug} className="squad-agent-item text-sm">
                <span className="mono">{agent.slug}</span>
                {agent.role ? <span className="text-muted"> — {agent.role}</span> : null}
                {agent.model_tier ? <span className="squad-tier-badge text-sm">{agent.model_tier}</span> : null}
              </li>
            ))}
          </ul>
        </div>
      ) : null}

      {squad.best_of_n && squad.best_of_n > 1 ? (
        <div className="squad-meta-row">
          <span className="squad-meta-label text-muted text-sm">Best-of-N:</span>
          <span className="text-sm">×{squad.best_of_n}</span>
        </div>
      ) : null}

      <footer className="squad-card-footer">
        {online ? (
          <a
            href={launchHref}
            className="btn btn-sm btn-primary"
            aria-label={`Use ${squad.slug} as a squad hint for a new run`}
          >
            Use as hint → launch
          </a>
        ) : (
          <span className="text-muted text-sm">Offline — launch disabled</span>
        )}
      </footer>
    </article>
  );
}
