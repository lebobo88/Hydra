"""Tests for ``hydra_core.head_registry``.

Section A is HERMETIC: it builds tmp trees and injects them through
``pack_root`` / ``pack_root_for``; it never calls ``repo_registry``, never reads
``~/.hydra`` and passes with every sibling checkout absent.

Section B is CHECKOUT-DEPENDENT (integration): it resolves the real squad.yaml
heads against the real sibling checkouts.  Each test is skipped with an explicit
reason when its pack checkout is unavailable, and never fails for that reason.
"""
from __future__ import annotations

import errno
from pathlib import Path
from unittest import mock

import pytest

from hydra_core import squad_loader

from hydra_core.head_registry import HeadResolution, resolve_all_heads, resolve_head
from hydra_core.native_packs import native_pack, native_pack_root
from hydra_core.squad_loader import AgentSpec, SquadPack, discover_squads

REPO_ROOT = Path(__file__).resolve().parents[1]


# --------------------------------------------------------------------------
# SECTION A: HERMETIC fixture tests (tmp_path only; no sibling checkout)
# --------------------------------------------------------------------------

# "executive" -> plugin "executive-suite", lead agent "boardroom".
SQUAD = "executive"
PLUGIN = "executive-suite"


def _agent_file(root: Path, rel: str, name: str) -> None:
    p = root / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(f"---\nname: {name}\n---\nbody\n", encoding="utf-8")


def _pack(*heads: tuple[str, str | None]) -> dict[str, SquadPack]:
    agents = tuple(AgentSpec(slug=s, agent_file=f) for s, f in heads)
    return {SQUAD: SquadPack(slug=SQUAD, name=SQUAD, description="", entrypoint="claude-native",
                             agents=agents)}


def _adir(root: Path) -> str:
    return f"plugins/{PLUGIN}/agents"


def test_override_applies_to_lead_head(tmp_path: Path) -> None:
    _agent_file(tmp_path, f"{_adir(tmp_path)}/boardroom.md", "boardroom")
    packs = _pack(("boardroom", None))
    r = resolve_head(SQUAD, "boardroom", packs=packs, pack_root=tmp_path)
    assert r.rule == "override"
    assert r.agent_name == "boardroom"
    assert r.qualified_name == f"{PLUGIN}:boardroom"
    assert r.agent_path == tmp_path / _adir(tmp_path) / "boardroom.md"
    assert r.frontmatter_name == "boardroom"
    assert r.stale_agent_file is None and r.error is None


def test_override_matches_head_by_agent_file_stem(tmp_path: Path) -> None:
    _agent_file(tmp_path, f"{_adir(tmp_path)}/boardroom.md", "boardroom")
    packs = _pack(("chair", f"{_adir(tmp_path)}/boardroom.md"))
    r = resolve_head(SQUAD, "chair", packs=packs, pack_root=tmp_path)
    assert r.rule == "override"
    assert r.agent_name == "boardroom"


def test_override_does_not_apply_to_other_heads(tmp_path: Path) -> None:
    _agent_file(tmp_path, f"{_adir(tmp_path)}/boardroom.md", "boardroom")
    _agent_file(tmp_path, f"{_adir(tmp_path)}/cfo.md", "cfo")
    packs = _pack(("boardroom", None), ("cfo", None))
    r = resolve_head(SQUAD, "cfo", packs=packs, pack_root=tmp_path)
    assert r.rule == "slug"
    assert r.agent_name == "cfo"


def test_agent_file_stem_nested_subdirectory(tmp_path: Path) -> None:
    nested = f"{_adir(tmp_path)}/helios-crew/video-synth.md"
    _agent_file(tmp_path, nested, "video-synth")
    packs = _pack(("synth-head", nested))
    r = resolve_head(SQUAD, "synth-head", packs=packs, pack_root=tmp_path)
    assert r.rule == "agent_file_stem"
    assert r.agent_name == "video-synth"
    assert r.qualified_name == f"{PLUGIN}:video-synth"
    assert r.agent_path == tmp_path / nested
    assert r.frontmatter_name == "video-synth"


