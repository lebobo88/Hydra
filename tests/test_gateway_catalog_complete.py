from __future__ import annotations

import json
from pathlib import Path

import pytest

from hydra_core.toolshed import PP_HARNESS_TOOLS, build_default_shed


REPO_ROOT = Path(__file__).resolve().parents[1]


def test_build_default_shed_covers_all_mcp_backends() -> None:
    backends = json.loads((REPO_ROOT / "scripts" / "backends.template.json").read_text(
        encoding="utf-8"
    ))
    exempt = {
        "hydra-cockpit",  # Node HTTP cockpit bridge, non-MCP, reports 0 tools.
    }

    shed = build_default_shed()
    configured = set(backends) - exempt
    registered = set(shed.servers)

    assert configured <= registered, (
        f"missing static catalogs for: {sorted(configured - registered)}"
    )


def test_new_gateway_catalogs_expose_representative_tools() -> None:
    shed = build_default_shed()

    assert shed.describe("hydra_control", "hydra.workflow.launch") is not None
    assert shed.describe("rlm_gaming", "rlmgaming.ping") is not None
    assert shed.describe("marketbliss", "mb.ping") is not None
    assert shed.describe("xenia", "xenia.ping") is not None


def test_ack_run_is_exposed_through_the_gateway() -> None:
    """E2-9: the pp daemon defines `ack_run` and the pp SessionStart guidance
    points operators at it, so the gateway allow-list must expose it."""
    assert "ack_run" in PP_HARNESS_TOOLS

    shed = build_default_shed()
    assert shed.describe("pp_harness", "ack_run") is not None


def test_pp_harness_allowlist_covers_cached_schema_catalog() -> None:
    """Regression guard for E2-9.

    The only pp_harness schema catalog is user-scope
    (``~/.hydra/gateway_schemas.json``, written by the gateway when it
    introspects the daemon); it is not vendored in-repo. When it is present,
    every tool it lists under ``pp_harness`` must be in ``PP_HARNESS_TOOLS`` so
    a newly added daemon tool cannot silently drop out of the gateway. When it
    is absent (CI, a fresh checkout) this check skips and the static assertion
    in the test above still holds.
    """
    catalog_path = Path.home() / ".hydra" / "gateway_schemas.json"
    if not catalog_path.is_file():
        pytest.skip(f"no gateway schema catalog at {catalog_path}")

    catalog = json.loads(catalog_path.read_text(encoding="utf-8"))
    pp_schemas = catalog.get("pp_harness")
    if not isinstance(pp_schemas, dict) or not pp_schemas:
        pytest.skip("gateway schema catalog has no pp_harness section")

    missing = sorted(set(pp_schemas) - set(PP_HARNESS_TOOLS))
    assert not missing, (
        "pp_harness tools present in the gateway schema catalog but missing "
        f"from PP_HARNESS_TOOLS: {missing}"
    )


# --------------------------------------------------------------------------
# E2-39: pp_codex / pp_agy input schemas must survive the cataloging pipeline.
# --------------------------------------------------------------------------
from mcp_servers.hydra_gateway.server import (  # noqa: E402
    _apply_schema_cache_to_shed, _build_static_tool_list,
)

# Stand-in for a live `tools/list` against
# `node pair-programmer/daemon/dist/index.js mcp-agy`.
FIXTURE_BACKEND_SCHEMAS: dict[str, dict[str, dict]] = {
    "pp_agy": {
        "critique": {
            "type": "object",
            "properties": {
                "artifact_text": {"type": "string"},
                "rubric_md": {"type": "string"},
                "cwd": {"type": "string"},
                "model": {"type": "string"},
                "output_schema": {},
                "timeout_ms": {"type": "number"},
            },
            "required": ["artifact_text", "rubric_md", "cwd"],
            "additionalProperties": False,
        },
        "generate": {
            "type": "object",
            "properties": {
                "prompt": {"type": "string"},
                "cwd": {"type": "string"},
            },
            "required": ["prompt", "cwd"],
            "additionalProperties": False,
        },
    },
}

