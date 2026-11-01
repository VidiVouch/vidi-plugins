"""Workflow units never talk to each other: disagreement travels as evidence in packets read by fresh units. Live
discussion exists only in the lead-owned team phase, which team_phase.py records and refuses to close while a
teammate is standing. These tests run the real CLI against temporary files and pin the texts that state the rule."""

import json
import re
import subprocess
import sys

import pytest
from conftest import REPO

SKILL = REPO / "skills/deep-review"
SCRIPT = SKILL / "scripts/team_phase.py"
REFERENCE = SKILL / "references/work-unit.md"


def cli(*args, success=True):
    result = subprocess.run([sys.executable, str(SCRIPT), *map(str, args)], capture_output=True, text=True, timeout=30)
    if success:
        assert result.returncode == 0, result.stderr
        assert result.stderr == ""
        return json.loads(result.stdout)
    assert result.returncode == 1, result.stdout
    assert not result.stdout and "Traceback" not in result.stderr
    return result.stderr


@pytest.fixture
def record(tmp_path):
    path = tmp_path / "run" / "team-phases.json"
    cli("open", "--record", path, "--round", 1)
    return path


def test_a_phase_records_each_teammate_from_spawn_to_shutdown_and_then_closes(record):
    phase = cli("spawn", "--record", record, "--name", "fixer-r1-b1", "--files", "src/a.py", "src/b.py")
    assert phase["round"] == 1 and phase["standing"] == ["fixer-r1-b1"]
    cli("spawn", "--record", record, "--name", "fixer-r1-b2", "--files", "src/c.py")
    assert cli("shutdown", "--record", record, "--name", "fixer-r1-b1")["standing"] == ["fixer-r1-b2"]
    cli("shutdown", "--record", record, "--name", "fixer-r1-b2")
    closed = cli("close", "--record", record)
    assert closed["closedAt"] and closed["standing"] == []
    stored = json.loads(record.read_text())
    assert stored["version"] == 1 and [p["round"] for p in stored["phases"]] == [1]
    teammates = stored["phases"][0]["teammates"]
    assert [t["name"] for t in teammates] == ["fixer-r1-b1", "fixer-r1-b2"]
    assert teammates[0]["files"] == ["src/a.py", "src/b.py"]
    assert all(t["spawnedAt"] and t["shutDownAt"] for t in teammates)


def test_close_is_refused_while_any_teammate_is_standing_and_names_them(record):
    cli("spawn", "--record", record, "--name", "fixer-r1-b1", "--files", "src/a.py")
    cli("spawn", "--record", record, "--name", "fixer-r1-b2", "--files", "src/b.py")
    cli("shutdown", "--record", record, "--name", "fixer-r1-b1")
    error = cli("close", "--record", record, success=False)
    assert "still standing: fixer-r1-b2" in error and "shut each one down by name" in error
    assert json.loads(record.read_text())["phases"][0]["closedAt"] is None
    cli("shutdown", "--record", record, "--name", "fixer-r1-b2")
    assert cli("close", "--record", record)["closedAt"]


def test_one_phase_at_a_time_and_each_round_once(record):
    assert "round 1 is still open" in cli("open", "--record", record, "--round", 2, success=False)
    cli("close", "--record", record)
    assert "round 1 already has a phase" in cli("open", "--record", record, "--round", 1, success=False)
    assert cli("open", "--record", record, "--round", 2)["round"] == 2
    assert "no open phase" in cli("close", "--record", record.parent / "none.json", success=False)
    assert "no open phase" in cli(
        "spawn", "--record", record.parent / "none.json", "--name", "x-r1-", "--files", "a", success=False
    )


def test_a_name_carries_its_round_and_is_never_reused(record):
    assert "must carry this phase's round as -r1-" in cli(
        "spawn", "--record", record, "--name", "fixer-r2-b1", "--files", "src/a.py", success=False
    )
    cli("spawn", "--record", record, "--name", "fixer-r1-b1", "--files", "src/a.py")
    assert "already used; a replacement gets a new name" in cli(
        "spawn", "--record", record, "--name", "fixer-r1-b1", "--files", "src/z.py", success=False
    )
    cli("spawn", "--record", record, "--name", "cause-r1-r2-b1", "--files", "src/b.py")
    for name in ("fixer-r1-b1", "cause-r1-r2-b1"):
        cli("shutdown", "--record", record, "--name", name)
    cli("close", "--record", record)
    cli("open", "--record", record, "--round", 2)
    # A name from an earlier phase is a stale address: the round tag refuses it, and so does the reuse rule when the
    # tag happens to match.
    assert "round as -r2-" in cli("spawn", "--record", record, "--name", "fixer-r1-b1", "--files", "a", success=False)
    assert "already used" in cli("spawn", "--record", record, "--name", "cause-r1-r2-b1", "--files", "a", success=False)


