# Hydra Cockpit — Design Document

> **Status:** Stage 1 deliverable (design, reviewable). Stage 2 (build, chunks C0–C8) requires a **separate operator approval** after this document is reviewed.
> **Initiative:** hydra-cockpit
> **Hydra workflow (Stage 1):** `5ebd4268-5de0-4dbf-a82d-42c596d4818e`
> **Approved plan:** `C:\Users\robob\.claude\plans\explain-the-ui-for-tidy-trinket.md`
> **Target app root:** `C:\AiAppDeployments\Hydra\web\`
> **Authoring agent:** `docs-author` (typed producer, engineering squad, pp_harness run `run_665s1xz7U3RN`)

---

## 0. Summary & decision

Hydra (the LangGraph orchestrator at `C:\AiAppDeployments\Hydra\`) has no UI of its own. Operators drive it through the `hydra` CLI (`run/status/approve/resume/...`). The AgentMesh web console only **observes** Hydra (read views + approve/reject on gates), constitutionally capped at three sanctioned operator writes by `CONSTITUTION.md` Article III §6. That leaves roughly 80% of Hydra's interaction surface CLI-only: launching workflows, three of the five resume actions, budget edits, memory/trace exploration, and replay.

**Decision (user-confirmed): hybrid.** The mesh console stays the read-mostly fleet observatory. A new **standalone Hydra Cockpit** at `C:\AiAppDeployments\Hydra\web\` owns the full Hydra interaction surface. The mesh console deep-links into it. This keeps Article III §6 untouched: the cockpit introduces **no new authority** — it is a second front-end onto Hydra's *existing* governed write paths, deriving authority from Hydra's own governance regime (TheEights → AgentSmith → Hydra precedence, Cerberus venom gate, eights budget governance, constitution attestation/postcheck), the same regime that already governs the `hydra` CLI today.

**v1 scope (user-confirmed, all four pillars):** goal intake/launch, full gate cockpit (all five resume actions), live execution view, memory/trace/replay.

This document expands the approved plan summary into the full UX spec, architecture spec, governance argument, resolved gaps, and the C0–C8 build staging that Stage 2 will execute.

---

# 1. UX Specification

Stack: **React 18 + Vite + TypeScript**, mirroring the mesh console idioms in `AgentMesh\web\src\`. Hash routing. The mesh 8-state machine. WCAG 2.2 AA. Reuse (do not reinvent) `ConfirmDialog`, the polling hook (`useConsoleLive`), the degraded-notice/state-screen patterns, and the gate-normalization logic from `HitlView.tsx`.

## 1.1 View / route inventory

| Route | View | Purpose | Key actions |
|---|---|---|---|
| `#/` | **Launchpad** | Active + recent workflows; phase chips; budget bars; gate badges (a pending gate is the loudest element on the card) | New Run → `#/launch`; open workflow; open gate |
| `#/launch` | **Launch Composer** | Goal text; **router preview** (squad fingerprint scores, read-only); squad hints; budget cap; **dry-run (default) vs live** toggle | Preview routing (read-only); **Dry-run** (default); **Launch** (live, gated) |
| `#/workflow/:id` | **Live Workflow** | 8-node phase-machine viz; typed envelope stream with judge verdicts + Reflexion ×1 markers; budget ticker with 80%/100% bands; task list | Open gate; Modify budget; Abort; Replay |
| `#/gate/:hitl_id` | **Gate Cockpit** | The `HITLRequest` rendered **verbatim**; expiry countdown; all **5 resume actions** | approve · reject · modify-budget · change-squads · force-dispatch |
| `#/squads` | **Squads** | Discovered squad packs (13 today); entrypoint, industries, accepts/emits, agents | use-as-hint → prefilled `#/launch` |
| `#/campaigns` | **Campaigns** | Campaign rollups; per-phase budget vs eights cap | open child workflow; use-as-hint |
| `#/memory` | **Memory / Trace / Replay** | 8-cell episodic grid (qian, kun, zhen, xun, kan, li, gen, dui); semantic search; trace timeline; replay-from-checkpoint picker | search; tag; Replay |

The 8 episodic cells (`qian, kun, zhen, xun, kan, li, gen, dui`) are the canonical bagua keys used by `hydra-mem` (`write_episodic.cells` enum). The grid renders one cell per trigram with its record count and a click-through to the cell's records.

The 8 phase-machine nodes mirror the supervisor graph: **intake → planning → approval → dispatch → executing → judge → synthesis → postcheck** (with `surfaced` and `done` as terminal annotations). `approval`, `synthesis`, and `judge_synthesis` are the `interrupt_before` boundaries.

