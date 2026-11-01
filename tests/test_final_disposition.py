"""The final disposition of findings is read from each finding's round history, never from the union `confirmed`."""

import re

import pytest
from conftest import REPO
from test_deep_review import CLEAN, FINDING, FIX_PACKET, direct, workflow

SKILL = REPO / "skills/deep-review"
OUTCOMES = {"open", "fixed", "refuted", "unruled", "degraded"}
ONE = {"claude-bugs#1": {**CLEAN, "findings": [FINDING]}}
NIT = {**FINDING, "line_start": 10, "line_end": 11, "severity": "nit", "description": "Rename for clarity"}
OTHER = {**FINDING, "file": "src/b.py", "description": "Second reachable defect"}


def ruling(id, real, severity="warning", reason="ruled"):
    return {"id": id, "real": real, "severity": severity, "reason": reason, "domain": "logic"}


def only(result, outcome):
    """The one finding under `outcome`, after checking it is the only finding anywhere in the record."""
    assert set(result["disposition"]) == OUTCOMES
    assert sum(len(v) for v in result["disposition"].values()) == 1
    (finding,) = result["disposition"][outcome]
    assert finding["outcome"] == outcome
    return finding


def test_a_fixed_finding_leaves_the_open_list_but_stays_in_history():
    result = workflow({"fix": True}, {**ONE, "fix:bucket1/1#1": FIX_PACKET})["result"]
    assert result["rounds"] == 2 and not result["incomplete"]
    fixed = only(result, "fixed")
    assert fixed["rounds"] == [{"round": 1, "ruling": "confirmed", "severity": "warning", "reason": ""}]
    assert fixed["outcomeReason"] == "confirmed in round 1; fixed in round 1; not re-detected by claude-bugs in round 2"
    # History keeps the finding; the current state does not list it as open.
    assert len(result["confirmed"]) == 1 and result["currentFindings"] == []


def test_a_refuted_claim_is_recorded_with_its_reason_instead_of_vanishing():
    data = workflow(
        {"fix": True}, {**ONE, "boss:inherit#1": {"rulings": [ruling(0, False, reason="guarded at line 1")]}}
    )
    result = data["result"]
    assert result["rounds"] == 1 and not result["incomplete"] and not result["fixed"]
    refuted = only(result, "refuted")
    assert refuted["outcomeReason"] == "round 1: guarded at line 1"
    assert refuted["rounds"] == [
        {"round": 1, "ruling": "refuted", "severity": "warning", "reason": "guarded at line 1"}
    ]
    assert result["confirmed"] == [] and result["currentFindings"] == []
    assert any("1 refuted" in line for line in data["logs"])


def test_an_unruled_finding_is_neither_open_nor_fixed():
    result = workflow({"fix": True}, {**ONE, "boss:inherit#1": {"rulings": []}})["result"]
    assert result["incomplete"] and not result["fixed"]
    unruled = only(result, "unruled")
    assert unruled["outcomeReason"].startswith("round 1: UNRULED")
    assert unruled["rounds"][0]["ruling"] == "unruled"
    assert len(result["confirmed"]) == 1  # fail-closed history still lists it; the disposition does not call it open


def test_open_is_a_confirmed_finding_nobody_was_sent_to_fix():
    report_only = workflow({"fix": False}, ONE)["result"]
    assert only(report_only, "open")["outcomeReason"] == "confirmed in round 1; report-only run, nothing was fixed"

    # A nit beside a warning: the warning is fixed and cleared, the nit was never sent to a fixer, so it stays open.
    both = workflow(
        {"fix": True},
        {
            "claude-bugs#1": {**CLEAN, "findings": [FINDING, NIT]},
            "boss:inherit#1": {"rulings": [ruling(0, True), ruling(1, True, "nit")]},
        },
    )["result"]
    assert both["rounds"] == 2 and not both["incomplete"]
    assert [f["description"] for f in both["disposition"]["fixed"]] == [FINDING["description"]]
    (nit,) = both["disposition"]["open"]
    assert nit["severity"] == "nit" and nit["outcomeReason"] == "confirmed in round 1; nits are not sent to fixers"
    assert len(both["confirmed"]) == 2  # history holds both; only one is open now

    # Two bosses, one invalid: the finding the valid boss confirmed is open (the loop stopped before fixing),
    # which is a different state from unruled.
    stopped = workflow(
        {"fix": True, "boss": ["logic", "taste"]},
        {**ONE, "boss:logic#1": {"rulings": [ruling(0, True)]}, "boss:taste#1": {"rulings": []}},
    )["result"]
    assert stopped["incomplete"] and not stopped["fixed"]
    assert only(stopped, "open")["outcomeReason"].endswith("the loop stopped before a fixer was dispatched")


