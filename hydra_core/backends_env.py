"""``${VAR}`` expansion for MCP backend specs (E2-5).

Backend specs (``~/.hydra/backends.json``, ``~/.claude.json`` mcpServers,
``.mcp.json``) carry an ``env`` map that is handed verbatim to the stdio child
process.  Historically the only way to give a backend a secret was to write the
secret *inline* into that JSON file, which puts long-lived key material into a
plaintext config that is read by every dispatcher, copied by
``hydra gateway-backup``, and easy to leak.

This module lets a spec reference the parent process environment instead::

    "env": {"HYDRA_OPERATOR_KEY": "${HYDRA_OPERATOR_KEY}"}
    "env": {"PP_TIER": "${PP_TIER:-standard}"}

Both the gateway backend pool (``mcp_servers/hydra_gateway/server.py``) and the
direct dispatcher (``hydra_core.dispatcher.MCPStdioDispatcher``) run every spec
through :func:`expand_spec_env` before building ``StdioServerParameters``, so
the two paths cannot drift.

Contract:

* ``${VAR}``            -> ``os.environ["VAR"]``; if unset, log a WARNING that
  names the variable (never a value) and substitute the empty string.
* ``${VAR:-default}``   -> ``os.environ["VAR"]`` when set and non-empty,
  otherwise ``default``.  A default never warns.
* ``$${...}``           -> a literal ``${...}`` (escape hatch for a value that
  really does contain the sigil).
* Anything else is passed through untouched.

Expansion is **fail-soft by design**: a missing variable must never crash a
dispatch.  A backend that needs the secret fails closed on its own terms (an
empty operator key degrades WS-AUTH), which is a clearer failure than an
exception thrown from transport setup.

No function in this module ever logs, returns, or embeds a *resolved* value in
a message.  :func:`looks_like_inline_secret` is a shape check only.
"""

from __future__ import annotations

import logging
import os
import re
from typing import Any, Mapping

logger = logging.getLogger(__name__)

__all__ = [
    "expand_env_refs",
    "expand_spec_env",
    "has_env_ref",
    "looks_like_inline_secret",
]

# ${VAR} or ${VAR:-default}.  A doubled sigil ($${...}) is handled by the
# replacement callback below, which sees the leading "$" via the escape group.
_REF_RE = re.compile(
    r"(?P<escape>\$?)\$\{(?P<name>[A-Za-z_][A-Za-z0-9_]*)(?::-(?P<default>[^}]*))?\}"
)

# A 64-character hex string — the shape of the operator key minted by
# ``hydra_core.auth.capability``.  Used ONLY to warn that a config file looks
# like it holds inline key material; the value itself is never printed.
_INLINE_SECRET_RE = re.compile(r"^[0-9a-fA-F]{64}$")


def has_env_ref(value: Any) -> bool:
    """True when ``value`` is a string containing at least one ``${VAR}`` ref."""
    if not isinstance(value, str):
        return False
    return any(m.group("escape") != "$" for m in _REF_RE.finditer(value))


def looks_like_inline_secret(value: Any) -> bool:
    """True when ``value`` has the shape of an inline 64-hex secret.

    Pattern check only.  Callers use this to emit a "move this to the
    environment" warning; the value must never be printed or logged.
    """
    return isinstance(value, str) and bool(_INLINE_SECRET_RE.match(value.strip()))


def expand_env_refs(
    value: Any,
    *,
    environ: Mapping[str, str] | None = None,
    context: str = "",
) -> Any:
    """Expand ``${VAR}`` / ``${VAR:-default}`` references inside ``value``.

    Non-string values are returned unchanged.  A reference to a variable that
    is unset (or set to the empty string) and carries no default expands to
    ``""`` and logs one WARNING naming the variable.
    """
    if not isinstance(value, str):
        return value

    env = os.environ if environ is None else environ
    where = f" ({context})" if context else ""

    def _sub(match: "re.Match[str]") -> str:
        if match.group("escape") == "$":
            # "$${VAR}" -> literal "${VAR}"
            return match.group(0)[1:]
        name = match.group("name")
        default = match.group("default")
        resolved = env.get(name) or ""
        if resolved:
            return resolved
        if default is not None:
            return default
        logger.warning(
            "backend env reference ${%s} is unset%s; substituting empty string. "
            "Set %s in the environment that launches Hydra.",
            name, where, name,
        )
        return ""

    return _REF_RE.sub(_sub, value)


def expand_spec_env(
    spec: Mapping[str, Any],
    *,
    environ: Mapping[str, str] | None = None,
    server: str | None = None,
) -> dict[str, Any]:
    """Return a copy of ``spec`` with ``env`` values and ``args`` expanded.

    The input mapping is never mutated — callers pass the registry's own dict
    and must not have it rewritten under them.  ``env`` keys are left alone;
    only values are expanded.  ``command`` and ``cwd`` are expanded too so a
    spec can reference a root path from the environment.
    """
    out: dict[str, Any] = dict(spec)
    ctx = f"server={server}" if server else ""

    env = spec.get("env")
    if isinstance(env, Mapping):
        out["env"] = {
            str(k): expand_env_refs(
                v, environ=environ, context=f"{ctx} key={k}" if ctx else f"key={k}"
            )
            for k, v in env.items()
        }

    args = spec.get("args")
    if isinstance(args, (list, tuple)):
        out["args"] = [
            expand_env_refs(a, environ=environ, context=ctx) for a in args
        ]

    for field in ("command", "cwd"):
        if isinstance(spec.get(field), str):
            out[field] = expand_env_refs(
                spec[field], environ=environ, context=ctx
            )

    return out
