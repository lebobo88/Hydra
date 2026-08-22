"""Unit tests for target-repo hygiene scaffolding (.gitignore + test-runner excludes).

Covers:
  - ensure_target_repo_ignores: idempotent .gitignore scaffolding for
    ``.harness/`` and ``.hydra/`` in a dispatched target repo.
  - ensure_target_repo_test_excludes: vitest / jest / pytest config merges so
    the runner never re-collects a stale harness-worktree copy of the suite.
  - fail-soft: neither function's failure may abort a stage
    (mirrors test_agents_bootstrap.py's ensure_agents_md_failsoft pattern).
"""
from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

from hydra_core.schemas import DevTask
from hydra_core.squad_loader import discover_squads
from hydra_core.squad_node import (
    _via_mcp,
    ensure_target_repo_ignores,
    ensure_target_repo_test_excludes,
)
from hydra_core.state import HydraState

HYDRA_ROOT = Path(__file__).resolve().parents[1]


# ---------------------------------------------------------------------------
# ensure_target_repo_ignores
# ---------------------------------------------------------------------------

def test_greenfield_repo_gets_gitignore_with_both_entries(tmp_path: Path) -> None:
    ensure_target_repo_ignores(str(tmp_path))

    gitignore = tmp_path / ".gitignore"
    assert gitignore.is_file()
    lines = gitignore.read_text(encoding="utf-8").splitlines()
    assert ".harness/" in lines
    assert ".hydra/" in lines


def test_existing_gitignore_is_appended_not_overwritten(tmp_path: Path) -> None:
    gitignore = tmp_path / ".gitignore"
    gitignore.write_text("node_modules/\n.env\n", encoding="utf-8")

    ensure_target_repo_ignores(str(tmp_path))

    text = gitignore.read_text(encoding="utf-8")
    assert "node_modules/" in text
    assert ".env" in text
    assert ".harness/" in text
    assert ".hydra/" in text


def test_ensure_target_repo_ignores_is_idempotent(tmp_path: Path) -> None:
    ensure_target_repo_ignores(str(tmp_path))
    ensure_target_repo_ignores(str(tmp_path))
    ensure_target_repo_ignores(str(tmp_path))

    lines = (tmp_path / ".gitignore").read_text(encoding="utf-8").splitlines()
    assert lines.count(".harness/") == 1
    assert lines.count(".hydra/") == 1


def test_repo_already_ignoring_harness_is_left_unchanged_for_that_entry(tmp_path: Path) -> None:
    gitignore = tmp_path / ".gitignore"
    original = "dist/\n.harness/\n"
    gitignore.write_text(original, encoding="utf-8")

    ensure_target_repo_ignores(str(tmp_path))

    text = gitignore.read_text(encoding="utf-8")
    # The existing .harness/ line is untouched (no duplicate)...
    assert text.count(".harness/") == 1
    # ...but the still-missing .hydra/ entry is added.
    assert ".hydra/" in text


def test_broader_existing_pattern_prevents_duplicate_entry(tmp_path: Path) -> None:
    gitignore = tmp_path / ".gitignore"
    gitignore.write_text(".*\n", encoding="utf-8")

    ensure_target_repo_ignores(str(tmp_path))

    text = gitignore.read_text(encoding="utf-8")
    # ".*" already covers both dot-directories -- nothing new added.
    assert ".harness/" not in text
    assert ".hydra/" not in text


def test_ensure_target_repo_ignores_failsoft(monkeypatch, tmp_path: Path) -> None:
    def boom(*_a, **_k):
        raise RuntimeError("disk exploded")

    monkeypatch.setattr(Path, "is_file", boom)
    # Must not raise.
    ensure_target_repo_ignores(str(tmp_path))


# ---------------------------------------------------------------------------
# ensure_target_repo_test_excludes — vitest
# ---------------------------------------------------------------------------