@pytest.mark.parametrize(
    "args, responses, phrase, incomplete",
    [
        ({"fix": True, "maxRounds": 1}, ONE, "the fix was not re-reviewed within the round limit", True),
        (
            {"fix": True},
            {**ONE, "fix:bucket1/1#1": None},
            "the fixer failed and its partial edits were not re-reviewed",
            True,
        ),
        (
            {"fix": True, "reviewers": {"grok": direct()}},
            {
                "grok#1": {**CLEAN, "findings": [FINDING]},
                "grok#2": {"findings": [], "degraded": True, "reason": "timeout"},
            },
            "grok did not complete the round 2 re-review that would clear it",
            False,
        ),
    ],
)
def test_an_unverified_fix_is_degraded_not_fixed_and_not_open(args, responses, phrase, incomplete):
    result = workflow(args, responses)["result"]
    assert result["fixed"] and result["incomplete"] is incomplete
    degraded = only(result, "degraded")
    assert phrase in degraded["outcomeReason"]
    assert degraded["rounds"][-1]["ruling"] == "confirmed"
    assert len(result["confirmed"]) == 1


def test_a_finding_re_detected_after_a_fix_starts_again_from_that_round():
    again = {**ONE, "claude-bugs#2": {**CLEAN, "findings": [FINDING]}}
    cleared = workflow({"fix": True, "maxRounds": 3}, again)["result"]
    assert cleared["rounds"] == 3 and not cleared["incomplete"]
    fixed = only(cleared, "fixed")
    assert [r["round"] for r in fixed["rounds"]] == [1, 2]
    assert fixed["outcomeReason"] == (
        "re-detected in round 2 after a fix; fixed in round 2; not re-detected by claude-bugs in round 3"
    )
    assert len(cleared["confirmed"]) == 1  # the union does not double-count a re-detection
    capped = workflow({"fix": True, "maxRounds": 2}, again)["result"]
    assert only(capped, "degraded")["outcomeReason"].startswith(
        "re-detected in round 2 after a fix; fixer dispatched in round 2"
    )


def test_each_finding_appears_once_and_the_union_is_not_the_current_state():
    data = workflow(
        {"fix": True},
        {
            "claude-bugs#1": {**CLEAN, "findings": [FINDING, OTHER]},
            "boss:inherit#1": {"rulings": [ruling(0, True), ruling(1, False, reason="unreachable branch")]},
            "fix:bucket1/1#1": FIX_PACKET,
        },
    )
    result = data["result"]
    assert result["rounds"] == 2 and not result["incomplete"]
    placed = {o: [f["file"] for f in fs] for o, fs in result["disposition"].items()}
    assert placed == {"fixed": ["src/a.py"], "refuted": ["src/b.py"], "open": [], "unruled": [], "degraded": []}
    assert [f["file"] for f in result["confirmed"]] == ["src/a.py"]
    assert result["currentFindings"] == []
    assert "Round 1: 1 new confirmed (1 actionable this round, 1 refuted, 1 total)" in data["logs"]
    for f in result["disposition"]["fixed"] + result["disposition"]["refuted"]:
        assert set(f) >= {
            "file",
            "line_start",
            "line_end",
            "severity",
            "description",
            "sources",
            "outcome",
            "outcomeReason",
            "rounds",
        }


def test_workflow_status_helper_and_references_agree_on_the_five_outcomes():
    source = (SKILL / "deep-review.workflow.js").read_text()
    literal = re.search(r"const disposition = \{ ([^}]*) \}", source).group(1)
    assert {k.strip().split(":")[0] for k in literal.split(",")} == OUTCOMES
    helper = (SKILL / "scripts/run_status.py").read_text()
    tuple_ = re.search(r"OUTCOMES = \(([^)]*)\)", helper).group(1)
    assert set(re.findall(r'"(\w+)"', tuple_)) == OUTCOMES
    reference = (SKILL / "references/disposition.md").read_text()
    for outcome in OUTCOMES:
        assert f"- `{outcome}`:" in reference, outcome
    assert "`confirmed`" in reference and "dedupKey" in reference and "run_status.py" in reference
    skill = (SKILL / "SKILL.md").read_text()
    assert "result.disposition" in skill and "references/disposition.md" in skill
    assert "Never fill any section from `confirmed`" in skill
    for heading in ("### Fixed in this run", "### Refuted", "### Unruled", "### Degraded", "### Open: Critical"):
        assert heading in skill, heading
    assert "references/disposition.md" in (SKILL / "references/run-lifecycle.md").read_text()
    assert "`disposition`" in (SKILL / "references/result-meaning.md").read_text()
    for path in (SKILL / "references/disposition.md", REPO / "tests/test_final_disposition.py"):
        assert not {"\u2014", "\u2013"} & set(path.read_text()), path
