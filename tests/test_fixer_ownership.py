"""Fixer ownership stays disjoint and bounded: each fresh fixer owns one file bucket, returns one typed packet and
is gone. A finding whose cause is in a file the fixer does not own is escalated in the packet, not edited, and the
repair is routed to one fresh fixer owning that file, never to every file the evidence cites and without rerunning
anything else. Offline, mocked Workflow runtime; never calls a model."""

import json

import pytest
from conftest import REPO
from test_deep_review import CLEAN, FIX_PACKET, workflow

SKILL = REPO / "skills/deep-review"
# The finding shows in a test; the defect it demonstrates is in the source file the test exercises.
TEST_FINDING = {
    "file": "tests/test_a.py",
    "line_start": 3,
    "line_end": 4,
    "severity": "warning",
    "description": "test_empty_input asserts the guard but src/a.py returns the unguarded read",
}
EVIDENCE = [
    {"kind": "code", "ref": "tests/test_a.py:3", "note": "the assertion that fails"},
    {"kind": "code", "ref": "src/a.py:2", "note": "the read runs before the guard"},
    {"kind": "document", "ref": "docs/a.md", "note": "documents the guard the test expects"},
]
REASON = "the test is right; the read in src/a.py runs before the guard"


def escalating(id, file="src/a.py", reason=REASON, evidence=EVIDENCE, **rest):
    """A fix packet that changed nothing and escalates the one finding to the owner of `file`."""
    return {
        **FIX_PACKET,
        "files": [],
        "checks": [],
        "escalations": [{"id": id, "file": file, "reason": reason, "evidence": evidence}],
        **rest,
    }


def rulings(n):
    return {"rulings": [{"id": i, "real": True, "severity": "warning"} for i in range(n)]}


def fix_calls(data, round):
    return [c for c in data["calls"] if c["opts"]["phase"] == "Fix" and c["opts"]["label"].endswith(f"#{round}")]


def review_calls(data, round):
    return [c for c in data["calls"] if c["opts"]["phase"] == "Review" and c["opts"]["label"].endswith(f"#{round}")]


def brief(call):
    """The file list a fixer was given: the second paragraph of its prompt, after the identity line."""
    return json.loads(call["prompt"].split("\n\n")[2])


ONE = {"claude-bugs#1": {**CLEAN, "findings": [TEST_FINDING]}}


def test_test_evidence_naming_a_source_defect_repairs_the_source_owner_without_rerunning_anything():
    """The fixer owning the test file escalates to src/a.py. One fresh fixer owning only src/a.py is dispatched in
    the same round; the test and the document the evidence cites are not routed, no reviewer or bucket reruns, and
    the next round clears the finding as fixed with the route on record."""
    cause = {**FIX_PACKET, "summary": "Moved the guard before the read in src/a.py"}
    data = workflow({"fix": True}, {**ONE, "fix:bucket1/1#1": escalating("0"), "fix:cause1/1#1": cause})
    result = data["result"]
    assert result["rounds"] == 2 and not result["incomplete"] and result["fixed"]
    first, second = fix_calls(data, 1)
    assert [c["opts"]["label"] for c in (first, second)] == ["fix:bucket1/1#1", "fix:cause1/1#1"]
    assert [f["file"] for f in brief(first)] == ["tests/test_a.py"]
    assert "escalatedFrom" not in brief(first)[0]["findings"][0]
    (owned,) = brief(second)
    assert owned["file"] == "src/a.py", "the repair goes to the cause file only"
    (routed,) = owned["findings"]
    assert routed["id"] == "0" and routed["line_start"] == 3 and routed["line_end"] == 4
    assert routed["escalatedFrom"] == {"file": "tests/test_a.py", "round": 1, "reason": REASON, "evidence": EVIDENCE}
    assert '"escalations"' in second["prompt"] and "cannot be escalated again" in second["prompt"]
    assert second["opts"]["agentType"] == "fixer" and set(second["opts"]) == {"label", "phase", "schema", "agentType"}
    assert len(review_calls(data, 1)) == 2, "the review lanes of round 1 are not rerun for the escalation"
    assert len(data["calls"]) == 2 + 1 + 2 + 2, (
        "round 1: two reviewers, the boss, two fixers; round 2: two reviewers and nothing to judge"
    )
    bucket, cause_record = result["fixResults"]
    assert bucket == {"round": 1, "bucket": 1, "owned": ["tests/test_a.py"], "result": escalating("0"), "error": ""}
    assert cause_record == {
        "round": 1,
        "bucket": 1,
        "cause": True,
        "owned": ["src/a.py"],
        "routed": [{"id": "0", "from": "tests/test_a.py"}],
        "result": cause,
        "error": "",
    }
    names = [b["name"] for b in result["admission"]["barriers"]]
    assert names == ["review#1", "judge#1", "fix#1", "fix:cause#1", "review#2", "judge#2", "conclude"]
    assert all(b["open"] == 0 for b in result["admission"]["barriers"])
    (fixed,) = result["disposition"]["fixed"]
    assert fixed["escalation"] == {
        "round": 1,
        "from": "tests/test_a.py",
        "file": "src/a.py",
        "reason": REASON,
        "evidence": EVIDENCE,
    }
    assert (
        "fixed in round 1 (routed to src/a.py, named as the cause by the fixer owning tests/test_a.py)"
        in fixed["outcomeReason"]
    )
    assert result["disposition"]["open"] == [] and result["disposition"]["degraded"] == []


