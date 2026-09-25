"""Unit tests for the pure snapshot/compare helpers backing
``tests/conftest.py``'s ``_real_hydra_home_pollution_guard`` session fixture.

These exercise the comparison LOGIC against throwaway ``tmp_path`` copies --
never the two real watched paths (the operator's real checkout's
``docs/plans/`` and the real ``~/.hydra/repos.json``). The session fixture
itself stays read-only against those real paths; this file proves the logic
it delegates to actually detects a change, independently of that read-only
constraint.
"""
from __future__ import annotations

from pathlib import Path

from conftest import (
    _diff_pollution_guard_snapshots,
    _snapshot_plans_dir,
    _snapshot_repos_json_sha256,
)


def test_changed_repos_json_digest_is_reported(tmp_path: Path):
    plans_dir = tmp_path / "docs" / "plans"
    plans_dir.mkdir(parents=True)
    repos_json = tmp_path / "repos.json"
    repos_json.write_text('{"hydra": "/repos/hydra"}', encoding="utf-8")

    before_plans = _snapshot_plans_dir(plans_dir)
    before_sha = _snapshot_repos_json_sha256(repos_json)

    repos_json.write_text('{"hydra": "/repos/hydra-TAMPERED"}', encoding="utf-8")

    after_plans = _snapshot_plans_dir(plans_dir)
    after_sha = _snapshot_repos_json_sha256(repos_json)

    problems = _diff_pollution_guard_snapshots(
        plans_dir=plans_dir,
        repos_json=repos_json,
        before_plans=before_plans,
        before_repos_sha=before_sha,
        after_plans=after_plans,
        after_repos_sha=after_sha,
    )
    assert len(problems) == 1
    assert "repos.json changed" in problems[0]


def test_new_file_in_watched_plans_dir_is_reported(tmp_path: Path):
    plans_dir = tmp_path / "docs" / "plans"
    plans_dir.mkdir(parents=True)
    (plans_dir / "existing.html").write_text("<p>pre-existing</p>", encoding="utf-8")
    repos_json = tmp_path / "repos.json"
    repos_json.write_text("{}", encoding="utf-8")

    before_plans = _snapshot_plans_dir(plans_dir)
    before_sha = _snapshot_repos_json_sha256(repos_json)

    (plans_dir / "leaked-plan.html").write_text("<p>leaked</p>", encoding="utf-8")

    after_plans = _snapshot_plans_dir(plans_dir)
    after_sha = _snapshot_repos_json_sha256(repos_json)

    problems = _diff_pollution_guard_snapshots(
        plans_dir=plans_dir,
        repos_json=repos_json,
        before_plans=before_plans,
        before_repos_sha=before_sha,
        after_plans=after_plans,
        after_repos_sha=after_sha,
    )
    assert len(problems) == 1
    assert "leaked-plan.html" in problems[0]
    # Pre-existing litter must never be flagged.
    assert "existing.html" not in problems[0]


def test_absent_to_present_repos_json_is_reported(tmp_path: Path):
    plans_dir = tmp_path / "docs" / "plans"
    plans_dir.mkdir(parents=True)
    repos_json = tmp_path / "repos.json"
    # repos.json does not exist yet at "before" time.

    before_plans = _snapshot_plans_dir(plans_dir)
    before_sha = _snapshot_repos_json_sha256(repos_json)
    assert before_sha is None

    repos_json.write_text('{"leaked": "/repos/leaked"}', encoding="utf-8")

    after_plans = _snapshot_plans_dir(plans_dir)
    after_sha = _snapshot_repos_json_sha256(repos_json)
    assert after_sha is not None

    problems = _diff_pollution_guard_snapshots(
        plans_dir=plans_dir,
        repos_json=repos_json,
        before_plans=before_plans,
        before_repos_sha=before_sha,
        after_plans=after_plans,
        after_repos_sha=after_sha,
    )
    assert len(problems) == 1
    assert "repos.json changed" in problems[0]


def test_overwritten_existing_file_is_reported_as_modified(tmp_path: Path):
    plans_dir = tmp_path / "docs" / "plans"
    plans_dir.mkdir(parents=True)
    (plans_dir / "existing.html").write_text("<p>original</p>", encoding="utf-8")
    (plans_dir / "untouched.html").write_text("<p>leave me alone</p>", encoding="utf-8")
    repos_json = tmp_path / "repos.json"
    repos_json.write_text("{}", encoding="utf-8")

    before_plans = _snapshot_plans_dir(plans_dir)
    before_sha = _snapshot_repos_json_sha256(repos_json)

    # Overwrite an existing file in place -- name-only snapshotting would
    # miss this entirely since the set of names is unchanged.
    (plans_dir / "existing.html").write_text("<p>TAMPERED</p>", encoding="utf-8")

    after_plans = _snapshot_plans_dir(plans_dir)
    after_sha = _snapshot_repos_json_sha256(repos_json)

    problems = _diff_pollution_guard_snapshots(
        plans_dir=plans_dir,
        repos_json=repos_json,
        before_plans=before_plans,
        before_repos_sha=before_sha,
        after_plans=after_plans,
        after_repos_sha=after_sha,
    )
    assert len(problems) == 1
    assert "existing.html" in problems[0]
    assert "overwritten" in problems[0]
    # A file that was neither added, removed, nor modified must never be
    # named in the problem list.
    assert "untouched.html" not in problems[0]


def test_removed_file_is_reported(tmp_path: Path):
    plans_dir = tmp_path / "docs" / "plans"
    plans_dir.mkdir(parents=True)
    (plans_dir / "existing.html").write_text("<p>pre-existing</p>", encoding="utf-8")
    (plans_dir / "doomed.html").write_text("<p>about to vanish</p>", encoding="utf-8")
    repos_json = tmp_path / "repos.json"
    repos_json.write_text("{}", encoding="utf-8")

    before_plans = _snapshot_plans_dir(plans_dir)
    before_sha = _snapshot_repos_json_sha256(repos_json)

    (plans_dir / "doomed.html").unlink()

    after_plans = _snapshot_plans_dir(plans_dir)
    after_sha = _snapshot_repos_json_sha256(repos_json)

    problems = _diff_pollution_guard_snapshots(
        plans_dir=plans_dir,
        repos_json=repos_json,
        before_plans=before_plans,
        before_repos_sha=before_sha,
        after_plans=after_plans,
        after_repos_sha=after_sha,
    )
    assert len(problems) == 1
    assert "doomed.html" in problems[0]
    assert "disappeared" in problems[0]
    assert "existing.html" not in problems[0]


def test_unchanged_state_reports_nothing(tmp_path: Path):
    plans_dir = tmp_path / "docs" / "plans"
    plans_dir.mkdir(parents=True)
    (plans_dir / "existing.html").write_text("<p>pre-existing</p>", encoding="utf-8")
    repos_json = tmp_path / "repos.json"
    repos_json.write_text('{"hydra": "/repos/hydra"}', encoding="utf-8")

    before_plans = _snapshot_plans_dir(plans_dir)
    before_sha = _snapshot_repos_json_sha256(repos_json)

    # Nothing touched between snapshots.

    after_plans = _snapshot_plans_dir(plans_dir)
    after_sha = _snapshot_repos_json_sha256(repos_json)

    problems = _diff_pollution_guard_snapshots(
        plans_dir=plans_dir,
        repos_json=repos_json,
        before_plans=before_plans,
        before_repos_sha=before_sha,
        after_plans=after_plans,
        after_repos_sha=after_sha,
    )
    assert problems == []
