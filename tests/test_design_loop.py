"""Offline packet/privacy and mocked Workflow checks. Never call a model."""

import copy
import importlib.util
import json
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
SKILL = REPO / "skills/design-loop"
NODE = shutil.which("node")
spec = importlib.util.spec_from_file_location("design_prepare", SKILL / "scripts/prepare.py")
assert spec and spec.loader
prepare = importlib.util.module_from_spec(spec)
spec.loader.exec_module(prepare)
# Signature-only fixture: these tests verify bytes/metadata, not image decoding.
IMAGE = b"\x89PNG\r\n\x1a\nfixture-not-a-live-vision-test"


@pytest.fixture
def source(tmp_path, monkeypatch):
    monkeypatch.delenv("CLAUDE_CODE_SUBAGENT_MODEL_FORCE", raising=False)
    image = tmp_path / "PRIVATE-author-fable-round-7-target-9.75.png"
    image.write_bytes(IMAGE)
    capture = {
        "path": str(image),
        "viewport": {"width": 1280, "height": 900, "device_scale": 1},
        "theme": "light",
        "state": "ticket list",
        "fixture": "support-v1",
        "private_annotation": "PRIVATE-capture",
    }
    return {
        "agents_loaded": True,
        "route_ready": True,
        "blindness_checked": True,
        "brief": {
            "audience": "Support staff",
            "tasks": ["Assign the urgent ticket"],
            "requirements": ["Keep status visible"],
            "brand": ["Keep the blue accent"],
            "acceptance_target": "PRIVATE-brief-target",
        },
        "candidates": [
            {"key": "PRIVATE-reference", "revision": "PRIVATE-rev-A", "captures": [copy.deepcopy(capture)]},
            {"key": "PRIVATE-current", "revision": "PRIVATE-rev-B", "captures": [copy.deepcopy(capture)]},
        ],
        "order": ["PRIVATE-current", "PRIVATE-reference"],
        "focus": "PRIVATE-current",
        "round": 2,
        "max_rounds": 3,
        "target_overall": 9.75,
        "earlier_scores": "PRIVATE-scores",
        "original_request": "PRIVATE-full-request",
        "runtime": {"status": "passed", "revision": "PRIVATE-rev-B", "evidence": ["PRIVATE-runtime-record"]},
    }


@pytest.fixture
def prepared(source, tmp_path):
    return prepare.prepare(source, env={}, temp_parent=tmp_path)


def valid_result(prepared, *, score: float | None = 8, preference="C1"):
    packet = prepared["workflow"]["packet"]
    candidates = []
    observed = []
    for c in packet["candidates"]:
        ids = [e["id"] for e in c["evidence"]]
        observed.extend(ids)
        rating = {"score": score, "reason": "Action is visually prominent", "evidence_ids": ids}
        candidates.append(
            {
                "id": c["id"],
                "perceived_primary_action": "Assign ticket",
                "ratings": {k: copy.deepcopy(rating) for k in prepare.DIMENSIONS},
                "overall": copy.deepcopy(rating),
                "strengths": [{"description": "Status remains visible", "evidence_ids": ids}],
                "findings": [],
                "unknowns": ["Keyboard interaction is not shown"],
            }
        )
    return {
        "status": "reviewed",
        "packet_id": packet["packet_id"],
        "observed_evidence_ids": observed,
        "limitations": [],
        "candidates": candidates,
        "comparison": {"preference": preference, "reason": "The task action is clearer", "evidence_ids": observed},
    }


def run_workflow(prepared, replies=None, remaining=100_000):
    if not NODE:
        pytest.skip("node not installed")
    harness = r"""
const fs = require('fs');
const input = JSON.parse(fs.readFileSync(0, 'utf8'));
const calls = [], counts = {}, logs = [];
const agent = async (prompt, opts) => {
  calls.push({prompt, opts});
  const key = opts.agentType.split(':').pop();
  const index = counts[key] || 0; counts[key] = index + 1;
  const replies = input.replies[key] || [input.defaultResult];
  const reply = replies[Math.min(index, replies.length - 1)];
  if (reply && reply.throw) throw Error('fixture transport error');
  return reply;
};
const AsyncFunction = Object.getPrototypeOf(async function(){}).constructor;
const body = input.script.replace('export const meta', 'const meta');
const run = new AsyncFunction('args', 'agent', 'parallel', 'phase', 'log', 'budget', body);
const parallel = thunks => Promise.all(thunks.map(f => Promise.resolve().then(f).catch(() => null)));
run(input.args, agent, parallel, () => {}, x => logs.push(x), {remaining: () => input.remaining})
  .then(result => process.stdout.write(JSON.stringify({result, calls, logs})))
  .catch(error => process.stdout.write(JSON.stringify({error: error.message, calls, logs})));
"""
    payload = {
        "script": (SKILL / "design-loop.workflow.js").read_text(),
        "args": prepared["workflow"],
        "defaultResult": valid_result(prepared),
        "replies": replies or {},
        "remaining": remaining,
    }
    result = subprocess.run(
        [NODE, "-e", harness], input=json.dumps(payload), text=True, capture_output=True, timeout=30, check=True
    )
    return json.loads(result.stdout)