def test_slug_rule(tmp_path: Path) -> None:
    _agent_file(tmp_path, f"{_adir(tmp_path)}/cfo.md", "cfo")
    packs = _pack(("cfo", None))
    r = resolve_head(SQUAD, "cfo", packs=packs, pack_root=tmp_path)
    assert r.rule == "slug"
    assert r.stale_agent_file is None


def test_slug_rule_is_not_recursive(tmp_path: Path) -> None:
    _agent_file(tmp_path, f"{_adir(tmp_path)}/sub/cfo.md", "cfo")
    packs = _pack(("cfo", None))
    r = resolve_head(SQUAD, "cfo", packs=packs, pack_root=tmp_path)
    assert r.rule == "unresolved"


def test_unresolved(tmp_path: Path) -> None:
    packs = _pack(("ghost", None))
    r = resolve_head(SQUAD, "ghost", packs=packs, pack_root=tmp_path)
    assert r.rule == "unresolved"
    assert r.agent_path is None
    assert r.frontmatter_name is None
    assert r.error


def test_stale_agent_file_falls_through_to_slug(tmp_path: Path) -> None:
    _agent_file(tmp_path, f"{_adir(tmp_path)}/cfo.md", "cfo")
    packs = _pack(("cfo", ".claude/agents/cfo.md"))
    r = resolve_head(SQUAD, "cfo", packs=packs, pack_root=tmp_path)
    assert r.rule == "slug"
    assert r.stale_agent_file == ".claude/agents/cfo.md"
    assert r.agent_path == tmp_path / _adir(tmp_path) / "cfo.md"


def test_stale_agent_file_and_no_slug_file_is_unresolved(tmp_path: Path) -> None:
    packs = _pack(("cfo", ".claude/agents/cfo.md"))
    r = resolve_head(SQUAD, "cfo", packs=packs, pack_root=tmp_path)
    assert r.rule == "unresolved"
    assert r.stale_agent_file == ".claude/agents/cfo.md"


def test_claude_native_pack_without_native_entry_is_unresolved(tmp_path: Path) -> None:
    packs = {"nopack": SquadPack(slug="nopack", name="n", description="", entrypoint="claude-native",
                                 agents=(AgentSpec(slug="a"),))}
    r = resolve_head("nopack", "a", packs=packs, pack_root=tmp_path)
    assert r.rule == "unresolved"
    assert r.error and "nopack" in r.error


def test_resolve_all_heads_uses_injected_root_and_skips_non_native(tmp_path: Path) -> None:
    _agent_file(tmp_path, f"{_adir(tmp_path)}/boardroom.md", "boardroom")
    _agent_file(tmp_path, f"{_adir(tmp_path)}/cfo.md", "cfo")
    packs = _pack(("boardroom", None), ("cfo", None), ("ghost", None))
    packs["engineering"] = SquadPack(slug="engineering", name="e", description="", entrypoint="mcp",
                                     agents=(AgentSpec(slug="x"),))
    seen: list[str] = []

    def root_for(slug: str) -> Path:
        seen.append(slug)
        return tmp_path

    out = resolve_all_heads(packs, pack_root_for=root_for)
    assert seen == [SQUAD]
    assert {(r.head, r.rule) for r in out} == {
        ("boardroom", "override"), ("cfo", "slug"), ("ghost", "unresolved")}


def test_resolve_all_heads_unresolvable_root_is_reported_not_raised() -> None:
    packs = _pack(("cfo", None))

    def boom(slug: str) -> Path:
        raise RuntimeError("no checkout")

    out = resolve_all_heads(packs, pack_root_for=boom)
    assert [r.rule for r in out] == ["unresolved"]
    assert "no checkout" in (out[0].error or "")


