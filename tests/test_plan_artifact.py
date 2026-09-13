"""P2 — plan artifact writer + renderer tests.

Purely additive, like P0/P1: nothing in the runtime graph calls
`write_repo_artifact` / `hydra_core.plan_artifact` yet. These tests pin the
PROPERTIES the new code must hold (determinism, escape-safety, contract
compliance), not just one instance that happens to pass.
"""
from __future__ import annotations

import html
import json
import os
import re
import shutil
import subprocess
import uuid
from pathlib import Path

import pytest

from hydra_core.artifact_store import (
    ArtifactStoreError,
    write_attended_artifact,
    write_native_artifact,
    write_repo_artifact,
)
from hydra_core.plan_artifact import (
    PlanFigure,
    PlanFigureError,
    plan_slug,
    render_plan_html,
    render_plan_json,
)
from hydra_core.schemas import Plan, PlanStep

WF = uuid.uuid4()


def _plan(**overrides) -> Plan:
    base = dict(
        origin_squad="hydra",
        workflow_id=WF,
        rigor="standard",
        goal_restatement="Ship the plan artifact renderer.",
        summary="Render Plan envelopes to tracked, diffable HTML+JSON.",
        steps=[],
    )
    base.update(overrides)
    return Plan(**base)


def _step(step_id: str, **overrides) -> PlanStep:
    base = dict(
        step_id=step_id,
        target_squad="engineering",
        envelope_type="DEV_TASK",
        description=f"Do the work for {step_id}",
    )
    base.update(overrides)
    return PlanStep(**base)


def _diamond_plan() -> Plan:
    # a -> b, a -> c, b -> d, c -> d  (classic diamond)
    steps = [
        _step("a"),
        _step("b", depends_on=["a"]),
        _step("c", depends_on=["a"]),
        _step("d", depends_on=["b", "c"], estimated_budget_usd=42.0),
    ]
    return _plan(
        steps=steps,
        non_goals=["no full rewrite"],
        open_questions=["which vendor?"],
        risks=["schedule risk"],
        dissents=["reviewer X disagreed on scope"],
    )


# --------------------------------------------------------------------------- #
# write_repo_artifact                                                        #
# --------------------------------------------------------------------------- #


def test_write_repo_artifact_accepts_docs_plans_html(tmp_path):
    ref = write_repo_artifact(tmp_path, "docs/plans/plan-x.html", "<h1>hi</h1>")
    assert (tmp_path / "docs" / "plans" / "plan-x.html").read_text(encoding="utf-8") == "<h1>hi</h1>"
    assert ref.tier == "episodic"


def test_write_repo_artifact_rejects_escape(tmp_path):
    with pytest.raises(ArtifactStoreError):
        write_repo_artifact(tmp_path, "../../escape.html", "no")


def test_write_repo_artifact_rejects_outside_allowed_root(tmp_path):
    (tmp_path / "src").mkdir()
    with pytest.raises(ArtifactStoreError):
        write_repo_artifact(tmp_path, "src/main.py", "no")


def test_write_repo_artifact_rejects_bad_suffix(tmp_path):
    with pytest.raises(ArtifactStoreError):
        write_repo_artifact(tmp_path, "docs/plans/x.png", "no")


def test_write_repo_artifact_rejects_allowed_root_escaping_repo_root(tmp_path):
    # allowed_roots is resolved relative to repo_root; if the result is never
    # checked to actually be UNDER repo_root, an allowed_roots of (".."),
    # or any other out-of-tree value, turns the allow-list into a way to
    # write anywhere with an allowed suffix.
    with pytest.raises(ArtifactStoreError):
        write_repo_artifact(
            tmp_path,
            "docs/plans/x.html",
            "no",
            allowed_roots=("..",),
        )


# --------------------------------------------------------------------------- #
# PlanFigure — relative-path-only guard                                      #
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    "bad_path",
    [
        "https://evil.example/x.png",
        "http://evil.example/x.png",
        "data:image/png;base64,AAA",
        "//evil.example/x.png",
        "/etc/passwd",
        "../../../etc/passwd",
        "assets\\..\\..\\escape.png",
        "C:\\Windows\\System32\\config",
    ],
)
def test_plan_figure_rejects_hostile_relative_path(bad_path):
    with pytest.raises(PlanFigureError):
        PlanFigure(relative_path=bad_path, alt_text="x")