def test_the_cause_owner_is_a_fresh_fixer_after_the_bucket_that_owned_the_file_returned():
    """Six files make two buckets (round-robin: bucket 1 owns the test, src/o2.py and src/o4.py). Bucket 1
    escalates its test finding to src/o1.py, a file bucket 2 owned in the first stage. The bucket 2 fixer has
    returned and terminated, so one fresh fixer owning src/o1.py gets the routed finding; bucket 2 is not rerun
    and the other five files are untouched."""
    others = [{**TEST_FINDING, "file": f"src/o{i}.py", "description": f"defect {i}"} for i in range(1, 6)]
    findings = [TEST_FINDING, *others]
    packet = lambda files: {**FIX_PACKET, "files": files}  # noqa: E731
    data = workflow(
        {"fix": True},
        {
            "claude-bugs#1": {**CLEAN, "findings": findings},
            "boss:inherit#1": rulings(6),
            "fix:bucket1/2#1": {**escalating("0", file="src/o1.py"), "files": ["src/o1.py"], "disposition": "failed"},
            "fix:bucket2/2#1": packet(["src/o1.py"]),
        },
    )
    # The first packet above edits a file outside its bucket: refused, and no cause stage runs after a failed stage.
    (degraded,) = [d for d in data["result"]["degraded"] if d["key"] == "fix#1"]
    assert "outside its bucket: src/o1.py" in degraded["reason"] and len(fix_calls(data, 1)) == 2

    data = workflow(
        {"fix": True},
        {
            "claude-bugs#1": {**CLEAN, "findings": findings},
            "boss:inherit#1": rulings(6),
            "fix:bucket1/2#1": {**escalating("0", file="src/o1.py"), "files": ["src/o2.py", "src/o4.py"]},
            "fix:bucket2/2#1": packet(["src/o1.py", "src/o3.py"]),
            "fix:cause1/1#1": packet(["src/o1.py"]),
        },
    )
    result = data["result"]
    one, two, cause = fix_calls(data, 1)
    assert sorted(f["file"] for f in brief(one)) == ["src/o2.py", "src/o4.py", "tests/test_a.py"]
    assert sorted(f["file"] for f in brief(two)) == ["src/o1.py", "src/o3.py", "src/o5.py"]
    assert [f["file"] for f in brief(cause)] == ["src/o1.py"]
    assert cause["opts"]["label"] == "fix:cause1/1#1"
    assert data["calls"].index(cause) > max(data["calls"].index(one), data["calls"].index(two))
    assert len(review_calls(data, 1)) == 2 and len(fix_calls(data, 1)) == 3
    assert result["fixResults"][2]["routed"] == [{"id": "0", "from": "tests/test_a.py"}]
    assert result["rounds"] == 2 and not result["incomplete"]
    assert len(result["disposition"]["fixed"]) == 6