def test_stem_rule_alone_resolves_heads_with_nonmatching_slugs(tmp_path: Path) -> None:
    """Hermetic form of the garland/rlm-gaming property: heads whose slug differs
    from their declared agent_file stem resolve only through the stem rule."""
    _agent_file(tmp_path, f"{_adir(tmp_path)}/alpha.md", "alpha")
    _agent_file(tmp_path, f"{_adir(tmp_path)}/nested/beta.md", "beta")
    packs = _pack(("head-a", f"{_adir(tmp_path)}/alpha.md"),
                  ("head-b", f"{_adir(tmp_path)}/nested/beta.md"))
    out = {r.head: r for r in resolve_all_heads(packs, pack_root_for=lambda _s: tmp_path)}
    assert {r.rule for r in out.values()} == {"agent_file_stem"}
    assert out["head-a"].agent_name == "alpha" and out["head-b"].agent_name == "beta"
    assert all(r.stale_agent_file is None and r.error is None for r in out.values())


def test_stale_declared_path_under_each_resolving_rule(tmp_path: Path) -> None:
    _agent_file(tmp_path, f"{_adir(tmp_path)}/boardroom.md", "boardroom")
    _agent_file(tmp_path, f"{_adir(tmp_path)}/cfo.md", "cfo")
    packs = _pack(("boardroom", "gone/boardroom.md"), ("cfo", "gone/cfo.md"))
    out = {r.head: r for r in resolve_all_heads(packs, pack_root_for=lambda _s: tmp_path)}
    assert (out["boardroom"].rule, out["boardroom"].stale_agent_file) == ("override", "gone/boardroom.md")
    assert (out["cfo"].rule, out["cfo"].stale_agent_file) == ("slug", "gone/cfo.md")


def test_override_resolves_and_declared_agent_file_is_stale(tmp_path: Path) -> None:
    # Override picks the agent; the declared path is still checked independently.
    _agent_file(tmp_path, f"{_adir(tmp_path)}/boardroom.md", "boardroom")
    packs = _pack(("boardroom", ".claude/agents/boardroom.md"))
    r = resolve_head(SQUAD, "boardroom", packs=packs, pack_root=tmp_path)
    assert r.rule == "override"
    assert r.agent_name == "boardroom"
    assert r.stale_agent_file == ".claude/agents/boardroom.md"


def test_override_with_resolvable_declaration_is_not_stale(tmp_path: Path) -> None:
    _agent_file(tmp_path, f"{_adir(tmp_path)}/boardroom.md", "boardroom")
    packs = _pack(("boardroom", f"{_adir(tmp_path)}/boardroom.md"))
    r = resolve_head(SQUAD, "boardroom", packs=packs, pack_root=tmp_path)
    assert r.rule == "override" and r.stale_agent_file is None


def test_override_two_head_collision_slug_beats_agent_file_stem(tmp_path: Path) -> None:
    # "boardroom" matches lead_agent by slug; "chair" matches by agent_file stem.
    _agent_file(tmp_path, f"{_adir(tmp_path)}/boardroom.md", "boardroom")
    packs = _pack(("chair", f"{_adir(tmp_path)}/boardroom.md"), ("boardroom", None))
    by_head = {r.head: r for r in resolve_all_heads(packs, pack_root_for=lambda _s: tmp_path)}
    assert by_head["boardroom"].rule == "override"
    assert by_head["chair"].rule == "agent_file_stem"
    assert by_head["chair"].agent_name == "boardroom"
    assert sum(r.rule == "override" for r in by_head.values()) == 1


