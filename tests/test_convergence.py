"""Bounded convergence and durable patch capture. Every way the review loop ends is recorded explicitly in
`result.termination`; a finding that survives two fix attempts stops the loop instead of taking a third fixer;
a fixing run leaves the gate to the fresh read-only run of the resulting snapshot; and the host captures the
patch from the saved result before any worktree teardown. Offline, mocked Workflow runtime; never calls a model."""

import json
from pathlib import Path

import pytest
from conftest import REPO
from test_deep_review import CLEAN, FINDING, FIX_PACKET, direct, workflow
from test_final_disposition import only
from test_run_attempt import case as attempt_case  # noqa: F401  (a fixture; pytest registers it by this name)
from test_run_attempt import classify, write_result

SKILL = REPO / "skills/deep-review"
ONE = {"claude-bugs#1": {**CLEAN, "findings": [FINDING]}}
DISABLED = {"policy": "disabled", "available": False}
CANCELLED = "Terminal failure: request cancelled by the runtime"


def seen_in(rounds):
    """The bug lane re-detects the same finding in each listed round."""
    return {f"claude-bugs#{n}": {**CLEAN, "findings": [FINDING]} for n in rounds}


def fix_labels(data):
    return [c["opts"]["label"] for c in data["calls"] if c["opts"]["phase"] == "Fix"]


@pytest.mark.parametrize(
    "args, responses, state, round, reason, incomplete",
    [
        ({}, {}, "converged", 1, "round 1: nothing actionable (0 confirmed nit(s), 0 refuted)", False),
        ({"fix": True}, ONE, "converged", 2, "round 2: nothing actionable (0 confirmed nit(s), 0 refuted)", False),
        ({"fix": False}, ONE, "report-only", 1, "round 1: 1 actionable finding(s) reported; fixing is off", False),
        (
            {"fix": True, "maxRounds": 1},
            ONE,
            "round-cap",
            1,
            "round 1: the round cap of 1 was reached after a fix stage; the fixed tree was not re-reviewed",
            True,
        ),
        (
            {"fix": True},
            {**ONE, "fix:bucket1/1#1": None},
            "fixer-failed",
            1,
            "round 1: fixer failed or returned no packet",
            True,
        ),
        (
            {"fix": True},
            {**ONE, "boss:inherit#1": {"rulings": []}},
            "judge-failed",
            1,
            "round 1: the round was not ruled; nothing is fixed or cleared on unruled findings",
            True,
        ),
        (
            {"reviewers": {"astra-standard": direct("astra-standard")}, "limits": {"concurrency": 1}},
            {"astra-standard#1": "__throw__:request cancelled by the runtime"},
            "stopped",
            1,
            CANCELLED,
            True,
        ),
        (
            {},
            {"claude-bugs#1": {"findings": [], "degraded": True, "reason": "timeout"}},
            "stopped",
            1,
            "Required reviewer claude-bugs failed",
            True,
        ),
        (
            {"reviewers": {"grok": direct(policy="required", available=False)}},
            {},
            "review-failed",
            0,
            "grok: degraded: fixture",
            True,
        ),
        (
            {"reviewers": {"claude-bugs": DISABLED, "claude-conventions": DISABLED}},
            {},
            "no-reviewers",
            0,
            "no reviewer was available to dispatch",
            True,
        ),
    ],
)
def test_every_way_the_loop_ends_is_recorded_explicitly(args, responses, state, round, reason, incomplete):
    result = workflow(args, responses)["result"]
    assert result["termination"] == {"state": state, "round": round, "reason": reason}
    assert result["rounds"] == round and result["incomplete"] is incomplete
    if state == "stopped":
        assert result["admission"]["stopReason"] == reason
    else:
        assert result["admission"]["stopReason"] == ""
    assert result["admission"]["barriers"][-1] == {
        "name": "conclude",
        "dispatched": result["admission"]["started"],
        "open": 0,
    }


