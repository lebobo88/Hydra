"""Hydra#89 step 2: per-head squad legs in ``host_bridge.begin_squad_stage``.

* the head-less path is byte-identical to the starting commit (fixture
  ``tests/fixtures/begin_squad_stage_headless_S0720869.json`` was captured from
  commit 0720869 BEFORE host_bridge.py was edited; only the temp project root
  was normalised to ``<ROOT>`` and path separators to ``/``);
* each head leg gets its own cursor file, call_key and artifact.

Hydra#91: the ``planning`` case is narrowed to exactly two insertions in the
generated "Plan JSON Schema" section (the ``heads`` field-list line and the
``heads`` JSON-schema property), because Hydra#91 adds ``PlanStep.heads``.
The ``basic`` and ``rich`` cases are unchanged.
"""
from __future__ import annotations

import json
import re as _re
from pathlib import Path
from typing import Any
from uuid import uuid4

import pytest

from hydra_core import host_bridge

FIXTURE = Path(__file__).parent / "fixtures" / "begin_squad_stage_headless_S0720869.json"

# Hydra#91: the ``planning`` case is narrowed to exactly two insertions in each
# generated "Plan JSON Schema" prompt section, because PlanStep.heads is new.
_I1_ANCHOR = "- `rationale` (Union[str, NoneType], optional)"
_I1_LINE = "- `heads` (list[str], optional)"
_I2_ANCHOR = '"model_tier":'
_I2_PROP = '"heads":{"items":{"type":"string"},"title":"Heads","type":"array"},'


def _with_plan_step_heads(case_fixture: Any) -> Any:
    """Return ``case_fixture`` with exactly the I1 and I2 insertions applied to
    each string containing 'Plan JSON Schema' (each anchor exactly once per
    string, exactly 2 such strings)."""
    hits: list[str] = []

    def fix(obj: Any) -> Any:
        if isinstance(obj, dict):
            return {k: fix(v) for k, v in obj.items()}
        if isinstance(obj, list):
            return [fix(v) for v in obj]
        if isinstance(obj, str) and "Plan JSON Schema" in obj:
            assert obj.count(_I1_ANCHOR) == 1
            assert obj.count(_I2_ANCHOR) == 1
            hits.append(obj)
            return (obj.replace(_I1_ANCHOR, _I1_ANCHOR + "\n" + _I1_LINE)
                    .replace(_I2_ANCHOR, _I2_PROP + _I2_ANCHOR))
        return obj

    out = fix(case_fixture)
    assert len(hits) == 2
    return out


class _StubDispatcher:
    def __init__(self) -> None:
        self.calls: list[tuple[str, str, dict[str, Any]]] = []

    def call_mcp(self, server: str, tool: str, args: dict[str, Any],
                 *, squad_id: str | None = None) -> dict[str, Any]:
        self.calls.append((server, tool, args))
        return {"status": "done"}


def _begin(tmp_path: Path, wf: str, *, task_id: str = "task-cs1", **kw: Any) -> dict[str, Any]:
    return host_bridge.begin_squad_stage(
        workflow_id=wf, task_id=task_id, squad_slug="customer-support",
        entrypoint="claude-skill", lead_agent="xenia:support-lead",
        pack_cwd=str(tmp_path / "pack"), request_text="triage the ticket",
        project_root=tmp_path, **kw,
    )


def _norm(obj: Any, root: str) -> Any:
    def fix(s: str) -> str:
        s = s.replace(root, "<ROOT>").replace("\\", "/")
        # The planning prompt lists ``sorted(SCHEMA_REGISTRY)``; other tests in
        # the suite register extra types (e.g. JUDGE_VERDICT) in-process, so
        # that single line is order-dependent and is masked. Nothing else in
        # the prompt is normalised.
        return _re.sub(
            r"(### Allowed PlanStep\.envelope_type values\n)[^\n]*", r"\1<TYPES>", s)

    if isinstance(obj, dict):
        return {k: _norm(v, root) for k, v in obj.items()}
    if isinstance(obj, list):
        return [_norm(v, root) for v in obj]
    if isinstance(obj, str):
        return fix(obj)
    return obj


@pytest.mark.parametrize("case", ["basic", "rich", "planning"])
def test_headless_begin_squad_stage_byte_identical_to_starting_commit(
    tmp_path: Path, case: str,
) -> None:
    fx = json.loads(FIXTURE.read_text(encoding="utf-8"))[case]
    if case == "planning":
        fx = _with_plan_step_heads(fx)
    kw = dict(fx["kwargs"])
    res = host_bridge.begin_squad_stage(project_root=str(tmp_path), **kw)
    root = str(tmp_path)
    cursor = json.loads(Path(res["cursor_path"]).read_text(encoding="utf-8"))
    assert _norm(cursor, root) == _norm(fx["cursor"], root)
    assert _norm(res, root) == _norm(fx["step_result"], root)
    assert "head" not in res and "leg" not in res
    assert "head" not in cursor and "leg" not in cursor
    assert "## Head" not in cursor["pending_action"]["prompt"]


