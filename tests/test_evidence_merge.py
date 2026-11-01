"""Disagreement between judges is resolved by evidence, never by count, and a fixer may rebut a confirmed finding
with evidence instead of fixing it, so a false claim is answered by the next round's judges rather than sent to a
fixer every round. Offline, mocked Workflow runtime; never calls a model."""

import json

import pytest
from conftest import REPO
from test_deep_review import CLEAN, FINDING, FIX_PACKET, workflow
from test_judging_panel import CANDIDATE, PANEL, entry, judged, panel, unanimous

SKILL = REPO / "skills/deep-review"
ONE = {"claude-bugs#1": {**CLEAN, "findings": [FINDING]}}
EVIDENCE = [{"kind": "code", "ref": "src/a.py:1", "note": "returns before the read when the input is empty"}]
REASON = "the guard at line 1 returns before the read"
REBUTTAL = {"round": 1, "reason": REASON, "evidence": EVIDENCE}


def rebutting(id, reason=REASON, evidence=EVIDENCE):
    """A fix packet that changed nothing and rebuts the one finding instead."""
    return {**FIX_PACKET, "files": [], "checks": [], "rebuttals": [{"id": id, "reason": reason, "evidence": evidence}]}


def by_judge(words, candidates=(CANDIDATE,), round=1):
    return {f"{name}#{round}": judged(candidates, word, round) for name, word in zip(PANEL, words, strict=True)}


def judge_calls(data, round):
    return [c for c in data["calls"] if c["opts"]["phase"] == "Judge" and c["opts"]["label"].endswith(f"#{round}")]


def fix_calls(data, round):
    return [c for c in data["calls"] if c["opts"]["phase"] == "Fix" and c["opts"]["label"].endswith(f"#{round}")]


# ---- the merge ------------------------------------------------------------------


@pytest.mark.parametrize(
    "words",
    [
        ("confirmed", "confirmed", "unresolved"),
        ("confirmed", "unresolved", "unreviewed"),
        ("unreviewed", "confirmed", "unreviewed"),
    ],
)
def test_a_demonstration_beside_judges_that_could_not_decide_confirms_and_is_fixed(words):
    """Unresolved and unreviewed entries carry no verdict: one judge that traced the defect rules it, the finding goes
    to a fixer, and the merged reason records what every judge said, with an unreviewed judge's limit in place of a
    reason it did not give."""
    data = workflow({"judges": panel(), "fix": True}, {**ONE, **by_judge(words), "fix:bucket1/1#1": FIX_PACKET})
    result = data["result"]
    assert result["rounds"] == 2 and not result["incomplete"] and result["fixed"]
    (fixed,) = result["disposition"]["fixed"]
    assert fixed["dispositions"] == dict(zip(PANEL, words, strict=True))
    said = {
        "confirmed": "confirmed by fixture",
        "unresolved": "unresolved by fixture",
        "unreviewed": "budget exhausted before this candidate",
    }
    assert fixed["rounds"][0]["reason"] == "; ".join(f"{n}: {said[w]}" for n, w in zip(PANEL, words, strict=True))
    assert not any(d["key"].startswith("judge:") for d in result["degraded"])


@pytest.mark.parametrize("words", [("refuted", "refuted", "unresolved"), ("unreviewed", "refuted", "unreviewed")])
def test_a_refutation_beside_judges_that_could_not_decide_refutes(words):
    """A claim nobody demonstrated is not confirmed by default: one refutation with evidence ends it, with cause."""
    data = workflow({"judges": panel(), "fix": True}, {**ONE, **by_judge(words)})
    result = data["result"]
    assert result["rounds"] == 1 and not result["incomplete"] and not result["fixed"]
    assert not fix_calls(data, 1)
    (refuted,) = result["disposition"]["refuted"]
    assert refuted["dispositions"] == dict(zip(PANEL, words, strict=True))
    assert refuted["outcomeReason"].startswith("round 1: judge-fable: ")
    assert result["confirmed"] == [] and result["currentFindings"] == []