## 1.2 ASCII wireframes (the 4 key screens)

### 1.2.1 Launchpad `#/`

```
+--------------------------------------------------------------------------+
|  HYDRA COCKPIT          [Launchpad] Launch  Squads  Campaigns  Memory     |
|  bridge: ok  ·  checkpoints.db: fresh 3s ago            [ + New Run ]     |
+--------------------------------------------------------------------------+
|  ACTIVE (2)                                                               |
|  +--------------------------------------------------------------+         |
|  | 5ebd4268  Stage1 cockpit design        [executing]           |         |
|  | phase ● ● ● ● ◐ ○ ○ ○   eng                                  |         |
|  | budget [##########------] $42 / $80 (52%)                    |         |
|  |                                              [open ▸]         |         |
|  +--------------------------------------------------------------+         |
|  | 1d48bb4d  payments idempotency     ⚠ GATE: high_risk         |  <--    |
|  | phase ● ● ◐ ○ ○ ○ ○ ○   eng, exec        expires 03:58:11    | loudest |
|  | budget [################] $80 / $80 (100%)  ⛔                |         |
|  |                              [ open gate ▸ ]                  |         |
|  +--------------------------------------------------------------+         |
|                                                                          |
|  RECENT (5)                                                               |
|  9af0…  marketing launch      [done]     replay ▸                        |
|  77be…  research synth        [surfaced] open ▸                          |
+--------------------------------------------------------------------------+
```

### 1.2.2 Launch Composer `#/launch`

```
+--------------------------------------------------------------------------+
|  ← Launchpad        LAUNCH COMPOSER                                       |
+--------------------------------------------------------------------------+
|  Goal                                                                     |
|  [ Add idempotency-key support to /payments POST..................... ]   |
|                                                                          |
|  Router preview (read-only)              [ Preview routing ]              |
|  +------------------------------------------------------------+          |
|  | engineering   ████████████  0.82   ← selected              |          |
|  | executive     ███           0.21                           |          |
|  | research-ds   ██            0.14                           |          |
|  +------------------------------------------------------------+          |
|                                                                          |
|  Squad hints (optional)  [engineering ✕] [+ add]                         |
|  Budget cap (USD)        [  80  ]                                         |
|                                                                          |
|  Mode:  ( • ) Dry-run  (validate routing + plan, NO dispatch)  ← default |
|         ( ) Live       (real dispatch — High-risk write)                 |
|                                                                          |
|                       [ Dry-run ]      [ Launch (live) ⚠ ]               |
+--------------------------------------------------------------------------+
```

`Launch (live)` is a **High-risk** write: it opens `ConfirmDialog` with `confirm:true` + a server-issued confirm nonce. Dry-run is the default and is **not** high-risk (it dispatches nothing; it returns the router decision + plan only).

### 1.2.3 Live Workflow `#/workflow/:id`

```
+--------------------------------------------------------------------------+
|  ← Launchpad    WORKFLOW 5ebd4268   [executing]      ● live (SSE)         |
+--------------------------------------------------------------------------+
|  Phase machine                                                           |
|   intake → planning → approval → dispatch → executing → judge → synth →  |
|     ✓        ✓          ✓ (HITL)    ✓          ◐         ○       ○        |
|                                                          ↑ now            |
|                                                                          |
|  Budget ticker                                                           |
|  [############------------]  $42 / $80   52%                              |
|  bands:  | 0 ........ 80% ⚠ downgrade ........ 100% ⛔ HITL |             |
|                                                                          |
|  Envelope stream                                  [ Modify budget ]      |
|  13:42:21  DEV_TASK    eng → eng     dispatched     [Abort] [Replay]      |
|  13:42:48  judge       codex  cross  outcome=revise                      |
|  13:42:49  reflexion   ×1     retry_index=1                              |
|  13:43:02  judge       codex  cross  outcome=approve  ✓                  |
|  13:43:10  DECISION_RECORD   synthesized                                  |
+--------------------------------------------------------------------------+
```

The envelope stream renders typed envelopes from `trace.jsonl` with judge verdicts and the **Reflexion ×1** marker (`retry_index=1`; a second reflexion would violate the invariant and is flagged red). The budget ticker shows the 80% downgrade band and the 100% HITL band.

### 1.2.4 Gate Cockpit `#/gate/:hitl_id`

