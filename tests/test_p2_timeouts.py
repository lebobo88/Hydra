"""P2.1: the codegen/judge timeouts are env-tunable with sane defaults.

Previously both were hardcoded at 30 min (silent), so a slow/wedged codex judge
or nested `claude -p` generation held the sequential best-of loop for 30 min at
~0 CPU — indistinguishable from a dispatch hang. These tests pin the new sane
defaults and the env overrides.
"""
from __future__ import annotations

import pytest

from hydra_core import squad_node
from hydra_core.judge import mcp_client


# ─── generation timeout (seconds) ───────────────────────────────────────────

def test_gen_timeout_default(monkeypatch):
    monkeypatch.delenv("HYDRA_GEN_TIMEOUT_S", raising=False)
    assert squad_node._gen_timeout_s() == 600


def test_gen_timeout_env_override(monkeypatch):
    monkeypatch.setenv("HYDRA_GEN_TIMEOUT_S", "300")
    assert squad_node._gen_timeout_s() == 300


@pytest.mark.parametrize("bad", ["0", "-5", "abc", ""])
def test_gen_timeout_bad_env_falls_back(monkeypatch, bad):
    monkeypatch.setenv("HYDRA_GEN_TIMEOUT_S", bad)
    assert squad_node._gen_timeout_s() == 600


# ─── judge timeout (milliseconds) ───────────────────────────────────────────

def test_judge_timeout_default(monkeypatch):
    monkeypatch.delenv("HYDRA_JUDGE_TIMEOUT_MS", raising=False)
    assert squad_node._judge_timeout_ms() == 480_000
    # Both consumers share the env var + default.
    assert mcp_client._default_judge_timeout_ms() == 480_000


def test_judge_timeout_env_override(monkeypatch):
    monkeypatch.setenv("HYDRA_JUDGE_TIMEOUT_MS", "120000")
    assert squad_node._judge_timeout_ms() == 120_000
    assert mcp_client._default_judge_timeout_ms() == 120_000


@pytest.mark.parametrize("bad", ["0", "-1", "nan", ""])
def test_judge_timeout_bad_env_falls_back(monkeypatch, bad):
    monkeypatch.setenv("HYDRA_JUDGE_TIMEOUT_MS", bad)
    assert squad_node._judge_timeout_ms() == 480_000
    assert mcp_client._default_judge_timeout_ms() == 480_000


def test_mcp_critique_client_default_is_not_the_old_30min(monkeypatch):
    # Regression guard: the client must no longer default to the 30-min ceiling.
    monkeypatch.delenv("HYDRA_JUDGE_TIMEOUT_MS", raising=False)
    client = mcp_client.MCPCritiqueClient(dispatcher=object(), cwd=".")
    assert client.timeout_ms == 480_000
    assert client.timeout_ms != 1_800_000


def test_mcp_critique_client_reads_env(monkeypatch):
    monkeypatch.setenv("HYDRA_JUDGE_TIMEOUT_MS", "90000")
    client = mcp_client.MCPCritiqueClient(dispatcher=object(), cwd=".")
    assert client.timeout_ms == 90_000
