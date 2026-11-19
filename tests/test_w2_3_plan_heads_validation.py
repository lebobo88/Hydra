"""Hydra#91 step 2: ``PlanStep.heads`` validated at ingest.

``hydra_core.ingest`` is the only writer of ``state.plan_ref`` (pinned below by
an AST detector), so rejecting a bad plan in its PLAN branch means no
unvalidated plan can reach ``materialise_plan_steps``. Head resolution here goes
only through a monkeypatched resolver, an injected ``discover`` callable, or
``SquadPack`` objects built in the test.
"""
from __future__ import annotations

import ast
import json
import runpy
from pathlib import Path
from typing import Any
from uuid import uuid4

import pytest

from hydra_core import head_registry
from hydra_core.head_registry import HeadResolution
from hydra_core.ingest import dispatch_ingested_envelopes
from hydra_core.plan_artifact import render_plan_html
from hydra_core.plan_heads import validate_plan_heads
from hydra_core.schemas import Plan
from hydra_core.squad_loader import SquadPack
from hydra_core.state import HydraState
from hydra_core.supervisor import materialise_plan_steps

_ROOT = Path(__file__).resolve().parents[1]
_FIXTURES = Path(__file__).parent / "fixtures"
_CAPTURE = _FIXTURES / "w2_3_headless_ingest_S66e5c29.capture.txt"
_FIXTURE = _FIXTURES / "w2_3_headless_ingest_S66e5c29.json"


class _Disp:
    def __init__(self, root: Path) -> None:
        self.project_root = root


def _pack(slug: str, entrypoint: str = "claude-native") -> SquadPack:
    return SquadPack(slug=slug, name=slug, description=slug, entrypoint=entrypoint)


def _packs() -> dict[str, SquadPack]:
    return {
        "executive": _pack("executive"),
        "engineering": _pack("engineering", "mcp"),
        "planning": _pack("planning"),
    }


def _res(rule: str, error: str | None = None) -> HeadResolution:
    return HeadResolution(squad="s", head="h", plugin="p", agent_name="a",
                          qualified_name="p:a", agent_path=None, rule=rule,  # type: ignore[arg-type]
                          error=error)


def _resolver_known(*known: str):
    def resolve(squad: str, head: str, *, packs: Any = None) -> HeadResolution:
        if head in known:
            return _res("slug")
        return _res("unresolved", f"head {head!r} not declared by squad {squad!r}")
    return resolve


def _plan_dict(wf: Any, steps: list[dict[str, Any]]) -> dict[str, Any]:
    return {
        "id": str(uuid4()), "type": "PLAN", "origin_squad": "planning",
        "target_squad": "hydra", "workflow_id": str(wf), "rigor": "standard",
        "goal_restatement": "ship it", "summary": "ship it", "plan_revision": 1,
        "steps": steps,
    }


def _step(step_id: str = "s1", squad: str = "executive", **kw: Any) -> dict[str, Any]:
    base: dict[str, Any] = {
        "step_id": step_id, "target_squad": squad, "envelope_type": "DEV_TASK",
        "description": "d", "estimated_budget_usd": 1.0, "rationale": "r",
    }
    base.update(kw)
    return base


def _ingest(monkeypatch, tmp_path, steps, packs=None):
    monkeypatch.setenv("HYDRA_PLAN_PHASE", "1")
    state = HydraState(root_goal="x")
    out = dispatch_ingested_envelopes(
        state, [_plan_dict(state.workflow_id, steps)],
        packs=_packs() if packs is None else packs, dispatcher=_Disp(tmp_path))
    return state, out


def _assert_rejected(state, out, tmp_path, *needles: str) -> None:
    item = out.items[0]
    assert item.status == "failed"
    for n in needles:
        assert n in item.detail, (n, item.detail)
    assert "plan_ref" not in out.plan_patch
    assert "plan_status" not in out.plan_patch
    assert not out.plan_patch
    assert not (tmp_path / "docs").exists()
    assert not list(tmp_path.rglob("*.html"))
    assert not materialise_plan_steps(state).get("tasks")


# --------------------------------------------------------------------------- #
# ingest rejections
# --------------------------------------------------------------------------- #

def test_unknown_head_rejects_plan_naming_step_and_head(monkeypatch, tmp_path):
    monkeypatch.setattr(head_registry, "resolve_head", _resolver_known("known"))
    state, out = _ingest(monkeypatch, tmp_path,
                         [_step("step-x", heads=["known", "ghost-head"])])
    _assert_rejected(state, out, tmp_path, "step-x", "ghost-head",
                     "does not resolve")
    assert out.items[0].errors[0]["field"] == "steps[0].heads"


