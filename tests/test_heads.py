"""Tests for the head naming overlay (Stage 4 — Three Crowns)."""
from __future__ import annotations

from pathlib import Path

import pytest

from hydra_core.heads import (
    HeadAlias,
    alias_for,
    all_aliases,
    cathedral_name,
    crown_label_for_squad,
    crown_of,
    heads_in_crown,
    load_aliases,
)


REPO_ROOT = Path(__file__).resolve().parents[1]


# --- built-in roster ---------------------------------------------------------

def test_solon_athena_hermes_and_eight_more_are_present_in_executive_crown():
    aliases = load_aliases(REPO_ROOT)
    expected = {
        "ceo": "Solon", "cso": "Athena", "cmo": "Hermes",
        "cto": "Hephaestus", "cfo": "Demeter", "coo": "Hestia",
        "clo": "Themis", "cpo": "Asclepius", "boardroom": "Iris",
    }
    for plaza, mythic in expected.items():
        assert aliases[plaza].mythic == mythic
        assert aliases[plaza].crown == "executive"


def test_forge_crown_has_seven_named_heads():
    plaza_to_mythic = {
        "architect": "Daedalus", "engineer": "Prometheus",
        "qa-reviewer": "Argus", "test-strategist": "Hygeia",
        "security-reviewer": "Cerberus", "devops-sre": "Charon",
        "docs-author": "Mnemosyne",
    }
    forge = {a.plaza: a.mythic for a in heads_in_crown("forge", project_root=REPO_ROOT)}
    for plaza, mythic in plaza_to_mythic.items():
        assert forge.get(plaza) == mythic


def test_garland_crown_has_eight_muses():
    expected = {
        "brand-strategist": "Calliope", "copywriter": "Erato",
        "content-strategist": "Polyhymnia", "social-community": "Terpsichore",
        "paid-acquisition": "Euterpe", "pr-earned": "Clio",
        "seo-discovery": "Urania", "photo-cinema": "Helios",
    }
    garland = {a.plaza: a.mythic for a in heads_in_crown("garland", project_root=REPO_ROOT)}
    for plaza, mythic in expected.items():
        assert garland.get(plaza) == mythic


# --- single-slug API ---------------------------------------------------------

def test_cathedral_name_returns_mythic_for_known_slug():
    assert cathedral_name("ceo", project_root=REPO_ROOT) == "Solon"
    assert cathedral_name("engineer", project_root=REPO_ROOT) == "Prometheus"
    assert cathedral_name("photo-cinema", project_root=REPO_ROOT) == "Helios"


def test_cathedral_name_passthrough_for_unknown_slug():
    assert cathedral_name("not-a-head", project_root=REPO_ROOT) == "not-a-head"


def test_crown_of_for_known_and_unknown():
    assert crown_of("ceo", project_root=REPO_ROOT) == "executive"
    assert crown_of("architect", project_root=REPO_ROOT) == "forge"
    assert crown_of("helios", project_root=REPO_ROOT) == "unaffiliated"
    assert crown_of("photo-cinema", project_root=REPO_ROOT) == "garland"


def test_alias_for_returns_head_alias_object():
    a = alias_for("cerberus", project_root=REPO_ROOT)
    assert a is None  # plaza is the slug, "cerberus" is the mythic name
    a = alias_for("security-reviewer", project_root=REPO_ROOT)
    assert isinstance(a, HeadAlias)
    assert a.mythic == "Cerberus"
    assert "guardian" in a.register.lower()


# --- overlay precedence ------------------------------------------------------

def test_overlay_overrides_builtin(tmp_path):
    # Build a minimal project layout with an overlay that renames ceo → Justinian.
    sq = tmp_path / "squads" / "executive"
    sq.mkdir(parents=True)
    (sq / "heads.yaml").write_text(
        """
heads:
  - plaza: ceo
    mythic: Justinian
    crown: executive
    register: "Codex-writer"
""",
        encoding="utf-8",
    )
    name = cathedral_name("ceo", project_root=tmp_path)
    assert name == "Justinian"


def test_malformed_overlay_raises_value_error(tmp_path):
    sq = tmp_path / "squads" / "executive"
    sq.mkdir(parents=True)
    (sq / "heads.yaml").write_text("heads: [: : :", encoding="utf-8")  # broken YAML
    with pytest.raises(ValueError):
        load_aliases(tmp_path)


def test_overlay_missing_is_not_an_error(tmp_path):
    # No overlay anywhere — built-ins should still load.
    aliases = load_aliases(tmp_path)
    assert "ceo" in aliases and aliases["ceo"].mythic == "Solon"


# --- crown label rendering ---------------------------------------------------

def test_crown_label_for_squad_renders_known_crowns():
    assert crown_label_for_squad("executive") == "the Executive Crown"
    assert crown_label_for_squad("engineering") == "the Forge Crown"
    assert crown_label_for_squad("creative") == "the Garland Crown"
    assert crown_label_for_squad("garland") == "the Garland Crown"


def test_crown_label_for_squad_falls_back_for_unknown():
    assert crown_label_for_squad("legal-compliance") == "Legal Compliance"
    assert crown_label_for_squad("research-ds") == "Research Ds"


# --- iteration API -----------------------------------------------------------

def test_all_aliases_returns_non_empty():
    all_a = list(all_aliases(project_root=REPO_ROOT))
    assert len(all_a) >= 24  # 9 exec + 8 forge + 8 garland (Asclepius double-counts)
    crowns = {a.crown for a in all_a}
    assert "executive" in crowns
    assert "forge" in crowns
    assert "garland" in crowns
