#!/usr/bin/env python3
"""Claude Code session-level Iolaus check.

Wired by `.claude-plugin/hooks.json` as PreToolUse on the Task tool. Reads the
project's squad registry and refuses to spawn a sub-agent whose `subagent_type`
matches a squad slug past its `deprecated_after` date.

This is the *Claude-Code-session* layer of Iolaus. The hard enforcement still
lives in `hydra_core/iolaus.py` — the supervisor calls `pre_dispatch` on every
squad invocation. This hook is the first line: it stops a deprecated squad
from even being spawned by the sub-agent system.

Exit codes:
    0  → allow
    2  → block (Claude Code surfaces the message to the user)

Reads JSON tool-call payload from stdin (Claude Code hook contract).
"""
from __future__ import annotations

import json
import sys
from datetime import date, datetime, timezone
from pathlib import Path


def _project_root() -> Path:
    # The hook runs from the project working directory.
    return Path.cwd()


def _load_deprecated_slugs(root: Path) -> dict[str, str]:
    """Return {slug: deprecated_after_iso} for every squad past its date."""
    import yaml  # type: ignore

    today = datetime.now(timezone.utc).date()
    out: dict[str, str] = {}
    squads_dir = root / "squads"
    if not squads_dir.is_dir():
        return out
    for child in squads_dir.iterdir():
        if not child.is_dir():
            continue
        ym = child / "squad.yaml"
        if not ym.is_file():
            continue
        try:
            data = yaml.safe_load(ym.read_text(encoding="utf-8")) or {}
        except Exception:
            continue
        raw = data.get("deprecated_after")
        if raw in (None, "null", "None"):
            continue
        try:
            d = date.fromisoformat(str(raw))
        except ValueError:
            continue
        if today >= d:
            out[child.name] = d.isoformat()
    return out


def _read_payload() -> dict:
    raw = sys.stdin.read() or "{}"
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        return {}


def main() -> int:
    payload = _read_payload()
    tool_input = payload.get("tool_input") or payload.get("input") or {}
    # The Task tool carries `subagent_type` in its input. Other tools we leave alone.
    target = (
        tool_input.get("subagent_type")
        or tool_input.get("agent")
        or tool_input.get("squad")
    )
    if not target:
        return 0
    deprecated = _load_deprecated_slugs(_project_root())
    if target in deprecated:
        sys.stderr.write(
            f"Iolaus refused: squad '{target}' was deprecated on "
            f"{deprecated[target]}. The stump is cauterized — heads do not "
            "regrow at this slug. Choose an active squad or revive the pack "
            "by editing its squad.yaml.\n"
        )
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
