"""Coverage for the MarketBliss-owned immutable rubric registrations."""
from __future__ import annotations

from hydra_core.judge.registry import get_rubric


def test_marketbliss_rubric_ids_are_registered_with_source_requirements() -> None:
    expected_requirements = {
        "marketing-brief-clarity@1": "audience/JTBD",
        "creative-brief-completeness@1": "proof points",
        "attribution-soundness@1": "causal assumptions",
        "regulated-claims-check@1": "substantiation",
        "experimentation-design@1": "randomization unit",
        "shot-list-coverage@1": "clearance status",
        "production-plan-completeness@1": "post-production milestones",
        "ip-clearance@1": "permitted usage scope",
    }

    for rubric_id, source_requirement in expected_requirements.items():
        rubric = get_rubric(rubric_id)
        assert rubric.kind == "marketing"
        assert rubric.score_dimensions == ("requirements_complete",)
        assert source_requirement in rubric.body_md


def test_existing_brand_consistency_id_remains_available_without_rewriting_v1() -> None:
    """The pre-existing v1 remains replay-stable; source reconciliation needs v2."""
    rubric = get_rubric("brand-consistency@1")
    assert rubric.kind == "garland"
    assert "voice_match" in rubric.score_dimensions
