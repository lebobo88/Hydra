"""AST-based enforcement guard: no NEW unguarded ``json.dumps``/``json.dump``
call site may appear anywhere in ``hydra_core`` or ``mcp_servers`` (excluding
test files) without a reviewed, named entry in ``ALLOWLIST`` below.

Why AST, not grep/regex: a regex over source text cannot tell a real call
from a docstring/comment MENTIONING ``json.dumps(`` (this module's own
docstrings do exactly that), and cannot reliably scope to a call nested
inside a function vs. one at module scope. Walking the parsed tree and
matching ``ast.Call`` nodes whose ``func`` is an ``Attribute`` named
``dumps``/``dump`` (i.e. ``json.dumps(...)``, ``_json.dumps(...)``,
``json.dump(...)`` under any import alias for the ``json`` module) is exact:
it only ever matches a real call expression, never text that merely looks
like one. A call already passing ``allow_nan=False`` is exempt -- that is
itself a valid (if verbose) form of the guard `dumps_strict` centralizes.

A call to `dumps_strict` / `dumps_tool_response_safe` / `_cli_json_dumps`
(any bare ``ast.Name`` callee, not ``ast.Attribute``) never matches this
scan at all -- it isn't `X.dumps(...)`. That is precisely how a guarded call
site "disappears" from the offender list once it is fixed.

Stable site identity: NOT a bare line number (a one-line change anywhere
above a site shifts every subsequent line number, which would make the
allow-list churn on unrelated diffs and mask a genuinely new site behind
"same line number, different call"). Each site's identity is
``(posix relative path, dotted enclosing-function path, first 40 chars of
the unparsed first argument)``. The enclosing-function path survives
reformatting and line shifts; the argument snippet disambiguates two
guarded-looking calls in the same function (e.g.
``hydra_gateway/server.py``'s two exception-fallback branches) and changes
only when someone edits the call's actual payload shape -- exactly the
moment a reviewer should look at the allow-list entry again.
"""
from __future__ import annotations

import ast
from pathlib import Path
from typing import NamedTuple

REPO_ROOT = Path(__file__).resolve().parents[1]
SCAN_ROOTS = ["hydra_core", "mcp_servers"]


class Site(NamedTuple):
    path: str          # posix-relative to repo root
    func_path: str      # dotted enclosing function/method names, "" if module scope
    snippet: str        # first 40 chars of the unparsed first argument
    lineno: int          # current line, for the failure message only -- NOT part of identity


def _is_test_path(rel_path: Path) -> bool:
    parts = rel_path.parts
    if any(p == "tests" for p in parts):
        return True
    if rel_path.name.startswith("test_"):
        return True
    return False


def _enclosing_func_paths(tree: ast.AST) -> dict[int, str]:
    """Map every line number covered by a function body to the dotted path
    of its innermost enclosing function (``outer.inner`` for a nested def),
    "" for module-level code."""
    line_to_func: dict[int, str] = {}

    def walk(node: ast.AST, stack: list[str]) -> None:
        for child in ast.iter_child_nodes(node):
            child_stack = stack
            if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef)):
                child_stack = stack + [child.name]
                start, end = child.lineno, getattr(child, "end_lineno", child.lineno)
                for ln in range(start, end + 1):
                    line_to_func[ln] = ".".join(child_stack)
            walk(child, child_stack)

    walk(tree, [])
    return line_to_func


def _find_unguarded_sites(py_path: Path) -> list[Site]:
    rel = py_path.relative_to(REPO_ROOT)
    source = py_path.read_text(encoding="utf-8")
    tree = ast.parse(source, filename=str(rel))
    line_to_func = _enclosing_func_paths(tree)

    sites: list[Site] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        if not (isinstance(func, ast.Attribute) and func.attr in ("dumps", "dump")):
            continue
        guarded = any(
            kw.arg == "allow_nan"
            and isinstance(kw.value, ast.Constant)
            and kw.value.value is False
            for kw in node.keywords
        )
        if guarded:
            continue
        if node.args:
            try:
                snippet = ast.unparse(node.args[0])[:40]
            except Exception:  # noqa: BLE001 — pathological node, still identify by position
                snippet = "<unparseable>"
        else:
            snippet = "<no-args>"
        func_path = line_to_func.get(node.lineno, "")
        sites.append(Site(rel.as_posix(), func_path, snippet, node.lineno))
    return sites


def _scan_repo() -> list[Site]:
    found: list[Site] = []
    for root_name in SCAN_ROOTS:
        root = REPO_ROOT / root_name
        if not root.is_dir():
            continue
        for py_path in sorted(root.rglob("*.py")):
            rel = py_path.relative_to(REPO_ROOT)
            if _is_test_path(rel):
                continue
            found.extend(_find_unguarded_sites(py_path))
    return found