def test_vitest_config_gets_excludes_merged_preserving_defaults(tmp_path: Path) -> None:
    cfg = tmp_path / "vitest.config.ts"
    cfg.write_text(
        "import { defineConfig } from 'vitest/config'\n\n"
        "export default defineConfig({\n"
        "  test: {\n"
        "    environment: 'jsdom',\n"
        "  },\n"
        "})\n",
        encoding="utf-8",
    )

    ensure_target_repo_test_excludes(str(tmp_path))

    text = cfg.read_text(encoding="utf-8")
    assert "configDefaults" in text
    assert "configDefaults.exclude" in text
    assert "'**/.harness/**'" in text
    assert "'**/.hydra/**'" in text
    assert "environment: 'jsdom'" in text  # existing config preserved


def test_vitest_config_with_existing_exclude_array_is_merged_not_replaced(tmp_path: Path) -> None:
    cfg = tmp_path / "vite.config.js"
    cfg.write_text(
        "import { defineConfig, configDefaults } from 'vitest/config'\n\n"
        "export default defineConfig({\n"
        "  test: {\n"
        "    exclude: [...configDefaults.exclude, 'e2e/**'],\n"
        "  },\n"
        "})\n",
        encoding="utf-8",
    )

    ensure_target_repo_test_excludes(str(tmp_path))

    text = cfg.read_text(encoding="utf-8")
    assert "'e2e/**'" in text  # operator's existing exclude preserved
    assert "'**/.harness/**'" in text
    assert "'**/.hydra/**'" in text


def test_vitest_config_does_not_merge_into_sibling_optimize_deps_exclude(tmp_path: Path) -> None:
    """Regression: an unbounded search for `exclude:` after `test:` would
    match `optimizeDeps.exclude` (a very common vite key) instead of adding
    `test.exclude` -- silently failing to fix the doubled-test-count issue
    AND corrupting an unrelated build setting."""
    cfg = tmp_path / "vite.config.js"
    cfg.write_text(
        "import { defineConfig } from 'vite';\n"
        "export default defineConfig({\n"
        "  test: { globals: true },\n"
        "  optimizeDeps: { exclude: ['some-dep'] },\n"
        "});\n",
        encoding="utf-8",
    )

    ensure_target_repo_test_excludes(str(tmp_path))

    text = cfg.read_text(encoding="utf-8")
    # optimizeDeps.exclude must be byte-identical -- untouched.
    assert "optimizeDeps: { exclude: ['some-dep'] }" in text
    # The harness globs must land in test.exclude, not optimizeDeps.exclude.
    test_block = re.search(r"test\s*:\s*\{([^}]*)\}", text)
    assert test_block is not None
    assert "'**/.harness/**'" in test_block.group(1)
    assert "'**/.hydra/**'" in test_block.group(1)


def test_vitest_config_with_existing_test_exclude_and_sibling_optimize_deps(tmp_path: Path) -> None:
    """Same shape as above, but test.exclude already exists -- confirm the
    merge lands in test.exclude (preserving its own entries) and
    optimizeDeps.exclude is left alone."""
    cfg = tmp_path / "vite.config.js"
    cfg.write_text(
        "import { defineConfig, configDefaults } from 'vitest/config';\n"
        "export default defineConfig({\n"
        "  test: { exclude: [...configDefaults.exclude, 'e2e/**'] },\n"
        "  optimizeDeps: { exclude: ['some-dep'] },\n"
        "});\n",
        encoding="utf-8",
    )

    ensure_target_repo_test_excludes(str(tmp_path))

    text = cfg.read_text(encoding="utf-8")
    assert "optimizeDeps: { exclude: ['some-dep'] }" in text  # untouched
    test_block = re.search(r"test\s*:\s*\{([^}]*)\}", text)
    assert test_block is not None
    assert "'e2e/**'" in test_block.group(1)  # operator's entry preserved
    assert "'**/.harness/**'" in test_block.group(1)
    assert "'**/.hydra/**'" in test_block.group(1)


