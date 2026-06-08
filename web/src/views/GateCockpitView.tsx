/**
 * Hydra Cockpit — Gate Cockpit (#/gate/:hitl_id) — THE CEREMONY (R4).
 *
 * Renders the HITLRequest VERBATIM (no paraphrase — per design spec §1.2.4).
 * Expiry countdown disables all actions on expiry.
 * All 5 resume actions via ConfirmDialog.
 * default_option highlighted but NEVER preselected (silence ≠ consent).
 * Required resolution note on every resume.
 * Typed workflow-id challenge on high-risk gates and unconditionally on force-dispatch.
 *
 * R4 additions — CEREMONY LAYER ONLY (security/bridge contract unchanged):
 *  - Routine vs venom-class branching (visual only, same security logic)
 *  - Seal-break ceremony for venom-class: crack(600ms) → split(400ms) → reveal(300ms)
 *  - venom-seal.png centerpiece; SVG crack line overlay; half-split + reveal
 *  - Reduced-motion: seal fades directly to revealed form (no crack animation)
 *  - Rubricated venom warning in --venom / Cinzel
 *  - aria-describedby wires venom text to force-dispatch radio
 *  - role=alertdialog on gate panel (blocking, assertive)
 *  - "Approving inscribes your name" ghost text
 *  - Focus trap + Esc cancel (in existing ConfirmDialog)
 *  - 8-state: loading seal materializing, already-resolved, offline, error/degraded
 */

import { useCallback, useEffect, useRef, useState } from 'react';
import { ConfirmDialog } from '../components/ConfirmDialog.tsx';
import { LoadingScreen, ErrorScreen, OfflineBanner, DegradedBanner } from '../components/StateScreens.tsx';
import type { CockpitDialogState, HitlGate } from '../cockpit/types.ts';
import { resumeGate, previewNonce, CockpitWriteError, fetchHitl } from '../api/client.ts';

// ---------------------------------------------------------------------------
// Constants
// ---------------------------------------------------------------------------

const HIGH_RISK_REASONS = new Set(['prod_deploy', 'constitution_breach', 'policy_breach', 'high_risk']);

/** Actions that trigger the seal-break ceremony. */
const VENOM_CLASS_ACTIONS = new Set(['force-dispatch', 'replay-live']);

/** Reasons that trigger the seal-break ceremony regardless of action. */
const VENOM_CLASS_REASONS = new Set([
  'constitution_breach',
  'force_dispatch',
  'force-dispatch',
  'live_replay',
  'modify_budget_over_cap',
]);

const RESUME_ACTIONS = [
  { action: 'approve', label: 'Approve', risk: 'med' as const },
  { action: 'reject', label: 'Reject', risk: 'low' as const },
  { action: 'modify-budget', label: 'Modify budget', risk: 'high' as const },
  { action: 'change-squads', label: 'Change squads', risk: 'med' as const },
  { action: 'force-dispatch', label: 'Force dispatch', risk: 'venom' as const },
] as const;

// ---------------------------------------------------------------------------
// Seal-break ceremony state machine
// ---------------------------------------------------------------------------

type SealStage =
  | 'idle'        // seal visible, no crack
  | 'cracking'    // gold crack draws (600ms standard / 300ms venom)
  | 'splitting'   // halves separate ±12px / ±24px with rotation (400ms)
  | 'revealed';   // gate form revealed (300ms clip-path)

// ---------------------------------------------------------------------------
// Helpers
// ---------------------------------------------------------------------------

function isVenomClass(gate: HitlGate | null, action?: string): boolean {
  if (!gate) return false;
  if (action && VENOM_CLASS_ACTIONS.has(action)) return true;
  if (gate.reason && VENOM_CLASS_REASONS.has(gate.reason)) return true;
  if (gate.reason && HIGH_RISK_REASONS.has(gate.reason)) return true;
  return false;
}

function isVenomAction(action: string): boolean {
  return VENOM_CLASS_ACTIONS.has(action) || action === 'force-dispatch';
}

// ---------------------------------------------------------------------------
// Expiry countdown
// ---------------------------------------------------------------------------

