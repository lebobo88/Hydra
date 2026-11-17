"""Checks for plugins/hydra/workflows/sibling-audit.js.

T1-T9 read the script as text. T10 and B1-B9 run the script under node with
stand-ins for the Workflow globals (no agents, no network, no daemon); they
skip when node is not installed. T8 and T9 are cheap text checks only: B8 and
B9 are the proof that the guard and the failure logging work.
"""

from __future__ import annotations

import json
import re
import shutil
import subprocess
from pathlib import Path
from typing import Any

import pytest

REPO = Path(__file__).resolve().parents[1]
SCRIPT = REPO / "plugins" / "hydra" / "workflows" / "sibling-audit.js"

NINE_NAMES = [
    "ExecutiveSuite", "Senate", "MarketBliss", "RLM-Creative", "RLM-Gaming",
    "Xenia", "pair-programmer", "AgentSmith", "TheEights",
]
RULE_NAMES = ["READ_ONLY_RULE", "UNTRUSTED_DATA_RULE", "NO_SECRETS_RULE"]
NODE_REASON = "node not installed; workflow behaviour checks skipped"
needs_node = pytest.mark.skipif(shutil.which("node") is None, reason=NODE_REASON)


def _text() -> str:
    return SCRIPT.read_text(encoding="utf-8")


# --------------------------------------------------------------------------
# Small JavaScript scanning helpers (double-quoted strings and comments only).
# --------------------------------------------------------------------------

def _scan(text: str):
    """Yield (index, char, in_string) skipping comments; strings are marked."""
    i, n = 0, len(text)
    in_str = False
    while i < n:
        c = text[i]
        if in_str:
            yield i, c, True
            if c == "\\" and i + 1 < n:
                i += 1
                yield i, text[i], True
            elif c == '"':
                in_str = False
        else:
            if text.startswith("//", i):
                j = text.find("\n", i)
                i = n if j < 0 else j
                continue
            if text.startswith("/*", i):
                j = text.find("*/", i + 2)
                i = n if j < 0 else j + 2
                continue
            if c == '"':
                in_str = True
                yield i, c, True
            else:
                yield i, c, False
        i += 1


def _match_braces(text: str, start: int) -> str:
    """Return the balanced {...} text whose opening brace is at text[start]."""
    assert text[start] == "{"
    depth = 0
    for i, c, in_str in _scan(text[start:]):
        if in_str:
            continue
        if c == "{":
            depth += 1
        elif c == "}":
            depth -= 1
            if depth == 0:
                return text[start:start + i + 1]
    raise AssertionError("unbalanced braces")


def _meta_text(text: str) -> str:
    assert text.startswith("export const meta = {"), "script must start with export const meta = {"
    return _match_braces(text, text.index("{"))


def _const_literal(text: str, name: str) -> str:
    m = re.search(r"^const " + re.escape(name) + r" = ", text, re.M)
    assert m, f"{name} not defined"
    return _match_braces(text, m.end())


def _function_body(text: str, name: str) -> str:
    m = re.search(r"function " + re.escape(name) + r"\([^)]*\)\s*\{", text)
    assert m, f"function {name} not found"
    return _match_braces(text, m.end() - 1)


def _rule_text(text: str, name: str) -> str:
    m = re.search(r'^const ' + name + r' = ("(?:[^"\\]|\\.)*");', text, re.M)
    assert m, f"{name} must be a plain double-quoted string literal"
    return json.loads(m.group(1))


# --------------------------------------------------------------------------
# Static tests
# --------------------------------------------------------------------------

def _meta_value(text: str) -> dict[str, Any]:
    meta = _meta_text(text)
    # Split into string and non-string segments.
    segs: list[tuple[bool, str]] = []
    cur, cur_str = "", False
    for _, c, in_str in _scan(meta):
        if in_str != cur_str and cur:
            segs.append((cur_str, cur))
            cur = ""
        cur_str = in_str
        cur += c
    if cur:
        segs.append((cur_str, cur))
    out = []
    for is_str, seg in segs:
        if is_str:
            out.append(seg)
            continue
        for bad in ("(", ")", "...", "${", "`", "'"):
            assert bad not in seg, f"meta must be a pure literal; found {bad!r} outside a string"
        seg = re.sub(r"([A-Za-z_]\w*)(\s*:)", r'"\1"\2', seg)
        out.append(seg)
    joined = "".join(out)
    joined = re.sub(r",(\s*[}\]])", r"\1", joined)
    return json.loads(joined)


