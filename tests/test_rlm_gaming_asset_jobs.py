from uuid import uuid4

import pytest
from pydantic import ValidationError

from hydra_core.schemas import AssetJob, MemoryRef, validate_envelope
from mcp_servers.hydra_control.server import _ENVELOPE_EXTRA_FIELDS


@pytest.mark.parametrize("model_type", ["mesh", "rig"])
def test_rlm_gaming_3d_asset_job_is_a_valid_hydra_envelope(model_type: str) -> None:
    contract = MemoryRef(
        tier="episodic",
        key=f"rlmgaming:output:dcc/{model_type}-contract.md",
        summary=f"{model_type} contract",
    )
    job = AssetJob(
        workflow_id=uuid4(),
        origin_squad="rlm-gaming",
        target_squad="garland",
        model_type=model_type,
        output_bucket="rlm-garland/game-assets",
        context_refs=[contract],
        style_refs=[contract],
        provenance_required=True,
    )

    parsed = validate_envelope(job.model_dump(mode="json"))

    assert parsed.model_type == model_type
    assert parsed.provenance_required is True
    assert parsed.context_refs == [contract]


def test_asset_job_requires_an_output_bucket() -> None:
    with pytest.raises(ValidationError):
        AssetJob(
            workflow_id=uuid4(),
            origin_squad="rlm-gaming",
            model_type="mesh",
        )


def test_control_plane_allows_asset_job_provenance() -> None:
    assert "provenance_required" in _ENVELOPE_EXTRA_FIELDS["ASSET_JOB"]
