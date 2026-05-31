"""Offline schema-cache refresher for the Hydra gateway.

Connects to every configured backend MCP server once, fetches each tool's
*real* ``inputSchema``, and writes ``~/.hydra/gateway_schemas.json`` in the
shape ``{server: {tool_name: inputSchema}}``.

The gateway (``server.py``) loads this cache at startup and overlays it onto
the static toolshed catalog, so advertised tools carry typed params. Without
real schemas the gateway falls back to ``{"type":"object"}``, which strips
type info and mangles nested objects / numeric args on the proxy hop.

Run it on demand (never at gateway startup or inside ``list_tools`` — keep
those fast and connection-free):

    python -m mcp_servers.hydra_gateway.refresh_schemas

The ``_SELF_NAMES`` guard inside ``AsyncBackendPool`` excludes
``hydra_gateway``/``hydra_toolshed`` so the refresh can never recurse into the
gateway itself.
"""
from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path
from typing import Any

_HERE = Path(__file__).resolve()
sys.path.insert(0, str(_HERE.parents[2]))

from hydra_core.dispatcher import _load_backend_registry  # noqa: E402
from mcp_servers.hydra_gateway.server import (  # noqa: E402
    AsyncBackendPool, GATEWAY_SCHEMA_CACHE,
)


async def _collect(pool: AsyncBackendPool) -> dict[str, dict[str, Any]]:
    """Fetch real schemas from every reachable backend."""
    out: dict[str, dict[str, Any]] = {}
    for server in pool.server_names:
        tools = await pool.list_tools(server)
        if not tools:
            print(f"  [skip]  {server}: unreachable or no tools")
            continue
        schemas = {
            tool["name"]: tool["inputSchema"]
            for tool in tools
            if isinstance(tool.get("inputSchema"), dict)
        }
        out[server] = schemas
        print(f"  [ok]    {server}: {len(schemas)} schema(s)")
    return out


async def _run(path: Path) -> int:
    specs = _load_backend_registry()
    if not specs:
        print("No backends configured in ~/.hydra/backends.json")
        return 1
    pool = AsyncBackendPool(specs)
    print(f"Refreshing schemas from {len(pool.server_names)} backend(s)…")
    try:
        cache = await _collect(pool)
    finally:
        await pool.close()

    if not cache:
        print("No schemas collected — leaving existing cache untouched.")
        return 1

    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(cache, indent=2, default=str), encoding="utf-8")
    total = sum(len(v) for v in cache.values())
    print(f"\nWrote {total} tool schema(s) across {len(cache)} backend(s) to {path}")
    return 0


def main(argv: list[str] | None = None) -> int:
    path = GATEWAY_SCHEMA_CACHE
    if argv and argv[0] not in ("-h", "--help"):
        path = Path(argv[0])
    return asyncio.run(_run(path))


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
