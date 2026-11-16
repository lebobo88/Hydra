"""Tests for ``cli._doctor_head_lines`` and its ``doctor`` wiring.

Section A is HERMETIC: fake ``HeadResolution`` records and a fake native-pack
mapping; no sibling checkout and no ``~/.hydra``.

Section B is CHECKOUT-DEPENDENT: it runs the helper against the real squads and
the Senate checkout, and is skipped with a reason when that checkout is absent.
"""
from __future__ import annotations

import argparse
from pathlib import Path

import pytest

from hydra_core import cli, squad_loader
from hydra_core.head_registry import HeadResolution
from hydra_core.native_packs import native_pack_root
from hydra_core.squad_loader import SquadPack

REPO_ROOT = Path(__file__).resolve().parents[1]

# --------------------------------------------------------------------------
# SECTION A: HERMETIC
# --------------------------------------------------------------------------


def _rec(head="h", squad="sq", **kw) -> HeadResolution:
    base = dict(
        squad=squad, head=head, plugin="plug", agent_name=head,
        qualified_name=f"plug:{head}", agent_path=Path("x") / f"{head}.md",
        rule="slug", frontmatter_name=head,
    )
    base.update(kw)
    return HeadResolution(**base)


def _packs(*slugs: str, entrypoint="claude-native") -> dict[str, SquadPack]:
    return {s: SquadPack(slug=s, name=s, description="", entrypoint=entrypoint)
            for s in slugs}


def _run(records, packs=None, native=None):
    packs = packs if packs is not None else _packs("sq")
    native = native if native is not None else {"sq": object()}
    return cli._doctor_head_lines(
        packs, resolve_all=lambda p: records, native_packs=native)


def _kind(lines, prefix):
    return [ln for ln in lines if ln.startswith(prefix)]


def test_doctor_heads_clean_summary_ok():
    lines, fails = _run([_rec()])
    assert fails == 0
    assert lines == ["OK:   head registry  packs=1 heads=1 fail=0 warn=0"]


def test_doctor_heads_unresolved_is_fail():
    lines, fails = _run([_rec(rule="unresolved", agent_path=None,
                              frontmatter_name=None, error="no such head")])
    assert "FAIL: head sq/h: no agent file resolved" in lines
    assert fails == 1
    assert lines[-1].startswith("FAIL: head registry  packs=1 heads=1 fail=1")


def test_doctor_heads_frontmatter_mismatch_is_fail():
    lines, fails = _run([_rec(frontmatter_name="other")])
    assert ("FAIL: head sq/h: frontmatter name 'other' != resolved agent 'h'"
            in lines)
    assert fails == 1


def test_doctor_heads_frontmatter_read_error_warns_without_mismatch():
    rec = _rec(frontmatter_name=None, error="/p/h.md: PermissionError: denied")
    lines, fails = _run([rec])
    head_lines = [ln for ln in lines if "sq/h" in ln]
    assert len(head_lines) == 1
    assert head_lines[0].startswith("WARN: ") and "PermissionError" in head_lines[0]
    assert not _kind(lines, "FAIL")
    assert fails == 0


def test_doctor_heads_stale_agent_file_warns():
    lines, fails = _run([_rec(stale_agent_file="agents/gone.md")])
    assert ("WARN: head sq/h: declared agent_file does not resolve "
            "(agents/gone.md)") in lines
    assert fails == 0
    assert lines[-1] == "OK:   head registry  packs=1 heads=1 fail=0 warn=1"


def test_doctor_heads_error_on_resolved_head_warns():
    lines, fails = _run([_rec(error="tie between a and b")])
    assert "WARN: head sq/h: tie between a and b" in lines
    assert fails == 0


def test_doctor_heads_override_with_stale_and_error_yields_both_warns():
    lines, fails = _run([_rec(rule="override", stale_agent_file="a/b.md",
                              error="tie")])
    warns = _kind(lines, "WARN: head sq/h")
    assert len(warns) == 2
    assert any("does not resolve (a/b.md)" in w for w in warns)
    assert any(w.endswith(": tie") for w in warns)
    assert fails == 0


def test_doctor_heads_checkout_unavailable_one_pack_warn_no_head_fail():
    err = "pack checkout unavailable: unknown repo"
    recs = [_rec(head=h, rule="unresolved", agent_path=None,
                 frontmatter_name=None, error=err) for h in ("a", "b", "c")]
    lines, fails = _run(recs)
    assert _kind(lines, "WARN: pack sq: checkout unavailable") == [
        "WARN: pack sq: checkout unavailable — unknown repo"]
    assert not _kind(lines, "FAIL")
    assert fails == 0
    assert lines[-1] == "OK:   head registry  packs=1 heads=3 fail=0 warn=1"


def test_doctor_heads_native_pack_missing_from_mapping_is_fail():
    lines, fails = _run([], packs=_packs("sq"), native={})
    assert ("FAIL: pack sq: claude-native pack has no NATIVE_PACKS entry"
            in lines)
    assert fails == 1


def test_doctor_heads_non_native_pack_not_checked_against_mapping():
    lines, fails = _run([], packs=_packs("sq", entrypoint="mcp"), native={})
    assert fails == 0
    assert lines == ["OK:   head registry  packs=0 heads=0 fail=0 warn=0"]