def test_duplicate_head_in_step_rejected(monkeypatch, tmp_path):
    monkeypatch.setattr(head_registry, "resolve_head", _resolver_known("dup"))
    state, out = _ingest(monkeypatch, tmp_path,
                         [_step("step-d", heads=["dup", "dup"])])
    _assert_rejected(state, out, tmp_path, "step-d", "repeats head 'dup'")


def test_heads_on_planning_squad_rejected_naming_step_and_head(monkeypatch, tmp_path):
    monkeypatch.setattr(head_registry, "resolve_head", _resolver_known("a", "b"))
    state, out = _ingest(monkeypatch, tmp_path,
                         [_step("step-p", "planning", heads=["a", "b"])])
    _assert_rejected(state, out, tmp_path, "step-p", "head 'a'", "head 'b'",
                     "squad 'planning' cannot run head legs")


def test_heads_on_non_claude_native_squad_rejected_naming_step_and_head(
        monkeypatch, tmp_path):
    monkeypatch.setattr(head_registry, "resolve_head", _resolver_known("a", "b"))
    state, out = _ingest(monkeypatch, tmp_path,
                         [_step("step-e", "engineering", heads=["a", "b"])])
    _assert_rejected(state, out, tmp_path, "step-e", "head 'a'", "head 'b'",
                     "squad 'engineering' (entrypoint 'mcp') cannot run head legs")
    state, out = _ingest(monkeypatch, tmp_path,
                         [_step("step-m", "nonexistent", heads=["a"])])
    assert "(entrypoint 'missing')" in out.items[0].detail


def test_valid_heads_accepted_and_plan_ref_preserves_order(monkeypatch, tmp_path):
    monkeypatch.setattr(head_registry, "resolve_head",
                        _resolver_known("h3", "h1", "h2"))
    state, out = _ingest(monkeypatch, tmp_path,
                         [_step("step-v", heads=["h3", "h1", "h2"])])
    assert out.items[0].status == "drafted", out.items[0].detail
    ref = out.plan_patch["plan_ref"]
    assert ref["steps"][0]["heads"] == ["h3", "h1", "h2"]
    # an accepted plan must materialise without raising, carrying the order
    approved = HydraState(
        root_goal="x", workflow_id=state.workflow_id, plan_ref=ref,
        plan_status="judged", plan_revision=1, phase="approval",
        hitl_history=[{"gate_node": "plan_gate", "resolution": "approve",
                       "plan_revision": 1}])
    tasks = materialise_plan_steps(approved)["tasks"]
    assert tasks[0].heads == ["h3", "h1", "h2"]


def test_invalid_head_slug_rejected_at_ingest(monkeypatch, tmp_path):
    monkeypatch.setattr(head_registry, "resolve_head",
                        _resolver_known("Bad_Slug", "a--b", "ok"))
    state, out = _ingest(monkeypatch, tmp_path,
                         [_step("step-s", heads=["Bad_Slug", "a--b", "ok"])])
    _assert_rejected(state, out, tmp_path, "step 'step-s' head 'Bad_Slug' is not a "
                     "valid head slug:", "head 'a--b' is not a valid head slug:")


# --------------------------------------------------------------------------- #
# head-less identity
# --------------------------------------------------------------------------- #

def test_headless_ingest_identical_to_start_capture(monkeypatch, tmp_path):
    monkeypatch.setenv("HYDRA_PLAN_PHASE", "1")
    cap = runpy.run_path(str(_CAPTURE), run_name="w2_3_ingest_capture")["capture"]
    got = json.loads(json.dumps(cap(tmp_path), sort_keys=True))
    want = json.loads(_FIXTURE.read_text(encoding="utf-8"))
    assert got["items"] == want["items"]
    assert got["plan_patch"] == want["plan_patch"]
    assert got["plan_ref"] == want["plan_ref"]
    assert got["html_files"] == want["html_files"]
    assert got["html"] == want["html"]


def test_headless_plan_never_calls_discover_or_resolver(monkeypatch, tmp_path):
    def boom(*a: Any, **k: Any):
        raise AssertionError("must not be called for a head-less plan")

    monkeypatch.setattr(head_registry, "resolve_head", boom)
    monkeypatch.setattr("hydra_core.plan_heads.discover_squads", boom)
    plan = Plan.model_validate(_plan_dict(uuid4(), [_step("a"), _step("b")]))
    assert validate_plan_heads(plan, discover=boom, resolver=boom) == []
    assert validate_plan_heads(plan) == []
    state, out = _ingest(monkeypatch, tmp_path, [_step("a"), _step("b")])
    assert out.items[0].status == "drafted"


