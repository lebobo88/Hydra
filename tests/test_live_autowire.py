"""RC3/RC4 — a real dispatcher auto-wires autonomous pp codegen + a real judge.

Historically only the cli `--live` path set ``drive_pp_loop`` and wired
``MCPCritiqueClient``; the interactive skill / gateway / host-bound paths left
them unset → engineering only scaffolded (no codegen) and judge-enabled squads
got NoOp skeleton verdicts (every verdict downgraded to "revise" → Reflexion
churn). ``build_supervisor`` now keys off the dispatcher's ``live_execution``
marker so any run against a real dispatcher gets both — no `--live` required.
"""
from __future__ import annotations

from pathlib import Path

import hydra_core.judge as judge_pkg
from hydra_core.supervisor import build_supervisor

HYDRA_ROOT = Path(__file__).resolve().parents[1]


class _BaseDisp:
    def call_mcp(self, *_a, **_k):
        return {"status": "done", "result": {}}
    def emit_claude_prompt(self, *_a, **_k):
        return {"status": "host_pickup_required", "summary": ""}
    def invoke_claude_skill(self, *_a, **_k):
        return {"status": "host_pickup_required", "summary": ""}
    def spawn_subprocess(self, *_a, **_k):
        return {"status": "done", "stdout": "", "returncode": 0}


class _LiveDisp(_BaseDisp):
    live_execution = True


class _StubDisp(_BaseDisp):
    pass  # no live_execution marker


def _patch_critique(monkeypatch) -> list:
    constructed: list = []

    class _FakeCritique:
        def __init__(self, *, dispatcher, cwd):
            constructed.append((dispatcher, cwd))
        def critique(self, **_k):
            return {"outcome": "pass", "critique_md": "x" * 90, "score_json": {"c": 9}}

    monkeypatch.setattr(judge_pkg, "MCPCritiqueClient", _FakeCritique)
    return constructed


def test_live_dispatcher_autowires_drive_and_critique(monkeypatch) -> None:
    constructed = _patch_critique(monkeypatch)
    disp = _LiveDisp()
    build_supervisor(project_root=HYDRA_ROOT, dispatcher=disp, force_pure_python=True)
    # drive loop enabled so engineering dispatch generates code, not just scaffolds.
    assert getattr(disp, "drive_pp_loop", False) is True
    # a real cross-vendor judge was wired for the judge-enabled squads.
    assert constructed, "MCPCritiqueClient must be auto-wired for a live dispatcher"
    assert constructed[0][0] is disp


def test_stub_dispatcher_stays_dry(monkeypatch) -> None:
    constructed = _patch_critique(monkeypatch)
    disp = _StubDisp()
    build_supervisor(project_root=HYDRA_ROOT, dispatcher=disp, force_pure_python=True)
    # No live marker → unchanged dry behaviour: no drive loop, no real judge.
    assert getattr(disp, "drive_pp_loop", False) is False
    assert not constructed


def test_explicit_critique_client_not_overridden(monkeypatch) -> None:
    constructed = _patch_critique(monkeypatch)
    sentinel = object()
    disp = _LiveDisp()
    build_supervisor(project_root=HYDRA_ROOT, dispatcher=disp,
                     critique_client=sentinel, force_pure_python=True)
    # A caller-supplied critique client wins; we do not construct a second one.
    assert not constructed
    # drive loop is still enabled (independent of the critique client).
    assert getattr(disp, "drive_pp_loop", False) is True