def test_override_tie_within_stem_tier_applies_to_none_and_reports(tmp_path: Path) -> None:
    _agent_file(tmp_path, f"{_adir(tmp_path)}/boardroom.md", "boardroom")
    decl = f"{_adir(tmp_path)}/boardroom.md"
    packs = _pack(("a", decl), ("b", decl), ("other", None))
    by_head = {r.head: r for r in resolve_all_heads(packs, pack_root_for=lambda _s: tmp_path)}
    for h in ("a", "b"):
        assert by_head[h].rule == "agent_file_stem"
        assert by_head[h].agent_path is not None
        assert by_head[h].error and "ambiguous" in by_head[h].error
    assert by_head["other"].error and "no agent file" in by_head["other"].error
    assert not any(r.rule == "override" for r in by_head.values())


# --------------------------------------------------------------------------
# Section A2 (hermetic): never-raises contract for filesystem/config failures
# --------------------------------------------------------------------------

def _raising_resolver(exc: BaseException, only: str | None = None):
    real = squad_loader.resolve_agent_file_path

    def _resolve(pack_root: Path, declared: str) -> Path:
        if only is None or declared == only:
            raise exc
        return real(pack_root, declared)

    return _resolve


def test_declared_path_permission_error_keeps_override(tmp_path: Path) -> None:
    _agent_file(tmp_path, f"{_adir(tmp_path)}/boardroom.md", "boardroom")
    packs = _pack(("boardroom", ".claude/agents/boardroom.md"))
    with mock.patch("hydra_core.head_registry.resolve_agent_file_path",
                    side_effect=PermissionError(errno.EACCES, "denied")):
        r = resolve_head(SQUAD, "boardroom", packs=packs, pack_root=tmp_path)
    assert r.rule == "override"
    assert r.stale_agent_file
    assert r.error and "PermissionError" in r.error


def test_declared_path_oserror_keeps_override(tmp_path: Path) -> None:
    _agent_file(tmp_path, f"{_adir(tmp_path)}/boardroom.md", "boardroom")
    packs = _pack(("boardroom", ".claude/agents/boardroom.md"))
    with mock.patch("hydra_core.head_registry.resolve_agent_file_path",
                    side_effect=OSError(errno.EIO, "I/O error")):
        r = resolve_head(SQUAD, "boardroom", packs=packs, pack_root=tmp_path)
    assert r.rule == "override"
    assert r.stale_agent_file
    assert r.error and "OSError" in r.error and "I/O error" in r.error


def test_declared_path_oserror_falls_through_to_slug(tmp_path: Path) -> None:
    _agent_file(tmp_path, f"{_adir(tmp_path)}/cfo.md", "cfo")
    packs = _pack(("cfo", ".claude/agents/other.md"))
    with mock.patch("hydra_core.head_registry.resolve_agent_file_path",
                    side_effect=PermissionError(errno.EACCES, "denied")):
        r = resolve_head(SQUAD, "cfo", packs=packs, pack_root=tmp_path)
    assert r.rule == "slug"
    assert r.stale_agent_file == ".claude/agents/other.md"
    assert r.error and "declared agent_file .claude/agents/other.md" in r.error


def test_declared_path_oserror_and_ambiguity_are_both_reported(tmp_path: Path) -> None:
    _agent_file(tmp_path, f"{_adir(tmp_path)}/boardroom.md", "boardroom")
    decl = f"{_adir(tmp_path)}/boardroom.md"
    packs = _pack(("a", decl), ("b", decl))
    with mock.patch("hydra_core.head_registry.resolve_agent_file_path",
                    side_effect=PermissionError(errno.EACCES, "denied")):
        r = resolve_head(SQUAD, "a", packs=packs, pack_root=tmp_path)
    assert r.rule == "unresolved"
    assert r.error and "ambiguous" in r.error and "PermissionError" in r.error
    assert "; " in r.error