```
+--------------------------------------------------------------------------+
|  ← Workflow 1d48bb4d      GATE COCKPIT          expires in 03:58:11 ⏳    |
+--------------------------------------------------------------------------+
|  ====== HITL REQUEST — VERBATIM ======                                   |
|  workflow_id : 1d48bb4d-2248-470a-82f2-7713c71aa55e                      |
|  reason      : high_risk                                                  |
|  summary     : Dispatch creative+engineering to launch campaign for $80k |
|  options     : approve | reject | modify-budget                          |
|  default     : reject                                                     |
|  expires     : 2026-06-07T18:00:00Z                                       |
|  =====================================                                   |
|                                                                          |
|  Resume action                                                           |
|   ( ) approve            ( ) reject  «default — highlighted, NOT preset» |
|   ( ) modify-budget  [ ____ USD ]                                        |
|   ( ) change-squads  [ engineering, executive            ]              |
|   ( ) force-dispatch  ⚠ venom-class · Cerberus-gated                     |
|                                                                          |
|  Resolution note (required)                                              |
|  [ ................................................................ ]     |
|                                                                          |
|  Type the workflow id to confirm (high-risk / force-dispatch):           |
|  [ 1d48bb4d-2248-470a-82f2-7713c71aa55e ]                                |
|                                                                          |
|                                  [ Cancel ]   [ Resume ⚠ ]               |
+--------------------------------------------------------------------------+
```

The HITL request is rendered **verbatim** — `reason`, `summary`, `options`, `default_option`, `expires_at` — exactly as the envelope carries them, per the HITL protocol ("do not paraphrase the request"). The `default_option` is **highlighted but never preselected** ("silence ≠ consent"). A **resolution note is required** on every resume. The **typed workflow-id challenge** appears on high-risk gates and **unconditionally on force-dispatch**. The **expiry countdown disables all actions** when it reaches zero (the workflow becomes `surfaced`).

## 1.3 Interaction flows

### 1.3.1 Happy path — launch → watch → gate → resume → done

1. Launchpad → **New Run** → Launch Composer.
2. Enter goal; **Preview routing** (read-only) confirms squad selection.
3. Leave **Dry-run** selected → click **Dry-run**: bridge returns the router decision + flat plan; nothing dispatched. Operator reviews.
4. Switch to **Live**, click **Launch (live)** → ConfirmDialog (confirm + nonce) → bridge launches a **detached `hydra run` subprocess**, returns `{workflow_id, pid, log}` immediately, and the UI **navigates to `#/workflow/:id`** ("fire-and-attach").
5. Live Workflow view opens an **SSE stream**; phases animate; envelopes append.
6. At the `approval` interrupt the workflow pauses; a **gate badge** appears; UI offers **Open gate**.
7. Gate Cockpit renders the request verbatim; operator picks **approve**, writes a resolution note, (types the workflow id if high-risk), confirms.
8. Bridge calls `hydra.workflow.resume` (action `approve`) via the `hydra_control` stdio child; the resume.lock serializes it. The workflow resumes; SSE resumes streaming.
9. Phase reaches `postcheck` → `done`; budget ticker settles; envelope stream shows the final `DECISION_RECORD`.

### 1.3.2 Reject path

At step 7 operator picks **reject** → resolution note required → confirm → `hydra.workflow.resume` with action `reject`. The workflow is marked `surfaced`; the rejection is logged to `hitl_history` (append-only). The Live Workflow view shows the terminal `surfaced` state and the rejection note.

### 1.3.3 Budget-tripwire path

As the budget ticker crosses **80%**, the band turns amber and the stream shows a model-tier downgrade event (governance auto-action). At **100%** the supervisor raises a `budget_approval` HITL gate; the Live Workflow view surfaces the gate badge. Operator opens the Gate Cockpit and chooses **modify-budget** (enter new USD cap — High-risk, nonce-gated, typed challenge) or **reject**. modify-budget patches `state.budget.budget_usd` and resumes.

### 1.3.4 Replay path

From a `done`/`surfaced` workflow (Live Workflow header or Memory view) operator picks **Replay** → the replay picker lets them choose `--from-phase` and optionally `--swap-model`, and a **live vs dry** toggle. Replay is a **High-risk** write; with `--live` it is additionally **venom-gated**. The bridge launches the detached replay subprocess (fire-and-attach) and navigates to the new workflow's `#/workflow/:id`.

## 1.4 Risk affordances (cross-cutting)

| Affordance | Rule |
|---|---|
| **Default option** | `default_option` is highlighted but **never preselected**. Silence ≠ consent. |
| **Resolution note** | **Required** on every gate resume (non-empty). |
| **Typed workflow-id challenge** | Required on **high-risk** gates and **unconditionally on force-dispatch**. The typed string must equal the workflow id exactly. |
| **Force-dispatch** | venom-class: Cerberus-gated server-side **and** typed challenge in the UI. Emits a `policy_override` event; operator owns the risk. |
| **Replay --live** | venom-gated in addition to High-risk confirm + nonce. |
| **Expiry countdown** | Live countdown; **disables all actions** on expiry; the workflow is marked `surfaced`. |
| **Offline state** | **All write buttons disabled**; read views show last-known snapshot with a staleness banner. |
| **Confirm nonce** | High-risk writes require a server-issued confirm nonce (single-use, short-TTL) in addition to `confirm:true` + CSRF. |

