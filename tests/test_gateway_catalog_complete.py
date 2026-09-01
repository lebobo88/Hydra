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