def test_plan_figure_accepts_genuine_relative_path():
    fig = PlanFigure(relative_path="assets/plan-x/architecture.png", alt_text="architecture")
    assert fig.relative_path == "assets/plan-x/architecture.png"


@pytest.mark.parametrize(
    "bad_path",
    [
        "\nhttps://evil.example/x.png",  # leading newline in front of a scheme
        " javascript:alert(1)",  # leading space in front of a scheme
        "assets/plan-x/architec\tture.png",  # embedded tab, otherwise-relative
        "\x01assets/plan-x/architecture.png",  # leading C0 control (not whitespace)
    ],
)
def test_plan_figure_rejects_whitespace_or_control_prefixed_path(bad_path):
    # Browsers trim leading C0 controls and spaces before resolving a URL, so
    # a scheme check performed on the untrimmed string is not enough -- the
    # guard must reject outright rather than trim and re-check.
    with pytest.raises(PlanFigureError):
        PlanFigure(relative_path=bad_path, alt_text="x")


def test_plan_figure_still_accepts_legitimate_nested_path():
    # The whitespace/control guard must not become an over-broad rejection
    # of ordinary paths.
    fig = PlanFigure(
        relative_path="docs/plans/assets/plan-x/diagram.png", alt_text="diagram"
    )
    assert fig.relative_path == "docs/plans/assets/plan-x/diagram.png"


def test_write_native_artifact_still_rejects_html():
    with pytest.raises(ArtifactStoreError):
        write_native_artifact("executive", "attended/plan.html", "no")


def test_write_attended_artifact_still_rejects_html(tmp_path):
    with pytest.raises(ArtifactStoreError):
        write_attended_artifact(tmp_path, "wf1", "plan.html", "no")


# --------------------------------------------------------------------------- #
# plan_slug                                                                  #
# --------------------------------------------------------------------------- #


def test_plan_slug_stable_across_calls():
    wf = uuid.uuid4()
    assert plan_slug("Ship the thing", wf) == plan_slug("Ship the thing", wf)


def test_plan_slug_bounded_length():
    wf = uuid.uuid4()
    slug = plan_slug("x " * 200, wf)
    goal_part, _, suffix = slug.rpartition("-")
    assert len(goal_part) <= 48
    assert re.fullmatch(r"[0-9a-f]{8}", suffix)


def test_plan_slug_differs_for_different_workflows_same_goal():
    wf1, wf2 = uuid.uuid4(), uuid.uuid4()
    assert plan_slug("Ship the thing", wf1) != plan_slug("Ship the thing", wf2)


# --------------------------------------------------------------------------- #
# render_plan_html — determinism + contract                                  #
# --------------------------------------------------------------------------- #


def test_render_plan_html_byte_identical_twice():
    plan = _diamond_plan()
    assert render_plan_html(plan) == render_plan_html(plan)


def test_render_plan_html_byte_identical_with_kwargs_repeated():
    plan = _diamond_plan()
    fig = PlanFigure(relative_path="assets/x/arch.png", alt_text="architecture", step_id="a")
    h1 = render_plan_html(plan, constitution_hash="abc123", judge_verdict="approved", figures=[fig])
    h2 = render_plan_html(plan, constitution_hash="abc123", judge_verdict="approved", figures=[fig])
    assert h1 == h2


def test_render_plan_html_no_document_wrapper_tags():
    html_out = render_plan_html(_diamond_plan()).lower()
    for forbidden in ("<!doctype", "<html", "<head", "<body"):
        assert forbidden not in html_out


def test_render_plan_html_no_data_uri_or_external_resources():
    fig = PlanFigure(relative_path="assets/x/arch.png", alt_text="architecture", step_id="a")
    html_out = render_plan_html(_diamond_plan(), figures=[fig])
    assert "data:" not in html_out
    assert "<script" not in html_out.lower()
    assert "http://" not in html_out
    assert "https://" not in html_out


def test_render_plan_html_emits_no_img_tag_when_no_figures():
    html_out = render_plan_html(_diamond_plan())
    assert "<img" not in html_out


def test_render_plan_html_step_table_has_step_id_first_cell():
    html_out = render_plan_html(_diamond_plan())
    assert '<td>a</td>' in html_out
    # step_id is the first <td> in each row
    for row in re.findall(r"<tr>(.*?)</tr>", html_out, re.DOTALL):
        cells = re.findall(r"<td>(.*?)</td>", row, re.DOTALL)
        if cells:
            assert cells[0] in {"a", "b", "c", "d"}


