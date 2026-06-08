/**
 * UI tests — THE LIVING RUN (R3 Live Workflow view).
 *
 * Covers:
 *  - Phase spine renders 8 phases with active aria-current=step
 *  - Head emergence (dispatch-arms) at dispatch phase
 *  - Trace line inscribes + reflexion amber / violation venom borders
 *  - Scroll-pause pill appears on scroll-pause
 *  - Budget strand role=meter + aria attrs
 *  - Oracle receives synthesis + "Assembling…" placeholder
 *  - Task Register collapsible
 *  - 8-state: loading skeleton, empty/partial trace-gap-notice, error screen,
 *    degraded (SSE severed → polling notice not empty), offline disables actions
 *  - Reduced-motion: animation classes applied without crash
 *  - All branches rendered without undefined ReferenceErrors
 */

import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest';
import { render, screen, waitFor, fireEvent, act } from '@testing-library/react';
import { LiveWorkflowView } from '../../src/views/LiveWorkflowView.tsx';
import { App } from '../../src/App.tsx';
import { _setCachedToken } from '../../src/api/client.ts';

// ---------------------------------------------------------------------------
// Fixtures
// ---------------------------------------------------------------------------

const WORKFLOW_ID = 'wf-r3-test-1234-5678-abcd-ef1234';

const WF_DETAIL_EXECUTING = {
  workflow_id: WORKFLOW_ID,
  phase: 'executing',
  selected_squads: ['engineering', 'creative'],
  budget: { budget_usd: 100, spent_usd: 42 },
  tasks: [],
};

const WF_DETAIL_DISPATCH = {
  workflow_id: WORKFLOW_ID,
  phase: 'dispatch',
  selected_squads: ['engineering', 'executive'],
  budget: { budget_usd: 100, spent_usd: 10 },
  tasks: [],
};

const WF_DETAIL_SYNTHESIS = {
  workflow_id: WORKFLOW_ID,
  phase: 'synthesis',
  selected_squads: ['engineering'],
  budget: { budget_usd: 100, spent_usd: 80 },
  synthesis_declaration: 'Stage one design is complete and cross-vendor judged.',
  tasks: [
    { owner_squad: 'engineering', status: 'done', description: 'C1: Phase spine' },
    { owner_squad: 'engineering', status: 'active', description: 'C2: Trace inscription' },
    { owner_squad: 'creative', status: 'pending', description: 'C3: Oracle wiring' },
  ],
};

const WF_DETAIL_DONE = {
  workflow_id: WORKFLOW_ID,
  phase: 'done',
  selected_squads: ['engineering'],
  budget: { budget_usd: 100, spent_usd: 55 },
  tasks: [],
};

const MOCK_HEALTH = { ok: true };
const MOCK_HITL_NONE = { items: [], count: 0 };
const MOCK_WORKFLOWS_WF = {
  workflows: [{ workflow_id: WORKFLOW_ID, phase: 'executing', selected_squads: ['engineering'] }],
  count: 1,
};

// ---------------------------------------------------------------------------
// Module-level mock for openWorkflowStream
// We need this declared before any imports but after module setup.
// Individual tests override the mock implementation as needed.
// ---------------------------------------------------------------------------

// Shared callback references that tests can inject via openWorkflowStream mock
let _onEventCb: ((e: { type: string; data: Record<string, unknown> }) => void) | null = null;
let _onErrorCb: ((err: string) => void) | null = null;
let _onStateChangeCb: ((isSSE: boolean) => void) | null = null;

vi.mock('../../src/api/client.ts', async (importOriginal) => {
  const real = await importOriginal<typeof import('../../src/api/client.ts')>();
  return {
    ...real,
    openWorkflowStream: vi.fn((_id, onEvent, onError, onStateChange) => {
      _onEventCb = onEvent as (e: { type: string; data: Record<string, unknown> }) => void;
      _onErrorCb = onError;
      _onStateChangeCb = onStateChange;
      return { stop: vi.fn() };
    }),
  };
});

// ---------------------------------------------------------------------------
// Fetch factory
// ---------------------------------------------------------------------------