def test_meta_is_pure_literal_with_required_fields() -> None:
    meta = _meta_value(_text())
    assert meta["name"] == "sibling-audit"
    assert isinstance(meta["description"], str) and meta["description"].strip()
    phases = meta.get("phases", [])
    assert isinstance(phases, list) and phases
    for p in phases:
        assert isinstance(p, dict) and isinstance(p.get("title"), str) and p["title"]
        assert set(p) <= {"title", "detail"}
        assert isinstance(p.get("detail", ""), str)


def test_meta_phase_titles_match_phase_calls() -> None:
    text = _text()
    titles = {p["title"] for p in _meta_value(text)["phases"]}
    body = text[len(_meta_text(text)):]
    used = set(re.findall(r'\bphase\(\s*"([^"]+)"\s*\)', body))
    used |= set(re.findall(r'\bphase\s*:\s*"([^"]+)"', body))
    assert titles == used


def test_no_hardcoded_repo_paths() -> None:
    text = _text()
    for name in NINE_NAMES:
        assert name not in text, name
    assert "AiAppDeployments" not in text
    assert not re.search(r"[A-Za-z]:[\\/]", text)
    for frag in ("~/", "/c/", "/Users/"):
        assert frag not in text, frag


FORBIDDEN = [
    r"Date\.now", r"Math\.random", r"new Date\(\s*\)", r"\brequire\(",
    r"^\s*import\b", r"\bimport\s*\(", r"\bprocess\.", r"\bfs\.", r"child_process",
    r"\bfetch\(", r"__dirname", r"\bgate\(", r"\bagentType\b",
    r"wait(?:ing)? for (?:the )?(?:user|operator|human)", r"\buser input\b",
    r"\breadline\b", r"\bstdin\b", r"\bAskUserQuestion\b",
]


def test_forbidden_apis_absent() -> None:
    text = _text()
    for pat in FORBIDDEN:
        assert not re.search(pat, text, re.M), pat


def test_rule_constants_defined_and_non_trivial() -> None:
    text = _text()
    keywords = {
        "READ_ONLY_RULE": ["write", "commit", "install", "test"],
        "UNTRUSTED_DATA_RULE": ["untrusted"],
        "NO_SECRETS_RULE": ["secret"],
    }
    for name, words in keywords.items():
        body = _rule_text(text, name)
        assert len(body) >= 80, name
        for w in words:
            assert w in body, (name, w)


def test_every_agent_call_uses_a_rule_carrying_builder() -> None:
    text = _text()
    assert len(re.findall(r"\bagent\(", text)) == 2
    builders = re.findall(r"\bagent\(\s*(build\w*Prompt)\(", text)
    assert len(builders) == 2 and len(set(builders)) == 2
    for b in builders:
        body = _function_body(text, b)
        for rule in RULE_NAMES:
            assert rule in body, (b, rule)
    assert not re.search(r"=\s*agent\b", text)
    assert not re.search(r"[(,]\s*agent\s*[,)]", text)


