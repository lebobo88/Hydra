"""Hook matchers must actually fire on the tool names the host sends (Hydra#82).

Hermetic: no pwsh, no network, no daemon. The matcher semantics are pinned by
``hook_fires`` below, which follows the documented rule at
https://code.claude.com/docs/en/hooks#matcher-patterns. The installed host
version is unverified, so that helper pins an *assumption*; every test uses
``hook_fires`` and never ``re.fullmatch``.
"""
from __future__ import annotations

import json
import os
import re
import subprocess
import sys
from pathlib import Path

import pytest

from hydra_core.native_packs import NATIVE_PACKS
from hydra_core.toolshed import build_default_shed

REPO = Path(__file__).resolve().parent.parent
HOOKS_DIR = REPO / "plugins" / "hydra" / "hooks"
HOOKS_JSON = HOOKS_DIR / "hooks.json"
CHECK = HOOKS_DIR / "iolaus_check.py"
LOG = HOOKS_DIR / "iolaus_log.py"
HYDRA_HOOK = HOOKS_DIR / "hydra_hook.py"

_SAN = re.compile(r"[^A-Za-z0-9_-]")


def _sanitise(name: str) -> str:
    return _SAN.sub("_", name)


_CATALOG = build_default_shed()._catalog
GATEWAY_NAMES = {
    _sanitise(f"mcp__hydra_gateway__{server}__{entry.name}")
    for server, entries in _CATALOG.items()
    for entry in entries
}
DIRECT_NAMES = {
    _sanitise(f"mcp__{server}__{entry.name}")
    for server, entries in _CATALOG.items()
    for entry in entries
}
HOST_BUILTINS = {
    "Agent", "Bash", "Edit", "Write", "NotebookEdit", "Read", "Glob", "Grep",
    "Skill", "WebFetch", "WebSearch", "TodoWrite", "TaskCreate", "TaskGet",
    "TaskList", "TaskUpdate", "TaskOutput", "TaskStop",
}

_PLAIN = re.compile(r"^[A-Za-z0-9_|]+$")


def hook_fires(matcher: str, tool_name: str) -> bool:
    """Documented matcher rule: '' or '*' = all; letters/digits/_/| = exact
    names; anything else = unanchored regex search."""
    if matcher in ("", "*"):
        return True
    if _PLAIN.match(matcher):
        return tool_name in matcher.split("|")
    return bool(re.search(matcher, tool_name))


PRE_DOCTOR = 'pwsh -NoProfile -File "${CLAUDE_PLUGIN_ROOT}/hooks/hydra-doctor.ps1" doctor --quick'
PRE_CHECK = 'python "${CLAUDE_PLUGIN_ROOT}/hooks/iolaus_check.py"'
PRE_HYDRA = 'python "${CLAUDE_PLUGIN_ROOT}/hooks/hydra_hook.py" pre'
POST_LOG = 'python "${CLAUDE_PLUGIN_ROOT}/hooks/iolaus_log.py"'
POST_HYDRA = 'python "${CLAUDE_PLUGIN_ROOT}/hooks/hydra_hook.py" post'
POST_ECHO = "echo postcheck"

M_MEM = "^mcp__(hydra_gateway__)?hydra_memory__.+$"
M_ES = "^mcp__(hydra_gateway__)?executive_suite__.+$"
M_RLM = "^mcp__(hydra_gateway__)?rlm_creative__.+$"
M_SENATE = "mcp__(hydra_gateway__)?senate__.*"
M_POST_MEM = "^mcp__(hydra_gateway__)?hydra_memory__hydra-mem_write_.+$"
M_POST_OUT = (
    "^mcp__(hydra_gateway__)?"
    "(executive_suite__es|rlm_creative__rlm|senate__senate)_output_write$"
)

STALE = [
    ("PreToolUse", "Task"),
    ("PreToolUse", "mcp__hydra_memory__.*"),
    ("PreToolUse", "mcp__executive_suite__.*"),
    ("PreToolUse", "mcp__rlm_creative__.*"),
    ("PostToolUse", "Task"),
    ("PostToolUse", "mcp__hydra_memory__write.*"),
    ("PostToolUse", "mcp__(executive_suite|rlm_creative|senate)__.*output\\.write.*"),
]


