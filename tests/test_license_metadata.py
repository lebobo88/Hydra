"""Packaging metadata must agree with the LICENSE file (finding E2-33).

`pyproject.toml` once declared ``license = { text = "Proprietary" }`` while the
repository shipped an MIT `LICENSE`. These tests keep the two in lockstep.
"""

from __future__ import annotations

import tomllib
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
PYPROJECT = REPO_ROOT / "pyproject.toml"
LICENSE = REPO_ROOT / "LICENSE"

# SPDX identifier -> the first line the matching LICENSE file must carry.
_SPDX_TO_HEADING = {
    "MIT": "MIT License",
    "Apache-2.0": "Apache License",
    "BSD-3-Clause": "BSD 3-Clause License",
}


@pytest.fixture(scope="module")
def project_table() -> dict:
    with PYPROJECT.open("rb") as handle:
        return tomllib.load(handle)["project"]


def test_license_is_an_spdx_expression(project_table: dict) -> None:
    """PEP 639: ``license`` is a string SPDX expression, not a legacy table."""
    license_value = project_table["license"]
    assert isinstance(license_value, str), (
        "expected a PEP 639 SPDX string, got a legacy table: " f"{license_value!r}"
    )
    assert license_value in _SPDX_TO_HEADING, f"unrecognized SPDX identifier {license_value!r}"


def test_license_files_points_at_the_license(project_table: dict) -> None:
    assert project_table["license-files"] == ["LICENSE"]
    assert LICENSE.is_file(), "pyproject references LICENSE but the file is missing"


def test_pyproject_license_matches_license_file(project_table: dict) -> None:
    """The declared SPDX identifier must match the LICENSE file's first line."""
    first_line = LICENSE.read_text(encoding="utf-8").splitlines()[0].strip()
    expected = _SPDX_TO_HEADING[project_table["license"]]
    assert first_line == expected, (
        f"pyproject declares {project_table['license']!r} but LICENSE begins {first_line!r}"
    )


def test_no_license_classifier(project_table: dict) -> None:
    """PEP 639 forbids ``License ::`` classifiers beside an SPDX expression."""
    classifiers = project_table.get("classifiers", [])
    offenders = [c for c in classifiers if c.startswith("License ::")]
    assert not offenders, f"remove deprecated license classifiers: {offenders}"


def test_build_backend_supports_pep_639() -> None:
    """setuptools gained PEP 639 support in 77.0.0."""
    with PYPROJECT.open("rb") as handle:
        requires = tomllib.load(handle)["build-system"]["requires"]
    setuptools_pin = next(r for r in requires if r.startswith("setuptools"))
    floor = setuptools_pin.split(">=", 1)[1].strip()
    assert tuple(int(part) for part in floor.split(".")) >= (77,), (
        f"PEP 639 needs setuptools>=77, pyproject pins {setuptools_pin!r}"
    )
