"""Tests for hydra_core.memory.search_episodic — the honest full-text backing
for hydra-mem.semantic_search.

No network / no LLM: pure SQLite over a tmp_path db.
"""
from __future__ import annotations

from uuid import uuid4

import pytest

from hydra_core.memory import append_episodic, search_episodic


@pytest.fixture()
def db(tmp_path):
    return tmp_path / "episodic.db"


def test_search_matches_payload_text(db):
    wf = uuid4()
    append_episodic(wf, "decision", {"text": "adopt zircon caching layer"}, db=db)
    append_episodic(wf, "note", {"text": "unrelated content about apples"}, db=db)

    hits = search_episodic("zircon", db=db)
    assert len(hits) == 1
    assert hits[0].tier == "episodic"
    assert "zircon" in hits[0].summary
    # The handle resolves back to the full row.
    assert hits[0].key.startswith("ep:")


def test_search_matches_kind(db):
    wf = uuid4()
    append_episodic(wf, "retrospective", {"text": "nothing notable"}, db=db)
    hits = search_episodic("retrospective", db=db)
    assert len(hits) == 1


def test_search_miss_returns_empty(db):
    append_episodic(uuid4(), "note", {"text": "hello world"}, db=db)
    assert search_episodic("nonexistentterm", db=db) == []


def test_empty_query_returns_empty(db):
    append_episodic(uuid4(), "note", {"text": "hello"}, db=db)
    assert search_episodic("", db=db) == []
    assert search_episodic("   ", db=db) == []


def test_like_wildcards_are_escaped(db):
    # A literal "%" must NOT behave like LIKE's match-all wildcard.
    append_episodic(uuid4(), "note", {"text": "plain text, no percent"}, db=db)
    assert search_episodic("%", db=db) == []
    assert search_episodic("_", db=db) == []


def test_workflow_scope(db):
    wf_a, wf_b = uuid4(), uuid4()
    append_episodic(wf_a, "note", {"text": "shared keyword"}, db=db)
    append_episodic(wf_b, "note", {"text": "shared keyword"}, db=db)

    scoped = search_episodic("shared", workflow_id=wf_a, db=db)
    assert len(scoped) == 1
    unscoped = search_episodic("shared", db=db)
    assert len(unscoped) == 2


def test_k_is_capped_and_respected(db):
    wf = uuid4()
    for i in range(6):
        append_episodic(wf, "note", {"text": f"keyword item {i}"}, db=db)
    assert len(search_episodic("keyword", k=3, db=db)) == 3
    # Over-large k is clamped, not an error.
    assert len(search_episodic("keyword", k=9999, db=db)) == 6


# --- leaf-server TheEights federation (opt-in, threaded, bounded) -------------

def _reset_fed(server, monkeypatch, *, enabled=True, timeout="8"):
    if enabled:
        monkeypatch.setenv("HYDRA_MEM_FEDERATE_EIGHTS", "1")
    else:
        monkeypatch.delenv("HYDRA_MEM_FEDERATE_EIGHTS", raising=False)
    monkeypatch.setenv("HYDRA_MEM_FEDERATE_TIMEOUT", timeout)
    server._EIGHTS_FED_DISABLED = False
    server._FED_EXECUTOR = None


def test_federation_disabled_by_default(monkeypatch):
    import mcp_servers.hydra_memory.server as srv
    _reset_fed(srv, monkeypatch, enabled=False)
    assert srv._maybe_federate_search("q", {}) is None


def test_federation_runs_off_async_thread_and_returns_hits(monkeypatch):
    import mcp_servers.hydra_memory.server as srv
    _reset_fed(srv, monkeypatch)

    class _FakeAtt:
        workflow_id = None
        def memory_search(self, query, top_k=5, workflow_id=None):
            return {"hits": [{"id": "e1", "q": query, "k": top_k, "wf": workflow_id}]}

    srv._EIGHTS_ATTESTOR = _FakeAtt()
    out = srv._maybe_federate_search("quasar", {"k": 3, "workflow_id": "wf-9"})
    assert out == {"hits": [{"id": "e1", "q": "quasar", "k": 3, "wf": "wf-9"}]}


def test_federation_times_out_to_local(monkeypatch):
    import time
    import mcp_servers.hydra_memory.server as srv
    _reset_fed(srv, monkeypatch, timeout="1")

    class _SlowAtt:
        workflow_id = None
        def memory_search(self, query, top_k=5, workflow_id=None):
            time.sleep(5)
            return {"hits": ["late"]}

    srv._EIGHTS_ATTESTOR = _SlowAtt()
    t0 = time.time()
    assert srv._maybe_federate_search("slow", {}) is None
    assert time.time() - t0 < 3  # bounded by the ~1s timeout, not the 5s sleep