def test_preparation_projects_public_fields_and_preserves_bytes(source, prepared):
    public = prepared["workflow"]["packet"]
    assert "PRIVATE" not in json.dumps(public)
    assert "9.75" not in json.dumps(public)
    assert public["candidates"][0]["id"] == prepared["workflow"]["control"]["focus_id"] == "C1"
    assert prepared["provenance"][0]["key"] == source["focus"]
    assert prepared["workflow"]["control"]["runtime"] == source["runtime"]
    root = Path(prepared["public_root"])
    assert sorted(p.name for p in root.iterdir()) == ["C1E1.png", "C2E1.png"]
    for candidate in public["candidates"]:
        for image in candidate["evidence"]:
            assert Path(image["path"]).read_bytes() == IMAGE
    assert prepare.verify(prepared)["image_hashes_verified"]


def test_preparation_does_not_mutate_input(source, tmp_path):
    before = copy.deepcopy(source)
    prepare.prepare(source, env={}, temp_parent=tmp_path)
    assert source == before


def test_default_order_records_a_complete_permutation(source, tmp_path):
    source.pop("order")
    result = prepare.prepare(source, env={}, temp_parent=tmp_path)
    assert {p["key"] for p in result["provenance"]} == {c["key"] for c in source["candidates"]}
    focus = result["workflow"]["control"]["focus_id"]
    assert next(p["key"] for p in result["provenance"] if p["id"] == focus) == source["focus"]


@pytest.mark.parametrize("change", ["image", "packet", "symlink"])
def test_verification_refuses_changed_evidence(prepared, tmp_path, change):
    image = Path(prepared["workflow"]["packet"]["candidates"][0]["evidence"][0]["path"])
    if change == "image":
        image.write_bytes(IMAGE + b"changed")
    elif change == "packet":
        prepared["workflow"]["packet"]["brief"]["audience"] = "A different task"
    else:
        other = tmp_path / "other.png"
        other.write_bytes(IMAGE)
        image.unlink()
        image.symlink_to(other)
    with pytest.raises(ValueError):
        prepare.verify(prepared)


@pytest.mark.parametrize("value", ["1", "TRUE", "yes", " On ", "unknown"])
def test_forced_or_unknown_model_override_is_refused(source, value):
    with pytest.raises(ValueError, match="MODEL_FORCE"):
        prepare.prepare(source, env={"CLAUDE_CODE_SUBAGENT_MODEL_FORCE": value})


@pytest.mark.parametrize("value", ["", "0", "false", "NO", " off "])
def test_inactive_model_force_is_allowed(value):
    assert prepare.named_agents({"CLAUDE_CODE_SUBAGENT_MODEL_FORCE": value})["forced_model"] is False


@pytest.mark.parametrize(
    ("key", "value"),
    [
        ("round", 4),
        ("round", True),
        ("max_rounds", 0),
        ("target_overall", float("nan")),
        ("target_overall", True),
        ("target_overall", 11),
        ("order", ["PRIVATE-current", "PRIVATE-current"]),
        ("focus", "missing"),
        ("blindness_checked", False),
        ("route_ready", False),
        ("agents_loaded", False),
    ],
)
def test_invalid_or_unready_input_is_refused_before_copying(source, tmp_path, key, value):
    source[key] = value
    before = list(tmp_path.iterdir())
    with pytest.raises(ValueError):
        prepare.prepare(source, env={}, temp_parent=tmp_path)
    assert list(tmp_path.iterdir()) == before


def test_smoke_can_check_an_unproven_route(source, tmp_path):
    source.update(purpose="smoke", route_ready=False, round=1, max_rounds=1)
    prepared = prepare.prepare(source, env={}, temp_parent=tmp_path)
    result = run_workflow(prepared)["result"]
    assert result["purpose"] == "smoke"
    assert not result["accepted"] and not result["can_revise"]


