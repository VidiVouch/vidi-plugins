"""Team availability must not choose the fix strategy or the edit destination.

The injected `CLAUDE_CODE_EXPERIMENTAL_AGENT_TEAMS` fact used to double as the fix
strategy, so enabling teams silently put persistent fixers in the live checkout.
These tests run the real injected shell line and pin the skill text and runtime
so that frozen-worktree fixing stays the default until live editing is selected.
"""

import os
import re
import subprocess

import pytest
from conftest import REPO

SKILL = REPO / "skills/deep-review"
SKILL_TEXT = (SKILL / "SKILL.md").read_text()
INJECTIONS = re.findall(r"^- ([^:]+): !`(.*)`$", SKILL_TEXT, re.M)


def run_injection(command, teams):
    env = {key: value for key, value in os.environ.items() if key != "CLAUDE_CODE_EXPERIMENTAL_AGENT_TEAMS"}
    if teams is not None:
        env["CLAUDE_CODE_EXPERIMENTAL_AGENT_TEAMS"] = teams
    return subprocess.run(
        ["bash", "-c", command], env=env, text=True, capture_output=True, timeout=10, check=True
    ).stdout.strip()


@pytest.mark.parametrize(("teams", "expected"), [("1", "available"), ("0", "unavailable"), (None, "unavailable")])
def test_injected_team_fact_reports_availability_only(teams, expected):
    commands = [command for label, command in INJECTIONS if "CLAUDE_CODE_EXPERIMENTAL_AGENT_TEAMS" in command]
    assert [label for label, command in INJECTIONS if "CLAUDE_CODE_EXPERIMENTAL_AGENT_TEAMS" in command] == [
        "Team support"
    ]
    output = run_injection(commands[0], teams)
    assert output == expected
    assert "teams" not in output and "workflow" not in output


def test_frozen_worktree_fixing_is_the_default_until_live_editing_is_selected():
    text = " ".join(SKILL_TEXT.split())
    assert "[ --fix-strategy workflow|teams ]" in text
    assert "`--fix-strategy workflow|teams` - default workflow: fresh fixers in the frozen worktree" in text
    assert "An enabled teams environment variable does not select this strategy" in text
    assert "fix: <true unless --nofix or explicit --fix-strategy teams>" in text
    assert "Reject unavailable team mode rather than silently selecting another strategy" in text
    assert "Runs only with explicit --fix-strategy teams and fixing (not --nofix)" in text
    assert "team availability alone never opts into live-tree edits" in text
    assert "Only explicit teams mode opts into live-tree edits" in text
    assert "the injected Team support line is availability only" in text
    assert "the edit destination is the frozen worktree unless live editing is explicitly selected" in text
    assert "a decision the user makes, not one the environment makes" in text
    # The retired coupling: a strategy read off the environment instead of the flag.
    for phrase in ("Fix strategy ==", "injected **Fix strategy**", 'echo "teams"', "Fix strategy:"):
        assert phrase not in SKILL_TEXT, phrase


def test_runtime_and_callers_never_branch_on_team_availability():
    runtime = [
        SKILL / "deep-review.workflow.js",
        *sorted((SKILL / "scripts").glob("*.py")),
        *sorted(REPO.glob("agents/*.md")),
    ]
    callers = [REPO / "skills" / name / "SKILL.md" for name in ("epoch-review", "rebuild", "rebuild-examine")]
    for path in runtime + callers:
        assert "CLAUDE_CODE_EXPERIMENTAL_AGENT_TEAMS" not in path.read_text(), path
    for path in callers:
        assert "--fix-strategy teams" not in path.read_text(), path
    # The workflow takes one boolean and one root; it has no strategy or destination argument to misread.
    workflow = (SKILL / "deep-review.workflow.js").read_text()
    assert re.search(r"^\s*if \(!A\.fix \|\|", workflow, re.M)
    assert not re.search(r"A\.(fixStrategy|teams|liveEdit|inPlace)\b", workflow)