def test_render_plan_html_escapes_hostile_content():
    plan = _plan(
        goal_restatement="<script>alert(1)</script>",
        steps=[_step("a", description="<img src=x onerror=alert(1)>")],
    )
    html_out = render_plan_html(plan)
    assert "<script>alert(1)</script>" not in html_out
    # The hostile markup must be neutralized to inert escaped text, not left
    # as a live tag -- no unescaped "<img" or "<script" anywhere in output.
    assert "<img src=x" not in html_out
    assert "&lt;img src=x onerror=alert(1)&gt;" in html_out


# --------------------------------------------------------------------------- #
# Mermaid dependency graph == steps' depends_on edge set, exactly            #
# --------------------------------------------------------------------------- #


def _mermaid_edges_by_node_id(html_out: str) -> set[tuple[str, str]]:
    block = re.search(r'<pre class="mermaid">(.*?)</pre>', html_out, re.DOTALL).group(1)
    # A real browser decodes HTML entities (`&quot;` -> `"`, `&gt;` -> `>`)
    # when it extracts the <pre> block's textContent -- entity-decoding
    # happens in HTML parsing, BEFORE Mermaid ever sees the text. A test that
    # scans the still-escaped `block` for `-->` would miss an edge that only
    # becomes live after that decoding step, which is exactly the class of
    # injection this suite exists to catch. Unescape first, like the browser
    # does, then scan for arrows the way Mermaid's own tokenizer would.
    decoded = html.unescape(block)
    return {(m.group(1), m.group(2)) for m in re.finditer(r"(\S+)\s*-->\s*(\S+)", decoded)}


def _node_ids(plan: Plan) -> dict[str, str]:
    # MUST mirror hydra_core.plan_artifact._mermaid_graph's synthetic id
    # scheme exactly (n0, n1, ... in step order) -- that scheme, not the raw
    # step_id, is what the renderer actually emits as Mermaid node ids.
    return {step.step_id: f"n{i}" for i, step in enumerate(plan.steps)}


def _mermaid_edges(html_out: str, plan: Plan) -> set[tuple[str, str]]:
    """Rendered Mermaid edges, translated back from synthetic node ids to
    the `step_id` pairs they represent."""
    node_ids = _node_ids(plan)
    by_synth = {v: k for k, v in node_ids.items()}
    edges = set()
    for src, dst in _mermaid_edges_by_node_id(html_out):
        # Any edge endpoint that isn't a known synthetic node id is exactly
        # the forged-edge failure mode this test exists to catch.
        assert src in by_synth, f"unrecognized mermaid node id {src!r} (possible forged edge)"
        assert dst in by_synth, f"unrecognized mermaid node id {dst!r} (possible forged edge)"
        edges.add((by_synth[src], by_synth[dst]))
    return edges


def _plan_edges(plan: Plan) -> set[tuple[str, str]]:
    return {(dep, step.step_id) for step in plan.steps for dep in step.depends_on}


@pytest.mark.parametrize(
    "steps",
    [
        [_step("a")],  # single isolated node, no edges
        [_step("a"), _step("b", depends_on=["a"])],  # linear chain
        [  # diamond
            _step("a"),
            _step("b", depends_on=["a"]),
            _step("c", depends_on=["a"]),
            _step("d", depends_on=["b", "c"]),
        ],
        [  # fan-out + fan-in with an unrelated isolated node
            _step("root"),
            _step("l1"),
            _step("l2", depends_on=["root"]),
            _step("l3", depends_on=["root"]),
            _step("join", depends_on=["l2", "l3"]),
        ],
    ],
)
def test_mermaid_edges_match_depends_on_exactly(steps):
    plan = _plan(steps=steps)
    html_out = render_plan_html(plan)
    assert _mermaid_edges(html_out, plan) == _plan_edges(plan)


def test_mermaid_node_ids_are_synthetic_not_step_id():
    # The graph's structural identifiers must never be the raw step_id --
    # only the human-readable label may carry it (HTML-escaped).
    plan = _plan(steps=[_step("a"), _step("b", depends_on=["a"])])
    html_out = render_plan_html(plan)
    block = re.search(r'<pre class="mermaid">(.*?)</pre>', html_out, re.DOTALL).group(1)
    assert re.search(r"\bn0\b", block)
    assert re.search(r"\bn1\b", block)
    assert "n0 --> n1" in block


