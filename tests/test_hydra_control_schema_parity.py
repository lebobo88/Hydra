"""WS1-A: schema parity between hydra_core.toolshed.SCHEMA_OVERRIDES["hydra_control"]
and mcp_servers/hydra_control/server.py's own ``_TOOL_SCHEMAS``.

hydra_core/ must stay runtime-agnostic (AGENTS.md "Engineering Standards"):
mcp_servers/hydra_control/server.py is the runtime binding (it never imports
hydra_core at module scope; it shells out to `python -m hydra_core.cli`), so
hydra_core importing FROM it would invert the intended dependency direction.
The chosen fix is therefore a hand-duplicated schema plus this test, which
fails loudly the moment the two copies drift -- exactly the failure mode that
let `risk` go missing from the toolshed copy in the first place (WS1-A).

This test file is offline: it imports mcp_servers/hydra_control/server.py by
path (no MCP transport, no subprocess, no network).
"""
from __future__ import annotations

import importlib.util
from pathlib import Path
from typing import Any

from hydra_core.toolshed import HYDRA_CONTROL_TOOLS, SCHEMA_OVERRIDES

REPO_ROOT = Path(__file__).resolve().parents[1]


def _load_hydra_control_server():
    spec = importlib.util.spec_from_file_location(
        "hydra_control_server_under_test",
        REPO_ROOT / "mcp_servers" / "hydra_control" / "server.py",
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_every_hydra_control_tool_has_a_toolshed_schema_override() -> None:
    overrides = SCHEMA_OVERRIDES.get("hydra_control", {})
    missing = [name for name in HYDRA_CONTROL_TOOLS if name not in overrides]
    assert not missing, (
        f"HYDRA_CONTROL_TOOLS entries missing from toolshed SCHEMA_OVERRIDES: {missing}"
    )


def test_toolshed_schema_overrides_match_server_registry_exactly() -> None:
    """The load-bearing regression guard: every HYDRA_CONTROL_TOOLS entry's
    hand-duplicated inputSchema in toolshed.py must be byte-for-byte identical
    (as JSON-comparable dicts) to the server's own _TOOL_SCHEMAS entry.

    This is what would have caught `risk` going missing from the plan/launch
    toolshed overrides before it shipped."""
    server = _load_hydra_control_server()
    server_schemas: dict[str, dict[str, Any]] = server._TOOL_SCHEMAS
    overrides = SCHEMA_OVERRIDES.get("hydra_control", {})

    mismatches: dict[str, tuple[Any, Any]] = {}
    for name in HYDRA_CONTROL_TOOLS:
        server_entry = server_schemas.get(name)
        assert server_entry is not None, (
            f"{name!r} is listed in HYDRA_CONTROL_TOOLS but the server "
            f"defines no _TOOL_SCHEMAS entry for it"
        )
        server_schema = server_entry.get("inputSchema", {})
        toolshed_schema = overrides.get(name, {})
        if server_schema != toolshed_schema:
            mismatches[name] = (server_schema, toolshed_schema)

    assert not mismatches, (
        "toolshed SCHEMA_OVERRIDES['hydra_control'] has drifted from the "
        f"server's own _TOOL_SCHEMAS for: {sorted(mismatches)}\n"
        + "\n".join(
            f"  {name}:\n    server={srv}\n    toolshed={ts}"
            for name, (srv, ts) in mismatches.items()
        )
    )


def test_launch_and_plan_schemas_declare_risk_and_repo_params() -> None:
    """Regression guard for the specific WS1-A drift: `risk` was missing from
    the toolshed copy, and `repo`/`repos`/`repo_subpath` were entirely absent
    from BOTH copies (the proximate cause of the target_repo_id: null bug)."""
    overrides = SCHEMA_OVERRIDES["hydra_control"]
    for tool_name in ("hydra.workflow.launch", "hydra.workflow.plan"):
        props = overrides[tool_name]["properties"]
        for field in ("risk", "repo", "repos", "repo_subpath"):
            assert field in props, f"{tool_name} toolshed schema missing {field!r}"
