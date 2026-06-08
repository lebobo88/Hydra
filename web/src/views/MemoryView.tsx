/**
 * Hydra Cockpit — Memory view (#/memory).
 * THE EIGHT CELLS: 8 bagua cells (qian/kun/zhen/xun/kan/li/gen/dui) in a radial ring.
 * Dui (victory/joy) is gilded — "Remember the wins."
 * Semantic search inscribes results; replay-from-checkpoint via venom-aware confirm.
 * 8-state: loading (radial skeleton), empty (0 counts shown, not blank), error,
 *   degraded (source-unreachable notice), offline (search/replay disabled), live.
 * 2D arrow-key navigation across the ring (role=grid / gridcell).
 * Reduced-motion: no shimmer/inscription animations (CSS opt-in pattern).
 *
 * DATA CONTRACT (unchanged from prior rounds):
 *   GET /api/memory/cells                 → { cells:[{cell,count}], degraded? }
 *   GET /api/memory/cells?cell=X&limit=N  → { records:[EightsCellRecord[]] }
 *   GET /api/memory/search?q=X&k=N        → SearchResult { results:[] }
 *   GET /api/memory/workflow/:id          → { records:[EightsCellRecord[]] }
 */

import { useState, useEffect, useRef, useCallback } from 'react';
import { LoadingScreen, ErrorScreen, DegradedBanner, OfflineBanner } from '../components/StateScreens.tsx';
import { ViewHeader } from '../components/ViewHeader.tsx';
import { ConfirmDialog } from '../components/ConfirmDialog.tsx';
import type { CockpitDialogState } from '../cockpit/types.ts';
import type { EightsCell, EightsCellRecord, SearchResult } from '../api/client.ts';
import { previewNonce, replayWorkflow, CockpitWriteError } from '../api/client.ts';

// ---------------------------------------------------------------------------
// Bagua cell definitions — fixed ring order (clockwise from top, index 0 = top)
// ---------------------------------------------------------------------------

interface BaguaCell {
  key: string;
  symbol: string;
  label: string;
  isDui: boolean;
  /** Degrees clockwise from top (SVG: 0° = top of ring) */
  angleDeg: number;
}

const BAGUA_CELLS: BaguaCell[] = [
  { key: 'qian', symbol: '☰', label: 'Qian (Heaven)',   isDui: false, angleDeg:   0 },
  { key: 'zhen', symbol: '☳', label: 'Zhen (Thunder)',  isDui: false, angleDeg:  45 },
  { key: 'kan',  symbol: '☵', label: 'Kan (Water)',     isDui: false, angleDeg:  90 },
  { key: 'gen',  symbol: '☶', label: 'Gen (Mountain)',  isDui: false, angleDeg: 135 },
  { key: 'kun',  symbol: '☷', label: 'Kun (Earth)',     isDui: false, angleDeg: 180 },
  { key: 'xun',  symbol: '☴', label: 'Xun (Wind)',      isDui: false, angleDeg: 225 },
  { key: 'li',   symbol: '☲', label: 'Li (Fire)',       isDui: false, angleDeg: 270 },
  { key: 'dui',  symbol: '☱', label: 'Dui (Lake)',      isDui: true,  angleDeg: 315 },
] as const;

// 2D arrow-key navigation map: given current index → next index for each direction.
// The ring is treated as a 2×4 grid (top row = indices 0..3, bottom row = 4..7).
// Left/Right move around the ring; Up/Down cross between the two arcs.
function nextIndexForKey(current: number, key: string): number {
  const n = BAGUA_CELLS.length; // 8
  switch (key) {
    case 'ArrowRight': return (current + 1) % n;
    case 'ArrowLeft':  return (current - 1 + n) % n;
    case 'ArrowDown':  return (current + 4) % n; // jump to opposite position
    case 'ArrowUp':    return (current + 4) % n; // symmetric: opposite cell
    case 'Home':       return 0;
    case 'End':        return n - 1;
    default:           return current;
  }
}

// ---------------------------------------------------------------------------
// Radial geometry helpers
// ---------------------------------------------------------------------------

const RING_R = 110; // ring radius (SVG units)
const CENTER = 140; // SVG viewBox center