def test_vitest_config_does_not_merge_into_nested_coverage_exclude(tmp_path: Path) -> None:
    """Same bug one level down: `test: { coverage: { exclude: [...] } }` must
    not be mistaken for a top-level `test.exclude` -- the coverage array is
    left untouched and a NEW top-level `exclude:` is added to `test` itself."""
    cfg = tmp_path / "vitest.config.ts"
    cfg.write_text(
        "import { defineConfig } from 'vitest/config'\n"
        "export default defineConfig({\n"
        "  test: {\n"
        "    coverage: { exclude: ['**/*.d.ts'] },\n"
        "  },\n"
        "})\n",
        encoding="utf-8",
    )

    ensure_target_repo_test_excludes(str(tmp_path))

    text = cfg.read_text(encoding="utf-8")
    # coverage.exclude must be byte-identical -- untouched.
    assert "coverage: { exclude: ['**/*.d.ts'] }" in text
    assert "'**/.harness/**'" in text
    assert "'**/.hydra/**'" in text
    # The new exclude must be a DIRECT child of `test`, not merged into
    # coverage's array.
    coverage_excl = re.search(r"coverage\s*:\s*\{\s*exclude\s*:\s*\[([^\]]*)\]", text)
    assert coverage_excl is not None
    assert "harness" not in coverage_excl.group(1)
    assert "hydra" not in coverage_excl.group(1)


def _assert_single_top_level_exclude_in_test_block(text: str) -> str:
    """Extract the `test: { ... }` block via brace-matching (not `[^}]*`, so
    a nested object doesn't truncate it) and assert it contains exactly one
    depth-0 `exclude:` key. Returns the block body for further assertions."""
    from hydra_core.squad_node import _find_matching_brace, _mask_js_strings_and_comments

    masked = _mask_js_strings_and_comments(text)
    test_match = re.search(r"\btest\s*:\s*\{", masked)
    assert test_match is not None
    open_idx = test_match.end() - 1
    close_idx = _find_matching_brace(masked, open_idx)
    assert close_idx is not None
    body = text[test_match.end():close_idx]
    masked_body = masked[test_match.end():close_idx]
    depth0_excludes = [
        m for m in re.finditer(r"exclude\s*:\s*\[([^\]]*)\]", masked_body)
        if masked_body.count("{", 0, m.start()) - masked_body.count("}", 0, m.start()) == 0
    ]
    assert len(depth0_excludes) == 1, f"expected exactly one top-level exclude, found {len(depth0_excludes)}"
    return body


def test_vitest_config_brace_in_string_literal_does_not_duplicate_exclude(tmp_path: Path) -> None:
    """Regression: a `}` inside a string literal (e.g. a filename) must not
    be counted as closing the `test:` block early -- that would push the
    real `exclude:` key outside the scanned span and cause us to insert a
    SECOND `exclude:` key (JS later-key-wins silently discards ours; strict
    toolchains reject duplicate keys outright)."""
    cfg = tmp_path / "vitest.config.ts"
    cfg.write_text(
        "import { defineConfig } from 'vitest/config'\n"
        "export default defineConfig({\n"
        "  test: {\n"
        "    setupFiles: ['./weird-}-name.ts'],\n"
        "    exclude: ['e2e/**'],\n"
        "  },\n"
        "})\n",
        encoding="utf-8",
    )

    ensure_target_repo_test_excludes(str(tmp_path))

    text = cfg.read_text(encoding="utf-8")
    body = _assert_single_top_level_exclude_in_test_block(text)
    assert "'e2e/**'" in body  # operator's entry survives
    assert "'**/.harness/**'" in body
    assert "'**/.hydra/**'" in body
    assert "'./weird-}-name.ts'" in text  # the string literal is untouched


