"""Tests for the Society-of-Mind deliberation cycle (Stage 4)."""
from __future__ import annotations

from pathlib import Path

import pytest

from hydra_core.deliberation import (
    CrossCritique,
    Dissent,
    HeadDraft,
    classify_dissent_cell,
    deliberate,
    render_for_envelope,
    render_for_user,
)
from hydra_core.heads import alias_for


REPO_ROOT = Path(__file__).resolve().parents[1]


# --- dissent classifier ------------------------------------------------------

def test_classify_dissent_routes_risk_to_kan():
    assert classify_dissent_cell("regulatory risk we have not modeled") == "kan"
    assert classify_dissent_cell("there is an exfiltration vulnerability here") == "kan"


def test_classify_dissent_routes_validated_win_to_dui():
    assert classify_dissent_cell("we shipped this exact play before and customers loved it") == "dui"


def test_classify_dissent_defaults_to_kan_on_neutral_text():
    # Per manifesto: bias toward remembering risk.
    assert classify_dissent_cell("an open question worth revisiting") == "kan"


# --- stub responders ---------------------------------------------------------

def _stub_draft(plaza: str, question: str, context: str) -> HeadDraft:
    alias = alias_for(plaza, project_root=REPO_ROOT)
    mythic = alias.mythic if alias else plaza
    if plaza == "ceo":
        position = "Expand to EU after we close the seed round; tie to Q3 OKR."
        cells = ["qian", "li"]
    elif plaza == "cfo":
        position = "Hold. Unit economics in EU are 18 months from break-even; risk to runway."
        cells = ["kan", "gen"]
    else:
        position = "Defer to whichever path the board votes; I see merit in both."
        cells = ["li"]
    return HeadDraft(plaza=plaza, mythic=mythic, position=position, confidence=0.7, cells_proposed=cells)


def _stub_critique(critic_alias, target_draft, question):
    if critic_alias.plaza == "cfo":
        return CrossCritique(
            critic_plaza=critic_alias.plaza, critic_mythic=critic_alias.mythic,
            target_plaza=target_draft.plaza, target_mythic=target_draft.mythic,
            steelman="If the seed closes on time and EU CAC stays under model, the timing is defensible.",
            remaining_concern="If FX moves against us by 8% the EU bet breaks our covenant — that is a substantive risk.",
        )
    if critic_alias.plaza == "ceo":
        return CrossCritique(
            critic_plaza=critic_alias.plaza, critic_mythic=critic_alias.mythic,
            target_plaza=target_draft.plaza, target_mythic=target_draft.mythic,
            steelman="The hold position protects runway through a known-bad scenario.",
            remaining_concern="",
        )
    return CrossCritique(
        critic_plaza=critic_alias.plaza, critic_mythic=critic_alias.mythic,
        target_plaza=target_draft.plaza, target_mythic=target_draft.mythic,
        steelman="(no specific objection)",
        remaining_concern="",
    )


def _stub_iris(question, drafts, critiques):
    return (
        "User-perspective read: the question is whether the EU bet protects "
        "the calling or distracts from it. Demeter's runway concern is the "
        "load-bearing constraint; Solon's growth thesis is the upside."
    )


def _stub_synthesizer(question, drafts, critiques, iris_reflection):
    return (
        "Recommendation: stage the EU expansion behind a 2026 Q3 close-rate "
        "milestone. Demeter's covenant concern is real; Solon's OKR alignment "
        "stands but is conditional on funding."
    )


# --- cycle -------------------------------------------------------------------

def test_deliberation_produces_four_drafts_and_synthesizes():
    outcome = deliberate(
        question="Should we expand to EU next quarter?",
        heads=["ceo", "cfo", "cso", "boardroom"],
        draft=_stub_draft,
        critique=_stub_critique,
        iris=_stub_iris,
        synthesize=_stub_synthesizer,
        project_root=REPO_ROOT,
    )
    assert len(outcome.drafts) == 4
    assert outcome.participants() == ["Solon", "Demeter", "Athena", "Iris"]
    assert outcome.synthesis.startswith("Recommendation")
    assert "Demeter" in outcome.iris_reflection


def test_deliberation_preserves_dissents_routed_to_kan():
    outcome = deliberate(
        question="Should we expand to EU next quarter?",
        heads=["ceo", "cfo"],
        draft=_stub_draft,
        critique=_stub_critique,
        iris=_stub_iris,
        synthesize=_stub_synthesizer,
        project_root=REPO_ROOT,
    )
    kan = outcome.kan_dissents()
    assert any("covenant" in d.statement.lower() for d in kan)
    assert all(d.cell == "kan" for d in kan)


def test_deliberation_raises_on_unresolvable_heads():
    with pytest.raises(ValueError):
        deliberate(
            question="?",
            heads=["not-a-head"],
            draft=_stub_draft,
            critique=_stub_critique,
            iris=_stub_iris,
            synthesize=_stub_synthesizer,
            project_root=REPO_ROOT,
        )


# --- renderers ---------------------------------------------------------------

def test_render_for_user_is_cathedral_voice():
    outcome = deliberate(
        question="Should we expand to EU next quarter?",
        heads=["ceo", "cfo"],
        draft=_stub_draft,
        critique=_stub_critique,
        iris=_stub_iris,
        synthesize=_stub_synthesizer,
        project_root=REPO_ROOT,
    )
    rendered = render_for_user(outcome, project_root=REPO_ROOT)
    assert "Solon" in rendered
    assert "Demeter" in rendered
    # No plaza slugs leak into user-facing text.
    assert "ceo" not in rendered
    assert "cfo" not in rendered
    assert "Iris reflects" in rendered
    assert "Risk (Kan ☵)" in rendered


def test_render_for_envelope_is_dual_register():
    outcome = deliberate(
        question="Should we expand to EU next quarter?",
        heads=["ceo", "cfo"],
        draft=_stub_draft,
        critique=_stub_critique,
        iris=_stub_iris,
        synthesize=_stub_synthesizer,
        project_root=REPO_ROOT,
    )
    env = render_for_envelope(outcome)
    # Plaza slugs are preserved alongside mythic names in the envelope.
    drafts = env["drafts"]
    assert {"plaza", "mythic"}.issubset(drafts[0].keys())
    assert drafts[0]["plaza"] == "ceo"
    assert drafts[0]["mythic"] == "Solon"


# --- supervisor synthesis renders crown labels -------------------------------

def test_supervisor_synthesis_uses_cathedral_crown_labels():
    """The Stage 4 threshold: synthesized answer signed by mythic / crown names."""
    from hydra_core.heads import crown_label_for_squad

    # The synthesizer code path uses `crown_label_for_squad` for each selected
    # squad. Confirm the rendering as exercised by node_synthesis.
    label = ", ".join(crown_label_for_squad(s) for s in ["executive", "engineering"])
    assert label == "the Executive Crown, the Forge Crown"