## 1.5 Per-view 8-state matrix

The 8 states are the mesh union: **loading / empty / error / degraded / offline / partial / live / confirm**. Each view declares a screen for every state (reusing `StateScreens.tsx` idioms).

| View | loading | empty | error | degraded | offline | partial | live | confirm |
|---|---|---|---|---|---|---|---|---|
| Launchpad | skeleton cards | "No workflows yet — New Run" | bridge-error banner + retry | "checkpoints.db stale" notice; cached list | read-only cached; New Run disabled | some workflows missing state | full live list | n/a |
| Launch Composer | n/a | n/a | preview/launch error inline | router preview unavailable → manual hints only | Launch + Dry-run disabled | preview partial (scores missing) | ready | ConfirmDialog (launch nonce) |
| Live Workflow | phase skeleton | "No envelopes yet" | SSE error → fall back to poll banner | SSE down, polling fallback active | stream paused; actions disabled | trace tail gap (cursor reset) | SSE attached, streaming | ConfirmDialog (modify-budget / abort) |
| Gate Cockpit | loading gate | "Gate already resolved" | resume error inline (idempotent retry) | eights down → live-gate mode notice | resume disabled | options partial → render raw payload | gate live, countdown running | ConfirmDialog (resume + typed challenge + nonce) |
| Squads | skeleton | "No squads discovered" | error banner | stale registry notice | read-only | partial pack metadata | live list | n/a |
| Campaigns | skeleton | "No campaigns" | error banner | per-phase cap unknown | read-only | partial rollups | live | n/a |
| Memory | grid skeleton | "No episodic records" | search/grid error | semantic search degraded → episodic only | read-only; tag/replay disabled | some cells unresolved | live grid + search | ConfirmDialog (tag / replay) |

## 1.6 Accessibility — WCAG 2.2 AA commitment

The cockpit commits to **WCAG 2.2 AA**:

- Full keyboard operability; visible focus indicators (2.4.7, plus 2.4.11 *Focus Not Obscured* new in 2.2).
- Target size **≥ 24×24 CSS px** for all interactive controls (2.5.8, new in 2.2).
- Radio groups (resume actions, dry/live) and the typed-challenge field are labeled and announced; the verbatim HITL block uses a `<pre>`/`role="region"` with an accessible name.
- The expiry countdown uses `aria-live="polite"`; the **disabled-on-expiry** transition is announced.
- Color is never the sole signal: budget bands, gate badges, and verdict outcomes carry text/iconography in addition to color (1.4.1).
- Contrast ≥ 4.5:1 (text) / 3:1 (UI components & graphics) (1.4.3, 1.4.11).
- `ConfirmDialog` is a focus-trapped modal with `aria-modal`, restoring focus on close (no keyboard trap, 2.1.2).
- Reduced-motion honored for the phase-machine animation (`prefers-reduced-motion`).

---

# 2. Architecture Specification

## 2.1 App layout & ports

Standalone app at `C:\AiAppDeployments\Hydra\web\`:

```
Hydra\web\
  server\        # the loopback bridge (Node 20 / TS), fork of AgentMesh\web\server\
  src\           # React 18 + Vite + TS SPA
  docs\          # this document
  package.json   # vite 5185, bridge npm scripts, vitest
