/**
 * Hydra Cockpit — multi-step confirm dialog.
 * Accessibility-first: aria-modal, focus trap, Esc close, visible :focus-visible.
 *
 * Enforces:
 *  - Typed challenge (workflow_id) when the gate is high-risk or force-dispatch.
 *  - Required resolution note on every gate resume.
 *  - Option radios with default highlighted but NEVER preselected (silence ≠ consent).
 *  - Numeric/list validation for modify-budget / change-squads.
 */

import { useEffect, useRef, useState } from 'react';
import type { CockpitDialogState } from '../cockpit/types.ts';

const OPTION_ARG_META: Record<string, { label: string; placeholder: string; numeric: boolean }> = {
  'modify-budget': { label: 'New budget cap (USD, numeric)', placeholder: 'e.g. 120', numeric: true },
  'change-squads': { label: 'Squads (comma-separated slugs)', placeholder: 'e.g. engineering,executive', numeric: false },
};

interface ConfirmDialogProps {
  state: CockpitDialogState | null;
  onConfirm: (params: {
    note?: string;
    option?: string;
    optionArg?: string;
    typedChallenge?: string;
  }) => void;
  onCancel: () => void;
  busy?: boolean;
}

export function ConfirmDialog({ state, onConfirm, onCancel, busy }: ConfirmDialogProps): JSX.Element | null {
  const [typed, setTyped] = useState('');
  const [note, setNote] = useState('');
  const [option, setOption] = useState<string | null>(null);
  const [optionArg, setOptionArg] = useState('');
  const firstInputRef = useRef<HTMLInputElement | HTMLTextAreaElement | null>(null);
  const dialogRef = useRef<HTMLDivElement>(null);

  // Reset state and focus first input on open
  useEffect(() => {
    setTyped('');
    setNote('');
    // SILENCE IS NOT CONSENT: never preselect default option
    setOption(null);
    setOptionArg('');
    if (state) {
      const id = setTimeout(() => firstInputRef.current?.focus(), 30);
      return () => clearTimeout(id);
    }
    return undefined;
  }, [state]);

  // Esc to cancel
  useEffect(() => {
    if (!state) return undefined;
    const handler = (e: KeyboardEvent): void => {
      if (e.key === 'Escape' && !busy) onCancel();
    };
    window.addEventListener('keydown', handler);
    return () => window.removeEventListener('keydown', handler);
  }, [state, busy, onCancel]);

  // Focus trap
  useEffect(() => {
    if (!state || !dialogRef.current) return undefined;
    const dlg = dialogRef.current;
    const handler = (e: KeyboardEvent): void => {
      if (e.key !== 'Tab') return;
      const focusable = Array.from(
        dlg.querySelectorAll<HTMLElement>(
          'button:not(:disabled), input:not(:disabled), textarea:not(:disabled), [tabindex="0"]',
        ),
      ).filter((el) => !el.hasAttribute('data-focus-sentinel'));
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
    dlg.addEventListener('keydown', handler);
    return () => dlg.removeEventListener('keydown', handler);
  }, [state]);

  if (!state) return null;

  const needsTypedChallenge = !!state.typedChallenge;
  const typedOk = !needsTypedChallenge || typed === state.typedChallenge;

  const needsNote = !!state.withNote;
  const noteOk = !needsNote || note.trim().length > 0;

  const hasOptions = state.options && state.options.length > 0;
  const optionOk = !hasOptions || option !== null;

  const argMeta = option !== null ? OPTION_ARG_META[option] : undefined;
  const optionArgOk =
    !argMeta ||
    (optionArg.trim().length > 0 && (!argMeta.numeric || !Number.isNaN(Number(optionArg.trim()))));

  const canConfirm = typedOk && noteOk && optionOk && optionArgOk && !busy;

  function handleConfirm(): void {
    if (!canConfirm) return;
    onConfirm({
      ...(needsNote ? { note: note.trim() } : {}),
      ...(option !== null ? { option } : {}),
      ...(argMeta ? { optionArg: optionArg.trim() } : {}),
      ...(needsTypedChallenge ? { typedChallenge: typed } : {}),
    });
  }

  const isDanger = state.danger ?? false;

  return (
    <div
      className="dlg-backdrop"
      onMouseDown={(e) => {
        if (e.target === e.currentTarget && !busy) onCancel();
      }}
      role="presentation"
    >
      <div
        className={`dlg${isDanger ? ' dlg-danger' : ''}`}
        role="dialog"
        aria-modal="true"
        aria-labelledby="dlg-title"
        ref={dialogRef}
        tabIndex={-1}
        data-testid="confirm-dialog"
      >
        <h2 className="dlg-title" id="dlg-title">
          {state.title}
        </h2>

        {state.lines.map((line, i) => (
          <p className="dlg-line" key={i}>
            {line}
          </p>
        ))}

        {/* Typed challenge (workflow_id) — high-risk gates and force-dispatch */}
        {needsTypedChallenge ? (
          <label className="dlg-field">
            <span>
              {state.typedLabel ?? 'Type to confirm'}:{' '}
              <code className="mono">{state.typedChallenge}</code>
            </span>
            <input
              ref={(el) => { firstInputRef.current = el; }}
              type="text"
              value={typed}
              onChange={(e) => setTyped(e.target.value)}
              spellCheck={false}
              autoComplete="off"
              placeholder={state.typedChallenge}
              disabled={busy}
              aria-required="true"
              aria-label={state.typedLabel ?? 'Type to confirm'}
              data-testid="typed-challenge-input"
            />
            {!typedOk && typed.length > 0 ? (
              <span className="dlg-warn" role="alert">
                Does not match — check the workflow ID
              </span>
            ) : null}
          </label>
        ) : null}

        {/* Gate options — radios, default highlighted but never preselected */}
        {hasOptions ? (
          <fieldset className="dlg-field dlg-options" style={{ border: 0, padding: 0, margin: 0 }}>
            <legend className="dlg-options-legend">
              Resume action <strong>(choose one — nothing is preselected)</strong>
            </legend>
            {state.options!.map((opt) => (
              <label
                key={opt}
                className={`dlg-option-label${state.defaultOption === opt ? ' dlg-option-default' : ''}`}
              >
                <input
                  type="radio"
                  name="gate-action"
                  value={opt}
                  checked={option === opt}
                  onChange={() => { setOption(opt); setOptionArg(''); }}
                  disabled={busy}
                  aria-label={`${opt}${state.defaultOption === opt ? ' (gate default — requires explicit selection)' : ''}`}
                />
                <span className="mono">{opt}</span>
                {state.defaultOption === opt ? (
                  <span className="dlg-option-default-badge" aria-hidden="true">
                    default
                  </span>
                ) : null}
              </label>
            ))}
          </fieldset>
        ) : null}

        {/* Option argument (modify-budget → numeric USD; change-squads → comma list) */}
        {argMeta ? (
          <label className="dlg-field">
            <span>
              {argMeta.label} <strong>(required for {option})</strong>
            </span>
            <input
              type="text"
              value={optionArg}
              onChange={(e) => setOptionArg(e.target.value)}
              spellCheck={false}
              autoComplete="off"
              placeholder={argMeta.placeholder}
              disabled={busy}
              aria-required="true"
              aria-label={argMeta.label}
              inputMode={argMeta.numeric ? 'decimal' : 'text'}
              data-testid="option-arg-input"
            />
            {!optionArgOk && optionArg.trim().length > 0 ? (
              <span className="dlg-warn" role="alert">
                {argMeta.numeric ? 'Must be a valid number' : 'Cannot be blank'}
              </span>
            ) : null}
          </label>
        ) : null}

        {/* Resolution note — required on every gate resume */}
        {needsNote ? (
          <label className="dlg-field">
            <span>
              Resolution note <strong>(required)</strong> — recorded with the decision
            </span>
            <textarea
              ref={(el) => {
                if (!needsTypedChallenge) firstInputRef.current = el;
              }}
              value={note}
              rows={3}
              onChange={(e) => setNote(e.target.value)}
              placeholder="Why are you taking this action?"
              disabled={busy}
              aria-required="true"
              aria-label="Resolution note"
              data-testid="resolution-note"
            />
            {note.length > 0 && note.trim().length === 0 ? (
              <span className="dlg-warn" role="alert">
                Note cannot be blank
              </span>
            ) : null}
          </label>
        ) : null}

        <div className="dlg-actions">
          <button
            className="btn btn-ghost"
            onClick={onCancel}
            disabled={busy}
            type="button"
          >
            Cancel
          </button>
          <button
            className={`btn ${isDanger ? 'btn-danger' : 'btn-primary'}`}
            onClick={handleConfirm}
            disabled={!canConfirm}
            type="button"
            aria-disabled={!canConfirm}
            data-testid="confirm-btn"
          >
            {busy ? 'Working…' : state.verb}
          </button>
        </div>
      </div>
    </div>
  );
}
