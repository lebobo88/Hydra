"""R3 (LOW, hardening; cross-vendor gpt-6-astra, final re-review of
feat/planning-phase HEAD 0bccdcb):
``hydra_core.host_bridge._adopt_or_launch_smoke_job`` adopts any existing
result/marker/sidecar file it finds at the deterministic
``smoke_job.job_paths(cursor_file, call_key)`` paths for a "launching"
reservation, purely by presence. A crash between saving the reservation
(``cursor["smoke_job"] = {...}; save_cursor(...)``) and ``start_job``'s own
stale-result/-sidecar/-marker removal could leave a LATER retry of the same
``(cursor_file, call_key)`` pair adopting a result/marker/sidecar left over
from an EARLIER run of those same deterministic paths -- e.g.
``recover_stalled_stage`` reusing the judge call_key for a fresh smoke
attempt after an earlier one already wrote a terminal result there.

Fixed: every reservation now mints a unique ``launch_token`` (``uuid4().hex``)
before it is saved, threads it through to the worker's argv
(:func:`smoke_job.start_job`), and the worker echoes it back into every
marker/sidecar/result file it writes. Adoption (this module) and
:func:`smoke_job.poll_job`'s result read both verify the token before
trusting any of that evidence; a legacy reservation with no token (persisted
before this field existed) keeps today's unconditional-trust behaviour.
"""
from __future__ import annotations

import json
import time
from pathlib import Path
from uuid import uuid4

import pytest

from hydra_core import host_bridge, smoke_job

from tests.test_p2_process_identity_hardening import (
    FakeDispatcher,
    _drive_to_await_judge,
    _JUDGE_KEY,
    _seed_launching_reservation,
)


def test_stale_result_with_wrong_token_is_not_adopted_matching_token_is(
    tmp_path, monkeypatch,
):
    disp = FakeDispatcher(required_cross_vendor=True)
    res, _work_path = _drive_to_await_judge(disp, tmp_path, monkeypatch)

    # A tiny grace window so the "still launching -> lost" transition (the
    # no-token-matches / no-other-evidence case) resolves fast in this test.
    monkeypatch.setenv("HYDRA_SMOKE_LAUNCH_GRACE_S", "0.2")

    cursor, paths = _seed_launching_reservation(host_bridge, res, tmp_path)
    # Simulate the crash window: the reservation carries a FRESH token...
    real_token = uuid4().hex
    cursor["smoke_job"]["launch_token"] = real_token
    host_bridge.save_cursor(res["cursor_path"], cursor)

    # ...but the result file at the same deterministic path is a PASS left
    # over from an EARLIER run of this call_key, carrying a DIFFERENT token
    # (the crash happened before start_job's own stale-file cleanup ran).
    Path(paths["result_path"]).parent.mkdir(parents=True, exist_ok=True)
    Path(paths["result_path"]).write_text(json.dumps({
        "status": "pass", "reason": "stale earlier run", "finished_at": time.time(),
        "launch_token": "some-earlier-unrelated-token",
    }), encoding="utf-8")

    job = host_bridge._adopt_or_launch_smoke_job(
        cursor, cursor_file=res["cursor_path"], call_key=_JUDGE_KEY,
        work_path=_work_path, stage_id="stage-1")

    assert job.get("adopted") != "result_already_written", (
        f"a result file carrying a DIFFERENT launch_token must never be "
        f"adopted as this reservation's own outcome; got {job!r}"
    )
    # It must not report the stale pass through poll_job either.
    if job.get("state") == "launching" and not job.get("spawn_error"):
        # Still within the (tiny) startup bound on this call -- poll again
        # after the grace window elapses to force the "lost" resolution and
        # prove it never surfaces as "pass" from the stale file.
        time.sleep(0.3)
        job2 = host_bridge._adopt_or_launch_smoke_job(
            cursor, cursor_file=res["cursor_path"], call_key=_JUDGE_KEY,
            work_path=_work_path, stage_id="stage-1")
        assert job2.get("spawn_error"), (
            f"past the startup bound with only a wrong-token result on disk, "
            f"the reservation must resolve lost, never adopt the stale pass; "
            f"got {job2!r}"
        )
        result = smoke_job.poll_job(job2)
    else:
        assert job.get("spawn_error"), job
        result = smoke_job.poll_job(job)

    assert result is not None
    assert result.get("status") != "pass", (
        f"the stale wrong-token result must never surface as this call's "
        f"own pass outcome; got {result!r}"
    )

    # Counterpart: a result carrying the MATCHING token is still adopted.
    cursor2, paths2 = _seed_launching_reservation(host_bridge, res, tmp_path)
    match_token = uuid4().hex
    cursor2["smoke_job"]["launch_token"] = match_token
    host_bridge.save_cursor(res["cursor_path"], cursor2)
    Path(paths2["result_path"]).write_text(json.dumps({
        "status": "pass", "reason": "this run's own result",
        "finished_at": time.time(), "launch_token": match_token,
    }), encoding="utf-8")

    job3 = host_bridge._adopt_or_launch_smoke_job(
        cursor2, cursor_file=res["cursor_path"], call_key=_JUDGE_KEY,
        work_path=_work_path, stage_id="stage-1")
    assert job3.get("adopted") == "result_already_written", job3
    result3 = smoke_job.poll_job(job3)
    assert result3 is not None and result3.get("status") == "pass", result3


def test_legacy_reservation_without_launch_token_keeps_unconditional_trust(
    tmp_path, monkeypatch,
):
    """A reservation persisted before ``launch_token`` existed (no key at
    all, simulated here by never setting it) must keep today's behaviour --
    it adopts a result file purely by presence, exactly like before this
    fix."""
    disp = FakeDispatcher(required_cross_vendor=True)
    res, _work_path = _drive_to_await_judge(disp, tmp_path, monkeypatch)

    cursor, paths = _seed_launching_reservation(host_bridge, res, tmp_path)
    assert "launch_token" not in cursor["smoke_job"]

    Path(paths["result_path"]).parent.mkdir(parents=True, exist_ok=True)
    Path(paths["result_path"]).write_text(json.dumps({
        "status": "pass", "reason": "legacy result, no token at all",
        "finished_at": time.time(),
    }), encoding="utf-8")

    job = host_bridge._adopt_or_launch_smoke_job(
        cursor, cursor_file=res["cursor_path"], call_key=_JUDGE_KEY,
        work_path=_work_path, stage_id="stage-1")
    assert job.get("adopted") == "result_already_written", job