def test_vitest_config_brace_in_line_comment_does_not_duplicate_exclude(tmp_path: Path) -> None:
    cfg = tmp_path / "vitest.config.ts"
    cfg.write_text(
        "import { defineConfig } from 'vitest/config'\n"
        "export default defineConfig({\n"
        "  test: {\n"
        "    // TODO: nested block } here\n"
        "    exclude: ['e2e/**'],\n"
        "  },\n"
        "})\n",
        encoding="utf-8",
    )

    ensure_target_repo_test_excludes(str(tmp_path))

    text = cfg.read_text(encoding="utf-8")
    body = _assert_single_top_level_exclude_in_test_block(text)
    assert "'e2e/**'" in body
    assert "'**/.harness/**'" in body
    assert "'**/.hydra/**'" in body
    assert "// TODO: nested block } here" in text  # comment untouched


def test_vitest_config_brace_in_block_comment_does_not_duplicate_exclude(tmp_path: Path) -> None:
    cfg = tmp_path / "vitest.config.ts"
    cfg.write_text(
        "import { defineConfig } from 'vitest/config'\n"
        "export default defineConfig({\n"
        "  test: {\n"
        "    /* legacy } shape */\n"
        "    exclude: ['e2e/**'],\n"
        "  },\n"
        "})\n",
        encoding="utf-8",
    )

    ensure_target_repo_test_excludes(str(tmp_path))

    text = cfg.read_text(encoding="utf-8")
    body = _assert_single_top_level_exclude_in_test_block(text)
    assert "'e2e/**'" in body
    assert "'**/.harness/**'" in body
    assert "'**/.hydra/**'" in body
    assert "/* legacy } shape */" in text  # comment untouched


def test_vitest_config_regex_literal_quote_over_masks_safely(tmp_path: Path) -> None:
    """`_mask_js_strings_and_comments` has no notion of regex literals, so a
    quote character inside a `/regex/` (e.g. `/["{]/`) is misread as a string
    opener. With no matching close-quote later in the file, masking runs to
    EOF -- swallowing the real `test: {}` block along with it. This is a
    known blind spot, not a corruption risk: with the block invisible, no
    match is found, so the function returns False and leaves the file
    byte-identical rather than guessing."""
    cfg = tmp_path / "vitest.config.ts"
    original = (
        "import { defineConfig } from 'vitest/config'\n"
        'const r = /["{]/;\n'
        "export default defineConfig({\n"
        "  test: {\n"
        "    environment: 'jsdom',\n"
        "  },\n"
        "})\n"
    )
    cfg.write_text(original, encoding="utf-8")

    from hydra_core.squad_node import _ensure_vitest_excludes

    changed = _ensure_vitest_excludes(cfg)

    assert changed is False
    assert cfg.read_text(encoding="utf-8") == original  # byte-identical


# ---------------------------------------------------------------------------
# ensure_target_repo_test_excludes — jest
# ---------------------------------------------------------------------------

def test_jest_config_js_top_level_merges_correctly(tmp_path: Path) -> None:
    cfg = tmp_path / "jest.config.js"
    cfg.write_text(
        "module.exports = {\n"
        "  testPathIgnorePatterns: ['/node_modules/'],\n"
        "};\n",
        encoding="utf-8",
    )

    ensure_target_repo_test_excludes(str(tmp_path))

    text = cfg.read_text(encoding="utf-8")
    assert "/node_modules/" in text  # preserved
    assert "/.harness/" in text
    assert "/.hydra/" in text


def test_jest_config_js_brace_in_comment_does_not_duplicate_key(tmp_path: Path) -> None:
    """Same brace-in-comment hazard as vitest, on the jest JS-config path."""
    cfg = tmp_path / "jest.config.js"
    cfg.write_text(
        "module.exports = {\n"
        "  // TODO: nested block } here\n"
        "  testPathIgnorePatterns: ['/node_modules/'],\n"
        "};\n",
        encoding="utf-8",
    )

    ensure_target_repo_test_excludes(str(tmp_path))

    text = cfg.read_text(encoding="utf-8")
    matches = re.findall(r"testPathIgnorePatterns\s*:\s*\[[^\]]*\]", text)
    assert len(matches) == 1, f"expected exactly one testPathIgnorePatterns key, found {len(matches)}"
    assert "/node_modules/" in matches[0]
    assert "/.harness/" in matches[0]
    assert "/.hydra/" in matches[0]
    assert "// TODO: nested block } here" in text  # comment untouched


