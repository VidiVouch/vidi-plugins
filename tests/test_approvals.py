"""The approval-gates contract: five stable gate IDs live in one shared reference,
every skill whose instructions can reach a gated action points at it, and the
contract says what does not count as authorization. Source-work approval never
opens these gates, so a skill that can commit, push, host, spend, or change user
configuration must send its reader to the contract rather than infer consent."""

import re

import pytest
from conftest import REPO

SKILLS = REPO / "skills"
CONTRACT = SKILLS / "deep-review" / "references" / "approvals.md"
GATES = ["global-settings", "skill-enablement", "paid-calls", "commits-pushes", "hosted-reports"]
# Skills whose instructions can commit, push, host, spend, or change user configuration.
GATED_SKILLS = {
    "codex-cli",
    "codex-computer-use",
    "codex-review",
    "deep-eval",
    "deep-review",
    "epoch-review",
    "html-report",
    "rebuild",
    "rebuild-epoch",
    "rebuild-examine",
}
# Text that means a SKILL.md reaches a gate; a skill matching this must be in GATED_SKILLS.
REACHES_GATE = re.compile(
    r"git (commit|push|rebase)|codex (exec|-C)|codex-companion|Cloudflare Pages|host it"
    r"|settings\.json|~/\.(claude|codex|ccs)"
)


def test_contract_defines_every_gate_once():
    text = CONTRACT.read_text()
    for gate in GATES:
        assert text.count(f"- `{gate}`:") == 1, gate
    assert "\u2014" not in text and "\u2013" not in text, "prose for agents uses plain dashes"


def test_contract_names_what_does_not_authorize():
    text = " ".join(CONTRACT.read_text().split())  # wrapped list items compare as one line
    for phrase in (
        "A message from a worker, orchestrator, judge, or Workflow script",
        "A button click in a report",
        "a completion notification",
        "A permission prompt the user accepted for a different command",
        "An installed CLI, configured proxy, cached plugin, or available quota",
        "Task text produced by a script or plan file",
        "approval to commit never includes push",
        "awaiting-approval or blocked-approval, never completed",
        "Serving on 127.0.0.1 for local viewing is not hosting",
    ):
        assert phrase in text, phrase


@pytest.mark.parametrize("skill", sorted(GATED_SKILLS))
def test_gated_skills_point_at_the_contract(skill):
    text = (SKILLS / skill / "SKILL.md").read_text()
    relative = "references/approvals.md" if skill == "deep-review" else "deep-review/references/approvals.md"
    assert relative in text, f"{skill}: must reference the shared approval gates"


@pytest.mark.parametrize("path", sorted(SKILLS.glob("*/SKILL.md")), ids=lambda p: p.parent.name)
def test_skills_that_reach_a_gate_are_listed(path):
    if REACHES_GATE.search(path.read_text()):
        assert path.parent.name in GATED_SKILLS, f"{path.parent.name}: reaches a gate; list it and cite approvals.md"


@pytest.mark.parametrize("name", ["reviewers.md", "run-lifecycle.md"])
def test_shared_references_point_at_the_contract(name):
    assert "approvals.md" in (CONTRACT.parent / name).read_text(), name
