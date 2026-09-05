"""Model pricing table + call-cost estimation (B8 fix support).

``hydra_core`` stays runtime-agnostic: this module knows *rates*, not any
vendor's billing API, and imports no provider SDK.

A host result that reports ``tokens_in``/``tokens_out``/``model`` but not a
measured ``cost_usd`` can still be *priced* here so the budget tripwires
(``should_downgrade_model`` / ``should_block_for_budget``) see an honest
estimate instead of silently treating the call as free. See
``hydra_core.governance.record_cost`` (``source="estimated"``) for how a
priced call is folded into the ledger.

Resolution order for a model id's rate:
  1. an operator override file at ``<HYDRA_HOME>/pricing.json``, merged OVER
     the built-in table (an id present in the override wins).
  2. the built-in table below.

``HYDRA_HOME`` is resolved from ``os.environ`` **at call time**, inside
``_load_override_table``, never at import — unlike ``hydra_core/memory.py``
(module-level ``HYDRA_HOME = os.environ.get(...)``), which forces
``tests/conftest.py`` to compensate for the stale binding. Reading the env var
per-call means a test (or host) that sets ``HYDRA_HOME`` after this module has
already been imported still gets the override.

A model id absent from both tables is NOT an error: ``get_rate``/``price_call``
return ``None``, which callers must treat as *unmeasured*, never as free. A
malformed or unreadable override file is also not an error: it is ignored and
the built-in table is used.
"""
from __future__ import annotations

import json
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Optional


@dataclass(frozen=True)
class ModelRate:
    """Per-million-token USD rates, mirroring vendor list pricing.

    ``cache_write_per_mtok``/``cache_read_per_mtok`` default to 0.0 for a
    vendor/model that publishes no separate cache rate (a cache-token count
    of 0 then contributes nothing, which is the correct behaviour for a
    model with no cache tier).
    """

    input_per_mtok: float
    output_per_mtok: float
    cache_write_per_mtok: float = 0.0
    cache_read_per_mtok: float = 0.0


# Seeded with the model ids this repo already names:
#   - Claude host-session ids (this attended engineering harness runs on
#     Claude Code; see CLAUDE.md / session attribution for the live ids).
#     Sonnet rates are the published list rates ($3/$15/$3.75/$0.30 per
#     Mtok in/out/cache-write/cache-read); the sibling tiers are scaled
#     from that anchor since Anthropic has not published separate public
#     list rates for these particular ids at authoring time.
#   - codex judge pins from ``host_bridge._STATIC_JUDGE_MODEL_PINS["codex"]``.
#   - agy (gemini) judge pins from
#     ``host_bridge._STATIC_JUDGE_MODEL_PINS["agy"]``.
_BUILTIN_RATES: dict[str, ModelRate] = {
    "claude-sonnet-5": ModelRate(3.0, 15.0, 3.75, 0.30),
    "claude-opus-5": ModelRate(15.0, 75.0, 18.75, 1.50),
    "claude-fable-5-1": ModelRate(3.0, 15.0, 3.75, 0.30),
    "claude-haiku-4-5-20251001": ModelRate(0.80, 4.0, 1.0, 0.08),
    "gpt-5.6-terra": ModelRate(1.75, 14.0),
    "gpt-5.6-sol": ModelRate(1.75, 14.0),
    "gemini-3.8-flash-medium": ModelRate(0.30, 2.50),
    "gemini-3.1-pro-high": ModelRate(1.25, 10.0),
}


def _load_override_table() -> dict[str, ModelRate]:
    """Read ``<HYDRA_HOME>/pricing.json``, resolved from ``os.environ`` NOW.

    Returns ``{}`` on any error — missing file, unreadable path, malformed
    JSON, or a per-model entry with the wrong shape — so a bad override file
    never crashes the engine; it just means no override applies.
    """
    home = os.environ.get("HYDRA_HOME") or str(Path.home() / ".hydra")
    path = Path(home) / "pricing.json"
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except Exception:  # noqa: BLE001 — missing/unreadable/malformed file: no override
        return {}
    if not isinstance(raw, dict):
        return {}
    out: dict[str, ModelRate] = {}
    for model_id, spec in raw.items():
        if not isinstance(spec, dict):
            continue
        try:
            out[str(model_id)] = ModelRate(
                input_per_mtok=float(spec["input_per_mtok"]),
                output_per_mtok=float(spec["output_per_mtok"]),
                cache_write_per_mtok=float(spec.get("cache_write_per_mtok", 0.0)),
                cache_read_per_mtok=float(spec.get("cache_read_per_mtok", 0.0)),
            )
        except Exception:  # noqa: BLE001 — one bad entry does not poison the rest
            continue
    return out


def get_rate(model_id: str) -> Optional[ModelRate]:
    """Resolve ``model_id``'s rate: override table wins, else builtin, else None."""
    overrides = _load_override_table()
    if model_id in overrides:
        return overrides[model_id]
    return _BUILTIN_RATES.get(model_id)


def price_call(
    model_id: str,
    tokens_in: int,
    tokens_out: int,
    *,
    cache_write_tokens: int = 0,
    cache_read_tokens: int = 0,
) -> Optional[float]:
    """Price one call's token usage in USD, or ``None`` if ``model_id`` is unknown.

    ``None`` is NOT an error — callers (``hydra_core/host_bridge.py``,
    ``hydra_core/transcript_cost.py``) must treat an unknown model as
    *unmeasured*, never as a free ($0.0) call.
    """
    rate = get_rate(model_id)
    if rate is None:
        return None
    cost = (
        tokens_in * rate.input_per_mtok
        + tokens_out * rate.output_per_mtok
        + cache_write_tokens * rate.cache_write_per_mtok
        + cache_read_tokens * rate.cache_read_per_mtok
    ) / 1_000_000.0
    return round(cost, 8)