def test_jest_config_js_does_not_merge_into_nested_project_pattern(tmp_path: Path) -> None:
    """Same unbounded-search class of bug as vitest: a `testPathIgnorePatterns`
    nested inside a per-project sub-object (multi-project jest config) is a
    different scope than the top-level key -- must not be silently rewritten."""
    cfg = tmp_path / "jest.config.js"
    original = (
        "module.exports = {\n"
        "  projects: [\n"
        "    { testPathIgnorePatterns: ['/legacy/'] },\n"
        "  ],\n"
        "};\n"
    )
    cfg.write_text(original, encoding="utf-8")

    ensure_target_repo_test_excludes(str(tmp_path))

    text = cfg.read_text(encoding="utf-8")
    # The nested per-project array (identified by its own '/legacy/' entry)
    # must be byte-identical -- never merged into a different scope's array.
    nested = re.search(r"testPathIgnorePatterns\s*:\s*\[([^\]]*'/legacy/'[^\]]*)\]", text)
    assert nested is not None
    assert "harness" not in nested.group(1)
    assert "hydra" not in nested.group(1)
    # Since no top-level key existed, a NEW top-level key carries the globs
    # instead (documenting the actual, conservative behaviour).
    assert "/.harness/" in text
    assert "/.hydra/" in text


def test_jest_config_json_gets_testpathignorepatterns_merged(tmp_path: Path) -> None:
    cfg = tmp_path / "jest.config.json"
    cfg.write_text(json.dumps({"testPathIgnorePatterns": ["/node_modules/"]}), encoding="utf-8")

    ensure_target_repo_test_excludes(str(tmp_path))

    data = json.loads(cfg.read_text(encoding="utf-8"))
    patterns = data["testPathIgnorePatterns"]
    assert "/node_modules/" in patterns  # preserved
    assert any(".harness" in p for p in patterns)
    assert any(".hydra" in p for p in patterns)


def test_package_json_jest_block_gets_merged(tmp_path: Path) -> None:
    pkg = tmp_path / "package.json"
    pkg.write_text(json.dumps({"name": "x", "jest": {"testPathIgnorePatterns": ["/dist/"]}}), encoding="utf-8")

    ensure_target_repo_test_excludes(str(tmp_path))

    data = json.loads(pkg.read_text(encoding="utf-8"))
    patterns = data["jest"]["testPathIgnorePatterns"]
    assert "/dist/" in patterns
    assert any(".harness" in p for p in patterns)
    assert any(".hydra" in p for p in patterns)


# ---------------------------------------------------------------------------
# ensure_target_repo_test_excludes — pytest
# ---------------------------------------------------------------------------

def test_pytest_pyproject_norecursedirs_merged(tmp_path: Path) -> None:
    cfg = tmp_path / "pyproject.toml"
    cfg.write_text(
        "[tool.pytest.ini_options]\n"
        'testpaths = ["tests"]\n'
        'norecursedirs = ["node_modules", "build"]\n',
        encoding="utf-8",
    )

    ensure_target_repo_test_excludes(str(tmp_path))

    text = cfg.read_text(encoding="utf-8")
    assert '"node_modules"' in text  # preserved
    assert '"build"' in text  # preserved
    assert '".harness"' in text
    assert '".hydra"' in text


def test_pytest_pyproject_no_norecursedirs_key_gets_one_created(tmp_path: Path) -> None:
    cfg = tmp_path / "pyproject.toml"
    cfg.write_text(
        "[tool.pytest.ini_options]\n"
        'testpaths = ["tests"]\n',
        encoding="utf-8",
    )

    ensure_target_repo_test_excludes(str(tmp_path))

    text = cfg.read_text(encoding="utf-8")
    assert "norecursedirs" in text
    assert ".harness" in text
    assert ".hydra" in text


