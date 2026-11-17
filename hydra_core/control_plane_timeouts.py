"""Shared control-plane timeout facts for hydra_control and the gateway.

Stdlib only: this module must not import hydra_core, mcp_servers or mcp, so
both the hydra_control server and the gateway can import it without creating
a cycle.

hydra_control has no cap of its own for ``hydra.repo.resolve``,
``hydra.cockpit.audit`` and ``hydra.envelope.record``: they run in-process
and only internal git or attestor timeouts limit them. The gateway's only
obligation for those three is to exceed its 120 s short default.
``hydra.workflow.finalize`` does have a cap (named by ``FINALIZE_TIMEOUT_ENV``),
and the gateway cap must exceed it by ``GATEWAY_MARGIN_S``.
"""
from __future__ import annotations

import math
import os

FINALIZE_TIMEOUT_ENV: str = "HYDRA_FINALIZE_TIMEOUT_S"
FINALIZE_TIMEOUT_DEFAULT_S: int = 600
GATEWAY_MARGIN_S: float = 60.0

# Full tool names (not final segments) that need the gateway's long class.
HYDRA_CONTROL_LONG_VERBS: frozenset[str] = frozenset({
    "hydra.workflow.finalize",
    "hydra.repo.resolve",
    "hydra.cockpit.audit",
    "hydra.envelope.record",
})


def finalize_timeout_s() -> float:
    """Return the finalize cap in seconds, read live from the environment.

    Falls back to ``FINALIZE_TIMEOUT_DEFAULT_S`` when the value is missing,
    unparseable, zero or negative, or NaN.
    """
    raw = os.environ.get(FINALIZE_TIMEOUT_ENV)
    try:
        value = float(raw) if raw is not None else math.nan
    except (TypeError, ValueError):
        return float(FINALIZE_TIMEOUT_DEFAULT_S)
    if math.isnan(value) or value <= 0:
        return float(FINALIZE_TIMEOUT_DEFAULT_S)
    return value