def test_mermaid_step_id_cannot_break_out_of_pre_or_inject_script():
    # A hostile step_id must not close the surrounding <pre> and inject a
    # live <script>. It should appear only as inert, escaped label text.
    plan = _plan(steps=[_step("</pre><script>alert(1)</script>")])
    html_out = render_plan_html(plan)
    assert "<script>alert(1)</script>" not in html_out
    assert "</pre><script>" not in html_out


def test_mermaid_step_id_with_arrow_or_newline_cannot_forge_an_edge():
    # A step_id containing "-->" or a newline must not be able to add or
    # alter an edge -- the rendered edge set (translated through the
    # synthetic node-id scheme) must equal depends_on exactly, no more.
    hostile_id = "x\n--> n0\nevil-node[\"pwned\"]\nn0"
    steps = [_step(hostile_id), _step("b", depends_on=[hostile_id])]
    plan = _plan(steps=steps)
    html_out = render_plan_html(plan)
    # The forged text renders only as inert, HTML-escaped label content
    # (never as a live, unescaped Mermaid statement)...
    assert 'evil-node["pwned"]' not in html_out
    assert html.escape('evil-node["pwned"]', quote=True) in html_out
    # ...and, decisively, the edge set is exactly depends_on -- no forged
    # or altered edges reached the graph regardless of what the label says.
    assert _mermaid_edges(html_out, plan) == _plan_edges(plan)


def test_mermaid_step_id_cannot_close_label_and_append_forged_edge():
    # A step_id crafted to CLOSE the quoted Mermaid label with a literal `"]`
    # and then append a fresh edge statement. HTML-escaping alone does not
    # stop this: a browser decodes `&quot;` back to `"` before Mermaid ever
    # parses the text, so the quote must be gone from the underlying label,
    # not merely re-encoded. The rendered edge set must equal depends_on
    # exactly -- no more, no less -- regardless of what the label attempts.
    hostile_id = 'a"] --> n1\n    evil["pwned'
    steps = [_step(hostile_id), _step("b", depends_on=[hostile_id])]
    plan = _plan(steps=steps)
    html_out = render_plan_html(plan)
    assert _mermaid_edges(html_out, plan) == _plan_edges(plan)
    # And, checking what a browser would actually hand Mermaid (entities
    # decoded), no live `evil["pwned"` node statement leaked out of the
    # quoted label into the graph source.
    block = re.search(r'<pre class="mermaid">(.*?)</pre>', html_out, re.DOTALL).group(1)
    assert 'evil["pwned"' not in html.unescape(block)


def test_mermaid_step_id_with_newline_quote_bracket_brace_does_not_break_label_or_topology():
    # A step_id carrying every character the mermaid-syntax sanitizer targets
    # at once (newline, quote, bracket, brace) must neither break the
    # quoted label out of its node statement nor alter the edge topology.
    hostile_id = 'x\n"y[z]{w}'
    steps = [_step(hostile_id), _step("b", depends_on=[hostile_id])]
    plan = _plan(steps=steps)
    html_out = render_plan_html(plan)
    assert _mermaid_edges(html_out, plan) == _plan_edges(plan)
    block = re.search(r'<pre class="mermaid">(.*?)</pre>', html_out, re.DOTALL).group(1)
    # The label's node statement line must still be a single well-formed
    # `nN["..."]` statement -- the raw quote/bracket/newline never survive
    # into the Mermaid source to split it into multiple statements/lines.
    assert '"y[z]{w}' not in block
    assert re.search(r'n0\["[^\n"]*"\]', block)


def test_hostile_content_escaped_across_narrative_fields():
    hostile = "<script>alert(1)</script>\"'&<>"
    plan = _plan(
        goal_restatement=hostile,
        summary=hostile,
        non_goals=[hostile],
        open_questions=[hostile],
        risks=[hostile],
        dissents=[hostile],
        steps=[
            _step(
                "a",
                description=hostile,
                acceptance_criteria=[hostile],
                rationale=hostile,
            )
        ],
    )
    html_out = render_plan_html(plan, judge_verdict=hostile, approval_record=hostile)
    assert "<script>alert(1)</script>" not in html_out
    escaped = html.escape(hostile, quote=True)
    # Every field that carries the hostile string renders it escaped, never raw.
    assert html_out.count(escaped) >= 8


