import { defineConfig } from 'vitest/config';
import react from '@vitejs/plugin-react';

export default defineConfig({
  plugins: [react()],
  test: {
    include: ['src/**/*.test.tsx', 'tests/ui/**/*.test.tsx'],
    environment: 'jsdom',
    globals: true,
    setupFiles: ['tests/ui/setup.ts'],
  },
});
