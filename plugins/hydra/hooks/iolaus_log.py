#!/usr/bin/env python3
"""PostToolUse logger — records sub-agent lifecycle close events.

Appends a one-line JSON event to `<project>/.hydra/iolaus.log` for every
completed sub-agent spawn. The supervisor's per-workflow trace remains the
canonical record; this file is the session-level companion log.
"""
from __future__ import annotations

import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path


def main() -> int:
    raw = sys.stdin.read() or "{}"
    try:
        payload = json.loads(raw)
    except json.JSONDecodeError:
        return 0  # never fail the post-hook

    tool_input = payload.get("tool_input") or {}
    target = (
        tool_input.get("subagent_type")
        or tool_input.get("agent")
        or tool_input.get("squad")
    )
    if not target:
        return 0

    out_dir = Path.cwd() / ".hydra"
    out_dir.mkdir(parents=True, exist_ok=True)
    log = out_dir / "iolaus.log"
    record = {
        "ts": datetime.now(timezone.utc).isoformat(),
        "kind": "post_dispatch_session",
        "target": target,
        "tool": payload.get("tool_name"),
        "status": payload.get("status", "unknown"),
    }
    try:
        with log.open("a", encoding="utf-8") as f:
            f.write(json.dumps(record) + os.linesep)
    except OSError:
        pass
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
