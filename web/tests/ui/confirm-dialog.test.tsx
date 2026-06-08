/**
 * UI tests for ConfirmDialog.
 * Covers:
 *  - Confirm button disabled until typed challenge matches exactly
 *  - Required resolution note prevents confirmation
 *  - Option radios: nothing preselected (silence ≠ consent)
 *  - Numeric validation for modify-budget
 *  - Cancel and Esc close the dialog
 */

import { describe, it, expect, vi } from 'vitest';
import { render, screen, fireEvent } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { ConfirmDialog } from '../../src/components/ConfirmDialog.tsx';
import type { CockpitDialogState } from '../../src/cockpit/types.ts';

const WORKFLOW_ID = 'abc12345-def6-7890-abcd-ef1234567890';

function makeDialog(overrides: Partial<CockpitDialogState> = {}): CockpitDialogState {
  return {
    kind: 'gate-resume',
    title: 'Test dialog',
    verb: 'Confirm',
    lines: ['Are you sure?'],
    danger: false,
    payload: {},
    ...overrides,
  };
}

describe('ConfirmDialog', () => {
  it('renders nothing when state is null', () => {
    const { container } = render(
      <ConfirmDialog state={null} onConfirm={vi.fn()} onCancel={vi.fn()} />,
    );
    expect(container.firstChild).toBeNull();
  });

  it('renders dialog with title and lines', () => {
    render(
      <ConfirmDialog
        state={makeDialog({ title: 'My Dialog', lines: ['Line one', 'Line two'] })}
        onConfirm={vi.fn()}
        onCancel={vi.fn()}
      />,
    );
    expect(screen.getByText('My Dialog')).toBeTruthy();
    expect(screen.getByText('Line one')).toBeTruthy();
    expect(screen.getByText('Line two')).toBeTruthy();
  });

  it('confirm button is enabled with no constraints', () => {
    const onConfirm = vi.fn();
    render(
      <ConfirmDialog
        state={makeDialog()}
        onConfirm={onConfirm}
        onCancel={vi.fn()}
      />,
    );
    const btn = screen.getByTestId('confirm-btn');
    expect(btn).not.toBeDisabled();
    fireEvent.click(btn);
    expect(onConfirm).toHaveBeenCalledOnce();
  });

  it('confirm button disabled until typed challenge matches exactly', async () => {
    const user = userEvent.setup();
    const onConfirm = vi.fn();
    render(
      <ConfirmDialog
        state={makeDialog({ typedChallenge: WORKFLOW_ID, typedLabel: 'Type workflow ID' })}
        onConfirm={onConfirm}
        onCancel={vi.fn()}
      />,
    );
    const btn = screen.getByTestId('confirm-btn');
    const input = screen.getByTestId('typed-challenge-input');

    // Initially disabled (empty)
    expect(btn).toBeDisabled();

    // Type wrong value → still disabled
    await user.type(input, 'wrong-value');
    expect(btn).toBeDisabled();

    // Clear and type correct value
    await user.clear(input);
    await user.type(input, WORKFLOW_ID);
    expect(btn).not.toBeDisabled();
    fireEvent.click(btn);
    expect(onConfirm).toHaveBeenCalledOnce();
  });

  it('confirm disabled when required note is empty', async () => {
    const user = userEvent.setup();
    const onConfirm = vi.fn();
    render(
      <ConfirmDialog
        state={makeDialog({ withNote: true })}
        onConfirm={onConfirm}
        onCancel={vi.fn()}
      />,
    );
    const btn = screen.getByTestId('confirm-btn');
    const note = screen.getByTestId('resolution-note');

    // Initially disabled (note empty)
    expect(btn).toBeDisabled();

    // Type a note → enabled
    await user.type(note, 'My reason for this action');
    expect(btn).not.toBeDisabled();
    fireEvent.click(btn);
    expect(onConfirm).toHaveBeenCalledWith(expect.objectContaining({ note: 'My reason for this action' }));
  });

  it('both typed challenge AND note required simultaneously', async () => {
    const user = userEvent.setup();
    render(
      <ConfirmDialog
        state={makeDialog({ typedChallenge: WORKFLOW_ID, typedLabel: 'Type workflow ID', withNote: true })}
        onConfirm={vi.fn()}
        onCancel={vi.fn()}
      />,
    );
    const btn = screen.getByTestId('confirm-btn');
    const input = screen.getByTestId('typed-challenge-input');
    const note = screen.getByTestId('resolution-note');

    // Type correct challenge but no note → disabled
    await user.type(input, WORKFLOW_ID);
    expect(btn).toBeDisabled();

    // Add note → enabled
    await user.type(note, 'Reason');
    expect(btn).not.toBeDisabled();
  });

  it('options: nothing preselected by default (silence ≠ consent)', () => {
    render(
      <ConfirmDialog
        state={makeDialog({
          options: ['approve', 'reject', 'modify-budget'],
          defaultOption: 'reject',
        })}
        onConfirm={vi.fn()}
        onCancel={vi.fn()}
      />,
    );
    // All radios should be unchecked
    const radios = screen.getAllByRole('radio');
    for (const radio of radios) {
      expect((radio as HTMLInputElement).checked).toBe(false);
    }
    // Confirm button should be disabled (no option selected)
    expect(screen.getByTestId('confirm-btn')).toBeDisabled();
  });

  it('selecting an option enables confirm', async () => {
    const user = userEvent.setup();
    const onConfirm = vi.fn();
    render(
      <ConfirmDialog
        state={makeDialog({ options: ['approve', 'reject'], defaultOption: 'reject' })}
        onConfirm={onConfirm}
        onCancel={vi.fn()}
      />,
    );
    const btn = screen.getByTestId('confirm-btn');
    expect(btn).toBeDisabled();

    // Select approve
    const approveRadio = screen.getByRole('radio', { name: /approve/ });
    await user.click(approveRadio);
    expect(btn).not.toBeDisabled();
  });

  it('modify-budget requires numeric argument', async () => {
    const user = userEvent.setup();
    const onConfirm = vi.fn();
    render(
      <ConfirmDialog
        state={makeDialog({ options: ['modify-budget'], defaultOption: null })}
        onConfirm={onConfirm}
        onCancel={vi.fn()}
      />,
    );
    const btn = screen.getByTestId('confirm-btn');

    // Select modify-budget
    const modifyRadio = screen.getByRole('radio', { name: /modify-budget/ });
    await user.click(modifyRadio);

    // Arg input appears, but button still disabled (empty arg)
    const argInput = screen.getByTestId('option-arg-input');
    expect(btn).toBeDisabled();

    // Type invalid text → still disabled
    await user.type(argInput, 'not-a-number');
    expect(btn).toBeDisabled();

    // Clear and type valid number → enabled
    await user.clear(argInput);
    await user.type(argInput, '120');
    expect(btn).not.toBeDisabled();
    fireEvent.click(btn);
    expect(onConfirm).toHaveBeenCalledWith(expect.objectContaining({ optionArg: '120' }));
  });

  it('Cancel button calls onCancel', () => {
    const onCancel = vi.fn();
    render(
      <ConfirmDialog
        state={makeDialog()}
        onConfirm={vi.fn()}
        onCancel={onCancel}
      />,
    );
    fireEvent.click(screen.getByText('Cancel'));
    expect(onCancel).toHaveBeenCalledOnce();
  });

  it('Escape key calls onCancel', () => {
    const onCancel = vi.fn();
    render(
      <ConfirmDialog
        state={makeDialog()}
        onConfirm={vi.fn()}
        onCancel={onCancel}
      />,
    );
    fireEvent.keyDown(window, { key: 'Escape' });
    expect(onCancel).toHaveBeenCalledOnce();
  });

  it('busy prop disables all inputs and buttons', () => {
    render(
      <ConfirmDialog
        state={makeDialog({ withNote: true })}
        onConfirm={vi.fn()}
        onCancel={vi.fn()}
        busy={true}
      />,
    );
    expect(screen.getByTestId('confirm-btn')).toBeDisabled();
    // Cancel also disabled when busy
    expect(screen.getByText('Cancel')).toBeDisabled();
  });

  it('shows "Working…" text when busy', () => {
    render(
      <ConfirmDialog
        state={makeDialog({ verb: 'Launch', withNote: true })}
        onConfirm={vi.fn()}
        onCancel={vi.fn()}
        busy={true}
      />,
    );
    expect(screen.getByText('Working…')).toBeTruthy();
  });

  it('applies danger styling when danger=true', () => {
    render(
      <ConfirmDialog
        state={makeDialog({ danger: true })}
        onConfirm={vi.fn()}
        onCancel={vi.fn()}
      />,
    );
    const dlg = screen.getByTestId('confirm-dialog');
    expect(dlg.classList.contains('dlg-danger')).toBe(true);
  });

  it('no danger styling when danger=false', () => {
    render(
      <ConfirmDialog
        state={makeDialog({ danger: false })}
        onConfirm={vi.fn()}
        onCancel={vi.fn()}
      />,
    );
    const dlg = screen.getByTestId('confirm-dialog');
    expect(dlg.classList.contains('dlg-danger')).toBe(false);
  });
});
