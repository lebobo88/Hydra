"""Drift check for the Covenant -> plugins/hydra/agents adapter.

Only the compare step (compareDirs) is exercised, with node but without the
cov toolchain or network.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
SCRIPT = REPO / "covenant" / "scripts" / "sync-agents.mjs"
AGENTS = REPO / "plugins" / "hydra" / "agents"
NAMES = ["plan-author.md", "plan-critic.md", "plan-scribe.md"]

pytestmark = pytest.mark.skipif(shutil.which("node") is None, reason="node not installed")


def _compare(emitted: Path, installed: Path) -> list[str]:
    code = (
        f"import {{compareDirs}} from {json.dumps(SCRIPT.as_uri())};"
        "console.log(JSON.stringify(compareDirs(process.argv[1], process.argv[2])))"
    )
    out = subprocess.run(
        ["node", "--input-type=module", "-e", code, str(emitted), str(installed)],
        check=True, capture_output=True, text=True,
    )
    return json.loads(out.stdout)


def _emitted_from_installed(tmp_path: Path) -> Path:
    emitted = tmp_path / "emitted"
    emitted.mkdir()
    for n in NAMES:
        data = (AGENTS / n).read_bytes().replace(b"\r\n", b"\n")
        (emitted / n).write_bytes(data)
    return emitted


def test_package_scripts_use_adapter() -> None:
    scripts = json.loads((REPO / "covenant" / "package.json").read_text())["scripts"]
    assert scripts["test"] == "node scripts/sync-agents.mjs check"
    assert scripts["build"] == "node scripts/sync-agents.mjs build"
    assert "../plugins/hydra" not in json.dumps(scripts)


def test_installed_agents_match_emitted_form(tmp_path: Path) -> None:
    assert _compare(_emitted_from_installed(tmp_path), AGENTS) == []


def test_crlf_checkout_is_not_drift(tmp_path: Path) -> None:
    emitted = _emitted_from_installed(tmp_path)
    inst = tmp_path / "inst"
    inst.mkdir()
    for n in NAMES:
        (inst / n).write_bytes((emitted / n).read_bytes().replace(b"\n", b"\r\n"))
    assert _compare(emitted, inst) == []


def test_changed_agent_is_drift(tmp_path: Path) -> None:
    emitted = _emitted_from_installed(tmp_path)
    inst = tmp_path / "inst"
    shutil.copytree(emitted, inst)
    with (inst / "plan-critic.md").open("ab") as fh:
        fh.write(b"extra\n")
    problems = _compare(emitted, inst)
    assert len(problems) == 1 and "plan-critic.md" in problems[0]


STUB_BIN = """\
import { mkdirSync, writeFileSync } from "node:fs";
import { join } from "node:path";
const a = process.argv.slice(2);
if (a[0] !== "build" || a[1] !== "--target" || a[2] !== "claude" || a[3] !== "--out") process.exit(3);
const dir = join(a[4], ".claude", "agents");
mkdirSync(dir, { recursive: true });
writeFileSync(join(dir, "plan-stub.md"), "stub agent\\n");
"""


def _run(script: Path, mode: str, installed: Path) -> subprocess.CompletedProcess[str]:
    env = {k: v for k, v in os.environ.items() if not k.startswith("COV_")}
    env["COV_INSTALLED_DIR"] = str(installed)
    return subprocess.run(
        ["node", str(script), mode], capture_output=True, text=True, env=env, cwd=script.parent,
    )


def test_build_and_check_work_in_path_with_space(tmp_path: Path) -> None:
    """The adapter must run cov via node (no shell) so a space in the checkout
    path is harmless. A copy of the adapter sits under a spaced path with a
    stub cov package resolved through its package.json `bin` field."""
    cov = tmp_path / "dir with space" / "covenant"
    (cov / "scripts").mkdir(parents=True)
    script = cov / "scripts" / "sync-agents.mjs"
    shutil.copy(SCRIPT, script)
    pkg = cov / "node_modules" / "@lebobo88" / "covenant-cli"
    pkg.mkdir(parents=True)
    (pkg / "package.json").write_text(
        json.dumps({"name": "@lebobo88/covenant-cli", "type": "module", "bin": {"cov": "./bin.js"}})
    )
    (pkg / "bin.js").write_text(STUB_BIN)
    installed = tmp_path / "dir with space" / "installed"

    built = _run(script, "build", installed)
    assert built.returncode == 0, built.stderr
    assert (installed / "plan-stub.md").read_text().strip() == "stub agent"
    assert "DEP0190" not in built.stderr

    checked = _run(script, "check", installed)
    assert checked.returncode == 0, checked.stderr
    assert "DEP0190" not in checked.stderr

    with (installed / "plan-stub.md").open("ab") as fh:
        fh.write(b"altered\n")
    drift = _run(script, "check", installed)
    assert drift.returncode == 1
    assert "plan-stub.md" in drift.stderr


def test_missing_agent_is_drift(tmp_path: Path) -> None:
    emitted = _emitted_from_installed(tmp_path)
    inst = tmp_path / "inst"
    shutil.copytree(emitted, inst)
    (inst / "plan-scribe.md").unlink()
    problems = _compare(emitted, inst)
    assert len(problems) == 1 and "missing" in problems[0]