def test_a_finding_that_survives_two_fix_attempts_ends_the_loop_as_a_repeated_failure():
    gate = {"commands": [["true"]], "timeoutSeconds": 5}  # registered, so only the fixes can be why it is skipped
    data = workflow({"fix": True, "maxRounds": 5, "gate": gate}, seen_in((1, 2, 3)))
    result = data["result"]
    assert result["rounds"] == 3 and result["admission"]["maxRounds"] == 5
    assert result["termination"] == {
        "state": "repeated-failure",
        "round": 3,
        "reason": "round 3: 1 finding(s) re-detected after 2 fix attempts (src/a.py:2-3); no further fixer is dispatched",
    }
    # Two fresh fixers were asked; the third sighting dispatches none, whatever the cap allows.
    assert fix_labels(data) == ["fix:bucket1/1#1", "fix:bucket1/1#2"]
    assert len([c for c in data["calls"] if c["opts"]["label"].startswith("claude-bugs#")]) == 3
    still = only(result, "open")
    assert [r["round"] for r in still["rounds"]] == [1, 2, 3]
    assert still["outcomeReason"] == (
        "re-detected in round 3 after a fix; re-detected after 2 fix attempts;"
        " the loop stopped as a repeated failure instead of dispatching another fixer"
    )
    # Earlier rounds did edit the tree: the patch is kept, the gate belongs to the post-fix snapshot's own run,
    # and nothing about this ending is missing evidence, so the run is not incomplete.
    assert result["fixed"] and result["requiresPostFixReview"] and not result["incomplete"]
    assert result["gate"]["state"] == "skipped"
    assert (
        result["gate"]["reason"]
        == "fixes were applied; the gate belongs to the fresh read-only run of the resulting snapshot"
    )
    assert not any(c["opts"]["phase"] == "Conclude" for c in data["calls"])
    assert not any(d["key"] == "final-verification" for d in result["degraded"])
    assert all(b["open"] == 0 for b in result["admission"]["barriers"])
    assert result["admission"]["barriers"][-1]["name"] == "conclude"


def test_a_cleared_finding_converges_and_the_cap_still_ends_an_unreviewed_fix():
    cleared = workflow({"fix": True, "maxRounds": 5}, seen_in((1, 2)))
    assert cleared["result"]["termination"]["state"] == "converged" and cleared["result"]["rounds"] == 3
    assert fix_labels(cleared) == ["fix:bucket1/1#1", "fix:bucket1/1#2"]
    assert only(cleared["result"], "fixed")["rounds"][-1]["round"] == 2
    capped = workflow({"fix": True, "maxRounds": 2}, seen_in((1, 2)))["result"]
    assert capped["termination"]["state"] == "round-cap" and capped["termination"]["round"] == 2
    assert capped["incomplete"] and any(d["key"] == "final-verification" for d in capped["degraded"])


def test_a_rebuttal_is_not_a_fix_attempt():
    objection = {
        **FIX_PACKET,
        "files": [],
        "checks": [],
        "rebuttals": [
            {
                "id": "0",
                "reason": "the input is guarded one line earlier",
                "evidence": [{"kind": "code", "ref": "src/a.py:1", "note": "the guard"}],
            }
        ],
    }
    # Round 1 objects, round 2 confirms over the objection and fixes (attempt 1), round 3 fixes again (attempt 2),
    # round 4 still sees it: the fixer that left the code alone in round 1 did not use up an attempt.
    data = workflow({"fix": True, "maxRounds": 6}, {**seen_in((1, 2, 3, 4)), "fix:bucket1/1#1": objection})
    result = data["result"]
    assert fix_labels(data) == ["fix:bucket1/1#1", "fix:bucket1/1#2", "fix:bucket1/1#3"]
    assert result["termination"]["state"] == "repeated-failure" and result["termination"]["round"] == 4
    assert result["rounds"] == 4
    still = only(result, "open")
    assert still["rebuttal"]["round"] == 1 and [r["round"] for r in still["rounds"]] == [1, 2, 3, 4]


def test_an_escalation_is_not_a_fix_attempt_but_the_cause_fixer_is_one():
    escalation = {
        **FIX_PACKET,
        "files": [],
        "checks": [],
        "escalations": [
            {
                "id": "0",
                "file": "src/b.py",
                "reason": "the value src/a.py reads is produced unguarded in src/b.py",
                "evidence": [{"kind": "code", "ref": "src/b.py:1", "note": "the producer"}],
            }
        ],
    }
    # Round 1: the anchor's owner escalates (no attempt), the cause owner fixes (attempt 1); round 2 re-detects and
    # the cause owner fixes again (attempt 2); round 3 re-detects: repeated failure, no third cause fixer.
    data = workflow({"fix": True, "maxRounds": 6}, {**seen_in((1, 2, 3)), "fix:bucket1/1#1": escalation})
    result = data["result"]
    assert fix_labels(data) == ["fix:bucket1/1#1", "fix:cause1/1#1", "fix:bucket1/1#2"]
    assert result["termination"]["state"] == "repeated-failure" and result["termination"]["round"] == 3
    still = only(result, "open")
    assert still["escalation"]["file"] == "src/b.py" and [r["round"] for r in still["rounds"]] == [1, 2, 3]