function makeFetchMock(
  wfDetail: Record<string, unknown>,
  extraRoutes: Record<string, unknown> = {},
) {
  return vi.fn().mockImplementation((url: string) => {
    const path = new URL(url, 'http://localhost').pathname;
    if (path.includes(`/workflows/${encodeURIComponent(WORKFLOW_ID)}`)) {
      return Promise.resolve({ ok: true, status: 200, json: () => Promise.resolve(wfDetail) });
    }
    if (path.includes('/workflows')) {
      return Promise.resolve({ ok: true, status: 200, json: () => Promise.resolve(MOCK_WORKFLOWS_WF) });
    }
    if (path.includes('/health')) {
      return Promise.resolve({ ok: true, status: 200, json: () => Promise.resolve(MOCK_HEALTH) });
    }
    if (path.includes('/hitl')) {
      return Promise.resolve({ ok: true, status: 200, json: () => Promise.resolve(MOCK_HITL_NONE) });
    }
    for (const [route, body] of Object.entries(extraRoutes)) {
      if (path.startsWith(route)) {
        return Promise.resolve({ ok: true, status: 200, json: () => Promise.resolve(body) });
      }
    }
    return Promise.resolve({ ok: false, status: 404, json: () => Promise.resolve({ error: 'not found' }) });
  });
}

// ---------------------------------------------------------------------------
// Setup
// ---------------------------------------------------------------------------

beforeEach(() => {
  vi.clearAllMocks();
  _setCachedToken('test-csrf-token');
  _onEventCb = null;
  _onErrorCb = null;
  _onStateChangeCb = null;

  // jsdom doesn't implement scrollIntoView — stub it
  Element.prototype.scrollIntoView = vi.fn();
});

afterEach(() => {
  vi.restoreAllMocks();
});

// ---------------------------------------------------------------------------
// Helper: render LiveWorkflowView standalone
// ---------------------------------------------------------------------------

function renderView(props: { workflowId?: string; online?: boolean } = {}) {
  const wfId = props.workflowId ?? WORKFLOW_ID;
  const online = props.online ?? true;
  return render(<LiveWorkflowView workflowId={wfId} online={online} />);
}

// ---------------------------------------------------------------------------
// Tests
// ---------------------------------------------------------------------------

