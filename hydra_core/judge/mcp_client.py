"""MCP-backed critique client.

Wraps the pair-programmer `pp_codex.critique` and `pp_agy.critique` MCP tools
behind the `CritiqueClient` Protocol so the supervisor can score envelopes with
real cross-vendor judgments. Reuses Hydra's existing `MCPStdioDispatcher` to
avoid duplicating MCP-stdio plumbing.

Configuration:
  - `pp_codex` and `pp_agy` servers must be reachable via the dispatcher.
    In standalone mode: registered in `~/.claude.json` mcpServers.
    In gateway mode: registered in `~/.hydra/backends.json` (the dispatcher
    checks both locations with backends.json as fallback).
  - `cwd` defaults to the Hydra project root — PP uses it as the sandbox
    workspace for the critique call.

Failure modes (all surface as JudgeDispatchError via the dispatcher):
  - MCP server missing from both ~/.claude.json and ~/.hydra/backends.json
  - critique tool returned status="failed"
  - response missing the expected `outcome` field
"""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .schemas import JudgeVendor


# P2.1: default wall-clock cap (ms) for a codex/agy critique. Was a hardcoded
# 30 min (1_800_000), which OVERRODE the pp daemon's own 5-min self-kill and let a
# slow/mis-authed judge hold a stage for 30 min at ~0 CPU (indistinguishable from a
# dispatch hang). 8 min is ample for a critique; a wedged judge now fails over to a
# `skip` verdict fast. Env-tunable via HYDRA_JUDGE_TIMEOUT_MS.
_DEFAULT_JUDGE_TIMEOUT_MS = 480_000


def _default_judge_timeout_ms() -> int:
    raw = os.environ.get("HYDRA_JUDGE_TIMEOUT_MS")
    try:
        v = int(raw) if raw else _DEFAULT_JUDGE_TIMEOUT_MS
    except (TypeError, ValueError):
        v = _DEFAULT_JUDGE_TIMEOUT_MS
    return v if v > 0 else _DEFAULT_JUDGE_TIMEOUT_MS


_VENDOR_TO_SERVER: dict[JudgeVendor, str] = {
    "codex": "pp_codex",
    "agy": "pp_agy",
    # claude: served via Claude Code subagent dispatch (Phase 3+). Not wired here.
}


@dataclass
class MCPCritiqueClient:
    """A CritiqueClient backed by `MCPStdioDispatcher.call_mcp`.

    `dispatcher` must expose `call_mcp(server: str, tool: str, args: dict)` and
    return `{"status": "done"|"failed", "result": <payload>, ...}` — the same
    shape `hydra_core.dispatcher.MCPStdioDispatcher` produces.
    """
    dispatcher: Any  # MCPStdioDispatcher (kept untyped to avoid a hard import)
    cwd: str | Path
    # Per-call wall-clock budget (ms) forwarded to the critique tool. Defaults to
    # 8 min (env HYDRA_JUDGE_TIMEOUT_MS) so a slow/wedged judge fails over fast
    # instead of holding the stage for the old 30-min ceiling. On the gateway path
    # this also raises the gateway's per-call cap (see AsyncBackendPool
    # ._resolve_tool_timeout); on the direct-dispatch path it flows into the pp
    # daemon CLI runner's own timeout_ms (otherwise capped at the daemon's
    # 5-min default).
    timeout_ms: int = field(default_factory=_default_judge_timeout_ms)

    def critique(
        self,
        *,
        vendor: JudgeVendor,
        artifact_text: str,
        rubric_md: str,
    ) -> dict[str, Any]:
        server = _VENDOR_TO_SERVER.get(vendor)
        if server is None:
            raise RuntimeError(
                f"MCPCritiqueClient does not support vendor={vendor!r}. "
                f"Supported: {sorted(_VENDOR_TO_SERVER)}"
            )
        envelope = self.dispatcher.call_mcp(
            server=server,
            tool="critique",
            args={
                "artifact_text": artifact_text,
                "rubric_md": rubric_md,
                "cwd": str(self.cwd),
                "timeout_ms": self.timeout_ms,
            },
        )
        if not isinstance(envelope, dict) or envelope.get("status") == "failed":
            raise RuntimeError(
                f"pp critique call failed (vendor={vendor}, server={server}): "
                f"{envelope!r}"
            )
        # The dispatcher unwraps MCP TextContent to a dict via _extract_mcp_result;
        # accept either `{result: {...}}` or a bare dict.
        result = envelope.get("result", envelope)
        return _normalize_pp_response(result)


def _normalize_pp_response(raw: Any) -> dict[str, Any]:
    """Coerce PP's critique payload into the {outcome, critique_md, score_json}
    shape the dispatcher's pragmatic-pass guard expects.

    PP's MCP critique tool returns a CodexResult / AgyResult envelope of
    shape:
        {
          "text": "<raw CLI stdout>",
          "parsed": { "outcome": ..., "critique_md": ..., "score": { ... } },
          "tokens_in": ..., "tokens_out": ..., "cost_usd": ..., "model": ..., ...
        }

    The structured judgment lives in `parsed`. We unwrap it and accept any of
    `outcome|verdict` / `critique_md|critique` / `score|scores|score_json`.
    Unknown shapes raise.
    """
    if not isinstance(raw, dict):
        raise RuntimeError(f"unexpected critique payload type: {type(raw).__name__}")

    # Unwrap the PP envelope to its `parsed` block when present.
    judgment: Any = raw.get("parsed") if isinstance(raw.get("parsed"), dict) else raw
    if not isinstance(judgment, dict):
        raise RuntimeError(
            f"critique response had non-dict parsed block: {type(judgment).__name__}"
        )

    outcome = judgment.get("outcome") or judgment.get("verdict")
    critique = judgment.get("critique_md") or judgment.get("critique") or ""
    scores = (
        judgment.get("score")
        or judgment.get("scores")
        or judgment.get("score_json")
        or {}
    )

    if outcome not in {"pass", "revise", "fail", "skip"}:
        raise RuntimeError(
            f"critique response missing valid outcome/verdict; "
            f"got {outcome!r} in parsed keys={sorted(judgment)}"
        )
    return {
        "outcome": outcome,
        "critique_md": critique,
        "score_json": scores,
    }
