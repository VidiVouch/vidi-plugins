"""Every judge of the --panel is required. A judge that returns nothing usable is reported as an incomplete panel
and replaced once by a fresh instance of the same definition on the identical brief; a judge still missing after
that leaves the round unruled and the run incomplete. Ruling with the two remaining judges is reduced coverage and
happens only under the authorization the request registered (`reducedPanel`), never as a silent two-of-three
fallback. Offline, mocked Workflow runtime; never calls a model."""

import importlib
import json

import pytest
from conftest import REPO
from test_deep_review import CLEAN, FINDING, FIX_PACKET, workflow
from test_judging_panel import PANEL, brief, judged, manifest_api, panel, prepare, unanimous
from test_run_gate import gate, measure, run  # noqa: F401  (the gate fixture and helpers run the real helper)

SKILL = REPO / "skills/deep-review"
ONE = {"claude-bugs#1": {**CLEAN, "findings": [FINDING]}}
AUTH = "user, this session, 2026-11-01: 'if grok drops out, the other two judges may rule this review'"
NO_RESULT = "judge failed or returned no result"
status_api = importlib.import_module("run_status")


def judge_calls(data):
    return [c for c in data["calls"] if c["opts"]["phase"] == "Judge"]


def dispatch(data, label):
    return next(d for d in data["result"]["admission"]["dispatches"] if d["label"] == label)


def degraded(result):
    return {d["key"]: d["reason"] for d in result["degraded"]}


def twice(name, first=None, again=None):
    """Both instances of one judge slot return nothing usable."""
    return {f"{name}#1": first, f"{name}#1:replacement": again}


# ---- one fresh replacement -----------------------------------------------------


@pytest.mark.parametrize(
    "bad, reason",
    [
        (None, NO_RESULT),
        ("__throw__:fixture error", NO_RESULT),
        ({"rulings": []}, "invalid judging result"),
        (judged(snapshot="b" * 64), "judged snapshot"),
        (judged(findings=[]), "candidates without a disposition"),
    ],
)
def test_a_failed_judge_is_replaced_once_by_a_fresh_instance_on_the_identical_brief(bad, reason):
    """The slot's first instance fails its check: the panel is logged incomplete, one fresh judge-astra is dispatched
    by definition on the very brief the others received, after all three first instances returned, and the panel is
    complete again: the merge runs on three results, the finding is fixed, and both instances stay on record."""
    good = judged(reason="astra reproduced the read with []")
    responses = {**ONE, **unanimous("confirmed"), "judge-astra#1": bad, "judge-astra#1:replacement": good}
    data = workflow({"judges": panel(), "fix": True}, {**responses, "fix:bucket1/1#1": FIX_PACKET})
    result = data["result"]
    judges = judge_calls(data)
    labels = [c["opts"]["label"] for c in judges]
    assert labels[:4] == [f"{name}#1" for name in PANEL] + ["judge-astra#1:replacement"], labels
    replacement = judges[3]
    assert replacement["opts"] == {
        "label": "judge-astra#1:replacement",
        "phase": "Judge",
        "schema": judges[1]["opts"]["schema"],
        "agentType": "vidi-skills:judge-astra",
    }
    assert brief(replacement["prompt"]) == brief(judges[0]["prompt"]) == brief(judges[1]["prompt"])
    # Dispatched only after every first instance returned: it is the fourth Judge call, not interleaved with them.
    assert data["calls"].index(replacement) > max(data["calls"].index(c) for c in judges[:3])
    assert any(
        line.startswith("Round 1: panel incomplete, judge-astra returned no usable result (")
        and reason in line
        and "dispatching a fresh instance of each on the same brief" in line
        for line in data["logs"]
    ), data["logs"]
    (record,) = [r for r in result["judgeResults"] if r["round"] == 1]
    assert record["replaced"] == {
        "judge-astra": {
            "result": bad if isinstance(bad, dict) else None,
            "error": record["replaced"]["judge-astra"]["error"],
        }
    }
    assert reason in record["replaced"]["judge-astra"]["error"]
    assert record["results"]["judge-astra"] == {"result": good, "error": ""}
    assert record["results"]["judge-fable"]["error"] == "" and record["results"]["judge-grok"]["error"] == ""
    assert dispatch(data, "judge-astra#1:replacement")["state"] == "returned"
    assert not result["incomplete"] and result["fixed"] and result["reducedPanel"] is None
    assert not any(key.startswith("judge:") for key in degraded(result)), degraded(result)
    (fixed,) = result["disposition"]["fixed"]
    assert fixed["dispositions"] == dict.fromkeys(PANEL, "confirmed")
    assert "judge-astra: astra reproduced the read with []" in fixed["rounds"][0]["reason"]