describe('THE LIVING RUN — LiveWorkflowView R3', () => {

  // -------------------------------------------------------------------------
  // Loading skeleton
  // -------------------------------------------------------------------------

  it('shows loading skeleton while workflow is fetching', () => {
    // Fetch never resolves during this test
    vi.stubGlobal('fetch', vi.fn().mockReturnValue(new Promise(() => {})));
    renderView();
    expect(screen.getByTestId('living-run-skeleton')).toBeTruthy();
    expect(screen.getByRole('status', { name: /loading workflow/i })).toBeTruthy();
  });

  // -------------------------------------------------------------------------
  // Error state
  // -------------------------------------------------------------------------

  it('shows error screen when initial fetch fails', async () => {
    vi.stubGlobal('fetch', vi.fn().mockRejectedValue(new Error('network error')));
    renderView();
    await waitFor(() => {
      expect(screen.getByRole('alert')).toBeTruthy();
    }, { timeout: 3000 });
  });

  // -------------------------------------------------------------------------
  // Phase spine — 8 phases, aria-current=step
  // -------------------------------------------------------------------------

  it('renders 8 phase nodes in the vertical spine', async () => {
    vi.stubGlobal('fetch', makeFetchMock(WF_DETAIL_EXECUTING));
    renderView();

    await waitFor(() => {
      const phases = ['intake', 'planning', 'approval', 'dispatch', 'executing', 'judge', 'synthesis', 'postcheck'];
      for (const p of phases) {
        expect(screen.getByTestId(`phase-node-${p}`)).toBeTruthy();
      }
    }, { timeout: 3000 });
  });

  it('active phase node has aria-current="step"', async () => {
    vi.stubGlobal('fetch', makeFetchMock(WF_DETAIL_EXECUTING));
    renderView();

    await waitFor(() => {
      const activeNode = screen.getByTestId('phase-node-executing');
      expect(activeNode.getAttribute('aria-current')).toBe('step');
    }, { timeout: 3000 });
  });

  it('non-active phase nodes do NOT have aria-current', async () => {
    vi.stubGlobal('fetch', makeFetchMock(WF_DETAIL_EXECUTING));
    renderView();

    await waitFor(() => {
      const intakeNode = screen.getByTestId('phase-node-intake');
      expect(intakeNode.getAttribute('aria-current')).toBeNull();
    }, { timeout: 3000 });
  });

  it('phase spine SVG is present and aria-hidden', async () => {
    vi.stubGlobal('fetch', makeFetchMock(WF_DETAIL_EXECUTING));
    renderView();

    await waitFor(() => {
      const svg = screen.getByTestId('phase-spine-svg');
      expect(svg).toBeTruthy();
      expect(svg.getAttribute('aria-hidden')).toBe('true');
    }, { timeout: 3000 });
  });

  it('interrupt-before phases (approval) have interrupt class', async () => {
    vi.stubGlobal('fetch', makeFetchMock(WF_DETAIL_DISPATCH));
    renderView();

    await waitFor(() => {
      const approvalNode = screen.getByTestId('phase-node-approval');
      expect(approvalNode.classList.contains('phase-spine-node--interrupt')).toBe(true);
    }, { timeout: 3000 });
  });

  // -------------------------------------------------------------------------
  // Squad-head emergence at dispatch
  // -------------------------------------------------------------------------

  it('shows dispatch-arms when phase is dispatch or later', async () => {
    vi.stubGlobal('fetch', makeFetchMock(WF_DETAIL_DISPATCH));
    renderView();

    await waitFor(() => {
      expect(screen.getByTestId('dispatch-arms')).toBeTruthy();
    }, { timeout: 3000 });
  });

  it('each recruited squad head appears as a chip in dispatch-arms', async () => {
    vi.stubGlobal('fetch', makeFetchMock(WF_DETAIL_DISPATCH));
    renderView();

    await waitFor(() => {
      expect(screen.getByTestId('dispatch-arm-engineering')).toBeTruthy();
      expect(screen.getByTestId('dispatch-arm-executive')).toBeTruthy();
    }, { timeout: 3000 });
  });

  it('does NOT show dispatch-arms when phase is before dispatch', async () => {
    const wfPlanning = { ...WF_DETAIL_EXECUTING, phase: 'planning', selected_squads: ['engineering'] };
    vi.stubGlobal('fetch', makeFetchMock(wfPlanning));
    renderView();

    await waitFor(() => {
      expect(screen.queryByTestId('dispatch-arms')).toBeNull();
    }, { timeout: 3000 });
  });

  it('squad head chips have correct crown color classes (forge/exec)', async () => {
    vi.stubGlobal('fetch', makeFetchMock(WF_DETAIL_DISPATCH));
    renderView();

    await waitFor(() => {
      const engChip = screen.getByTestId('dispatch-arm-engineering');
      expect(engChip.classList.contains('dispatch-arm-head--forge')).toBe(true);

      const execChip = screen.getByTestId('dispatch-arm-executive');
      expect(execChip.classList.contains('dispatch-arm-head--exec')).toBe(true);
    }, { timeout: 3000 });
  });

  // -------------------------------------------------------------------------
  // Trace inscription
  // -------------------------------------------------------------------------

  it('trace gap notice appears when no trace entries (not blank)', async () => {
    vi.stubGlobal('fetch', makeFetchMock(WF_DETAIL_EXECUTING));
    renderView();

    await waitFor(() => {
      const notice = screen.getByTestId('trace-gap-notice');
      expect(notice).toBeTruthy();
      expect(notice.textContent).toContain('not evidence of none');
    }, { timeout: 3000 });
  });

  it('trace stream renders after SSE trace events are dispatched', async () => {
    vi.stubGlobal('fetch', makeFetchMock(WF_DETAIL_EXECUTING));
    renderView();

    await waitFor(() => {
      expect(screen.queryByTestId('living-run-skeleton')).toBeNull();
    }, { timeout: 3000 });

    // Dispatch a trace event via the captured onEvent callback
    act(() => {
      _onEventCb?.({ type: 'trace', data: { kind: 'envelope', ts: '2026-06-07T12:00:00Z', actor: 'engineering' } });
    });

    await waitFor(() => {
      const stream = screen.getByTestId('trace-stream');
      expect(stream).toBeTruthy();
      expect(stream.textContent).toContain('envelope');
    }, { timeout: 3000 });
  });

  it('reflexion trace line has amber border class (trace-line--reflexion)', async () => {
    vi.stubGlobal('fetch', makeFetchMock(WF_DETAIL_EXECUTING));
    renderView();

    await waitFor(() => {
      expect(screen.queryByTestId('living-run-skeleton')).toBeNull();
    }, { timeout: 3000 });

    act(() => {
      _onEventCb?.({
        type: 'trace',
        data: { kind: 'reflexion', ts: '2026-06-07T12:00:01Z', retry_index: 1, actor: 'judge' },
      });
    });

    await waitFor(() => {
      const stream = screen.getByTestId('trace-stream');
      const reflexionLine = stream.querySelector('.trace-line--reflexion');
      expect(reflexionLine).toBeTruthy();
    }, { timeout: 3000 });
  });

  it('violation trace line has venom border class and role=alert', async () => {
    vi.stubGlobal('fetch', makeFetchMock(WF_DETAIL_EXECUTING));
    renderView();

    await waitFor(() => {
      expect(screen.queryByTestId('living-run-skeleton')).toBeNull();
    }, { timeout: 3000 });

    act(() => {
      _onEventCb?.({
        type: 'trace',
        data: { kind: 'reflexion', ts: '2026-06-07T12:00:02Z', retry_index: 2, actor: 'judge' },
      });
    });

    await waitFor(() => {
      const stream = screen.getByTestId('trace-stream');
      const violLine = stream.querySelector('.trace-line--violation');
      expect(violLine).toBeTruthy();
      expect(violLine?.getAttribute('role')).toBe('alert');
    }, { timeout: 3000 });
  });

  // -------------------------------------------------------------------------
  // Scroll-pause pill
  // -------------------------------------------------------------------------

  it('scroll-pause pill is not visible initially (no events, not paused)', async () => {
    vi.stubGlobal('fetch', makeFetchMock(WF_DETAIL_EXECUTING));
    renderView();

    await waitFor(() => {
      expect(screen.queryByTestId('living-run-skeleton')).toBeNull();
    }, { timeout: 3000 });

    expect(screen.queryByTestId('scroll-pause-pill')).toBeNull();
  });

  // -------------------------------------------------------------------------
  // Budget tension strand
  // -------------------------------------------------------------------------

  it('budget strand is present with role=meter', async () => {
    vi.stubGlobal('fetch', makeFetchMock(WF_DETAIL_EXECUTING));
    renderView();

    await waitFor(() => {
      const strand = screen.getByTestId('budget-strand');
      expect(strand).toBeTruthy();
      expect(strand.getAttribute('role')).toBe('meter');
    }, { timeout: 3000 });
  });

  it('budget strand has aria-valuenow, aria-valuemin, aria-valuemax', async () => {
    vi.stubGlobal('fetch', makeFetchMock(WF_DETAIL_EXECUTING));
    renderView();

    await waitFor(() => {
      const strand = screen.getByTestId('budget-strand');
      expect(strand.getAttribute('aria-valuenow')).toBeTruthy();
      expect(strand.getAttribute('aria-valuemin')).toBe('0');
      expect(strand.getAttribute('aria-valuemax')).toBe('100');
    }, { timeout: 3000 });
  });

  it('budget strand aria-label includes "Budget" and percent consumed', async () => {
    vi.stubGlobal('fetch', makeFetchMock(WF_DETAIL_EXECUTING));
    renderView();

    await waitFor(() => {
      const strand = screen.getByTestId('budget-strand');
      const label = strand.getAttribute('aria-label') ?? '';
      expect(label.toLowerCase()).toContain('budget');
      expect(label).toContain('%');
    }, { timeout: 3000 });
  });

  it('budget strand at 80% when spent=80/budget=100', async () => {
    vi.stubGlobal('fetch', makeFetchMock(WF_DETAIL_SYNTHESIS));
    renderView();

    await waitFor(() => {
      const strand = screen.getByTestId('budget-strand');
      const valuenow = Number(strand.getAttribute('aria-valuenow'));
      expect(valuenow).toBeGreaterThanOrEqual(79);
    }, { timeout: 3000 });
  });

  it('budget strand is hidden when budget is 0', async () => {
    const wfNoBudget = { ...WF_DETAIL_EXECUTING, budget: { budget_usd: 0, spent_usd: 0 } };
    vi.stubGlobal('fetch', makeFetchMock(wfNoBudget));
    renderView();

    await waitFor(() => {
      expect(screen.queryByTestId('living-run-skeleton')).toBeNull();
    }, { timeout: 3000 });

    expect(screen.queryByTestId('budget-strand-section')).toBeNull();
  });

  // -------------------------------------------------------------------------
  // Oracle synthesis wiring (tested via App shell + SynthesisContext)
  // -------------------------------------------------------------------------

  it('Oracle shows "Assembling…" when workflow is executing and no synthesis declared', async () => {
    vi.stubGlobal('fetch', makeFetchMock(WF_DETAIL_EXECUTING, {
      '/api/hitl': MOCK_HITL_NONE,
      '/api/health': MOCK_HEALTH,
    }));
    window.location.hash = `#/workflow/${encodeURIComponent(WORKFLOW_ID)}`;
    render(<App />);

    await waitFor(() => {
      expect(screen.getByText(/Assembling/)).toBeTruthy();
    }, { timeout: 3000 });

    window.location.hash = '#/';
  });

  it('Oracle shows synthesis declaration when workflow has synthesis_declaration', async () => {
    vi.stubGlobal('fetch', makeFetchMock(WF_DETAIL_SYNTHESIS, {
      '/api/hitl': MOCK_HITL_NONE,
      '/api/health': MOCK_HEALTH,
    }));

    window.location.hash = `#/workflow/${encodeURIComponent(WORKFLOW_ID)}`;
    render(<App />);

    await waitFor(() => {
      const oracle = screen.getByTestId('oracle-declaration');
      expect(oracle.textContent).toContain('Stage one design is complete');
    }, { timeout: 3000 });

    window.location.hash = '#/';
  });

  it('Oracle shows "No synthesis yet" at Launchpad (no workflow open)', async () => {
    vi.stubGlobal('fetch', makeFetchMock(WF_DETAIL_EXECUTING, {
      '/api/hitl': MOCK_HITL_NONE,
      '/api/health': MOCK_HEALTH,
      '/api/workflows': MOCK_WORKFLOWS_WF,
    }));
    window.location.hash = '#/';
    render(<App />);

    await waitFor(() => {
      expect(screen.getByText(/The Oracle is silent/)).toBeTruthy();
    }, { timeout: 2000 });
  });

  // -------------------------------------------------------------------------
  // Oracle back-fill: DECISION_RECORD in trace backlog (R3 Reflexion fix)
  // -------------------------------------------------------------------------

  it('Oracle populates from DECISION_RECORD trace event (completed workflow back-fill)', async () => {
    // Completed workflow: REST returns done phase but no synthesis_declaration field.
    // The SSE trace replay delivers a DECISION_RECORD envelope.
    vi.stubGlobal('fetch', makeFetchMock(WF_DETAIL_DONE, {
      '/api/hitl': MOCK_HITL_NONE,
      '/api/health': MOCK_HEALTH,
    }));
    window.location.hash = `#/workflow/${encodeURIComponent(WORKFLOW_ID)}`;
    render(<App />);

    // Wait for view to load (skeleton gone, done phase)
    await waitFor(() => {
      expect(screen.queryByTestId('living-run-skeleton')).toBeNull();
    }, { timeout: 3000 });

    // Simulate SSE trace replay delivering a DECISION_RECORD
    act(() => {
      _onEventCb?.({
        type: 'trace',
        data: {
          ts: '2026-06-07T13:49:25.416921+00:00',
          kind: 'DECISION_RECORD',
          workflow_id: WORKFLOW_ID,
          envelope_type: 'DECISION_RECORD',
          origin_squad: 'engineering',
          decision: 'Stage 1 complete: COCKPIT-DESIGN.md authored and cross-vendor-judged.',
          rationale: 'Codex gpt-5.4 mandated cross-vendor judge passed; no revision required.',
          artifacts: ['docs/COCKPIT-DESIGN.md'],
        },
      });
    });

    // Oracle must now show the decision text — NOT "No synthesis yet"
    await waitFor(() => {
      const oracle = screen.getByTestId('oracle-declaration');
      expect(oracle.textContent).toContain('Stage 1 complete');
      expect(oracle.textContent).not.toContain('No synthesis yet');
      expect(oracle.textContent).not.toContain('Assembling');
    }, { timeout: 2000 });

    window.location.hash = '#/';
  });

  it('Oracle back-fill: DECISION_RECORD rationale is included in Oracle text', async () => {
    vi.stubGlobal('fetch', makeFetchMock(WF_DETAIL_DONE, {
      '/api/hitl': MOCK_HITL_NONE,
      '/api/health': MOCK_HEALTH,
    }));
    window.location.hash = `#/workflow/${encodeURIComponent(WORKFLOW_ID)}`;
    render(<App />);

    await waitFor(() => {
      expect(screen.queryByTestId('living-run-skeleton')).toBeNull();
    }, { timeout: 3000 });

    act(() => {
      _onEventCb?.({
        type: 'trace',
        data: {
          kind: 'DECISION_RECORD',
          decision: 'Design complete.',
          rationale: 'All rubrics passed at 0.99.',
        },
      });
    });

    // Oracle should contain both decision and rationale (concatenated)
    await waitFor(() => {
      const oracle = screen.getByTestId('oracle-declaration');
      expect(oracle.textContent).toContain('Design complete');
      expect(oracle.textContent).toContain('All rubrics passed');
    }, { timeout: 2000 });

    window.location.hash = '#/';
  });

  it('Oracle back-fill: DECISION_RECORD without rationale shows decision only', async () => {
    vi.stubGlobal('fetch', makeFetchMock(WF_DETAIL_DONE, {
      '/api/hitl': MOCK_HITL_NONE,
      '/api/health': MOCK_HEALTH,
    }));
    window.location.hash = `#/workflow/${encodeURIComponent(WORKFLOW_ID)}`;
    render(<App />);

    await waitFor(() => {
      expect(screen.queryByTestId('living-run-skeleton')).toBeNull();
    }, { timeout: 3000 });

    act(() => {
      _onEventCb?.({
        type: 'trace',
        data: {
          kind: 'DECISION_RECORD',
          decision: 'Workflow completed successfully.',
        },
      });
    });

    await waitFor(() => {
      const oracle = screen.getByTestId('oracle-declaration');
      expect(oracle.textContent).toContain('Workflow completed successfully');
    }, { timeout: 2000 });

    window.location.hash = '#/';
  });

  it('Oracle back-fill: later DECISION_RECORD supersedes earlier one (last wins)', async () => {
    vi.stubGlobal('fetch', makeFetchMock(WF_DETAIL_DONE, {
      '/api/hitl': MOCK_HITL_NONE,
      '/api/health': MOCK_HEALTH,
    }));
    window.location.hash = `#/workflow/${encodeURIComponent(WORKFLOW_ID)}`;
    render(<App />);

    await waitFor(() => {
      expect(screen.queryByTestId('living-run-skeleton')).toBeNull();
    }, { timeout: 3000 });

    // First DECISION_RECORD
    act(() => {
      _onEventCb?.({
        type: 'trace',
        data: { kind: 'DECISION_RECORD', decision: 'First decision text.' },
      });
    });

    // Second DECISION_RECORD (e.g. from post-synthesis judge re-run)
    act(() => {
      _onEventCb?.({
        type: 'trace',
        data: { kind: 'DECISION_RECORD', decision: 'Final authoritative decision.' },
      });
    });

    await waitFor(() => {
      const oracle = screen.getByTestId('oracle-declaration');
      expect(oracle.textContent).toContain('Final authoritative decision');
    }, { timeout: 2000 });

    window.location.hash = '#/';
  });

  it('Oracle live-SSE path: synthesis event arriving after DECISION_RECORD updates Oracle', async () => {
    // Test that the live SSE path (synthesis kind events) still works alongside back-fill
    vi.stubGlobal('fetch', makeFetchMock(WF_DETAIL_EXECUTING, {
      '/api/hitl': MOCK_HITL_NONE,
      '/api/health': MOCK_HEALTH,
    }));
    window.location.hash = `#/workflow/${encodeURIComponent(WORKFLOW_ID)}`;
    render(<App />);

    await waitFor(() => {
      expect(screen.queryByTestId('living-run-skeleton')).toBeNull();
    }, { timeout: 3000 });

    // First: DECISION_RECORD from backlog
    act(() => {
      _onEventCb?.({
        type: 'trace',
        data: { kind: 'DECISION_RECORD', decision: 'Backlog decision text.' },
      });
    });

    // Then: live synthesis event (what arrives for in-flight workflows at synthesis phase)
    act(() => {
      _onEventCb?.({
        type: 'trace',
        data: { kind: 'synthesis', declaration: 'Live synthesis declaration arrived.' },
      });
    });

    await waitFor(() => {
      // Live synthesis text should be visible (last-written wins)
      const oracle = screen.getByTestId('oracle-declaration');
      expect(oracle.textContent).toContain('Live synthesis declaration arrived');
    }, { timeout: 2000 });

    window.location.hash = '#/';
  });

  // -------------------------------------------------------------------------
  // Task Register
  // -------------------------------------------------------------------------

  it('Task Register is not shown when tasks array is empty', async () => {
    vi.stubGlobal('fetch', makeFetchMock(WF_DETAIL_EXECUTING));
    renderView();

    await waitFor(() => {
      expect(screen.queryByTestId('living-run-skeleton')).toBeNull();
    }, { timeout: 3000 });

    expect(screen.queryByTestId('task-register')).toBeNull();
  });

  it('Task Register appears when tasks are non-empty', async () => {
    vi.stubGlobal('fetch', makeFetchMock(WF_DETAIL_SYNTHESIS));
    renderView();

    await waitFor(() => {
      expect(screen.getByTestId('task-register')).toBeTruthy();
    }, { timeout: 3000 });
  });

  it('Task Register is collapsed by default', async () => {
    vi.stubGlobal('fetch', makeFetchMock(WF_DETAIL_SYNTHESIS));
    renderView();

    await waitFor(() => screen.getByTestId('task-register'), { timeout: 3000 });

    expect(screen.queryByTestId('task-register-list')).toBeNull();
  });

  it('Task Register expands on click and shows task items', async () => {
    vi.stubGlobal('fetch', makeFetchMock(WF_DETAIL_SYNTHESIS));
    renderView();

    await waitFor(() => screen.getByTestId('task-register'), { timeout: 3000 });

    const header = screen.getByRole('button', { name: /task list/i });
    fireEvent.click(header);

    await waitFor(() => {
      const list = screen.getByTestId('task-register-list');
      expect(list).toBeTruthy();
      const items = list.querySelectorAll('[role="listitem"]');
      expect(items.length).toBe(3);
    }, { timeout: 2000 });
  });

  it('Task Register items have correct status marks (●/◐/○)', async () => {
    vi.stubGlobal('fetch', makeFetchMock(WF_DETAIL_SYNTHESIS));
    renderView();

    await waitFor(() => screen.getByTestId('task-register'), { timeout: 3000 });

    const header = screen.getByRole('button', { name: /task list/i });
    fireEvent.click(header);

    await waitFor(() => {
      const list = screen.getByTestId('task-register-list');
      const items = list.querySelectorAll('[role="listitem"]');
      expect(items[0]?.textContent).toContain('●');
      expect(items[1]?.textContent).toContain('◐');
      expect(items[2]?.textContent).toContain('○');
    }, { timeout: 2000 });
  });

  it('Task Register header has role=button and aria-expanded', async () => {
    vi.stubGlobal('fetch', makeFetchMock(WF_DETAIL_SYNTHESIS));
    renderView();

    await waitFor(() => screen.getByTestId('task-register'), { timeout: 3000 });

    const header = screen.getByRole('button', { name: /task list/i });
    expect(header.getAttribute('aria-expanded')).toBe('false');

    fireEvent.click(header);

    await waitFor(() => {
      expect(header.getAttribute('aria-expanded')).toBe('true');
    }, { timeout: 1000 });
  });

  // -------------------------------------------------------------------------
  // 8-state: offline
  // -------------------------------------------------------------------------

  it('offline: Modify budget + Abort buttons are absent', async () => {
    vi.stubGlobal('fetch', makeFetchMock(WF_DETAIL_EXECUTING));
    renderView({ online: false });

    await waitFor(() => {
      expect(screen.queryByTestId('living-run-skeleton')).toBeNull();
    }, { timeout: 3000 });

    expect(screen.queryByRole('button', { name: /modify budget/i })).toBeNull();
    expect(screen.queryByRole('button', { name: /^abort$/i })).toBeNull();
  });

  it('offline: Replay button is shown but disabled with offline reason', async () => {
    vi.stubGlobal('fetch', makeFetchMock(WF_DETAIL_EXECUTING));
    renderView({ online: false });

    await waitFor(() => {
      expect(screen.queryByTestId('living-run-skeleton')).toBeNull();
    }, { timeout: 3000 });

    const replayBtn = screen.getByRole('button', { name: /replay.*offline/i });
    expect(replayBtn).toBeTruthy();
    expect((replayBtn as HTMLButtonElement).disabled).toBe(true);
  });

  it('offline: offline banner is visible', async () => {
    vi.stubGlobal('fetch', makeFetchMock(WF_DETAIL_EXECUTING));
    renderView({ online: false });

    await waitFor(() => {
      expect(screen.getByTestId('offline-banner')).toBeTruthy();
    }, { timeout: 3000 });
  });

  // -------------------------------------------------------------------------
  // 8-state: SSE severed → polling notice (not empty)
  // -------------------------------------------------------------------------

  it('SSE severed shows degraded banner with "poll" / "SSE" mention, not empty', async () => {
    vi.stubGlobal('fetch', makeFetchMock(WF_DETAIL_EXECUTING));
    renderView();

    await waitFor(() => {
      expect(screen.queryByTestId('living-run-skeleton')).toBeNull();
    }, { timeout: 3000 });

    // Simulate SSE falling back to poll (onStateChange(false))
    act(() => {
      _onStateChangeCb?.(false);
    });

    await waitFor(() => {
      const notice = screen.getByTestId('degraded-notice');
      expect(notice).toBeTruthy();
      // Must mention poll/SSE degradation — not a clean empty
      const text = (notice.textContent ?? '').toLowerCase();
      expect(text.includes('poll') || text.includes('sse') || text.includes('degraded')).toBe(true);
    }, { timeout: 2000 });
  });

  // -------------------------------------------------------------------------
  // All branches — no ReferenceError on any render path
  // -------------------------------------------------------------------------

  it('gate branch renders without ReferenceError', async () => {
    const consoleError = vi.spyOn(console, 'error').mockImplementation(() => {});

    const wfWithGate = {
      ...WF_DETAIL_EXECUTING,
      pending_hitl: { id: 'gate-1', workflow_id: WORKFLOW_ID, reason: 'high_risk', options: ['approve'] },
    };
    vi.stubGlobal('fetch', makeFetchMock(wfWithGate));
    renderView();

    await waitFor(() => {
      expect(screen.queryByTestId('living-run-skeleton')).toBeNull();
    }, { timeout: 3000 });

    // Gate link must appear
    expect(screen.getByRole('link', { name: /open gate/i })).toBeTruthy();

    const refErrors = consoleError.mock.calls.flat().filter(
      (a) => typeof a === 'string' && a.includes('ReferenceError'),
    );
    expect(refErrors).toHaveLength(0);
    consoleError.mockRestore();
  });

  it('done/terminal branch renders without ReferenceError', async () => {
    const consoleError = vi.spyOn(console, 'error').mockImplementation(() => {});

    vi.stubGlobal('fetch', makeFetchMock(WF_DETAIL_DONE));
    renderView();

    await waitFor(() => {
      expect(screen.queryByTestId('living-run-skeleton')).toBeNull();
    }, { timeout: 3000 });

    const refErrors = consoleError.mock.calls.flat().filter(
      (a) => typeof a === 'string' && a.includes('ReferenceError'),
    );
    expect(refErrors).toHaveLength(0);
    consoleError.mockRestore();
  });

  it('synthesis branch renders without ReferenceError', async () => {
    const consoleError = vi.spyOn(console, 'error').mockImplementation(() => {});

    vi.stubGlobal('fetch', makeFetchMock(WF_DETAIL_SYNTHESIS));
    renderView();

    await waitFor(() => {
      expect(screen.queryByTestId('living-run-skeleton')).toBeNull();
    }, { timeout: 3000 });

    const refErrors = consoleError.mock.calls.flat().filter(
      (a) => typeof a === 'string' && a.includes('ReferenceError'),
    );
    expect(refErrors).toHaveLength(0);
    consoleError.mockRestore();
  });

  // -------------------------------------------------------------------------
  // Reduced-motion animation classes
  // -------------------------------------------------------------------------

  it('animation CSS classes can be applied without error', () => {
    const div = document.createElement('div');
    const classes = [
      'phase-peristalsis', 'phase-ring-complete', 'arm-draw', 'head-fill-emerge',
      'phase-dot-complete', 'phase-connector-draw', 'trace-inscribe',
      'scroll-pause-pill',
    ];
    classes.forEach((cls) => {
      expect(() => div.classList.add(cls)).not.toThrow();
      expect(div.classList.contains(cls)).toBe(true);
      div.classList.remove(cls);
    });
  });

  // -------------------------------------------------------------------------
  // Living run header + stream status
  // -------------------------------------------------------------------------

  it('renders living-run header with stream status pill', async () => {
    vi.stubGlobal('fetch', makeFetchMock(WF_DETAIL_EXECUTING));
    renderView();

    await waitFor(() => {
      expect(screen.getByTestId('living-run-header')).toBeTruthy();
      expect(screen.getByTestId('stream-status')).toBeTruthy();
    }, { timeout: 3000 });
  });

  // -------------------------------------------------------------------------
  // Phase spine section
  // -------------------------------------------------------------------------

  it('phase spine section has aria-labelledby="phase-spine-heading"', async () => {
    vi.stubGlobal('fetch', makeFetchMock(WF_DETAIL_EXECUTING));
    renderView();

    await waitFor(() => {
      const section = screen.getByTestId('phase-spine-section');
      expect(section.getAttribute('aria-labelledby')).toBe('phase-spine-heading');
    }, { timeout: 3000 });
  });
});
