import { defineConfig } from 'vite';
import react from '@vitejs/plugin-react';
import { readFileSync, existsSync } from 'node:fs';
import { fileURLToPath } from 'node:url';

// Discover the bridge port the same way AgentMesh does: read the
// .hydra-cockpit-bridge-port file written by web/server/index.ts at startup.
// Port file lives at the Hydra\web\ root (one level up from vite.config.ts,
// but vite.config.ts IS at the web root, so resolve relative to import.meta.url).
function readBridgePort(): number {
  const portFile = fileURLToPath(new URL('./.hydra-cockpit-bridge-port', import.meta.url));
  if (existsSync(portFile)) {
    const raw = readFileSync(portFile, 'utf8').trim();
    const n = parseInt(raw, 10);
    if (!isNaN(n) && n > 0) return n;
  }
  // Env override — validate the same way as the file (parseInt + isNaN/>0 guard)
  // so a bad env value can't produce an invalid proxy target.
  const envRaw = process.env['HYDRA_COCKPIT_BRIDGE_PORT'];
  if (envRaw !== undefined) {
    const envPort = parseInt(envRaw, 10);
    if (!isNaN(envPort) && envPort > 0) return envPort;
  }
  // Fallback to the preferred port defined in COCKPIT-DESIGN.md §2.1.
  return 8795;
}

const DEV_PORT = 5185;
const bridgePort = readBridgePort();

export default defineConfig({
  plugins: [react()],
  server: {
    host: '127.0.0.1',
    port: DEV_PORT,
    strictPort: true,
    proxy: {
      '/api': {
        target: `http://127.0.0.1:${bridgePort}`,
        changeOrigin: false,
        rewrite: (p) => p,
      },
    },
  },
  build: {
    outDir: 'dist',
    sourcemap: true,
  },
});