@pytest.mark.parametrize(
    "words",
    [
        ("confirmed", "refuted", "refuted"),
        ("refuted", "confirmed", "confirmed"),
        ("confirmed", "refuted", "unresolved"),
    ],
)
def test_a_demonstrated_defect_is_not_outvoted_and_a_refutation_is_not_overruled_by_count(words):
    """A confirmation beside a refutation is conflicting evidence: whatever the count, the candidate is neither
    refuted nor fixed. It stays open with both sides on record, the loop stops and the run is incomplete."""
    data = workflow({"judges": panel(), "fix": True}, {**ONE, **by_judge(words)})
    result = data["result"]
    assert result["incomplete"] and not result["fixed"] and result["rounds"] == 1
    assert not fix_calls(data, 1)
    assert result["disposition"]["refuted"] == [] and result["disposition"]["fixed"] == []
    (unruled,) = result["disposition"]["unruled"]
    assert unruled["dispositions"] == dict(zip(PANEL, words, strict=True))
    assert unruled["outcomeReason"].startswith("round 1: UNRULED: evidence conflicts (")
    assert "a demonstrated defect is not outvoted" in unruled["outcomeReason"]
    for name in PANEL:
        assert f"{name}: " in unruled["outcomeReason"], "both sides' reasons are in the record"
    assert len(result["confirmed"]) == 1  # fail-closed history: an unruled finding is never cleared
    assert {d["key"] for d in result["degraded"]} == {"judge:panel"}


def test_a_demonstrated_blocker_keeps_its_severity_over_lower_ratings():
    """One judge traced a critical, one confirmed it as a nit, one could not decide: it is fixed as a critical."""
    responses = {
        **ONE,
        "judge-fable#1": {**judged(), "findings": [entry(CANDIDATE, "confirmed", "critical", "traced the crash")]},
        "judge-astra#1": {**judged(), "findings": [entry(CANDIDATE, "confirmed", "nit", "cosmetic")]},
        "judge-grok#1": judged(disposition="unresolved"),
        "fix:bucket1/1#1": FIX_PACKET,
    }
    result = workflow({"judges": panel(), "fix": True}, responses)["result"]
    (fixed,) = result["disposition"]["fixed"]
    assert fixed["severity"] == "critical" and fixed["rounds"][0]["severity"] == "critical"
    assert not result["incomplete"]


def test_no_verdict_at_all_is_unruled_not_refuted():
    data = workflow({"judges": panel(), "fix": True}, {**ONE, **by_judge(("unresolved", "unreviewed", "unreviewed"))})
    result = data["result"]
    assert result["incomplete"] and not fix_calls(data, 1)
    (unruled,) = result["disposition"]["unruled"]
    assert unruled["outcomeReason"].startswith("round 1: UNRULED: no judge demonstrated or refuted it (")
    assert result["disposition"]["refuted"] == []


# ---- rebuttals ------------------------------------------------------------------


def test_a_rebutted_finding_is_judged_again_with_the_objection_and_a_false_claim_dies_in_one_round():
    """Round 1 confirms a false claim; the fixer rebuts it with evidence instead of fixing it. Round 2 carries the
    objection to fresh judges even though no reviewer re-detected the finding; they uphold it, the claim is refuted
    with cause, and no second fixer was sent."""
    data = workflow(
        {"judges": panel(), "fix": True},
        {
            **ONE,
            **unanimous("confirmed"),
            "fix:bucket1/1#1": rebutting(CANDIDATE["id"]),
            "claude-bugs#2": CLEAN,
            **unanimous("refuted", round=2),
        },
    )
    result = data["result"]
    assert result["rounds"] == 2 and not result["incomplete"]
    # The round 1 fixer was told the id and how to rebut; its packet is on record.
    (fixer,) = fix_calls(data, 1)
    assert f'"id": "{CANDIDATE["id"]}"' in fixer["prompt"] and '"rebuttals"' in fixer["prompt"]
    assert "priorRebuttal" in fixer["prompt"] and "a second rebuttal is refused" in fixer["prompt"]
    assert result["fixResults"][0]["result"]["rebuttals"] == rebutting(CANDIDATE["id"])["rebuttals"]
    assert not fix_calls(data, 2)
    # Round 2's brief carries the same candidate, same id, with the rebuttal and the clause; round 1's did not.
    assert all('"rebuttal"' not in c["prompt"] for c in judge_calls(data, 1))
    for call in judge_calls(data, 2):
        assert json.dumps([{**CANDIDATE, "rebuttal": REBUTTAL}], indent=2) in call["prompt"]
        assert "objection is the implementer's claim, not a ruling" in call["prompt"]
    assert result["judgeResults"][1]["candidates"] == [{**CANDIDATE, "rebuttal": REBUTTAL}]
    (refuted,) = result["disposition"]["refuted"]
    assert refuted["rebuttal"] == REBUTTAL
    assert refuted["outcomeReason"].startswith(
        "round 2, upholding the fixer's rebuttal: judge-fable: refuted by fixture"
    )
    assert [r["ruling"] for r in refuted["rounds"]] == ["confirmed", "refuted"]
    assert result["currentFindings"] == []