```

| Concern | Cockpit | Mesh console (unchanged) |
|---|---|---|
| Bridge port | **8795** (preferred) + probe to **8820** | 8790 |
| Port file | `.hydra-cockpit-bridge-port` | `.mesh-bridge-port` |
| Vite dev | **5185** | 5180 |
| CSRF header | `X-Hydra-Token` | `X-Mesh-Token` |
| Fixed envelope actor / project | `hydra-cockpit` / `Hydra` | `mesh-console` / `AgentMesh` |

The port-probe logic forks `choosePort()` from `AgentMesh\web\server\index.ts` (probe preferred → roll forward up to the max; pinned-port failure is fatal). The bound port is written atomically to `.hydra-cockpit-bridge-port` on listen and removed on shutdown.

## 2.2 The 5 forked invariants

Forked verbatim in intent from `AgentMesh\web\server\index.ts`:

1. **Loopback bind** — bind `127.0.0.1`, never `0.0.0.0`.
2. **Host-header DNS-rebinding check** — `isLoopbackHost()` on every request; reject non-loopback Host with `400 HOST_REJECTED`.
3. **Per-session CSRF** — `X-Hydra-Token` required on all POSTs; **timing-safe** comparison (`verifyToken`); `403 CSRF` on missing/wrong.
4. **Fixed server-side envelope** — `actor='hydra-cockpit'`, `project='Hydra'` injected server-side on every audited call; browser input never touches these fields.
5. **No-email-in-payloads** — `GET /api/session` returns `{ token, actor: 'hydra-cockpit' }` — never the operator email. No response payload carries an email.

## 2.3 Three-transport mix

Each operation class uses the transport that gives Hydra's existing guarantees for free:

### 2.3.1 Reads → `hydra_memory` stdio MCP child

Spawn a `hydra_memory` stdio MCP child (forking `mesh-client.ts` single-flight stdio idioms). Read tools:

- `workflows_list` — workflow summaries for Launchpad/Campaigns.
- `workflow_status` — one workflow's live state.
- `squad_list` — discovered packs for Squads view.
- `hitl_pending` — pending gates for the gate badges / Gate Cockpit.
- `episodic` — the 8-cell grid + cell records.
- `semantic_search` — Memory view search.

All reads go through a read whitelist (hard allowlist + forbidden-verb denylist), structurally separate from the write path (GET-only, no write tool reachable).

### 2.3.2 Gate resumes (×5) → `hydra_control` stdio child

A `hydra_control` stdio child calls `hydra.workflow.resume` for the five actions (`approve`, `reject`, `modify-budget`, `force-dispatch`, `change-squads`). This **inherits `resume.lock` serialization and idempotency for free** — the same path the mesh console's write #3 uses. The bridge's validation alphabet **must remain byte-identical** to `hydra_control\server.py`:

```
WORKFLOW_ID_RE = /^[A-Za-z0-9][A-Za-z0-9\-_]{0,63}$/      // == _WORKFLOW_ID_RE
OPTION_RE      = /^[A-Za-z0-9 ,._\-]{0,200}$/             // == _OPTION_RE
RESUME_ACTIONS = approve | reject | modify-budget | force-dispatch | change-squads
```

(Confirmed against `Hydra\mcp_servers\hydra_control\server.py` lines 50–55. Keeping these identical bridge↔Python prevents a request the bridge accepts but Python rejects, or vice-versa.)

### 2.3.3 Launch / replay → detached CLI subprocess ("fire-and-attach")

`launch` and `replay` spawn a **detached `hydra` CLI subprocess** with a **fixed, validated argv** (no shell, no string interpolation of user input — argv tokens only, each matched against the validation alphabet so no shell metacharacters can appear), a **per-workflow log file**, and return `{workflow_id, pid, log}` **immediately**. The UI then attaches via SSE. This mirrors `_launch_resume()` in `hydra_control\server.py` (detached `Popen` with fixed argv, log file).

> **Why fire-and-attach:** a `hydra run` can take minutes and pauses at HITL gates. Blocking the HTTP request would hang the bridge. Returning the id + pid + log immediately lets the UI navigate to `#/workflow/:id` and stream.

### 2.3.4 Change detector → `better-sqlite3` read-only mtime probe

A `better-sqlite3` **read-only** handle probes the **mtime** of `~/.hydra/checkpoints.db`. A bump in mtime signals "a checkpoint was written" — used to catch **interrupt pauses** (the LangGraph `interrupt_before` boundaries) that may not produce a trace line immediately. Read-only open; no schema writes.

## 2.4 Real-time: SSE

### 2.4.1 `GET /api/workflows/:id/stream`

Tails the workflow's append-only `trace.jsonl` (`<project>\.hydra\<workflow_id>\trace.jsonl`, per `hydra_core.telemetry.trace_path`) via **`fs.watch` + a byte cursor**. Each new JSONL line is parsed and emitted as a typed SSE event:

| SSE event | Source line `kind` | Payload |
|---|---|---|
| `trace` | any envelope/judge/boundary line | the raw trace record (envelope id/type, judge verdict, reflexion marker, span) |
| `state` | `node_context`, `workflow_start`, phase transitions | current phase, selected squads, budget snapshot |
| `gate` | a `HITL_REQUEST` surfacing | the verbatim gate payload (reason, summary, options, default, expires) |
| `ping` | heartbeat (every ~15s) | `{}` (keeps the connection + proxies alive) |
| `done` | terminal (`done` / `surfaced`) | final phase + DECISION_RECORD ref |

In addition to the trace tail, a **2s checkpoint-mtime poll** (§2.3.4) catches `interrupt` pauses where the supervisor checkpoints **before** emitting a visible trace line — without it, a gate pause could go unseen until the next trace write.

