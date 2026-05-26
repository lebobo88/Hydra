"""B8 — TheEights pending-call spool + replay.

Covers the spool primitive (`PendingSpool`) and its integration with
`EightsAttestor`. The bootstrap session lost 5 evolution proposals filed
during eights downtime — this queue is the mechanism that prevents that
loss by spooling durable payloads to disk and replaying them when the
daemon recovers.

Three layers exercised:
  * `PendingSpool` itself — atomic write, count, list, replay drain
    (success / failure / partial / corrupt-file).
  * `EightsAttestor._call` — durable tools spool on failure, ephemeral
    tools don't.
  * `EightsAttestor.replay_pending` — drains the spool through the
    dispatcher when it's healthy again.
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from hydra_core.eights.attestation import EightsAttestor
from hydra_core.eights.pending_spool import PendingSpool, SpooledCall


# ----- PendingSpool unit tests -----


def test_spool_writes_one_json_file_per_call(tmp_path: Path) -> None:
    spool = PendingSpool(root=tmp_path)
    sc = spool.spool(
        tool="eights.evolution.propose",
        args={"slug": "router/v2", "diff": "+ new rule"},
        workflow_id="wf-1",
        reason="exception:TimeoutError",
    )

    files = list(tmp_path.iterdir())
    assert len(files) == 1
    assert files[0].name == f"{sc.id}.json"
    payload = json.loads(files[0].read_text("utf-8"))
    assert payload["tool"] == "eights.evolution.propose"
    assert payload["args"] == {"slug": "router/v2", "diff": "+ new rule"}
    assert payload["workflow_id"] == "wf-1"
    assert payload["reason"] == "exception:TimeoutError"
    assert payload["id"] == sc.id


def test_spool_count_zero_when_dir_absent(tmp_path: Path) -> None:
    spool = PendingSpool(root=tmp_path / "never-created")
    assert spool.count() == 0
    assert spool.list_pending() == []


def test_spool_atomic_write_no_partial_visible(tmp_path: Path) -> None:
    spool = PendingSpool(root=tmp_path)
    spool.spool(tool="eights.constitution.attest", args={"hash": "sha256:abc"})
    # No `.json.partial` should ever be visible after a successful spool
    assert not any(p.suffix == ".partial" for p in tmp_path.iterdir())


def test_replay_drains_on_success_deletes_files(tmp_path: Path) -> None:
    spool = PendingSpool(root=tmp_path)
    spool.spool(tool="eights.evolution.propose", args={"slug": "a"})
    spool.spool(tool="eights.evolution.propose", args={"slug": "b"})
    assert spool.count() == 2

    delivered: list[tuple[str, dict[str, Any]]] = []

    def send(tool: str, args: dict[str, Any]) -> Any:
        delivered.append((tool, args))
        return {"status": "done"}

    summary = spool.replay(send)
    assert summary == {"sent": 2, "failed": 0, "skipped": 0}
    assert spool.count() == 0
    # Both calls reached the sender
    assert {a["slug"] for _, a in delivered} == {"a", "b"}


def test_replay_leaves_failed_entries_on_disk(tmp_path: Path) -> None:
    spool = PendingSpool(root=tmp_path)
    spool.spool(tool="eights.evolution.propose", args={"slug": "ok"})
    spool.spool(tool="eights.evolution.propose", args={"slug": "fail"})

    def send(_tool: str, args: dict[str, Any]) -> Any:
        if args.get("slug") == "fail":
            raise RuntimeError("daemon still flaky")
        return {"status": "done"}

    summary = spool.replay(send)
    assert summary["sent"] == 1
    assert summary["failed"] == 1
    # The failed entry stays for the next replay attempt
    assert spool.count() == 1
    remaining = spool.list_pending()
    assert remaining[0].args == {"slug": "fail"}


def test_replay_returning_none_counts_as_failure(tmp_path: Path) -> None:
    spool = PendingSpool(root=tmp_path)
    spool.spool(tool="eights.constitution.attest", args={"hash": "x"})

    def send(_tool: str, _args: dict[str, Any]) -> Any:
        return None

    summary = spool.replay(send)
    assert summary["failed"] == 1
    assert spool.count() == 1


def test_replay_skips_corrupt_files_silently(tmp_path: Path) -> None:
    spool = PendingSpool(root=tmp_path)
    spool.spool(tool="eights.evolution.propose", args={"slug": "good"})
    # Corrupt file — invalid JSON but ends in .json so the iterator picks it up
    (tmp_path / "ZZZZ-bad.json").write_text("{not valid json", encoding="utf-8")

    def send(_tool: str, _args: dict[str, Any]) -> Any:
        return {"status": "done"}

    summary = spool.replay(send)
    assert summary["sent"] == 1
    # The corrupt file is still on disk for an operator to inspect
    assert (tmp_path / "ZZZZ-bad.json").exists()


def test_replay_empty_spool_returns_zero_summary(tmp_path: Path) -> None:
    spool = PendingSpool(root=tmp_path)
    summary = spool.replay(lambda _t, _a: {"status": "done"})
    assert summary == {"sent": 0, "failed": 0, "skipped": 0}


# ----- EightsAttestor integration tests -----


class _DownDispatcher:
    """Dispatcher that always raises on call_mcp."""
    def call_mcp(self, *_a: Any, **_k: Any) -> Any:
        raise ConnectionError("eights-daemon -32000")


class _UpDispatcher:
    """Dispatcher that always succeeds. Records every call for verification."""
    def __init__(self) -> None:
        self.calls: list[tuple[str, str, dict[str, Any]]] = []

    def call_mcp(self, server: str, tool: str, args: dict[str, Any], **_kw: Any) -> dict[str, Any]:
        self.calls.append((server, tool, args))
        return {"status": "done", "tool": tool, "result": {"ok": True}}


def test_attestor_spools_durable_tool_on_failure(tmp_path: Path) -> None:
    attestor = EightsAttestor(
        dispatcher=_DownDispatcher(),
        workflow_id="wf-X",
        spool=PendingSpool(root=tmp_path),
    )
    result = attestor._call("eights.evolution.propose", {"slug": "router/v2"})
    assert result is None
    # Durable tool → spooled
    assert attestor.pending_count() == 1
    spooled = attestor.spool.list_pending()
    assert spooled[0].tool == "eights.evolution.propose"
    assert spooled[0].workflow_id == "wf-X"
    assert spooled[0].reason.startswith("exception:")


def test_attestor_does_not_spool_ephemeral_tool(tmp_path: Path) -> None:
    attestor = EightsAttestor(
        dispatcher=_DownDispatcher(),
        spool=PendingSpool(root=tmp_path),
    )
    # ceiling_tick is ephemeral — by the time the daemon is back, the tick
    # would be stale. Spool MUST stay empty.
    attestor.ceiling_tick(workflow_id="wf-X", node="intake")
    assert attestor.pending_count() == 0


def test_attestor_replays_when_daemon_recovers(tmp_path: Path) -> None:
    spool = PendingSpool(root=tmp_path)
    # Down phase — spool a real durable proposal
    down = EightsAttestor(dispatcher=_DownDispatcher(), workflow_id="wf-A", spool=spool)
    down._call("eights.evolution.propose", {"slug": "router/v2"})
    down._call("eights.constitution.attest", {"hash": "sha256:zzz"})
    assert spool.count() == 2

    # Up phase — a new workflow takes over, dispatcher is healthy now
    up_dispatcher = _UpDispatcher()
    up = EightsAttestor(dispatcher=up_dispatcher, workflow_id="wf-B", spool=spool)
    summary = up.replay_pending()
    assert summary == {"sent": 2, "failed": 0, "skipped": 0}
    assert spool.count() == 0
    delivered_tools = sorted(t for _, t, _ in up_dispatcher.calls)
    assert delivered_tools == [
        "eights.constitution.attest",
        "eights.evolution.propose",
    ]


def test_attestor_replay_noop_when_disabled(tmp_path: Path) -> None:
    spool = PendingSpool(root=tmp_path)
    spool.spool(tool="eights.evolution.propose", args={"slug": "x"})
    attestor = EightsAttestor(dispatcher=None, enabled=False, spool=spool)
    summary = attestor.replay_pending()
    assert summary == {"sent": 0, "failed": 0, "skipped": 0}
    # Spool is preserved for when the daemon is later enabled
    assert spool.count() == 1