def test_standing_teammates_own_disjoint_files_and_the_cause_owner_waits_for_the_stage(record):
    cli("spawn", "--record", record, "--name", "fixer-r1-b1", "--files", "tests/test_a.py")
    cli("spawn", "--record", record, "--name", "fixer-r1-b2", "--files", "src/a.py")
    error = cli("spawn", "--record", record, "--name", "fixer-r1-cause1", "--files", "src/a.py", success=False)
    assert "src/a.py is owned by standing teammate fixer-r1-b2; shut it down first" in error
    cli("shutdown", "--record", record, "--name", "fixer-r1-b1")
    cli("shutdown", "--record", record, "--name", "fixer-r1-b2")
    assert cli("spawn", "--record", record, "--name", "fixer-r1-cause1", "--files", "src/a.py")["standing"] == [
        "fixer-r1-cause1"
    ]
    assert "each listed once" in cli(
        "spawn", "--record", record, "--name", "fixer-r1-b3", "--files", "x", "x", success=False
    )


def test_shutdown_needs_a_standing_teammate_of_the_open_phase(record):
    assert "no teammate fixer-r1-b9" in cli("shutdown", "--record", record, "--name", "fixer-r1-b9", success=False)
    cli("spawn", "--record", record, "--name", "fixer-r1-b1", "--files", "src/a.py")
    cli("shutdown", "--record", record, "--name", "fixer-r1-b1")
    assert "already shut down" in cli("shutdown", "--record", record, "--name", "fixer-r1-b1", success=False)


def test_a_damaged_record_is_refused_not_repaired(tmp_path):
    path = tmp_path / "team-phases.json"
    path.write_text("{")
    assert "cannot read team phase record" in cli("open", "--record", path, "--round", 1, success=False)
    path.write_text(json.dumps({"version": 2, "phases": []}))
    assert "invalid team phase record" in cli("close", "--record", path, success=False)


def test_the_script_never_spawns_messages_or_stops_an_agent():
    source = SCRIPT.read_text()
    assert not re.search(r"subprocess|os\.kill|signal\.|SendMessage|Agent\(", source)
    assert "Never spawns, messages or stops an agent" in source


def test_workflow_units_disagree_through_evidence_and_have_no_message_transport():
    """The runtime offers a unit one prompt and one result. Nothing in the workflow or a worker definition asks a
    unit to message another; disagreement is a rebuttal, an escalation or a judge result read by a fresh unit."""
    workflow = (SKILL / "deep-review.workflow.js").read_text()
    assert "SendMessage" not in workflow and "teammate" not in workflow.replace("standing teammate", "")
    assert "const FRESH_OPTS = ['label', 'phase', 'schema', 'model', 'effort', 'agentType']" in workflow
    for path in sorted(REPO.glob("agents/*.md")) + sorted((SKILL / "reviewers").glob("*.md")):
        assert "SendMessage" not in path.read_text(), path
    fixer = " ".join((REPO / "agents/fixer.md").read_text().split())
    assert "Discussion is with the lead only" in fixer and "do not message another teammate" in fixer
    assert "rebut it in the packet" in fixer and "escalate it in the packet" in fixer


def test_reference_names_the_transport_and_the_lead_owned_phase():
    text = REFERENCE.read_text()
    assert "## How units disagree" in text
    section = " ".join(text.split("## How units disagree")[1].split("## The lead stays thin")[0].split())
    assert "Units in the Workflow runtime never talk to each other" in section
    assert "no address, no message tool and no teammate" in section
    assert "merged by evidence, never by count" in section
    assert "rebuts it in its fix packet" in section and "escalates in the packet" in section
    assert "no party's conclusion reaches another's brief before that party has formed its own" in section
    assert "needs no team support" in section
    assert "the lead owns it, never the Workflow" in section
    assert "`scripts/team_phase.py`" in section and "SendMessage" in section
    assert "refuses while any teammate is still standing" in section
    assert "teammates do not message each other" in section
    assert "never spawns, messages or stops an agent" in section


def test_skill_keeps_the_phase_on_record_from_open_to_close():
    text = " ".join((SKILL / "SKILL.md").read_text().split())
    phase = text.split("## Phase 3.6")[1].split("## Phase 4")[0]
    assert "This is the only place deep-review holds a live discussion" in phase
    assert "a Workflow unit has no address and no message tool" in phase
    assert 'TEAM_RECORD="$RUN_DIR/team-phases.json"' in phase
    commands = [
        'team_phase.py open --record "$TEAM_RECORD" --round <round>',
        'team_phase.py spawn --record "$TEAM_RECORD" --name <name> --files <bucket>',
        'team_phase.py shutdown --record "$TEAM_RECORD" --name <name>',
        'team_phase.py close --record "$TEAM_RECORD"',
    ]
    positions = [phase.index(command) for command in commands]
    assert positions == sorted(positions), "open, spawn, shutdown, close, in that order"
    assert positions[-1] < phase.index("re-run the workflow report-only"), "the phase closes before the re-review"
    assert "Discussion is `SendMessage` between you and one teammate" in phase
    assert "never ask teammates to message each other" in phase
    assert "refuses until each has a recorded shutdown" in phase
    rules = text.split("## Important rules")[1]
    assert "Units do not talk to each other" in rules and "Live discussion exists only in Phase 3.6" in rules


def test_the_texts_this_rule_added_carry_no_em_or_en_dash():
    reference = REFERENCE.read_text().split("## How units disagree")[1].split("## The lead stays thin")[0]
    for text in (SCRIPT.read_text(), reference, (REPO / "tests/test_team_phase.py").read_text()):
        assert "\u2013" not in text and "\u2014" not in text
