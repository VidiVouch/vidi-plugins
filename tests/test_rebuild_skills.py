"""Structural guards for the rebuild epoch-pipeline skills. Each skill's
SKILL.md must carry the required frontmatter keys, the four skills must stay
mutually consistent on the ledger/gate contract, and the deterministic gate
script must stay syntactically valid and executable."""

import os
import re
import shutil
import stat
import subprocess

import pytest
from conftest import REPO

SKILLS = REPO / "skills"
REBUILD_SKILLS = ["rebuild", "rebuild-plan", "rebuild-epoch", "rebuild-examine"]
GATE = SKILLS / "rebuild-epoch" / "scripts" / "rebuild-gate.sh"

FRONTMATTER = re.compile(r"^---\n(.*?)\n---\n", re.DOTALL)


def _frontmatter(skill):
    text = (SKILLS / skill / "SKILL.md").read_text()
    m = FRONTMATTER.match(text)
    assert m, f"{skill}: SKILL.md must open with a YAML frontmatter block"
    return m.group(1), text


@pytest.mark.parametrize("skill", REBUILD_SKILLS)
def test_frontmatter_keys(skill):
    fm, _ = _frontmatter(skill)
    for key in ("name:", "description:", "argument-hint:", "allowed-tools:"):
        assert key in fm, f"{skill}: frontmatter missing {key}"
    assert f"name: {skill}" in fm, f"{skill}: frontmatter name must equal the directory"


@pytest.mark.parametrize("skill", REBUILD_SKILLS)
def test_claude_only_marms(skill):
    # These are Claude-only skills (! injection / Workflow / Skill dispatch);
    # they must say so, so nobody wires them into Codex where those are literal.
    _, text = _frontmatter(skill)
    assert "Claude-only" in text, f"{skill}: must declare itself Claude-only"


def test_gate_script_is_executable():
    mode = os.stat(GATE).st_mode
    assert mode & stat.S_IXUSR, "rebuild-gate.sh must be executable"


@pytest.mark.skipif(shutil.which("bash") is None, reason="bash not installed")
def test_gate_script_parses():
    out = subprocess.run(["bash", "-n", str(GATE)], capture_output=True, timeout=30, check=False)
    assert out.returncode == 0, out.stderr.decode()


@pytest.mark.skipif(shutil.which("bash") is None, reason="bash not installed")
def test_gate_script_usage_exits_2():
    # No args -> usage error, exit 2 (the documented usage/env-error code).
    out = subprocess.run(["bash", str(GATE)], capture_output=True, timeout=30, check=False)
    assert out.returncode == 2, out.stderr.decode()


def test_examine_calls_deep_review_workflow_by_plugin_root():
    # rebuild-examine must reuse deep-review's workflow at the plugin-root path,
    # never reimplement it — the whole reason these live in one plugin.
    text = (SKILLS / "rebuild-examine" / "SKILL.md").read_text()
    assert "deep-review/deep-review.workflow.js" in text
    assert "${CLAUDE_PLUGIN_ROOT}" in text


def test_epoch_and_examine_agree_on_gate_path():
    # Both callers must reference the one gate script by the same plugin-root path.
    ref = "${CLAUDE_PLUGIN_ROOT}/skills/rebuild-epoch/scripts/rebuild-gate.sh"
    for skill in ("rebuild-epoch", "rebuild-examine"):
        assert ref in (SKILLS / skill / "SKILL.md").read_text(), f"{skill}: must call the gate by its plugin-root path"


def test_canonical_bookkeeping_token():
    # The ledger status is IMPLEMENTED@<sha> for both build and --fix re-entry;
    # REVISED@ must never be introduced as a ledger status (only a spoken verb).
    epoch = (SKILLS / "rebuild-epoch" / "SKILL.md").read_text()
    assert "spoken return verb" in epoch, "rebuild-epoch must distinguish the spoken REVISED@ verb from the ledger token"