def test_the_attempt_bound_is_per_finding_and_a_fresh_finding_still_gets_its_fixers():
    other = {**FINDING, "file": "src/c.py", "description": "Unchecked return value"}
    # The first finding comes back in rounds 1 to 3; the second appears only in round 3 alongside it.
    responses = {
        **seen_in((1, 2)),
        "claude-bugs#3": {**CLEAN, "findings": [FINDING, other]},
        "boss:inherit#3": {"rulings": [{"id": i, "real": True, "severity": "warning"} for i in (0, 1)]},
    }
    result = workflow({"fix": True, "maxRounds": 6}, responses)["result"]
    assert result["termination"]["state"] == "repeated-failure" and result["termination"]["round"] == 3
    assert result["termination"]["reason"].startswith(
        "round 3: 1 finding(s) re-detected after 2 fix attempts (src/a.py:2-3)"
    )
    placed = {k: [f["file"] for f in v] for k, v in result["disposition"].items()}
    assert placed == {"open": ["src/a.py", "src/c.py"], "fixed": [], "refuted": [], "unruled": [], "degraded": []}
    fresh = next(f for f in result["disposition"]["open"] if f["file"] == "src/c.py")
    assert fresh["outcomeReason"] == "confirmed in round 3; the loop stopped before a fixer was dispatched"


def test_the_host_captures_the_patch_from_the_saved_result_before_teardown():
    skill = (SKILL / "SKILL.md").read_text()
    teardown = skill[skill.index("## Phase 3.5") : skill.index("## Phase 3.6")]
    result_check = teardown.index('test -s "$RUN_DIR/result.json" ||')
    fixed_read = teardown.index(
        'FIXED=$(python3 -c \'import json,sys; print(json.load(open(sys.argv[1]))["fixed"])\' "$RUN_DIR/result.json")'
    )
    patch = teardown.index(
        '(set -C; git -C "$WT" diff --cached --binary --full-index "$BASE_TREE" > "$PATCH") || exit 1'
    )
    removal = teardown.index('git -C "$PROJ" worktree remove --force "$WT"')
    assert result_check < fixed_read < patch < removal
    assert '"$FIX" = "yes"' not in teardown and 'if [ "$FIXED" = "True" ]; then' in teardown
    assert "termination: {state, round, reason}" in skill and "**Ended:** <result.termination.state>" in skill
    for state in (
        "converged",
        "report-only",
        "repeated-failure",
        "round-cap",
        "fixer-failed",
        "stopped",
        "no-reviewers",
    ):
        assert f"`{state}`" in skill, state
    lifecycle = (SKILL / "references/run-lifecycle.md").read_text()
    assert "`termination` records why the loop ended" in lifecycle and "fix loop has two bounds." in lifecycle
    assert "Before any worktree teardown the saved result's `fixed` decides" in lifecycle
    assert "repeated\n  failure" in (SKILL / "references/disposition.md").read_text()
    for name in ("references/run-lifecycle.md", "references/disposition.md", "references/result-meaning.md"):
        text = (SKILL / name).read_text()
        assert "\u2014" not in text and "\u2013" not in text, name


def test_a_modified_result_without_its_patch_names_the_gap(attempt_case):  # noqa: F811  (the imported fixture)
    _, init, _, _ = attempt_case
    manifest = init()
    write_result(manifest, fixed=True, requiresPostFixReview=True)
    report = json.loads(classify(manifest).stdout)
    assert report["category"] == "modified" and not report["artifacts"]["patch"]
    assert report["reasons"] == [
        "no patch at patchPath; the fixes exist only in the worktree until Phase 3.5 captures them"
    ]
    Path(json.loads(manifest.read_text())["outputs"]["patchPath"]).write_bytes(b"")
    report = json.loads(classify(manifest).stdout)
    assert report["category"] == "modified" and report["artifacts"]["patch"] and report["reasons"] == []