# --------------------------------------------------------------------------- #
# failure contract
# --------------------------------------------------------------------------- #

def _plan_with_heads() -> Plan:
    return Plan.model_validate(_plan_dict(uuid4(), [
        _step("s1", heads=["a", "b"]), _step("s2", heads=["c"]), _step("s3")]))


def test_discovery_exception_rejects_naming_step_and_head_without_raising(
        monkeypatch, tmp_path):
    def bad_discover():
        raise RuntimeError("yaml exploded " + "x" * 400)

    errs = validate_plan_heads(_plan_with_heads(), discover=bad_discover)
    assert [e["field"] for e in errs] == [
        "steps[0].heads", "steps[0].heads", "steps[1].heads"]
    for e, (sid, head) in zip(errs, [("s1", "a"), ("s1", "b"), ("s2", "c")]):
        assert f"step '{sid}' head '{head}' could not be checked: squad pack " \
               "discovery failed (RuntimeError: yaml exploded" in e["msg"]
    assert len(errs[0]["msg"].split("RuntimeError: ")[1]) <= 200 + 2

    # through ingest: patched discovery used when ingest-level packs are absent
    monkeypatch.setattr("hydra_core.plan_heads.discover_squads", lambda *_a: bad_discover())
    monkeypatch.setenv("HYDRA_PLAN_PHASE", "1")
    state = HydraState(root_goal="x")
    # ingest passes its own packs; simulate a discovery fault via a non-mapping
    out = dispatch_ingested_envelopes(
        state, [_plan_dict(state.workflow_id, [_step("s9", heads=["zz"])])],
        packs=None, dispatcher=_Disp(tmp_path))  # type: ignore[arg-type]
    _assert_rejected(state, out, tmp_path, "s9", "zz",
                     "squad pack discovery failed")


def test_resolver_exception_rejects_that_head_and_checks_the_rest():
    def resolver(squad: str, head: str, *, packs: Any = None) -> HeadResolution:
        if head == "boom":
            raise ValueError("kaboom")
        return _res("unresolved", "nope")

    plan = Plan.model_validate(_plan_dict(uuid4(), [
        _step("s1", heads=["boom", "second"])]))
    errs = validate_plan_heads(plan, packs=_packs(), resolver=resolver)
    assert len(errs) == 2
    assert "step 's1' head 'boom' could not be checked: head resolver failed " \
           "(ValueError: kaboom)" in errs[0]["msg"]
    assert "step 's1' head 'second' does not resolve for squad 'executive': nope" \
        in errs[1]["msg"]


@pytest.mark.parametrize("kwargs", [
    pytest.param(dict(discover=lambda: (_ for _ in ()).throw(RuntimeError("r"))),
                 id="discover-raises"),
    pytest.param(dict(discover=lambda: 5), id="discover-non-mapping"),
    pytest.param(dict(packs=_packs(),
                      resolver=lambda *a, **k: (_ for _ in ()).throw(ValueError("v"))),
                 id="resolver-raises"),
    pytest.param(dict(packs=_packs(), resolver=lambda *a, **k: object()),
                 id="resolver-no-rule"),
])
def test_validate_plan_heads_never_raises(kwargs):
    plan = Plan.model_validate(_plan_dict(uuid4(), [_step("s1", heads=["a"])]))
    errs = validate_plan_heads(plan, **kwargs)
    assert errs and all(set(e) == {"field", "msg"} for e in errs)
    assert all("s1" in e["msg"] and "'a'" in e["msg"] for e in errs)


# --------------------------------------------------------------------------- #
# plan_ref single-writer proof (AST, not text search)
# --------------------------------------------------------------------------- #

def _is_const(node: ast.AST, value: str) -> bool:
    return isinstance(node, ast.Constant) and node.value == value


def _holds_form_i_or_ii(node: ast.AST) -> bool:
    for sub in ast.walk(node):
        if isinstance(sub, ast.Dict) and any(
                k is not None and _is_const(k, "plan_ref") for k in sub.keys):
            return True
        if isinstance(sub, ast.keyword) and sub.arg == "plan_ref":
            return True
    return False