def test_pytest_ini_norecursedirs_merged(tmp_path: Path) -> None:
    cfg = tmp_path / "pytest.ini"
    cfg.write_text("[pytest]\nnorecursedirs = node_modules build\n", encoding="utf-8")

    ensure_target_repo_test_excludes(str(tmp_path))

    text = cfg.read_text(encoding="utf-8")
    assert "node_modules" in text
    assert "build" in text
    assert ".harness" in text
    assert ".hydra" in text


def test_setup_cfg_uses_tool_pytest_section_header(tmp_path: Path) -> None:
    """Regression: setup.cfg namespaces pytest options under ``[tool:pytest]``,
    not a bare ``[pytest]`` (which pytest never reads there). Merging into the
    wrong header would silently no-op every real setup.cfg."""
    cfg = tmp_path / "setup.cfg"
    cfg.write_text(
        "[metadata]\nname = demo\n\n"
        "[tool:pytest]\nnorecursedirs = node_modules build\n",
        encoding="utf-8",
    )

    ensure_target_repo_test_excludes(str(tmp_path))

    text = cfg.read_text(encoding="utf-8")
    assert "node_modules" in text  # preserved
    assert "build" in text  # preserved
    assert ".harness" in text
    assert ".hydra" in text


def test_setup_cfg_without_pytest_section_is_left_alone(tmp_path: Path) -> None:
    """A setup.cfg with no ``[tool:pytest]`` section must not be rewritten
    (and a bare ``[pytest]`` header must not be treated as pytest config)."""
    cfg = tmp_path / "setup.cfg"
    original = "[metadata]\nname = demo\n"
    cfg.write_text(original, encoding="utf-8")

    ensure_target_repo_test_excludes(str(tmp_path))

    assert cfg.read_text(encoding="utf-8") == original  # byte-identical


# ---------------------------------------------------------------------------
# No recognized runner -> no-op, no error
# ---------------------------------------------------------------------------

def test_repo_with_no_recognized_runner_is_left_alone(tmp_path: Path) -> None:
    (tmp_path / "README.md").write_text("hello\n", encoding="utf-8")

    # Must not raise, and must not create any config file.
    ensure_target_repo_test_excludes(str(tmp_path))

    assert list(tmp_path.iterdir()) == [tmp_path / "README.md"]


def test_ensure_target_repo_test_excludes_failsoft(monkeypatch, tmp_path: Path) -> None:
    def boom(*_a, **_k):
        raise RuntimeError("disk exploded")

    monkeypatch.setattr(Path, "is_file", boom)
    # Must not raise.
    ensure_target_repo_test_excludes(str(tmp_path))


# ---------------------------------------------------------------------------
# Wired into the attended dispatch path (mirrors test_agents_bootstrap.py)
# ---------------------------------------------------------------------------

class _RecordingDispatcher:
    def __init__(self) -> None:
        self.responses = {
            ("pp_harness", "start_run"): {
                "status": "done",
                "result": {"run_id": "run_IGN"},
            },
        }
        self.calls: list[tuple[str, str, dict[str, Any], str | None]] = []

    def call_mcp(self, server, tool, args, *, squad_id=None):
        self.calls.append((server, tool, args, squad_id))
        return self.responses.get((server, tool), {"status": "done", "result": {}})

    def emit_claude_prompt(self, *_a, **_k): raise NotImplementedError  # pragma: no cover
    def invoke_claude_skill(self, *_a, **_k): raise NotImplementedError  # pragma: no cover
    def spawn_subprocess(self, *_a, **_k): raise NotImplementedError  # pragma: no cover


def _eng_pack():
    return discover_squads(HYDRA_ROOT)["engineering"]


