"""Tests for hydra-mem.workflows_list live/stale classification + ordering.

Regression guard for the fix where a bulk `updated_at` bump (e.g. `hydra reap`
re-stamping many closed rows) could page genuinely-active workflows out of the
limit window. Live (non-terminal / gated) rows must sort ahead of terminal ones
regardless of timestamp, and each row carries an additive `live` boolean.
"""
from mcp_servers.hydra_memory import server as s


def test_is_live_predicate():
    assert s._is_live("approval", False) is True      # non-terminal
    assert s._is_live("synthesis", False) is True
    assert s._is_live("done", False) is False          # terminal
    assert s._is_live("surfaced", False) is False
    assert s._is_live("done", True) is True            # terminal but awaiting gate
    assert s._is_live("surfaced", True) is True


def test_workflows_list_live_first_ordering(monkeypatch):
    # Two terminal rows with the NEWEST timestamps (as if just reaped) and one
    # live row that is OLDER — the live row must still come first.
    rows = {
        "wf-terminal-new-1": {"phase": "surfaced", "pending_hitl": None,
                              "root_goal": "old closed A"},
        "wf-terminal-new-2": {"phase": "done", "pending_hitl": None,
                              "root_goal": "old closed B"},
        "wf-live-old":       {"phase": "approval", "pending_hitl": None,
                              "root_goal": "still running"},
    }
    ts = {
        "wf-terminal-new-1": "2026-06-08T12:00:00+00:00",  # newest
        "wf-terminal-new-2": "2026-06-08T11:59:00+00:00",
        "wf-live-old":       "2026-06-01T00:00:00+00:00",  # oldest
    }

    class _FakeConn:
        def close(self):
            pass
    monkeypatch.setattr(s, "_open_checkpoints_ro", lambda: _FakeConn())
    monkeypatch.setattr(s, "_checkpoint_thread_ids", lambda conn, cap=None: list(rows))
    monkeypatch.setattr(s, "_load_state_values",
                        lambda wf: {"values": rows[wf], "ts": ts[wf]})

    handler = s._tool_handlers()["hydra-mem.workflows_list"]
    out = handler({"limit": 50})
    wfs = out["workflows"]

    # Every row carries the additive `live` flag.
    assert all("live" in w for w in wfs)
    by_id = {w["workflow_id"]: w for w in wfs}
    assert by_id["wf-live-old"]["live"] is True
    assert by_id["wf-terminal-new-1"]["live"] is False

    # The live (older) row sorts ahead of the newer terminal rows.
    assert wfs[0]["workflow_id"] == "wf-live-old"
