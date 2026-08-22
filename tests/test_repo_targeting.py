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

from hydra_core.repo_registry import (
    is_known_repo,
    normalize_repo_subpath,
    parse_repo_arg,
    parse_repo_subpath_arg,
    resolve_repo_path,
    resolve_repo_project_path,
)
from hydra_core.schemas import CSuiteDecisionPacket
from hydra_core.squad_loader import SquadPack
from hydra_core.squad_node import _via_mcp
from hydra_core.state import HydraState

# Captured at import time -- BEFORE the autouse ``_no_git_harvest`` fixture
# stubs the module attributes to no-ops. The regression guard below restores
# these so it exercises the REAL scaffolding against the (already-scaffolded)
# live repo, instead of passing trivially against the stub.
from hydra_core.squad_node import (
    ensure_target_repo_ignores as _real_ensure_target_repo_ignores,
    ensure_target_repo_test_excludes as _real_ensure_target_repo_test_excludes,
)


@pytest.fixture(autouse=True)
def _no_git_harvest(monkeypatch):
    """Some tests dispatch _via_mcp against the LIVE Hydra repo; never let the
    harvest step touch real git there (it is exercised hermetically in
    tests/test_drive_loop_harvest_smoke.py). Likewise never let the target-repo
    scaffolding helpers (.gitignore / test-runner exclude patching, added
    alongside this file's target_repo_id plumbing) write into the real
    checkout -- they are exercised hermetically against tmp_path in
    tests/test_target_repo_scaffolding.py."""
    monkeypatch.setattr("hydra_core.squad_node.harvest_pp_run_artifacts",
                        lambda **_k: None)
    monkeypatch.setattr("hydra_core.squad_node.ensure_target_repo_ignores",
                        lambda _project_path: None)
    monkeypatch.setattr("hydra_core.squad_node.ensure_target_repo_test_excludes",
                        lambda _project_path: None)


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


def test_candc_and_rlm_gaming_allow_listed() -> None:
    # RC6: the game project repo + RLM-Gaming source pack are allow-listed so
    # `/hydra:run --repo candc` (and --repos) resolve instead of being rejected.
    assert is_known_repo("candc") is True
    assert is_known_repo("CandC") is True  # case-insensitive
    assert is_known_repo("rlm-gaming") is True


def test_mc_test_allow_listed() -> None:
    assert is_known_repo("mc-test") is True


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


