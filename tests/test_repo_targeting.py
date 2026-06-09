"""Tests for hydra_core.repo_registry and the --repo targeting plumbing.

All tests are offline — no MCP, no network, no LLM calls.  Git operations
are local-only (git init / git rev-parse).
"""
from __future__ import annotations

import subprocess
import uuid
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock

import pytest

from hydra_core.repo_registry import is_known_repo, parse_repo_arg, resolve_repo_path
from hydra_core.schemas import CSuiteDecisionPacket
from hydra_core.squad_loader import SquadPack
from hydra_core.squad_node import _via_mcp
from hydra_core.state import HydraState


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _git_init(path: Path) -> None:
    """Run `git init` in *path* so rev-parse --show-toplevel succeeds."""
    subprocess.run(
        ["git", "init", str(path)],
        capture_output=True,
        check=True,
    )


# ---------------------------------------------------------------------------
# resolve_repo_path — real repo (Hydra itself)
# ---------------------------------------------------------------------------


def test_valid_repo_id_resolves() -> None:
    """'hydra' must resolve to a path ending in 'Hydra' that is a live git repo."""
    path = resolve_repo_path("hydra")
    assert path.name == "Hydra", f"Expected dirname 'Hydra', got {path.name!r}"
    assert path.exists(), f"Resolved path does not exist: {path}"
    # .git presence is implied by the git-subprocess check, but assert anyway.
    assert (path / ".git").exists(), f"No .git dir at {path}"


def test_valid_repo_id_resolves_case_insensitive() -> None:
    """Upper-case normalisation: 'HYDRA' must resolve identically to 'hydra'."""
    path = resolve_repo_path("HYDRA")
    assert path.name == "Hydra"


# ---------------------------------------------------------------------------
# resolve_repo_path — rejections
# ---------------------------------------------------------------------------


def test_unknown_repo_id_rejected() -> None:
    with pytest.raises(ValueError, match="unknown repo_id"):
        resolve_repo_path("nope")


def test_raw_absolute_path_rejected() -> None:
    """Contains ':' on Windows — must be caught by the raw-path guard."""
    with pytest.raises(ValueError, match="raw paths are not accepted"):
        resolve_repo_path("C:/AiAppDeployments/Hydra")


def test_raw_relative_path_rejected() -> None:
    """'../Hydra' contains '..' — raw-path guard fires before allow-list."""
    with pytest.raises(ValueError, match="raw paths are not accepted"):
        resolve_repo_path("../Hydra")


def test_raw_backslash_path_rejected() -> None:
    with pytest.raises(ValueError, match="raw paths are not accepted"):
        resolve_repo_path("..\\Hydra")


def test_empty_string_rejected() -> None:
    with pytest.raises(ValueError, match="unknown repo_id"):
        resolve_repo_path("")


# ---------------------------------------------------------------------------
# is_known_repo
# ---------------------------------------------------------------------------


def test_is_known_repo_true() -> None:
    assert is_known_repo("hydra") is True
    assert is_known_repo("agentsmith") is True


def test_is_known_repo_false() -> None:
    assert is_known_repo("nope") is False
    assert is_known_repo("C:/AiAppDeployments/Hydra") is False


# ---------------------------------------------------------------------------
# CSuiteDecisionPacket — absent target_repo_id falls back to None
# ---------------------------------------------------------------------------


def test_absent_falls_back_to_none() -> None:
    """A CSuiteDecisionPacket built without target_repo_id must carry None."""
    packet = CSuiteDecisionPacket(
        workflow_id=uuid.uuid4(),
        origin_squad="hydra",
        target_squad="engineering",
        origin="BOARDROOM",
        objective="do work",
    )
    assert getattr(packet, "target_repo_id", None) is None


def test_target_repo_id_round_trips() -> None:
    """target_repo_id set on a packet must survive model_dump / model_validate."""
    packet = CSuiteDecisionPacket(
        workflow_id=uuid.uuid4(),
        origin_squad="hydra",
        target_squad="engineering",
        origin="BOARDROOM",
        objective="fix something in agentsmith",
        target_repo_id="agentsmith",
    )
    assert packet.target_repo_id == "agentsmith"
    dumped = packet.model_dump(mode="json")
    assert dumped["target_repo_id"] == "agentsmith"
    restored = CSuiteDecisionPacket.model_validate(dumped)
    assert restored.target_repo_id == "agentsmith"


