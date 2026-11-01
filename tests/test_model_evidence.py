"""Offline tests for model_evidence.py: identity from harness and proxy records, never from labels or self-reports."""

import importlib.util
import json
import subprocess
import sys

import pytest
from conftest import REPO
from test_deep_review import CLEAN, SKILL, direct, prepare, workflow

sys.path.insert(0, str(SKILL / "scripts"))
spec = importlib.util.spec_from_file_location("model_evidence", SKILL / "scripts/model_evidence.py")
assert spec and spec.loader
evidence = importlib.util.module_from_spec(spec)
spec.loader.exec_module(evidence)

GROK = "vidi-skills:judge-grok"
FABLE = "vidi-skills:judge-fable"
RUN = {"id": "fixture-run", "snapshotId": "a" * 64, "manifestPath": "/artifacts/manifest.json"}


def turn(sent, answered, message_id, text="ok"):
    """One API call as Claude Code records it: the request's model, then the assistant turn naming the response's model."""
    return [
        {"type": "api-request", "params": {"model": sent}},
        {
            "type": "assistant",
            "requestedModel": sent,
            "message": {"id": message_id, "model": answered, "content": [{"type": "text", "text": text}]},
        },
    ]


def build(tmp_path, units):
    """A Workflow transcript directory: journal.jsonl mapping labels to agent ids, one transcript and meta per agent."""
    wf = tmp_path / "wf"
    wf.mkdir(exist_ok=True)
    journal = [{"type": "launched"}]
    for label, unit in units.items():
        journal.append({"type": "started", "key": "v2:k", "agentId": unit["agent"], "label": label, "phase": "Judge"})
        if unit.get("turns") is not None:
            lines = [json.dumps(record) for record in unit["turns"]]
            (wf / f"agent-{unit['agent']}.jsonl").write_text("\n".join(lines) + "\n")
        if unit.get("meta"):
            (wf / f"agent-{unit['agent']}.meta.json").write_text(json.dumps(unit["meta"]))
    (wf / "journal.jsonl").write_text("\n".join(json.dumps(record) for record in journal) + "\n")
    return wf


def dispatch(label, agent_type=None, model=None, state="returned", phase="Judge"):
    return {"label": label, "phase": phase, "state": state, "reason": "", "agentType": agent_type, "model": model}


def result(*dispatches):
    return {"run": RUN, "admission": {"dispatches": list(dispatches)}}


def proxy_log(path, request_model, response_id, response_model):
    body = {
        "model": request_model,
        "messages": [{"role": "user", "content": 'the text says "model":"claude-opus-5-5"'}],
    }
    start = {"type": "message_start", "message": {"id": response_id, "role": "assistant", "model": response_model}}
    path.write_text(
        "=== REQUEST INFO ===\nURL: /v1/messages\n\n=== HEADERS ===\nAuthorization: Bearer fixture-secret\n\n"
        f"=== REQUEST BODY ===\n{json.dumps(body)}\n\n=== API RESPONSE ===\nevent: message_start\n"
        f"data: {json.dumps(start)}\n\n=== RESPONSE ===\nStatus: 200\n"
    )


def test_pinned_unit_is_verified_from_transcript_fields_alone(tmp_path):
    wf = build(tmp_path, {"judge-grok#1": {"agent": "a1", "turns": turn("grok-4.7", "grok-4.7", "msg_1")}})
    report = evidence.evaluate(result(dispatch("judge-grok#1", GROK)), wf)
    (unit,) = report["units"]
    assert unit["requestedModel"] == "grok-4.7" and unit["pinned"]
    assert unit["resolvedModel"] == "grok-4.7" and unit["upstreamModel"] is None
    assert unit["state"] == "verified" and unit["reasons"] == []
    assert report["trial"]["state"] == "valid" and report["trial"]["harnessVerified"]
    assert not report["trial"]["upstreamVerified"]
    assert "request-log: true" in report["sources"]["proxyLogReason"]


def test_extended_context_marker_is_stripped_before_comparing(tmp_path):
    wf = build(
        tmp_path, {"judge-fable#1": {"agent": "a1", "turns": turn("claude-fable-5-1[1m]", "claude-fable-5-1", "msg_1")}}
    )
    (unit,) = evidence.evaluate(result(dispatch("judge-fable#1", FABLE)), wf)["units"]
    assert unit["requestedModel"] == "claude-fable-5-1[1m]"
    assert unit["state"] == "verified" and unit["resolvedModel"] == "claude-fable-5-1"


