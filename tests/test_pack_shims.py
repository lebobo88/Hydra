"""Smoke tests for the executive_suite + rlm_creative MCP shims.

Exercises the bare-stdio fallback so these run without the optional `mcp` SDK.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path
from uuid import uuid4

import pytest

from hydra_core.schemas import (
    Constraints, CSuiteDecisionPacket, CreativeBrief, MemoryRef,
)
from hydra_core.squad_loader import discover_squads


HYDRA_ROOT = Path(__file__).resolve().parents[1]


# ---------- fixtures ----------

def _make_fake_es_root(root: Path) -> Path:
    base = root / "plugins/executive-suite"
    (base / "agents").mkdir(parents=True)
    (base / "agents/ceo.md").write_text("# CEO\nrole: chief executive", encoding="utf-8")
    (base / "agents/cfo.md").write_text("# CFO\nrole: chief financial", encoding="utf-8")
    (base / "skills/financial-frameworks").mkdir(parents=True)
    (base / "skills/financial-frameworks/SKILL.md").write_text("# Skill: WACC/IRR", encoding="utf-8")
    (base / "commands").mkdir(parents=True)
    (base / "commands/board-meeting.md").write_text("# /board-meeting", encoding="utf-8")
    return root


def _make_fake_rlm_root(root: Path) -> Path:
    base = root / "plugins/rlm-creative"
    (base / "skills/comfyui").mkdir(parents=True)
    (base / "skills/comfyui/SKILL.md").write_text("# Skill: ComfyUI", encoding="utf-8")
    (base / "commands").mkdir(parents=True)
    (base / "commands/creative-campaign.md").write_text("# /creative-campaign", encoding="utf-8")
    (base / "commands/photo-direction.md").write_text("# /photo-direction", encoding="utf-8")
    (base / "agents/helios-crew").mkdir(parents=True)
    for head in ["calliope", "erato", "polyhymnia", "terpsichore", "euterpe", "clio", "urania", "helios"]:
        (base / f"agents/{head}.md").write_text(f"# {head.capitalize()}", encoding="utf-8")
    for sub in ["video-synth", "audio-foley", "music-score", "dialogue-mix", "blender-model", "blender-rig", "governance-c2pa"]:
        (base / f"agents/helios-crew/{sub}.md").write_text(f"# {sub}", encoding="utf-8")
    return root


# ---------- bare-stdio JSON-RPC harness ----------

def _bare_call(module: str, root_env: dict[str, str], requests: list[dict]) -> list[dict]:
    """Spawn `python -m <module>` in bare-stdio mode, send N requests, return parsed responses."""
    proc = subprocess.Popen(
        [sys.executable, "-m", module],
        cwd=str(HYDRA_ROOT),
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        env={**os.environ, **root_env,
             "HYDRA_MCP_BARE": "1", "PYTHONIOENCODING": "utf-8"},
        text=True,
        encoding="utf-8",
    )
    payload = "\n".join(json.dumps(r) for r in requests) + "\n"
    try:
        stdout, stderr = proc.communicate(payload, timeout=15)
    except subprocess.TimeoutExpired:
        proc.kill()
        raise
    if proc.returncode not in (0, None):
        pytest.fail(f"server {module} exited {proc.returncode}\nstderr:\n{stderr}")
    lines = [ln for ln in stdout.splitlines() if ln.strip()]
    return [json.loads(ln) for ln in lines]


# ---------- executive_suite ----------

def test_executive_mcp_lists_tools_and_roster(tmp_path):
    root = _make_fake_es_root(tmp_path / "es")
    responses = _bare_call(
        "mcp_servers.executive_suite",
        {"HYDRA_ES_ROOT": str(root)},
        [
            {"id": "1", "method": "list_tools"},
            {"id": "2", "method": "es.roster.list", "params": {}},
            {"id": "3", "method": "es.skill.list", "params": {}},
            {"id": "4", "method": "es.command.list", "params": {}},
            {"id": "5", "method": "es.ping", "params": {}},
        ],
    )
    by_id = {r["id"]: r for r in responses}
    assert "es.roster.list" in by_id["1"]["result"]
    assert "es.output.write" in by_id["1"]["result"]
    assert len(by_id["1"]["result"]) == 9  # 9 tools

    roster = [a["name"] for a in by_id["2"]["result"]["agents"]]
    assert {"ceo", "cfo"} <= set(roster)
    assert any(s["name"] == "financial-frameworks" for s in by_id["3"]["result"]["skills"])
    assert any(c["name"] == "board-meeting" for c in by_id["4"]["result"]["commands"])
    assert by_id["5"]["result"]["ok"] is True


def test_executive_mcp_write_read_roundtrip(tmp_path):
    root = _make_fake_es_root(tmp_path / "es")
    responses = _bare_call(
        "mcp_servers.executive_suite",
        {"HYDRA_ES_ROOT": str(root)},
        [
            {"id": "1", "method": "es.output.write",
             "params": {"domain": "finance", "topic": "Q3 Capital Plan",
                        "content": "# Q3 Plan\nbody"}},
        ],
    )
    write_res = responses[0]["result"]
    rel = write_res["relative"]
    assert rel.startswith("output/finance/")
    assert (root / rel).read_text(encoding="utf-8") == "# Q3 Plan\nbody"

    # Read it back through the server.
    responses2 = _bare_call(
        "mcp_servers.executive_suite",
        {"HYDRA_ES_ROOT": str(root)},
        [{"id": "2", "method": "es.output.read", "params": {"path": rel}}],
    )
    assert responses2[0]["result"]["content"] == "# Q3 Plan\nbody"


def test_executive_mcp_rejects_path_escape(tmp_path):
    root = _make_fake_es_root(tmp_path / "es")
    responses = _bare_call(
        "mcp_servers.executive_suite",
        {"HYDRA_ES_ROOT": str(root)},
        [{"id": "1", "method": "es.output.read", "params": {"path": "../escape.txt"}}],
    )
    # Either an explicit error envelope or PermissionError in 'error'.
    assert "error" in responses[0] or responses[0]["result"].get("error") == "not_found"


# ---------- rlm_creative ----------

def test_rlm_mcp_lists_plugin_skills_commands_and_agents(tmp_path):
    root = _make_fake_rlm_root(tmp_path / "rlm")
    responses = _bare_call(
        "mcp_servers.rlm_creative",
        {"HYDRA_RLM_ROOT": str(root)},
        [
            {"id": "1", "method": "list_tools"},
            {"id": "2", "method": "rlm.skill.list", "params": {}},
            {"id": "3", "method": "rlm.command.list", "params": {}},
            {"id": "4", "method": "rlm.agent.list", "params": {}},
        ],
    )
    by_id = {r["id"]: r for r in responses}
    assert "rlm.output.write" in by_id["1"]["result"]

    skills = [s["name"] for s in by_id["2"]["result"]["skills"]]
    assert "comfyui" in skills

    cmds = [c["name"] for c in by_id["3"]["result"]["commands"]]
    assert {"creative-campaign", "photo-direction"} <= set(cmds)

    agents = [a["name"] for a in by_id["4"]["result"]["agents"]]
    assert {"brand-strategist", "blender-model"} <= set(agents)
    assert len(agents) == 15


def test_rlm_root_precedence(tmp_path):
    root_primary = _make_fake_rlm_root(tmp_path / "rlm_primary")
    root_secondary = _make_fake_rlm_root(tmp_path / "rlm_secondary")

    # Primary overrides secondary
    responses = _bare_call(
        "mcp_servers.rlm_creative",
        {"HYDRA_RLM_ROOT": str(root_primary), "HYDRA_RLM_CREATIVE_ROOT": str(root_secondary)},
        [{"id": "1", "method": "rlm.ping", "params": {}}],
    )
    assert responses[0]["result"]["root"] == str(root_primary.resolve())

    # Secondary used when primary absent
    responses2 = _bare_call(
        "mcp_servers.rlm_creative",
        {"HYDRA_RLM_CREATIVE_ROOT": str(root_secondary)},
        [{"id": "1", "method": "rlm.ping", "params": {}}],
    )
    assert responses2[0]["result"]["root"] == str(root_secondary.resolve())


def test_rlm_creative_server_agent_catalog_validation(tmp_path):
    from mcp_servers.rlm_creative.server import _tool_handlers
    import os

    # Incomplete root missing files raises FileNotFoundError
    broken_root = tmp_path / "broken_rlm"
    (broken_root / "plugins/rlm-creative/agents").mkdir(parents=True)
    os.environ["HYDRA_RLM_ROOT"] = str(broken_root)
    with pytest.raises(FileNotFoundError, match="Missing required Garland agent files"):
        _tool_handlers()

    # Unexpected extra files raises ValueError
    full_root = _make_fake_rlm_root(tmp_path / "full_rlm")
    (full_root / "plugins/rlm-creative/agents/stray.md").write_text("# Stray", encoding="utf-8")
    os.environ["HYDRA_RLM_ROOT"] = str(full_root)
    with pytest.raises(ValueError, match="Unexpected extra agent markdown files"):
        _tool_handlers()

    # Clean root succeeds and returns 9 tools
    clean_root = _make_fake_rlm_root(tmp_path / "clean_rlm")
    os.environ["HYDRA_RLM_ROOT"] = str(clean_root)
    handlers = _tool_handlers()
    assert len(handlers) == 9
    assert len(handlers["rlm.agent.list"]({})["agents"]) == 15


def test_rlm_mcp_write_roundtrip(tmp_path):
    root = _make_fake_rlm_root(tmp_path / "rlm")
    responses = _bare_call(
        "mcp_servers.rlm_creative",
        {"HYDRA_RLM_ROOT": str(root)},
        [{"id": "1", "method": "rlm.output.write",
          "params": {"phase": "brief", "topic": "launch-teaser", "content": "# Teaser",
                     "domain": "creative", "scopes": ["team:garland-crew"]}}],
    )
    rel = responses[0]["result"]["relative"]
    assert rel.startswith("RLM/output/brief/")
    assert (root / rel).exists()


@pytest.mark.parametrize("params", [
    {"phase": "brief", "topic": "launch-teaser", "content": "# Teaser", "domain": "other", "scopes": ["team:garland-crew"]},
    {"phase": "draft", "topic": "launch-teaser", "content": "# Teaser", "domain": "creative", "scopes": ["team:garland-crew"]},
    {"phase": "brief", "topic": "Launch Teaser", "content": "# Teaser", "domain": "creative", "scopes": ["team:garland-crew"]},
    {"phase": "brief", "topic": "launch-teaser", "content": "# Teaser", "domain": "creative", "scopes": ["uncontrolled"]},
])
def test_rlm_mcp_rejects_invalid_output_contract(tmp_path, params):
    root = _make_fake_rlm_root(tmp_path / "rlm")
    responses = _bare_call(
        "mcp_servers.rlm_creative", {"HYDRA_RLM_ROOT": str(root)},
        [{"id": "1", "method": "rlm.output.write", "params": params}],
    )
    assert responses[0]["result"]["error"] == "invalid_output_contract"
    assert not (root / "RLM/output").exists()


# ---------- dispatcher enrichment ----------

class _FakeDispatcher:
    """Records call_mcp invocations and returns scripted responses."""
    def __init__(self, scripted: dict[tuple[str, str], dict]):
        self.calls: list[tuple[str, str, dict]] = []
        self._scripted = scripted

    def call_mcp(self, server, tool, args, **_kw):
        self.calls.append((server, tool, args))
        key = (server, tool)
        if key in self._scripted:
            return {"status": "done", "tool": tool, "result": self._scripted[key]}
        return {"status": "failed", "error": "no script"}

    def spawn_subprocess(self, cmd, env=None):
        return {"status": "stub"}

    def emit_claude_prompt(self, prompt, agent=None):
        return {"status": "host_pickup_required", "summary": "(boardroom prompt sent)"}

    def invoke_claude_skill(self, skill, args):
        return {"status": "host_pickup_required", "summary": f"would invoke /{skill}"}


def test_via_impersonation_persists_real_memoryref():
    from hydra_core.squad_node import _via_impersonation
    from hydra_core.state import HydraState

    packs = discover_squads(HYDRA_ROOT)
    pack = packs["executive"]

    wf = uuid4()
    inbound = CSuiteDecisionPacket(
        workflow_id=wf,
        origin_squad="hydra",
        origin="BOARDROOM",
        objective="Review Q3 capital allocation",
        constraints=Constraints(),
        proposed_tasks=[],
    )
    dispatcher = _FakeDispatcher({
        ("executive_suite", "es.roster.list"): {"agents": [{"name": "ceo"}, {"name": "cfo"}]},
        ("executive_suite", "es.output.write"): {
            "path": "C:/fake/output/finance/review-q3-capital-allocation-2026-05-19.md",
            "relative": "output/finance/review-q3-capital-allocation-2026-05-19.md",
            "bytes": 512,
        },
    })

    result = _via_impersonation(HydraState(root_goal="x"), pack, inbound, dispatcher)
    # F11: emit_claude_prompt returns host_pickup_required → status is now
    # 'deferred_to_host' (not 'done') so governance can block unexecuted workflows.
    assert result.status == "deferred_to_host"
    assert result.host_pickup_pending is True
    decision = result.envelopes[0]
    assert decision.artifacts and isinstance(decision.artifacts[0], MemoryRef)
    assert decision.artifacts[0].key.startswith("es:output:output/finance/")

    seen_tools = [c[1] for c in dispatcher.calls]
    assert "es.roster.list" in seen_tools
    assert "es.output.write" in seen_tools


def test_via_claude_skill_persists_real_memoryref():
    from hydra_core.squad_node import _via_claude_skill
    from hydra_core.state import HydraState

    packs = discover_squads(HYDRA_ROOT)
    pack = packs["garland"]

    wf = uuid4()
    inbound = CreativeBrief(
        workflow_id=wf,
        origin_squad="hydra",
        campaign_id=uuid4(),
        objective="30-second cinematic teaser",
        target_audience="enterprise buyers",
        constraints=Constraints(),
    )
    dispatcher = _FakeDispatcher({
        ("rlm_creative", "rlm.command.list"): {"commands": [{"name": "creative-campaign"}, {"name": "photo-direction"}]},
        ("rlm_creative", "rlm.output.write"): {
            "path": "C:/fake/RLM/output/brief/teaser-2026-05-19.md",
            "relative": "RLM/output/brief/teaser-2026-05-19.md",
            "bytes": 320,
        },
    })

    result = _via_claude_skill(HydraState(root_goal="x"), pack, inbound, dispatcher)
    assert result.envelopes
    decision = result.envelopes[0]
    assert decision.artifacts[0].key.startswith("rlm:output:RLM/output/brief/")

    seen_tools = [c[1] for c in dispatcher.calls]
    assert "rlm.command.list" in seen_tools
    assert "rlm.output.write" in seen_tools
    write_args = next(call[2] for call in dispatcher.calls if call[1] == "rlm.output.write")
    assert write_args["domain"] == "creative"
    assert write_args["scopes"] == ["team:garland-crew"]


def test_via_impersonation_falls_back_when_mcp_unreachable():
    """If both MCP calls fail, the dispatcher must still produce a result with
    a synthetic MemoryRef and not error out (graceful degradation)."""
    from hydra_core.squad_node import _via_impersonation
    from hydra_core.state import HydraState

    packs = discover_squads(HYDRA_ROOT)
    pack = packs["executive"]
    inbound = CSuiteDecisionPacket(
        workflow_id=uuid4(),
        origin_squad="hydra",
        origin="BOARDROOM",
        objective="anything",
        constraints=Constraints(),
        proposed_tasks=[],
    )
    dispatcher = _FakeDispatcher({})  # all calls return status=failed
    result = _via_impersonation(HydraState(root_goal="x"), pack, inbound, dispatcher)
    # F11: emit_claude_prompt returns host_pickup_required → status is now
    # 'deferred_to_host' so governance can block unexecuted pack workflows.
    assert result.status == "deferred_to_host"
    assert result.envelopes[0].artifacts[0].key.startswith("es:boardroom:")