The trace contract is exactly `hydra_core.telemetry.emit`: one JSON object per line, fields `ts` (ISO-8601 UTC), `kind`, `workflow_id`, plus kind-specific payload. The cursor is a byte offset; on reconnect the client sends `Last-Event-ID` (the byte offset) so the bridge resumes from the cursor (no re-replay of seen lines; a cursor reset → `partial` state).

### 2.4.2 Polling fallback

An equivalent **polling endpoint** (`GET /api/workflows/:id/trace?cursor=<byte>`) returns the same record shapes for environments where SSE is unavailable (corporate proxies). The UI's live hook (forking `useConsoleLive`) chooses SSE first, falls back to polling on SSE error, and surfaces the `degraded` state when it does.

**UI cadence:** 2–3s for the **viewed active** workflow; 8s elsewhere; **stop when done or when the tab is hidden** (`visibilitychange`).

## 2.5 Write whitelist (8, frozen server-side)

The bridge exposes a **frozen, server-side write whitelist of exactly 8 tools**. The browser supplies only payload fields; tool names are server-controlled literals. Every write requires `confirm:true` + CSRF; **high-risk writes additionally require a server-issued confirm nonce** (single-use, short-TTL, bound to the action + workflow).

| # | Write | Risk class | Extra gate | Transport |
|---|---|---|---|---|
| 1 | `launch` | **High** (dry-run is the default; live launch is High) | confirm nonce | detached CLI |
| 2 | `approve` | **Med** | confirm nonce | `hydra_control` resume |
| 3 | `reject` | **Low** | — | `hydra_control` resume |
| 4 | `modify-budget` | **High** | confirm nonce + typed challenge | `hydra_control` resume |
| 5 | `force-dispatch` | **venom-class** | **Cerberus-gated** + typed challenge + nonce | `hydra_control` resume |
| 6 | `change-squads` | **Med** | confirm nonce | `hydra_control` resume |
| 7 | `replay` | **High** (**venom-gated if `--live`**) | confirm nonce (+ venom if live) | detached CLI |
| 8 | `tag_memory` | **Low** | — | `hydra_memory` tag |

The whitelist is a frozen `Object.freeze([...])` set with an `allowWriteTool()` closure (forking `write-whitelist.ts`). Adding a tool is a governance event. Every write is dispatched only after: Host check → CSRF → whitelist membership → payload validation → (nonce if high-risk) → (typed challenge if high-risk/force-dispatch) → (Cerberus/venom if venom-class).

## 2.6 Audit

Two complementary records per write:

1. **eights envelope** — one eights envelope per write via a **NEW `hydra.cockpit.audit` tool added to `hydra_control`**. This tool drives `EightsAttestor.envelope_record` and is **spool-safe when eights is down** (spools locally, replays on reconnect). The fixed server-side envelope (`actor='hydra-cockpit'`, `project='Hydra'`) is applied here; no email in the payload.
2. **trace ledger** — the existing `trace.jsonl` Hydra already emits for the resumed/launched workflow.

This gives the cockpit the same audit posture as the CLI: every governed write is attested to TheEights and appears in the workflow trace.

## 2.7 Deep-link contract (mesh → cockpit, one-directional)

The mesh console links **into** the cockpit (never the reverse — the mesh console gains no new authority):

```
http://127.0.0.1:5185/#/workflow/:id
http://127.0.0.1:5185/#/gate/:hitl_id
http://127.0.0.1:5185/#/launch?goal=<urlencoded>&squads=a,b
```

The Launch Composer reads `goal` and `squads` from the query string to prefill (still dry-run by default; still gated to launch).

---

# 3. Governance

## 3.1 No AgentMesh CONSTITUTION amendment is required

`CONSTITUTION.md` Article III §6 reads, verbatim:

> "To grant the web console any tool outside its read-only whitelist plus the two sanctioned operator writes (daemon restart, HITL acknowledgement)."

This clause scopes to **the mesh console's envelope** (`actor='mesh-console'`, project `AgentMesh`), which the cockpit **does not touch**. The cockpit is a **separate front-end** with its own envelope (`actor='hydra-cockpit'`, project `Hydra`). It introduces **no new authority**: its eight writes are all pre-existing Hydra governed write paths (the same `hydra resume` / `hydra run` / memory-tag paths the CLI already drives). The cockpit's authority derives from the **Hydra → TheEights precedence** that already governs the `hydra` CLI today:

