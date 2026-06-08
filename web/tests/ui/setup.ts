import '@testing-library/jest-dom';
import { afterEach, vi } from 'vitest';
import { cleanup } from '@testing-library/react';

// Clean up after each test
afterEach(() => {
  cleanup();
});

// Mock EventSource globally
const mockEventSource = vi.fn().mockImplementation(() => ({
  addEventListener: vi.fn(),
  removeEventListener: vi.fn(),
  close: vi.fn(),
  onerror: null,
  readyState: 1,
  CONNECTING: 0,
  OPEN: 1,
  CLOSED: 2,
}));
vi.stubGlobal('EventSource', mockEventSource);

// Default fetch mock — individual tests override as needed
vi.stubGlobal('fetch', vi.fn().mockResolvedValue({
  ok: true,
  status: 200,
  json: () => Promise.resolve({}),
}));

// jsdom does not implement scrollIntoView — stub it globally
Element.prototype.scrollIntoView = vi.fn();

// jsdom does not implement matchMedia — stub globally for prefers-reduced-motion checks
if (typeof window.matchMedia !== 'function') {
  vi.stubGlobal('matchMedia', vi.fn().mockImplementation((query: string) => ({
    matches: false, // default: motion allowed (no-preference)
    media: query,
    onchange: null,
    addListener: vi.fn(),
    removeListener: vi.fn(),
    addEventListener: vi.fn(),
    removeEventListener: vi.fn(),
    dispatchEvent: vi.fn(),
  })));
}

// jsdom does not implement ResizeObserver — stub globally
if (typeof window.ResizeObserver !== 'function') {
  vi.stubGlobal('ResizeObserver', vi.fn().mockImplementation(() => ({
    observe: vi.fn(),
    unobserve: vi.fn(),
    disconnect: vi.fn(),
  })));
}

// jsdom does not implement requestAnimationFrame with full semantics — stub if missing
if (typeof window.requestAnimationFrame !== 'function') {
  vi.stubGlobal('requestAnimationFrame', vi.fn().mockImplementation((cb: FrameRequestCallback) => {
    setTimeout(() => cb(performance.now()), 16);
    return 0;
  }));
  vi.stubGlobal('cancelAnimationFrame', vi.fn());
}

// jsdom does not implement HTMLCanvasElement.prototype.getContext (2d).
// Stub it globally so AmbientField can reach its visibilitychange listener registration.
// The stub returns a minimal 2D context duck-type that satisfies all calls inside AmbientField.
const stub2DCtx = {
  clearRect: vi.fn(),
  beginPath: vi.fn(),
  arc: vi.fn(),
  fill: vi.fn(),
  createRadialGradient: vi.fn().mockReturnValue({
    addColorStop: vi.fn(),
  }),
  setTransform: vi.fn(),
  fillStyle: '',
};
// eslint-disable-next-line @typescript-eslint/no-explicit-any
HTMLCanvasElement.prototype.getContext = vi.fn().mockReturnValue(stub2DCtx) as any;
