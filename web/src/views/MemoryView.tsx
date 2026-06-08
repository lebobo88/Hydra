/**
 * Hydra Cockpit — Memory view (#/memory).
 * 8-cell episodic grid (qian/kun/zhen/xun/kan/li/gen/dui), semantic search,
 * trace timeline per workflow, replay-from-checkpoint launcher.
 * 8-state handling.
 */

import { useState, useEffect } from 'react';
import { LoadingScreen, EmptyScreen, ErrorScreen, DegradedBanner, OfflineBanner } from '../components/StateScreens.tsx';
import { ConfirmDialog } from '../components/ConfirmDialog.tsx';
import type { CockpitDialogState } from '../cockpit/types.ts';
import type { EightsCell, EightsCellRecord, SearchResult } from '../api/client.ts';
import { previewNonce, replayWorkflow, CockpitWriteError } from '../api/client.ts';

const BAGUA_CELLS = [
  { key: 'qian', symbol: '☰', label: 'Qian (Heaven)' },
  { key: 'kun', symbol: '☷', label: 'Kun (Earth)' },
  { key: 'zhen', symbol: '☳', label: 'Zhen (Thunder)' },
  { key: 'xun', symbol: '☴', label: 'Xun (Wind)' },
  { key: 'kan', symbol: '☵', label: 'Kan (Water)' },
  { key: 'li', symbol: '☲', label: 'Li (Fire)' },
  { key: 'gen', symbol: '☶', label: 'Gen (Mountain)' },
  { key: 'dui', symbol: '☱', label: 'Dui (Lake)' },
] as const;

interface MemoryViewProps {
  online: boolean;
}

