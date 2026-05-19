"""Shared helpers for thin pack-shim MCP servers (executive_suite, rlm_creative).

Both servers expose roster/skill/command introspection over a Claude-Code pack
directory and a sandboxed output writer. The actual tool-name mapping lives in
each server module; this file just provides the safe filesystem primitives and
the JSON-RPC / MCP-SDK runner used by both.
"""
from __future__ import annotations

import asyncio
import json
import os
import re
import sys
import traceback
from datetime import date
from pathlib import Path
from typing import Any, Callable


# --------- filesystem helpers (path-trust enforced) ---------

def resolve_root(env_var: str, default: str) -> Path:
    root = Path(os.environ.get(env_var, default)).resolve()
    return root


def _safe_join(root: Path, relative: str) -> Path:
    candidate = (root / relative).resolve()
    try:
        candidate.relative_to(root)
    except ValueError as e:
        raise PermissionError(
            f"path {relative!r} escapes root {root!s}"
        ) from e
    return candidate


def read_markdown(root: Path, relative: str) -> dict[str, Any]:
    path = _safe_join(root, relative)
    if not path.exists():
        return {"error": "not_found", "path": str(path)}
    text = path.read_text(encoding="utf-8")
    return {"path": str(path), "content": text}


def list_dir(root: Path, relative: str, *, suffix: str | None = None,
             only_dirs: bool = False) -> list[dict[str, Any]]:
    base = _safe_join(root, relative)
    if not base.exists():
        return []
    out: list[dict[str, Any]] = []
    for child in sorted(base.iterdir()):
        if only_dirs and not child.is_dir():
            continue
        if suffix and child.is_file() and not child.name.endswith(suffix):
            continue
        out.append({
            "name": child.stem if child.is_file() else child.name,
            "path": str(child.relative_to(root)).replace("\\", "/"),
            "is_dir": child.is_dir(),
        })
    return out


_SLUG_RE = re.compile(r"[^a-z0-9]+")


def kebab(text: str) -> str:
    s = _SLUG_RE.sub("-", (text or "untitled").lower()).strip("-")
    return s or "untitled"


def write_output(root: Path, relative_dir: str, topic: str, content: str,
                 *, ext: str = ".md") -> dict[str, Any]:
    safe_topic = kebab(topic)
    today = date.today().isoformat()
    fname = f"{safe_topic}-{today}{ext}"
    target = _safe_join(root, f"{relative_dir.rstrip('/')}/{fname}")
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(content, encoding="utf-8")
    rel = str(target.relative_to(root)).replace("\\", "/")
    return {
        "path": str(target),
        "relative": rel,
        "bytes": len(content.encode("utf-8")),
    }


def read_output(root: Path, relative_path: str) -> dict[str, Any]:
    return read_markdown(root, relative_path)


# --------- runner: MCP SDK preferred, bare-stdio fallback ---------

def _serve_with_mcp_sdk(name: str, handlers: dict[str, Callable[[dict[str, Any]], Any]]) -> bool:
    try:
        from mcp.server import Server  # type: ignore
        from mcp.server.stdio import stdio_server  # type: ignore
        import mcp.types as t  # type: ignore
    except ImportError:
        return False

    server = Server(name)

    @server.list_tools()
    async def _list_tools():
        return [
            t.Tool(name=n, description=n, inputSchema={"type": "object"})
            for n in handlers
        ]

    @server.call_tool()
    async def _call_tool(name: str, arguments: dict):
        if name not in handlers:
            raise ValueError(f"unknown tool: {name}")
        result = handlers[name](arguments or {})
        return [t.TextContent(type="text", text=json.dumps(result, default=str))]

    async def run() -> None:
        async with stdio_server() as (r, w):
            await server.run(r, w, server.create_initialization_options())

    asyncio.run(run())
    return True


def _serve_bare(name: str, handlers: dict[str, Callable[[dict[str, Any]], Any]]) -> None:
    sys.stderr.write(f"{name}: bare-stdio fallback (no mcp SDK)\n")
    for raw in sys.stdin:
        raw = raw.strip()
        if not raw:
            continue
        try:
            msg = json.loads(raw)
        except json.JSONDecodeError as e:
            sys.stdout.write(json.dumps({"error": "parse_error", "detail": str(e)}) + "\n")
            sys.stdout.flush()
            continue
        try:
            method = msg.get("method") or msg.get("tool")
            args = msg.get("params") or msg.get("arguments") or {}
            if method == "list_tools":
                out = {"id": msg.get("id"), "result": list(handlers)}
            elif method in handlers:
                out = {"id": msg.get("id"), "result": handlers[method](args)}
            else:
                out = {"id": msg.get("id"), "error": f"unknown_method: {method!r}"}
        except Exception as e:
            out = {"id": msg.get("id"), "error": str(e),
                   "traceback": traceback.format_exc()}
        sys.stdout.write(json.dumps(out, default=str) + "\n")
        sys.stdout.flush()


def run_server(name: str, handlers: dict[str, Callable[[dict[str, Any]], Any]]) -> None:
    if os.environ.get("HYDRA_MCP_BARE") == "1":
        _serve_bare(name, handlers)
        return
    if not _serve_with_mcp_sdk(name, handlers):
        _serve_bare(name, handlers)
