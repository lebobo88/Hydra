"""hydra_core.fleet — Parallel cross-repo dispatch primitive (WS8 SLICE 1 + 2).

Exposes ``dispatch_fleet``: a bounded-concurrency fan-out that calls
``execute_squad`` in parallel across a set of tasks whose target repos are
DISTINCT.  The key invariants this module enforces are:

1. **Same-repo guard** — two tasks that share a ``target_repo_id`` (including
   two tasks both targeting ``None``) would collide on the per-project
   ``.harness/.lock``.  Duplicates are rejected *before* fan-out.
2. **No shared-state mutation inside workers** — ``execute_squad`` / ``_via_mcp``
   normally appends to ``state.open_pp_runs`` from inside the call.  Workers
   receive a *per-call collector list* instead; the fleet merges those lists into
   ``state.open_pp_runs`` in the **main thread** after the join.
3. **Failure isolation** — one worker's exception does NOT crash the fleet or
   cancel other workers.  A failing task gets a ``SquadResult(status="failed")``.
4. **Deterministic ordering** — results are returned in the same order as the
   input ``tasks``, regardless of completion order.  The collection loop
   uses ``concurrent.futures.as_completed`` so cancellation fires on the
   FIRST surfaced/trigger result regardless of submit order; results are
   stored by their INPUT INDEX (``futures[future] = idx`` map) so the
   final list is always in submission order.
5. **Bounded concurrency** — ``max_workers`` is clamped to ``[1, FLEET_MAX_CAP]``.
6. **Per-worker dispatcher** — each worker receives a *fresh* ``Dispatcher``
   instance from ``dispatcher_factory()``.  ``MCPStdioDispatcher`` owns a single
   asyncio event loop; sharing it across threads races on
   ``loop.run_until_complete()`` (``RuntimeError: This event loop is already
   running``).  Giving each worker its own factory-constructed dispatcher means
   each thread has its own loop/session, so concurrent pp calls on DISTINCT repos
   do not serialise behind one lock and do not corrupt shared asyncio state.
   Thread-safety contract: the factory MUST return an independent object with no
   shared mutable asyncio state.  ``MCPStdioDispatcher.__init__`` satisfies this
   because it creates a fresh ``_loop=None`` and an empty ``_sessions`` cache on
   every construction.
7. **Engineering/mcp-only eligibility** — only tasks whose pack has
   ``entrypoint == "mcp"`` are eligible for the fleet.  Non-mcp packs
   (``agent-impersonation``, ``claude-skill``, ``subprocess``, ``stub``) are NOT
   eligible because:
     - They call ``_record_mcp_failure`` → ``state.error_counters`` (unsafe
       read-modify-write from a worker thread).
     - They ignore ``target_repo_id`` (use a configured ``project_path`` from
       ``squad.yaml``), so claiming distinct ``target_repo_id`` values for them
       does not prevent ``.harness/.lock`` collisions.
   Non-eligible tasks submitted to the fleet are rejected with a clear rationale
   and placed back on the sequential path by the caller (node_dispatch).
8. **Cancellation propagation (WS8 SLICE 2)** — ``dispatch_fleet`` accepts two
   optional arguments that control early cancellation:

   - ``cancel_event: threading.Event | None`` — an externally-visible flag that
     any party (caller or the collection loop) can set to signal "stop starting
     new work".  Workers check this flag AT ENTRY (before calling ``execute_squad``
     / the expensive pp dispatch): if set, the worker returns a
     ``SquadResult(status="cancelled")`` immediately without dispatching.
     Workers that are already past the entry-check complete naturally — threads
     cannot be force-killed; this is expected and documented.  The OUTPUT
     STRUCTURE is always deterministic: every index ``[0..n-1]`` holds a result
     (cancelled-or-not); the shape given the same completion-trigger is stable.

   - ``should_cancel: Callable[[SquadResult], bool] | None`` — called from the
     **main thread** collection loop for each result that comes back.  If it
     returns ``True``, ``cancel_event`` is set and ``future.cancel()`` is called
     on every future that has not yet started.  A cancelled future's slot is
     filled with a ``SquadResult(status="cancelled")``.  The default ``None``
     means "never auto-cancel" — all tasks run (previous behaviour; no
     regression).

   Thread-safety contract: ``cancel_event`` is read-only from worker threads
   (``.is_set()`` only).  It is set exclusively from the single-threaded
   collection loop (or by the caller before ``dispatch_fleet`` is invoked).
   Workers NEVER set it.  This satisfies the slice-1 "no shared-state mutation
   inside workers" invariant.

Deferred to later slices (NOT in scope here):
  - Per-task budget isolation / partial-rollback
  - Campaign wiring (chaining fleet dispatches across phases)
"""
from __future__ import annotations