def test_report_schema_has_required_sections() -> None:
    text = _text()
    audit = json.loads(_const_literal(text, "AUDIT_REPORT_SCHEMA"))
    verify = json.loads(_const_literal(text, "VERIFY_SCHEMA"))

    def check_required(node: Any) -> None:
        if isinstance(node, dict):
            if "required" in node:
                assert set(node["required"]) <= set(node.get("properties", {}))
            for v in node.values():
                check_required(v)
        elif isinstance(node, list):
            for v in node:
                check_required(v)

    for schema in (audit, verify):
        assert schema["type"] == "object"
        check_required(schema)
    props = audit["properties"]
    for sec in ("repo_id", "plugin_manifest", "inventory", "mcp_servers",
                "hydra_reachability", "findings", "root_causes", "alignment_plan"):
        assert sec in props, sec
    assert {"present", "path", "marketplace", "user_scope_install", "symlinks",
            "dangling_links"} <= set(props["plugin_manifest"]["properties"])
    assert {"agents", "skills", "commands", "hooks", "counts",
            "collisions_with_hydra"} <= set(props["inventory"]["properties"])
    assert {"name", "tools", "gateway_reachability"} <= set(props["mcp_servers"]["items"]["properties"])
    assert {"entrypoint", "squad_yaml", "native_packs_mapping"} <= set(props["hydra_reachability"]["properties"])
    assert {"id", "title", "file", "line", "evidence", "severity"} <= set(props["findings"]["items"]["properties"])
    item = verify["properties"]["verified_findings"]["items"]
    assert {"finding_id", "status", "recheck_evidence"} <= set(item["properties"])
    assert set(item["properties"]["status"]["enum"]) == {"confirmed", "refuted", "unverified"}


def test_args_guard_present() -> None:
    text = _text()
    assert "Array.isArray(args)" in text
    assert re.search(r"args\.length", text)
    assert re.search(r'throw new Error\(\s*"[^"]*\bargs\b', text)


def test_uses_pipeline_and_logs_failures() -> None:
    text = _text()
    assert re.search(r"\bpipeline\(", text)
    assert re.search(r"\blog\(", text)


# --------------------------------------------------------------------------
# Shared node wrapper (T10 and B1-B9)
# --------------------------------------------------------------------------

_HARNESS_HEAD = r"""
const SCENARIO = __SCENARIO__;
const agentCalls = [];
const logCalls = [];
const phaseCalls = [];

function clone(v) { return v === undefined ? undefined : JSON.parse(JSON.stringify(v)); }

async function pipeline(items, ...stages) {
  return Promise.all(items.map(async (item, index) => {
    let prev = item;
    try {
      for (const stage of stages) { prev = await stage(prev, item, index); }
      return prev;
    } catch (e) {
      return null;
    }
  }));
}
async function parallel(thunks) {
  return Promise.all(thunks.map(async (t) => { try { return await t(); } catch (e) { return null; } }));
}
function phase(title) { phaseCalls.push(title); }
function log(message) { logCalls.push(String(message)); }

async function agent(prompt, opts) {
  agentCalls.push({prompt, opts: clone(opts)});
  let best = null;
  for (const p of SCENARIO.paths) {
    if (prompt.includes(p) && (best === null || p.length > best.length)) { best = p; }
  }
  const table = (SCENARIO.by_phase[opts.phase] || {});
  let value;
  if (best !== null && Object.prototype.hasOwnProperty.call(table, best)) { value = table[best]; }
  else { value = SCENARIO.defaults[opts.phase]; }
  if (value !== null && typeof value === "object" && value.__throw__) { throw new Error(value.__throw__); }
  return clone(value);
}

"""

_HARNESS_TAIL = r"""

const ARGS = (SCENARIO.args !== null && typeof SCENARIO.args === "object" && SCENARIO.args.__undefined__)
  ? undefined : SCENARIO.args;
let out;
try {
  const result = await __workflow(agent, pipeline, parallel, phase, log, ARGS);
  out = {result: result === undefined ? null : result};
} catch (e) {
  out = {error: String(e && e.message ? e.message : e)};
}
out.agent_calls = agentCalls;
out.log_calls = logCalls;
out.phase_calls = phaseCalls;
console.log(JSON.stringify(out));
"""


def _wrapper_source(scenario: dict[str, Any]) -> str:
    text = _text()
    assert text.startswith("export const meta")
    body = text.replace("export const meta", "const meta", 1)
    head = _HARNESS_HEAD.replace("__SCENARIO__", json.dumps(scenario))
    fn = ("async function __workflow(agent, pipeline, parallel, phase, log, args) {\n"
          + body + "\n}\n")
    return head + fn + _HARNESS_TAIL


def _write_wrapper(tmp_path: Path, scenario: dict[str, Any]) -> Path:
    path = tmp_path / "sibling_audit_wrapper.mjs"
    path.write_text(_wrapper_source(scenario), encoding="utf-8")
    return path