def _plan_ref_writes(tree: ast.AST) -> list[tuple[int, str]]:
    """Every STATE WRITE of ``plan_ref``: forms (i)-(v) of the step spec.

    A class-body ``AnnAssign`` with a ``Name`` target (the field declaration)
    is a declaration, not a write, and is not flagged.
    """
    hits: list[tuple[int, str]] = []
    for n in ast.walk(tree):
        if isinstance(n, ast.Dict) and any(
                k is not None and _is_const(k, "plan_ref") for k in n.keys):
            hits.append((n.lineno, "i dict-key"))
        elif isinstance(n, ast.keyword) and n.arg == "plan_ref":
            hits.append((n.value.lineno, "ii call-keyword"))
        elif isinstance(n, (ast.Assign, ast.AugAssign, ast.AnnAssign)):
            targets = n.targets if isinstance(n, ast.Assign) else [n.target]
            for t in targets:
                for e in ast.walk(t):
                    if isinstance(e, ast.Subscript) and _is_const(e.slice, "plan_ref"):
                        hits.append((n.lineno, "iii subscript-assign"))
                    elif isinstance(e, ast.Attribute) and e.attr == "plan_ref":
                        hits.append((n.lineno, "iv attribute-assign"))
        if isinstance(n, ast.Call):
            f = n.func
            if (isinstance(f, ast.Attribute) and f.attr == "setdefault"
                    and n.args and _is_const(n.args[0], "plan_ref")):
                hits.append((n.lineno, "v setdefault"))
            if ((isinstance(f, ast.Attribute) and f.attr == "update")
                    or (isinstance(f, ast.Name) and f.id == "dict")) \
                    and _holds_form_i_or_ii(n):
                hits.append((n.lineno, "v update/dict"))
    return hits


def _enclosing_def(tree: ast.Module, lineno: int) -> str | None:
    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) \
                and node.lineno <= lineno <= (node.end_lineno or node.lineno):
            return node.name
    return None


# (relative path, enclosing top-level function) -> justification. EMPTY at the
# Hydra#91 start commit: the detector found exactly one write, in ingest.py.
_ALLOWED_WRITES: dict[tuple[str, str], str] = {}


def test_plan_ref_single_writer_is_ingest():
    found: list[tuple[str, int, str, str | None]] = []
    for path in sorted((_ROOT / "hydra_core").rglob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        rel = path.relative_to(_ROOT).as_posix()
        for lineno, form in _plan_ref_writes(tree):
            found.append((rel, lineno, form, _enclosing_def(tree, lineno)))
    assert found, "detector found no plan_ref write at all: it is broken"
    for rel, lineno, form, fn in found:
        if (rel, fn or "") in _ALLOWED_WRITES:
            continue
        assert (rel, fn) == ("hydra_core/ingest.py", "dispatch_ingested_envelopes"), (
            f"plan_ref written outside the ingest PLAN branch: {rel}:{lineno} "
            f"({form}, in {fn}) -- it could put an unvalidated plan in front of "
            "materialise_plan_steps")


@pytest.mark.parametrize("src,prefix", [
    ("x = {'plan_ref': 1}", "i "),
    ("f(plan_ref=1)", "ii "),
    ("d['plan_ref'] = 1", "iii "),
    ("d['plan_ref'] += 1", "iii "),
    ("s.plan_ref = 1", "iv "),
    ("s.plan_ref += 1", "iv "),
    ("d.update({'plan_ref': 1})", "v "),
    ("d.update(plan_ref=1)", "v "),
    ("d.setdefault('plan_ref', 1)", "v "),
    ("dict(plan_ref=1)", "v "),
    ("dict({'plan_ref': 1})", "v "),
])
def test_plan_ref_write_detector_catches_each_form(src, prefix):
    forms = [form for _, form in _plan_ref_writes(ast.parse(src))]
    assert any(f.startswith(prefix) for f in forms), (src, forms)


def test_plan_ref_detector_ignores_declaration_and_reads():
    decl = "class S:\n    plan_ref: dict | None = None\n    other: int = 1\n"
    assert _plan_ref_writes(ast.parse(decl)) == []
    reads = "a = s.plan_ref\nb = d['plan_ref']\nc = d.get('plan_ref')\n"
    assert _plan_ref_writes(ast.parse(reads)) == []


# --------------------------------------------------------------------------- #
# renderer
# --------------------------------------------------------------------------- #

def test_render_plan_html_shows_heads_in_order():
    plan = Plan.model_validate(_plan_dict(uuid4(), [
        _step("s1", heads=["h3", "h1", "h2"]), _step("s2")]))
    html = render_plan_html(plan)
    assert "heads: h3, h1, h2</p>" in html
    assert html.count("heads:") == 1  # head-less step renders no row
    # the renderer must be safe even though ingest rejects such slugs
    evil = Plan.model_validate(_plan_dict(uuid4(), [
        _step("s1", heads=["<script>alert(1)</script>"])]))
    out = render_plan_html(evil)
    assert "<script>alert(1)</script>" not in out
    assert "&lt;script&gt;alert(1)&lt;/script&gt;" in out