def test_resolve_all_heads_survives_one_head_fs_error(tmp_path: Path) -> None:
    for h in ("one", "two", "three"):
        _agent_file(tmp_path, f"{_adir(tmp_path)}/{h}.md", h)
    packs = _pack(("one", "x/one.md"), ("two", "x/two.md"), ("three", "x/three.md"))
    with mock.patch("hydra_core.head_registry.resolve_agent_file_path",
                    side_effect=_raising_resolver(PermissionError(errno.EACCES, "denied"),
                                                  only="x/two.md")):
        out = resolve_all_heads(packs, pack_root_for=lambda _s: tmp_path)
    assert len(out) == 3
    by_head = {r.head: r for r in out}
    assert "PermissionError" in (by_head["two"].error or "")
    assert by_head["two"].rule == "slug"
    assert by_head["one"].error is None and by_head["three"].error is None


def test_candidate_is_file_permission_error_is_reported_not_raised(tmp_path: Path) -> None:
    packs = _pack(("boardroom", None), ("cfo", None))
    real = Path.is_file

    def flaky(self: Path) -> bool:
        if self.name in ("boardroom.md", "cfo.md"):
            raise PermissionError(errno.EACCES, "denied")
        return real(self)

    with mock.patch.object(Path, "is_file", flaky):
        out = {r.head: r for r in resolve_all_heads(packs, pack_root_for=lambda _s: tmp_path)}
    for head in ("boardroom", "cfo"):  # override candidate, then slug candidate
        assert out[head].rule == "unresolved"
        assert "PermissionError" in (out[head].error or "")


def _write_squad_yaml(repo: Path, text: str) -> None:
    d = repo / "squads" / SQUAD
    d.mkdir(parents=True, exist_ok=True)
    (d / "squad.yaml").write_text(text, encoding="utf-8")


@pytest.mark.parametrize(("label", "text", "exc_name"), [
    ("top_level_list", "- a\n- b\n", "AttributeError"),
    ("top_level_scalar", "just a string\n", "AttributeError"),
    ("agents_not_iterable", "agents: 5\n", "TypeError"),
    ("agent_unknown_key", "agents:\n  - slug: a\n    bogus: 1\n", "TypeError"),
    ("duplicate_slug", "agents:\n  - slug: a\n  - slug: a\n", "ValueError"),
])
def test_resolve_head_real_discovery_malformed_config_is_unresolved(
        tmp_path: Path, label: str, text: str, exc_name: str) -> None:
    _write_squad_yaml(tmp_path, text)
    with mock.patch.object(squad_loader, "USER_SQUAD_DIR", tmp_path / "no-user-squads"),             mock.patch("hydra_core.head_registry._repo_root", return_value=tmp_path):
        r = resolve_head(SQUAD, "boardroom")  # packs=None: real discover_squads
    assert r.rule == "unresolved", label
    assert r.agent_path is None
    assert r.error and f"squad discovery failed: {exc_name}" in r.error, r.error


def test_unreadable_agent_file_keeps_override_and_reports(tmp_path: Path) -> None:
    _agent_file(tmp_path, f"{_adir(tmp_path)}/boardroom.md", "boardroom")
    packs = _pack(("boardroom", None))
    real_read = Path.read_text

    def deny(self: Path, *a: object, **k: object):
        if self.name == "boardroom.md":
            raise PermissionError(errno.EACCES, "denied")
        return real_read(self, *a, **k)

    # Formerly this denied Path.open to exercise a separate pre-read probe; the
    # read now happens once in cli._read_agent_frontmatter, which returns the error.
    with mock.patch.object(Path, "read_text", deny):
        r = resolve_head(SQUAD, "boardroom", packs=packs, pack_root=tmp_path)
    assert r.rule == "override"
    assert r.agent_path == tmp_path / _adir(tmp_path) / "boardroom.md"
    assert r.frontmatter_name is None
    assert r.error and "PermissionError" in r.error


def _deny_read_for(target: Path):
    real_read = Path.read_text

    def deny(self: Path, *a: object, **k: object):
        if self == target:
            raise PermissionError(errno.EACCES, "denied")
        return real_read(self, *a, **k)

    return deny