def test_a_re_detected_rebutted_finding_carries_the_objection_once():
    data = workflow(
        {"judges": panel(), "fix": True},
        {
            **ONE,
            **unanimous("confirmed"),
            "fix:bucket1/1#1": rebutting(CANDIDATE["id"]),
            "claude-bugs#2": {**CLEAN, "findings": [FINDING]},
            **unanimous("refuted", round=2),
        },
    )["result"]
    assert data["judgeResults"][1]["candidates"] == [{**CANDIDATE, "rebuttal": REBUTTAL}]
    assert len(data["disposition"]["refuted"]) == 1 and sum(len(v) for v in data["disposition"].values()) == 1


def test_a_finding_confirmed_over_a_rebuttal_returns_to_a_fixer_that_sees_both_sides():
    """The judges answer the objection and confirm anyway: the round 2 fixer receives the prior rebuttal and the
    ruling, fixes it, and round 3 clears it. The record says the finding was confirmed over the rebuttal."""
    data = workflow(
        {"judges": panel(), "fix": True, "maxRounds": 3},
        {
            **ONE,
            **unanimous("confirmed"),
            "fix:bucket1/1#1": rebutting(CANDIDATE["id"]),
            "claude-bugs#2": CLEAN,
            **{f"{n}#2": judged(round=2, reason="the guard is dead code; the read is reached") for n in PANEL},
            "fix:bucket1/1#2": FIX_PACKET,
            "claude-bugs#3": CLEAN,
        },
    )
    result = data["result"]
    assert result["rounds"] == 3 and not result["incomplete"]
    (fixer,) = fix_calls(data, 2)
    assert '"priorRebuttal": {' in fixer["prompt"] and f'"reason": "{REASON}"' in fixer["prompt"]
    assert '"ruling": "judge-fable: the guard is dead code; the read is reached; judge-astra:' in fixer["prompt"]
    (fixed,) = result["disposition"]["fixed"]
    assert fixed["rebuttal"] == REBUTTAL
    assert fixed["outcomeReason"] == (
        "confirmed over the fixer's rebuttal in round 2; fixed in round 2; not re-detected by claude-bugs in round 3"
    )


def test_a_second_rebuttal_of_the_same_finding_is_refused():
    data = workflow(
        {"judges": panel(), "fix": True, "maxRounds": 3},
        {
            **ONE,
            **unanimous("confirmed"),
            "fix:bucket1/1#1": rebutting(CANDIDATE["id"]),
            "claude-bugs#2": CLEAN,
            **unanimous("confirmed", round=2),
            "fix:bucket1/1#2": rebutting(CANDIDATE["id"], reason="still false"),
        },
    )
    result = data["result"]
    assert result["incomplete"] and result["rounds"] == 2
    degraded = next(d for d in result["degraded"] if d["key"] == "fix#2")
    assert (
        f"finding {CANDIDATE['id']} was rebutted in round 1 and confirmed over that objection; a second rebuttal is refused"
        in degraded["reason"]
    )
    assert result["fixResults"][1]["error"].startswith(f"finding {CANDIDATE['id']} was rebutted in round 1")


