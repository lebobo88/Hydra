"""Tests for Iolaus — the lifecycle cauterizer."""
from __future__ import annotations

from datetime import date, timedelta
from uuid import uuid4

import pytest

from hydra_core.iolaus import (
    SpawnLedger,
    ledger_for,
    post_dispatch,
    pre_dispatch,
    reset_ledger,
)
from hydra_core.schemas import CSuiteDecisionPacket
from hydra_core.squad_loader import SquadPack
from hydra_core.version import (
    DoubleSpawnRefused,
    SquadDeprecated,
    Version,
    is_deprecated,
    parse_deprecated_after,
)


# --- helpers -----------------------------------------------------------------

def _pack(slug: str = "testing", **over) -> SquadPack:
    defaults = dict(
        slug=slug,
        name=slug,
        description="test",
        entrypoint="stub",
        version=Version(1, 0, 0),
        deprecated_after=None,
        invoke={},
    )
    defaults.update(over)
    return SquadPack(**defaults)


def _envelope(workflow_id=None) -> CSuiteDecisionPacket:
    return CSuiteDecisionPacket(
        workflow_id=workflow_id or uuid4(),
        origin_squad="hydra",
        target_squad="testing",
        origin="BOARDROOM",
        objective="test dispatch",
    )


# --- Version + parse helpers -------------------------------------------------

def test_version_parses_dotted_string():
    v = Version.parse("2.5.7")
    assert (v.major, v.minor, v.patch) == (2, 5, 7)
    assert str(v) == "2.5.7"


def test_version_defaults_missing_components():
    assert Version.parse("3") == Version(3, 0, 0)
    assert Version.parse("3.1") == Version(3, 1, 0)


def test_version_ordering():
    assert Version.parse("1.0.0") < Version.parse("1.0.1")
    assert Version.parse("1.2.0") > Version.parse("1.1.99")


def test_parse_deprecated_after_handles_none_string_and_date():
    assert parse_deprecated_after(None) is None
    assert parse_deprecated_after("2026-01-15") == date(2026, 1, 15)
    assert parse_deprecated_after(date(2026, 2, 1)) == date(2026, 2, 1)


def test_is_deprecated_compares_against_today():
    yesterday = date.today() - timedelta(days=1)
    tomorrow = date.today() + timedelta(days=1)
    assert is_deprecated(yesterday)
    assert not is_deprecated(tomorrow)
    assert not is_deprecated(None)


# --- pre_dispatch ------------------------------------------------------------

def test_pre_dispatch_allows_active_squad():
    pack = _pack()
    env = _envelope()
    verdict = pre_dispatch(pack, env, ledger=SpawnLedger())
    assert verdict.allowed
    assert verdict.event.kind == "pre_dispatch"
    assert verdict.event.squad_slug == "testing"


def test_pre_dispatch_refuses_deprecated_squad():
    yesterday = date.today() - timedelta(days=1)
    pack = _pack(deprecated_after=yesterday)
    env = _envelope()
    with pytest.raises(SquadDeprecated) as exc:
        pre_dispatch(pack, env, ledger=SpawnLedger())
    assert exc.value.slug == "testing"
    assert exc.value.deprecated_after == yesterday


def test_pre_dispatch_allows_archived_replay_bypass():
    yesterday = date.today() - timedelta(days=1)
    pack = _pack(deprecated_after=yesterday)
    env = _envelope()
    # Replay flow: allow_archived bypasses the deprecation refusal.
    verdict = pre_dispatch(pack, env, ledger=SpawnLedger(), allow_archived=True)
    assert verdict.allowed


def test_pre_dispatch_refuses_duplicate_spawn_in_same_workflow():
    pack = _pack()
    env = _envelope()
    led = SpawnLedger()
    pre_dispatch(pack, env, ledger=led)
    with pytest.raises(DoubleSpawnRefused) as exc:
        pre_dispatch(pack, env, ledger=led)
    assert exc.value.slug == "testing"
    assert exc.value.envelope_id == str(env.id)


def test_pre_dispatch_allows_same_squad_for_different_envelope():
    pack = _pack()
    env_a = _envelope()
    env_b = _envelope(workflow_id=env_a.workflow_id)
    led = SpawnLedger()
    pre_dispatch(pack, env_a, ledger=led)
    # Same squad, same workflow, different envelope → permitted (Pentecost
    # cycle, not Legion regeneration).
    verdict = pre_dispatch(pack, env_b, ledger=led)
    assert verdict.allowed


# --- spawn ledger ------------------------------------------------------------