import threading
from concurrent.futures import Future, ThreadPoolExecutor, as_completed
from typing import Any, Callable

from .squad_loader import SquadPack
from .squad_node import Dispatcher, SquadResult, execute_squad
from .state import HydraState, TaskState

# Hard cap on fleet concurrency.  Even if the caller asks for more workers,
# we clamp to this value — distinct repos share the same machine's I/O, so
# unbounded concurrency hurts rather than helps.
FLEET_MAX_CAP: int = 8

# Module-level default when state.fleet_max_concurrency is not set.
FLEET_DEFAULT_CONCURRENCY: int = 4

# Only packs with this entrypoint are fleet-eligible (see module docstring §7).
_FLEET_ELIGIBLE_ENTRYPOINT = "mcp"


def dispatch_fleet(
    state: HydraState,
    tasks: list[TaskState],
    dispatcher_factory: Callable[[], Dispatcher],
    *,
    build_payload: Callable[[TaskState], Any],
    packs: dict[str, SquadPack],
    max_concurrency: int | None = None,
    cancel_event: threading.Event | None = None,
    should_cancel: Callable[[SquadResult], bool] | None = None,
) -> tuple[list[SquadResult], list[Any]]:
    """Fan out *mcp-only* ``tasks`` in parallel, each in its own thread.

    Parameters
    ----------
    state:
        Shared workflow state.  Workers MUST NOT write to it; only the main
        thread merges collected ``open_pp_runs`` entries after the join.
    tasks:
        Ordered list of ``TaskState`` objects to dispatch.  The returned
        ``list[SquadResult]`` is in the SAME order — ``results[i]``
        corresponds to ``tasks[i]``.
    dispatcher_factory:
        Zero-argument callable that returns a *fresh*, thread-local
        ``Dispatcher`` instance.  Called exactly once per worker thread.
        ``MCPStdioDispatcher`` is safe to construct per-thread because its
        ``__init__`` creates a new asyncio event loop and empty session cache
        on each invocation — no shared asyncio state.
    build_payload:
        Callable that takes a ``TaskState`` and returns the ``HydraEnvelope``
        (e.g. a ``CSuiteDecisionPacket``) to pass to ``execute_squad``.
        Called EXACTLY ONCE per task (stored result is reused for guard + submit).
    packs:
        Mapping from squad slug to ``SquadPack``.  Tasks whose squad has no
        pack entry, or whose pack is not ``entrypoint="mcp"``, get an immediate
        ``failed`` result.
    max_concurrency:
        Number of parallel workers.  Defaults to
        ``state.fleet_max_concurrency`` (field added in SLICE 1) which itself
        defaults to ``FLEET_DEFAULT_CONCURRENCY``.  Clamped to
        ``[1, FLEET_MAX_CAP]``.
    cancel_event:
        Optional ``threading.Event`` used to propagate cancellation.  If not
        provided, a fresh event is created internally (caller can pass one to
        observe or trigger cancellation externally).  Workers check
        ``cancel_event.is_set()`` AT ENTRY before calling ``execute_squad``.
        If set, the worker returns immediately with
        ``SquadResult(status="cancelled")`` without dispatching.
        In-flight workers that are already past the entry-check complete
        naturally (threads cannot be force-killed; this is intentional and
        documented).
    should_cancel:
        Optional callable ``(result: SquadResult) -> bool`` evaluated in the
        **main-thread** collection loop for each future that completes.  If it
        returns ``True``, ``cancel_event`` is set and ``future.cancel()`` is
        called on every not-yet-started future.  Futures that return ``True``
        from ``.cancel()`` have their slot filled with a cancelled result.
        Default ``None`` means never auto-cancel (all tasks run; no regression
        from SLICE 1).

    Returns
    -------
    tuple[list[SquadResult], list[Any]]
        - ``results``: one entry per input task, in input order.  Rejected /
          failed tasks have ``SquadResult(status="failed")``.  Cancelled tasks
          have ``SquadResult(status="cancelled")``.
        - ``worker_trackers``: pre-sized list of length ``n``.  Slot ``i``
          holds the ``ToolUsageTracker`` (or ``None``) that was used by the
          worker that ran ``tasks[i]``.  The tracker is extracted from the
          worker's return value by the MAIN-THREAD collection loop — workers
          do NOT write to this list directly (fix 6 / "no worker mutates
          shared state" invariant is now literally true).  Slots for tasks
          rejected before fan-out (same-repo guard, non-mcp pack, payload-build
          error, cancelled) are ``None``.  Caller iterates in index order
          ``0..n-1`` for a deterministic, input-ordered merge.
    """
    if not tasks:
        return [], []

    # ------------------------------------------------------------------ #
    # Cancellation setup (WS8 SLICE 2).
    # The cancel_event is shared read-only to workers (.is_set() only).
    # It is set exclusively from the main-thread collection loop or by the
    # caller before dispatch_fleet is invoked.  Workers NEVER set it.
    # ------------------------------------------------------------------ #
    if cancel_event is None:
        cancel_event = threading.Event()

    # ------------------------------------------------------------------ #
    # Resolve concurrency cap
    # ------------------------------------------------------------------ #
    _max_conc = max_concurrency if max_concurrency is not None else getattr(
        state, "fleet_max_concurrency", FLEET_DEFAULT_CONCURRENCY
    )
    _max_conc = max(1, min(int(_max_conc), FLEET_MAX_CAP))

    n = len(tasks)
    results: list[SquadResult | None] = [None] * n
    # Pre-sized: each runnable worker writes its tracker to results[idx] and
    # worker_trackers[idx].  Assignment to a distinct slot is race-free (no
    # shared list.append from worker threads).  Rejected/not-run slots stay None.
    worker_trackers: list[Any] = [None] * n

    # ------------------------------------------------------------------ #
    # Build each payload EXACTLY ONCE (Fix 5).
    # A payload-build failure immediately fails that task; it never affects
    # the guard or submission of other tasks, and the post-join merge is
    # unaffected because no workers were started for it.
    # ------------------------------------------------------------------ #
    stored_payloads: list[Any | None] = [None] * n
    for idx, task in enumerate(tasks):
        try:
            stored_payloads[idx] = build_payload(task)
        except Exception as exc:
            results[idx] = SquadResult(
                envelopes=[],
                artifacts=[],
                status="failed",
                rationale=f"fleet: payload build error for task {idx}: {type(exc).__name__}: {exc}",
            )

    # ------------------------------------------------------------------ #
    # Per-task eligibility: pack present AND entrypoint == "mcp" (Fix 3+4).
    # Non-mcp tasks are rejected immediately; the caller keeps them on the
    # sequential path.
    # ------------------------------------------------------------------ #
    for idx, task in enumerate(tasks):
        if results[idx] is not None:
            continue  # already failed (payload build error)
        pack = packs.get(task.owner_squad)
        if pack is None:
            results[idx] = SquadResult(
                envelopes=[],
                artifacts=[],
                status="failed",
                rationale=f"fleet: no pack for squad {task.owner_squad!r}",
            )
            continue
        if pack.entrypoint != _FLEET_ELIGIBLE_ENTRYPOINT:
            results[idx] = SquadResult(
                envelopes=[],
                artifacts=[],
                status="failed",
                rationale=(
                    f"fleet: only mcp-entrypoint packs are fleet-eligible; "
                    f"squad {task.owner_squad!r} has entrypoint={pack.entrypoint!r} "
                    "(non-mcp packs may race on state.error_counters or ignore "
                    "target_repo_id — use the sequential path)"
                ),
            )

    # ------------------------------------------------------------------ #
    # Same-repo guard — build the set of tasks that may actually run.
    # Uses the STORED payloads (Fix 5) so we never call build_payload again.
    # Key: target_repo_id (None counts as its own "repo" — the workflow root).
    # At most ONE task per distinct key may proceed; extras are immediately
    # rejected with a clear rationale.
    # ------------------------------------------------------------------ #
    seen_repo_ids: dict[Any, int] = {}    # repo_id_key -> first-seen index
    runnable_indices: list[int] = []      # indices eligible for fan-out

    for idx, task in enumerate(tasks):
        if results[idx] is not None:
            continue  # already rejected above
        payload = stored_payloads[idx]
        repo_key = getattr(payload, "target_repo_id", None)

        if repo_key in seen_repo_ids:
            first_idx = seen_repo_ids[repo_key]
            reason = (
                f"fleet requires distinct target repos; "
                f"duplicate target_repo_id={repo_key!r} "
                f"(same-repo parallel would collide on .harness/.lock; "
                f"first occurrence at task index {first_idx})"
            )
            results[idx] = SquadResult(
                envelopes=[],
                artifacts=[],
                status="failed",
                rationale=reason,
            )
        else:
            seen_repo_ids[repo_key] = idx
            runnable_indices.append(idx)

    if not runnable_indices:
        # All tasks rejected or pack-missing — nothing to dispatch.
        return results, worker_trackers  # type: ignore[return-value]

    # ------------------------------------------------------------------ #
    # Fan out via bounded ThreadPoolExecutor.
    # Per-worker dispatcher (Fix 2): each worker calls dispatcher_factory()
    # to get its OWN Dispatcher with its OWN asyncio event loop.  This
    # prevents run_until_complete() races on a shared loop.
    # Each worker also gets its own collector list (Fix: no shared-state write).
    # ------------------------------------------------------------------ #
    collectors: dict[int, list[dict[str, str]]] = {idx: [] for idx in runnable_indices}
    # futures maps Future -> input index.  Used to look up idx after as_completed.
    futures: dict[Future[tuple[SquadResult, Any]], int] = {}

    with ThreadPoolExecutor(max_workers=min(_max_conc, len(runnable_indices))) as pool:
        for idx in runnable_indices:
            task = tasks[idx]
            pack = packs[task.owner_squad]
            payload = stored_payloads[idx]   # pre-built; NOT re-calling build_payload
            collector = collectors[idx]

            # Fix 6: workers RETURN (SquadResult, tracker) tuple; the main-
            # thread collection loop assigns worker_trackers[idx] from the
            # return value.  Workers no longer write to worker_trackers at all
            # — "no worker mutates shared state" invariant is now literally true.
            future = pool.submit(
                _fleet_worker,
                idx,
                pack,
                payload,
                dispatcher_factory,   # factory; worker calls it to get its own dispatcher
                collector,
                cancel_event,         # read-only from workers (.is_set() only)
            )
            futures[future] = idx

        # ------------------------------------------------------------------ #
        # Collect results — completion-driven via as_completed (Fix 1).
        #
        # Using as_completed() means the loop reacts to whichever future
        # finishes FIRST, regardless of submission order.  A fast surfaced
        # result at index N triggers cancellation before slow index 0 would
        # even be looked at under a sequential dict-order iteration.
        #
        # Result slots are written by INPUT INDEX (futures[future] = idx),
        # so the final results list is always in submission (input) order.
        #
        # Cancellation (WS8 SLICE 2): after each completion, if
        # should_cancel(result) is True: set cancel_event + future.cancel()
        # on every future whose slot is still empty.  Futures that accept
        # .cancel() (not yet started) get an immediate cancelled result so
        # the outer loop never calls .result() on them (CancelledError guard).
        # In-flight workers past their entry-check complete naturally.
        # ------------------------------------------------------------------ #
        for future in as_completed(futures):
            idx = futures[future]
            # Skip if already filled by a prior cancel() sweep.
            if results[idx] is not None:
                continue
            try:
                squad_result, tracker = future.result()
            except Exception as exc:
                # CancelledError: a successful .cancel() made this future
                # unreachable via .result().  Any other exception is a bug
                # in _fleet_worker (which normally swallows all exceptions).
                if "cancel" in type(exc).__name__.lower():
                    squad_result = SquadResult(
                        envelopes=[],
                        artifacts=[],
                        status="cancelled",
                        rationale="fleet: future was cancelled (CancelledError on result())",
                    )
                else:
                    squad_result = SquadResult(
                        envelopes=[],
                        artifacts=[],
                        status="failed",
                        rationale=f"fleet worker error (outer catch): {type(exc).__name__}: {exc}",
                    )
                tracker = None
            results[idx] = squad_result
            # Fix 6: main thread assigns tracker from return value — no worker
            # writes to worker_trackers.
            worker_trackers[idx] = tracker

            # Auto-cancel: if should_cancel triggers, set cancel_event and
            # call .cancel() on every not-yet-started future.  Only the
            # main thread ever sets cancel_event here (workers only .is_set()).
            if should_cancel is not None and should_cancel(squad_result):
                cancel_event.set()
                for other_future, other_idx in futures.items():
                    if other_future is not future and results[other_idx] is None:
                        if other_future.cancel():
                            # Future had not started yet — fill its slot now
                            # to prevent the outer as_completed loop from
                            # calling .result() (which would raise CancelledError).
                            results[other_idx] = SquadResult(
                                envelopes=[],
                                artifacts=[],
                                status="cancelled",
                                rationale=(
                                    "fleet cancelled: should_cancel triggered by "
                                    "a completed result; this task had not yet started"
                                ),
                            )

    # ------------------------------------------------------------------ #
    # Post-join: merge collector entries into state.open_pp_runs (MAIN THREAD).
    # Workers NEVER touched state.open_pp_runs; merge happens here, once,
    # in the single-threaded main thread (Fix 2 / concurrency-safety invariant).
    # ------------------------------------------------------------------ #
    for idx in runnable_indices:
        for entry in collectors[idx]:
            state.open_pp_runs.append(entry)

    return results, worker_trackers  # type: ignore[return-value]