def _cfg() -> dict:
    return json.loads(HOOKS_JSON.read_text(encoding="utf-8"))["hooks"]


def _groups(event: str) -> list[dict]:
    return _cfg().get(event, [])


def _commands(event: str, tool: str) -> list[str]:
    out: list[str] = []
    for g in _groups(event):
        if hook_fires(g.get("matcher", ""), tool):
            out.extend(h["command"] for h in g["hooks"])
    return out


def _gw(server: str, tool: str) -> str:
    return _sanitise(f"mcp__hydra_gateway__{server}__{tool}")


# ---------------------------------------------------------------- (a)


def test_name_sets_and_dispatch_helper():
    for name in (
        "mcp__hydra_gateway__hydra_memory__hydra-mem_write_episodic",
        "mcp__hydra_gateway__hydra_memory__hydra-mem_semantic_search",
        "mcp__hydra_gateway__executive_suite__es_output_write",
        "mcp__hydra_gateway__rlm_creative__rlm_output_write",
        "mcp__hydra_gateway__senate__senate_output_write",
        "mcp__hydra_gateway__hydra_control__hydra_workflow_plan",
        "mcp__hydra_gateway__hydra_control__hydra_workflow_step",
        "mcp__hydra_gateway__hydra_control__hydra_workflow_submit_host_result",
        "mcp__hydra_gateway__hydra_control__hydra_workflow_launch",
    ):
        assert name in GATEWAY_NAMES, name
    assert "mcp__hydra_memory__hydra-mem_write_episodic" in DIRECT_NAMES
    assert "Task" not in HOST_BUILTINS and "Agent" in HOST_BUILTINS
    assert hook_fires("Task", "TaskCreate") is False
    assert hook_fires("Write|Edit|NotebookEdit", "Edit") is True
    assert hook_fires("Agent", "Agent") is True


# ---------------------------------------------------------------- (b)


def test_every_mcp_matcher_fires_on_gateway_and_direct():
    selected = [
        (ev, g["matcher"])
        for ev in ("PreToolUse", "PostToolUse")
        for g in _groups(ev)
        if "mcp__" in g.get("matcher", "")
    ]
    matchers = {m for _, m in selected}
    assert matchers == {M_MEM, M_ES, M_RLM, M_SENATE, M_POST_MEM, M_POST_OUT}, matchers
    servers = ("hydra_memory", "executive_suite", "rlm_creative", "senate")
    for ev, matcher in selected:
        assert any(hook_fires(matcher, n) for n in GATEWAY_NAMES), (
            f"{ev} matcher {matcher!r} fires on no gateway tool name"
        )
        for server in servers:
            if server not in matcher:
                continue
            own = [n for n in DIRECT_NAMES if n.startswith(f"mcp__{server}__")]
            assert own, f"no direct names for {server}"
            assert any(hook_fires(matcher, n) for n in own), (
                f"{ev} matcher {matcher!r} fires on no direct {server} name"
            )


# ---------------------------------------------------------------- (c)


def test_iolaus_matchers_fire_on_agent():
    for ev, script in (("PreToolUse", "iolaus_check"), ("PostToolUse", "iolaus_log")):
        groups = [
            g for g in _groups(ev)
            if any(script in h["command"] for h in g["hooks"])
        ]
        assert groups, f"no {script} group in {ev}"
        for g in groups:
            assert hook_fires(g["matcher"], "Agent"), (
                f"{ev} {script} matcher {g['matcher']!r} does not fire on Agent"
            )


def test_hydra_hook_wired_for_pre_and_post_on_agent():
    for ev in ("PreToolUse", "PostToolUse"):
        groups = [
            g for g in _groups(ev)
            if any("hydra_hook.py" in h["command"] for h in g["hooks"])
        ]
        assert groups, f"no hydra_hook.py group in {ev}"
        for g in groups:
            assert hook_fires(g["matcher"], "Agent"), (
                f"{ev} hydra_hook matcher {g['matcher']!r} does not fire on Agent"
            )
            assert not hook_fires(g["matcher"], "TaskCreate"), (
                f"{ev} hydra_hook matcher {g['matcher']!r} fires on TaskCreate"
            )
    assert HYDRA_HOOK.is_file(), HYDRA_HOOK


# ---------------------------------------------------------------- (d)