# What the schema refresh wrote before the fix: shape without substance.
PLACEHOLDER_SCHEMA = {"type": "object", "properties": {}, "required": None}


def _surfaced(server: str, tool: str, schema_cache: dict) -> dict:
    """The inputSchema the gateway advertises for one tool."""
    shed = build_default_shed()
    _apply_schema_cache_to_shed(shed, schema_cache)
    specs = {name: {"type": "stdio"} for name in shed.servers}
    tools = _build_static_tool_list(shed, specs, schema_cache)
    match = [t for t in tools if t["name"] == f"{server}__{tool}"]
    assert match, f"{server}__{tool} not advertised by the gateway"
    return match[0]["inputSchema"]


def test_backend_schema_reaches_catalog_describe_and_tool_definition() -> None:
    """E2-39 parity: a backend-published schema survives every hop.

    The catalogued entry, ``gateway_describe`` and the surfaced tool
    definition must all carry the backend's ``properties`` and ``required``.
    """
    shed = build_default_shed()
    _apply_schema_cache_to_shed(shed, FIXTURE_BACKEND_SCHEMAS)

    described = shed.describe("pp_agy", "critique")
    assert described is not None
    schema = described["input_schema"]
    assert set(schema["properties"]) >= {"artifact_text", "rubric_md", "cwd"}
    assert schema["required"] == ["artifact_text", "rubric_md", "cwd"]

    surfaced = _surfaced("pp_agy", "critique", FIXTURE_BACKEND_SCHEMAS)
    assert surfaced == FIXTURE_BACKEND_SCHEMAS["pp_agy"]["critique"]


def test_every_fixture_backend_schema_lands_non_empty() -> None:
    """Parity sweep: no catalogued tool loses a non-empty backend schema."""
    shed = build_default_shed()
    _apply_schema_cache_to_shed(shed, FIXTURE_BACKEND_SCHEMAS)

    for server, tools in FIXTURE_BACKEND_SCHEMAS.items():
        for tool, backend_schema in tools.items():
            if not backend_schema.get("properties"):
                continue
            described = shed.describe(server, tool)
            assert described is not None, f"{server}.{tool} missing from catalog"
            assert described["input_schema"].get("properties"), (
                f"{server}.{tool} catalogued with an empty schema"
            )


def test_placeholder_cache_entry_never_overwrites_a_real_schema() -> None:
    """Regression: the empty placeholder must not clobber a real schema.

    ``{"type":"object","properties":{},"required":null}`` is truthy, so the
    old ``cached or entry.input_schema`` resolution let it win and stripped
    every parameter from the surfaced tool.
    """
    placeholder_cache = {
        "pp_codex": {"critique": dict(PLACEHOLDER_SCHEMA),
                     "generate": dict(PLACEHOLDER_SCHEMA)},
    }
    shed = build_default_shed()
    _apply_schema_cache_to_shed(shed, placeholder_cache)

    for tool, required in (("critique", ["artifact_text", "rubric_md", "cwd"]),
                           ("generate", ["prompt", "cwd"])):
        described = shed.describe("pp_codex", tool)
        assert described is not None
        schema = described["input_schema"]
        assert schema.get("properties"), f"pp_codex.{tool} stripped by placeholder"
        assert schema["required"] == required

        surfaced = _surfaced("pp_codex", tool, placeholder_cache)
        assert "cwd" in surfaced["properties"]
        assert "cwd" in surfaced["required"]


def test_pp_judge_tools_declare_cwd_without_any_schema_cache() -> None:
    """A fresh checkout (no ~/.hydra cache) still exposes the required args."""
    for server in ("pp_codex", "pp_agy"):
        for tool in ("generate", "critique"):
            surfaced = _surfaced(server, tool, {})
            assert "cwd" in surfaced.get("properties", {}), f"{server}.{tool}"
            assert "cwd" in surfaced.get("required", []), f"{server}.{tool}"
