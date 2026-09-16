# RCA — TheEights record outcomes, the spool, and "check the return"

**Status:** diagnosis complete — **passed cross-vendor review** (codex
`gpt-5.6-terra`, four passes). Reviews 1–3 returned **revise**; review 4 returned
**pass** with every dimension at the maximum, and independently reproduced the
measurements. Every point raised was verified against source before being folded
in. Findings added after review 1 are marked **NEW IN R2**; corrections after
reviews 2 and 3 are marked **R3**. Remediation is not implemented; §6 is the
recommended path.
**Date:** 2026-09-16. **Observed:** Hydra `feat/planning-phase`, TheEights
`fable-audit-2`, operator spool `~/.hydra/eights-pending{,-dead}`, TheEights
state `~/.eights/state.db` (read-only, `mode=ro`, `query_only`).

**Trigger.** The planning-phase plan (§10a, gate 8) asserted a *live* bug —
Hydra envelope types that cannot reach TheEights, with the rejection discarded
unchecked at `mcp_servers/hydra_control/server.py` — and prescribed "fix the
enum, and check the return". The operator asked for a root-cause analysis before
deciding.

Labels: **MEASURED** (observed on this machine) · **READ** (established from
source) · **INFERRED** (follows from measured/read facts, not directly observed).

---

## 1. Summary

The prescription targets the wrong defect, and the defects it misses are larger.

1. **The enum gap is latent.** Zero of ~1,375 spool/dead-letter records carry the
   five affected types. The fix is still correct and cheap.
2. **"Check the return" is a no-op** at every direct call site: the attestor
   returns the same `None` for a rejection, a timeout, a non-live run and a bug.
3. **The spool mixes payloads that must never be replayed (`stub`, provably
   unsent) with payloads of unknown outcome, and replay treats them identically.**
   The doctor banner recommends an unfiltered bulk replay of the dead-letter
   queue into a live TheEights.
4. **Replay is not idempotent on the daemon.** A replayed HITL request files a
   *new* ticket every time; a replayed envelope record errors rather than
   deduplicating — contradicting the attestor's own docstring. **NEW IN R2**
5. **The gateway proxy reports every backend tool error as success.**
6. **No recorded Hydra envelope has a linked semantic memory row** — 0 of 1,989
   by `memory_id`, and 0 of 14,704 memories reference any recorded envelope id at
   all. The indexing step of envelope recording has not run for Hydra envelopes,
   because Hydra's attestor strips the envelope before sending it. Other
   Hydra-related memories do exist, written by other routes. **NEW IN R2, scope
   corrected in R3**

---

## 2. Findings

### F1 · Outcomes collapse to `None` — READ, reproduced

`EightsAttestor._call` (`hydra_core/eights/attestation.py:261`):

| Outcome | Spooled? | Returns |
|---|---|---|
| attestor disabled / no dispatcher | yes, `eights_disabled_or_no_dispatcher` | `None` |
| `_dispatch_call` raised | yes, `exception:<Type>` | `None` |
| advisory constitution rejection | no | `None` |
| non-success result — covers daemon rejection, **timeout**, transport error, and `stub` | yes, `_failure_reason(result)` | `None` |
| success | — | result dict |

`_failure_reason` (`:228`) computes a discriminator and hands it only to the
spool. `envelope_record` (`:502`) returns `_call`'s value unchanged.

Reproduced against a temporary spool (`HYDRA_EIGHTS_SPOOL`/`_DEAD_LETTER`; real
spool untouched): a `_NullDispatcher`, a dispatcher object without `call_mcp`,
and a dispatcher returning a rejection each gave `envelope_record(...) is None`
and were spooled as `stub`, `exception:AttributeError` and `failed:...`.

**Qualification (from review, verified).** One caller does get a signal: the
cockpit audit (`server.py:247-250`) compares `attestor.pending_count()` before
and after, and reports `spooled`. That distinguishes *spooled vs not* — it does
not say *why*, and it is racy against concurrent spool writers. So "check the
return" is a no-op at the direct `envelope_record` call sites; the cockpit
caller has a coarse, unreasoned signal.

### F2 · `failed` does not mean "rejected" — READ