def test_two_failed_judges_are_replaced_in_one_step_and_fill_the_panel():
    responses = {
        **ONE,
        **unanimous("confirmed"),
        "judge-fable#1": None,
        "judge-grok#1": "__throw__:fixture error",
        "judge-fable#1:replacement": judged(),
        "judge-grok#1:replacement": judged(),
    }
    data = workflow({"judges": panel()}, responses)
    result = data["result"]
    labels = [c["opts"]["label"] for c in judge_calls(data)]
    assert labels == [f"{name}#1" for name in PANEL] + ["judge-fable#1:replacement", "judge-grok#1:replacement"]
    assert len(data["calls"]) == 7  # two reviewers, three judges, two replacements, report-only
    assert set(result["judgeResults"][0]["replaced"]) == {"judge-fable", "judge-grok"}
    assert all(result["judgeResults"][0]["results"][name]["error"] == "" for name in PANEL)
    assert not result["incomplete"] and result["degraded"] == []
    assert result["disposition"]["open"][0]["dispositions"] == dict.fromkeys(PANEL, "confirmed")


def test_a_replacement_the_admission_stop_cancels_is_recorded_and_not_tried_again():
    data = workflow({"judges": panel(), "limits": {"maxAgents": 4}}, {**ONE, **unanimous("confirmed")})
    result = data["result"]
    assert len(data["calls"]) == 4 and result["incomplete"]
    assert dispatch(data, "judge-grok#1")["state"] == "cancelled"
    assert dispatch(data, "judge-grok#1:replacement")["state"] == "cancelled"
    assert [d["label"] for d in result["admission"]["dispatches"] if d["label"].startswith("judge-grok")] == [
        "judge-grok#1",
        "judge-grok#1:replacement",
    ]
    assert result["judgeResults"][0]["replaced"]["judge-grok"] == {"result": None, "error": NO_RESULT}
    assert result["judgeResults"][0]["results"]["judge-grok"] == {"result": None, "error": NO_RESULT}


# ---- a slot still empty: incomplete, no silent two of three ------------------------


def test_a_judge_still_missing_after_its_replacement_leaves_the_round_unruled_and_the_run_incomplete():
    """Two judges confirmed and one slot empty twice: nothing is fixed, the finding is unruled with `missing` for the
    absent judge, the judge's key is degraded with both errors, no third instance is dispatched and the run is
    incomplete. This is the two-of-three fallback the panel does not have."""
    first = {**judged(), "summary": ""}
    responses = {**ONE, **unanimous("confirmed"), **twice("judge-astra", first, "__throw__:fixture error")}
    data = workflow({"judges": panel(), "fix": True}, responses)
    result = data["result"]
    assert result["incomplete"] and not result["fixed"] and result["rounds"] == 1
    assert not any(c["opts"]["phase"] == "Fix" for c in data["calls"])
    assert [c["opts"]["label"] for c in judge_calls(data) if "astra" in c["opts"]["label"]] == [
        "judge-astra#1",
        "judge-astra#1:replacement",
    ]
    assert degraded(result) == {
        "judge:judge-astra": "degraded: no usable result from two fresh judge-astra instances (first: invalid judging "
        f"result; replacement: {NO_RESULT}); the round is unruled, nothing is fixed or cleared on a partial panel"
    }
    (unruled,) = result["disposition"]["unruled"]
    assert unruled["dispositions"] == {"judge-fable": "confirmed", "judge-astra": "missing", "judge-grok": "confirmed"}
    assert unruled["outcomeReason"].startswith("round 1: UNRULED: a judge returned no usable result (")
    record = result["judgeResults"][0]
    assert record["replaced"]["judge-astra"] == {"result": first, "error": "invalid judging result"}
    assert record["results"]["judge-astra"] == {"result": None, "error": NO_RESULT}
    assert result["reducedPanel"] is None
    assert status_api.coverage({**result, "coverageComplete": True})["boss"] == {
        "state": "failed",
        "reason": degraded(result)["judge:judge-astra"],
    }


# ---- reduced coverage, only when the request authorized it --------------------------