- **Cerberus venom gate** — force-dispatch (and live replay) pass the same venom gate the CLI does.
- **eights budget governance** — budget tripwire (80% downgrade) and 100% HITL are enforced by the supervisor regardless of front-end.
- **constitution attestation / postcheck** — `enforce_governance` runs at postcheck regardless of front-end.

Because the mesh-console envelope is untouched and no new authority is created, **Article III §6 needs no amendment** for the cockpit.

## 3.2 Obligations (in scope for Stage 2)

1. **Register `AgentMesh\manifests\hydra-cockpit.mesh-manifest.yaml`** (`kind: SiblingManifest`) with an **http-get health probe** against the bridge `GET /api/health`, so **meshd observes the bridge** as a sibling. Validated against `mesh-manifest.schema.json` (fail-closed).
2. **File eights envelopes for every write** (§2.6) via the new `hydra.cockpit.audit` tool.

## 3.3 Discovered drift (flag for a separate governance fix — out of scope here)

`CONSTITUTION.md` Article III §6 still says **"two sanctioned operator writes"** (daemon restart, HITL acknowledgement). However, the **C3 amendment** (`tool-permission-matrix.md`, 2026-06-05; operator-approved campaign `1d48bb4d-2248-470a-82f2-7713c71aa55e`) sanctioned a **third** mesh-console write: **`mesh.hitl.resolve`**. The code already reflects three (`AgentMesh\web\server\write-whitelist.ts` freezes a 3-tool set — `mesh.supervisor.restart`, `mesh.hitl.ack`, `mesh.hitl.resolve` — though its own doc-comments lines 8–9 / 63 / 68 still say "two"/"exactly two").

> **This is a pre-existing AgentMesh constitution-vs-code drift, independent of the cockpit. It is flagged here for a separate governance fix and is explicitly out of scope for the hydra-cockpit initiative.** The cockpit's no-amendment argument (§3.1) does not depend on resolving it: the cockpit is a different envelope either way.

---

# 4. Resolved gaps (encoded into the build chunks)

### Gap (a) — `hydra run` cannot accept a pre-allocated workflow id

`hydra_core\cli.py` `_cmd_run` (line ~245) does `workflow_id = uuid4()` with **no `--workflow-id` argument** (confirmed: the `hydra run` parser exposes only `--squad / --live / --verbose / --no-checkpoint`). The bridge needs to **pre-allocate the id it returns** to the UI for immediate `#/workflow/:id` navigation.

**Resolution → chunk C2:** add a `--workflow-id <uuid>` flag to `_cmd_run`. When supplied (and well-formed), `_cmd_run` uses it instead of minting a fresh uuid; the bridge generates the id, passes it on the validated argv, and returns it instantly. (Rejected alternative: log-tail parsing the minted id — racy and fragile.)

### Gap (b) — replay is skill-only today

Replay exists only as a **model-driven skill** (`Hydra\.claude\commands\hydra-replay.md` → `/hydra:replay`); there is **no deterministic `replay` subcommand** in `cli.py`. The bridge must launch replay as a fixed-argv detached subprocess, which requires a CLI surface.

**Resolution → chunk C6:** first add a **deterministic CLI subcommand**:

```
hydra replay <workflow_id> [--from-phase <phase>] [--swap-model <id>] [--live]
```

then wire the bridge to it (fire-and-attach, venom-gated when `--live`).

---

# 5. Build staging — C0 … C8

## 5.1 Dependency graph

```
            C0 scaffold  (BLOCKING)
                  |
            C1 bridge core + read path
                  |
      +-----------+-----------+
      |           |           |
  C2 launch   C3 gate     C4 SSE
   contract    writes    streaming
      |   \     /   \      /
      |    \   /     \    /
      |     \ /       \  /
      |  C5 eights     C6 replay
      |  audit +       + memory tag
      |  mesh-manifest (needs C2+C4)
      |  (needs C2+C3)
      |
  C7 React SPA  (against FROZEN C1 contracts — can start in parallel)
      |
  C8 mesh-console "Open in Cockpit" deep-link  (AgentMesh repo — LAST)
```

## 5.2 Chunk definitions