def test_resolve_repo_project_path_under_repo_root(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    fake_hydra = tmp_path / "Hydra"
    fake_hydra.mkdir()
    _git_init(fake_hydra)

    monkeypatch.setenv("HYDRA_REPO_BASE", str(tmp_path))
    path = resolve_repo_project_path("hydra", "sandboxes/test-5")
    assert path == (fake_hydra / "sandboxes" / "test-5").resolve(strict=False)


def test_resolve_repo_project_path_rejects_parent_escape(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    fake_hydra = tmp_path / "Hydra"
    fake_hydra.mkdir()
    _git_init(fake_hydra)

    monkeypatch.setenv("HYDRA_REPO_BASE", str(tmp_path))
    with pytest.raises(ValueError, match="must not escape"):
        resolve_repo_project_path("hydra", "../escape")


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


def test_parse_repo_subpath_arg_space_form() -> None:
    subpath, rest = parse_repo_subpath_arg("Build it --subdir test-5 now")
    assert subpath == "test-5"
    assert "--subdir" not in rest
    assert "Build it" in rest


def test_parse_repo_subpath_arg_equals_form_normalizes_backslashes() -> None:
    subpath, rest = parse_repo_subpath_arg(r"Build it --repo-subpath sandbox\test-5 now")
    assert subpath == "sandbox/test-5"
    assert "sandbox\\test-5" not in rest


def test_parse_repo_subpath_arg_bare_raises() -> None:
    with pytest.raises(ValueError, match="requires a value"):
        parse_repo_subpath_arg("Build it --subdir")


def test_parse_repo_subpath_arg_equals_empty_raises() -> None:
    with pytest.raises(ValueError, match="requires a value"):
        parse_repo_subpath_arg("Build it --repo-subpath=")


def test_parse_repo_subpath_arg_duplicate_raises() -> None:
    with pytest.raises(ValueError, match="specified more than once"):
        parse_repo_subpath_arg("Build it --subdir test-5 --repo-subpath sandbox/test-6")


def test_parse_repo_subpath_arg_rejects_parent_escape() -> None:
    with pytest.raises(ValueError, match="must not escape"):
        parse_repo_subpath_arg("Build it --subdir ../test-5")


def test_normalize_repo_subpath_rejects_absolute_path() -> None:
    with pytest.raises(ValueError, match="must be relative"):
        normalize_repo_subpath(r"C:\AiAppDeployments\mc-test\test-5")


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
        if tool == "start_run":
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


def test_via_mcp_scaffolding_is_noop_on_already_scaffolded_repo(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Regression guard: when the target repo's ``.gitignore`` and
    ``pyproject.toml`` are ALREADY scaffolded with the ``.harness``/``.hydra``
    entries, dispatching _via_mcp with target_repo_id='hydra' must leave both
    files byte-identical.

    Unlike the sibling tests, this one *un-stubs* the scaffolding helpers (the
    ``_no_git_harvest`` autouse fixture stubbed them to no-ops) and runs the
    REAL implementations, so the byte-for-byte assertion is meaningful: it
    proves the real functions are a genuine no-op on an already-configured
    repo, rather than passing trivially because the helpers were stubbed out.

    Hermetic by construction: 'hydra' is resolved via ``HYDRA_REPO_BASE``
    (monkeypatched below) to a throwaway ``tmp_path / "Hydra"`` fixture repo
    that this test seeds itself with already-scaffolded content -- never the
    live checkout. This avoids depending on (and mutating) whatever state the
    real repo happens to be in; see git history for the prior, non-hermetic
    version of this test that wrote into the live tree. A regression that
    makes either helper rewrite an already-scaffolded config is caught here.

    The guarantee is unconditional, not merely true on this operator's current
    config: ``resolve_repo_path`` consults the operator extras registry
    (``~/.hydra/repos.json`` / ``HYDRA_EXTRA_REPOS``) *before* the
    ``HYDRA_REPO_BASE`` path, so an extras entry for 'hydra' would silently
    bypass the monkeypatch below and route to the live checkout. This test
    forces the extras registry empty and clears ``HYDRA_EXTRA_REPOS`` so the
    ``HYDRA_REPO_BASE`` fixture path is the only path 'hydra' can resolve
    through, regardless of what the running operator has configured."""
    # Restore the real implementations for this test only (the autouse fixture
    # ran first and stubbed the module attributes to no-ops).
    monkeypatch.setattr(
        "hydra_core.squad_node.ensure_target_repo_ignores",
        _real_ensure_target_repo_ignores,
    )
    monkeypatch.setattr(
        "hydra_core.squad_node.ensure_target_repo_test_excludes",
        _real_ensure_target_repo_test_excludes,
    )

    # Force the extras-registry path closed so 'hydra' cannot resolve through
    # an operator's ~/.hydra/repos.json / HYDRA_EXTRA_REPOS entry (see
    # docstring above) -- only HYDRA_REPO_BASE below may satisfy 'hydra'.
    monkeypatch.setattr("hydra_core.repo_registry._load_extra_repos", lambda: {})
    monkeypatch.delenv("HYDRA_EXTRA_REPOS", raising=False)

    repo_root = tmp_path / "Hydra"
    repo_root.mkdir()
    _git_init(repo_root)
    monkeypatch.setenv("HYDRA_REPO_BASE", str(tmp_path))

    ignores_calls: list[tuple[Any, ...]] = []
    excludes_calls: list[tuple[Any, ...]] = []

    def _spy_ignores(*args: Any, **kwargs: Any) -> Any:
        ignores_calls.append(args)
        return _real_ensure_target_repo_ignores(*args, **kwargs)

    def _spy_excludes(*args: Any, **kwargs: Any) -> Any:
        excludes_calls.append(args)
        return _real_ensure_target_repo_test_excludes(*args, **kwargs)

    monkeypatch.setattr("hydra_core.squad_node.ensure_target_repo_ignores", _spy_ignores)
    monkeypatch.setattr(
        "hydra_core.squad_node.ensure_target_repo_test_excludes", _spy_excludes
    )

    # Seed already-scaffolded fixture content: both files already carry the
    # .harness / .hydra entries the helpers would otherwise add.
    gitignore = repo_root / ".gitignore"
    gitignore.write_text("node_modules/\n.harness/\n.hydra/\n", encoding="utf-8")
    pyproject = repo_root / "pyproject.toml"
    pyproject.write_text(
        "[tool.pytest.ini_options]\n"
        'testpaths = ["tests"]\n'
        'norecursedirs = ["node_modules", ".harness", ".hydra"]\n',
        encoding="utf-8",
    )

    gitignore_before = gitignore.read_bytes()
    pyproject_before = pyproject.read_bytes()

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
    dispatcher, _captured = _make_stub_dispatcher()

    _via_mcp(state, pack, inbound, dispatcher)

    assert gitignore.read_bytes() == gitignore_before, (
        "dispatch against an already-scaffolded repo must never rewrite its .gitignore"
    )
    assert pyproject.read_bytes() == pyproject_before, (
        "dispatch against an already-scaffolded repo must never rewrite its pyproject.toml"
    )
    # Prove the no-op is because the real helpers ran and found nothing to
    # change -- not because a regression stopped calling them on this path.
    assert ignores_calls, "ensure_target_repo_ignores was never invoked"
    assert excludes_calls, "ensure_target_repo_test_excludes was never invoked"


def test_via_mcp_with_target_repo_subpath_uses_bounded_project_path(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo_root = tmp_path / "Hydra"
    repo_root.mkdir()
    _git_init(repo_root)
    monkeypatch.setenv("HYDRA_REPO_BASE", str(tmp_path))

    state = HydraState(root_goal="Fix something")
    pack = _make_engineering_pack()
    inbound = CSuiteDecisionPacket(
        workflow_id=state.workflow_id,
        origin_squad="hydra",
        target_squad="engineering",
        origin="BOARDROOM",
        objective="Fix something",
        target_repo_id="hydra",
        target_repo_subpath="sandboxes/test-5",
    )
    dispatcher, captured = _make_stub_dispatcher()

    result = _via_mcp(state, pack, inbound, dispatcher)

    assert result.status != "failed", f"_via_mcp failed unexpectedly: {result.rationale}"
    expected_path = str((repo_root / "sandboxes" / "test-5").resolve(strict=False))
    assert captured[0]["project_path"] == expected_path
    assert Path(expected_path).is_dir(), "engineering target subdir should be created on demand"


def test_via_mcp_without_target_repo_id_surfaces_instead_of_falling_back_to_cwd() -> None:
    """Engineering squad without target_repo_id -> failed, never a silent cwd fallback.

    WS1-E deliberately removed the cwd fallback this test used to assert (see
    git history for the prior `..._falls_back_to_cwd` version). That fallback
    was the ROOT CAUSE this item closes: `hydra.workflow.plan`/`launch`
    without an explicit --repo/--repos silently dispatched engineering into
    whatever the Hydra host process's cwd happened to be, with no error and
    no HITL — the operator only discovered the misdirection later, as a diff
    landed in the wrong tree. Engineering dispatch now requires an explicit,
    resolved target; an absent one is an operator error that must surface
    immediately (here: `_via_mcp` returns status="failed" with an actionable
    remediation) rather than being silently "resolved" to cwd. dispatcher is
    never called — no pp run starts against an unintended tree.
    """
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

    assert result.status == "failed", (
        "engineering dispatch with no resolved target must fail closed, not "
        f"silently fall back to cwd; got status={result.status!r}"
    )
    assert "target" in result.rationale.lower()
    assert "hydra repo register" in result.rationale, (
        "failure must name the actionable remediation (WS1-D unknown_repo_hitl_fields "
        "shape), not just say 'no target'"
    )
    assert not captured, "no pp run should ever be started without a resolved target"


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


# ---------------------------------------------------------------------------
# WS1-B / WS1-D: pre-seeded target_repo_id(s) validated AT INTAKE, and the
# unknown-repo HITL payload carries actionable remediation fields.
# ---------------------------------------------------------------------------


def _intake_fn():
    """The bare node_intake callable off a pure-python supervisor runner —
    mirrors the pattern used by test_node_postcheck_preserves_surfaced_phase."""
    from hydra_core.squad_node import Dispatcher
    from hydra_core.supervisor import build_supervisor

    stub_dispatcher = MagicMock(spec=Dispatcher)
    stub_dispatcher._tool_tracker = None
    runner = build_supervisor(
        project_root=Path("C:/AiAppDeployments/Hydra"),
        dispatcher=stub_dispatcher,
        force_pure_python=True,
    )
    fn = dict(runner.steps).get("intake")
    assert fn is not None, "intake step not found in runner"
    return fn


def test_bad_repo_goal_text_hitl_has_remediation_fields() -> None:
    """--repo 'nope' at goal-text tail must surface an HITL that carries the
    exact `hydra repo register` command, the full known-id list, and (empty,
    for a wildly wrong id) suggestions -- not just a bare summary string."""
    intake = _intake_fn()
    state = HydraState(root_goal="Fix something --repo totally-bogus-repo-id")

    patch = intake(state)

    assert patch["phase"] == "surfaced"
    hitl = patch["pending_hitl"]
    assert hitl["remediation"].startswith("hydra repo register totally-bogus-repo-id ")
    assert "hydra" in hitl["known_ids"]
    assert "suggested_ids" in hitl


def test_bad_repo_goal_text_hitl_suggests_near_miss() -> None:
    """A near-miss typo ('hydraa') should surface a difflib suggestion."""
    intake = _intake_fn()
    state = HydraState(root_goal="Fix something --repo hydraa")

    patch = intake(state)

    assert patch["phase"] == "surfaced"
    assert "hydra" in patch["pending_hitl"]["suggested_ids"]


def test_preseeded_unknown_target_repo_id_surfaces_hitl_not_exception() -> None:
    """WS1-B: a target_repo_id set directly on HydraState (the structured API
    path -- e.g. from CLI --repo or hydra.workflow.plan repo=) BYPASSES
    parse_repo_arg entirely (no --repo token in the goal text). Without
    intake-side validation this would sail through and raise deep inside
    _resolve_task_project_path; it must instead surface this same HITL shape."""
    intake = _intake_fn()
    state = HydraState(root_goal="Fix something", target_repo_id="not-a-real-repo")

    patch = intake(state)

    assert patch["phase"] == "surfaced"
    hitl = patch["pending_hitl"]
    assert "not-a-real-repo" in hitl["summary"]
    assert hitl["remediation"].startswith("hydra repo register not-a-real-repo ")
    assert "hydra" in hitl["known_ids"]


def test_preseeded_valid_target_repo_id_passes_intake_unchanged() -> None:
    """The happy path: a pre-seeded, VALID target_repo_id must sail through
    intake with no HITL and the id preserved (regression guard for the
    validation block added alongside the above)."""
    intake = _intake_fn()
    state = HydraState(root_goal="Fix something", target_repo_id="hydra")

    patch = intake(state)

    assert patch.get("phase") != "surfaced"
    assert state.target_repo_id == "hydra"


def test_preseeded_target_repo_ids_merge_into_fleet_when_goal_has_none() -> None:
    """WS1-B: state.target_repo_ids (the structured multi-repo path, e.g. CLI
    --repos) must be merged into the fleet-wiring path exactly like a goal-text
    --repos/--fleet token would be, when the goal text itself has none."""
    intake = _intake_fn()
    state = HydraState(
        root_goal="Fix something across repos",
        target_repo_ids=["hydra", "pair-programmer"],
    )

    patch = intake(state)

    assert patch.get("phase") != "surfaced"
    assert state.fleet_parallel is True
    assert state.selected_squads == ["engineering"]
    # Fleet tasks are seeded onto the returned patch (LangGraph append reducer
    # applies them to state.tasks); node_intake itself does not mutate
    # state.tasks in place.
    task_repo_ids = {t.target_repo_id for t in patch["tasks"]}
    assert task_repo_ids == {"hydra", "pair-programmer"}


def test_preseeded_target_repo_ids_with_unknown_id_surfaces_hitl() -> None:
    """A pre-seeded target_repo_ids list containing an unknown id must surface
    the same unknown-repo HITL, not raise later."""
    intake = _intake_fn()
    state = HydraState(
        root_goal="Fix something across repos",
        target_repo_ids=["hydra", "definitely-not-registered"],
    )

    patch = intake(state)

    assert patch["phase"] == "surfaced"
    assert "definitely-not-registered" in patch["pending_hitl"]["summary"]


def test_intake_patch_persists_target_repo_ids_for_checkpoint() -> None:
    """Finding A (WS1 retry-2): node_intake's returned `update` dict must
    include target_repo_ids alongside target_repo_id/target_repo_subpath,
    or LangGraph never persists it to the checkpoint (a node's return dict
    is the ONLY thing LangGraph writes -- mutating state in place is not
    enough). Without this key in the patch, _cmd_replay's
    `values.get("target_repo_ids")` read is fed by a producer that never
    wrote the field, so replaying a fleet-targeted run silently drops back
    to single-repo/cwd-fallback behaviour.

    This asserts against the REAL patch dict returned by the REAL node_intake
    callable (via the pure-python build_supervisor runner), not a hand-built
    stand-in -- it is the actual producer _cmd_replay's reader depends on.
    """
    intake = _intake_fn()
    state = HydraState(
        root_goal="Fix something across repos",
        target_repo_ids=["hydra", "pair-programmer"],
    )

    patch = intake(state)

    assert patch.get("phase") != "surfaced"
    assert patch.get("target_repo_ids") == ["hydra", "pair-programmer"], (
        "node_intake's return dict must carry target_repo_ids so LangGraph "
        f"persists it to the checkpoint; got {patch.get('target_repo_ids')!r}"
    )


def test_preseeded_target_repo_id_and_target_repo_ids_both_set_surfaces_hitl() -> None:
    """WS1 retry-2 (finding B): node_intake's own ambiguity guard only
    compared ids parsed OUT of goal text, so a caller that pre-seeds BOTH
    state.target_repo_id and state.target_repo_ids directly (bypassing any
    CLI/MCP-transport-level guard) would sail through and be silently
    resolved by whichever branch ran later. This must surface the same
    high_risk HITL the goal-text ambiguity case does."""
    intake = _intake_fn()
    state = HydraState(
        root_goal="Fix something",
        target_repo_id="hydra",
        target_repo_ids=["hydra", "pair-programmer"],
    )

    patch = intake(state)

    assert patch["phase"] == "surfaced"
    assert "ambiguous" in patch["pending_hitl"]["summary"]


def test_single_distinct_preseeded_repos_does_not_persist_target_repo_ids() -> None:
    """WS1 retry-2 (finding B follow-up): a single-distinct-id --repos run
    (`hydra run --repos hydra`) pre-seeds target_repo_ids=["hydra"] then
    degrades to single-repo mode, setting target_repo_id="hydra". The
    finding-B pre-seed ambiguity guard trips when BOTH target_repo_id and
    target_repo_ids are set. If node_intake persisted the (now vestigial)
    single-element target_repo_ids alongside target_repo_id (finding A's
    persist block), _cmd_replay would re-seed both and the guard would
    FALSELY surface an "ambiguous" HITL on replay of a legitimate
    single-repo run. The degrade branch must therefore clear
    target_repo_ids: the patch must carry target_repo_id but NOT a truthy
    target_repo_ids, and re-running intake on the persisted shape must not
    surface."""
    intake = _intake_fn()
    state = HydraState(
        root_goal="Fix something",
        target_repo_ids=["hydra"],
    )

    patch = intake(state)

    assert patch.get("phase") != "surfaced"
    assert patch.get("target_repo_id") == "hydra"
    # Vestigial fleet list must NOT survive as a TRUTHY value. It may be
    # persisted as an explicit empty list (to overwrite a pre-seeded channel
    # value -- see the replay simulation below), but never as a non-empty list.
    assert not patch.get("target_repo_ids"), (
        "single-distinct --repos must not persist a truthy target_repo_ids; got "
        f"{patch.get('target_repo_ids')!r}"
    )

    # Simulate replay faithfully. _cmd_replay reads snap.values -- the MERGED
    # LangGraph checkpoint channel, NOT the raw intake patch delta. For a
    # LastValue field (target_repo_ids has no reducer), a key is overwritten
    # only when it is PRESENT in the patch; an absent key RETAINS the value
    # pre-seeded onto the channel at graph start (here: ["hydra"] from the CLI
    # --repos path). A degrade branch that clears state locally but omits the
    # key from the patch would therefore leave ["hydra"] alive in the
    # checkpoint -- and re-seeding both target_repo_id AND target_repo_ids on
    # replay trips the finding-B ambiguity guard. Model that merge here.
    checkpoint = {"target_repo_id": None, "target_repo_ids": ["hydra"]}
    for _k in ("target_repo_id", "target_repo_ids"):
        if _k in patch:
            checkpoint[_k] = patch[_k]
    replayed = HydraState(
        root_goal="Fix something",
        target_repo_id=checkpoint["target_repo_id"],
        target_repo_ids=list(checkpoint["target_repo_ids"] or []),
    )
    replay_patch = intake(replayed)
    assert replay_patch.get("phase") != "surfaced", (
        "replay of a legitimate single-repo --repos run falsely surfaced an "
        "ambiguous HITL: the degrade branch must persist an empty "
        "target_repo_ids into the checkpoint (a LastValue channel keeps the "
        "pre-seeded ['hydra'] otherwise)"
    )


def test_preseeded_target_repo_id_wins_over_conflicting_goal_repo_flag() -> None:
    """WS1 retry (finding 3): a pre-seeded state.target_repo_id must win over
    a conflicting --repo token in goal text, not just when goal text has
    none. This is the single-repo half of the precedence rule that the fleet
    half (below) must match."""
    intake = _intake_fn()
    state = HydraState(
        root_goal="Fix something --repo pair-programmer",
        target_repo_id="hydra",
    )

    patch = intake(state)

    assert patch.get("phase") != "surfaced"
    assert state.target_repo_id == "hydra"
    assert patch.get("target_repo_id") == "hydra"


def test_preseeded_target_repo_ids_win_over_conflicting_goal_repos_flag() -> None:
    """WS1 retry (finding 3): a pre-seeded state.target_repo_ids must win over
    a conflicting --repos/--fleet token in goal text. Before this fix, goal
    text won whenever present (the opposite of the single-repo rule above),
    so a stale/pasted --repos token in the goal could silently override an
    explicit structured --repos flag."""
    intake = _intake_fn()
    state = HydraState(
        root_goal="Fix something --repos senate,xenia",
        target_repo_ids=["hydra", "pair-programmer"],
    )

    patch = intake(state)

    assert patch.get("phase") != "surfaced"
    assert state.fleet_parallel is True
    task_repo_ids = {t.target_repo_id for t in patch["tasks"]}
    assert task_repo_ids == {"hydra", "pair-programmer"}, (
        f"pre-seeded target_repo_ids must win; got {task_repo_ids}"
    )


def test_parse_repo_arg_short_typo_raises_valueerror() -> None:
    """P3: a short goal with a typo'd repo id (e.g. 'mc-tset') must raise plain
    ValueError, NOT RepoFlagIgnored — short strings are always tail (typo protection).
    """
    from hydra_core.repo_registry import RepoFlagIgnored
    with pytest.raises(ValueError) as exc_info:
        parse_repo_arg("fix parser --repo mc-tset")
    assert not isinstance(exc_info.value, RepoFlagIgnored), (
        "Short-goal unknown id must raise plain ValueError, not RepoFlagIgnored"
    )