@pytest.mark.parametrize("event,stale", STALE)
def test_stale_matchers_miss_gateway_and_agent(event, stale):
    hits = [n for n in GATEWAY_NAMES if hook_fires(stale, n)]
    assert not hits, f"stale {event} matcher {stale!r} fires on {hits[:3]}"
    assert not hook_fires(stale, "Agent")
    # and none of them is still present in hooks.json
    assert stale not in [g.get("matcher") for g in _groups(event)]


# ---------------------------------------------------------------- (e)


_CONTROL = [
    _gw("hydra_control", f"hydra.workflow.{v}")
    for v in ("plan", "step", "submit_host_result", "launch")
]


@pytest.mark.parametrize("tool", _CONTROL)
def test_gateway_routing_table_control_verbs(tool):
    assert tool in GATEWAY_NAMES
    assert _commands("PreToolUse", tool) == []
    assert _commands("PostToolUse", tool) == []


@pytest.mark.parametrize(
    "tool,pre,post",
    [
        (_gw("hydra_memory", "hydra-mem.write_episodic"), [PRE_DOCTOR], [POST_ECHO]),
        (_gw("hydra_memory", "hydra-mem.semantic_search"), [PRE_DOCTOR], []),
        (_gw("executive_suite", "es.output.write"), [PRE_DOCTOR], [POST_ECHO]),
        (_gw("rlm_creative", "rlm.output.write"), [PRE_DOCTOR], [POST_ECHO]),
        (_gw("senate", "senate.output.write"), [PRE_DOCTOR], [POST_ECHO]),
        ("Agent", [PRE_CHECK, PRE_HYDRA], [POST_LOG, POST_HYDRA]),
    ],
)
def test_gateway_routing_table(tool, pre, post):
    assert _commands("PreToolUse", tool) == pre, tool
    assert _commands("PostToolUse", tool) == post, tool


# ---------------------------------------------------------------- (f)


def test_negative_routing_cases():
    assert not hook_fires(M_POST_MEM, _gw("hydra_memory", "hydra-mem.semantic_search"))
    assert not hook_fires(M_POST_OUT, "mcp__hydra_gateway__executive_suite__es_output_write_extra")
    assert not hook_fires(M_POST_OUT, "mcp__hydra_gateway__senate__es_output_write")
    agent_matchers = [
        g["matcher"] for ev in ("PreToolUse", "PostToolUse")
        for g in _groups(ev) if g.get("matcher") == "Agent"
    ]
    assert len(agent_matchers) == 2
    assert not hook_fires("Agent", "TaskCreate")
    control = [n for n in GATEWAY_NAMES | DIRECT_NAMES if "hydra_control" in n]
    assert control
    for m in (M_MEM, M_ES, M_RLM, M_POST_MEM, M_POST_OUT):
        for n in control:
            assert not hook_fires(m, n), (m, n)


# ---------------------------------------------------------------- (g)


def test_unchanged_matchers_byte_identical():
    hooks = _cfg()
    pre = {g["matcher"]: g["hooks"] for g in hooks["PreToolUse"]}
    expected = {
        "Write|Edit|NotebookEdit": 'pwsh -NoProfile -File "${CLAUDE_PLUGIN_ROOT}/hooks/hydra-block-direct-write.ps1"',
        "Bash": 'pwsh -NoProfile -File "${CLAUDE_PLUGIN_ROOT}/hooks/hydra-block-bash-writes.ps1"',
        "Skill": 'pwsh -NoProfile -File "${CLAUDE_PLUGIN_ROOT}/hooks/hydra-block-direct-pp.ps1"',
        "mcp__(hydra_gateway__)?senate__.*": PRE_DOCTOR,
    }
    for matcher, command in expected.items():
        assert matcher in pre, matcher
        assert pre[matcher] == [{"type": "command", "command": command, "timeout": 10}], matcher
    total = sum(len(g["hooks"]) for groups in hooks.values() for g in groups)
    assert total == 18
    assert sum(len(groups) for groups in hooks.values()) == 14
    assert not any("plugin_" in g.get("matcher", "") for gs in hooks.values() for g in gs)


# ---------------------------------------------------------------- scripts


