"""Hydra#92 W2.4 step 1: baseline captures for parallel head legs.

Both fixtures were produced at commit 8ad24e5 by running the capture script
``fixtures/w2_4_attended_step_baseline_S8ad24e5.capture.txt`` (loaded here with
runpy). Each test reruns the real ``cli._cmd_attended_step`` /
``cli._cmd_attended_submit`` flows against a real SqliteSaver file under
``tmp_path`` and compares with the stored fixture.

* identity fixture: zero_head, one_head, one_ready_of_two_heads, engineering.
  Immutable: no later step changes this test or its fixture.
* characterization fixture: three_head_serial. Records today's serial behaviour;
  a later step narrows the comparison.
"""
from __future__ import annotations

import json
import re
import runpy
import tempfile
from pathlib import Path

import pytest

_FIXTURES = Path(__file__).parent / "fixtures"
_CAPTURE = _FIXTURES / "w2_4_attended_step_baseline_S8ad24e5.capture.txt"
_IDENTITY = _FIXTURES / "w2_4_attended_step_baseline_S8ad24e5.json"
_SERIAL = _FIXTURES / "w2_4_three_head_serial_S8ad24e5.json"

_DRIVE = re.compile(r"[A-Za-z]:[\\/]")


@pytest.fixture(scope="module")
def cap() -> dict:
    return runpy.run_path(str(_CAPTURE), run_name="w2_4_baseline_capture")


def _load(path: Path):
    return json.loads(path.read_text(encoding="utf-8"))


def _walk_strings(obj):
    if isinstance(obj, dict):
        for k, v in obj.items():
            yield str(k)
            yield from _walk_strings(v)
    elif isinstance(obj, list):
        for v in obj:
            yield from _walk_strings(v)
    elif isinstance(obj, str):
        yield obj


def _host_action_cwds(obj):
    if isinstance(obj, dict):
        for k, v in obj.items():
            if k == "host_action" and isinstance(v, dict):
                yield v.get("cwd")
            yield from _host_action_cwds(v)
    elif isinstance(obj, list):
        for v in obj:
            yield from _host_action_cwds(v)


def test_baseline_responses_match_capture(cap, tmp_path):
    want = _load(_IDENTITY)
    assert sorted(want) == ["engineering", "one_head", "one_ready_of_two_heads",
                            "zero_head"]
    got = cap["capture_identity"](tmp_path)
    assert got == want


def test_three_head_serial_characterization_matches_capture(cap, tmp_path):
    want = _load(_SERIAL)
    # W2.4: a step now serves every ready head leg, so the comparison against
    # today's serial capture is narrowed. The capture's host is a legacy host:
    # it reads only the top-level host_action, submits it and steps again.
    got = cap["capture_three_head_serial"](tmp_path)
    # every head submit ran at cost 0.0 so later prompts do not depend on spend
    assert len(got["steps"]) == 3 and len(got["submits"]) == 3
    assert len(want["steps"]) == 3 and len(want["submits"]) == 3
    # (i) the first step serves all three legs; minus host_actions it is the capture
    first = dict(got["steps"][0])
    assert len(first.pop("host_actions")) == 3
    assert first == want["steps"][0]
    # (ii) later head steps: top-level leg identity and prompt equal the capture
    for g, w in zip(got["steps"][1:], want["steps"][1:]):
        for key in ("run_id", "leg", "head"):
            assert g[key] == w[key]
        assert g["host_action"]["call_key"] == w["host_action"]["call_key"]
        assert g["host_action"]["prompt"] == w["host_action"]["prompt"]
    # (iii) submit responses
    for g, w in zip(got["submits"], want["submits"]):
        for key in ("ok", "status", "run_id", "already_charged"):
            assert g.get(key) == w.get(key)
    # (iv) the lead step and its prompt
    assert got["lead_step"] == want["lead_step"]
    assert got["lead_prompt"] == want["lead_prompt"]
    assert got["lead_step"]["host_action"]["prompt"] == got["lead_prompt"]
    # (v) the final normalised attended_head_results
    assert got["attended_head_results"] == want["attended_head_results"]
    assert sorted(got["attended_head_results"]) == sorted(
        f"{got['lead_step']['task_id']}:{h}" for h in ("h1", "h2", "h3"))


def test_w2_4_fixtures_contain_no_absolute_paths(tmp_path):
    forbidden = {str(tmp_path), str(tmp_path.resolve()), tempfile.gettempdir()}
    forbidden.discard("")
    for path in (_IDENTITY, _SERIAL):
        data = _load(path)
        cwds = list(_host_action_cwds(data))
        assert cwds, path.name
        assert all(c == "<pack_dir>" for c in cwds), (path.name, cwds)
        for s in _walk_strings(data):
            assert not _DRIVE.search(s), (path.name, s[:200])
            assert not s.startswith(("/home/", "/Users/")), (path.name, s[:200])
            for bad in forbidden:
                assert bad not in s, (path.name, bad)