def test_reduced_coverage_rules_with_two_judges_only_under_the_registered_authorization():
    responses = {
        **ONE,
        **unanimous("confirmed"),
        **twice("judge-astra"),
        "fix:bucket1/1#1": FIX_PACKET,
    }
    data = workflow({"judges": panel(), "fix": True, "reducedPanel": AUTH}, responses)
    result = data["result"]
    assert not result["incomplete"] and result["fixed"] and result["rounds"] == 2
    assert result["reducedPanel"] == AUTH
    assert result["degraded"] == [
        {
            "key": "judge:judge-astra",
            "reason": f"reduced coverage: no usable result from two fresh judge-astra instances (first: {NO_RESULT}; "
            f"replacement: {NO_RESULT}); the panel ruled with judge-fable and judge-grok under the registered "
            "authorization",
        }
    ]
    (fixed,) = result["disposition"]["fixed"]
    assert fixed["dispositions"] == {"judge-fable": "confirmed", "judge-astra": "missing", "judge-grok": "confirmed"}
    assert fixed["rounds"][0]["reason"] == "judge-fable: confirmed by fixture; judge-grok: confirmed by fixture"
    assert status_api.coverage({**result, "coverageComplete": True})["boss"] == {
        "state": "reduced",
        "reason": result["degraded"][0]["reason"],
    }
    # Still a merge by evidence: the two judges in conflict rule nothing.
    split = workflow(
        {"judges": panel(), "fix": True, "reducedPanel": AUTH},
        {**ONE, **unanimous("confirmed"), "judge-grok#1": judged(disposition="refuted"), **twice("judge-astra")},
    )["result"]
    assert split["incomplete"] and not split["fixed"]
    assert set(degraded(split)) == {"judge:judge-astra", "judge:panel"}
    assert degraded(split)["judge:judge-astra"].startswith("reduced coverage:")
    assert split["disposition"]["unruled"][0]["outcomeReason"].startswith("round 1: UNRULED: evidence conflicts")


def test_the_authorization_never_covers_two_missing_judges():
    responses = {**ONE, **unanimous("confirmed"), **twice("judge-astra"), **twice("judge-grok")}
    data = workflow({"judges": panel(), "fix": True, "reducedPanel": AUTH}, responses)
    result = data["result"]
    assert result["incomplete"] and not result["fixed"]
    assert not any(c["opts"]["phase"] == "Fix" for c in data["calls"])
    assert set(degraded(result)) == {"judge:judge-astra", "judge:judge-grok"}
    for reason in degraded(result).values():
        assert reason.startswith("degraded: no usable result from two fresh judge-")
        assert reason.endswith("; the authorization covers one missing judge, not 2")
    assert result["disposition"]["unruled"][0]["dispositions"] == {
        "judge-fable": "confirmed",
        "judge-astra": "missing",
        "judge-grok": "missing",
    }


def test_the_authorization_changes_nothing_when_every_judge_answers():
    data = workflow({"judges": panel(), "reducedPanel": AUTH}, {**ONE, **unanimous("confirmed")})
    result = data["result"]
    assert result["degraded"] == [] and not result["incomplete"] and result["reducedPanel"] == AUTH
    assert len(judge_calls(data)) == 3 and result["judgeResults"][0]["replaced"] == {}


@pytest.mark.parametrize(
    "args, message",
    [
        ({"reducedPanel": AUTH}, "needs judges (--panel)"),
        ({"judges": panel(), "reducedPanel": ""}, "recorded authorization"),
        ({"judges": panel(), "reducedPanel": "   "}, "recorded authorization"),
        ({"judges": panel(), "reducedPanel": True}, "recorded authorization"),
        ({"judges": panel(), "reducedPanel": {"by": "user"}}, "recorded authorization"),
    ],
)
def test_an_authorization_without_a_panel_or_without_words_refuses_to_start(args, message):
    error = workflow(args, ONE, succeeds=False)["error"]
    assert message in error, error


def test_boss_mode_carries_no_authorization():
    result = workflow({}, ONE)["result"]
    assert result["reducedPanel"] is None and result["judgeResults"] == []


# ---- the run record -----------------------------------------------------------------


@pytest.mark.parametrize(
    "args, message",
    [
        ({"reducedPanel": AUTH}, "needs judges"),
        ({"judges": panel(), "reducedPanel": ""}, "recorded authorization"),
        ({"judges": panel(), "reducedPanel": ["user said so"]}, "recorded authorization"),
    ],
)
def test_manifest_refuses_an_authorization_without_a_panel_or_without_words(tmp_path, args, message):
    with pytest.raises(manifest_api.ManifestError, match=message):
        manifest_api.validate_args({"repoRoot": str(tmp_path), "mode": "uncommitted", **args}, tmp_path.resolve())
    manifest_api.validate_args(
        {"repoRoot": str(tmp_path), "mode": "uncommitted", "judges": panel(), "reducedPanel": AUTH}, tmp_path.resolve()
    )
    manifest_api.validate_args(
        {"repoRoot": str(tmp_path), "mode": "uncommitted", "reducedPanel": None}, tmp_path.resolve()
    )


