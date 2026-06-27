"""Shared pytest fixtures for the Hydra test suite."""
from __future__ import annotations

import pytest


@pytest.fixture(autouse=True)
def _isolate_claude_engineer_env(monkeypatch):
    """Hermetic default: never let the operator's ``HYDRA_CLAUDE_ENGINEER`` /
    ``HYDRA_DISABLE_CLAUDE_ENGINEER`` shell env leak into a test run.

    ``HYDRA_CLAUDE_ENGINEER=1`` is an explicit force-on for headless Claude
    generation; if it leaked in, a scripted-dispatcher test exercising the drive
    loop would try to spawn a real ``claude`` subprocess. Unset both flags by
    default; tests that need them opt in via ``monkeypatch.setenv`` in-test.
    """
    monkeypatch.delenv("HYDRA_CLAUDE_ENGINEER", raising=False)
    monkeypatch.delenv("HYDRA_DISABLE_CLAUDE_ENGINEER", raising=False)
