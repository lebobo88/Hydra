from __future__ import annotations

import os
import shutil
from pathlib import Path
from uuid import uuid4

import pytest


@pytest.fixture()
def tmp_path() -> Path:
    """Repo-local tmp_path for sandboxed Windows runs.

    The repo-local base lives INSIDE the Hydra git tree (the sandbox cannot
    write to the system temp dir), so without a ceiling, ``git rev-parse``
    invoked from any nested fixture dir would walk up and discover Hydra's
    own ``.git`` — breaking tests that create deliberately non-git dirs (e.g.
    repo-registry git-root verification). ``GIT_CEILING_DIRECTORIES`` stops
    git from ascending above the tmp base so a non-git fixture dir reads as
    genuinely non-git.
    """
    base = Path(__file__).resolve().parent / ".tmp-pytest"
    base.mkdir(parents=True, exist_ok=True)
    path = base / f"pytest-{uuid4().hex}"
    path.mkdir(parents=True, exist_ok=False)
    prev_ceiling = os.environ.get("GIT_CEILING_DIRECTORIES")
    os.environ["GIT_CEILING_DIRECTORIES"] = str(base)
    try:
        yield path
    finally:
        if prev_ceiling is None:
            os.environ.pop("GIT_CEILING_DIRECTORIES", None)
        else:
            os.environ["GIT_CEILING_DIRECTORIES"] = prev_ceiling
        shutil.rmtree(path, ignore_errors=True)