function useExpiryCountdown(expiresAt: string | null | undefined): { expired: boolean; remaining: string } {
  const [now, setNow] = useState(Date.now());
  useEffect(() => {
    if (!expiresAt) return;
    const interval = setInterval(() => setNow(Date.now()), 1000);
    return () => clearInterval(interval);
  }, [expiresAt]);

  if (!expiresAt) return { expired: false, remaining: '' };
  const expiry = new Date(expiresAt).getTime();
  if (isNaN(expiry)) return { expired: false, remaining: expiresAt };
  const diffMs = expiry - now;
  if (diffMs <= 0) return { expired: true, remaining: 'Expired' };
  const h = Math.floor(diffMs / 3600000);
  const m = Math.floor((diffMs % 3600000) / 60000);
  const s = Math.floor((diffMs % 60000) / 1000);
  const remaining = [h > 0 ? `${h}h` : '', `${m.toString().padStart(2, '0')}m`, `${s.toString().padStart(2, '0')}s`]
    .filter(Boolean)
    .join(' ');
  return { expired: false, remaining };
}

// ---------------------------------------------------------------------------
// Seal-break ceremony component
// ---------------------------------------------------------------------------

interface SealCeremonyProps {
  venomClass: boolean;
  stage: SealStage;
  /** aria-hidden when revealed */
}

function SealCeremony({ venomClass, stage }: SealCeremonyProps): JSX.Element {
  const crackMs = venomClass ? 300 : 600;

  // Derive CSS classes per stage
  const sealHalvesClass = stage === 'splitting' || stage === 'revealed'
    ? `seal-halves seal-halves--split${venomClass ? ' seal-halves--venom' : ''}`
    : `seal-halves${venomClass ? ' seal-halves--venom' : ''}`;

  const crackClass = stage === 'cracking' || stage === 'splitting' || stage === 'revealed'
    ? `seal-crack seal-crack--drawing${venomClass ? ' seal-crack--venom' : ''}`
    : `seal-crack${venomClass ? ' seal-crack--venom' : ''}`;

  return (
    <div
      className="seal-ceremony"
      aria-hidden={stage === 'revealed' ? 'true' : undefined}
      data-stage={stage}
      data-venom={venomClass ? 'true' : 'false'}
      data-testid="seal-ceremony"
    >
      {/* Seal halves — split on ceremony trigger */}
      <div className={sealHalvesClass} data-testid="seal-halves">
        {/* Left half */}
        <div className="seal-half seal-half--left" data-testid="seal-half-left">
          <img
            src="/images/chosen/venom-seal.png"
            alt=""
            aria-hidden="true"
            className="seal-img seal-img--left"
            draggable={false}
          />
        </div>
        {/* Right half */}
        <div className="seal-half seal-half--right" data-testid="seal-half-right">
          <img
            src="/images/chosen/venom-seal.png"
            alt=""
            aria-hidden="true"
            className="seal-img seal-img--right"
            draggable={false}
          />
        </div>
        {/* SVG gold crack line overlay — draws via stroke-dashoffset */}
        <svg
          className={crackClass}
          viewBox="0 0 240 240"
          aria-hidden="true"
          data-testid="seal-crack-svg"
          style={
            stage === 'cracking'
              ? ({ '--crack-duration': `${crackMs}ms` } as React.CSSProperties)
              : undefined
          }
        >
          {/* Jagged crack down the center */}
          <path
            d="M120,20 L114,60 L124,100 L112,140 L122,180 L116,220"
            className="seal-crack-path"
            strokeDasharray="220"
            strokeDashoffset={stage === 'idle' ? '220' : '0'}
            stroke="var(--spirit-amber)"
            strokeWidth="2"
            fill="none"
            strokeLinecap="round"
            strokeLinejoin="round"
          />
        </svg>
      </div>
    </div>
  );
}

// ---------------------------------------------------------------------------
// Venom warning paragraph (rubricated in vermillion)
// ---------------------------------------------------------------------------

interface VenomWarningProps {
  reason: string | undefined;
  id: string;
}

function VenomWarning({ reason, id }: VenomWarningProps): JSX.Element {
  return (
    <p
      id={id}
      className="venom-named"
      role="note"
      data-testid="venom-warning"
    >
      <span className="venom-named-label covenant" aria-label="Venom named">VENOM NAMED:</span>{' '}
      <span className="venom-named-reason">{reason ?? 'high-risk gate'}</span>
    </p>
  );
}