def test_smoke_cannot_bypass_round_bound(source):
    source.update(purpose="smoke", route_ready=False)
    with pytest.raises(ValueError, match="exactly one"):
        prepare.prepare(source, env={})


def test_stale_runtime_pass_is_refused(source):
    source["runtime"]["revision"] = "other-tree"
    with pytest.raises(ValueError, match="runtime pass"):
        prepare.prepare(source, env={})


def test_incomparable_contexts_are_refused(source):
    source["candidates"][1]["captures"][0]["viewport"]["width"] = 360
    with pytest.raises(ValueError, match="matching"):
        prepare.prepare(source, env={})


def test_cli_does_not_overwrite_a_private_record(source, tmp_path):
    input_path, output = tmp_path / "input.json", tmp_path / "prepared.json"
    input_path.write_text(json.dumps(source))
    output.write_text("keep-this-record")
    process = subprocess.run(
        [sys.executable, str(SKILL / "scripts/prepare.py"), "prepare", str(input_path), "--out", str(output)],
        capture_output=True,
        text=True,
        check=False,
        timeout=30,
    )
    assert process.returncode == 1 and "new private file" in process.stderr
    assert output.read_text() == "keep-this-record"


def test_fresh_pinned_seats_receive_identical_public_only_prompts(prepared):
    data = run_workflow(prepared)
    assert len(data["calls"]) == 3
    assert len({c["prompt"] for c in data["calls"]}) == 1
    for call in data["calls"]:
        assert "PRIVATE" not in call["prompt"] and "9.75" not in call["prompt"]
        assert "model" not in call["opts"]
        assert call["opts"]["agentType"].startswith("vidi-skills:design-judge-")
        assert call["opts"]["schema"] == prepared["workflow"]["judge_schema"]
    result = data["result"]
    assert result["status"] == "needs_adjudication" and result["coverage_complete"]
    assert result["can_revise"] and not result["accepted"] and result["target_met"] is False
    assert {j["requested_model"] for j in result["judges"]} == set(prepare.MODELS.values())
    assert all(j["resolved_model"] is None and j["upstream_model"] is None for j in result["judges"])


@pytest.mark.parametrize("failure", [None, {}, {"throw": True}])
def test_failed_seat_gets_one_replacement_then_stays_missing(prepared, failure):
    data = run_workflow(prepared, {"design-judge-grok": [failure]})
    assert len(data["calls"]) == 4
    assert data["result"]["status"] == "incomplete"
    assert not data["result"]["can_revise"] and not data["result"]["accepted"]
    assert len(data["result"]["judges"]) == 3
    assert len(data["result"]["judges"][2]["attempts"]) == 2


def test_replacement_keeps_model_packet_and_first_failure(prepared):
    data = run_workflow(prepared, {"design-judge-grok": [None, valid_result(prepared)]})
    result = data["result"]
    assert result["coverage_complete"]
    assert [a["status"] for a in result["judges"][2]["attempts"]] == ["invalid", "reviewed"]
    grok = [c for c in data["calls"] if c["opts"]["agentType"].endswith("grok")]
    assert grok[0]["prompt"] == grok[1]["prompt"]


@pytest.mark.parametrize("status", ["contaminated", "insufficient_evidence"])
def test_semantic_failure_is_not_retried(prepared, status):
    result = valid_result(prepared)
    result.update(status=status, limitations=["Cannot provide a clean complete review"])
    data = run_workflow(prepared, {"design-judge-fable": [result]})
    assert len(data["calls"]) == 3
    assert not data["result"]["coverage_complete"]


@pytest.mark.parametrize("status", ["contaminated", "insufficient_evidence"])
@pytest.mark.parametrize("defect", ["citation_typo", "missing_field", "wrong_packet", "missing_limit"])
def test_malformed_semantic_failure_cannot_be_retried_away(prepared, status, defect):
    result = valid_result(prepared)
    result.update(status=status, limitations=["Cannot provide a clean complete review"])
    if defect == "citation_typo":
        result["observed_evidence_ids"] = ["C1E1.png"]
    elif defect == "missing_field":
        result.pop("candidates")
    elif defect == "wrong_packet":
        result["packet_id"] = "another-packet"
    else:
        result["limitations"] = []
    data = run_workflow(prepared, {"design-judge-fable": [result, valid_result(prepared)]})
    assert len(data["calls"]) == 3
    assert data["result"]["status"] == "incomplete"
    seat = data["result"]["judges"][0]
    assert seat["result"] is None
    assert len(seat["attempts"]) == 1
    assert seat["attempts"][0]["status"] == status
    assert seat["attempts"][0]["reason"] and seat["attempts"][0]["result"] == result