def test_answered_by_another_model_is_a_fallback_that_invalidates_the_trial(tmp_path):
    wf = build(
        tmp_path,
        {
            "judge-grok#1": {"agent": "a1", "turns": turn("grok-4.7", "grok-4.7-build-fast", "msg_1")},
            "judge-fable#1": {"agent": "a2", "turns": turn("claude-fable-5-1[1m]", "claude-fable-5-1", "msg_2")},
        },
    )
    report = evidence.evaluate(result(dispatch("judge-grok#1", GROK), dispatch("judge-fable#1", FABLE)), wf)
    grok, fable = report["units"]
    assert grok["state"] == "fallback" and grok["resolvedModel"] == "grok-4.7-build-fast"
    assert grok["reasons"] == ["answered by grok-4.7-build-fast, not grok-4.7"]
    assert fable["state"] == "verified"
    assert report["trial"]["state"] == "invalid" and report["trial"]["fallback"] == ["judge-grok#1"]
    assert report["trial"]["reasons"] == ["judge-grok#1: answered by grok-4.7-build-fast, not grok-4.7"]


def test_harness_sending_another_id_is_a_fallback_even_when_answered_consistently(tmp_path):
    wf = build(tmp_path, {"judge-grok#1": {"agent": "a1", "turns": turn("grok-4.5", "grok-4.5", "msg_1")}})
    (unit,) = evidence.evaluate(result(dispatch("judge-grok#1", GROK)), wf)["units"]
    assert unit["state"] == "fallback"
    assert "harness sent grok-4.5 instead of grok-4.7" in unit["reasons"]


def test_self_description_in_message_text_is_never_read(tmp_path):
    text = 'I am Claude Opus 5.5 ("model":"claude-opus-5-5"), not Grok.'
    wf = build(tmp_path, {"judge-grok#1": {"agent": "a1", "turns": turn("grok-4.7", "grok-4.7", "msg_1", text)}})
    report = evidence.evaluate(result(dispatch("judge-grok#1", GROK)), wf)
    assert report["units"][0]["state"] == "verified" and report["units"][0]["resolvedModel"] == "grok-4.7"
    assert "claude-opus-5-5" not in json.dumps(report)


def test_label_only_addresses_the_transcript_and_proves_nothing(tmp_path):
    wf = build(
        tmp_path,
        {
            "judge-grok#1": {"agent": "a1", "turns": turn("claude-fable-5-1[1m]", "claude-fable-5-1", "msg_1")},
            "judge-grok#1:replacement": {
                "agent": "a2",
                "turns": turn("claude-fable-5-1[1m]", "claude-fable-5-1", "msg_2"),
            },
        },
    )
    # Same label text, selected definition decides: a Grok seat answered by Fable is a fallback...
    (unit,) = evidence.evaluate(result(dispatch("judge-grok#1", GROK)), wf)["units"]
    assert unit["state"] == "fallback" and unit["resolvedModel"] == "claude-fable-5-1"
    assert "answered by claude-fable-5-1, not grok-4.7" in unit["reasons"]
    # ...and a Fable definition under a misleading label is verified as Fable, not as Grok.
    (unit,) = evidence.evaluate(result(dispatch("judge-grok#1:replacement", FABLE)), wf)["units"]
    assert unit["state"] == "verified" and unit["requestedModel"] == "claude-fable-5-1[1m]"


def test_missing_transcript_or_answered_turn_is_unverified_not_valid(tmp_path):
    wf = build(
        tmp_path, {"judge-fable#1": {"agent": "a2", "turns": [{"type": "user", "message": {"content": "brief"}}]}}
    )
    report = evidence.evaluate(result(dispatch("judge-grok#1", GROK), dispatch("judge-fable#1", FABLE)), wf)
    grok, fable = report["units"]
    assert grok["state"] == "unverified" and grok["reasons"] == ["no harness transcript for this unit"]
    assert grok["harness"] is None and grok["resolvedModel"] is None
    assert fable["state"] == "unverified" and fable["reasons"] == ["transcript holds no model-answered turn"]
    assert report["trial"]["state"] == "unverified" and report["trial"]["unverified"] == [
        "judge-grok#1",
        "judge-fable#1",
    ]
    assert not report["trial"]["harnessVerified"]