def _node() -> str:
    exe = shutil.which("node")
    assert exe
    return exe


@needs_node
def test_script_parses_as_javascript(tmp_path: Path) -> None:
    wrapper = _write_wrapper(tmp_path, _scenario())
    proc = subprocess.run(
        [_node(), "--check", str(wrapper)],
        capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=60,
    )
    assert proc.returncode == 0, proc.stderr


# --------------------------------------------------------------------------
# Behaviour tests
# --------------------------------------------------------------------------

REPOS = [
    {"id": f"repo-{n}", "name": f"Repo {n}", "path": f"/stub/repo-{n}"} for n in range(1, 10)
]
UNDEFINED = {"__undefined__": True}


def _finding(i: int) -> dict[str, Any]:
    return {"id": f"F{i}", "title": f"finding {i}", "file": "a.txt", "line": i,
            "evidence": "e", "severity": "low"}


def _report(count: int = 3) -> dict[str, Any]:
    return {"repo_id": "report-says", "findings": [_finding(i) for i in range(1, count + 1)]}


def _verdicts(count: int = 3) -> dict[str, Any]:
    return {"verified_findings": [
        {"finding_id": f"F{i}", "status": "confirmed", "recheck_evidence": f"seen {i}"}
        for i in range(1, count + 1)
    ]}


def _scenario(*, args: Any = None, audit: dict[str, Any] | None = None,
              verify: dict[str, Any] | None = None) -> dict[str, Any]:
    return {
        "args": REPOS if args is None else args,
        "paths": [r["path"] for r in REPOS],
        "by_phase": {"Audit": audit or {}, "Verify": verify or {}},
        "defaults": {"Audit": _report(), "Verify": _verdicts()},
    }


def _run(tmp_path: Path, scenario: dict[str, Any]) -> dict[str, Any]:
    wrapper = _write_wrapper(tmp_path, scenario)
    proc = subprocess.run(
        [_node(), str(wrapper)],
        capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=60,
    )
    assert proc.returncode == 0, proc.stderr
    return json.loads(proc.stdout.strip().splitlines()[-1])


def _phase_calls(out: dict[str, Any], phase: str, repo: dict[str, str] | None = None) -> list[dict[str, Any]]:
    calls = [c for c in out["agent_calls"] if c["opts"]["phase"] == phase]
    if repo is not None:
        calls = [c for c in calls if repo["path"] in c["prompt"]]
    return calls


def _by_id(out: dict[str, Any]) -> dict[str, dict[str, Any]]:
    return {e["repo_id"]: e for e in out["result"]}


@needs_node
def test_one_entry_per_input_in_order(tmp_path: Path) -> None:
    ids = [r["id"] for r in REPOS]
    clean = _run(tmp_path, _scenario())
    assert [e["repo_id"] for e in clean["result"]] == ids
    mixed = _run(tmp_path, _scenario(
        audit={"/stub/repo-3": None, "/stub/repo-7": {"__throw__": "boom"}},
        verify={"/stub/repo-5": None},
    ))
    assert len(mixed["result"]) == 9
    assert [e["repo_id"] for e in mixed["result"]] == ids


@needs_node
def test_verify_skipped_after_failed_audit(tmp_path: Path) -> None:
    out = _run(tmp_path, _scenario(audit={"/stub/repo-4": None}))
    entry = _by_id(out)["repo-4"]
    assert entry == {"repo_id": "repo-4", "status": "failed", "stage": "audit"}
    assert _phase_calls(out, "Verify", REPOS[3]) == []
    assert len(out["agent_calls"]) == 17


@needs_node
def test_verify_failure_keeps_audit(tmp_path: Path) -> None:
    out = _run(tmp_path, _scenario(verify={"/stub/repo-6": None}))
    entry = _by_id(out)["repo-6"]
    assert entry["status"] == "failed" and entry["stage"] == "verify"
    assert entry["repo_id"] == "repo-6"
    findings = entry["report"]["findings"]
    assert [f["id"] for f in findings] == ["F1", "F2", "F3"]
    assert all(f["status"] == "unverified" for f in findings)
    assert entry["report"]["repo_id"] == "report-says"


