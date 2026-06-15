---
id: server-authority-fairplay@1
bare_id: server-authority-fairplay
kind: security
version: 1
title: "Online builds are server-authoritative and cheat-resistant"
owner_head: game-security-gate
hitl_required: true
---
# Server-authority & fair-play rubric

The Sentinel gates any `online == true` design/build on this. It is the venom
gate for client-authority — see cerberus.yaml. HITL mandatory.

Score 0..1 per cluster:

- **server_authority**: for titles with a trusted server (most competitive /
  PvP / live-economy games), reward/economy/progression/hit-registration truth is
  computed and validated server-side; the client is a renderer + input source.
  **Carve-out:** peer-to-peer, deterministic-lockstep RTS, GGPO-style rollback
  fighting games, and trusted-group co-op may not have an authoritative server —
  for these, the equivalent bar is a documented trust model (deterministic
  desync-detection + checksum verification, host validation, or a designated
  authority peer). The gate fails on *unjustified* client trust, not on the mere
  absence of a dedicated server when the architecture is deliberately P2P.
- **input_validation**: every client→server message is range/rate/sanity
  validated; no trust of client-reported damage, position-truth, currency, or
  loot grants.
- **anticheat_plan**: a named anti-cheat approach (EAC / BattlEye / VAC /
  Ricochet / custom + server heuristics) with deployment + update path. Not
  "TBD". For **non-competitive PvE / trusted-group co-op**, a documented
  "anti-cheat: N/A — non-competitive, no ranked/economy exposure" with the
  reasoning is acceptable in lieu of a client kernel driver.
- **netcode_model_safe**: the replication model (rollback / lockstep / client-
  pred + server-recon) does not open a desync-exploit; lockstep RNG is seeded
  server-side.
- **rate_abuse**: matchmaking, trade, chat, and economy actions are rate-limited
  and abuse-monitored.
- **telemetry_privacy**: anti-cheat + analytics telemetry respects PII
  minimization and minors' data rules (cross-ref cerberus `game.pii_telemetry`).
- **report_and_ban**: a player report path + enforcement pipeline exists.

Outcome:
- pass: every cluster ≥ 0.8 AND no client-authoritative state for value-bearing
  systems AND The Sentinel approved.
- revise: any cluster in [0.6, 0.8).
- fail: any cluster < 0.6 OR any value-bearing state is client-authoritative OR
  anti-cheat is "TBD"/disabled.
