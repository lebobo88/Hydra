"""``hydra submit-host-result``'s ``--result`` file is an INPUT boundary for
an untrusted host-subagent report (cross-vendor judge finding, REVISE round,
HIGH). ``hydra_core/cli.py``'s parse of that file used a bare ``json.loads``
(which ACCEPTS a bare NaN/Infinity token), and everything downstream of the
parse -- ``host_bridge.submit_host_result`` -> ``_apply_generate`` ->
``record_attempt`` (a pp-harness LEDGER side effect) -- used to run BEFORE
``submit_host_result``'s own strict ``save_cursor`` write was ever reached.
A poisoned cost could therefore already have hit the ledger by the time the
cursor write refused it, leaving the OLD cursor in place and the stage
retryable after ledger activity had already happened.

The fix rejects a non-finite value in the parsed result immediately after
parsing -- before the resume lock is even acquired and before
``_attended_live_dispatcher``/``host_bridge.submit_host_result`` is ever
called -- using the same ``find_non_finite_field`` walker
``_cmd_attended_finalize`` already uses for the analogous attended-result
boundary, not a new check.

These tests assert the SIDE EFFECT never happens (the dispatcher is never
even constructed), not merely that an error is raised -- a later guard
(e.g. inside ``submit_host_result`` itself) could also raise an error while
still letting a ledger call happen first.
"""
from __future__ import annotations

import argparse
import contextlib
import io
import json

import pytest

from hydra_core import cli as cli_module
from hydra_core import host_bridge


def _args(tmp_path, result_file):
    return argparse.Namespace(
        project=str(tmp_path),
        workflow_id="wf-1",
        run_id="run-1",
        call_key="generate-0",
        result=str(result_file),
        verbose=False,
    )


def test_attended_submit_rejects_non_finite_cost_before_any_dispatch(
    tmp_path, monkeypatch
):
    def _must_not_be_called(*_a, **_kw):
        raise AssertionError(
            "_attended_live_dispatcher must never be constructed once the "
            "--result parse has already found a non-finite value -- this "
            "proves the parse-level rejection, not a later guard, is what "
            "prevents the ledger/cursor side effects."
        )

    monkeypatch.setattr(cli_module, "_attended_live_dispatcher", _must_not_be_called)

    result_file = tmp_path / "result.json"
    result_file.write_text(json.dumps({"cost_usd": float("nan")}), encoding="utf-8")

    buf = io.StringIO()
    with contextlib.redirect_stdout(buf), contextlib.redirect_stderr(buf):
        rc = cli_module._cmd_attended_submit(_args(tmp_path, result_file))

    assert rc == 1
    payload = json.loads(buf.getvalue())
    assert payload["ok"] is False
    assert "non-finite value" in payload["error"]
    assert "$.cost_usd" in payload["error"]
    # No cursor file was ever created (the cursor-exists check, the lock, and
    # `submit_host_result` all sit downstream of the rejection).
    cfile = host_bridge.cursor_path(tmp_path, "wf-1", "run-1")
    assert not cfile.exists()


def test_attended_submit_rejects_string_non_finite_cost_before_any_dispatch(
    tmp_path, monkeypatch
):
    """Cross-vendor judge finding (follow-up round, HIGH): `find_non_finite_field`
    only recognizes an actual `float` NaN/Infinity -- a JSON STRING like
    `"cost_usd": "NaN"` is ordinary, valid JSON and sails past that walk
    untouched. This is the COMPLEMENTARY coercion-time check: it validates
    the value the same way the downstream cast (`_priced_cost`'s
    `coerce_untrusted_cost`) now does, catching the string BEFORE the
    dispatcher is ever constructed -- same side-effect-ordering proof as
    the real-float case above."""
    def _must_not_be_called(*_a, **_kw):
        raise AssertionError(
            "_attended_live_dispatcher must never be constructed once the "
            "--result parse has already found a string that does not "
            "coerce to a finite number."
        )

    monkeypatch.setattr(cli_module, "_attended_live_dispatcher", _must_not_be_called)

    result_file = tmp_path / "result.json"
    result_file.write_text(json.dumps({"cost_usd": "NaN"}), encoding="utf-8")

    buf = io.StringIO()
    with contextlib.redirect_stdout(buf), contextlib.redirect_stderr(buf):
        rc = cli_module._cmd_attended_submit(_args(tmp_path, result_file))

    assert rc == 1
    payload = json.loads(buf.getvalue())
    assert payload["ok"] is False
    assert "does not coerce to a finite number" in payload["error"]
    cfile = host_bridge.cursor_path(tmp_path, "wf-1", "run-1")
    assert not cfile.exists()


def test_attended_submit_rejects_string_non_finite_tokens(tmp_path, monkeypatch):
    def _must_not_be_called(*_a, **_kw):
        raise AssertionError("dispatcher must not be constructed")

    monkeypatch.setattr(cli_module, "_attended_live_dispatcher", _must_not_be_called)

    result_file = tmp_path / "result.json"
    result_file.write_text(
        json.dumps({"cost_usd": 0.02, "tokens_in": "Infinity"}), encoding="utf-8",
    )

    buf = io.StringIO()
    with contextlib.redirect_stdout(buf), contextlib.redirect_stderr(buf):
        rc = cli_module._cmd_attended_submit(_args(tmp_path, result_file))

    assert rc == 1
    payload = json.loads(buf.getvalue())
    assert payload["ok"] is False
    assert "does not coerce to a finite number" in payload["error"]


def test_attended_submit_finite_cost_is_unaffected(tmp_path, monkeypatch):
    """Control: an ordinary (finite) host result is not rejected by the new
    check -- it reaches the dispatcher exactly as before."""
    called = {"n": 0}

    class _StubDispatcher:
        live_execution = False

        def call_mcp(self, *_a, **_kw):
            return {"status": "done", "result": {}}

    def _fake_live_dispatcher(*_a, **_kw):
        called["n"] += 1
        return _StubDispatcher()

    monkeypatch.setattr(cli_module, "_attended_live_dispatcher", _fake_live_dispatcher)

    result_file = tmp_path / "result.json"
    result_file.write_text(json.dumps({"cost_usd": 4.5}), encoding="utf-8")

    buf = io.StringIO()
    with contextlib.redirect_stdout(buf), contextlib.redirect_stderr(buf):
        rc = cli_module._cmd_attended_submit(_args(tmp_path, result_file))

    # It fails downstream (no cursor exists in this minimal test setup), but
    # it MUST have reached the dispatcher -- proving the finite control path
    # is not blocked by the new check.
    assert called["n"] == 1
    assert rc == 1
    payload = json.loads(buf.getvalue())
    assert payload.get("error") in ("cursor_not_found",) or "cursor_not_found" in str(payload)