def test_text_read_failure_keeps_override(tmp_path: Path) -> None:
    """The override file exists, so is_file and any first open succeed; only the
    text read fails.  A failure partway through the read surfaces as the same
    OSError from the same read_text call, so it takes the same code path and
    needs no separate test."""
    target = tmp_path / _adir(tmp_path) / "boardroom.md"
    _agent_file(tmp_path, f"{_adir(tmp_path)}/boardroom.md", "boardroom")
    packs = _pack(("boardroom", None))
    with mock.patch.object(Path, "read_text", _deny_read_for(target)):
        r = resolve_head(SQUAD, "boardroom", packs=packs, pack_root=tmp_path)
    assert r.rule == "override"
    assert r.agent_path == target
    assert r.frontmatter_name is None
    assert r.error and "PermissionError" in r.error and str(target) in r.error


@pytest.mark.parametrize(("declared", "rel", "rule"), [
    (True, "helios-crew/video-synth.md", "agent_file_stem"),
    (False, "cfo.md", "slug"),
])
def test_text_read_failure_keeps_non_override_rules(
        tmp_path: Path, declared: bool, rel: str, rule: str) -> None:
    path_rel = f"{_adir(tmp_path)}/{rel}"
    target = tmp_path / path_rel
    _agent_file(tmp_path, path_rel, Path(rel).stem)
    packs = _pack(("cfo", path_rel if declared else None))
    with mock.patch.object(Path, "read_text", _deny_read_for(target)):
        r = resolve_head(SQUAD, "cfo", packs=packs, pack_root=tmp_path)
    assert r.rule == rule
    assert r.agent_path == target
    assert r.frontmatter_name is None
    assert r.error and "PermissionError" in r.error


def test_text_read_failure_and_ambiguity_are_both_reported(tmp_path: Path) -> None:
    decl = f"{_adir(tmp_path)}/boardroom.md"
    target = tmp_path / decl
    _agent_file(tmp_path, decl, "boardroom")
    packs = _pack(("a", decl), ("b", decl))
    with mock.patch.object(Path, "read_text", _deny_read_for(target)):
        r = resolve_head(SQUAD, "a", packs=packs, pack_root=tmp_path)
    assert r.rule == "agent_file_stem"
    assert r.error and "ambiguous" in r.error and "PermissionError" in r.error
    assert "; " in r.error


def test_agent_file_opened_once(tmp_path: Path) -> None:
    """Counts at ``io.open``: on this interpreter (CPython 3.14) Path.read_text ->
    Path.open -> io.open, and it does NOT go through ``builtins.open``.  Both are
    wrapped; the agent file must be opened exactly once in total."""
    import builtins
    import io
    import os

    target = tmp_path / _adir(tmp_path) / "boardroom.md"
    _agent_file(tmp_path, f"{_adir(tmp_path)}/boardroom.md", "boardroom")
    packs = _pack(("boardroom", None))
    count = {"n": 0}

    def counting(real):
        def wrapper(file, *a, **k):
            try:
                if os.fspath(file) == str(target):
                    count["n"] += 1
            except TypeError:
                pass
            return real(file, *a, **k)
        return wrapper

    with mock.patch.object(io, "open", counting(io.open)), \
            mock.patch.object(builtins, "open", counting(builtins.open)):
        r = resolve_head(SQUAD, "boardroom", packs=packs, pack_root=tmp_path)
    assert r.rule == "override" and r.frontmatter_name == "boardroom" and r.error is None
    assert count["n"] == 1


def _inf_squad_yaml() -> str:
    return ("slug: executive\nname: executive\nentrypoint: claude-native\n"
            "best_of_n: .inf\nagents:\n  - slug: boardroom\n")