`MCPStdioDispatcher.call_mcp` maps raw MCP `isError` to `status: "failed"`
(`hydra_core/dispatcher.py:749`, `:876`) — so a daemon-side Zod rejection is
correctly a failure on the direct path, not a silent success. **But the same
status is used for a timeout** (`:852-863`, `"timeout": True`) and for an
exception raised by `call_tool` after connect. A timeout is an *unknown*
outcome: the daemon may have committed the write. The spool reason string
`failed:...` therefore cannot be used to decide that a payload is permanently
unreplayable. *(This invalidates revision 1's remediation D — see §5.)*

### F3 · The gateway reports tool errors as success — READ

`mcp_servers/hydra_gateway/server.py` `_extract_result` (`:662-677`) always
returns `{"status": "done", ...}` and never inspects `isError`; the module has
no `isError` reference at all. A backend tool error proxied through the gateway
— a Zod rejection, `unknown tool`, a readiness refusal, a deadline — arrives
with `status: "done"` and the error inside `result`. Anything keying on
`status` treats it as success.
**Scope.** The supervisor's attestor dispatches directly through
`MCPStdioDispatcher`, which is correct (F2); it is not affected. Consumers going
through the gateway — host sessions, subagents, judges reading `status` — are.
The full consumer set was not enumerated.

### F4 · The spool admits non-replayable payloads — MEASURED

`_maybe_spool` spools every non-success outcome. Two reasons are never
transport-ambiguous:

- **`stub`** — a result with `status: "stub"`. `_NullDispatcher`
  (`hydra_core/cli.py:~99`) returns that from every call on non-live CLI paths.
  A stub call never reached a daemon.
- **`exception:AttributeError`** — `_dispatch_call` raised `AttributeError`.
  The observed shape is consistent with a dispatcher object lacking
  `call_mcp` (as the offline test doubles used this week do); that shape was
  reproduced. The suite is hermetic via `tests/conftest.py`; processes outside
  it write to the real `~/.hydra`.

**Provenance, corrected in R2.** A spool record stores `tool, args, reason,
attempts, spooled_at, workflow_id` — no caller, process, or traceback. A reason
establishes the *result shape*, not the originating process. The attributions
above are the reproduced producers of those shapes, not proof of origin.

**The two reasons are not equally certain, corrected in R3.**
- `stub` **is provably unsent.** Its only producer anywhere is `_NullDispatcher`
  (`hydra_core/cli.py:102-108`, four methods, all returning immediately with no
  I/O), and TheEights has no producer of a `stub` status. A `stub` record cannot
  correspond to a write the daemon received. — READ
- `exception:AttributeError` **is not.** It comes from the broad `except` around
  the entire `_dispatch_call` → `call_mcp` invocation
  (`attestation.py:266-275`). An `AttributeError` raised inside a dispatcher
  *after* it issued the request produces an identical record to a dispatcher
  with no `call_mcp`. The persisted record cannot prove the error preceded the
  send, so these entries are **unknown outcomes**, not junk. Revision 2 called
  them provably unsent; that was wrong.

| Reason | Pending | Dead-letter |
|---|---|---|
| `stub` | 45–48 (varies as live replay drains); newest 2026-09-14 08:23 | 248 (136 attest, 75 envelope.record, 37 hitl.request) |
| `exception:AttributeError` | 2 (1 attest, 1 hitl.request), 2026-09-13 21:51 | 488 (360 attest, 98 hitl.request, 30 envelope.record) |

Pending totals moved between measurements (80 → 77) because replay drained
entries; dead-letter counts reproduce exactly (1,298; 736 of these two reasons;
135 `hitl.request`).

**Correction to earlier in this session:** these were described as a stopped,
historical backlog. New entries of both reasons appeared on 2026-09-13/14, so
their producers are still active — though, per above, not attributable to a
specific process.

### F5 · Replay does not filter, and runs at scale — READ + MEASURED

`PendingSpool.replay` (`pending_spool.py:237`) and
`EightsAttestor.replay_pending._send` (`attestation.py:380`) retry every entry
through the **current** dispatcher; nothing reads `reason`. `node_intake`
drains once per workflow (`supervisor.py:~432`, `HYDRA_EIGHTS_REPLAY_MAX_REPLAYS`
default 1); so do `cli.py:1240`, `:1831`, `:4806`.

**MEASURED:** `Hydra/.hydra/*/trace.jsonl` contains 14,014
`supervisor.eights_replay` events whose `sent` fields sum to **8,296**.
Caveats: `sent` counts calls where `send_fn` returned success, so a stub
dispatcher contributes nothing, but a test double returning success would
inflate it; project-local traces may include non-production runs.

**INFERRED, not attributable:** a `stub`-origin payload has never reached a
daemon, so the first live replay would record it for real — a constitution
attestation, HITL ticket or envelope record for work that never ran live. Once
sent it is indistinguishable, since `reason` is not transmitted. The number of
such sends, if any, cannot be recovered.
Precedent that the class is real: `_prune_spooled_hitl_requests` (C3) already
guards one case (tickets for since-resolved gates).

### F6 · The recommended remediation triggers the hazard — READ, review-confirmed

`hydra doctor` (and the SessionStart banner) prints `run hydra eights-drain
--replay-dead-letter` above a dead-letter threshold. That path
(`cli.py:~4786`) calls `requeue_dead_letters` (`pending_spool.py:296`), which
moves **all** dead letters back with attempts reset (`:316`), disables the age
check, and replays through a real `MCPStdioDispatcher`. Today that would re-send
736 payloads: 248 `stub` entries that provably never reached a daemon (recording
them would create records for work that never ran live) and 488
`exception:AttributeError` entries of unknown outcome (replaying those risks
duplicates under F7). 135 of the 736 are HITL requests.

**Correction to earlier in this session:** clearing the backlog was described as
"a `hydra eights-drain --replay-dead-letter` decision for the operator". Without
first separating these entries, that advice is hazardous.

### F7 · Replay is not idempotent on the daemon — READ · NEW IN R2

- **HITL requests duplicate.** `GovernanceState.hitlRequest`
  (`TheEights/daemon/src/engines/governance-state.ts:222`) mints a fresh
  `request_id = hitl_${nanoid()}` on every call and does a plain `INSERT`;
  `hitl_queue.request_id` is the primary key (`stores/sqlite.ts:360`), so nothing
  deduplicates. Every replay of the same request — including a timeout whose
  original write actually committed — files **another pending ticket**.
- **Envelope records error instead of deduplicating.** `HydraEngine.record`
  (`engines/hydra.ts:~44-80`) does a plain `INSERT` keyed on
  `hydra_envelopes.envelope_id PRIMARY KEY` (`stores/sqlite.ts:321`). Replaying an
  already-committed envelope violates the key, throws, returns `isError`, is
  spooled, and retries until dead-lettered. The attestor docstring's claim —
  "Idempotent — the daemon dedupes by envelope id" (`attestation.py:503`) — is
  **false**.
- `record` also calls `memory.add` **before** that insert, with **no idempotency
  key**. Each failing retry would therefore leave a duplicate memory row — except
  that, per F8, the memory write never happens for Hydra envelopes today. The
  amplification is latent, not realised.

**Consequence for remediation:** "keep unknown outcomes retryable" is unsafe for
`hitl.request` until the daemon is idempotent. Retry safety is per-tool.

**Idempotency needs a stable key sent by the client, not only accepted by the
daemon — R3, established after review 3.** Neither side has a key today:
TheEights' `HitlRequestArgs` is `{envelope, run_id?, kind, payload}`
(`TheEights/daemon/src/mcp/governance.ts:47`), and Hydra's
`EightsAttestor.hitl_request` (`attestation.py:~707`) sends `run_id`, `kind` and a
payload. A daemon that merely *accepts* a key does not deduplicate a request whose
first call committed and timed out if the replay carries no key or a different
one.

The obvious candidate is the payload's `hitl_id`, which is
`str(hitl_envelope.get("id", ""))` — the HITL envelope's own id, and therefore
identical between an original call and its spooled replay, since the spool stores
arguments verbatim. MEASURED across all 163 spooled and dead-lettered
`hitl.request` records:

- **43 (26%) have an empty `hitl_id`.** The `""` fallback fires in practice. A key
  derived naively from `hitl_id` would collapse all of them into one ticket, or
  silently skip deduplication for them.
- The 120 non-empty ids are **all distinct**, so `hitl_id` is a sound key where
  present.
- `(workflow_id, gate_node)` is **not** a sound key: 1 of 161 such pairs already
  maps to more than one distinct request, which it would wrongly merge.
- INFERRED: the empty ids come from HITL payloads built as raw dicts without an
  `id`, rather than as `HITLRequest` models — the plan (C-g) records six such
  producers.

Envelope records and their memory rows already have a natural stable key, the
envelope id; `memory.add` supports an idempotency key when one is supplied
(`TheEights/daemon/src/engines/memory.ts:81-98`).

### F8 · No recorded Hydra envelope is linked to a semantic memory — MEASURED · NEW IN R2, scope corrected in R3

In `~/.eights/state.db`: **1,989** `hydra_envelopes` rows, **0** with a non-null
`memory_id` (DECISION_RECORD 1,886; COCKPIT_WRITE 47; DEV_TASK 36; others ≤ 6).

**Scope, established in R3 after review 2 found Hydra-looking memories.** Of
14,704 memories, a broad match (`hydra` in provenance or scopes, or
`DECISION_RECORD` / "hydra envelope" in content) finds 27. They were written by
other routes: `pp-bridge` (6 episodic), `hydra-supervisor` (5 semantic, 6
episodic — direct memory writes in June/July with URIs such as ADR paths and
`goal:` keys, or none), `execsuite-bridge` (3), `pp-daemon` (2 meta), and one
model-authored row. Linkage was then tested directly: **none of the 14,704
memories references any of the 1,989 recorded envelope ids** in provenance or
scopes, and none carries the `hydra-envelope://` URI that `HydraEngine.record`
stamps. So the finding is specifically that *envelope recording* has produced no
linked memory; Hydra-related memory does exist through other routes. A memory
whose *content* paraphrases an envelope without citing its id cannot be excluded
by this method.

Cause, READ and confirmed against stored payloads:
- `extractSummary` (`engines/hydra.ts`) accepts only `objective`, `summary`,
  `description` or `goal`; if none is a non-empty string it returns `null` and
  `memory.add` is **never called** (not rejected — skipped).
- Hydra's `EightsAttestor.envelope_record` (`attestation.py:502-515`) sends a
  whitelist of six fields: `id, type, workflow_id, origin_squad, target_squad,
  parent_id`.
- **MEASURED:** 1,880 of 1,989 stored payloads have exactly that key set (plus
  schema-defaulted `context_refs`), and none of the five most common key sets
  contains any of the four probed fields. The 20 richer records carry `decision`
  and `rationale`, which `extractSummary` does not probe.

So the indexing step of TheEights' "semantically-indexed record" has not run for
Hydra envelopes: memory search surfaces nothing *derived from envelope
recording*, though it can surface Hydra-related memories written by other
routes. This also bounds a claim made during X3: a PLAN gets a
memory row when `HydraEngine.record` receives the full envelope, as in its unit
test, but **not** through Hydra's attestor as it stands — and nothing in Hydra
records a PLAN to TheEights today anyway.

### F9 · The plan's original claims, re-examined

| Plan claim | Finding |
|---|---|
| Hydra types "cannot reach TheEights" | True of the closed 11-member enum for PLAN, SUPPORT_TICKET, PORTABLE_CONTEXT, VOC_REPORT, JUDGE_VERDICT. |
| It is **live** | Not observed: 0 occurrences across spool and dead-letter. Reachable: the supervisor records whatever a squad produced (`produced.model_dump`, `supervisor.py:2235/2551/2664`). |
| Rejection discarded at `server.py:~1281` | True in code; that verb returns `{"ok": true}` regardless. Its declared caller, AgentSmith `HydraBridge.envelopeRecord`, is defined and not called anywhere in AgentSmith. |
| Fix: "check the return" | No-op at direct call sites (F1). |

Observed, out of scope: 30 pending entries fail `not_ready: audit verification in
progress`, consistent with the known TheEights audit-ledger bloat.

---

## 3. What is not known

1. Whether any of the 8,296 replayed sends were `stub`- or test-double-origin.
2. The originating process of any `stub` or `exception:AttributeError` record.
3. Whether duplicate HITL tickets already exist in TheEights from replays of
   committed-but-timed-out requests. Measurable read-only, not yet measured.
4. The full set of gateway consumers that key on `status`.
5. For each `exception:*` record, whether the error occurred before or after the
   request reached the daemon. Not recoverable from existing records (R3).

---

## 4. Root causes

| # | Root cause | Findings |
|---|---|---|
| RC1 | Outcome information is flattened to strings/`None` before the component that must act on it — dispatcher → attestor → spool → replay. | F1, F2, F4, F5 |
| RC2 | Writes to TheEights are not idempotent, while the client assumes they are. | F7 |
| RC3 | The spool/replay layer has no notion of *replayability*; operator tooling trusts it. | F4, F5, F6 |
| RC4 | Gateway flattening discards the MCP error bit. | F3 |
| RC5 | Hydra's attestor and TheEights disagree on the envelope contract: the client whitelists routing fields, the daemon indexes content fields. | F8 |

---

## 5. Candidate paths, revised

Revision 1 proposed **D — classify at spool time from the reason** and ranked it
first. Review showed, and source confirms, that a reason string cannot separate
a rejection from a timeout (F2); D as written would drop payloads whose outcome
was genuinely unknown. It is replaced by D′.

| # | Path | Assessment |
|---|---|---|
| B | "Check the return" as prescribed | No-op. **Decline.** |
| C | Change `_call`'s public return contract for all callers | Wide hot-path blast radius; callers are fire-and-forget by design. **Decline.** |
| F | **Containment, narrowed in R3.** (a) Quarantine only `stub` entries, from both queues, into a directory no drain reads — the one reason proven unsent. (b) Change `eights-drain --replay-dead-letter` so it never replays `stub`, and replays `exception:*` only when the operator explicitly opts in, instead of bulk re-queuing everything. (c) Change the doctor message so it no longer recommends an unfiltered bulk replay. Deletes nothing. | Closes the F6 trigger. Does **not** alter normal intake replay of pending `exception:*` entries, which stay retryable as today. |
| E′ | **Replay-time filter restricted to `stub`**, applied on **every** drain path. **Narrowed in R3:** revision 2 also filtered "dispatcher-shape exceptions raised before any send", but no existing record can prove that (F4), so filtering them would stop retrying unknown writes. | Safe under uncertainty: touches only an outcome with no ambiguity. |
| I | **End-to-end idempotency — both sides, specified in R3.** *Daemon (TheEights):* `HitlRequestArgs` gains an idempotency key and `hitlRequest` returns the existing row on repeat; `record` checks for an existing `envelope_id` and returns success without re-writing, and passes the envelope id as `memory.add`'s idempotency key. *Client (Hydra):* `hitl_request` **must transmit a stable key** — the HITL envelope id — on the original call so every replay carries the same one. **Uniqueness contract:** the key must be non-empty and identify one logical request. A HITL with no id must never be sent or spooled keyless: either the producer is fixed to build a real `HITLRequest` (the 43 empty-id records show raw-dict producers exist), or a deterministic key is derived from the request's full identity — never from `(workflow_id, gate_node)` alone, which measurably collides. Existing keyless spool records stay un-deduplicable and are held for operator ruling. | Prerequisite for any retry of an unknown outcome to be safe. Daemon support alone is **not** sufficient. |
| D′ | **Typed outcome from the dispatcher.** The dispatcher, which alone sees raw `isError`, the timeout, and whether a send was attempted, attaches a structured outcome class (`rejected` / `unknown` / `not_sent` / `not_live`) and a pre-send / attempted-send marker that is **persisted into the spool record**. The attestor spools only `unknown` and `not_sent`; logs `rejected` distinctly; never spools `not_live`. Public `Optional[dict]` unchanged. | Correct long-term fix for RC1, and the only way future `exception:*` records become classifiable (§3.5). Depends on I for `unknown` retries to be safe. |
| H | **Gateway preserves the MCP error bit** (`status: "failed"` when `isError`). | Fixes RC4. Broad consumer blast radius; needs its own consumer audit. |
| J | **Resolve the envelope contract (RC5):** either the attestor sends a summarisable field, or `extractSummary` also probes `decision`/`rationale`. Needs a decision on which side owns it. | Restores precedent retrieval. Interacts with I: turning indexing on while replay is non-idempotent would activate the latent duplicate-memory amplification (F7). |

## 6. Proposed path forward (for cross-vendor re-review)

**Now, operationally:** do not run `hydra eights-drain --replay-dead-letter`.

**Sequencing, and why this order:**

1. **F + E′** — containment and a replay filter that is safe under uncertainty.
   Narrowed in R3 to the one provably-unsent reason, `stub`, plus removing the
   bulk replay recommendation. The 488 dead-lettered and 2 pending
   `exception:AttributeError` entries are **not** quarantined: their outcome is
   unknown, so they are held out of bulk replay pending an operator ruling rather
   than treated as junk.
2. **I** — end-to-end idempotency: the TheEights contract accepts a key, **and**
   Hydra sends a stable, non-empty one on every call, with raw-dict HITL producers
   fixed so no request goes out keyless. Daemon support without the client key
   does not make retries safe. Required before D′ permits unknown outcomes to
   retry, and before J.
3. **D′** — typed outcomes, once I makes retry of `unknown` safe.
4. **J** — semantic indexing, strictly after I; otherwise enabling memory writes
   activates the duplicate-memory amplification.
5. **H** — gateway error bit, as its own run with a consumer audit.

**X3** proceeds: the enum change is correct. Its commit message must describe it
as closing a *latent* gap, and must not claim PLAN gets a memory row in
production (F8).

**Scope.** None of 1–5 belongs to the planning-phase feature. Each is a separate
governed run. B and C are declined.

**Open forensic question for the operator:** whether duplicate HITL tickets or
non-live records already exist in TheEights (§3.1, §3.3).