def test_a_second_rebuttal_rounds_later_is_refused_and_the_first_stays_on_record():
    """Round 1 rebuts; round 2 confirms over the objection and its fixer leaves the finding partial; round 3
    re-detects it and judges it on its own merits, and its fixer tries to rebut again. The refusal reads the run's
    record by identity, not the previous round's candidate, so the round 3 fixer is told both sides, its second
    objection is refused, and the record keeps the round 1 rebuttal and the round 2 ruling."""
    data = workflow(
        {"judges": panel(), "fix": True, "maxRounds": 4},
        {
            **ONE,
            **unanimous("confirmed"),
            "fix:bucket1/1#1": rebutting(CANDIDATE["id"]),
            "claude-bugs#2": CLEAN,
            **{f"{n}#2": judged(round=2, reason="the guard is dead code; the read is reached") for n in PANEL},
            "fix:bucket1/1#2": {**FIX_PACKET, "disposition": "partial", "unresolved": ["could not reach it"]},
            "claude-bugs#3": {**CLEAN, "findings": [FINDING]},
            **unanimous("confirmed", round=3),
            "fix:bucket1/1#3": rebutting(CANDIDATE["id"], reason="second objection"),
        },
    )
    result = data["result"]
    assert result["incomplete"] and result["rounds"] == 3
    assert all('"rebuttal"' not in c["prompt"] for c in judge_calls(data, 3)), "round 3 judges the claim on its merits"
    (fixer,) = fix_calls(data, 3)
    assert '"priorRebuttal": {' in fixer["prompt"] and f'"reason": "{REASON}"' in fixer["prompt"]
    assert '"ruling": "judge-fable: the guard is dead code; the read is reached; judge-astra:' in fixer["prompt"]
    assert result["fixResults"][2]["error"].startswith(f"finding {CANDIDATE['id']} was rebutted in round 1")
    assert all(not r["error"] for r in result["fixResults"][:2])
    (degraded,) = result["disposition"]["degraded"]
    assert degraded["rebuttal"] == REBUTTAL, "the first rebuttal is the one on record"
    assert [r["ruling"] for r in degraded["rounds"]] == ["confirmed", "confirmed", "confirmed"]
    assert degraded["rounds"][1]["reason"].startswith("judge-fable: the guard is dead code")
    assert degraded["outcomeReason"].startswith("re-detected in round 3 after a fix; fixer dispatched in round 3; ")


def test_a_rebuttal_the_run_ends_on_is_degraded_not_fixed():
    result = workflow(
        {"judges": panel(), "fix": True, "maxRounds": 1},
        {**ONE, **unanimous("confirmed"), "fix:bucket1/1#1": rebutting(CANDIDATE["id"])},
    )["result"]
    assert result["incomplete"]
    (degraded,) = result["disposition"]["degraded"]
    assert degraded["rebuttal"] == REBUTTAL
    assert degraded["outcomeReason"] == (
        f"confirmed in round 1; rebutted by the fixer in round 1 ({REASON}); "
        "the run ended before a round ruled on the objection"
    )


def test_the_boss_mode_fixer_rebuts_by_positional_id_and_the_boss_rules_on_it():
    data = workflow(
        {"fix": True},
        {
            **ONE,
            "fix:bucket1/1#1": rebutting("0"),
            "claude-bugs#2": CLEAN,
            "boss:inherit#2": {"rulings": [{"id": 0, "real": False, "severity": "warning", "reason": "guard holds"}]},
        },
    )
    result = data["result"]
    assert result["rounds"] == 2 and not result["incomplete"]
    (fixer,) = fix_calls(data, 1)
    assert '"id": "0"' in fixer["prompt"]
    (boss1,) = judge_calls(data, 1)
    (boss2,) = judge_calls(data, 2)
    assert '"rebuttal"' not in boss1["prompt"]
    assert json.dumps([{**CANDIDATE, "id": 0, "rebuttal": REBUTTAL}], indent=2) in boss2["prompt"]
    assert "objection is the implementer's claim, not a ruling" in boss2["prompt"]
    (refuted,) = result["disposition"]["refuted"]
    assert refuted["outcomeReason"] == "round 2, upholding the fixer's rebuttal: guard holds"
    assert refuted["rebuttal"] == REBUTTAL