def test_discovery_inf_best_of_n_is_unresolved(tmp_path: Path) -> None:
    _write_squad_yaml(tmp_path, _inf_squad_yaml())
    with mock.patch.object(squad_loader, "USER_SQUAD_DIR", tmp_path / "no-user-squads"), \
            mock.patch("hydra_core.head_registry._repo_root", return_value=tmp_path):
        r = resolve_head(SQUAD, "boardroom")  # packs=None: real discover_squads
    assert r.rule == "unresolved"
    assert r.error and "squad discovery failed:" in r.error and "OverflowError" in r.error, r.error


def test_discovery_custom_exception_is_unresolved() -> None:
    class _Boom(Exception):
        pass

    with mock.patch("hydra_core.head_registry.discover_squads", side_effect=_Boom("boom")):
        r = resolve_head(SQUAD, "boardroom")
    assert r.rule == "unresolved"
    assert r.error and "_Boom" in r.error


def test_discovery_keyboard_interrupt_propagates() -> None:
    with mock.patch("hydra_core.head_registry.discover_squads", side_effect=KeyboardInterrupt):
        with pytest.raises(KeyboardInterrupt):
            resolve_head(SQUAD, "boardroom")


def test_resolve_head_discovery_value_error_is_unresolved() -> None:
    with mock.patch("hydra_core.head_registry.discover_squads",
                    side_effect=ValueError("bad squad.yaml")):
        r = resolve_head(SQUAD, "boardroom")
    assert r.rule == "unresolved"
    assert r.agent_path is None and r.stale_agent_file is None
    assert r.error and "squad discovery failed" in r.error and "bad squad.yaml" in r.error


def test_resolve_head_discovery_oserror_is_unresolved() -> None:
    with mock.patch("hydra_core.head_registry.discover_squads",
                    side_effect=PermissionError(errno.EACCES, "denied")):
        r = resolve_head(SQUAD, "boardroom")
    assert r.rule == "unresolved"
    assert r.error and "squad discovery failed: PermissionError" in r.error


def test_native_pack_root_value_error_is_checkout_unavailable() -> None:
    packs = _pack(("boardroom", None))
    with mock.patch("hydra_core.head_registry.native_pack_root",
                    side_effect=ValueError("not a git repo")):
        r = resolve_head(SQUAD, "boardroom", packs=packs)
    assert r.rule == "unresolved"
    assert r.error and r.error.startswith("pack checkout unavailable") and "not a git repo" in r.error


# --------------------------------------------------------------------------
# SECTION B: CHECKOUT-DEPENDENT integration tests (skipped when a checkout is absent)
# --------------------------------------------------------------------------

def _discover_repo_squads() -> dict[str, SquadPack]:
    """Discover this repo's squads/ only.

    ``discover_squads`` has no parameter to skip ``squad_loader.USER_SQUAD_DIR``
    (``Path.home()/.hydra/squads``), so the constant is patched to a directory
    that does not exist for the duration of the call.  Operator configuration is
    therefore never read or parsed, at collection time or later.
    """
    with mock.patch.object(squad_loader, "USER_SQUAD_DIR", REPO_ROOT / ".tmp-pytest" / "no-user-squads"):
        return discover_squads(REPO_ROOT)


def _discovered_native_packs() -> dict[str, SquadPack]:
    return {slug: pack for slug, pack in _discover_repo_squads().items()
            if pack.entrypoint == "claude-native"}


PACKS = _discovered_native_packs()
CASES = [(slug, a.slug) for slug, pack in PACKS.items() for a in pack.agents]
_ROOT_CACHE: dict[str, Path] = {}


def _checkout_or_skip(squad: str) -> Path:
    """Return the squad's checkout root, or pytest.skip naming squad and repo_id."""
    if squad not in _ROOT_CACHE:
        repo_id = native_pack(squad).repo_id
        try:
            root = native_pack_root(squad)
        except ValueError as exc:
            # repo_registry.resolve_repo_path raises ValueError for every "checkout
            # unavailable" condition (unregistered id, not a git repo root, git
            # missing or timed out).  Anything else is a bug and must fail.
            pytest.skip(f"checkout for squad {squad!r} (repo_id={repo_id!r}) not resolvable: {exc}")
        if not Path(root).is_dir():
            pytest.skip(f"checkout for squad {squad!r} (repo_id={repo_id!r}) missing dir: {root}")
        _ROOT_CACHE[squad] = Path(root)
    return _ROOT_CACHE[squad]