def _inbound(state: HydraState) -> DevTask:
    return DevTask(
        workflow_id=state.workflow_id,
        origin_squad="hydra",
        owner="backend",
        repo="hydra",
        branch="wf",
        instructions="scaffold ignores coverage",
        # WS1-E: engineering dispatch requires an explicit, resolved target
        # repo -- this file exercises the scaffolding side effect, not
        # repo-targeting, so point at "hydra" (this checkout) rather than
        # relying on the removed cwd fallback.
        target_repo_id="hydra",
    )


def test_via_mcp_scaffolds_ignores_into_project_path(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setattr("hydra_core.squad_node.harvest_pp_run_artifacts", lambda **_k: None)
    monkeypatch.setattr("hydra_core.squad_node._maybe_write_claude_shim", lambda _p: None)

    state = HydraState(root_goal="t")
    disp = _RecordingDispatcher()
    inbound = _inbound(state)
    inbound.target_repo_subpath = None

    # Force project_path to our tmp dir via the squad.yaml's project_path
    # branch is awkward to hit directly, so exercise the scaffolding function
    # against the same resolution the dispatch path would use: call it with
    # the dispatcher's recorded start_run project_path once _via_mcp runs.
    monkeypatch.setattr(
        "hydra_core.squad_node.ensure_target_repo_ignores",
        lambda project_path: (tmp_path / "MARKER_IGNORES").write_text(project_path, encoding="utf-8"),
    )
    monkeypatch.setattr(
        "hydra_core.squad_node.ensure_target_repo_test_excludes",
        lambda project_path: (tmp_path / "MARKER_EXCLUDES").write_text(project_path, encoding="utf-8"),
    )

    result = _via_mcp(state, _eng_pack(), inbound, disp)

    assert result.status == "running"
    assert (tmp_path / "MARKER_IGNORES").is_file()
    assert (tmp_path / "MARKER_EXCLUDES").is_file()

    # Confirm the markers were written with the SAME project_path that was
    # actually recorded in the start_run call -- not merely that the helpers
    # were reached with *some* argument.
    start_run_calls = [
        args for (server, tool, args, _sid) in disp.calls
        if (server, tool) == ("pp_harness", "start_run")
    ]
    assert start_run_calls, "expected _via_mcp to call pp_harness.start_run"
    expected_project_path = start_run_calls[0]["project_path"]
    assert (tmp_path / "MARKER_IGNORES").read_text(encoding="utf-8") == expected_project_path
    assert (tmp_path / "MARKER_EXCLUDES").read_text(encoding="utf-8") == expected_project_path


def test_via_mcp_ignores_scaffolding_failure_does_not_abort_stage(monkeypatch, tmp_path: Path) -> None:
    """Even a lower-level I/O failure inside the real (fail-soft) scaffolding
    functions must not propagate out of _via_mcp and surface the stage.

    Note on scope: this proves the stage survives the failure, not that the
    real ``ensure_target_repo_*`` code path was reached rather than skipped
    entirely before the failing call. ``Path.write_text`` is monkeypatched
    globally, so a code path that never calls ``write_text`` would pass this
    test just as trivially as one that does and recovers. Reach is instead
    covered positively by ``test_via_mcp_scaffolds_ignores_into_project_path``
    above, which asserts the markers ARE written on the non-failing path."""
    monkeypatch.setattr("hydra_core.squad_node.harvest_pp_run_artifacts", lambda **_k: None)
    monkeypatch.setattr("hydra_core.squad_node._maybe_write_claude_shim", lambda _p: None)

    def boom(*_a, **_k):
        raise RuntimeError("disk exploded")

    # Exercise the real ensure_target_repo_* functions (not mocks) with their
    # internal filesystem calls failing, to prove the try/except inside each
    # function -- not just careful call-site wrapping -- is what protects the
    # stage.
    monkeypatch.setattr(Path, "write_text", boom)

    state = HydraState(root_goal="t")
    disp = _RecordingDispatcher()

    result = _via_mcp(state, _eng_pack(), _inbound(state), disp)

    assert result.status == "running"