@pytest.mark.parametrize(
    "packet, reason",
    [
        ({k: v for k, v in FIX_PACKET.items() if k != "rebuttals"}, "invalid fix packet"),
        ({**FIX_PACKET, "rebuttals": [{"id": "0", "reason": REASON}]}, "invalid fix packet"),
        (rebutting("0", reason=" "), "invalid fix packet"),
        (rebutting("0", evidence=[{"kind": "guess", "ref": "x", "note": ""}]), "invalid fix packet"),
        (rebutting("0", evidence=[{"kind": "code", "ref": "src/a.py:1"}]), "invalid fix packet"),
        (rebutting("7"), "rebuttal names 7, which is not a finding in this bucket"),
        (
            rebutting("0", evidence=[]),
            "rebuttal of 0 cites no evidence; an objection without evidence goes under unresolved",
        ),
        (
            rebutting("0", evidence=[{"kind": "code", "ref": "src/a.py", "note": ""}]),
            'rebuttal of 0 cites code "src/a.py"',
        ),
        (rebutting("0", evidence=[{"kind": "code", "ref": "/abs/a.py:1", "note": ""}]), 'cites code "/abs/a.py:1"'),
        (
            {**FIX_PACKET, "rebuttals": rebutting("0")["rebuttals"] * 2},
            "finding 0 is rebutted twice; one rebuttal per finding",
        ),
    ],
)
def test_an_unbacked_or_misaddressed_rebuttal_is_a_failed_fixer(packet, reason):
    result = workflow({"fix": True}, {**ONE, "fix:bucket1/1#1": packet})["result"]
    assert result["incomplete"] and result["rounds"] == 1
    degraded = next(d for d in result["degraded"] if d["key"] == "fix#1")
    assert reason in degraded["reason"], degraded
    assert reason in result["fixResults"][0]["error"]


# ---- the documents --------------------------------------------------------------


def test_contract_schema_and_definitions_describe_the_merge_and_the_rebuttal():
    contract = (SKILL / "references/judge-contract.md").read_text()
    assert "## Merging and rebuttals" in contract and "by count" in contract and "not outvoted" in contract
    assert "all three results" not in contract and "`rebuttal`" in contract
    judging = (SKILL / "references/judging.md").read_text()
    assert "unanimous" not in judging and "never by count" in judging
    disposition = (SKILL / "references/disposition.md").read_text()
    assert "## Rebuttals" in disposition and "unanimous" not in disposition
    skill = (SKILL / "SKILL.md").read_text()
    assert "unanimous" not in skill and "Merging and rebuttals" in skill and "rebuts it" in skill
    fixer = (REPO / "agents/fixer.md").read_text()
    assert "`rebuttals`" in fixer and "`priorRebuttal`" in fixer and "Verify each finding" in fixer
    schema = json.loads((SKILL / "fix-output.schema.json").read_text())
    assert "rebuttals" in schema["required"]
    rebuttal = schema["properties"]["rebuttals"]["items"]
    assert rebuttal["required"] == ["id", "reason", "evidence"] and rebuttal["properties"]["evidence"]["minItems"] == 1
    source = (SKILL / "deep-review.workflow.js").read_text()
    assert "unanimous" not in source and "const rebuttals = new Map()" in source
    for path in (
        SKILL / "references/judge-contract.md",
        SKILL / "references/judging.md",
        SKILL / "references/disposition.md",
        REPO / "agents/fixer.md",
        REPO / "tests/test_evidence_merge.py",
    ):
        assert not {"\u2014", "\u2013"} & set(path.read_text()), path
