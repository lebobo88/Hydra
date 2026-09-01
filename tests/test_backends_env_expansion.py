"""E2-5 — ``${VAR}`` expansion for MCP backend env/args.

Covers the three surfaces the fix touches:

1. the shared helper (``hydra_core.backends_env``) — set / unset / default /
   escape / inline-secret shape check;
2. ``MCPStdioDispatcher`` building ``StdioServerParameters`` from an expanded
   spec, on both the one-shot and pooled paths;
3. ``hydra doctor`` — WARN on an inline 64-hex value, and ``source=env`` when
   the operator key arrives through a ``${VAR}`` reference.

No test writes, reads, or asserts on a real secret; the doctor tests use a
synthetic all-``a`` hex string.
"""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import patch

import pytest

from hydra_core.backends_env import (
    expand_env_refs,
    expand_spec_env,
    has_env_ref,
    looks_like_inline_secret,
)

REPO_ROOT = Path(__file__).resolve().parents[1]

# Synthetic, non-secret value with the shape of a 64-hex operator key.
SYNTHETIC_HEX = "a" * 64


# ---------------------------------------------------------------------------
# 1. Helper
# ---------------------------------------------------------------------------


def test_expand_uses_environment_when_variable_is_set():
    assert expand_env_refs("${E2_5_TOK}", environ={"E2_5_TOK": "resolved"}) == "resolved"


def test_expand_missing_variable_yields_empty_string_and_warns(caplog):
    with caplog.at_level("WARNING"):
        out = expand_env_refs("${E2_5_ABSENT}", environ={})
    assert out == ""
    assert "E2_5_ABSENT" in caplog.text


def test_expand_missing_variable_does_not_raise():
    """Fail-soft: a missing reference must never crash a dispatch."""
    assert expand_spec_env({"env": {"K": "${E2_5_ABSENT}"}}, environ={}) == {
        "env": {"K": ""}
    }


def test_default_is_used_when_variable_is_unset_and_never_warns(caplog):
    with caplog.at_level("WARNING"):
        out = expand_env_refs("${E2_5_ABSENT:-fallback}", environ={})
    assert out == "fallback"
    assert "E2_5_ABSENT" not in caplog.text


def test_default_is_ignored_when_variable_is_set():
    assert (
        expand_env_refs("${E2_5_TOK:-fallback}", environ={"E2_5_TOK": "real"}) == "real"
    )


def test_empty_variable_falls_back_to_default():
    assert expand_env_refs("${E2_5_TOK:-fallback}", environ={"E2_5_TOK": ""}) == "fallback"


def test_reference_embedded_in_a_larger_string():
    out = expand_env_refs("prefix:${E2_5_TOK}:suffix", environ={"E2_5_TOK": "mid"})
    assert out == "prefix:mid:suffix"


def test_doubled_sigil_is_a_literal_escape():
    assert expand_env_refs("$${E2_5_TOK}", environ={"E2_5_TOK": "x"}) == "${E2_5_TOK}"
    assert has_env_ref("$${E2_5_TOK}") is False


def test_non_string_values_pass_through_untouched():
    assert expand_env_refs(7) == 7
    assert expand_env_refs(None) is None


def test_expand_spec_env_does_not_mutate_the_input_spec():
    spec = {"env": {"K": "${E2_5_TOK}"}, "args": ["${E2_5_TOK}"]}
    out = expand_spec_env(spec, environ={"E2_5_TOK": "v"})
    assert spec["env"]["K"] == "${E2_5_TOK}"
    assert spec["args"] == ["${E2_5_TOK}"]
    assert out["env"]["K"] == "v"
    assert out["args"] == ["v"]


def test_expand_spec_env_covers_args_command_and_cwd():
    out = expand_spec_env(
        {
            "command": "${E2_5_BIN}",
            "args": ["-m", "${E2_5_MOD}"],
            "cwd": "${E2_5_ROOT}",
            "env": {"PLAIN": "no-refs-here"},
        },
        environ={"E2_5_BIN": "python", "E2_5_MOD": "pkg", "E2_5_ROOT": "/w"},
    )
    assert out["command"] == "python"
    assert out["args"] == ["-m", "pkg"]
    assert out["cwd"] == "/w"
    assert out["env"]["PLAIN"] == "no-refs-here"


def test_env_keys_are_never_expanded():
    out = expand_spec_env({"env": {"${E2_5_TOK}": "v"}}, environ={"E2_5_TOK": "x"})
    assert list(out["env"]) == ["${E2_5_TOK}"]


