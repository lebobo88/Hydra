"""Sum + price a subagent transcript's per-turn token usage (Part 5, B8).

A host driving ``hydra.workflow.plan`` / ``hydra.workflow.step`` /
``hydra.workflow.submit_host_result`` spawns an anonymous subagent (e.g. an
``Agent`` tool call) whose transcript is a JSONL file — one JSON object per
line — where each assistant turn carries a ``message`` object with a
``usage`` block (``input_tokens``, ``output_tokens``,
``cache_creation_input_tokens``, ``cache_read_input_tokens``) and a
``model`` id. This module sums that usage across every turn and prices it
through ``hydra_core.pricing`` so the host can report a *real* ``cost_usd``
to ``submit_host_result`` instead of omitting it (which — see B8 — used to
be silently treated as a $0.0 stage).

Any host driving the attended engineering loop should use
``summarize_transcript_cost`` rather than re-deriving this ad hoc; it is the
same technique used to reconstruct a real ``cost_usd`` for an engineer/judge
subagent that does not self-report spend.
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .pricing import price_call


@dataclass
class TranscriptCostSummary:
    """Aggregate token usage + priced cost for one subagent transcript.

    ``cost_usd`` only includes turns whose model was priceable (present in
    ``hydra_core.pricing``'s built-in table or the ``HYDRA_HOME`` override).
    ``unpriced_models`` lists any model id seen in the transcript that could
    not be priced — those turns' tokens still count toward
    ``tokens_in``/``tokens_out``/etc, but contribute $0.0 to ``cost_usd``, so
    a caller can tell whether the total is a complete or partial estimate.
    """

    tokens_in: int = 0
    tokens_out: int = 0
    cache_write_tokens: int = 0
    cache_read_tokens: int = 0
    cost_usd: float = 0.0
    turns: int = 0
    unpriced_models: set[str] = field(default_factory=set)

    @property
    def fully_priced(self) -> bool:
        """True when every turn with a known model contributed to cost_usd."""
        return not self.unpriced_models


def _iter_turn_usage(path: str | Path) -> list[tuple[str, dict[str, Any]]]:
    """Yield (model, usage_dict) for every line with a usable ``message.usage``.

    Malformed lines (non-JSON, missing ``message``/``usage``) are skipped —
    a corrupt or partial transcript must not crash the caller; it just
    contributes less data to the summary.
    """
    out: list[tuple[str, dict[str, Any]]] = []
    try:
        text = Path(path).read_text(encoding="utf-8")
    except OSError:
        return out
    for line in text.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            obj = json.loads(line)
        except (ValueError, TypeError):
            continue
        if not isinstance(obj, dict):
            continue
        message = obj.get("message")
        if not isinstance(message, dict):
            continue
        usage = message.get("usage")
        if not isinstance(usage, dict):
            continue
        model = str(message.get("model") or "")
        out.append((model, usage))
    return out


def _safe_int(value: Any) -> int:
    """Coerce a usage field to ``int``, tolerating malformed JSON values.

    A syntactically valid JSONL line can still carry a non-integer token
    value (a string, a nested object, a list, ``None``) — this module's own
    docstring promises tolerance of malformed lines, so a value that cannot
    be interpreted as a whole number of tokens contributes 0 rather than
    raising ``TypeError``/``ValueError`` out of ``int(...)``.
    """
    if isinstance(value, bool):
        # bool is an int subclass; treat it like any other non-numeric junk.
        return 0
    if isinstance(value, (int, float)):
        return int(value)
    if isinstance(value, str):
        try:
            return int(value.strip())
        except ValueError:
            try:
                return int(float(value.strip()))
            except (ValueError, TypeError):
                return 0
    return 0


def summarize_transcript_cost(path: str | Path) -> TranscriptCostSummary:
    """Sum + price every turn's usage in a subagent transcript JSONL file.

    A missing/unreadable file or one with no usable ``message.usage`` lines
    yields a zeroed summary (``turns=0``) rather than raising — the caller
    treats a zeroed summary the same way ``hydra_core.host_bridge`` treats an
    unreportable host result: unmeasured, not an error.
    """
    summary = TranscriptCostSummary()
    for model, usage in _iter_turn_usage(path):
        tin = _safe_int(usage.get("input_tokens"))
        tout = _safe_int(usage.get("output_tokens"))
        cache_write = _safe_int(usage.get("cache_creation_input_tokens"))
        cache_read = _safe_int(usage.get("cache_read_input_tokens"))

        summary.turns += 1
        summary.tokens_in += tin
        summary.tokens_out += tout
        summary.cache_write_tokens += cache_write
        summary.cache_read_tokens += cache_read

        if not model:
            continue
        priced = price_call(
            model, tin, tout,
            cache_write_tokens=cache_write, cache_read_tokens=cache_read,
        )
        if priced is None:
            summary.unpriced_models.add(model)
            continue
        summary.cost_usd += priced

    summary.cost_usd = round(summary.cost_usd, 8)
    return summary