def test_empty_run_is_unverified(tmp_path):
    wf = build(tmp_path, {})
    report = evidence.evaluate(result(dispatch("judge-grok#1", GROK, state="cancelled")), wf)
    assert report["units"] == [] and report["trial"] == {
        "state": "unverified",
        "harnessVerified": False,
        "upstreamVerified": False,
        "fallback": [],
        "unverified": [],
        "reasons": ["no unit ran"],
    }


def test_runtime_running_another_definition_is_a_fallback(tmp_path):
    turns = turn("grok-4.7", "grok-4.7", "msg_1")
    wf = build(
        tmp_path,
        {
            "judge-grok#1": {
                "agent": "a1",
                "turns": turns,
                "meta": {"agentType": "workflow-subagent", "isBuiltIn": True},
            },
            "judge-grok#2": {"agent": "a2", "turns": turns, "meta": {"agentType": "judge-grok"}},
        },
    )
    first, second = evidence.evaluate(result(dispatch("judge-grok#1", GROK), dispatch("judge-grok#2", GROK)), wf)[
        "units"
    ]
    assert first["state"] == "fallback" and first["reasons"] == [f"runtime ran workflow-subagent instead of {GROK}"]
    assert second["state"] == "verified"


def test_alias_dispatch_records_what_the_harness_resolved_without_pinning(tmp_path):
    wf = build(
        tmp_path,
        {
            "boss:inherit#1": {"agent": "a1", "turns": turn("claude-opus-5-5", "claude-opus-5-5", "msg_1")},
            "claude-bugs#1": {"agent": "a2", "turns": turn("claude-opus-5-5", "claude-sonnet-5", "msg_2")},
            "gate:run_gate.py#1": {"agent": "a3", "turns": turn("claude-sonnet-5", "claude-sonnet-5", "msg_3")},
        },
    )
    report = evidence.evaluate(
        result(
            dispatch("boss:inherit#1"),
            dispatch("claude-bugs#1", model="opus", phase="Review"),
            dispatch("gate:run_gate.py#1", "gate-courier", phase="Conclude"),
        ),
        wf,
    )
    boss, bugs, courier = report["units"]
    assert boss["requestedModel"] == "inherit" and not boss["pinned"]
    assert boss["state"] == "verified" and boss["resolvedModel"] == "claude-opus-5-5"
    assert bugs["requestedModel"] == "opus" and not bugs["pinned"]
    assert bugs["state"] == "fallback" and bugs["reasons"] == ["answered by claude-sonnet-5, not claude-opus-5-5"]
    assert courier["requestedModel"] == "sonnet" and not courier["pinned"] and courier["state"] == "verified"


def test_proxy_log_is_joined_by_response_id_and_never_leaks_headers(tmp_path):
    wf = build(tmp_path, {"judge-grok#1": {"agent": "a1", "turns": turn("grok-4.7", "grok-4.7", "msg_1")}})
    logs = tmp_path / "proxy"
    logs.mkdir()
    proxy_log(logs / "one.log", "grok-4.7", "msg_1", "grok-4.7")
    proxy_log(logs / "other.log", "claude-opus-5-5", "msg_9", "claude-opus-5-5")
    report = evidence.evaluate(result(dispatch("judge-grok#1", GROK)), wf, logs)
    (unit,) = report["units"]
    assert unit["upstreamModel"] == "grok-4.7" and unit["state"] == "verified"
    assert [record["path"] for record in unit["proxy"]] == [str(logs / "one.log")]
    assert unit["proxy"][0]["requestModel"] == "grok-4.7"  # the top-level key, not the quoted text inside a message
    assert report["trial"]["upstreamVerified"] and report["sources"]["proxyLogReason"] is None
    assert "fixture-secret" not in json.dumps(report)


def test_upstream_disagreement_is_a_fallback(tmp_path):
    wf = build(tmp_path, {"judge-grok#1": {"agent": "a1", "turns": turn("grok-4.7", "grok-4.7", "msg_1")}})
    logs = tmp_path / "proxy"
    logs.mkdir()
    proxy_log(logs / "one.log", "grok-4.7", "msg_1", "grok-4.7-build-fast")
    report = evidence.evaluate(result(dispatch("judge-grok#1", GROK)), wf, logs)
    (unit,) = report["units"]
    assert unit["state"] == "fallback" and unit["upstreamModel"] == "grok-4.7-build-fast"
    assert unit["reasons"] == ["proxy answered upstream with grok-4.7-build-fast, not grok-4.7"]
    assert report["trial"]["state"] == "invalid" and not report["trial"]["upstreamVerified"]