@pytest.mark.parametrize(
    "value,expected",
    [
        (SYNTHETIC_HEX, True),
        (SYNTHETIC_HEX.upper(), True),
        (f"  {SYNTHETIC_HEX}  ", True),
        ("a" * 63, False),
        ("a" * 65, False),
        ("${HYDRA_OPERATOR_KEY}", False),
        ("/some/path", False),
        (None, False),
        (12345, False),
    ],
)
def test_inline_secret_shape_check(value, expected):
    assert looks_like_inline_secret(value) is expected


def test_helper_reads_os_environ_when_no_mapping_is_given(monkeypatch):
    monkeypatch.setenv("E2_5_FROM_OS", "os-value")
    assert expand_env_refs("${E2_5_FROM_OS}") == "os-value"


# ---------------------------------------------------------------------------
# 2. Dispatcher builds StdioServerParameters from an expanded spec
# ---------------------------------------------------------------------------


def _captured_params_for(spec: dict, monkeypatch, pooled: bool):
    """Drive the dispatcher far enough to capture its StdioServerParameters."""
    from hydra_core.dispatcher import MCPStdioDispatcher

    captured: dict = {}

    class _FakeParams:
        def __init__(self, **kwargs):
            captured.update(kwargs)

    import mcp as _mcp

    monkeypatch.setattr(_mcp, "StdioServerParameters", _FakeParams, raising=False)

    disp = MCPStdioDispatcher(REPO_ROOT)
    disp._servers = {"e2_5_backend": spec}

    def _boom(*_a, **_kw):
        # Raises synchronously so the dispatcher unwinds right after it has
        # built StdioServerParameters — no transport is ever opened.
        raise RuntimeError("stop-after-params")

    import mcp.client.stdio as _stdio

    monkeypatch.setattr(_stdio, "stdio_client", _boom, raising=False)

    coro = (
        disp._get_or_connect_pooled_session("e2_5_backend")
        if pooled
        else disp._async_call("e2_5_backend", "noop", {})
    )
    import asyncio

    try:
        asyncio.run(coro)
    except Exception:
        pass
    return captured


@pytest.mark.parametrize("pooled", [False, True])
def test_dispatcher_passes_expanded_env_to_stdio_params(monkeypatch, pooled):
    monkeypatch.setenv("E2_5_DISPATCH_TOK", "from-parent-env")
    spec = {
        "command": "python",
        "args": ["-m", "pkg", "--root=${E2_5_DISPATCH_TOK}"],
        "env": {
            "SECRET_REF": "${E2_5_DISPATCH_TOK}",
            "WITH_DEFAULT": "${E2_5_UNSET_HERE:-fallback}",
            "PLAIN": "literal",
        },
        "cwd": ".",
    }
    captured = _captured_params_for(spec, monkeypatch, pooled=pooled)

    assert captured, "dispatcher never built StdioServerParameters"
    assert captured["env"]["SECRET_REF"] == "from-parent-env"
    assert captured["env"]["WITH_DEFAULT"] == "fallback"
    assert captured["env"]["PLAIN"] == "literal"
    assert captured["args"][-1] == "--root=from-parent-env"
    # The registry's own dict must be untouched.
    assert spec["env"]["SECRET_REF"] == "${E2_5_DISPATCH_TOK}"


def test_dispatcher_missing_reference_does_not_crash_param_build(monkeypatch):
    monkeypatch.delenv("E2_5_NEVER_SET", raising=False)
    spec = {
        "command": "python",
        "args": [],
        "env": {"SECRET_REF": "${E2_5_NEVER_SET}"},
    }
    captured = _captured_params_for(spec, monkeypatch, pooled=False)
    assert captured["env"]["SECRET_REF"] == ""


# ---------------------------------------------------------------------------
# 3. hydra doctor
# ---------------------------------------------------------------------------


def _run_doctor_with_backends(backends: dict, tmp_path, monkeypatch, capsys):
    """Run full-mode `hydra doctor` against a synthetic ~/.hydra/backends.json.

    The WS-AUTH probe lives past the `--quick` cut-off, so the MCP probes and
    the eights spool are stubbed out the same way the RA-6 doctor tests do.
    """
    from hydra_core import cli

    fake_home = tmp_path / "home"
    (fake_home / ".hydra").mkdir(parents=True)
    (fake_home / ".hydra" / "backends.json").write_text(
        json.dumps(backends), encoding="utf-8"
    )
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: fake_home))

    class _MockDispatcher:
        def call_mcp(self, server, tool, args, **_kw):
            return {"status": "failed", "error": "not_registered"}

    class _EmptySpool:
        def count(self):
            return 0

    with patch(
        "hydra_core.dispatcher.MCPStdioDispatcher", return_value=_MockDispatcher()
    ), patch("hydra_core.dispatcher._load_mcp_config", return_value={}), patch(
        "hydra_core.eights.pending_spool.PendingSpool", return_value=_EmptySpool()
    ):
        rc = cli.main(["--project", str(REPO_ROOT), "doctor"])
    return rc, capsys.readouterr().out


