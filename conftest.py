from __future__ import annotations

import shutil
from pathlib import Path
from uuid import uuid4

import pytest


@pytest.fixture()
def tmp_path() -> Path:
    """Repo-local tmp_path for sandboxed Windows runs."""
    base = Path(__file__).resolve().parent / ".tmp-pytest"
    base.mkdir(parents=True, exist_ok=True)
    path = base / f"pytest-{uuid4().hex}"
    path.mkdir(parents=True, exist_ok=False)
    try:
        yield path
    finally:
        shutil.rmtree(path, ignore_errors=True)