def test_module_ledger_persists_across_calls():
    pack = _pack(slug="ledgered")
    env = _envelope()
    try:
        pre_dispatch(pack, env)  # uses module-level ledger
        with pytest.raises(DoubleSpawnRefused):
            pre_dispatch(pack, env)
    finally:
        reset_ledger(env.workflow_id)


def test_reset_ledger_clears_history():
    pack = _pack(slug="resettable")
    env = _envelope()
    pre_dispatch(pack, env)
    reset_ledger(env.workflow_id)
    # After reset, the same dispatch is allowed again.
    verdict = pre_dispatch(pack, env)
    assert verdict.allowed
    reset_ledger(env.workflow_id)


def test_ledger_for_returns_same_instance():
    wid = uuid4()
    a = ledger_for(wid)
    b = ledger_for(wid)
    assert a is b
    reset_ledger(wid)


# --- post_dispatch -----------------------------------------------------------

def test_post_dispatch_records_status():
    pack = _pack()
    env = _envelope()
    evt = post_dispatch(pack, env, status="done", detail="stub ok")
    assert evt.kind == "post_dispatch"
    assert "status=done" in evt.detail
    assert evt.squad_slug == "testing"


# --- end-to-end via execute_squad -------------------------------------------

def test_execute_squad_attaches_lifecycle_events_to_artifacts():
    """The supervisor reads artifacts to find lifecycle events."""
    from hydra_core.squad_node import execute_squad
    from hydra_core.state import HydraState

    pack = _pack(slug="e2e-stub", entrypoint="stub")
    env = _envelope()
    state = HydraState(workflow_id=env.workflow_id, root_goal="test")

    result = execute_squad(state, pack, env, dispatcher=None)  # stub doesn't need dispatcher
    kinds = [a["data"]["kind"] for a in result.artifacts if a.get("kind") == "lifecycle_event"]
    assert "pre_dispatch" in kinds
    assert "post_dispatch" in kinds
    reset_ledger(env.workflow_id)


def test_execute_squad_returns_failed_on_deprecated_dispatch():
    from hydra_core.squad_node import execute_squad
    from hydra_core.state import HydraState

    yesterday = date.today() - timedelta(days=1)
    pack = _pack(slug="e2e-deprecated", deprecated_after=yesterday, entrypoint="stub")
    env = _envelope()
    state = HydraState(workflow_id=env.workflow_id, root_goal="test")

    result = execute_squad(state, pack, env, dispatcher=None)
    assert result.status == "failed"
    assert "iolaus" in result.rationale.lower()
    assert any(
        a.get("data", {}).get("kind") == "refused_deprecated"
        for a in result.artifacts
    )
    reset_ledger(env.workflow_id)


def test_execute_squad_returns_failed_on_duplicate_spawn():
    from hydra_core.squad_node import execute_squad
    from hydra_core.state import HydraState

    pack = _pack(slug="e2e-dup", entrypoint="stub")
    env = _envelope()
    state = HydraState(workflow_id=env.workflow_id, root_goal="test")

    first = execute_squad(state, pack, env, dispatcher=None)
    assert first.status in ("done", "surfaced", "running")

    second = execute_squad(state, pack, env, dispatcher=None)
    assert second.status == "failed"
    assert "iolaus" in second.rationale.lower()
    assert any(
        a.get("data", {}).get("kind") == "refused_duplicate"
        for a in second.artifacts
    )
    reset_ledger(env.workflow_id)


# --- squad_loader integration ------------------------------------------------

def test_squad_loader_parses_version_and_deprecated_after(tmp_path):
    from hydra_core.squad_loader import discover_squads

    sq_dir = tmp_path / "squads" / "vintage"
    sq_dir.mkdir(parents=True)
    (sq_dir / "squad.yaml").write_text(
        """
name: vintage
version: 2.3.4
deprecated_after: 2030-01-01
entrypoint: stub
""",
        encoding="utf-8",
    )
    packs = discover_squads(tmp_path)
    assert "vintage" in packs
    assert packs["vintage"].version == Version(2, 3, 4)
    assert packs["vintage"].deprecated_after == date(2030, 1, 1)


def test_squad_loader_defaults_version_to_1_0_0(tmp_path):
    from hydra_core.squad_loader import discover_squads

    sq_dir = tmp_path / "squads" / "unversioned"
    sq_dir.mkdir(parents=True)
    (sq_dir / "squad.yaml").write_text(
        """
name: unversioned
entrypoint: stub
""",
        encoding="utf-8",
    )
    packs = discover_squads(tmp_path)
    assert packs["unversioned"].version == Version(1, 0, 0)
    assert packs["unversioned"].deprecated_after is None
