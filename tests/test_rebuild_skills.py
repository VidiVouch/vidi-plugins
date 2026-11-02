"""Structural guards for the rebuild epoch-pipeline skills. Each skill's
SKILL.md must carry the required frontmatter keys, the four skills must stay
mutually consistent on the ledger/gate contract, and the deterministic gate
script must stay syntactically valid and executable."""

import json
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


def test_fresh_spawn_rule_is_preserved():
    # deep-review's dispatch layer refuses named, forked or resumed agents; the rebuild
    # skills state the same rule in prose for implementers and fixers, and it must stay.
    dispatcher = (SKILLS / "rebuild" / "SKILL.md").read_text().replace("\n  ", " ")
    epoch = (SKILLS / "rebuild-epoch" / "SKILL.md").read_text()
    assert "## Fresh-spawn rule" in dispatcher and "never resume a stalled agent" in dispatcher
    assert "never resume the original" in epoch and "Fresh spawn for every implementer round" in epoch


FRESH_LINE = (
    "You are a fresh instance with no prior conversation; this message and your agent definition are your whole brief."
)


def test_every_dispatch_is_fresh_from_a_written_handoff():
    # Every rebuild dispatch, first attempt included and whatever the role, is a new
    # instance briefed from its own handoff file; no path resumes a completed or stalled
    # agent. The opening line is the one deep-review's dispatch layer writes, so the agent
    # definitions' "wrong brief" rule reads the same in both places.
    # Whitespace is normalised so that wrapped and list-indented prose compares as one line.
    texts = {s: " ".join((SKILLS / s / "SKILL.md").read_text().split()) for s in REBUILD_SKILLS}
    workflow = (SKILLS / "deep-review" / "deep-review.workflow.js").read_text()
    assert FRESH_LINE in workflow

    rule = texts["rebuild"].split("## Fresh-spawn rule", 1)[1].split(" ## ", 1)[0]
    for role in ("architect", "plan-judge", "implementer", "examiner", "judge"):
        assert f"`{role}`" in rule, f"rebuild's fresh-spawn rule must cover {role}"
    assert "first attempt" in rule and "handoff-<unit>-<role>-<n>.md" in rule and FRESH_LINE in rule
    assert "never messaged, reopened, or handed a second unit" in rule
    assert "never means reattaching to an old conversation" in rule
    assert "never reopened, messaged, or handed further work" in texts["rebuild"]  # Phase 0 probe
    # One file per agent, <n> per role and unit: a judge panel never shares a file, a
    # single respawned judge takes the next number, and --final has a unit of its own.
    assert "one file per agent" in rule and "`final` for the final examination's judges" in rule
    assert "takes the next `<n>` alone" in rule

    epoch = texts["rebuild-epoch"]
    assert "handoff-$EPOCH-implementer-<n>.md" in epoch and FRESH_LINE in epoch
    assert epoch.count("new handoff file") >= 2, "gate-fix and revision rounds each get a new handoff file"

    plan = texts["rebuild-plan"]
    assert "handoff-plan-architect-<n>.md" in plan and "handoff-plan-plan-judge-<n>.md" in plan
    assert FRESH_LINE in plan and plan.count("new pair of judges") >= 2  # revision round and amend mode
    assert "each spawned from its own written handoff" in plan and "the judging round" not in plan
    assert "the architect's dispatches" in plan  # guide-mining sub-agents are not an unregistered path

    examine = texts["rebuild-examine"]
    assert "handoff-<epoch>-judge-<n>.md" in examine and "handoff-final-judge-<n>.md" in examine
    assert FRESH_LINE in examine and "never messaged or resumed" in examine
    assert "one handoff file per judge" in examine and "the JUDGING round" not in examine


def test_implementer_and_fixer_run_as_pinned_definitions_in_the_run_record():
    # The implementer is dispatched by agent type, so it runs on the ID the packaged
    # definition pins (the actual Opus 5.5 ID), never the session model or an alias, and
    # the ledger's registry row carries that requested model read from the definition.
    # The fixers a rebuild runs are the workflow's packaged fixer. The IDs asserted are
    # the roles file's, so a role change that forgets the skills is caught here.
    roles = json.loads((SKILLS / "deep-review" / "model-roles.json").read_text())
    texts = {s: " ".join((SKILLS / s / "SKILL.md").read_text().split()) for s in REBUILD_SKILLS}
    epoch = texts["rebuild-epoch"]
    assert "`subagent_type: vidi-skills:implementer`" in epoch and "(session model)" not in epoch
    assert 'model_roles.py" --dispatch implementer' in epoch and roles["implementer-opus"]["model"] in epoch
    assert "no `model`, `name`, fork or resume option" in epoch
    assert "The implementer is the packaged definition" in epoch
    for skill in REBUILD_SKILLS:
        assert "requestedModel" in texts[skill], f"{skill}: every registry writer must fill requestedModel"
    assert "`requestedModel` is what the dispatch asked for" in texts["rebuild"]
    examine = texts["rebuild-examine"]
    assert "agents/fixer.md" in examine and roles["fixer-opus"]["model"] in examine
    assert "dispatches no fixer of its own" in examine


def test_canonical_bookkeeping_token():
    # The ledger status is IMPLEMENTED@<sha> for both build and --fix re-entry;
    # REVISED@ must never be introduced as a ledger status (only a spoken verb).
    epoch = (SKILLS / "rebuild-epoch" / "SKILL.md").read_text()
    assert "spoken return verb" in epoch, (
        "rebuild-epoch must distinguish the spoken REVISED@ verb from the ledger token"
    )