function cellPosition(angleDeg: number): { cx: number; cy: number } {
  const rad = ((angleDeg - 90) * Math.PI) / 180; // offset so 0°=top
  return {
    cx: CENTER + RING_R * Math.cos(rad),
    cy: CENTER + RING_R * Math.sin(rad),
  };
}

// ---------------------------------------------------------------------------
// RadialSkeleton — loading state
// ---------------------------------------------------------------------------

function RadialSkeleton(): JSX.Element {
  return (
    <div className="bagua-radial bagua-radial--skeleton" aria-hidden="true">
      <svg
        viewBox="0 0 280 280"
        width="280"
        height="280"
        className="bagua-ring-svg"
        aria-hidden="true"
      >
        <circle cx={CENTER} cy={CENTER} r={RING_R} className="bagua-ring-track" />
        {BAGUA_CELLS.map(({ angleDeg }, i) => {
          const { cx, cy } = cellPosition(angleDeg);
          return (
            <circle
              key={i}
              cx={cx}
              cy={cy}
              r={22}
              className="bagua-skeleton-node"
            />
          );
        })}
      </svg>
    </div>
  );
}

// ---------------------------------------------------------------------------
// Cell radial buttons — the interactive overlay on the SVG backdrop
// ---------------------------------------------------------------------------

interface CellNodeProps {
  cell: BaguaCell;
  count: number;
  isSelected: boolean;
  focused: boolean;
  onSelect: () => void;
  onKeyDown: (e: React.KeyboardEvent) => void;
  cellRef: React.Ref<HTMLButtonElement>;
}

function CellNode({ cell, count, isSelected, focused, onSelect, onKeyDown, cellRef }: CellNodeProps): JSX.Element {
  const { cx, cy } = cellPosition(cell.angleDeg);
  const isDui = cell.isDui;

  // Each cell is an absolutely-positioned button layered over the SVG
  // using CSS transform relative to the SVG centre.
  const style: React.CSSProperties = {
    position: 'absolute',
    left: `${(cx / 280) * 100}%`,
    top: `${(cy / 280) * 100}%`,
    transform: 'translate(-50%, -50%)',
  };

  const classNames = [
    'bagua-cell-node',
    isDui ? 'bagua-cell-node--dui' : '',
    isSelected ? 'bagua-cell-node--selected' : '',
    focused ? 'bagua-cell-node--focused' : '',
  ].filter(Boolean).join(' ');

  return (
    <button
      ref={cellRef}
      type="button"
      role="gridcell"
      className={classNames}
      style={style}
      onClick={onSelect}
      onKeyDown={onKeyDown}
      aria-pressed={isSelected}
      aria-label={`${cell.label}: ${count} record${count !== 1 ? 's' : ''}`}
      tabIndex={focused ? 0 : -1}
      data-cell-key={cell.key}
      data-testid={`bagua-cell-${cell.key}`}
    >
      <span
        className={`bagua-cell-symbol${isDui ? ' bagua-cell-symbol--dui' : ''}`}
        aria-hidden="true"
      >
        {cell.symbol}
      </span>
      <span className="bagua-cell-count" aria-hidden="true">
        {count}
      </span>
      {isDui ? (
        <span className="bagua-dui-shimmer" aria-hidden="true" />
      ) : null}
    </button>
  );
}

// ---------------------------------------------------------------------------
// Main MemoryView
// ---------------------------------------------------------------------------

interface MemoryViewProps {
  online: boolean;
}