def test_a_re_detected_escalated_finding_goes_straight_to_the_cause_owner_and_cannot_be_escalated_again():
    """Round 1 routes the finding to src/a.py and the cause fixer leaves it partial. Round 2 re-detects it at the
    test: it is grouped under src/a.py in the first stage, no cause stage runs, and the fixer owning src/a.py sees
    the escalation. A fixer that escalates it again is refused and the loop stops with the reason on record."""
    partial = {**FIX_PACKET, "disposition": "partial", "unresolved": ["0: needs a design decision"]}
    responses = {
        "claude-bugs#1": {**CLEAN, "findings": [TEST_FINDING]},
        "claude-bugs#2": {**CLEAN, "findings": [TEST_FINDING]},
        "fix:bucket1/1#1": escalating("0"),
        "fix:cause1/1#1": partial,
    }
    data = workflow({"fix": True, "maxRounds": 3}, {**responses, "fix:bucket1/1#2": FIX_PACKET})
    result = data["result"]
    (second,) = fix_calls(data, 2)
    assert second["opts"]["label"] == "fix:bucket1/1#2"
    (owned,) = brief(second)
    assert owned["file"] == "src/a.py" and owned["findings"][0]["escalatedFrom"]["round"] == 1
    assert result["rounds"] == 3 and not result["incomplete"]
    (fixed,) = result["disposition"]["fixed"]
    assert fixed["rounds"][0]["round"] == 1 and fixed["rounds"][1]["round"] == 2
    assert "re-detected in round 2 after a fix; fixed in round 2 (routed to src/a.py" in fixed["outcomeReason"]

    again = workflow({"fix": True, "maxRounds": 3}, {**responses, "fix:bucket1/1#2": escalating("0", file="src/b.py")})
    result = again["result"]
    assert result["rounds"] == 2 and result["incomplete"]
    assert len(fix_calls(again, 2)) == 1, "a refused escalation dispatches no cause fixer"
    (degraded,) = [d for d in result["degraded"] if d["key"] == "fix#2"]
    assert (
        "finding 0 was escalated in round 1 to src/a.py and routed to that file's owner; a second escalation is refused"
        in degraded["reason"]
    )
    (item,) = result["disposition"]["degraded"]
    assert item["escalation"]["file"] == "src/a.py", "the first escalation stays on record"


def test_a_cause_fixer_cannot_pass_the_finding_on():
    """The one extra stage is bounded: the fixer that owns the cause cannot escalate the finding to a third file."""
    data = workflow(
        {"fix": True}, {**ONE, "fix:bucket1/1#1": escalating("0"), "fix:cause1/1#1": escalating("0", file="src/c.py")}
    )
    result = data["result"]
    assert result["rounds"] == 1 and result["incomplete"] and len(fix_calls(data, 1)) == 2
    (degraded,) = [d for d in result["degraded"] if d["key"] == "fix#1"]
    assert "a second escalation is refused" in degraded["reason"]
    assert result["fixResults"][1]["cause"] and "second escalation" in result["fixResults"][1]["error"]


def test_two_findings_escalated_to_one_file_share_one_cause_fixer():
    other = {**TEST_FINDING, "file": "tests/test_b.py", "description": "test_b shows the same unguarded read"}
    bucket = {
        **FIX_PACKET,
        "files": [],
        "checks": [],
        "escalations": [
            {"id": "0", "file": "src/a.py", "reason": REASON, "evidence": EVIDENCE},
            {"id": "1", "file": "src/a.py", "reason": REASON, "evidence": EVIDENCE},
        ],
    }
    data = workflow(
        {"fix": True},
        {
            "claude-bugs#1": {**CLEAN, "findings": [TEST_FINDING, other]},
            "boss:inherit#1": rulings(2),
            "fix:bucket1/1#1": bucket,
            "fix:cause1/1#1": FIX_PACKET,
        },
    )
    one, cause = fix_calls(data, 1)
    assert sorted(f["file"] for f in brief(one)) == ["tests/test_a.py", "tests/test_b.py"]
    (owned,) = brief(cause)
    assert owned["file"] == "src/a.py" and [f["id"] for f in owned["findings"]] == ["0", "1"]
    assert sorted(r["from"] for r in data["result"]["fixResults"][1]["routed"]) == [
        "tests/test_a.py",
        "tests/test_b.py",
    ]
    assert len(data["result"]["disposition"]["fixed"]) == 2


