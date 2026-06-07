/**
 * Hydra Cockpit bridge — loopback HTTP server.
 *
 * Scaffold stub. Full implementation lands in C1 (bridge core + read path).
 *
 * Port: 8795 (preferred), probe to 8820. See COCKPIT-DESIGN.md §2.1.
 * Port file: .hydra-cockpit-bridge-port (written atomically on listen,
 *            removed on shutdown). Vite proxy reads this file.
 * CSRF header: X-Hydra-Token. Loopback-only bind (127.0.0.1).
 */

// C1 will implement:
//   - choosePort() probe (8795 → 8820)
//   - isLoopbackHost() DNS-rebinding guard
//   - per-session X-Hydra-Token CSRF
//   - GET /api/health, GET /api/session
//   - hydra_memory stdio MCP child (read path)
//   - hydra_control stdio child (write path, C3)
//   - better-sqlite3 read-only mtime probe (§2.3.4)

export {};
