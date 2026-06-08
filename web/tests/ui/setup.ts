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