def test_doctor_warns_when_backends_json_holds_an_inline_hex_value(
    tmp_path, monkeypatch, capsys
):
    monkeypatch.delenv("HYDRA_OPERATOR_KEY", raising=False)
    backends = {
        "xenia_tickets": {
            "command": "python",
            "args": [],
            "env": {"HYDRA_OPERATOR_KEY": SYNTHETIC_HEX},
        }
    }
    rc, out = _run_doctor_with_backends(backends, tmp_path, monkeypatch, capsys)

    assert rc in (0, 1)
    assert "inline secret-shaped" in out
    assert "xenia_tickets.env.HYDRA_OPERATOR_KEY" in out
    # The value itself must never be printed.
    assert SYNTHETIC_HEX not in out
    assert "source=backends.json" in out


def test_doctor_reports_source_env_for_a_var_reference(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("HYDRA_OPERATOR_KEY", SYNTHETIC_HEX)
    backends = {
        "xenia_tickets": {
            "command": "python",
            "args": [],
            "env": {"HYDRA_OPERATOR_KEY": "${HYDRA_OPERATOR_KEY}"},
        }
    }
    rc, out = _run_doctor_with_backends(backends, tmp_path, monkeypatch, capsys)

    assert rc in (0, 1)
    assert "source=env" in out
    assert "source=backends.json" not in out
    assert "inline secret-shaped" not in out
    assert SYNTHETIC_HEX not in out


def test_doctor_var_reference_without_the_variable_stays_unprovisioned(
    tmp_path, monkeypatch, capsys
):
    monkeypatch.delenv("HYDRA_OPERATOR_KEY", raising=False)
    backends = {
        "xenia_tickets": {
            "command": "python",
            "args": [],
            "env": {"HYDRA_OPERATOR_KEY": "${HYDRA_OPERATOR_KEY}"},
        }
    }
    rc, out = _run_doctor_with_backends(backends, tmp_path, monkeypatch, capsys)

    assert rc in (0, 1)
    assert "WS-AUTH operator key unprovisioned" in out


# ---------------------------------------------------------------------------
# 4. Export rewrites inline secrets to references
# ---------------------------------------------------------------------------


def test_export_backends_rewrites_inline_secret_to_a_reference(tmp_path, capsys):
    from hydra_core import cli

    out_path = tmp_path / "backends.json"
    servers = {
        "xenia_tickets": {
            "command": "python",
            "args": ["-m", "mcp_servers.xenia_tickets"],
            "env": {"HYDRA_OPERATOR_KEY": SYNTHETIC_HEX, "PYTHONPATH": "/x"},
        }
    }

    with patch("hydra_core.dispatcher._load_user_scope_mcp", return_value=servers), \
         patch("hydra_core.dispatcher.BACKEND_REGISTRY", out_path):
        rc = cli._cmd_gateway_export_backends(object())

    assert rc == 0
    written = json.loads(out_path.read_text(encoding="utf-8"))
    assert written["xenia_tickets"]["env"]["HYDRA_OPERATOR_KEY"] == (
        "${HYDRA_OPERATOR_KEY}"
    )
    assert written["xenia_tickets"]["env"]["PYTHONPATH"] == "/x"
    assert SYNTHETIC_HEX not in out_path.read_text(encoding="utf-8")
    assert SYNTHETIC_HEX not in capsys.readouterr().out


def test_gateway_templates_reference_the_operator_key_by_variable():
    templates = json.loads(
        (REPO_ROOT / "hydra_core" / "gateway_templates.json").read_text(
            encoding="utf-8"
        )
    )
    for name in ("hydra_control", "xenia_tickets"):
        env = templates[name]["env_template"]
        assert env["HYDRA_OPERATOR_KEY"] == "${HYDRA_OPERATOR_KEY}"
        assert not looks_like_inline_secret(env["HYDRA_OPERATOR_KEY"])