def test_mermaid_has_no_library_import():
    html_out = render_plan_html(_diamond_plan())
    assert "mermaid.min.js" not in html_out
    assert "cdn" not in html_out.lower()


# --------------------------------------------------------------------------- #
# render_plan_json                                                           #
# --------------------------------------------------------------------------- #


def test_render_plan_json_byte_identical_twice():
    plan = _diamond_plan()
    assert render_plan_json(plan) == render_plan_json(plan)


def test_render_plan_json_round_trips_step_ids():
    plan = _diamond_plan()
    payload = json.loads(render_plan_json(plan))
    assert {s["step_id"] for s in payload["steps"]} == {"a", "b", "c", "d"}


# --------------------------------------------------------------------------- #
# Hook lockstep — docs/plans carve-out (both hooks must agree)               #
# --------------------------------------------------------------------------- #

HYDRA_ROOT = Path(__file__).resolve().parents[1]
HOOKS_DIR = HYDRA_ROOT / "plugins" / "hydra" / "hooks"
_PWSH = shutil.which("pwsh") or shutil.which("powershell")


def _win_root(tmp_path: Path) -> str:
    """Resolve tmp_path to a native Windows path pwsh will recognize."""
    result = subprocess.run(
        [_PWSH, "-NoProfile", "-Command", f"(Resolve-Path -LiteralPath '{tmp_path}').Path"],
        capture_output=True, text=True, timeout=20,
    )
    return result.stdout.strip()


@pytest.mark.skipif(_PWSH is None, reason="pwsh/powershell not on PATH")
def test_direct_write_hook_allows_docs_plans_html(tmp_path):
    root = _win_root(tmp_path)
    target = f"{root}\\docs\\plans\\plan-x.html"
    payload = json.dumps({"tool_name": "Write", "tool_input": {"file_path": target}})
    env = {**os.environ, "HYDRA_ENFORCE_ROUTING": "1", "CLAUDE_PROJECT_DIR": root}
    result = subprocess.run(
        [_PWSH, "-NoProfile", "-File", str(HOOKS_DIR / "hydra-block-direct-write.ps1")],
        input=payload, capture_output=True, text=True, env=env, timeout=20,
    )
    assert result.returncode == 0, result.stderr


@pytest.mark.skipif(_PWSH is None, reason="pwsh/powershell not on PATH")
def test_direct_write_hook_still_blocks_html_outside_docs_plans(tmp_path):
    root = _win_root(tmp_path)
    target = f"{root}\\index.html"
    payload = json.dumps({"tool_name": "Write", "tool_input": {"file_path": target}})
    env = {**os.environ, "HYDRA_ENFORCE_ROUTING": "1", "CLAUDE_PROJECT_DIR": root}
    result = subprocess.run(
        [_PWSH, "-NoProfile", "-File", str(HOOKS_DIR / "hydra-block-direct-write.ps1")],
        input=payload, capture_output=True, text=True, env=env, timeout=20,
    )
    assert result.returncode == 2
    assert "BLOCKED" in result.stderr


@pytest.mark.skipif(_PWSH is None, reason="pwsh/powershell not on PATH")
def test_bash_writes_hook_allows_docs_plans_html(tmp_path):
    root = _win_root(tmp_path)
    # Forward slashes end-to-end (root included): Git Bash consumes an
    # unquoted backslash as an escape character, so an unquoted Windows-style
    # path never reaches the directory a caller meant — see the note on
    # _p() below for the measured detail. Forward slashes are valid on
    # Windows and are what a caller running under Git Bash (the shell the
    # Bash tool actually invokes) would type for an unquoted destination.
    root_fs = root.replace("\\", "/")
    cmd = f"echo hi > {root_fs}/docs/plans/plan-y.html"
    payload = json.dumps({"tool_name": "Bash", "tool_input": {"command": cmd}, "cwd": root})
    env = {**os.environ, "HYDRA_ENFORCE_ROUTING": "1", "CLAUDE_PROJECT_DIR": root}
    result = subprocess.run(
        [_PWSH, "-NoProfile", "-File", str(HOOKS_DIR / "hydra-block-bash-writes.ps1")],
        input=payload, capture_output=True, text=True, env=env, timeout=20,
    )
    assert result.returncode == 0, result.stderr