def _fleet_worker(
    idx: int,
    pack: SquadPack,
    payload: Any,
    dispatcher_factory: Callable[[], Dispatcher],
    collector: list[dict[str, str]],
    cancel_event: threading.Event,
) -> tuple[SquadResult, Any]:
    """Single worker function run in a thread pool.

    Returns ``(SquadResult, tracker)`` so the main-thread collection loop can
    assign ``worker_trackers[idx] = tracker`` without any worker ever writing
    to the shared list — the "no worker mutates shared state" invariant is now
    literally true (Fix 6).

    Thread-safety contract
    ----------------------
    - ``cancel_event`` is checked AT ENTRY (before calling
      ``dispatcher_factory()`` / ``execute_squad``) via ``.is_set()``.  Workers
      NEVER set the event — only the main-thread collection loop sets it.  This
      keeps cancel_event as "read-only from workers": one set from the main
      thread; N ``.is_set()`` reads from worker threads.  No lock is needed
      because ``threading.Event.is_set()`` is thread-safe.
    - ``dispatcher_factory()`` is called here, inside the worker thread, so the
      returned dispatcher (and its asyncio event loop) is thread-local.  No
      asyncio state is shared across workers.
    - The factory MUST NOT mutate any shared list (no append to a shared
      collection).  It constructs, configures, and returns a fresh dispatcher.
    - Workers do NOT write to ``worker_trackers``; the tracker is RETURNED in
      the tuple and assigned by the main thread (Fix 6).
    - ``state`` is NOT passed to this function.  All work is done on the
      dispatcher and the pre-built ``payload``.  The only output written back
      is through ``collector`` (a per-worker list owned by the main thread) and
      the returned tuple.
    - Any exception is caught and returned as a ``failed`` ``SquadResult`` so one
      task's failure cannot propagate to the ThreadPoolExecutor and cancel the
      remaining futures.

    Note: ``execute_squad`` expects a ``state`` arg (for error_counters, etc.).
    We pass a lightweight read-only HydraState proxy below to satisfy the
    interface without enabling writes.  Currently execute_squad for mcp-entrypoint
    packs only *reads* state (for workflow_id / target_repo_id resolution) and
    writes ONLY to state.open_pp_runs (which is redirected to collector via
    collect_open_runs).  The error_counters write path in _via_mcp does NOT exist
    (only _via_impersonation / _via_claude_skill call _record_mcp_failure, and
    those are excluded by the mcp-only eligibility guard).
    """
    # Entry-check: if cancel_event is already set, return immediately without
    # dispatching.  This is the primary mechanism for preventing not-yet-started
    # workers from incurring expensive pp/MCP calls after a cancellation trigger.
    # Workers that are already PAST this check (i.e., inside dispatcher_factory()
    # or execute_squad()) complete naturally — threads cannot be force-killed.
    if cancel_event.is_set():
        return (
            SquadResult(
                envelopes=[],
                artifacts=[],
                status="cancelled",
                rationale="fleet cancelled before dispatch (cancel_event was set at worker entry)",
            ),
            None,  # no tracker; worker never constructed a dispatcher
        )
    try:
        # Per-worker fresh dispatcher (Fix 2).  The factory is a pure function:
        # it constructs, configures, and returns — it does NOT append to any
        # shared collection.
        worker_dispatcher = dispatcher_factory()
        # Extract tracker BEFORE calling execute_squad; return it alongside
        # the result so the main thread (not this worker) writes it to the
        # pre-sized worker_trackers list (Fix 6).
        tracker = getattr(worker_dispatcher, "_tool_tracker", None)
        squad_result = _execute_in_worker(worker_dispatcher, pack, payload, collector)
        return squad_result, tracker
    except Exception as exc:
        return (
            SquadResult(
                envelopes=[],
                artifacts=[],
                status="failed",
                rationale=f"fleet worker error: {type(exc).__name__}: {exc}",
            ),
            None,
        )


def _execute_in_worker(
    worker_dispatcher: Dispatcher,
    pack: SquadPack,
    payload: Any,
    collector: list[dict[str, str]],
) -> SquadResult:
    """Thin wrapper called from _fleet_worker; separated for clarity and testability.

    Constructs a minimal read-only HydraState stub (just workflow_id) so
    execute_squad has the state object it needs while keeping actual shared-state
    writes impossible via this path.
    """
    from .state import HydraState

    # Build a minimal state with the workflow_id from the payload.
    # This is the only field _via_mcp reads from state (for telemetry / harvest).
    # All other fields default safely: open_pp_runs=[] (will be redirected by
    # collect_open_runs anyway), error_counters={} (not mutated by mcp path).
    stub_state = HydraState(
        workflow_id=payload.workflow_id,
        root_goal="fleet-worker-stub",
    )

    return execute_squad(
        stub_state,
        pack,
        payload,
        worker_dispatcher,
        collect_open_runs=collector,
    )
