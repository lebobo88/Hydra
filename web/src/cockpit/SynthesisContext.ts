/**
 * SynthesisContext — lifts live/last synthesis declaration from LiveWorkflowView
 * up to the App shell's Oracle rail without prop-drilling through every route.
 *
 * Usage:
 *   Provider: wrap <App> children (or just provide at App level via useState)
 *   Consumer: LiveWorkflowView calls setSynthesis() when synthesis events arrive
 *   Oracle: reads latestSynthesis + isExecuting from context
 *
 * R1 left latestSynthesis as a null stub; R3 wires it now.
 */

import { createContext, useContext } from 'react';

export interface SynthesisState {
  /** The most recent synthesis declaration text for the open workflow, or null */
  latestSynthesis: string | null;
  /** Whether the open workflow is currently executing (synthesis phase incomplete) */
  isExecuting: boolean;
  /** Update from LiveWorkflowView when synthesis events arrive */
  setSynthesis: (text: string | null, executing: boolean) => void;
}

export const SynthesisContext = createContext<SynthesisState>({
  latestSynthesis: null,
  isExecuting: false,
  setSynthesis: () => undefined,
});

export function useSynthesis(): SynthesisState {
  return useContext(SynthesisContext);
}
