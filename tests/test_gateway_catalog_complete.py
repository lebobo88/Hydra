from __future__ import annotations

import json
from pathlib import Path

from hydra_core.toolshed import build_default_shed


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
