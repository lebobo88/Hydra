"""Hydra#81 (W0.3 of epic #78): the gateway catalogs for ``hydra_control`` and
``hydra_memory`` cannot drift from the servers' handler registries.

``hydra_core/inproc_tool_catalog.py`` is the single source of truth.  The two
servers derive ``_TOOL_SCHEMAS`` from it and ``hydra_core.toolshed`` derives
its tool lists and gateway fallback schemas from it.  These tests assert that
the servers' ``_tool_handlers()`` registries (keys only; no handler is ever
invoked) equal the catalog, and that the toolshed import stays dependency-light.

Everything here is offline: servers are loaded by path, no transport or server
process is started.  The gateway test passes an explicit empty schema cache and
never reads the operator's ``~/.hydra``.
"""
from __future__ import annotations

import importlib.util
import json
import subprocess
import sys
from pathlib import Path
from typing import Any

from hydra_core import toolshed
from hydra_core.inproc_tool_catalog import (
    HYDRA_CONTROL_TOOL_SPECS,
    HYDRA_MEMORY_TOOL_SPECS,
)
from hydra_core.toolshed import SCHEMA_OVERRIDES, build_default_shed

REPO_ROOT = Path(__file__).resolve().parents[1]

# Frozen: what was advertised before Hydra#81, in toolshed order.
PREVIOUS_CONTROL_TOOLS = [
    "hydra.control.ping", "hydra.workflow.launch", "hydra.workflow.plan",
    "hydra.workflow.step", "hydra.workflow.submit_host_result",
    "hydra.workflow.finalize", "hydra.workflow.resume",
    "hydra.workflow.submit_envelopes", "hydra.cockpit.audit",
    "hydra.venom.cross_check", "hydra.squad.list", "hydra.repo.list",
    "hydra.repo.resolve", "hydra.envelope.record", "hydra.telemetry.tail",
]
PREVIOUS_MEMORY_TOOLS = [
    "hydra-mem.write_episodic", "hydra-mem.read_episodic",
    "hydra-mem.list_workflow", "hydra-mem.semantic_search",
    "hydra-mem.query_eights", "hydra-mem.tag_memory",
]
NEW_CONTROL_TOOLS = ["hydra.workflow.budget", "hydra.worktree.sweep_status"]
NEW_MEMORY_TOOLS = [
    "hydra-mem.ping", "hydra-mem.workflows_list", "hydra-mem.workflow_status",
    "hydra-mem.squad_list", "hydra-mem.hitl_pending",
]


def _load_server(alias: str, *parts: str) -> Any:
    spec = importlib.util.spec_from_file_location(
        alias, REPO_ROOT.joinpath(*parts))
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _registry_diff(label: str, registry: set[str], spec: set[str],
                   shed_list: list[str]) -> str:
    lists = {"registry": registry, "catalog spec": spec,
             "toolshed list": set(shed_list)}
    lines = [f"{label}: handler registry / catalog spec / toolshed list differ"]
    for a in lists:
        for b in lists:
            if a != b:
                extra, missing = sorted(lists[a] - lists[b]), sorted(lists[b] - lists[a])
                if extra or missing:
                    lines.append(f"  {a} vs {b}: only in {a}={extra}; only in {b}={missing}")
    return "\n".join(lines)


def test_hydra_control_registry_equals_catalog() -> None:
    server = _load_server("hc_registry_under_test",
                          "mcp_servers", "hydra_control", "server.py")
    registry = set(server._tool_handlers().keys())  # keys only; no handler runs
    spec = set(HYDRA_CONTROL_TOOL_SPECS)
    shed = list(toolshed.HYDRA_CONTROL_TOOLS)
    assert registry == spec == set(shed), _registry_diff(
        "hydra_control", registry, spec, shed)


def test_hydra_memory_registry_equals_catalog() -> None:
    server = _load_server("hm_registry_under_test",
                          "mcp_servers", "hydra_memory", "server.py")
    registry = set(server._tool_handlers().keys())  # keys only; no handler runs
    spec = set(HYDRA_MEMORY_TOOL_SPECS)
    shed = list(toolshed.HYDRA_MEMORY_TOOLS)
    assert registry == spec == set(shed), _registry_diff(
        "hydra_memory", registry, spec, shed)