@pytest.mark.skipif(_PWSH is None, reason="pwsh/powershell not on PATH")
def test_bash_writes_hook_still_blocks_html_outside_docs_plans(tmp_path):
    root = _win_root(tmp_path)
    cmd = f"echo hi > {root}\\index.html"
    payload = json.dumps({"tool_name": "Bash", "tool_input": {"command": cmd}, "cwd": root})
    env = {**os.environ, "HYDRA_ENFORCE_ROUTING": "1", "CLAUDE_PROJECT_DIR": root}
    result = subprocess.run(
        [_PWSH, "-NoProfile", "-File", str(HOOKS_DIR / "hydra-block-bash-writes.ps1")],
        input=payload, capture_output=True, text=True, env=env, timeout=20,
    )
    assert result.returncode == 2
    assert "BLOCKED" in result.stderr


def _run_bash_hook(cmd: str, root: str) -> subprocess.CompletedProcess:
    payload = json.dumps({"tool_name": "Bash", "tool_input": {"command": cmd}, "cwd": root})
    env = {**os.environ, "HYDRA_ENFORCE_ROUTING": "1", "CLAUDE_PROJECT_DIR": root}
    return subprocess.run(
        [_PWSH, "-NoProfile", "-File", str(HOOKS_DIR / "hydra-block-bash-writes.ps1")],
        input=payload, capture_output=True, text=True, env=env, timeout=20,
    )


# Every write-detecting branch of hydra-block-bash-writes.ps1 must reach the
# SAME verdict for the SAME destination: allow docs/plans/x.html, block
# hydra_core/x.py. Before this fix, branches 4a (python open()), 4c
# (shutil), 5 (sed -i), and 7b (here-string) only ever checked the blocked-
# extension pattern against the raw command text and never consulted
# Test-BlockedDest, so they disagreed with branches 1/2/3/6 on the exact
# same docs/plans destination.
#
# Forward slashes, not backslashes: Read-ShellArgument (the hook's
# bash-argument reconstructor) processes an UNQUOTED backslash as an escape
# character, exactly like a real shell would — `\p` collapses to `p`. Git
# Bash (what the Bash tool actually runs) does the same: an unquoted
# Windows-style destination like `docs\plans\x.html` is consumed into
# `docsplansx.html`, a single mangled name in the cwd, never the plan
# directory. A test that asserted the guard should ALLOW that unquoted form
# was encoding a write that would silently land in the wrong place — the
# guard refusing it is correct (see
# test_bash_hook_unquoted_backslash_is_not_the_plans_directory below, which
# pins this in both directions). Forward slashes are valid on Windows and
# are what a caller on Git Bash actually types for an unquoted path, so
# they're what these branch-parity cases use.
def _p(root: str, *parts: str) -> str:
    return "/".join([root.replace("\\", "/"), *parts])


_HOOK_BRANCH_CASES = [
    pytest.param(
        lambda root: f"python -c \"open('{_p(root, 'docs', 'plans', 'x.html')}', 'w').write('x')\"",
        lambda root: f"python -c \"open('{_p(root, 'hydra_core', 'x.py')}', 'w').write('x')\"",
        id="python-open",
    ),
    pytest.param(
        lambda root: (
            "python -c \"import shutil; shutil.copy('a.txt', "
            f"'{_p(root, 'docs', 'plans', 'x.html')}')\""
        ),
        lambda root: (
            "python -c \"import shutil; shutil.copy('a.txt', "
            f"'{_p(root, 'hydra_core', 'x.py')}')\""
        ),
        id="python-shutil",
    ),
    pytest.param(
        lambda root: f"sed -i 's/a/b/' {_p(root, 'docs', 'plans', 'x.html')}",
        lambda root: f"sed -i 's/a/b/' {_p(root, 'hydra_core', 'x.py')}",
        id="sed-i",
    ),
    pytest.param(
        lambda root: f"@'\nhi\n'@ | Set-Content {_p(root, 'docs', 'plans', 'x.html')}",
        lambda root: f"@'\nhi\n'@ | Set-Content {_p(root, 'hydra_core', 'x.py')}",
        id="here-string-set-content",
    ),
]


@pytest.mark.skipif(_PWSH is None, reason="pwsh/powershell not on PATH")
@pytest.mark.parametrize("allowed_cmd,blocked_cmd", _HOOK_BRANCH_CASES)
def test_bash_hook_branch_allows_docs_plans_and_blocks_engine_source(tmp_path, allowed_cmd, blocked_cmd):
    root = _win_root(tmp_path)
    allowed_result = _run_bash_hook(allowed_cmd(root), root)
    assert allowed_result.returncode == 0, allowed_result.stderr

    blocked_result = _run_bash_hook(blocked_cmd(root), root)
    assert blocked_result.returncode == 2
    assert "BLOCKED" in blocked_result.stderr