export function MemoryView({ online }: MemoryViewProps): JSX.Element {
  // ---- Overview state ----
  const [cells, setCells] = useState<Record<string, EightsCell>>({});
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [degraded, setDegraded] = useState(false);

  // ---- Cell drill-down ----
  const [selectedCell, setSelectedCell] = useState<string | null>(null);
  const [cellRecords, setCellRecords] = useState<EightsCellRecord[]>([]);
  const [cellLoading, setCellLoading] = useState(false);

  // ---- 2D arrow-key grid navigation ----
  // focusedIndex is the currently roving-tabindex cell; -1 = grid not focused.
  const [focusedIndex, setFocusedIndex] = useState<number>(0);
  const cellRefs = useRef<(HTMLButtonElement | null)[]>([]);

  // ---- Semantic search ----
  const [searchQuery, setSearchQuery] = useState('');
  const [searchResults, setSearchResults] = useState<SearchResult['results']>([]);
  const [searchLoading, setSearchLoading] = useState(false);
  const [searchError, setSearchError] = useState<string | null>(null);
  // Track whether results have inscribed-in yet (for animation class toggling)
  const [searchResultsKey, setSearchResultsKey] = useState(0);

  // ---- Trace timeline ----
  const [traceWorkflowId, setTraceWorkflowId] = useState('');
  const [traceRecords, setTraceRecords] = useState<EightsCellRecord[]>([]);
  const [traceLoading, setTraceLoading] = useState(false);
  const [traceError, setTraceError] = useState<string | null>(null);

  // ---- Replay dialog ----
  const [dialog, setDialog] = useState<CockpitDialogState | null>(null);
  const [busy, setBusy] = useState(false);
  const [actionError, setActionError] = useState<string | null>(null);

  // -----------------------------------------------------------------------
  // Data loaders
  // -----------------------------------------------------------------------

  async function loadCells(): Promise<void> {
    setLoading(true);
    setError(null);
    setDegraded(false);
    try {
      const res = await fetch('/api/memory/cells');
      if (!res.ok) throw new Error(`memory cells error: ${res.status}`);
      const body = (await res.json()) as unknown;
      let arr: EightsCell[] = [];
      if (Array.isArray(body)) {
        arr = body as EightsCell[];
      } else if (body && typeof body === 'object') {
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
    } catch {
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
      setSearchResultsKey((k) => k + 1); // bump to re-trigger inscription animation
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

  // -----------------------------------------------------------------------
  // 2D arrow-key grid keyboard handler
  // -----------------------------------------------------------------------

  const handleGridKeyDown = useCallback((e: React.KeyboardEvent, currentIdx: number): void => {
    const isNavKey = ['ArrowRight', 'ArrowLeft', 'ArrowDown', 'ArrowUp', 'Home', 'End'].includes(e.key);
    if (!isNavKey && e.key !== 'Enter' && e.key !== ' ') return;

    if (e.key === 'Enter' || e.key === ' ') {
      e.preventDefault();
      const key = BAGUA_CELLS[currentIdx]!.key;
      void loadCell(key);
      return;
    }

    if (e.key === 'Escape') {
      setSelectedCell(null);
      return;
    }

    e.preventDefault();
    const nextIdx = nextIndexForKey(currentIdx, e.key);
    setFocusedIndex(nextIdx);
    cellRefs.current[nextIdx]?.focus();
  }, []);

  // -----------------------------------------------------------------------
  // Render: loading
  // -----------------------------------------------------------------------

  if (loading) {
    return (
      <div className="memory-view">
        <ViewHeader title="Memory" />
        <section className="memory-section" aria-labelledby="cells-heading">
          <h2 id="cells-heading" className="section-heading">The Eight Cells</h2>
          <RadialSkeleton />
          <LoadingScreen label="Loading episodic memory…" />
        </section>
      </div>
    );
  }

  if (error) {
    return (
      <div className="memory-view">
        <ViewHeader title="Memory" />
        <ErrorScreen message={error} onRetry={() => { void loadCells(); }} />
      </div>
    );
  }

  // -----------------------------------------------------------------------
  // Render: live / degraded / offline / empty
  // -----------------------------------------------------------------------

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

      {/* View header */}
      <ViewHeader title="Memory" />

      {/* Banners */}
      {!online ? <OfflineBanner /> : null}
      {degraded ? (
        <DegradedBanner
          sources={['hydra-mem']}
          message="Episodic memory source degraded — cells show last-known counts"
        />
      ) : null}
      {actionError ? (
        <div className="inline-error" role="alert" aria-live="assertive">
          <span aria-hidden="true">▲</span> {actionError}
        </div>
      ) : null}

      {/* ─── THE EIGHT CELLS ─────────────────────────────────────────────── */}
      <section
        className="memory-section"
        aria-labelledby="cells-heading"
        data-testid="eight-cells-section"
      >
        <h2 id="cells-heading" className="section-heading covenant">
          The Eight Cells
        </h2>

        {/* Radial backdrop image + interactive cell buttons */}
        <div
          className="bagua-radial"
          role="grid"
          aria-label="Eight episodic memory cells (bagua ring)"
          aria-rowcount={1}
          aria-colcount={8}
          data-testid="bagua-radial"
        >
          {/* cells-radial.png backdrop — the 8 trigram glyphs in a ring */}
          <img
            src="/images/chosen/cells-radial.png"
            alt=""
            aria-hidden="true"
            className="bagua-radial-img"
          />

          {/* Ring SVG — decorative orbit track + slow rotation; aria-hidden */}
          <svg
            viewBox="0 0 280 280"
            width="280"
            height="280"
            className="bagua-ring-svg"
            aria-hidden="true"
          >
            {/* Rotating outer dashed ring — slow 120s orbit */}
            <circle
              cx={CENTER}
              cy={CENTER}
              r={RING_R + 8}
              fill="none"
              stroke="var(--spirit-amber)"
              strokeWidth="0.4"
              opacity="0.12"
              strokeDasharray="3 8"
              className="bagua-ring-rotating"
              style={{ transformOrigin: `${CENTER}px ${CENTER}px` } as React.CSSProperties}
            />
            {/* Orbit track */}
            <circle
              cx={CENTER}
              cy={CENTER}
              r={RING_R}
              className="bagua-ring-track"
            />
            {/* Inner glow ring */}
            <circle
              cx={CENTER}
              cy={CENTER}
              r={RING_R - 10}
              fill="none"
              stroke="var(--spirit-amber)"
              strokeWidth="0.3"
              opacity="0.08"
              strokeDasharray="6 12"
              className="bagua-ring-rotating"
              style={{
                transformOrigin: `${CENTER}px ${CENTER}px`,
                animationDirection: 'reverse',
                animationDuration: '80s',
              } as React.CSSProperties}
            />
            {/* Spirit center glyph */}
            <circle cx={CENTER} cy={CENTER} r={18} className="bagua-ring-center" />
            {/* Ambient amber aura behind center */}
            <circle
              cx={CENTER}
              cy={CENTER}
              r={28}
              fill="none"
              stroke="var(--spirit-amber)"
              strokeWidth="0.5"
              opacity="0.15"
              className="spirit-glow-ring"
            />
            <text
              x={CENTER}
              y={CENTER + 1}
              textAnchor="middle"
              dominantBaseline="middle"
              className="bagua-center-symbol"
              aria-hidden="true"
            >
              ☯
            </text>
          </svg>

          {/* Interactive cell buttons — absolutely positioned over SVG */}
          <div
            className="bagua-cells-overlay"
            aria-label="Bagua cell ring"
          >
            {BAGUA_CELLS.map((cell, idx) => {
              const cellData = cells[cell.key];
              const count = cellData?.count ?? 0;
              return (
                <CellNode
                  key={cell.key}
                  cell={cell}
                  count={count}
                  isSelected={selectedCell === cell.key}
                  focused={focusedIndex === idx}
                  onSelect={() => {
                    setFocusedIndex(idx);
                    void loadCell(cell.key);
                  }}
                  onKeyDown={(e) => handleGridKeyDown(e, idx)}
                  cellRef={(el) => { cellRefs.current[idx] = el; }}
                />
              );
            })}
          </div>
        </div>

        {/* Accessible flat list — for SR without visual radial context */}
        <ul
          className="bagua-accessible-list sr-only-focusable"
          aria-label="Eight cells list (accessible)"
        >
          {BAGUA_CELLS.map((cell) => {
            const cellData = cells[cell.key];
            const count = cellData?.count ?? 0;
            return (
              <li key={cell.key}>
                <button
                  type="button"
                  className="bagua-accessible-btn"
                  onClick={() => { void loadCell(cell.key); }}
                  aria-label={`${cell.label}: ${count} record${count !== 1 ? 's' : ''}`}
                >
                  {cell.symbol} {cell.label} — {count} record{count !== 1 ? 's' : ''}
                </button>
              </li>
            );
          })}
        </ul>

        {/* Keyboard hint */}
        <p className="bagua-nav-hint" aria-hidden="true">
          Arrow keys navigate cells · Enter opens · Esc closes
        </p>

        {/* Cell detail panel */}
        {selectedCell ? (
          <div
            className="cell-detail"
            aria-label={`Records for cell ${selectedCell}`}
            aria-live="polite"
            data-testid="cell-detail"
          >
            <h3 className="cell-detail-heading">
              {BAGUA_CELLS.find((c) => c.key === selectedCell)?.symbol ?? ''}{' '}
              <span className="mono">{selectedCell}</span>
              {cellLoading
                ? ' (loading…)'
                : ` — ${cellRecords.length} record${cellRecords.length !== 1 ? 's' : ''}`}
            </h3>
            {cellRecords.length === 0 && !cellLoading ? (
              <p className="text-muted text-sm memory-empty-cell">
                No records in this cell yet.
              </p>
            ) : null}
            <ul className="cell-records" aria-label={`Records for ${selectedCell}`}>
              {cellRecords.map((rec, i) => (
                <li key={rec.id ?? i} className="cell-record trace-inscribe">
                  <span className="cell-record-id mono text-sm">
                    {rec.id?.slice(0, 8) ?? '—'}
                  </span>
                  {rec.workflow_id ? (
                    <span className="text-muted text-sm">
                      wf: {rec.workflow_id.slice(0, 8)}
                    </span>
                  ) : null}
                  {rec.ts ?? rec.created_at ? (
                    <span className="text-muted text-sm">
                      {new Date((rec.ts ?? rec.created_at) as string).toLocaleString()}
                    </span>
                  ) : null}
                  {rec.workflow_id && online ? (
                    <button
                      type="button"
                      className="btn btn-ghost btn-sm"
                      onClick={() => { void openReplayDialog(rec.workflow_id!); }}
                      aria-label={`Replay from checkpoint (workflow ${rec.workflow_id.slice(0, 8)})`}
                    >
                      Replay ▸
                    </button>
                  ) : null}
                  {rec.workflow_id && !online ? (
                    <span className="text-muted text-sm memory-replay-offline">
                      Replay unavailable offline
                    </span>
                  ) : null}
                </li>
              ))}
            </ul>
          </div>
        ) : null}
      </section>

      {/* ─── SEMANTIC SEARCH ─────────────────────────────────────────────── */}
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
              disabled={degraded || !online}
            />
            <button
              type="submit"
              className="btn btn-primary btn-sm"
              disabled={!searchQuery.trim() || searchLoading || degraded || !online}
              aria-label="Run semantic search"
            >
              {searchLoading ? 'Searching…' : 'Search'}
            </button>
          </div>
          {!online ? (
            <p className="text-muted text-sm memory-offline-reason" role="status">
              Search disabled — bridge offline.
            </p>
          ) : null}
        </form>
        {searchError ? (
          <div className="inline-error text-sm" role="alert">{searchError}</div>
        ) : null}
        {searchResults && searchResults.length > 0 ? (
          <ul
            className="search-results"
            aria-label="Search results"
            aria-live="polite"
            key={searchResultsKey}
          >
            {searchResults.map((r, i) => (
              <li
                key={i}
                className="search-result trace-inscribe search-result-inscribed"
                style={{
                  ['--line-index' as string]: i,
                  ['--result-index' as string]: i,
                } as React.CSSProperties}
              >
                <span className="mono text-sm">{r.cell ?? '?'}</span>
                {r.workflow_id ? (
                  <a
                    href={`#/workflow/${encodeURIComponent(r.workflow_id)}`}
                    className="btn-link text-sm"
                  >
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
          <p className="text-muted text-sm">No results for &ldquo;{searchQuery}&rdquo;.</p>
        ) : null}
      </section>

      {/* ─── TRACE TIMELINE ──────────────────────────────────────────────── */}
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
        {traceError ? (
          <div className="inline-error text-sm" role="alert">{traceError}</div>
        ) : null}
        {traceRecords.length > 0 ? (
          <div
            className="trace-timeline"
            aria-label="Trace timeline"
            aria-live="polite"
          >
            {traceRecords.map((rec, i) => (
              <div
                key={rec.id ?? i}
                className="trace-timeline-entry trace-inscribe"
                style={{ ['--line-index' as string]: i } as React.CSSProperties}
              >
                <span className="trace-ts mono text-sm">
                  {rec.ts ?? rec.created_at ?? ''}
                </span>
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
                {rec.workflow_id && !online ? (
                  <span className="text-muted text-sm memory-replay-offline">
                    Replay unavailable offline
                  </span>
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