def _resolve_real(squad: str, head: str) -> HeadResolution:
    return resolve_head(squad, head, packs=PACKS, pack_root=_checkout_or_skip(squad))


@pytest.mark.parametrize(("squad", "head"), CASES, ids=[f"{s}/{h}" for s, h in CASES])
def test_every_discovered_head_resolves(squad: str, head: str) -> None:
    r = _resolve_real(squad, head)
    detail = (f"squad={squad} head={head} rule={r.rule} stale_agent_file={r.stale_agent_file} "
              f"error={r.error}")
    assert r.agent_path is not None, detail
    assert r.agent_path.is_file(), detail
    assert r.frontmatter_name == r.agent_name, f"{detail} frontmatter_name={r.frontmatter_name!r} agent_name={r.agent_name!r}"


def test_garland_and_rlm_gaming_are_discovered_and_checkouts_resolve() -> None:
    """Integration guard: the packs are declared in this repo, and (when their
    checkouts exist) the checkout dirs resolve.  Skips, never fails, without them."""
    for squad in ("garland", "rlm-gaming"):
        assert squad in PACKS, f"{squad} not discovered among claude-native packs"
        assert _checkout_or_skip(squad).is_dir()


def test_garland_and_rlm_gaming_rule_table(capsys: pytest.CaptureFixture[str]) -> None:
    lines = []
    for squad, lead_head in (("garland", "brand-strategist"), ("rlm-gaming", "game-director")):
        rules = {a.slug: _resolve_real(squad, a.slug).rule for a in PACKS[squad].agents}
        lines += [f"{squad}/{h}: {rule}" for h, rule in rules.items()]
        assert rules[lead_head] == "override", f"{squad}/{lead_head}: {rules[lead_head]}"
        others = {h: r for h, r in rules.items() if h != lead_head}
        assert set(others.values()) == {"agent_file_stem"}, others
    with capsys.disabled():
        print("\n" + "\n".join(lines))


def test_legal_compliance_every_head_has_stale_agent_file() -> None:
    """lebobo88/Hydra#126: squad.yaml declares .claude/agents/*.md paths the Senate
    checkout does not have.  The stale declaration is reported for every head,
    including the override-resolved lead (general-counsel)."""
    heads = [a.slug for a in PACKS["legal-compliance"].agents]
    assert len(heads) == 12
    results = {h: _resolve_real("legal-compliance", h) for h in heads}
    missing = {h: r.stale_agent_file for h, r in results.items() if not r.stale_agent_file}
    assert not missing, f"heads without stale_agent_file: {sorted(missing)}"
    lead = results["general-counsel"]
    assert lead.rule == "override"
    assert lead.stale_agent_file == ".claude/agents/general-counsel.md"


def test_collection_never_reads_user_squad_dir(tmp_path: Path) -> None:
    """A malformed squad.yaml in the user squad dir cannot break discovery here."""
    bad = tmp_path / "squads" / "broken"
    bad.mkdir(parents=True)
    (bad / "squad.yaml").write_text("name: [unclosed\n  - : :\n", encoding="utf-8")
    with mock.patch.object(squad_loader, "USER_SQUAD_DIR", tmp_path / "squads"):
        with pytest.raises(ValueError, match="Malformed squad.yaml"):
            discover_squads(tmp_path / "nonexistent-project")  # the hazard is real
        packs = _discover_repo_squads()  # hostile user dir in place: still fine
    assert "broken" not in packs
    assert {"garland", "rlm-gaming", "executive"} <= set(packs)