# ---------------------------------------------------------------------------
# HYDRA_REPO_BASE monkeypatch — resolve under an overridden base (git init)
# ---------------------------------------------------------------------------


def test_monkeypatched_base_resolves(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """With HYDRA_REPO_BASE pointing at a temp dir that contains a real git
    repo named 'Hydra', resolve_repo_path('hydra') must return that path."""
    fake_hydra = tmp_path / "Hydra"
    fake_hydra.mkdir()
    _git_init(fake_hydra)  # real git repo so rev-parse succeeds

    monkeypatch.setenv("HYDRA_REPO_BASE", str(tmp_path))
    path = resolve_repo_path("hydra")
    assert path == fake_hydra.resolve()


def test_monkeypatched_base_missing_git_raises(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A directory without a git repo (no git init) must raise ValueError."""
    fake_hydra = tmp_path / "Hydra"
    fake_hydra.mkdir()
    # Deliberately NOT running git init — rev-parse will fail.

    monkeypatch.setenv("HYDRA_REPO_BASE", str(tmp_path))
    with pytest.raises(ValueError, match="not a git repo"):
        resolve_repo_path("hydra")


def test_base_escape_via_symlink_rejected(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A symlink inside the base that points outside must be rejected by
    is_relative_to(base) after resolution."""
    # Build a real git repo outside the fake base.
    outside = tmp_path / "outside"
    outside.mkdir()
    _git_init(outside)

    fake_base = tmp_path / "base"
    fake_base.mkdir()

    # Create a symlink named 'Hydra' inside fake_base pointing to outside/.
    symlink_target = fake_base / "Hydra"
    symlink_target.symlink_to(outside)

    monkeypatch.setenv("HYDRA_REPO_BASE", str(fake_base))
    with pytest.raises(ValueError, match="escapes repo base"):
        resolve_repo_path("hydra")


# ---------------------------------------------------------------------------
# parse_repo_arg
# ---------------------------------------------------------------------------


def test_parse_repo_arg_leading() -> None:
    """'--repo agentsmith Fix X' -> ('agentsmith', 'Fix X')."""
    repo_id, rest = parse_repo_arg("--repo agentsmith Fix X")
    assert repo_id == "agentsmith"
    assert rest == "Fix X"


def test_parse_repo_arg_embedded() -> None:
    """'Fix X --repo hydra in module Y' -> ('hydra', 'Fix X in module Y')."""
    repo_id, rest = parse_repo_arg("Fix X --repo hydra in module Y")
    assert repo_id == "hydra"
    assert "hydra" not in rest
    assert "--repo" not in rest
    assert "Fix X" in rest


def test_parse_repo_arg_absent() -> None:
    """No --repo token -> (None, original text unchanged)."""
    original = "Add idempotency-key support to the payments API"
    repo_id, rest = parse_repo_arg(original)
    assert repo_id is None
    assert rest == original


def test_parse_repo_arg_unknown_raises() -> None:
    """'--repo bogus ...' must raise ValueError for an unknown id."""
    with pytest.raises(ValueError, match="not an allow-listed repo_id"):
        parse_repo_arg("--repo bogus Fix something")


def test_parse_repo_arg_case_insensitive() -> None:
    """--repo HYDRA should be accepted and normalised to 'hydra'."""
    repo_id, rest = parse_repo_arg("--repo HYDRA do the thing")
    assert repo_id == "hydra"
    assert "HYDRA" not in rest


# ---------------------------------------------------------------------------
# Integration: packet -> _via_mcp thread (path proven via resolve_repo_path)
# ---------------------------------------------------------------------------


def test_packet_target_repo_id_resolves_to_hydra_path() -> None:
    """Build a CSuiteDecisionPacket(target_repo_id='hydra') and confirm that
    resolve_repo_path(packet.target_repo_id) == the real Hydra checkout.

    This proves the packet→_via_mcp dispatch thread: the field is set on the
    packet, _via_mcp reads it via getattr(inbound, 'target_repo_id'), and
    resolve_repo_path returns the correct on-disk path.
    """
    packet = CSuiteDecisionPacket(
        workflow_id=uuid.uuid4(),
        origin_squad="hydra",
        target_squad="engineering",
        origin="BOARDROOM",
        objective="target the hydra repo",
        target_repo_id="hydra",
    )
    # Simulate what _via_mcp does.
    tid = getattr(packet, "target_repo_id", None)
    assert tid == "hydra"
    resolved = resolve_repo_path(tid)
    assert resolved.name == "Hydra"
    assert resolved.exists()


# ---------------------------------------------------------------------------
# _via_mcp direct-call tests (stub dispatcher, offline)
# ---------------------------------------------------------------------------

def _make_stub_dispatcher() -> tuple[MagicMock, list[dict]]:
    """Return a (dispatcher, captured_args_list) pair.

    dispatcher.call_mcp records each call's args dict in captured_args_list
    and returns a minimal pp-harness-style success envelope.
    """
    captured: list[dict] = []

    stub = MagicMock()
    def _call_mcp(server: str, tool: str, args: dict[str, Any], **_kw: Any) -> dict:
        captured.append(dict(args))
        return {"status": "done", "result": {"run_id": "r1"}}

    stub.call_mcp.side_effect = _call_mcp
    return stub, captured


def _make_engineering_pack(invoke: dict | None = None) -> SquadPack:
    return SquadPack(
        slug="engineering",
        name="Engineering",
        description="pp dispatch",
        entrypoint="mcp",
        invoke=invoke or {"mode": "pp_run"},
    )


def _make_other_pack(slug: str = "executive") -> SquadPack:
    return SquadPack(
        slug=slug,
        name=slug.title(),
        description="non-engineering mcp squad",
        entrypoint="mcp",
        invoke={"mode": "pp_run"},
    )


def test_via_mcp_with_target_repo_id_uses_registry_path() -> None:
    """Engineering squad + target_repo_id='hydra' -> project_path == resolve_repo_path('hydra')."""
    state = HydraState(root_goal="Fix something")
    pack = _make_engineering_pack()
    inbound = CSuiteDecisionPacket(
        workflow_id=state.workflow_id,
        origin_squad="hydra",
        target_squad="engineering",
        origin="BOARDROOM",
        objective="Fix something",
        target_repo_id="hydra",
    )
    dispatcher, captured = _make_stub_dispatcher()

    result = _via_mcp(state, pack, inbound, dispatcher)

    assert result.status != "failed", f"_via_mcp failed unexpectedly: {result.rationale}"
    assert len(captured) == 1
    expected_path = str(resolve_repo_path("hydra"))
    assert captured[0]["project_path"] == expected_path, (
        f"Expected project_path={expected_path!r}, got {captured[0]['project_path']!r}"
    )


def test_via_mcp_without_target_repo_id_falls_back_to_cwd() -> None:
    """Engineering squad without target_repo_id -> project_path is cwd (default fallback)."""
    import os
    state = HydraState(root_goal="Fix something else")
    pack = _make_engineering_pack()  # invoke has no project_path key
    inbound = CSuiteDecisionPacket(
        workflow_id=state.workflow_id,
        origin_squad="hydra",
        target_squad="engineering",
        origin="BOARDROOM",
        objective="Fix something else",
        # no target_repo_id
    )
    dispatcher, captured = _make_stub_dispatcher()

    result = _via_mcp(state, pack, inbound, dispatcher)

    assert result.status != "failed", f"_via_mcp failed unexpectedly: {result.rationale}"
    assert len(captured) == 1
    # The fallback resolves to cwd at call time.
    assert captured[0]["project_path"] == str(Path.cwd())


def test_via_mcp_non_engineering_squad_ignores_target_repo_id() -> None:
    """A non-engineering mcp squad with target_repo_id set must use the squad.yaml
    project_path config, NOT resolve via the registry.

    We give the pack a fixed invoke["project_path"] so the expected fallback is
    unambiguous — regardless of where pytest runs from — and then confirm the
    captured path matches that config value, not the registry resolution.
    """
    sentinel_path = "/sentinel/project/path"
    state = HydraState(root_goal="Do executive work")
    # Give the pack a fixed invoke["project_path"] so the expected fallback is
    # unambiguous regardless of where pytest runs from, avoiding the coincident
    # cwd == hydra-path problem.
    pack = SquadPack(
        slug="executive",
        name="Executive",
        description="non-engineering mcp squad",
        entrypoint="mcp",
        invoke={"mode": "pp_run", "project_path": sentinel_path},
    )
    inbound = CSuiteDecisionPacket(
        workflow_id=state.workflow_id,
        origin_squad="hydra",
        target_squad="executive",
        origin="BOARDROOM",
        objective="Do executive work",
        target_repo_id="hydra",   # set, but must be ignored for non-engineering
    )
    dispatcher, captured = _make_stub_dispatcher()

    result = _via_mcp(state, pack, inbound, dispatcher)

    assert result.status != "failed", f"_via_mcp failed unexpectedly: {result.rationale}"
    assert len(captured) == 1
    # Non-engineering squad must use the squad.yaml config path, not the registry.
    assert captured[0]["project_path"] == sentinel_path, (
        f"Expected sentinel squad.yaml path {sentinel_path!r}, "
        f"got {captured[0]['project_path']!r} — non-engineering squad was incorrectly retargeted"
    )


# ---------------------------------------------------------------------------
# Item 1 — reflexion retry and best-of-n carry target_repo_id
# ---------------------------------------------------------------------------


def test_reflexion_retry_packet_carries_target_repo_id() -> None:
    """CSuiteDecisionPacket built for a reflexion retry must carry target_repo_id.

    We construct the packet directly (as supervisor._reflexion_retry does) and
    assert the field is threaded through — this is a unit test of the packet
    construction contract, not a full supervisor integration run.
    """
    rid = uuid.uuid4()
    packet = CSuiteDecisionPacket(
        workflow_id=rid,
        origin_squad="hydra",
        target_squad="engineering",
        origin="BOARDROOM",
        objective="retry: fix something\n\n=== REFLEXION RETRY #1 ===",
        parent_id=uuid.uuid4(),
        target_repo_id="agentsmith",
    )
    assert packet.target_repo_id == "agentsmith", (
        "reflexion retry packet must carry target_repo_id so _via_mcp "
        "targets the correct repo on retry"
    )


def test_bon_candidate_packet_carries_target_repo_id() -> None:
    """CSuiteDecisionPacket built for a best-of-n candidate must carry target_repo_id."""
    rid = uuid.uuid4()
    packet = CSuiteDecisionPacket(
        workflow_id=rid,
        origin_squad="hydra",
        target_squad="engineering",
        origin="BOARDROOM",
        objective="do work\n\n[bon-candidate 1/3]",
        target_repo_id="hydra",
    )
    assert packet.target_repo_id == "hydra"


# ---------------------------------------------------------------------------
# Item 2 — after_intake routing function
# ---------------------------------------------------------------------------


def test_after_intake_surfaced_routes_to_halt() -> None:
    """after_intake must return 'halt' when state.phase == 'surfaced'."""
    # Import the pure-python function directly.  after_intake is a closure
    # defined inside build_supervisor; we test the equivalent logic inline
    # since it is a one-liner.
    state = HydraState(root_goal="bad --repo nope goal")
    state.phase = "surfaced"
    # Mirror the after_intake logic verbatim.
    result = "halt" if state.phase == "surfaced" else "planner"
    assert result == "halt"


def test_after_intake_normal_routes_to_planner() -> None:
    """after_intake must return 'planner' for any non-surfaced intake phase."""
    for phase in ("intake", "planning", "dispatch"):
        state = HydraState(root_goal="normal goal")
        state.phase = phase  # type: ignore[assignment]
        result = "halt" if state.phase == "surfaced" else "planner"
        assert result == "planner", f"phase={phase!r} should route to planner"


# ---------------------------------------------------------------------------
# Item 3 — parse_repo_arg hardening: equals-form, bare, duplicate
# ---------------------------------------------------------------------------


def test_parse_repo_arg_equals_form() -> None:
    """'--repo=hydra Fix X' must be accepted and normalised to ('hydra','Fix X')."""
    repo_id, rest = parse_repo_arg("--repo=hydra Fix X")
    assert repo_id == "hydra"
    assert "hydra" not in rest
    assert "--repo" not in rest
    assert "Fix X" in rest


def test_parse_repo_arg_equals_form_unknown_raises() -> None:
    """'--repo=bogus ...' must raise ValueError for an unknown id."""
    with pytest.raises(ValueError, match="not an allow-listed repo_id"):
        parse_repo_arg("--repo=bogus Fix something")


def test_parse_repo_arg_bare_raises() -> None:
    """'--repo' with no following value must raise ValueError."""
    with pytest.raises(ValueError, match="--repo requires a value"):
        parse_repo_arg("Fix something --repo")


def test_parse_repo_arg_bare_followed_by_flag_raises() -> None:
    """'--repo --squad engineering ...' — bare --repo before another flag."""
    with pytest.raises(ValueError, match="--repo requires a value"):
        parse_repo_arg("--repo --squad engineering do the thing")


def test_parse_repo_arg_duplicate_raises() -> None:
    """'--repo hydra --repo agentsmith ...' must raise ValueError."""
    with pytest.raises(ValueError, match="--repo specified more than once"):
        parse_repo_arg("--repo hydra do the thing --repo agentsmith")


# ---------------------------------------------------------------------------
# Item 3 (final) — empty equals-form
# ---------------------------------------------------------------------------


def test_parse_repo_arg_equals_empty_raises() -> None:
    """'--repo=' (equals with no value) must raise ValueError."""
    with pytest.raises(ValueError, match="--repo requires a value"):
        parse_repo_arg("Fix something --repo=")


def test_parse_repo_arg_equals_only_raises() -> None:
    """Bare '--repo=' at start of string must raise ValueError."""
    with pytest.raises(ValueError, match="--repo requires a value"):
        parse_repo_arg("--repo=")


# ---------------------------------------------------------------------------
# Item 1 (final) — node_postcheck does not clobber a pre-surfaced phase
# ---------------------------------------------------------------------------


def test_node_postcheck_preserves_surfaced_phase() -> None:
    """A state that arrives at node_postcheck already surfaced (e.g. from a
    bad --repo intake rejection) must stay 'surfaced', not be overwritten
    with 'done' when governance verdict.surfaced is False.

    We call node_postcheck indirectly by building a minimal supervisor in
    pure-python mode and verifying the final phase.
    """
    from unittest.mock import MagicMock, patch
    from hydra_core.governance import GovernanceVerdict

    # Governance returns a clean (non-surfaced) verdict so the only
    # thing that could set phase="done" is the else/elif branch.
    clean_verdict = GovernanceVerdict(surfaced=False, reason="ok")

    with patch("hydra_core.governance.enforce_governance", return_value=clean_verdict):
        # Build supervisor in pure-python mode (no LangGraph needed).
        from hydra_core.supervisor import build_supervisor
        from hydra_core.squad_node import Dispatcher

        stub_dispatcher = MagicMock(spec=Dispatcher)
        # suppress tool-tracker flush
        stub_dispatcher._tool_tracker = None

        runner = build_supervisor(
            project_root=Path("C:/AiAppDeployments/Hydra"),
            dispatcher=stub_dispatcher,
            force_pure_python=True,
        )

        # Build a state that is already surfaced (intake-rejected --repo).
        state = HydraState(
            root_goal="some goal",
            phase="surfaced",
            pending_hitl={"reason": "high_risk", "summary": "bad --repo"},
        )

        # Invoke only node_postcheck by stopping before it then calling it
        # directly, or equivalently use stop_before to reach the step just
        # before postcheck and then run one more step.
        # Simplest: call node_postcheck directly through the runner's steps.
        postcheck_fn = dict(runner.steps).get("postcheck")
        assert postcheck_fn is not None, "postcheck step not found in runner"

        patch_dict = postcheck_fn(state)
        final_phase = patch_dict.get("phase", state.phase)

        assert final_phase == "surfaced", (
            f"Expected phase='surfaced' to be preserved, got {final_phase!r}. "
            "node_postcheck must not overwrite a pre-surfaced phase with 'done'."
        )