def judges_record():
    return {
        name: {
            "agentType": f"vidi-skills:{name}",
            "requestedModel": prepare.agent_model(name),
            "catalogModel": None,
            "resolvedModel": None,
            "upstreamModel": None,
        }
        for name in PANEL
    }


REDUCED_ENTRY = {"key": "judge:judge-astra", "reason": "reduced coverage: no usable result from two fresh instances"}


def test_finalize_accepts_a_short_panel_only_against_the_manifest_that_registered_the_authorization(run):  # noqa: F811
    """The result's own word is not the authorization: a `judge:` entry under `degraded` is accepted only when the
    manifest carries `reducedPanel`, the result echoes the same text, the entry names a registered judge as reduced
    coverage, and no second judge is missing."""
    _, manifest, result_path = run(judges=panel(), reducedPanel=AUTH)
    assert measure(manifest, result_path).returncode == 0
    recorded = json.loads(result_path.read_text())
    recorded.update(boss=[], judges=judges_record(), reducedPanel=AUTH, degraded=[REDUCED_ENTRY])

    def finalize(**changes):
        result_path.write_text(json.dumps({**recorded, **changes}))
        return gate(manifest, "finalize", result_path)

    refused = finalize(reducedPanel=None)
    assert refused.returncode == 2 and "Reduced-coverage authorization disagrees" in refused.stderr
    short = "Judging panel short a judge cannot be accepted without the registered reduced-coverage authorization"
    for degraded_list in (
        [REDUCED_ENTRY, {**REDUCED_ENTRY, "key": "judge:judge-grok"}],
        [{**REDUCED_ENTRY, "reason": "degraded: no usable result from two fresh instances"}],
        [{**REDUCED_ENTRY, "key": "judge:panel"}],
        [{**REDUCED_ENTRY, "key": "judge:judge-sonnet"}],
    ):
        refused = finalize(degraded=degraded_list)
        assert refused.returncode == 2 and short in refused.stderr, (degraded_list, refused.stderr)
    accepted = finalize()
    assert accepted.returncode == 0, accepted.stderr
    assert json.loads(accepted.stdout)["accepted"] is True
    # The same reduced result against a panel request that registered no authorization.
    _, plain_manifest, plain_result = run(judges=panel())
    assert measure(plain_manifest, plain_result).returncode == 0
    data = json.loads(plain_result.read_text())
    data.update(boss=[], judges=judges_record(), degraded=[REDUCED_ENTRY])
    plain_result.write_text(json.dumps(data))
    refused = gate(plain_manifest, "finalize", plain_result)
    assert refused.returncode == 2 and short in refused.stderr
    data["degraded"] = []
    plain_result.write_text(json.dumps(data))
    accepted = gate(plain_manifest, "finalize", plain_result)
    assert accepted.returncode == 0, accepted.stderr


# ---- the documents ------------------------------------------------------------------


def test_contract_skill_and_references_describe_the_replacement_and_the_authorization():
    contract = (SKILL / "references/judge-contract.md").read_text()
    assert "## When a judge slot fails" in contract and "`reducedPanel`" in contract
    prose = " ".join(contract.split())
    assert "does not inherit that fallback" in prose and "no optional third" in prose
    assert "REVIEW-5" not in contract and "`replaced`" in contract
    skill = (SKILL / "SKILL.md").read_text()
    assert '`--reduced-panel "<text>"`' in skill and "[ --reduced-panel <text> ]" in skill
    assert "reducedPanel: <the --reduced-panel text, or null" in skill and "never on\n  your own judgment" in skill
    assert "`replaced`" in skill and "reducedPanel, attempted" in skill
    assert "`replaced`" in (SKILL / "references/judging.md").read_text()
    assert "`reducedPanel`" in (SKILL / "references/reviewers.md").read_text()
    assert "fresh replacement" in (SKILL / "references/disposition.md").read_text()
    source = (SKILL / "deep-review.workflow.js").read_text()
    assert "`${key}#${roundNo}:replacement`" in source and "const REDUCED = " in source
    for path in (
        SKILL / "references/judge-contract.md",
        SKILL / "references/judging.md",
        SKILL / "references/disposition.md",
        SKILL / "references/reviewers.md",
        REPO / "tests/test_judge_replacement.py",
    ):
        assert not {"\u2014", "\u2013"} & set(path.read_text()), path