// ---------------------------------------------------------------------------
// Main Gate Cockpit View
// ---------------------------------------------------------------------------

interface GateCockpitViewProps {
  /** workflowId — used to look up the pending gate */
  workflowId: string;
  online: boolean;
}

export function GateCockpitView({ workflowId, online }: GateCockpitViewProps): JSX.Element {
  const [gate, setGate] = useState<HitlGate | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [resolved, setResolved] = useState(false);
  const [dialog, setDialog] = useState<CockpitDialogState | null>(null);
  const [busy, setBusy] = useState(false);
  const [actionError, setActionError] = useState<string | null>(null);
  const [degraded, setDegraded] = useState(false);

  // Ceremony state
  const [sealStage, setSealStage] = useState<SealStage>('idle');
  const [pendingAction, setPendingAction] = useState<string | null>(null);
  const [ceremonyActive, setCeremonyActive] = useState(false);

  // Reduced-motion preference
  const prefersReducedMotion =
    typeof window !== 'undefined' &&
    window.matchMedia != null &&
    window.matchMedia('(prefers-reduced-motion: reduce)').matches;

  const gatePanelRef = useRef<HTMLDivElement>(null);
  const venomWarningId = 'venom-warning-desc';

  // -------------------------------------------------------------------------
  // Data loading (unchanged from judged C6)
  // -------------------------------------------------------------------------

  const loadGate = useCallback(async (): Promise<void> => {
    setLoading(true);
    setError(null);
    try {
      // First try direct workflow status for pending_hitl
      const wfRes = await fetch(`/api/workflows/${encodeURIComponent(workflowId)}`);
      if (!wfRes.ok) throw new Error(`workflow fetch failed: ${wfRes.status}`);
      const wfData = (await wfRes.json()) as Record<string, unknown>;
      const pendingGate = wfData['pending_hitl'] as HitlGate | null;
      if (pendingGate) {
        setGate(pendingGate);
        setLoading(false);
        return;
      }
      // Fallback: query /api/hitl list for this workflow
      const hitlBody = (await fetchHitl()) as unknown;
      let items: HitlGate[] = [];
      if (Array.isArray(hitlBody)) items = hitlBody as HitlGate[];
      else if (hitlBody && typeof hitlBody === 'object' && 'items' in hitlBody) {
        items = ((hitlBody as { items?: HitlGate[] }).items ?? []);
      }
      const found = items.find((i) => i.workflow_id === workflowId);
      if (found) {
        setGate(found);
      } else {
        setDegraded(true);
      }
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
    } finally {
      setLoading(false);
    }
  }, [workflowId]);

  useEffect(() => {
    void loadGate();
    const interval = setInterval(() => { void loadGate(); }, 5000);
    return () => clearInterval(interval);
  }, [loadGate]);

  const { expired, remaining } = useExpiryCountdown(gate?.expires_at);

  // -------------------------------------------------------------------------
  // Ceremony orchestration
  // -------------------------------------------------------------------------

  /**
   * Run the seal-break ceremony, then open the resume dialog.
   * If reduced-motion: skip straight to revealed (no animation).
   */
  async function runCeremony(action: string): Promise<void> {
    setCeremonyActive(true);
    setSealStage('idle');
    setPendingAction(action);

    const venomFast = isVenomAction(action);
    const crackMs = venomFast ? 300 : 600;
    const halvesMs = 400;
    const revealMs = 300;

    if (prefersReducedMotion) {
      // Fade directly — seal appears already split, form immediately visible
      setSealStage('revealed');
      await openResumeDialog(action);
      return;
    }

    // Phase 1: crack
    setSealStage('cracking');
    await new Promise<void>((r) => setTimeout(r, crackMs));

    // Phase 2: split halves
    setSealStage('splitting');
    await new Promise<void>((r) => setTimeout(r, halvesMs));

    // Phase 3: reveal form
    setSealStage('revealed');
    await new Promise<void>((r) => setTimeout(r, revealMs));

    await openResumeDialog(action);
  }

  // -------------------------------------------------------------------------
  // Resume dialog logic (UNCHANGED from judged C6 — security contract verbatim)
  // -------------------------------------------------------------------------

  async function openResumeDialog(action: string): Promise<void> {
    setActionError(null);
    if (!gate) return;

    const isHighRisk = HIGH_RISK_REASONS.has(gate.reason ?? '') || action === 'force-dispatch' || action === 'modify-budget';
    const isForceDispatch = action === 'force-dispatch';

    try {
      // Always request a nonce for approve, modify-budget, change-squads, force-dispatch
      // reject is Low risk (no nonce)
      let nonce: string | undefined;
      if (action !== 'reject') {
        const nonceData = await previewNonce(action);
        nonce = nonceData.nonce;
      }

      const actionMeta = RESUME_ACTIONS.find((a) => a.action === action);
      const riskLabel = actionMeta?.risk === 'venom' ? ' ⛔ venom-class · Cerberus-gated' :
        actionMeta?.risk === 'high' ? ' ⚠ High-risk' : '';

      setDialog({
        kind: 'gate-resume',
        title: `${actionMeta?.label ?? action}${riskLabel}`,
        verb: `${actionMeta?.label ?? action}`,
        lines: [
          `Workflow: ${workflowId.slice(0, 8)}`,
          gate.summary ? `Summary: ${gate.summary}` : '',
          action === 'force-dispatch'
            ? 'Force-dispatch is venom-class and Cerberus-gated server-side. You own this risk.'
            : action === 'modify-budget'
              ? 'Enter a new budget cap. High-risk write.'
              : action === 'change-squads'
                ? 'Enter replacement squad slugs.'
                : '',
          'A resolution note is required and will be recorded.',
        ].filter(Boolean),
        danger: isHighRisk || action === 'reject',
        withNote: true,
        ...((action === 'modify-budget') ? { options: ['modify-budget'], defaultOption: null } : {}),
        ...((action === 'change-squads') ? { options: ['change-squads'], defaultOption: null } : {}),
        ...((isHighRisk || isForceDispatch) ? { typedChallenge: workflowId, typedLabel: 'Type the workflow ID to confirm' } : {}),
        payload: {
          action,
          ...(nonce ? { confirmNonce: nonce } : {}),
        },
      });
    } catch (e) {
      setActionError(e instanceof Error ? e.message : String(e));
      // Reset ceremony on error
      setCeremonyActive(false);
      setSealStage('idle');
      setPendingAction(null);
    }
  }

  async function handleDialogConfirm(params: {
    note?: string;
    option?: string;
    optionArg?: string;
    typedChallenge?: string;
  }): Promise<void> {
    if (!dialog || !gate) return;
    setBusy(true);
    setActionError(null);
    try {
      const action = String(dialog.payload['action'] ?? '');
      const nonce = dialog.payload['confirmNonce'] as string | undefined;
      const gateArgs: Parameters<typeof resumeGate>[0] = {
        workflow_id: workflowId,
        action,
      };
      const optVal = params.optionArg ?? params.option;
      if (optVal) gateArgs.option = optVal;
      if (nonce) gateArgs.confirmNonce = nonce;
      if (params.typedChallenge) gateArgs.typedChallenge = params.typedChallenge;
      await resumeGate(gateArgs);
      setDialog(null);
      setResolved(true);
      // Reset ceremony
      setCeremonyActive(false);
      setSealStage('idle');
      setPendingAction(null);
    } catch (e) {
      setActionError(e instanceof CockpitWriteError ? e.detail.error : String(e));
    } finally {
      setBusy(false);
    }
  }

  function handleDialogCancel(): void {
    setDialog(null);
    // Collapse ceremony back to idle on cancel
    setCeremonyActive(false);
    setSealStage('idle');
    setPendingAction(null);
  }

  // -------------------------------------------------------------------------
  // Action dispatch — branches routine vs venom-class
  // -------------------------------------------------------------------------

  async function handleActionClick(action: string): Promise<void> {
    setActionError(null);
    const needsCeremony = isVenomAction(action) || isVenomClass(gate, action);
    if (needsCeremony) {
      await runCeremony(action);
    } else {
      await openResumeDialog(action);
    }
  }

  // -------------------------------------------------------------------------
  // Focus trap on gate panel (ceremony is also a blocking alertdialog)
  // -------------------------------------------------------------------------

  useEffect(() => {
    if (!gate || !gatePanelRef.current) return undefined;
    const panel = gatePanelRef.current;
    const handler = (e: KeyboardEvent): void => {
      if (e.key !== 'Tab') return;
      const focusable = Array.from(
        panel.querySelectorAll<HTMLElement>(
          'button:not(:disabled), input:not(:disabled), textarea:not(:disabled), a[href], [tabindex="0"]',
        ),
      );
      if (focusable.length === 0) return;
      const first = focusable[0]!;
      const last = focusable[focusable.length - 1]!;
      if (e.shiftKey && document.activeElement === first) {
        e.preventDefault();
        last.focus();
      } else if (!e.shiftKey && document.activeElement === last) {
        e.preventDefault();
        first.focus();
      }
    };
    panel.addEventListener('keydown', handler);
    return () => panel.removeEventListener('keydown', handler);
  }, [gate]);

  // -------------------------------------------------------------------------
  // Render: 8-state branches
  // -------------------------------------------------------------------------

  // State 1: Loading (seal materializing)
  if (loading && !gate) {
    return (
      <div className="gate-view gate-view--loading" data-testid="gate-loading">
        <header className="view-header">
          <a href={`#/workflow/${encodeURIComponent(workflowId)}`} className="back-link">
            ← Workflow {workflowId.slice(0, 8)}
          </a>
        </header>
        <LoadingScreen label="Gate materializing — loading seal…" />
      </div>
    );
  }

  // State 2: Error (verbatim request still shown if gate available)
  if (error && !gate) {
    return (
      <div className="gate-view gate-view--error" data-testid="gate-error">
        <header className="view-header">
          <a href={`#/workflow/${encodeURIComponent(workflowId)}`} className="back-link">
            ← Workflow {workflowId.slice(0, 8)}
          </a>
        </header>
        <ErrorScreen message={error} onRetry={() => { void loadGate(); }} />
      </div>
    );
  }

  // State 3: Resolved / already-resolved
  if (resolved) {
    return (
      <div className="gate-view gate-view--resolved" data-testid="gate-resolved">
        <header className="view-header">
          <a href={`#/workflow/${encodeURIComponent(workflowId)}`} className="back-link">
            ← Workflow {workflowId.slice(0, 8)}
          </a>
        </header>
        <div className="gate-not-pending" data-testid="gate-not-pending">
          <p className="gate-not-pending-msg">This gate is no longer pending.</p>
          <a
            href={`#/workflow/${encodeURIComponent(workflowId)}`}
            className="btn btn-ghost"
          >
            View workflow {workflowId.slice(0, 8)} →
          </a>
        </div>
      </div>
    );
  }

  // Determine if this gate is venom-class (for ceremony / visual treatment)
  const gateIsVenom = isVenomClass(gate);
  const actionIsVenom = pendingAction !== null && isVenomAction(pendingAction);
  const showCeremony = ceremonyActive && (gateIsVenom || actionIsVenom);

  return (
    <div
      className={`gate-view${gateIsVenom ? ' gate-view--venom' : ''}`}
      data-testid="gate-view"
    >
      {/* ConfirmDialog renders on top (uses its own portal-like backdrop) */}
      {dialog ? (
        <ConfirmDialog
          state={dialog}
          onConfirm={(p) => { void handleDialogConfirm(p); }}
          onCancel={handleDialogCancel}
          busy={busy}
        />
      ) : null}

      <header className="view-header">
        <a href={`#/workflow/${encodeURIComponent(workflowId)}`} className="back-link">
          ← Workflow {workflowId.slice(0, 8)}
        </a>
        <h1 className="view-title">
          {gateIsVenom ? (
            <span className="covenant gate-title-venom" aria-label="Gate: venom class">
              THE GATE CEREMONY
            </span>
          ) : (
            'Gate Cockpit'
          )}
        </h1>
        {gate?.expires_at ? (
          <span
            className={`gate-expiry${expired ? ' gate-expiry--expired' : ''}`}
            role="status"
            aria-live="polite"
            aria-label={expired ? 'Gate expired — all actions disabled' : `Gate expires in ${remaining}`}
            data-testid="gate-expiry"
          >
            {expired ? '⏳ Expired — gate closed' : `⏳ expires in ${remaining}`}
          </span>
        ) : null}
      </header>

      {/* State 4: Offline — resume disabled with reason */}
      {!online ? (
        <OfflineBanner>
          <p className="gate-offline-reason" data-testid="gate-offline-reason">
            Cannot resume while the control bridge is unreachable.
          </p>
        </OfflineBanner>
      ) : null}

      {/* State 5: Degraded — verbatim request still shown */}
      {degraded ? (
        <DegradedBanner
          sources={['hydra-mem']}
          message="Gate data may be unavailable — no pending gate found for this workflow"
        />
      ) : null}

      {actionError ? (
        <div className="inline-error" role="alert" aria-live="assertive">
          <span aria-hidden="true">▲</span> {actionError}
        </div>
      ) : null}

      {expired ? (
        <div className="expiry-notice" role="alert" aria-live="assertive" data-testid="expiry-notice">
          <strong>Gate expired</strong> — all resume actions are disabled. The workflow is marked surfaced.
        </div>
      ) : null}

      {/* State 6: Gate not found / already resolved (no gate data) */}
      {!gate && !loading ? (
        <div className="gate-not-pending" data-testid="gate-not-pending">
          <p className="gate-not-pending-msg">This gate is no longer pending.</p>
          <a
            href={`#/workflow/${encodeURIComponent(workflowId)}`}
            className="btn btn-ghost"
          >
            View workflow {workflowId.slice(0, 8)} →
          </a>
        </div>
      ) : null}

      {gate ? (
        /*
         * THE GATE PANEL — role=alertdialog (blocking, assertive interrupt).
         * Venom-class gates: persistent venom border for duration of ceremony.
         */
        <div
          ref={gatePanelRef}
          className={[
            'gate-panel',
            gateIsVenom ? 'gate-panel--venom venom-context' : '',
            ceremonyActive ? 'gate-panel--ceremony' : '',
          ].filter(Boolean).join(' ')}
          role="alertdialog"
          aria-modal="true"
          aria-labelledby="gate-panel-title"
          aria-describedby={gateIsVenom ? venomWarningId : undefined}
          data-testid="gate-panel"
        >
          {/* Chain-link icon — semantic gate-is-locked affordance */}
          <div className="gate-chain-lock" aria-hidden="true">
            <svg width="24" height="24" viewBox="0 0 24 24" aria-hidden="true" focusable="false">
              <circle cx="8" cy="8" r="4" stroke="var(--venom)" strokeWidth="2" fill="none" />
              <circle cx="16" cy="16" r="4" stroke="var(--venom)" strokeWidth="2" fill="none" />
              <line x1="11" y1="11" x2="13" y2="13" stroke="var(--venom)" strokeWidth="2" />
            </svg>
          </div>

          <h2 id="gate-panel-title" className="gate-panel-title" data-testid="gate-panel-title">
            {gateIsVenom ? (
              <span className="covenant" aria-label="Name the venom">Name the venom</span>
            ) : (
              'Gate Resolution'
            )}
          </h2>

          {/* ----------------------------------------------------------------
              SEAL-BREAK CEREMONY — venom-class only
              Renders above the form; collapses when stage=revealed.
          ---------------------------------------------------------------- */}
          {showCeremony && sealStage !== 'revealed' ? (
            <div className="seal-ceremony-wrapper" data-testid="seal-ceremony-wrapper">
              <SealCeremony
                venomClass={actionIsVenom || gateIsVenom}
                stage={sealStage}
              />
              <p className="seal-ceremony-label sr-only" aria-live="assertive">
                {sealStage === 'cracking' ? 'Seal cracking — deliberate pause before dispatch' :
                 sealStage === 'splitting' ? 'Seal splitting — Discern before you Declare' :
                 'Seal broken — gate form revealed'}
              </p>
            </div>
          ) : null}

          {/* ----------------------------------------------------------------
              VENOM WARNING — rubricated in vermillion (--venom / Cinzel)
              aria-describedby source for the force-dispatch radio
          ---------------------------------------------------------------- */}
          {gateIsVenom ? (
            <VenomWarning reason={gate.reason} id={venomWarningId} />
          ) : null}

          {/* ----------------------------------------------------------------
              Verbatim HITL request — per spec: do not paraphrase
          ---------------------------------------------------------------- */}
          <section
            className="hitl-verbatim"
            aria-label="HITL request — verbatim"
            role="region"
          >
            <header className="hitl-verbatim-header">HITL REQUEST — VERBATIM</header>
            <pre className="hitl-verbatim-body" tabIndex={0}>
              {[
                gate.workflow_id ? `workflow_id : ${gate.workflow_id}` : null,
                gate.reason ? `reason      : ${gate.reason}` : null,
                gate.summary ? `summary     : ${gate.summary}` : null,
                gate.options ? `options     : ${gate.options.join(' | ')}` : null,
                gate.default_option !== undefined && gate.default_option !== null
                  ? `default     : ${gate.default_option}`
                  : null,
                gate.expires_at ? `expires     : ${gate.expires_at}` : null,
              ]
                .filter(Boolean)
                .join('\n')}
            </pre>
          </section>

          {/* Default option notice */}
          {gate.default_option ? (
            <div className="default-option-notice" aria-label="Default option notice">
              <span aria-hidden="true">ℹ</span> Default option is{' '}
              <strong className="mono">{gate.default_option}</strong> — highlighted below but{' '}
              <em>never preselected</em>. You must choose explicitly. Silence is not consent.
            </div>
          ) : null}

          {/* ----------------------------------------------------------------
              Resume actions
          ---------------------------------------------------------------- */}
          <section className="resume-actions" aria-labelledby="resume-heading">
            <h2 id="resume-heading" className="section-heading">Resume action</h2>
            <div className="resume-action-list" role="group" aria-label="Resume actions">
              {RESUME_ACTIONS
                .filter((a) =>
                  !gate.options ||
                  gate.options.includes(a.action) ||
                  gate.options.length === 0 ||
                  a.action === 'approve' ||
                  a.action === 'reject'
                )
                .map((meta) => {
                  const isDefault = gate.default_option === meta.action;
                  const isVenom = meta.risk === 'venom';
                  const isHigh = meta.risk === 'high';
                  const disabled = !online || expired || busy || ceremonyActive;
                  return (
                    <button
                      key={meta.action}
                      type="button"
                      className={[
                        'resume-action-btn',
                        isDefault ? 'resume-action-btn--default' : '',
                        isVenom ? 'resume-action-btn--venom' : '',
                        isHigh ? 'resume-action-btn--high' : '',
                        meta.risk === 'low' ? 'resume-action-btn--low' : '',
                      ].filter(Boolean).join(' ')}
                      onClick={() => { void handleActionClick(meta.action); }}
                      disabled={disabled}
                      aria-label={[
                        meta.label,
                        isDefault ? '(gate default — requires explicit selection)' : '',
                        isVenom ? '— venom class, Cerberus-gated' : '',
                        !online ? '— disabled: bridge offline' : '',
                        expired ? '— disabled: gate expired' : '',
                      ].filter(Boolean).join(' ')}
                      aria-pressed={false}
                      aria-describedby={isVenom && gateIsVenom ? venomWarningId : undefined}
                      data-testid={`action-btn-${meta.action}`}
                    >
                      {meta.label}
                      {isDefault ? (
                        <span className="resume-default-indicator" aria-hidden="true"> ← default</span>
                      ) : null}
                      {isVenom ? (
                        <span className="venom-badge" aria-hidden="true"> ⛔ venom</span>
                      ) : isHigh ? (
                        <span className="risk-badge risk-badge--high" aria-hidden="true"> ⚠</span>
                      ) : null}
                    </button>
                  );
                })}
            </div>
          </section>

          {/* ----------------------------------------------------------------
              Ghost text — "Approving inscribes your name…"
              aria-describedby wired to approve button via the enclosing region.
          ---------------------------------------------------------------- */}
          <p
            className="gate-inscription-ghost"
            id="gate-inscription-desc"
            aria-label="Covenant notice"
            data-testid="gate-inscription-ghost"
          >
            Approving inscribes your name and timestamp into the codex. This cannot be undone.
          </p>
        </div>
      ) : null}
    </div>
  );
}