# ---------------------------------------------------------------------------
# The allow-list. Every entry is a REVIEWED, DELIBERATE exception, not an
# oversight -- adding to it is the visible decision this test exists to
# force. Key: (path, func_path, snippet) as produced by `_find_unguarded_sites`.
# ---------------------------------------------------------------------------
ALLOWLIST: dict[tuple[str, str, str], str] = {
    (
        "hydra_core/strict_json.py",
        "dumps_strict",
        "payload",
    ): (
        "This IS the reference implementation `dumps_strict` wraps: the "
        "initial strict attempt inside the one function every other STRICT "
        "call site in the codebase routes through. `strict_json.py` is the "
        "out-of-scope module this whole guard is built around -- see the "
        "task's explicit 'Do NOT modify strict_json.py' boundary."
    ),
    (
        "hydra_core/strict_json.py",
        "dumps_tool_response_safe",
        "sanitized",
    ): (
        "The final, unconditionally-safe serialization of an ALREADY "
        "sanitized structure (every remaining float is finite by "
        "construction -- see `sanitize_non_finite`'s docstring): this is "
        "the guaranteed-succeed step `dumps_tool_response_safe` promises, "
        "not a bypass of it."
    ),
    (
        "hydra_core/cli.py",
        "_cli_json_dumps",
        "sanitized",
    ): (
        "The local CLI wrapper's own fallback, mirroring "
        "`dumps_tool_response_safe`'s finally-safe step above -- this "
        "function IS the guard every other `hydra_core/cli.py` call site "
        "now routes through (see the module-level 'CLI stdout/stderr is a "
        "machine boundary' comment)."
    ),
    (
        "hydra_core/host_bridge.py",
        "_capture_baseline_failures",
        "{'timeout_s': _baseline_timeout_s()}",
    ): (
        "Presence-only marker file: the reader (`_timeout_marker.is_file()`, "
        "same function, ~15 lines above) only checks the file EXISTS and "
        "never parses its contents back with `json.loads` -- there is no "
        "reachable failure mode a non-finite guard would protect against. "
        "See the nine-site table's discrepancy note for the sibling site "
        "(`_cache_file`, same function) that WAS genuinely read back and "
        "IS guarded."
    ),
    (
        "hydra_core/squad_node.py",
        "_via_claude_skill",
        "{'command': cmd, 'squad': pack.slug, **s",
    ): (
        "One-way prompt text handed to `run_host` (an external host-executed "
        "skill subprocess/LLM call) -- never parsed back into a Python "
        "structure by Hydra itself, only read as a text prompt by the host "
        "agent. Already wrapped in its own broad `except Exception: hosted "
        "= None` (\"never crash dispatch on a host hiccup\"), so a raise here "
        "would just be silently absorbed one frame up with no benefit over "
        "leaving it as `default=str`."
    ),
    (
        "mcp_servers/_pack_shim.py",
        "_serve_bare",
        "{'error': 'parse_error', 'detail': str(e",
    ): (
        "B1-reviewed 'leave' decision, not reopened here: a fixed-shape "
        "dict built only from the string literal \"parse_error\" and "
        "`str(exc)` -- no float can ever reach it. Proven (including "
        "against a pathological exception message) by "
        "tests/test_transport_security_json_boundaries.py::"
        "test_leave_sites_are_always_valid_json."
    ),
    (
        "mcp_servers/hydra_control/server.py",
        "_serve_bare",
        "{'error': 'parse_error', 'detail': str(e",
    ): (
        "Same B1-reviewed 'leave' shape as `_pack_shim.py`'s bare-stdio "
        "parse-error reply -- see that entry."
    ),
    (
        "mcp_servers/hydra_memory/server.py",
        "_serve_bare",
        "{'error': 'parse_error', 'detail': str(e",
    ): (
        "Same B1-reviewed 'leave' shape as `_pack_shim.py`'s bare-stdio "
        "parse-error reply -- see that entry."
    ),
    (
        "mcp_servers/hydra_toolshed/server.py",
        "_serve_bare",
        "{'error': 'parse_error', 'detail': str(e",
    ): (
        "Same B1-reviewed 'leave' shape as `_pack_shim.py`'s bare-stdio "
        "parse-error reply -- see that entry."
    ),
    (
        "mcp_servers/hydra_gateway/server.py",
        "main._call_tool",
        "{'status': 'failed', 'error': f'{type(ex",
    ): (
        "Fixed-shape dict of `status`/`error`/`tool` built only from "
        "`type(exc).__name__`, `str(exc)`, and the caller-supplied tool "
        "`name` -- string/f-string composition only, no float can reach "
        "it. Same class as the B1-reviewed bare-stdio 'leave' sites; the "
        "main result path two branches above already routes through "
        "`dumps_tool_response_safe`."
    ),
    (
        "mcp_servers/hydra_gateway/server.py",
        "main._call_tool",
        "{'status': 'failed', 'error': f'unknown ",
    ): (
        "Same fixed-shape, string-only `status`/`error` dict as the "
        "exception-fallback entry immediately above -- no float can reach "
        "it."
    ),
}


def test_no_new_unguarded_json_dumps_sites():
    found = _scan_repo()
    offenders = [s for s in found if (s.path, s.func_path, s.snippet) not in ALLOWLIST]
    if offenders:
        lines = [
            f"  {s.path}:{s.lineno} in `{s.func_path or '<module>'}` "
            f"(unguarded json.dumps/json.dump of `{s.snippet}...`)"
            for s in offenders
        ]
        raise AssertionError(
            "Unguarded json.dumps/json.dump call site(s) found outside the "
            "reviewed allow-list. Fix EACH by routing it through "
            "hydra_core.strict_json.dumps_strict (refuse on non-finite -- "
            "persisted/decision state) or dumps_tool_response_safe "
            "(sanitize-and-report -- a transport response that must always "
            "complete), OR, if this is a genuinely reviewed exception, add "
            "a new ALLOWLIST entry in tests/test_json_dumps_enforcement.py "
            "naming the reason:\n" + "\n".join(lines)
        )


def test_allowlist_entries_are_all_still_present():
    """The mirror check: every ALLOWLIST entry must still correspond to a
    real (currently unguarded) site. A stale entry (the code was fixed or
    deleted and nobody removed the allow-list line) would otherwise silently
    hide a future collision with a coincidentally-identical new site."""
    found = {(s.path, s.func_path, s.snippet) for s in _scan_repo()}
    stale = [key for key in ALLOWLIST if key not in found]
    assert not stale, (
        "Stale ALLOWLIST entries no longer match any unguarded call site "
        "(the code was fixed/removed) -- delete them from "
        "tests/test_json_dumps_enforcement.py: " + repr(stale)
    )
