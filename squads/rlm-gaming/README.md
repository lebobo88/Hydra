# squads/rlm-gaming — The Arcade Crown

Hydra-side registration for the **RLM-Gaming** squad pack. The authoritative
source pack (agents, skills, commands) lives at
`C:/AiAppDeployments/RLM-Gaming/` — see its `RLM-GAMING.md`.

## Files

| File | Purpose |
|---|---|
| `squad.yaml` | Squad descriptor: 19 Arcade heads, tools, accepts/emits, gates, delegation routing. |
| `heads.yaml` | Arcade-crown alias overlay (mythic names for the synthesizer). Mirror of the source-pack agents. |
| `cerberus.yaml` | The Sentinel — fair-play venom gate (client-authority, anti-cheat, unsigned gen-AI assets, PII telemetry). |
| `rubrics/*.md` | Squad-level gate rubrics (pillars, loot-box jurisdiction, ratings, platform cert, server-authority, AI provenance). |

## What this squad does

Senior game-studio brain. **Orchestrator + delegate**:

- Owns: vision/greenlight, design (systems/narrative/level/encounter/economy),
  production, runtime-AI & netcode direction, art/audio direction, QA/balance,
  live-ops, ratings/cert, fair-play security.
- Delegates code → `engineering` (pair-programmer game teams) via `PRD`/`DEV_TASK`.
- Delegates assets → `garland` (RLM-Creative/Helios) via
  `CREATIVE_BRIEF`/`SHOT_LIST`/`ASSET_JOB`.

## Activation checklist (per CONTRIBUTING-SQUADS.md §h)

- [x] `squad.yaml` parses; agents reference source-pack `agent_file` paths.
- [x] Gatekeeper heads exist for every HITL gate (Director, Producer,
      Forgemaster, Warden, Custodian, Arbiter, Sentinel).
- [x] Router fingerprint added to `hydra_core/router.py:_KEYWORDS['rlm-gaming']`.
- [x] `accepts`/`emits` use only schema-backed envelope types.
- [x] Rubric ids in `gates:` map to `rubrics/*.md` (2 reuse the pp library:
      `game-perf-budget`, `game-accessibility-guidelines`).
- [x] Gate `when` predicates use only real `Constraints` fields + valid phases.
- [x] `entrypoint: claude-skill` resolves: `_SKILL_PACK_SHIMS['rlm-gaming']`
      registered in `hydra_core/squad_node.py`, shim at `mcp_servers/rlm_gaming/`,
      backend in `~/.hydra/backends.json`, plugin manifest in the source pack's
      `.claude-plugin/`. No longer falls back to the Garland shim.
- [x] The Sentinel's venoms register: `load_cerberus_venoms` now scans every
      `squads/*/cerberus.yaml` (was engineering-only). `hydra doctor` venom
      count 6 → 11.
- [x] Smoke: `hydra run "Greenlight a 2D roguelite for Switch" --squad rlm-gaming`
      → routes to rlm-gaming, dispatch tool-scope resolves all 14 skills + the
      shim write tool. (Full claude-skill execution runs in the plugin host;
      headless CLI returns a stub envelope, same as Garland.)

## Judge plane (wired)

The 6 native gaming rubrics are registered and routed:

- `hydra_core/judge/registry.py` — `kind="arcade"`: `game-design-pillars-testable@1`,
  `loot-box-jurisdiction@1`, `esrb-pegi-iarc-rating@1`, `platform-cert-readiness@1`,
  `server-authority-fairplay@1`, `ai-content-provenance@1` (each with scored
  dimensions; bodies mirror `rubrics/*.md`).
- `hydra_core/judge/router.py` — `route_judge()` binds `game-design-pillars-testable@1`
  unconditionally for any `rlm-gaming` envelope, plus content-conditional
  `_GAMING_TOPIC_RUBRICS` (monetization → loot-box, online → server-authority,
  cert/rating → rating + cert-readiness, gen-AI → provenance).
- `hydra_core/judge/policy.yaml` — `rlm-gaming` added to `enabled_squads`; the five
  human-approval gates added to `hitl_on_fail`.
- `game-perf-budget` + `game-accessibility-guidelines` are judged DOWNSTREAM by
  the pair-programmer harness during the delegated engineering run (not here).

## Known remaining

- Live (`--live`) claude-skill dispatch through the host's native skill API is
  the production path; the headless CLI logs intent only (returns a stub
  envelope, same as Garland).

## Smoke test

```
/hydra:squads                 # rlm-gaming should appear with agents=19
/hydra:run "Design and build a parry mechanic for our Unreal soulslike"
/hydra:status <workflow_id>
```

Expect the workflow to emit a `DECISION_RECORD`, a `PRD`/`DEV_TASK` toward
`engineering`, and (if assets are needed) a `CREATIVE_BRIEF` toward `garland`.