def test_four_distinct_cursors_and_call_keys(tmp_path: Path) -> None:
    wf = str(uuid4())
    results = {h: _begin(tmp_path, wf, head=h) for h in ("h1", "h2", "h3")}
    results[None] = _begin(tmp_path, wf)

    files = {Path(r["cursor_path"]) for r in results.values()}
    assert len(files) == 4
    attended = tmp_path / ".hydra" / wf / "attended"
    assert sorted(p.name for p in attended.glob("*.json")) == [
        "task-cs1--h1.json", "task-cs1--h2.json", "task-cs1--h3.json", "task-cs1.json",
    ]
    keys = {r["host_action"]["call_key"] for r in results.values()}
    assert len(keys) == 4
    for h in ("h1", "h2", "h3"):
        r = results[h]
        assert r["run_id"] == f"task-cs1--{h}"
        assert r["task_id"] == "task-cs1"
        assert r["head"] == h and r["leg"] == "head"
        assert r["host_action"]["call_key"] == f"squad-task-cs1--{h}-0"
        cur = host_bridge.load_cursor(r["cursor_path"])
        assert cur["run_id"] == f"task-cs1--{h}"
        assert cur["head"] == h and cur["leg"] == "head"
        assert f"## Head\nhead: {h}" in cur["pending_action"]["prompt"]
    assert results[None]["run_id"] == "task-cs1"
    assert "head" not in results[None] and "leg" not in results[None]


def test_head_trace_carries_head_and_run_id(tmp_path: Path) -> None:
    wf = str(uuid4())
    _begin(tmp_path, wf, head="h1")
    lines = (tmp_path / ".hydra" / wf / "trace.jsonl").read_text(encoding="utf-8").splitlines()
    ev = [json.loads(x) for x in lines if "squad_stage_started" in x][0]
    payload = ev.get("payload", ev)
    assert payload["head"] == "h1"
    assert payload["run_id"] == "task-cs1--h1"


def test_three_head_legs_persist_three_distinct_artifacts(tmp_path: Path) -> None:
    wf = str(uuid4())
    disp = _StubDispatcher()
    started = {h: _begin(tmp_path, wf, head=h) for h in ("h1", "h2", "h3")}
    started[None] = _begin(tmp_path, wf)
    refs = {}
    for h, st in started.items():
        res = host_bridge.submit_host_result(
            disp, cursor_file=st["cursor_path"],
            call_key=st["host_action"]["call_key"],
            result={"text": f"# artifact {h}"},
        )
        assert res["status"] == "complete"
        refs[h] = res["artifact_ref"]["key"]
    assert len(set(refs.values())) == 4
    art = tmp_path / ".hydra" / wf / "attended" / "artifacts"
    for h in ("h1", "h2", "h3"):
        assert refs[h] == f"attended:artifacts:{wf}/task-cs1--{h}.md"
        assert (art / f"task-cs1--{h}.md").read_text(encoding="utf-8") == f"# artifact {h}"
    assert refs[None] == f"attended:artifacts:{wf}/task-cs1.md"
    assert (art / "task-cs1.md").read_text(encoding="utf-8") == "# artifact None"


def test_native_head_artifacts_keyed_by_leg_run_id(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    from hydra_core import artifact_store
    from hydra_core.schemas import MemoryRef

    written: list[str] = []

    def fake_write(slug: str, rel: str, text: str) -> MemoryRef:
        written.append(rel)
        return MemoryRef(tier="episodic", key=f"native:{slug}:{rel}", summary=rel)

    monkeypatch.setattr(host_bridge, "_has_native_pack", lambda slug: True)
    monkeypatch.setattr(artifact_store, "write_native_artifact", fake_write)
    wf = str(uuid4())
    for h in ("h1", "h2", None):
        st = _begin(tmp_path, wf, **({"head": h} if h else {}))
        host_bridge.submit_host_result(
            _StubDispatcher(), cursor_file=st["cursor_path"],
            call_key=st["host_action"]["call_key"], result={"text": "x"})
    assert written == ["attended/task-cs1--h1.md", "attended/task-cs1--h2.md",
                       "attended/task-cs1.md"]


@pytest.mark.parametrize("task_id,head", [
    ("task-cs1", "Bad Head"),
    ("task-cs1", "a--b"),
    ("task-cs1", ""),
    ("task-cs1-", "h1"),
    ("a--b", "h1"),
    ("", "h1"),
])
def test_invalid_head_or_task_id_raises_and_writes_no_cursor(
    tmp_path: Path, task_id: str, head: str,
) -> None:
    wf = str(uuid4())
    with pytest.raises(ValueError):
        _begin(tmp_path, wf, task_id=task_id, head=head)
    assert not (tmp_path / ".hydra" / wf / "attended").exists() or not list(
        (tmp_path / ".hydra" / wf / "attended").glob("*"))