@pytest.mark.skipif(_PWSH is None, reason="pwsh/powershell not on PATH")
def test_bash_hook_unquoted_backslash_is_not_the_plans_directory(tmp_path):
    """Pins the real semantics so the branch-parity cases above can't
    silently regress back to asserting the wrong thing.

    Git Bash consumes an unquoted backslash as an escape character, so an
    unquoted Windows-style destination under docs\\plans never actually
    lands there — it resolves to a single mangled filename in the cwd. The
    guard must therefore BLOCK the unquoted-backslash form (it is not, in
    fact, a write to the plan directory), while the double-quoted backslash
    form and the forward-slash form — both of which really do resolve to
    docs/plans on Git Bash — must be ALLOWED.
    """
    root = _win_root(tmp_path)
    root_fs = root.replace("\\", "/")
    unquoted = f"echo hi > {root}\\docs\\plans\\x.html"
    quoted = f'echo hi > "{root}\\docs\\plans\\x.html"'
    forward = f"echo hi > {root_fs}/docs/plans/x.html"

    unquoted_result = _run_bash_hook(unquoted, root)
    assert unquoted_result.returncode == 2
    assert "BLOCKED" in unquoted_result.stderr

    quoted_result = _run_bash_hook(quoted, root)
    assert quoted_result.returncode == 0, quoted_result.stderr

    forward_result = _run_bash_hook(forward, root)
    assert forward_result.returncode == 0, forward_result.stderr


# The three whole-command fallback heuristics (sed -i, python -c shutil,
# PowerShell here-string) route their carve-out decision through
# Test-BlockedDest so they resolve a destination exactly like every other
# branch does: a RELATIVE destination is joined against the command's cwd
# before the docs/plans check runs, not just an already-absolute one. A
# first revision of the fallback fix pattern-matched '\docs\plans\' onto the
# raw command text directly, which only ever matched an absolute path — a
# relative `docs/plans/p.html` (the natural way to write this, and what the
# redirect branch already got right) still tripped it. These cases pin both
# directions for all three fallback branches: relative and absolute plan
# destinations must be ALLOWED, and relative/absolute engine-source
# destinations, the carve-out's sibling directory, and a blocked extension
# INSIDE the plans directory must all still be BLOCKED.
_FALLBACK_BRANCH_CMDS = {
    "sed-i": lambda dest: f"sed -i 's/a/b/' {dest}",
    "python-shutil": lambda dest: (
        "python -c \"import shutil; shutil.copy('a.txt', " f"'{dest}')\""
    ),
    "here-string-set-content": lambda dest: f"@'\nhi\n'@ | Set-Content {dest}",
}


@pytest.mark.skipif(_PWSH is None, reason="pwsh/powershell not on PATH")
@pytest.mark.parametrize("branch_id", list(_FALLBACK_BRANCH_CMDS))
def test_bash_hook_fallback_branches_resolve_relative_and_absolute_alike(tmp_path, branch_id):
    root = _win_root(tmp_path)
    root_fs = root.replace("\\", "/")
    build = _FALLBACK_BRANCH_CMDS[branch_id]

    allowed_dests = [
        "docs/plans/p.html",                    # relative, in the carve-out
        f"{root_fs}/docs/plans/p.html",         # absolute, in the carve-out
    ]
    for dest in allowed_dests:
        result = _run_bash_hook(build(dest), root)
        assert result.returncode == 0, f"{branch_id} {dest}: {result.stderr}"

    blocked_dests = [
        "hydra_core/x.py",                      # relative engine source
        f"{root_fs}/hydra_core/x.py",           # absolute engine source
        "docs/plans-other/p.html",              # carve-out's sibling dir
        "docs/plans/p.py",                      # blocked ext INSIDE plans dir
    ]
    for dest in blocked_dests:
        result = _run_bash_hook(build(dest), root)
        assert result.returncode == 2, f"{branch_id} {dest}: expected BLOCKED, got {result.stdout!r}"
        assert "BLOCKED" in result.stderr, f"{branch_id} {dest}: {result.stderr}"
