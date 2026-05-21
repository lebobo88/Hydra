"""MCP-backed critique client.

Wraps the pair-programmer `pp_codex.critique` and `pp_gemini.critique` MCP tools
behind the `CritiqueClient` Protocol so the supervisor can score envelopes with
real cross-vendor judgments. Reuses Hydra's existing `MCPStdioDispatcher` to
avoid duplicating MCP-stdio plumbing.

Configuration:
  - User scope (~/.claude.json) must register `pp_codex` and `pp_gemini` servers
    (one stdio entry each, pointing at the compiled pair-programmer daemon).
  - `cwd` defaults to the Hydra project root — PP uses it as the sandbox
    workspace for the critique call.

Failure modes (all surface as JudgeDispatchError via the dispatcher):
  - MCP server missing from user scope (~/.claude.json mcpServers)
  - critique tool returned status="failed"
  - response missing the expected `outcome` field
"""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .schemas import JudgeVendor


_VENDOR_TO_SERVER: dict[JudgeVendor, str] = {
    "codex": "pp_codex",
    "gemini": "pp_gemini",
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

    PP's MCP critique tool returns a CodexResult / GeminiResult envelope of
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

    if outcome not in {"pass", "revise", "fail"}:
        raise RuntimeError(
            f"critique response missing valid outcome/verdict; "
            f"got {outcome!r} in parsed keys={sorted(judgment)}"
        )
    return {
        "outcome": outcome,
        "critique_md": critique,
        "score_json": scores,
    }