@pytest.mark.parametrize(
    "bad, reason",
    [
        ({**FIX_PACKET, "escalations": "src/a.py"}, "invalid fix packet"),
        ({**escalating("0"), "escalations": [{"id": "0", "file": "src/a.py", "reason": REASON}]}, "invalid fix packet"),
        (escalating("0", reason=" "), "invalid fix packet"),
        (escalating("0", evidence=[{"kind": "code", "ref": "src/a.py:2"}]), "invalid fix packet"),
        (escalating("7"), "escalation names 7, which is not a finding in this bucket"),
        ({**escalating("0"), "escalations": escalating("0")["escalations"] * 2}, "finding 0 is escalated twice"),
        (
            {**escalating("0"), "rebuttals": [{"id": "0", "reason": "false", "evidence": EVIDENCE[:1]}]},
            "finding 0 is both rebutted and escalated",
        ),
        (escalating("0", file="/abs/src/a.py"), "the cause is one repo-relative file"),
        (escalating("0", file="src/../a.py"), "the cause is one repo-relative file"),
        (escalating("0", file="./tests/test_a.py"), "names tests/test_a.py, which this fixer owns"),
        (escalating("0", evidence=[]), "escalation of 0 cites no evidence"),
        (
            escalating("0", evidence=[{"kind": "code", "ref": "a.py:0", "note": ""}]),
            'escalation of 0 cites code "a.py:0"',
        ),
    ],
)
def test_an_escalation_that_breaks_the_rules_is_a_failed_fixer(bad, reason):
    """Scope expansion is escalated under the rules or not at all: a malformed, duplicated, contradictory,
    self-addressed or unevidenced escalation stops the loop with its cause recorded, and nothing is routed."""
    data = workflow({"fix": True}, {**ONE, "fix:bucket1/1#1": bad})
    result = data["result"]
    assert result["incomplete"] and result["rounds"] == 1 and len(fix_calls(data, 1)) == 1
    (record,) = result["fixResults"]
    assert reason in record["error"] and record["owned"] == ["tests/test_a.py"]
    assert not any(d["key"].startswith("fix:cause") for d in result["degraded"])
    assert reason in next(d for d in result["degraded"] if d["key"] == "fix#1")["reason"]


def test_a_completed_packet_may_account_for_a_finding_by_escalating_it():
    """An escalated finding is handed to its owner, not left: `completed` with an escalation is not a contradiction,
    while `completed` beside an unresolved line still is."""
    data = workflow({"fix": True}, {**ONE, "fix:bucket1/1#1": escalating("0", disposition="completed")})
    assert data["result"]["fixResults"][0]["error"] == ""
    bad = escalating("0", disposition="completed", unresolved=["0: still wrong"])
    assert "contradicts" in workflow({"fix": True}, {**ONE, "fix:bucket1/1#1": bad})["result"]["fixResults"][0]["error"]


def test_the_contract_is_written_where_fixers_and_leads_read_it():
    schema = json.loads((SKILL / "fix-output.schema.json").read_text())
    assert schema["required"][-2:] == ["rebuttals", "escalations"]
    escalation = schema["properties"]["escalations"]["items"]
    assert escalation["required"] == ["id", "file", "reason", "evidence"]
    assert escalation["additionalProperties"] is False and escalation["properties"]["evidence"]["minItems"] == 1
    fixer = (REPO / "agents/fixer.md").read_text()
    for phrase in (
        "`escalations`",
        "`escalatedFrom`",
        "cannot be escalated again",
        "not every",
        "returned and terminated",
    ):
        assert phrase in fixer, phrase
    disposition = " ".join((SKILL / "references/disposition.md").read_text().split())
    assert "## Escalations" in disposition and "once per finding per run" in disposition
    skill = " ".join((SKILL / "SKILL.md").read_text().split())
    assert "escalates it in its packet" in skill and "`escalations` route a finding to the owner" in skill
    assert "never the files the evidence cites" in skill and "returns one packet and terminates" in skill
    source = (SKILL / "deep-review.workflow.js").read_text()
    assert "const escalations = new Map()" in source and "'escalations']" in source
    for path in (REPO / "agents/fixer.md", SKILL / "references/disposition.md", REPO / "tests/test_fixer_ownership.py"):
        assert not set("\N{EM DASH}\N{EN DASH}") & set(path.read_text()), path