@needs_node
def test_thrown_stage_attributed(tmp_path: Path) -> None:
    out = _run(tmp_path, _scenario(
        audit={"/stub/repo-2": {"__throw__": "boom"}},
        verify={"/stub/repo-8": {"__throw__": "boom"}},
    ))
    by_id = _by_id(out)
    assert by_id["repo-2"] == {"repo_id": "repo-2", "status": "failed", "stage": "unknown"}
    assert by_id["repo-8"] == {"repo_id": "repo-8", "status": "failed", "stage": "unknown"}
    assert len(out["result"]) == 9


@needs_node
def test_every_finding_preserved(tmp_path: Path) -> None:
    verdicts = {"verified_findings": [
        {"finding_id": "F1", "status": "confirmed", "recheck_evidence": "seen 1"},
        {"finding_id": "F2", "status": "refuted", "recheck_evidence": "seen 2"},
        {"finding_id": "F99", "status": "confirmed", "recheck_evidence": "ghost"},
    ]}
    out = _run(tmp_path, _scenario(verify={"/stub/repo-1": verdicts}))
    entry = _by_id(out)["repo-1"]
    assert entry["status"] == "verified"
    findings = entry["report"]["findings"]
    assert [f["id"] for f in findings] == ["F1", "F2", "F3"]
    assert [f["status"] for f in findings] == ["confirmed", "refuted", "unverified"]
    assert findings[0]["recheck_evidence"] == "seen 1"
    assert findings[2]["recheck_evidence"] is None
    assert "F99" not in json.dumps(entry)
    assert len(out["log_calls"]) == 1
    assert "F99" in out["log_calls"][0] and "repo-1" in out["log_calls"][0]


@needs_node
def test_repo_identity_from_original_item(tmp_path: Path) -> None:
    audit = {r["path"]: {"repo_id": "wrong-id", "findings": [_finding(1)]} for r in REPOS}
    out = _run(tmp_path, _scenario(audit=audit, verify={"/stub/repo-2": None}))
    assert [e["repo_id"] for e in out["result"]] == [r["id"] for r in REPOS]
    assert _by_id(out)["repo-1"]["report"]["repo_id"] == "wrong-id"


@needs_node
def test_prompts_carry_rules_at_runtime(tmp_path: Path) -> None:
    text = _text()
    rules = [_rule_text(text, n) for n in RULE_NAMES]
    titles = {p["title"] for p in _meta_value(text)["phases"]}
    out = _run(tmp_path, _scenario())
    assert len(out["agent_calls"]) == 18
    for call in out["agent_calls"]:
        prompt = call["prompt"]
        for rule in rules:
            assert rule in prompt
        matches = [r for r in REPOS if r["path"] in prompt]
        assert len(matches) == 1
        assert matches[0]["name"] in prompt
        assert isinstance(call["opts"].get("schema"), dict)
        assert call["opts"].get("phase") in titles


@needs_node
@pytest.mark.parametrize("bad_args", [
    UNDEFINED,
    None,
    [],
    [{"id": "repo-1", "name": "Repo 1"}],
    [{"id": 1, "name": "Repo 1", "path": "/stub/repo-1"}],
])
def test_args_guard_rejects(tmp_path: Path, bad_args: Any) -> None:
    scenario = _scenario()
    scenario["args"] = bad_args
    out = _run(tmp_path, scenario)
    assert "error" in out and "args" in out["error"]
    assert out["agent_calls"] == []


@needs_node
def test_failures_logged(tmp_path: Path) -> None:
    out = _run(tmp_path, _scenario(
        audit={"/stub/repo-2": None, "/stub/repo-8": {"__throw__": "boom"}},
        verify={"/stub/repo-5": None},
    ))
    expected = {"repo-2": "audit", "repo-5": "verify", "repo-8": "unknown"}
    assert len(out["log_calls"]) == len(expected)
    for repo_id, stage in expected.items():
        hits = [m for m in out["log_calls"] if repo_id in m]
        assert len(hits) == 1, repo_id
        assert stage in hits[0]