def test_hydra_control_server_schemas_equal_catalog() -> None:
    server = _load_server("hc_schema_under_test",
                          "mcp_servers", "hydra_control", "server.py")
    names = sorted(server._tool_handlers().keys())
    assert len(names) == 17
    assert server._TOOL_SCHEMAS == HYDRA_CONTROL_TOOL_SPECS
    overrides = SCHEMA_OVERRIDES["hydra_control"]
    bad = [n for n in names
           if overrides.get(n) != server._TOOL_SCHEMAS[n]["inputSchema"]]
    assert not bad, f"toolshed fallback differs from server schema for: {bad}"
    # The handler validates against _RESUME_ACTIONS; the advertised enum is a
    # copy in the catalog, so pin them together.
    resume = server._TOOL_SCHEMAS["hydra.workflow.resume"]["inputSchema"]
    assert resume["properties"]["action"]["enum"] == list(server._RESUME_ACTIONS)


def test_hydra_memory_server_schemas_equal_catalog() -> None:
    server = _load_server("hm_schema_under_test",
                          "mcp_servers", "hydra_memory", "server.py")
    names = sorted(server._tool_handlers().keys())
    assert len(names) == 11
    assert server._TOOL_SCHEMAS == HYDRA_MEMORY_TOOL_SPECS
    overrides = SCHEMA_OVERRIDES["hydra_memory"]
    bad = [n for n in names
           if overrides.get(n) != server._TOOL_SCHEMAS[n]["inputSchema"]]
    assert not bad, f"toolshed fallback differs from server schema for: {bad}"


def test_previously_advertised_tools_preserved() -> None:
    assert isinstance(toolshed.HYDRA_CONTROL_TOOLS, list)
    assert isinstance(toolshed.HYDRA_MEMORY_TOOLS, list)
    for frozen, current in (
        (PREVIOUS_CONTROL_TOOLS, toolshed.HYDRA_CONTROL_TOOLS),
        (PREVIOUS_MEMORY_TOOLS, toolshed.HYDRA_MEMORY_TOOLS),
    ):
        assert all(n in current for n in frozen), (
            f"previously advertised tools dropped: {[n for n in frozen if n not in current]}")
        assert [n for n in current if n in frozen] == frozen, (
            "relative order of previously advertised tools changed")


def _surfaced_tools() -> list[dict[str, Any]]:
    """Mirror of tests/test_gateway_catalog_complete.py::_surfaced, narrowed to
    the two servers under test and an explicit EMPTY schema cache."""
    from mcp_servers.hydra_gateway.server import (
        _apply_schema_cache_to_shed, _build_static_tool_list,
    )
    shed = build_default_shed()
    _apply_schema_cache_to_shed(shed, {})
    specs = {"hydra_control": {"type": "stdio"}, "hydra_memory": {"type": "stdio"}}
    return _build_static_tool_list(shed, specs, {})


def test_new_tools_surface_through_gateway() -> None:
    tools = _surfaced_tools()
    by_name = {t["name"]: t for t in tools}
    expected = (
        [("hydra_control", n, HYDRA_CONTROL_TOOL_SPECS) for n in NEW_CONTROL_TOOLS]
        + [("hydra_memory", n, HYDRA_MEMORY_TOOL_SPECS) for n in NEW_MEMORY_TOOLS]
    )
    assert len(expected) == 7
    for server, tool, specs in expected:
        name = f"{server}__{tool}"
        assert name in by_name, (
            f"{name} not advertised; observed: {sorted(by_name)}")
        schema = by_name[name]["inputSchema"]
        assert schema == specs[tool]["inputSchema"], name
        assert schema != {"type": "object"}, f"{name} fell back to a bare schema"


_IMPORT_PROBE = """
import json, sys
before = set(sys.modules)
import {module}
added = sorted(set(sys.modules) - before)
print(json.dumps(added))
"""


def _unexpected_modules(module: str, allowed: set[str]) -> list[str]:
    proc = subprocess.run(
        [sys.executable, "-c", _IMPORT_PROBE.format(module=module)],
        cwd=str(REPO_ROOT), capture_output=True, text=True, timeout=120,
    )
    assert proc.returncode == 0, (
        f"import probe for {module} died:\n{proc.stdout}\n{proc.stderr}")
    added = json.loads(proc.stdout.strip().splitlines()[-1])
    return [m for m in added
            if m not in allowed and m.split(".")[0] not in sys.stdlib_module_names]


def test_toolshed_import_adds_only_expected_modules() -> None:
    allowed = {"hydra_core", "hydra_core.toolshed",
               "hydra_core.inproc_tool_catalog", "hydra_core.eights"}
    unexpected = _unexpected_modules("hydra_core.toolshed", allowed)
    assert not unexpected, (
        f"importing hydra_core.toolshed loaded unexpected modules: {unexpected}")


def test_eights_package_import_adds_only_stdlib() -> None:
    allowed = {"hydra_core", "hydra_core.eights"}
    unexpected = _unexpected_modules("hydra_core.eights", allowed)
    assert not unexpected, (
        f"importing hydra_core.eights loaded unexpected modules: {unexpected}")