@pytest.mark.parametrize(
    "defect", ["missing_image", "wrong_packet", "missing_category", "cross_citation", "bad_score", "bad_comparison"]
)
def test_inconsistent_results_cannot_complete_the_panel(prepared, defect):
    result = valid_result(prepared)
    if defect == "missing_image":
        result["observed_evidence_ids"].pop()
    elif defect == "wrong_packet":
        result["packet_id"] = "different-packet"
    elif defect == "missing_category":
        result["candidates"][0]["ratings"].pop("clarity")
    elif defect == "cross_citation":
        result["candidates"][0]["overall"]["evidence_ids"] = ["C2E1"]
    elif defect == "bad_score":
        result["candidates"][0]["overall"]["score"] = 11
    else:
        result["comparison"]["evidence_ids"] = ["C1E1"]
    data = run_workflow(prepared, {"design-judge-astra": [result]})
    assert not data["result"]["coverage_complete"]
    assert len(data["calls"]) == 4


def test_high_scores_do_not_erase_a_blocker_or_create_approval(prepared):
    result = valid_result(prepared, score=10)
    result["candidates"][0]["findings"] = [
        {
            "severity": "blocker",
            "element": "Assign control",
            "consequence": "Required action is obscured",
            "recommendation": "Expose the control",
            "preserve": "Status labels",
            "evidence_ids": ["C1E1"],
        }
    ]
    data = run_workflow(prepared, {name: [result] for name in prepare.MODELS})["result"]
    assert data["target_met"] and len(data["blockers"]) == 3
    assert not data["accepted"] and data["status"] == "needs_adjudication"


def test_ties_unknown_scores_and_disagreement_are_retained(prepared):
    result = valid_result(prepared, score=None, preference="tie")
    data = run_workflow(prepared, {"design-judge-grok": [result]})["result"]
    assert data["coverage_complete"] and data["disagreement"]
    assert data["target_met"] is False and not data["accepted"]


def test_last_round_stops_repairs_without_retrying_low_scores(prepared):
    prepared["workflow"]["control"]["round"] = 3
    data = run_workflow(prepared)
    assert len(data["calls"]) == 3
    assert not data["result"]["can_revise"] and not data["result"]["target_met"]


def test_zero_budget_dispatches_nothing_and_preserves_missing_seats(prepared):
    data = run_workflow(prepared, remaining=0)
    assert data["calls"] == []
    assert data["result"]["status"] == "incomplete"
    assert len(data["result"]["judges"]) == 3


@pytest.mark.parametrize("location", ["packet", "brief", "candidate", "image", "viewport", "rubric"])
def test_private_key_accidentally_merged_into_packet_blocks_dispatch(prepared, location):
    packet = prepared["workflow"]["packet"]
    target = {
        "packet": packet,
        "brief": packet["brief"],
        "candidate": packet["candidates"][0],
        "image": packet["candidates"][0]["evidence"][0],
        "viewport": packet["candidates"][0]["evidence"][0]["viewport"],
        "rubric": packet["rubric"],
    }[location]
    target["target"] = "PRIVATE-target"
    data = run_workflow(prepared)
    assert data["error"] and not data["calls"]


def test_exhausted_round_allowance_blocks_dispatch(prepared):
    prepared["workflow"]["control"]["round"] = 4
    data = run_workflow(prepared)
    assert "allowance" in data["error"] and not data["calls"]


def test_no_target_is_not_interpreted_as_a_default_threshold(prepared):
    prepared["workflow"]["control"]["target_overall"] = None
    assert run_workflow(prepared)["result"]["target_met"] is None


def test_model_pins_and_read_only_definitions_match():
    for name, model in prepare.MODELS.items():
        definition = (REPO / "agents" / f"{name}.md").read_text()
        assert f"model: {model}\n" in definition and "tools: Read\n" in definition
        assert "skills:" not in definition and "memory:" not in definition


def test_eval_cases_are_proposals_not_claimed_results():
    cases = json.loads((SKILL / "evals/evals.json").read_text())
    assert cases["skill_name"] == "design-loop" and len(cases["evals"]) == 3
    assert all(c["prompt"] and c["expected_output"] for c in cases["evals"])
