#!/usr/bin/env python3
"""PostToolUse logger — records sub-agent lifecycle close events.

Appends a one-line JSON event to `<project>/.hydra/iolaus.log` for every
completed sub-agent spawn. The supervisor's per-workflow trace remains the
canonical record; this file is the session-level companion log.

The log is anchored at the Hydra PROJECT root (`$CLAUDE_PROJECT_DIR`, falling
back to `Path.cwd()` only when that env var is unset), never at the live
`cwd`. During an attended engineering stage `cwd` is the isolated
`.harness/worktrees/attended-*` worktree; anchoring there would create a
second, worktree-local `.hydra/iolaus.log` that both the worktree and the
project root then append to independently, producing a merge conflict on
every attended run (`_merge_worktree_back` / `git merge --abort` /
`pass_unlanded` downgrade). Anchoring at the project root keeps this a single
session-level file regardless of which worktree a hook fires from.
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

    project_root = os.environ.get("CLAUDE_PROJECT_DIR") or str(Path.cwd())
    out_dir = Path(project_root) / ".hydra"
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