@pytest.fixture
def project(tmp_path):
    for slug, dep in (("oldsquad", "2000-01-01"), ("activesquad", "null")):
        d = tmp_path / "squads" / slug
        d.mkdir(parents=True)
        (d / "squad.yaml").write_text(f"slug: {slug}\ndeprecated_after: {dep}\n", encoding="utf-8")
    return tmp_path


def _run(script: Path, stdin: str, root: Path) -> subprocess.CompletedProcess:
    env = dict(os.environ)
    env["CLAUDE_PROJECT_DIR"] = str(root)
    return subprocess.run(
        [sys.executable, str(script)], input=stdin, capture_output=True,
        text=True, cwd=str(root), env=env, timeout=60,
    )


def _agent(**tool_input) -> str:
    return json.dumps({"tool_name": "Agent", "tool_input": tool_input})


# ---------------------------------------------------------------- (h)


def _legit_agents() -> list[str]:
    names = {p.qualified_lead_agent for p in NATIVE_PACKS.values()}
    names |= {
        "hydra:engineer", "hydra:judge-cross-vendor", "hydra:judge-same-vendor",
        "hydra:plan-author", "general-purpose",
    }
    return sorted(names)


def test_iolaus_check_allows_legitimate_agents(project):
    agents = _legit_agents()
    assert len(agents) >= 6
    for name in agents:
        r = _run(CHECK, _agent(subagent_type=name, description="d", prompt="p"), project)
        assert r.returncode == 0, (name, r.stderr)


# ---------------------------------------------------------------- (i)


@pytest.mark.parametrize(
    "stdin",
    [
        _agent(subagent_type="oldsquad"),
        _agent(agent="oldsquad"),
        _agent(squad="oldsquad"),
        json.dumps({"tool_name": "Agent", "input": {"subagent_type": "oldsquad"}}),
    ],
)
def test_iolaus_check_denies_deprecated_slug_by_each_field(project, stdin):
    r = _run(CHECK, stdin, project)
    assert r.returncode == 2, (r.returncode, r.stderr)
    assert "Iolaus refused" in r.stderr


# ---------------------------------------------------------------- (j)


def test_iolaus_check_prompt_and_description_never_deny(project):
    for ti in (
        {"subagent_type": "general-purpose", "prompt": "oldsquad"},
        {"subagent_type": "general-purpose", "description": "oldsquad"},
        {"prompt": "oldsquad"},
        {"description": "oldsquad"},
    ):
        r = _run(CHECK, _agent(**ti), project)
        assert r.returncode == 0, (ti, r.stderr)


# ---------------------------------------------------------------- (k)


@pytest.mark.parametrize(
    "stdin",
    ["[]", '"x"', json.dumps({"tool_name": "Agent", "tool_input": "notadict"}), "not json{"],
)
def test_iolaus_check_fails_open_on_malformed(project, stdin):
    r = _run(CHECK, stdin, project)
    assert r.returncode == 0, (r.returncode, r.stderr)


# ---------------------------------------------------------------- (l)


def test_iolaus_check_ignores_non_agent_tools(project):
    payload = json.dumps({"tool_name": "TaskCreate", "tool_input": {"subagent_type": "oldsquad"}})
    r = _run(CHECK, payload, project)
    assert r.returncode == 0, (r.returncode, r.stderr)


# ---------------------------------------------------------------- (m)


def test_iolaus_log_records_agent_description(project):
    r = _run(
        LOG,
        _agent(subagent_type="general-purpose", description="x" * 300, prompt="SECRET PROMPT"),
        project,
    )
    assert r.returncode == 0, r.stderr
    lines = (project / ".hydra" / "iolaus.log").read_text(encoding="utf-8").splitlines()
    lines = [ln for ln in lines if ln.strip()]  # the script writes os.linesep
    assert len(lines) == 1
    rec = json.loads(lines[0])
    assert rec["target"] == "general-purpose"
    assert rec["tool"] == "Agent"
    assert rec["description"] == "x" * 200
    assert "prompt" not in rec
    assert "SECRET PROMPT" not in lines[0]


def test_iolaus_log_non_dict_payload_writes_nothing(project):
    for stdin in ("[]", '"x"', json.dumps({"tool_name": "Agent", "tool_input": "s"})):
        r = _run(LOG, stdin, project)
        assert r.returncode == 0, (stdin, r.stderr)
    assert not (project / ".hydra").exists()