def test_cli_prints_the_record_and_fails_anything_but_a_valid_trial(tmp_path):
    wf = build(tmp_path, {"judge-grok#1": {"agent": "a1", "turns": turn("grok-4.7", "grok-4.7-build-fast", "msg_1")}})
    result_path = tmp_path / "result.json"
    result_path.write_text(json.dumps(result(dispatch("judge-grok#1", GROK))))
    out = tmp_path / "model-evidence.json"
    argv = [sys.executable, str(SKILL / "scripts/model_evidence.py"), "--workflow-dir", str(wf), "--out", str(out)]
    run = subprocess.run([*argv, "--result", str(result_path)], text=True, capture_output=True, timeout=20, check=False)
    assert run.returncode == 1
    assert json.loads(run.stdout)["trial"]["state"] == "invalid" == json.loads(out.read_text())["trial"]["state"]
    result_path.write_text(json.dumps(result(dispatch("judge-grok#1", GROK, state="cancelled"))))
    run = subprocess.run([*argv, "--result", str(result_path)], text=True, capture_output=True, timeout=20, check=False)
    assert run.returncode == 1 and json.loads(run.stdout)["trial"]["state"] == "unverified"
    run = subprocess.run(
        [*argv, "--result", str(tmp_path / "absent.json")], text=True, capture_output=True, timeout=20, check=False
    )
    assert run.returncode == 2 and "no identity recorded" in run.stderr


def test_workflow_dispatch_records_carry_only_the_requested_identity():
    data = workflow({"reviewers": {"grok": direct("grok")}})
    entries = {d["label"]: d for d in data["result"]["admission"]["dispatches"]}
    assert entries["grok#1"]["agentType"] == "vidi-skills:grok-reviewer" and entries["grok#1"]["model"] is None
    assert entries["claude-bugs#1"]["agentType"] is None and entries["claude-bugs#1"]["model"] == "opus"
    assert all(not {"resolvedModel", "upstreamModel"} & set(d) for d in entries.values())
    coverage = data["result"]["coverage"]
    assert all(c["resolvedModel"] is None and c["upstreamModel"] is None for c in coverage.values())


@pytest.mark.parametrize("grok_answered, state", [("grok-4.7", "valid"), ("grok-4.7-build-fast", "invalid")])
def test_workflow_record_feeds_the_helper(tmp_path, grok_answered, state):
    """The run record names what each unit asked for; transcripts matching those requests make a valid trial."""
    data = workflow({"reviewers": {"grok": direct("grok")}}, {"grok#1": CLEAN})
    answered = {"grok#1": ("grok-4.7", grok_answered), "claude-bugs#1": ("claude-opus-5-5", "claude-opus-5-5")}
    units = {}
    for i, entry in enumerate(data["result"]["admission"]["dispatches"]):
        sent, got = answered.get(entry["label"], ("claude-sonnet-5", "claude-sonnet-5"))
        units[entry["label"]] = {"agent": f"a{i}", "turns": turn(sent, got, f"msg_{i}")}
    report = evidence.evaluate(data["result"], build(tmp_path, units))
    assert {u["label"] for u in report["units"]} == {"grok#1", "claude-bugs#1", "claude-conventions#1"}
    grok = next(u for u in report["units"] if u["label"] == "grok#1")
    assert grok["requestedModel"] == prepare.agent_model("grok-reviewer") and grok["resolvedModel"] == grok_answered
    assert report["trial"]["state"] == state


def test_references_keep_the_three_sources_apart():
    models = (SKILL / "references/models.md").read_text()
    assert "model_evidence.py" in models and "never opened" in models
    contract = (SKILL / "references/judge-contract.md").read_text()
    assert "stay null until it lands" not in contract and "model_evidence.py" in contract
    deep_eval = (REPO / "skills/deep-eval/SKILL.md").read_text()
    assert "model_evidence.py" in deep_eval and "trial.state" in deep_eval