def test_doctor_heads_summary_counts_equal_emitted_lines():
    recs = [_rec(head="a", frontmatter_name="zzz"),
            _rec(head="b", stale_agent_file="s"),
            _rec(head="c", rule="unresolved", agent_path=None,
                 frontmatter_name=None)]
    lines, fails = _run(recs)
    body = lines[:-1]
    assert len(_kind(body, "FAIL")) == fails == 2
    assert len(_kind(body, "WARN")) == 1
    assert lines[-1] == "FAIL: head registry  packs=1 heads=3 fail=2 warn=1"


def test_doctor_heads_unexpected_exception_is_visible_fail():
    def boom(_packs):
        raise RuntimeError("kaboom")

    lines, fails = cli._doctor_head_lines(
        _packs("sq"), resolve_all=boom, native_packs={"sq": object()})
    assert lines
    assert any(ln.startswith("FAIL: ") and "RuntimeError" in ln
               and "kaboom" in ln for ln in lines)
    assert fails >= 1


def test_doctor_heads_per_pack_exception_is_visible_fail():
    class Bad:
        squad = "sq"
        head = "h"
        error = None

        @property
        def rule(self):
            raise ValueError("bad record")

    lines, fails = _run([Bad()])
    assert any("ValueError" in ln and "(squad=sq)" in ln for ln in lines)
    assert fails >= 1


def test_doctor_heads_keyboard_interrupt_not_caught():
    def interrupt(_packs):
        raise KeyboardInterrupt

    with pytest.raises(KeyboardInterrupt):
        cli._doctor_head_lines(_packs("sq"), resolve_all=interrupt,
                               native_packs={"sq": object()})


def test_doctor_heads_helper_makes_no_frontmatter_reads(monkeypatch):
    def forbidden(*a, **k):
        raise AssertionError("helper must not re-read agent files")

    monkeypatch.setattr(cli, "_read_agent_frontmatter", forbidden)
    monkeypatch.setattr(cli, "_agent_frontmatter_name", forbidden)
    _lines, fails = _run([_rec()])
    assert fails == 0


def _doctor_args(quick: bool) -> argparse.Namespace:
    return argparse.Namespace(project=str(REPO_ROOT), quick=quick)


def test_doctor_quick_prints_nothing_about_head_registry(
        monkeypatch, tmp_path, capsys):
    monkeypatch.setattr(squad_loader, "USER_SQUAD_DIR", tmp_path)

    def must_not_run(*a, **k):
        raise AssertionError("helper must not run under --quick")

    monkeypatch.setattr(cli, "_doctor_head_lines", must_not_run)
    cli._cmd_doctor(_doctor_args(True))
    assert "head registry" not in capsys.readouterr().out


class _Stop(Exception):
    pass


def test_doctor_full_invokes_helper_exactly_once(monkeypatch, tmp_path):
    monkeypatch.setattr(squad_loader, "USER_SQUAD_DIR", tmp_path)
    calls = []

    def recorder(packs, **kw):
        calls.append(packs)
        raise _Stop

    monkeypatch.setattr(cli, "_doctor_head_lines", recorder)
    with pytest.raises(_Stop):
        cli._cmd_doctor(_doctor_args(False))
    assert len(calls) == 1


def test_doctor_full_prints_helper_summary_once(monkeypatch, tmp_path, capsys):
    monkeypatch.setattr(squad_loader, "USER_SQUAD_DIR", tmp_path)
    monkeypatch.setattr(
        cli, "_doctor_head_lines",
        lambda packs, **kw: (["FAIL: head registry  packs=0 heads=0 fail=3 "
                              "warn=0"], 3))
    # Stop right after the helper block: the next stage imports langgraph.
    import builtins
    real_import = builtins.__import__

    def fake_import(name, *a, **k):
        if name == "langgraph":
            raise _Stop
        return real_import(name, *a, **k)

    monkeypatch.setattr(builtins, "__import__", fake_import)
    with pytest.raises(_Stop):
        cli._cmd_doctor(_doctor_args(False))
    assert capsys.readouterr().out.count("head registry") == 1


# --------------------------------------------------------------------------
# SECTION B: CHECKOUT-DEPENDENT (needs the Senate checkout)
# --------------------------------------------------------------------------


def _legal_checkout_skip_reason() -> str | None:
    try:
        root = native_pack_root("legal-compliance")
    except ValueError as exc:
        return f"legal-compliance (Senate) checkout unavailable: {exc}"
    if not Path(root).is_dir():
        return f"legal-compliance (Senate) checkout directory missing: {root}"
    return None


_LEGAL_SKIP = _legal_checkout_skip_reason()


@pytest.mark.skipif(_LEGAL_SKIP is not None,
                    reason=_LEGAL_SKIP or "checkout available")
def test_doctor_heads_legal_compliance_stale_warns(monkeypatch, tmp_path):
    monkeypatch.setattr(squad_loader, "USER_SQUAD_DIR", tmp_path)
    packs = squad_loader.discover_squads(REPO_ROOT)
    lines, _fails = cli._doctor_head_lines(packs)
    stale = [ln for ln in lines
             if ln.startswith("WARN: head legal-compliance/")
             and "declared agent_file does not resolve" in ln]
    assert len(stale) == 12
    assert not [ln for ln in lines
                if ln.startswith("FAIL: head legal-compliance/")]