| Chunk | Scope | Depends on | Notes |
|---|---|---|---|
| **C0** | Scaffold: `web/package.json` (vite **5185**), tsconfigs, vitest, dir layout | — | **Blocking** — everything waits on it |
| **C1** | Bridge core + read path: 5 invariants, `hydra_memory` client, session/health/workflows/squads/memory routes, read whitelist | C0 | **Freezes the read contracts** C7 builds against |
| **C2** | Launch contract: `launch.ts`, `POST /api/launch`, **add `--workflow-id` to `_cmd_run`** (gap a), dry-run default, fire-and-attach | C1 | ∥ C3, C4 |
| **C3** | Gate writes: write-whitelist (8), `hydra_control` client, `POST /api/resume` (×5 actions), confirm-nonce, typed challenge, venom/Cerberus for force-dispatch | C1 | ∥ C2, C4. Validation alphabet identical to `hydra_control` |
| **C4** | SSE streaming: checkpoint-mtime reader, trace tail (`fs.watch` + cursor), event schema (`trace/state/gate/ping/done`), poll fallback | C1 | ∥ C2, C3 |
| **C5** | eights audit (**new `hydra.cockpit.audit` tool** in `hydra_control`, spool-safe) + register `hydra-cockpit.mesh-manifest.yaml` | **C2 + C3** | One eights envelope per write |
| **C6** | Replay (**add `hydra replay` CLI subcommand** — gap b) + memory `tag_memory` write | **C2 + C4** | Replay venom-gated if `--live` |
| **C7** | React SPA: all 7 views, 8-state screens, ConfirmDialog/typed-challenge, live hook (SSE→poll), WCAG 2.2 AA | C1 (frozen) | Can start in parallel against frozen C1 contracts (mock endpoints) |
| **C8** | Mesh-console **"Open in Cockpit"** deep-link button | C7 | **AgentMesh repo, last.** Uses §2.7 deep-link contract |

> **Stage 2 gate:** building C0–C8 requires a **separate operator approval** after this design is reviewed.

---

# 6. Reference files (mirror — do not reinvent)

| File | What to fork |
|---|---|
| `AgentMesh\web\server\index.ts` | bridge skeleton: port-probe, Host check, CSRF gate, read/write split, route table |
| `AgentMesh\web\server\mesh-client.ts` | stdio MCP child + single-flight call idioms (→ `hydra_memory` & `hydra_control` clients) |
| `AgentMesh\web\server\whitelist.ts` | read allowlist + forbidden-verb denylist |
| `AgentMesh\web\server\write-whitelist.ts` | frozen write set + `allowWriteTool()` closure (extend 3→8) |
| `AgentMesh\web\server\operator.ts` | `sessionToken` / `verifyToken` (timing-safe CSRF) |
| `AgentMesh\web\src\components\ConfirmDialog.tsx` | confirm + typed-challenge modal (focus trap, nonce field) |
| `AgentMesh\web\src\components\HitlView.tsx` | gate normalization; default highlighted-not-preselected; high-risk typed challenge |
| `AgentMesh\web\src\console\types.ts` | 8-state union; HITL/gate payload types |
| `AgentMesh\web\src\console\useConsoleLive.ts` | polling hook cadence + visibility/stop logic (→ SSE-first live hook) |
| `Hydra\mcp_servers\hydra_control\server.py` | detached-launch pattern + **validation regex alphabet (keep identical bridge↔Python)** |
| `Hydra\hydra_core\cli.py` | `_cmd_run` (gap a); resume subcommand semantics |
| `Hydra\hydra_core\supervisor.py` | phase enum / `interrupt_before` boundaries (phase-machine viz) |
| `Hydra\hydra_core\state.py` | `HydraState`, `BudgetLedger` (budget ticker bands) |
| `Hydra\hydra_core\telemetry.py` | `trace.jsonl` format = the **SSE contract** (`emit`: `ts/kind/workflow_id/...`) |

---

## Appendix A — Open questions, resolved

| # | Question (from plan) | Resolution |
|---|---|---|
| 1 | `hydra run` lacks `--workflow-id` | **Add the flag** (gap a, C2). Confirmed `_cmd_run` mints its own uuid. |
| 2 | Replay entrypoint | **Add `hydra replay` CLI subcommand** (gap b, C6) before wiring the bridge. Skill-only today. |
| 3 | eights filing from Node bridge | **Add `hydra.cockpit.audit` tool to `hydra_control`** (C5), spool-safe. |
| 4 | Article III §6 verbatim text | Confirmed verbatim (§3.1). No amendment needed; drift flagged separately (§3.3). |

## Appendix B — Verification (for review)

- **Stage 1 (this doc):** passes the campaign's cross-vendor judge gate (Codex mandated judge per AGENTS.md; Reflexion ×1 max); operator reviews this document before any Stage 2 dispatch.
- **Stage 2 (per chunk):** vitest bridge tests — CSRF timing-safe, Host rejection, whitelist closure, argv has no shell metacharacters, SSE framing, validation parity with `hydra_control`; UI tests (jsdom); end-to-end dry-run launch → watch → gate → all-5 resume on a throwaway workflow → verify eights envelopes via `eights_audit_trace` → verify the mesh deep-link opens the right gate; browser validation via claude-in-chrome (P7 pattern).