export function MemoryView({ online }: MemoryViewProps): JSX.Element {
  const [cells, setCells] = useState<Record<string, EightsCell>>({});
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [degraded, setDegraded] = useState(false);
  const [selectedCell, setSelectedCell] = useState<string | null>(null);
  const [cellRecords, setCellRecords] = useState<EightsCellRecord[]>([]);
  const [cellLoading, setCellLoading] = useState(false);

  // Semantic search state
  const [searchQuery, setSearchQuery] = useState('');
  const [searchResults, setSearchResults] = useState<SearchResult['results']>([]);
  const [searchLoading, setSearchLoading] = useState(false);
  const [searchError, setSearchError] = useState<string | null>(null);

  // Trace timeline (per workflow)
  const [traceWorkflowId, setTraceWorkflowId] = useState('');
  const [traceRecords, setTraceRecords] = useState<EightsCellRecord[]>([]);
  const [traceLoading, setTraceLoading] = useState(false);
  const [traceError, setTraceError] = useState<string | null>(null);

  // Replay dialog
  const [dialog, setDialog] = useState<CockpitDialogState | null>(null);
  const [busy, setBusy] = useState(false);
  const [actionError, setActionError] = useState<string | null>(null);

  async function loadCells(): Promise<void> {
    setLoading(true);
    setError(null);
    try {
      const res = await fetch('/api/memory/cells');
      if (!res.ok) throw new Error(`memory cells error: ${res.status}`);
      const body = (await res.json()) as unknown;
      // Normalize: may be EightsCell[] or { cells: EightsCell[] }
      let arr: EightsCell[] = [];
      if (Array.isArray(body)) arr = body as EightsCell[];
      else if (body && typeof body === 'object') {
        const b = body as { cells?: EightsCell[]; degraded?: boolean };
        arr = b.cells ?? [];
        if (b.degraded) setDegraded(true);
      }
      const map: Record<string, EightsCell> = {};
      for (const cell of arr) {
        if (cell.cell) map[cell.cell] = cell;
      }
      setCells(map);
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
    } finally {
      setLoading(false);
    }
  }

  async function loadCell(cellKey: string): Promise<void> {
    setSelectedCell(cellKey);
    setCellLoading(true);
    setCellRecords([]);
    try {
      const res = await fetch(`/api/memory/cells?cell=${encodeURIComponent(cellKey)}&limit=50`);
      if (!res.ok) throw new Error(`cell error: ${res.status}`);
      const body = (await res.json()) as unknown;
      let arr: EightsCellRecord[] = [];
      if (Array.isArray(body)) arr = body as EightsCellRecord[];
      else if (body && typeof body === 'object') {
        const b = body as { records?: EightsCellRecord[] };
        arr = b.records ?? [];
      }
      setCellRecords(arr);
    } catch (e) {
      setCellRecords([]);
    } finally {
      setCellLoading(false);
    }
  }

  async function handleSearch(): Promise<void> {
    if (!searchQuery.trim()) return;
    setSearchLoading(true);
    setSearchError(null);
    setSearchResults([]);
    try {
      const res = await fetch(`/api/memory/search?q=${encodeURIComponent(searchQuery)}&k=10`);
      if (!res.ok) throw new Error(`search error: ${res.status}`);
      const body = (await res.json()) as SearchResult;
      setSearchResults(body.results ?? []);
    } catch (e) {
      setSearchError(e instanceof Error ? e.message : String(e));
    } finally {
      setSearchLoading(false);
    }
  }

  async function handleTraceLoad(): Promise<void> {
    if (!traceWorkflowId.trim()) return;
    setTraceLoading(true);
    setTraceError(null);
    setTraceRecords([]);
    try {
      const res = await fetch(`/api/memory/workflow/${encodeURIComponent(traceWorkflowId.trim())}`);
      if (!res.ok) throw new Error(`trace error: ${res.status}`);
      const body = (await res.json()) as { records?: EightsCellRecord[] };
      setTraceRecords(body.records ?? []);
    } catch (e) {
      setTraceError(e instanceof Error ? e.message : String(e));
    } finally {
      setTraceLoading(false);
    }
  }

  async function openReplayDialog(workflowId: string): Promise<void> {
    setActionError(null);
    try {
      const nonceData = await previewNonce('replay');
      setDialog({
        kind: 'replay',
        title: 'Replay from checkpoint',
        verb: 'Replay',
        lines: [
          `Source workflow: ${workflowId.slice(0, 8)}`,
          'This is a High-risk write. The replay will launch as a new workflow.',
          'Live replay is additionally venom-gated (typed challenge required).',
        ],
        danger: true,
        payload: {
          workflowId,
          confirmNonce: nonceData.nonce,
        },
      });
    } catch (e) {
      setActionError(e instanceof Error ? e.message : String(e));
    }
  }

  async function handleDialogConfirm(params: {
    note?: string;
    option?: string;
    optionArg?: string;
    typedChallenge?: string;
  }): Promise<void> {
    if (!dialog) return;
    setBusy(true);
    setActionError(null);
    try {
      const workflowId = String(dialog.payload['workflowId'] ?? '');
      const nonce = String(dialog.payload['confirmNonce'] ?? '');
      const replayArgs: Parameters<typeof replayWorkflow>[0] = {
        workflow_id: workflowId,
        confirmNonce: nonce,
      };
      if (params.typedChallenge) replayArgs.typedChallenge = params.typedChallenge;
      const result = await replayWorkflow(replayArgs);
      setDialog(null);
      window.location.hash = `#/workflow/${encodeURIComponent(result.workflow_id)}`;
    } catch (e) {
      setActionError(e instanceof CockpitWriteError ? e.detail.error : String(e));
    } finally {
      setBusy(false);
    }
  }

  useEffect(() => { void loadCells(); }, []);

  if (loading) return <LoadingScreen label="Loading episodic memory…" />;
  if (error) return <ErrorScreen message={error} onRetry={() => { void loadCells(); }} />;

  const hasAnyCells = Object.keys(cells).length > 0;

  return (
    <div className="memory-view">
      {dialog ? (
        <ConfirmDialog
          state={dialog}
          onConfirm={(p) => { void handleDialogConfirm(p); }}
          onCancel={() => setDialog(null)}
          busy={busy}
        />
      ) : null}

      <header className="view-header">
        <a href="#/" className="back-link">← Launchpad</a>
        <h1 className="view-title">Memory</h1>
      </header>

      {!online ? <OfflineBanner /> : null}
      {degraded ? (
        <DegradedBanner sources={['hydra-mem']} message="Memory search is degraded — episodic cells only" />
      ) : null}
      {actionError ? (
        <div className="inline-error" role="alert" aria-live="assertive">
          <span aria-hidden="true">▲</span> {actionError}
        </div>
      ) : null}

      {/* 8-cell episodic grid */}
      <section className="memory-section" aria-labelledby="bagua-heading">
        <h2 id="bagua-heading" className="section-heading">Episodic cells</h2>
        {!hasAnyCells ? (
          <EmptyScreen message="No episodic records yet. Workflows write to these cells as they execute." />
        ) : (
          <div className="bagua-grid" role="list" aria-label="8 episodic memory cells (bagua)">
            {BAGUA_CELLS.map(({ key, symbol, label }) => {
              const cell = cells[key];
              const count = cell?.count ?? 0;
              const isSelected = selectedCell === key;
              return (
                <button
                  key={key}
                  type="button"
                  role="listitem"
                  className={`bagua-cell${isSelected ? ' bagua-cell--selected' : ''}${count > 0 ? ' bagua-cell--populated' : ''}`}
                  onClick={() => { void loadCell(key); }}
                  aria-pressed={isSelected}
                  aria-label={`${label}: ${count} record${count !== 1 ? 's' : ''}`}
                >
                  <span className="bagua-symbol" aria-hidden="true">{symbol}</span>
                  <span className="bagua-key mono">{key}</span>
                  <span className="bagua-label text-muted text-sm">{label}</span>
                  <span className="bagua-count" aria-hidden="true">{count > 0 ? count : '—'}</span>
                </button>
              );
            })}
          </div>
        )}

        {/* Cell detail records */}
        {selectedCell ? (
          <div className="cell-detail" aria-label={`Records for cell ${selectedCell}`} aria-live="polite">
            <h3 className="cell-detail-heading">
              Cell: <span className="mono">{selectedCell}</span>
              {cellLoading ? ' (loading…)' : ` — ${cellRecords.length} record${cellRecords.length !== 1 ? 's' : ''}`}
            </h3>
            {cellRecords.length === 0 && !cellLoading ? (
              <p className="text-muted text-sm">No records in this cell.</p>
            ) : null}
            <ul className="cell-records" aria-label={`Records for ${selectedCell}`}>
              {cellRecords.map((rec, i) => (
                <li key={rec.id ?? i} className="cell-record">
                  <span className="cell-record-id mono text-sm">{rec.id?.slice(0, 8) ?? '—'}</span>
                  {rec.workflow_id ? (
                    <span className="text-muted text-sm">wf: {rec.workflow_id.slice(0, 8)}</span>
                  ) : null}
                  {rec.ts ?? rec.created_at ? (
                    <span className="text-muted text-sm">
                      {new Date(rec.ts ?? rec.created_at ?? '').toLocaleString()}
                    </span>
                  ) : null}
                  {rec.workflow_id && online ? (
                    <button
                      type="button"
                      className="btn btn-ghost btn-sm"
                      onClick={() => { void openReplayDialog(rec.workflow_id!); }}
                    >
                      Replay ▸
                    </button>
                  ) : null}
                </li>
              ))}
            </ul>
          </div>
        ) : null}
      </section>

      {/* Semantic search */}
      <section className="memory-section" aria-labelledby="search-heading">
        <h2 id="search-heading" className="section-heading">Semantic search</h2>
        {degraded ? (
          <div className="inline-error text-sm" role="alert">
            Semantic search degraded — episodic cell data only.
          </div>
        ) : null}
        <form
          className="search-form"
          onSubmit={(e) => { e.preventDefault(); void handleSearch(); }}
          aria-label="Semantic memory search"
        >
          <div className="search-row">
            <input
              type="search"
              className="form-input"
              value={searchQuery}
              onChange={(e) => setSearchQuery(e.target.value)}
              placeholder="Search episodic memory…"
              aria-label="Search query"
              disabled={degraded}
            />
            <button
              type="submit"
              className="btn btn-primary btn-sm"
              disabled={!searchQuery.trim() || searchLoading || degraded}
              aria-label="Run semantic search"
            >
              {searchLoading ? 'Searching…' : 'Search'}
            </button>
          </div>
        </form>
        {searchError ? <div className="inline-error text-sm" role="alert">{searchError}</div> : null}
        {searchResults && searchResults.length > 0 ? (
          <ul className="search-results" aria-label="Search results" aria-live="polite">
            {searchResults.map((r, i) => (
              <li key={i} className="search-result">
                <span className="mono text-sm">{r.cell ?? '?'}</span>
                {r.workflow_id ? (
                  <a href={`#/workflow/${encodeURIComponent(r.workflow_id)}`} className="btn-link text-sm">
                    wf: {r.workflow_id.slice(0, 8)}
                  </a>
                ) : null}
                {r.score !== undefined ? (
                  <span className="search-score text-muted text-sm">
                    score: {r.score.toFixed(3)}
                  </span>
                ) : null}
              </li>
            ))}
          </ul>
        ) : searchResults && searchResults.length === 0 && !searchLoading && searchQuery ? (
          <p className="text-muted text-sm">No results for "{searchQuery}".</p>
        ) : null}
      </section>

      {/* Trace timeline */}
      <section className="memory-section" aria-labelledby="trace-heading">
        <h2 id="trace-heading" className="section-heading">Trace timeline</h2>
        <form
          className="search-form"
          onSubmit={(e) => { e.preventDefault(); void handleTraceLoad(); }}
          aria-label="Workflow trace timeline"
        >
          <div className="search-row">
            <input
              type="text"
              className="form-input"
              value={traceWorkflowId}
              onChange={(e) => setTraceWorkflowId(e.target.value)}
              placeholder="Workflow ID…"
              aria-label="Workflow ID for trace"
            />
            <button
              type="submit"
              className="btn btn-sm"
              disabled={!traceWorkflowId.trim() || traceLoading}
              aria-label="Load trace timeline"
            >
              {traceLoading ? 'Loading…' : 'Load trace'}
            </button>
          </div>
        </form>
        {traceError ? <div className="inline-error text-sm" role="alert">{traceError}</div> : null}
        {traceRecords.length > 0 ? (
          <div className="trace-timeline" aria-label="Trace timeline" aria-live="polite">
            {traceRecords.map((rec, i) => (
              <div key={rec.id ?? i} className="trace-timeline-entry">
                <span className="trace-ts mono text-sm">{rec.ts ?? rec.created_at ?? ''}</span>
                <span className="trace-cell-badge mono">{rec.cell ?? '?'}</span>
                {rec.workflow_id && online ? (
                  <button
                    type="button"
                    className="btn btn-ghost btn-sm"
                    onClick={() => { void openReplayDialog(rec.workflow_id!); }}
                    aria-label={`Replay from this checkpoint (workflow ${rec.workflow_id!.slice(0, 8)})`}
                  >
                    Replay ▸
                  </button>
                ) : null}
              </div>
            ))}
          </div>
        ) : !traceLoading && traceWorkflowId && traceRecords.length === 0 ? (
          <p className="text-muted text-sm">No episodic records for this workflow.</p>
        ) : null}
      </section>
    </div>
  );
}
