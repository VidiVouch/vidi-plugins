"""Offline policy/capability and mocked Workflow tests. Never call a model."""

import http.client
import importlib.util
import io
import json
import os
import re
import shutil
import subprocess
import sys
import urllib.error
from email.message import Message
from typing import Any
from unittest.mock import Mock

import pytest
from conftest import REPO

SKILL = REPO / "skills/deep-review"
NODE = shutil.which("node")
spec = importlib.util.spec_from_file_location("prepare_reviewers", SKILL / "scripts/prepare_reviewers.py")
assert spec and spec.loader
prepare = importlib.util.module_from_spec(spec)
spec.loader.exec_module(prepare)
CLEAN = {"findings": [], "degraded": False, "reason": ""}
FINDING = {
    "file": "src/a.py",
    "line_start": 2,
    "line_end": 3,
    "severity": "warning",
    "description": "Reachable input produces wrong output",
}
# The typed packet a fixer returns for its bucket (fix-output.schema.json, the hand-back's words).
FIX_PACKET = {
    "disposition": "completed",
    "summary": "Guarded the empty input in src/a.py",
    "files": ["src/a.py"],
    "checks": [{"command": "uv run ruff check", "passed": True}],
    "unresolved": [],
    "openObligations": [],
    "rebuttals": [],
    "escalations": [],
}


@pytest.fixture(autouse=True)
def no_live_capabilities(monkeypatch):
    monkeypatch.setattr(prepare.shutil, "which", lambda name: None)
    monkeypatch.setattr(
        prepare.urllib.request, "build_opener", Mock(side_effect=AssertionError("live network prohibited"))
    )


def configure(tmp_path, values):
    config = tmp_path / "reviewers.json"
    config.write_text(json.dumps(values))
    return config


def test_disabled_services_are_not_probed(tmp_path, monkeypatch):
    probe = Mock(return_value="/installed/binary")
    monkeypatch.setattr(prepare.shutil, "which", probe)
    result = prepare.prepare(tmp_path, env={})
    probe.assert_not_called()
    assert result["reviewers"]["cubic"]["policy"] == "disabled"
    assert result["reviewers"]["coderabbit"]["policy"] == "disabled"
    assert not result["reviewers"]["grok"]["available"]


def test_enabled_cli_is_not_entitlement_proof(tmp_path, monkeypatch):
    monkeypatch.setattr(prepare.shutil, "which", lambda name: "/bin/cubic")
    config = configure(tmp_path, {"cubic": {"policy": "optional"}})
    result = prepare.prepare(tmp_path, config, env={})["reviewers"]["cubic"]
    assert result["available"]
    assert "subscription and quota unverified" in result["reason"]


@pytest.mark.parametrize(
    "override",
    [
        {"typo": {"policy": "required"}},
        {"grok": {"policy": "auto"}},
        {"grok": {"policy": "required", "model": "some-other-model"}},
        {"grok": True},
        [],
    ],
)
def test_invalid_config_fails(tmp_path, override):
    with pytest.raises(ValueError):
        prepare.prepare(tmp_path, configure(tmp_path, override), env={})


def catalog(monkeypatch, payload):
    opener = Mock()
    opener.open.return_value = io.BytesIO(json.dumps(payload).encode())
    monkeypatch.setattr(prepare.urllib.request, "build_opener", Mock(return_value=opener))
    return opener


def test_direct_catalog_and_auth_are_not_upstream_proof(tmp_path, monkeypatch):
    opener = catalog(monkeypatch, {"data": [{"id": "gpt-6-astra"}, {"id": "grok-4.7"}]})
    result = prepare.prepare(
        tmp_path,
        probe_proxy=True,
        env={
            "ANTHROPIC_BASE_URL": "http://127.0.0.1:8319",
            "ANTHROPIC_AUTH_TOKEN": "fixture-secret",
        },
    )
    opener.open.assert_called_once()
    request = opener.open.call_args.args[0]
    assert request.full_url == "http://127.0.0.1:8319/v1/models"
    assert request.get_header("Authorization") == "Bearer fixture-secret"
    assert "fixture-secret" not in json.dumps(result)
    for key, agent_name in prepare.DIRECT.items():
        c = result["reviewers"][key]
        assert c["available"]
        assert c["agentType"] == f"vidi-skills:{agent_name}"
        assert c["resolvedModel"] is None and c["upstreamModel"] is None


def test_forced_model_blocks_direct_lanes(tmp_path, monkeypatch):
    probe = Mock(side_effect=AssertionError("must not probe"))
    monkeypatch.setattr(prepare, "proxy_models", probe)
    result = prepare.prepare(tmp_path, probe_proxy=True, env={"CLAUDE_CODE_SUBAGENT_MODEL_FORCE": "1"})
    assert not result["reviewers"]["grok"]["available"]
    probe.assert_not_called()


def test_disabled_direct_lanes_do_not_probe_catalog(tmp_path, monkeypatch):
    config = configure(tmp_path, {key: {"policy": "disabled"} for key in prepare.DIRECT})
    probe = Mock(side_effect=AssertionError("must not probe"))
    monkeypatch.setattr(prepare, "proxy_models", probe)
    prepare.prepare(tmp_path, config, probe_proxy=True, env={})
    probe.assert_not_called()


@pytest.mark.parametrize("payload", [{"data": []}, {"data": [{"id": "grok-other"}]}, {"data": "invalid"}, []])
def test_unknown_or_missing_model_not_available(tmp_path, monkeypatch, payload):
    catalog(monkeypatch, payload)
    result = prepare.prepare(tmp_path, probe_proxy=True, env={"ANTHROPIC_BASE_URL": "http://localhost:8319/v1"})
    assert not result["reviewers"]["grok"]["available"]


def test_proxy_failure_redacts_details(monkeypatch):
    opener = Mock()
    opener.open.side_effect = urllib.error.HTTPError("https://secret.invalid/token", 401, "secret", Message(), None)
    monkeypatch.setattr(prepare.urllib.request, "build_opener", Mock(return_value=opener))
    models, reason = prepare.proxy_models({"ANTHROPIC_BASE_URL": "https://proxy.example"})
    assert models is None and "401" in reason and "secret" not in reason
    assert prepare.NoRedirect().redirect_request(None, None, 302, None, None, "https://other.example") is None


@pytest.mark.parametrize(
    "url", ["", "http://remote.example", "https://user:secret@proxy.example", "https://proxy.example?token=secret"]
)
def test_invalid_proxy_does_not_send_credentials(url):
    models, reason = prepare.proxy_models({"ANTHROPIC_BASE_URL": url})
    assert models is None and reason


@pytest.mark.parametrize(
    "error",
    [
        http.client.BadStatusLine("fixture-secret"),
        http.client.LineTooLong("fixture-secret"),
        http.client.IncompleteRead(b"fixture-secret"),
        TimeoutError("fixture-secret"),
    ],
)
def test_optional_proxy_protocol_failure_does_not_abort_required_reviewers(tmp_path, monkeypatch, error):
    opener = Mock()
    opener.open.side_effect = error
    monkeypatch.setattr(prepare.urllib.request, "build_opener", Mock(return_value=opener))
    setup = prepare.prepare(tmp_path, probe_proxy=True, env={"ANTHROPIC_BASE_URL": "http://localhost:8319"})
    assert "fixture-secret" not in json.dumps(setup)
    result = workflow(setup)["result"]
    assert result["attempted"] == ["claude-bugs", "claude-conventions"]
    assert not result["incomplete"] and not result["coverageComplete"]


def test_malformed_proxy_url_is_unavailable():
    models, reason = prepare.proxy_models({"ANTHROPIC_BASE_URL": "http://[broken"})
    assert models is None and "No valid proxy" in reason


def test_policy_changes_fingerprint(tmp_path):
    before = prepare.prepare(tmp_path, env={})
    after = prepare.prepare(tmp_path, configure(tmp_path, {"grok": {"policy": "required"}}), env={})
    assert before["reviewerFingerprint"] != after["reviewerFingerprint"]


NODE_RUNNER = r"""
const fs = require('node:fs');
const { spawnSync } = require('node:child_process');
const input = JSON.parse(fs.readFileSync(0, 'utf8'));
const source = fs.readFileSync(input.path, 'utf8').replace('export const meta', 'const meta');
const calls = [], logs = [];
let active = 0, peak = 0;
const agent = async (prompt, opts) => {
  active++; peak = Math.max(active, peak);
  const index = calls.push({prompt, opts}) - 1;
  await new Promise(resolve => setImmediate(resolve));
  if (input.leakyParallel && index > 0) await new Promise(resolve => setTimeout(resolve, 30));
  active--;
  const response = input.responses[opts.label];
  if (typeof response === 'string' && response.startsWith('__throw__')) throw new Error(response.slice('__throw__:'.length) || 'fixture error');
  if (response !== undefined) return response;
  if (opts.label.startsWith('gate:')) {
    // Stand in for the gate courier: run the owned helper the prompt names, once, and return its printed report unchanged.
    const manifest = /--manifest "([^"]+)"/.exec(prompt)[1];
    const run = spawnSync(input.python, [input.gateHelper, 'run', '--manifest', manifest], {encoding: 'utf8'});
    const packet = {exitCode: run.status, stderr: run.stderr.slice(0, 2000)};
    if (run.stdout.trim()) packet.report = JSON.parse(run.stdout);
    return packet;
  }
  if (opts.phase === 'Judge' && opts.label.startsWith('boss:')) {
    return {rulings: [{id: 0, real: true, severity: 'warning'}]};
  }
  if (opts.phase === 'Fix') return {disposition: 'completed', summary: 'fixed', files: [], checks: [], unresolved: [], openObligations: [], rebuttals: [], escalations: []};
  return {findings: [], degraded: false, reason: ''};
};
const parallel = tasks => {
  const pending = tasks.map(t => Promise.resolve().then(t).catch(() => null));
  // leakyParallel imitates a runtime whose parallel() returns once the first thunk settles and leaves the rest running.
  if (input.leakyParallel && pending.length > 1) return pending[0].then(first => [first, ...pending.slice(1).map(() => null)]);
  return Promise.all(pending);
};
const AsyncFunction = Object.getPrototypeOf(async function(){}).constructor;
const budget = input.budget ? {total: input.budget.total, remaining: () => input.budget.remaining - calls.length} : {total: null};
new AsyncFunction('args', 'agent', 'parallel', 'log', 'phase', 'budget', source)(
  input.args, agent, parallel, x => logs.push(x), () => {}, budget
).then(result => console.log(JSON.stringify({result, calls, logs, peak})))
 .catch(error => {console.error(error.message); process.exitCode = 1;});
"""


def workflow(
    args=None, responses=None, *, succeeds=True, budget=None, path=None, leaky_parallel=False
) -> dict[str, Any]:
    assert NODE, "Node is required for offline Workflow behavior tests"
    params = {
        "repoRoot": "/fixture/repo with spaces",
        "mode": "uncommitted",
        "fix": False,
        "toolchain": "none",
        "run": {"id": "fixture-run", "snapshotId": "a" * 64, "manifestPath": "/artifacts/manifest.json"},
    }
    params.update(args or {})
    result = subprocess.run(
        ["node", "-e", NODE_RUNNER],
        text=True,
        capture_output=True,
        timeout=10,
        check=False,
        input=json.dumps(
            {
                "path": str(path or SKILL / "deep-review.workflow.js"),
                "args": params,
                "responses": responses or {},
                "budget": budget,
                "leakyParallel": leaky_parallel,
                "python": sys.executable,
                "gateHelper": str(SKILL / "scripts/run_gate.py"),
            }
        ),
    )
    if not succeeds:
        assert result.returncode != 0
        return {"error": result.stderr}
    assert result.returncode == 0, result.stderr
    return json.loads(result.stdout)


def direct(key="grok", policy="optional", available=True):
    name = prepare.DIRECT[key]
    model = prepare.agent_model(name)
    return {
        "policy": policy,
        "available": available,
        "agentType": f"vidi-skills:{name}",
        "requestedModel": model,
        "catalogModel": model if available else None,
        "reason": "fixture",
    }


def test_legacy_tools_do_not_auto_enable_services():
    result = workflow({"tools": {"cubic": True, "coderabbit": True, "codex": True, "grok": True}})["result"]
    assert result["attempted"] == ["claude-bugs", "claude-conventions"]
    assert not result["incomplete"] and not result["coverageComplete"]
    assert result["coverage"]["cubic"]["state"] == "disabled"
    assert result["coverage"]["grok"]["state"] == "unavailable"


def test_direct_agents_preserve_schema_without_alias_override():
    data = workflow({"reviewers": {key: direct(key) for key in prepare.DIRECT}})
    calls = [call for call in data["calls"] if call["opts"].get("agentType")]
    assert len(calls) == 3
    assert len({c["opts"]["agentType"] for c in calls}) == 3
    for call in calls:
        assert "model" not in call["opts"] and "effort" not in call["opts"]
        assert "schema" in call["opts"]
        assert not any(k in call["opts"] for k in ["resume", "fork", "name"])
        assert "/fixture/repo with spaces" in call["prompt"]
        assert "untracked" in call["prompt"]
    assert data["result"]["coverageComplete"]


@pytest.mark.parametrize("policy", ["disabled", "optional", "required"])
def test_unavailable_policy_states(policy):
    data = workflow({"reviewers": {"grok": direct(policy=policy, available=False)}})
    assert "grok" not in data["result"]["attempted"]
    assert data["result"]["incomplete"] == (policy == "required")
    if policy == "required":
        assert not data["calls"]
        assert any(x["key"] == "grok" for x in data["result"]["degraded"])


@pytest.mark.parametrize(
    "bad",
    [
        None,
        "__throw__",
        {},
        [],
        {"findings": [], "degraded": "false", "reason": ""},
        {"findings": []},
        {"findings": [], "degraded": True, "reason": "quota"},
        *[
            {"findings": [{**FINDING, **change}], "degraded": False, "reason": ""}
            for change in [
                {"file": "../outside.py"},
                {"file": "/absolute.py"},
                {"file": "C:\\outside.py"},
                {"line_start": 0},
                {"line_end": 1},
                {"line_start": 1.5},
                {"severity": "high"},
                {"description": ""},
            ]
        ],
    ],
)
def test_invalid_required_result_is_not_clean_or_fixable(bad):
    data = workflow({"reviewers": {"grok": direct(policy="required")}, "fix": True}, {"grok#1": bad})
    result = data["result"]
    assert result["incomplete"]
    assert result["coverage"]["grok"]["state"] == "failed"
    assert not result["confirmed"]
    assert not any(c["opts"]["phase"] == "Fix" for c in data["calls"])


def test_optional_subscription_failure_is_reduced_coverage():
    data = workflow(
        {"reviewers": {"cubic": {"policy": "optional", "available": True}}},
        {
            "cubic#1": {"findings": [], "degraded": True, "reason": "Subscription expired"},
        },
    )
    result = data["result"]
    assert not result["incomplete"] and not result["coverageComplete"]
    assert "cubic" in result["attempted"] and "cubic" not in result["successful"]
    assert any("Subscription expired" in d["reason"] for d in result["degraded"])


def test_all_disabled_is_no_data():
    defaults = json.loads((SKILL / "reviewer-defaults.json").read_text())
    result = workflow({"reviewers": {key: {"policy": "disabled", "available": False} for key in defaults}})["result"]
    assert result["incomplete"] and not result["attempted"] and not result["successful"]


def test_findings_aggregate_with_source_and_fresh_rounds():
    data = workflow(
        {"reviewers": {"grok": direct()}, "fix": True},
        {
            "grok#1": {**CLEAN, "findings": [FINDING]},
        },
    )
    result = data["result"]
    assert result["rounds"] == 2 and not result["incomplete"]
    assert result["confirmed"][0]["sources"] == ["grok"]
    assert [a["round"] for a in result["attempts"] if a["key"] == "grok"] == [1, 2]
    repair = next(c for c in data["calls"] if c["opts"]["label"] == "grok#2")
    assert "git diff HEAD and untracked files" in repair["prompt"]
    assert "original baseline" in repair["prompt"]


# The Workflow agent() options a fresh instance may receive; name, fork, resume
# and isolation would hand the unit to an existing agent or a forked conversation.
FRESH_OPTS = {"label", "phase", "schema", "model", "effort", "agentType"}


def test_two_units_of_one_reviewer_are_distinct_fresh_instances():
    """Round 2 re-dispatches grok after a fix: a new agent() call, not the round-1 instance resumed."""
    data = workflow({"reviewers": {"grok": direct()}, "fix": True}, {"grok#1": {**CLEAN, "findings": [FINDING]}})
    calls = data["calls"]
    labels = [c["opts"]["label"] for c in calls]
    assert len(set(labels)) == len(labels) == 8  # three reviewers twice, one boss, one fixer: one agent() call each
    assert {"grok#1", "boss:inherit#1", "fix:bucket1/1#1", "grok#2"} <= set(labels)
    for call in calls:
        assert set(call["opts"]) <= FRESH_OPTS, call["opts"]
        assert "fresh instance with no prior conversation" in call["prompt"]
    ordinals = sorted(int(re.search(r"; dispatch (\d+)\.", c["prompt"]).group(1)) for c in calls)
    assert ordinals == list(range(1, len(calls) + 1))
    first, second = (next(c["prompt"] for c in calls if c["opts"]["label"] == label) for label in ("grok#1", "grok#2"))
    assert first.splitlines()[0] != second.splitlines()[0]
    assert FINDING["description"] not in second and "fixed" not in second  # unit 1's result and fix summary stay out
    assert "Initial review" in first and "Repair round 2" in second


@pytest.mark.parametrize("option", ["name: 'kept'", "fork: true", "resume: 'agent-1'"])
def test_dispatch_refuses_a_named_forked_or_resumed_agent(tmp_path, option):
    """A future edit that names, forks or resumes a reviewer stops the run instead of reusing an agent."""
    source = (SKILL / "deep-review.workflow.js").read_text()
    marker = "const opts = { label: `${r.key}#${roundNo}`, phase: 'Review', schema: REVIEW_OUT }"
    assert marker in source
    patched = tmp_path / "patched.workflow.js"
    patched.write_text(source.replace(marker, marker[:-2] + f", {option} }}"))
    data = workflow(path=patched)
    assert not data["calls"], "no agent may start once a dispatch asks for a non-fresh instance"
    result = data["result"]
    assert result["incomplete"] and not result["successful"] and not result["attempted"]
    assert (
        "non-fresh agent" in result["admission"]["stopReason"]
        and option.split(":")[0] in result["admission"]["stopReason"]
    )
    assert all(d["state"] == "cancelled" for d in result["admission"]["dispatches"])


def test_named_definitions_dispatch_without_a_runtime_instance_name():
    """Reviewer and fixer lanes select a definition by agentType only; a `name` would make a teammate."""
    data = workflow({"reviewers": {"grok": direct()}, "fix": True}, {"grok#1": {**CLEAN, "findings": [FINDING]}})
    typed = [c for c in data["calls"] if "agentType" in c["opts"]]
    assert {c["opts"]["agentType"] for c in typed} == {"fixer", direct()["agentType"]}
    assert any(c["opts"]["phase"] == "Fix" for c in typed)
    for call in typed:
        assert not ({"name", "team_name", "resume", "fork", "isolation"} & set(call["opts"])), call["opts"]
    assert "const FRESH_OPTS" in (SKILL / "deep-review.workflow.js").read_text()


TERMINAL_STATES = {"returned", "failed", "cancelled"}


def test_cleanup_barriers_close_every_phase_before_the_next_starts():
    """Each phase boundary and the Conclude record a barrier that found no dispatch queued or running."""
    data = workflow({"reviewers": {"grok": direct()}, "fix": True}, {"grok#1": {**CLEAN, "findings": [FINDING]}})
    admission = data["result"]["admission"]
    assert [b["name"] for b in admission["barriers"]] == [
        "review#1",
        "judge#1",
        "fix#1",
        "review#2",
        "judge#2",
        "conclude",
    ]
    assert all(b["open"] == 0 for b in admission["barriers"])
    assert admission["barriers"][-1]["dispatched"] == admission["started"] == len(data["calls"]) == 8
    assert all(d["state"] in TERMINAL_STATES for d in admission["dispatches"])
    # A stopped run still ends behind a barrier: nothing it dispatched is active when it returns.
    stopped = workflow({"limits": {"concurrency": 1}}, {"claude-bugs#1": None})["result"]["admission"]
    assert stopped["stopReason"] and stopped["barriers"][-1] == {"name": "conclude", "dispatched": 1, "open": 0}
    assert all(d["state"] in TERMINAL_STATES for d in stopped["dispatches"])


def test_barrier_waits_for_units_a_leaky_parallel_left_running(tmp_path):
    """With a parallel() that returns early, the script's own barrier still waits; without it a unit leaks."""
    data = workflow(leaky_parallel=True)
    admission = data["result"]["admission"]
    assert len(data["calls"]) == admission["started"] == 2
    assert all(d["state"] in TERMINAL_STATES for d in admission["dispatches"])
    assert all(b["open"] == 0 for b in admission["barriers"]) and admission["barriers"][-1]["name"] == "conclude"
    assert data["result"]["successful"] == ["claude-bugs", "claude-conventions"]
    source = (SKILL / "deep-review.workflow.js").read_text()
    mutant = re.sub(r"\n\s*await barrier\([^\n]*", "", source)
    assert mutant != source and "await barrier(" not in mutant
    patched = tmp_path / "no-barrier.workflow.js"
    patched.write_text(mutant)
    leaked = workflow(path=patched, leaky_parallel=True)["result"]["admission"]
    assert leaked["barriers"] == [] and any(d["state"] == "running" for d in leaked["dispatches"])


def test_agent_definitions_declare_fresh_workers():
    names = {path.name for path in (REPO / "agents").glob("*.md")}
    required = {f"{name}.md" for name in prepare.DIRECT.values()} | {
        "fixer.md",
        "gate-courier.md",
        "implementer.md",
        "judge-fable.md",
        "judge-astra.md",
        "judge-grok.md",
    }
    # Other skills may add agents without becoming dependencies of this review tooling.
    assert required <= names
    for name in names:
        text = (REPO / "agents" / name).read_text()
        assert "fresh instance" in text and "no prior conversation" in text, name
        assert "\u2014" not in text and "\u2013" not in text, name


def test_round_cap_and_missing_boss_ruling_block_acceptance():
    responses = {"claude-bugs#1": {**CLEAN, "findings": [FINDING]}}
    capped = workflow({"fix": True, "maxRounds": 1}, responses)["result"]
    assert capped["incomplete"] and capped["fixed"]
    assert capped["rounds"] == capped["admission"]["maxRounds"] == 1
    assert any(d["key"] == "final-verification" for d in capped["degraded"])
    responses["boss:inherit#1"] = {"rulings": []}
    unruled = workflow({"fix": True}, responses)["result"]
    assert unruled["incomplete"] and not unruled["fixed"]


def test_invalid_config_key_rejected():
    assert "invalid reviewer" in workflow({"reviewers": {"typo": {"policy": "required"}}}, succeeds=False)["error"]


def test_packaged_defaults_and_agent_frontmatter():
    result = workflow()["result"]
    defaults = json.loads((SKILL / "reviewer-defaults.json").read_text())
    assert {key: c["policy"] for key, c in result["coverage"].items()} == {
        key: p["policy"] for key, p in defaults.items()
    }
    for name in prepare.DIRECT.values():
        text = (REPO / "agents" / f"{name}.md").read_text()
        assert f"name: {name}" in text
        assert "tools: Read, Grep, Glob, Bash(git -C * diff *)" in text
        assert "Bash," not in text and "Write," not in text and "Edit," not in text
        assert prepare.agent_model(name) not in ("opus", "sonnet", "haiku", "inherit")


@pytest.mark.parametrize("name", ["deep-review", "epoch-review", "rebuild-examine"])
def test_consumers_use_shared_policy_and_incomplete_contract(name):
    text = (REPO / "skills" / name / "SKILL.md").read_text()
    assert "prepare_reviewers.py" in text
    assert "reviewers: REVIEWER_SETUP.reviewers" in text
    assert "tools: REVIEWER_SETUP.tools" in text
    assert "incomplete" in text and "coverageComplete" in text
    assert "command -v cubic" not in text and "command -v coderabbit" not in text
    assert "command -v grok" not in text
    assert "run_manifest.py" in text and "run_gate.py" in text
    assert "args: RUN_ARGS" in text


def test_incomplete_examination_has_a_resumable_ledger_contract():
    examiner = (REPO / "skills/rebuild-examine/SKILL.md").read_text()
    dispatcher = (REPO / "skills/rebuild/SKILL.md").read_text()
    assert "Class: review-incomplete" in examiner and "Class: review-incomplete" in dispatcher
    assert "row to `REVISE@<HI>` with both artifact paths" in examiner
    assert "Do not leave JUDGING behind" in examiner
    assert "do not dispatch an implementer" in dispatcher.replace("\n     ", " ")
    assert "not implementation in step 1" in dispatcher
    assert "re-dispatch a fresh" in dispatcher


@pytest.mark.parametrize(
    "mode, scope, expected",
    [
        ("base", {"base": "release"}, "--base release"),
        ("commit", {"sha": "abc123"}, "--commit abc123"),
        ("range", {"rangeA": "aaa", "rangeB": "bbb", "rangeBIsHead": True}, "--range aaa..bbb"),
        ("uncommitted", {}, "INCLUDING untracked files"),
    ],
)
def test_direct_scope_and_plugin_spec_paths(mode, scope, expected):
    data = workflow({"mode": mode, **scope, "reviewers": {"grok": direct()}, "specDir": "/plugin/reviewers"})
    call = next(c for c in data["calls"] if c["opts"]["label"] == "grok#1")
    assert expected in call["prompt"]
    assert "/plugin/reviewers/grok.md" in call["prompt"]


def test_fixes_require_new_readonly_snapshot_and_external_gate():
    initial = {"claude-bugs#1": {**CLEAN, "findings": [FINDING]}}
    failed_fix = workflow({"fix": True}, {**initial, "fix:bucket1/1#1": None})["result"]
    assert failed_fix["incomplete"] and failed_fix["fixed"]
    fixed = workflow({"fix": True, "toolchain": "python"}, initial)
    assert fixed["result"]["requiresPostFixReview"]
    assert fixed["result"]["acceptance"] == "pending-external-verification"
    assert not any(c["opts"]["label"].startswith("typecheck") for c in fixed["calls"])
    assert fixed["result"]["currentFindings"] == []
    assert len(fixed["result"]["confirmed"]) == 1  # historical finding is not still open


def test_optional_failure_history_survives_later_success():
    data = workflow(
        {"fix": True, "reviewers": {"grok": direct()}},
        {
            "claude-bugs#1": {**CLEAN, "findings": [FINDING]},
            "grok#1": {"findings": [], "degraded": True, "reason": "timeout"},
        },
    )
    attempts = [a for a in data["result"]["attempts"] if a["key"] == "grok"]
    assert [a["state"] for a in attempts] == ["failed", "succeeded"]
    assert attempts[0]["reason"] == "timeout"
    assert "grok" in data["result"]["successful"]


def test_workflow_and_cli_schema_fields_agree():
    schema = json.loads((SKILL / "review-output.schema.json").read_text())
    call = workflow()["calls"][0]
    runtime = call["opts"]["schema"]
    assert runtime["required"] == schema["required"]
    assert runtime["properties"].keys() == schema["properties"].keys()
    native_finding = schema["properties"]["findings"]["items"]["properties"]
    runtime_finding = runtime["properties"]["findings"]["items"]["properties"]
    assert native_finding.keys() == runtime_finding.keys()
    assert native_finding["severity"] == runtime_finding["severity"]


def test_repeated_freezes_preserve_prior_worktrees(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    for argv in (
        ["init", "-q"],
        ["config", "user.name", "Fixture"],
        ["config", "user.email", "fixture@example.invalid"],
    ):
        subprocess.run(["git", "-C", str(repo), *argv], check=True, capture_output=True)
    (repo / "file").write_text("baseline")
    subprocess.run(["git", "-C", str(repo), "add", "file"], check=True, capture_output=True)
    subprocess.run(["git", "-C", str(repo), "commit", "-qm", "fixture"], check=True, capture_output=True)
    head = subprocess.check_output(["git", "-C", str(repo), "rev-parse", "HEAD"], text=True).strip()
    text = (SKILL / "SKILL.md").read_text()
    fragment = "WT_BASE=" + text.split("WT_BASE=", 1)[1].split("\n\n", 1)[0]
    paths = []
    for _ in range(2):
        out = subprocess.run(
            ["bash", "-c", fragment + '\nprintf "%s\\n" "$WT"'],
            env={**os.environ, "PROJ": str(repo), "START": head, "CACHE_KEY": "same", "TMPDIR": str(tmp_path)},
            text=True,
            capture_output=True,
            timeout=10,
            check=True,
        )
        path = tmp_path / out.stdout.strip().splitlines()[-1]
        (path / "partial-fix").write_text("keep this")
        paths.append(path)
    assert paths[0] != paths[1]
    assert all((path / "partial-fix").read_text() == "keep this" for path in paths)
    assert "workflow|teams" in text and "default workflow" in text
    assert "never short-circuits a requested review" in text


def test_missing_manifest_fails_before_dispatch():
    assert "manifest required" in workflow({"run": None}, succeeds=False)["error"]


@pytest.mark.parametrize("limits", [{"concurrency": 0}, {"maxAgents": -1}, {"minRemainingTokens": -1}])
def test_invalid_admission_limits_fail(limits):
    assert "admission limits" in workflow({"limits": limits}, succeeds=False)["error"]


def test_concurrency_and_total_attempt_limits():
    data = workflow(
        {"reviewers": {key: direct(key) for key in prepare.DIRECT}, "limits": {"concurrency": 2, "maxAgents": 3}}
    )
    assert data["peak"] <= 2
    assert len(data["calls"]) == data["result"]["admission"]["started"] == 3
    assert data["result"]["incomplete"]
    assert any(d["state"] == "cancelled" for d in data["result"]["admission"]["dispatches"])
    assert len(data["result"]["attempted"]) == 3


def test_required_failure_stops_waiting_dispatches():
    data = workflow({"limits": {"concurrency": 1}}, {"claude-bugs#1": None})
    assert len(data["calls"]) == 1
    assert data["result"]["coverage"]["claude-conventions"]["state"] == "cancelled"


@pytest.mark.parametrize("reason", ["quota exhausted", "HTTP 429", "authentication failed", "cancelled"])
def test_terminal_optional_failure_stops_waiting_dispatches(reason):
    data = workflow(
        {"reviewers": {key: direct(key) for key in prepare.DIRECT}, "limits": {"concurrency": 1}},
        {
            "astra-standard#1": {"findings": [], "degraded": True, "reason": reason},
        },
    )
    assert len(data["calls"]) == 3
    assert data["result"]["incomplete"]


def test_budget_is_checked_when_a_slot_is_acquired():
    data = workflow({"limits": {"concurrency": 1}}, budget={"total": 10, "remaining": 1})
    assert len(data["calls"]) == 1
    admission = data["result"]["admission"]
    assert admission["tokenBudgetKnown"] and admission["remainingTokens"] == 0
    assert admission["stop"]["reason"].startswith("Token budget admission threshold reached (0 tokens remaining")
    assert data["result"]["incomplete"]


def test_unknown_budget_is_reported_unknown_and_configured_limits_still_apply():
    """Without a finite runtime budget the result says so and nothing invents a reading; maxAgents still stops."""
    data = workflow({"limits": {"maxAgents": 1, "minRemainingTokens": 1000}})
    admission = data["result"]["admission"]
    assert admission["tokenBudgetKnown"] is False and admission["remainingTokens"] is None
    assert len(data["calls"]) == admission["started"] == 1
    assert admission["stop"]["reason"] == "Configured agent-attempt limit reached"
    assert admission["limits"] == {"concurrency": 3, "maxAgents": 1, "minRemainingTokens": 1000}
    assert admission["maxRounds"] == 3


def test_stop_accounts_for_in_flight_work_and_cancels_the_queue():
    """A terminal failure stops the queue at the next launch check; units already running finish and are counted."""
    data = workflow(
        {"reviewers": {key: direct(key) for key in prepare.DIRECT}, "limits": {"concurrency": 2}},
        {"astra-standard#1": {"findings": [], "degraded": True, "reason": "quota exhausted"}},
    )
    admission = data["result"]["admission"]
    assert data["peak"] <= 2
    assert [c["opts"]["label"] for c in data["calls"]] == [
        "claude-bugs#1",
        "claude-conventions#1",
        "astra-standard#1",
        "astra-adversarial#1",
    ]
    assert admission["stop"] == {
        "reason": "Terminal reviewer failure: quota exhausted",
        "dispatched": 4,
        "running": 1,
        "queued": 1,
    }
    assert {d["label"]: d["state"] for d in admission["dispatches"]} == {
        "claude-bugs#1": "returned",
        "claude-conventions#1": "returned",
        "astra-standard#1": "failed",
        "astra-adversarial#1": "returned",
        "grok#1": "cancelled",
    }
    assert admission["started"] == 4 and admission["barriers"][-1] == {"name": "conclude", "dispatched": 4, "open": 0}
    assert data["result"]["coverage"]["grok"]["state"] == "cancelled" and data["result"]["incomplete"]
    assert workflow()["result"]["admission"]["stop"] is None


def test_runtime_cancellation_stops_queued_work():
    """An agent() call rejected with a cancellation leaves the units still waiting for a slot unstarted."""
    data = workflow(
        {"reviewers": {key: direct(key) for key in prepare.DIRECT}, "limits": {"concurrency": 1}},
        {"astra-standard#1": "__throw__:request cancelled by the runtime"},
    )
    assert [c["opts"]["label"] for c in data["calls"]] == ["claude-bugs#1", "claude-conventions#1", "astra-standard#1"]
    admission = data["result"]["admission"]
    assert admission["stop"] == {
        "reason": "Terminal failure: request cancelled by the runtime",
        "dispatched": 3,
        "running": 0,
        "queued": 2,
    }
    assert {d["label"] for d in admission["dispatches"] if d["state"] == "cancelled"} == {
        "astra-adversarial#1",
        "grok#1",
    }
    assert data["result"]["incomplete"] and data["result"]["coverage"]["astra-standard"]["state"] == "failed"


def test_run_identity_is_in_every_prompt_and_result():
    data = workflow()
    assert all("fixture-run" in c["prompt"] and "a" * 64 in c["prompt"] for c in data["calls"])
    assert data["result"]["run"]["id"] == "fixture-run"
    assert len(data["result"]["reviewResults"]) == 2
    assert data["result"]["acceptance"] == "pending-external-verification"


ONE_FINDING = {"claude-bugs#1": {**CLEAN, "findings": [FINDING]}}


def test_fix_lane_returns_a_typed_packet_the_result_records():
    """The fixer is dispatched with the file-pinned fix schema and its packet lands in fixResults, not in prose."""
    data = workflow({"fix": True}, {**ONE_FINDING, "fix:bucket1/1#1": FIX_PACKET})
    fix = next(c for c in data["calls"] if c["opts"]["phase"] == "Fix")
    schema = json.loads((SKILL / "fix-output.schema.json").read_text())
    runtime = fix["opts"]["schema"]
    assert runtime["required"] == schema["required"]
    assert runtime["properties"].keys() == schema["properties"].keys()
    assert runtime["properties"]["disposition"]["enum"] == schema["properties"]["disposition"]["enum"]
    assert runtime["properties"]["checks"]["items"]["required"] == schema["properties"]["checks"]["items"]["required"]
    assert "structured-output tool" in fix["prompt"] and "openObligations" in fix["prompt"]
    result = data["result"]
    assert result["fixResults"] == [{"round": 1, "bucket": 1, "owned": ["src/a.py"], "result": FIX_PACKET, "error": ""}]
    assert result["rounds"] == 2 and not result["incomplete"]


def test_partial_fix_packet_continues_to_the_next_review_round():
    partial = {**FIX_PACKET, "disposition": "partial", "unresolved": ["line 2 needs a design decision"]}
    result = workflow({"fix": True}, {**ONE_FINDING, "fix:bucket1/1#1": partial})["result"]
    assert result["rounds"] == 2 and result["fixResults"][0]["result"]["disposition"] == "partial"
    assert not any(d["key"] == "fix#1" for d in result["degraded"])


@pytest.mark.parametrize(
    "bad, reason",
    [
        ("fixed", "no packet"),
        (None, "no packet"),
        ({}, "invalid fix packet"),
        ({**FIX_PACKET, "disposition": "done"}, "invalid fix packet"),
        ({**FIX_PACKET, "summary": " "}, "invalid fix packet"),
        ({**FIX_PACKET, "checks": [{"command": "uv run pytest"}]}, "invalid fix packet"),
        ({**FIX_PACKET, "unresolved": [""]}, "invalid fix packet"),
        ({**FIX_PACKET, "files": ["./src/a.py", "src/b.py"]}, "outside its bucket: src/b.py"),
        ({**FIX_PACKET, "unresolved": ["line 2 still wrong"]}, "contradicts"),
        ({**FIX_PACKET, "checks": [{"command": "uv run pytest", "passed": False}]}, "contradicts"),
        ({**FIX_PACKET, "disposition": "failed"}, "reported failed"),
    ],
)
def test_untyped_or_contradictory_fix_packet_stops_the_loop(bad, reason):
    """Prose, a malformed packet, edits outside the bucket, or a clean word with contrary evidence is a failed fixer."""
    result = workflow({"fix": True}, {**ONE_FINDING, "fix:bucket1/1#1": bad})["result"]
    assert result["incomplete"] and result["fixed"] and result["rounds"] == 1
    degraded = next(d for d in result["degraded"] if d["key"] == "fix#1")
    assert reason in degraded["reason"] and "require verification" in degraded["reason"]
    (record,) = result["fixResults"]
    assert reason in record["error"] and record["owned"] == ["src/a.py"]
    assert record["result"] == (bad if isinstance(bad, dict) else None)


def test_dispatch_refuses_a_unit_without_a_result_schema(tmp_path):
    """Every unit names its result schema; dropping the fix schema stops admission instead of admitting prose."""
    source = (SKILL / "deep-review.workflow.js").read_text()
    for site in (
        "phase: 'Review', schema: REVIEW_OUT",
        "phase: 'Judge', schema: BOSS_OUT",
        "phase: 'Fix', schema: FIX_OUT",
        "phase: 'Conclude', schema: GATE_OUT",
    ):
        assert site in source
    marker = "phase: 'Fix', schema: FIX_OUT, agentType: 'fixer'"
    patched = tmp_path / "untyped.workflow.js"
    patched.write_text(source.replace(marker, marker.replace("schema: FIX_OUT, ", "")))
    data = workflow({"fix": True}, ONE_FINDING, path=patched)
    result = data["result"]
    assert not any(c["opts"]["phase"] == "Fix" for c in data["calls"])
    assert result["incomplete"] and "no result schema" in result["admission"]["stopReason"]
    assert next(d for d in result["admission"]["dispatches"] if d["phase"] == "Fix")["state"] == "cancelled"


# ---- gate evidence ---------------------------------------------------------
# The registered gate runs through run_gate.py, never inside the Workflow sandbox.
# One courier unit runs that helper and returns its printed report; the controller
# decides from the value it receives, not from a log line or a worker's own claim.
GATE = {"commands": [["true"], ["uv", "run", "pytest", "-q"]], "timeoutSeconds": 120}
COURIER = "gate:run_gate.py#1"


def gate_check(index, command):
    return {
        "command": command,
        "exitCode": 0,
        "status": "passed",
        "stdout": f"/artifacts/gate-{index}.stdout",
        "stderr": f"/artifacts/gate-{index}.stderr",
        "stdoutSha256": "0" * 64,
        "stderrSha256": "0" * 64,
    }


GATE_REPORT = {
    "version": 1,
    "runId": "fixture-run",
    "snapshotId": "a" * 64,
    "status": "passed",
    "executor": {"helper": "run_gate.py", "pid": 4242},
    "registry": None,
    "checks": [gate_check(i, c) for i, c in enumerate(GATE["commands"])],
}
GATE_PACKET = {"exitCode": 0, "report": GATE_REPORT, "stderr": ""}


def test_gate_evidence_is_the_value_the_controller_receives():
    """The gate runs through one courier unit that executes run_gate.py; the controller decides from the report in
    the courier's return value, records it with run_gate.py as the executor, and stays complete because it passed."""
    data = workflow({"gate": {**GATE, "registry": "/evidence/processes-implementer-1.json"}}, {COURIER: GATE_PACKET})
    courier = next(c for c in data["calls"] if c["opts"]["label"] == COURIER)
    assert courier["opts"]["phase"] == "Conclude" and courier["opts"]["agentType"] == "gate-courier"
    assert set(courier["opts"]) <= FRESH_OPTS
    schema = json.loads((SKILL / "gate-output.schema.json").read_text())
    assert courier["opts"]["schema"]["required"] == schema["required"]
    assert courier["opts"]["schema"]["properties"].keys() == schema["properties"].keys()
    prompt = courier["prompt"]
    assert (
        'scripts/run_gate.py" run --manifest "/artifacts/manifest.json"'
        ' --registry "/evidence/processes-implementer-1.json"' in prompt
    )
    assert "at least 270 seconds" in prompt  # two commands of 120 seconds plus a margin
    assert "Do not run any check yourself" in prompt and "fixture-run" in prompt and "a" * 64 in prompt
    result = data["result"]
    assert result["gate"] == {
        "state": "passed",
        "reason": "",
        "report": GATE_REPORT,
        "executor": {"kind": "run_gate.py", "manifestPath": "/artifacts/manifest.json"},
        "courier": {"label": COURIER, "agentType": "gate-courier"},
    }
    assert not result["incomplete"] and not any(d["key"] == "gate" for d in result["degraded"])
    entry = next(d for d in result["admission"]["dispatches"] if d["label"] == COURIER)
    assert entry == {
        "label": COURIER,
        "phase": "Conclude",
        "state": "returned",
        "reason": "",
        "agentType": "gate-courier",  # the requested identity; what answered is read from the transcript afterwards
        "model": None,
        "executor": {"kind": "run_gate.py", "wrapper": "gate-courier"},
    }
    # The courier is a dispatched unit like any other: counted, barriered, and the last one.
    assert result["admission"]["started"] == len(data["calls"]) == 3
    assert result["admission"]["barriers"][-1] == {"name": "conclude", "dispatched": 3, "open": 0}
    assert data["calls"][-1]["opts"]["label"] == COURIER
    # The decision rests on the returned value alone: no display line carries the gate.
    assert all("gate" not in line.lower() for line in data["logs"])


@pytest.mark.parametrize(
    "packet, state, reason",
    [
        (None, "missing", "courier returned no packet"),
        ("the gate passed", "missing", "courier returned no packet"),
        ({"exitCode": 0, "stderr": ""}, "missing", "printed no report, exit 0"),
        (
            {"exitCode": 2, "stderr": "Gate/result verification refused: repository snapshot drift detected"},
            "missing",
            "snapshot drift detected",
        ),
        ({**GATE_PACKET, "exitCode": "0"}, "missing", "invalid gate packet"),
        ({**GATE_PACKET, "report": []}, "missing", "invalid gate packet"),
        ({**GATE_PACKET, "report": {**GATE_REPORT, "runId": "another-run"}}, "missing", "not this run"),
        ({**GATE_PACKET, "report": {**GATE_REPORT, "snapshotId": "b" * 64}}, "missing", "not this run"),
        (
            {**GATE_PACKET, "report": {**GATE_REPORT, "checks": [gate_check(0, ["false"]), GATE_REPORT["checks"][1]]}},
            "missing",
            "not this run",
        ),
        (
            {**GATE_PACKET, "report": {**GATE_REPORT, "checks": GATE_REPORT["checks"] + [gate_check(2, ["true"])]}},
            "missing",
            "not this run",
        ),
        (
            {
                **GATE_PACKET,
                "exitCode": 1,
                "report": {
                    **GATE_REPORT,
                    "status": "failed",
                    "checks": [{**gate_check(0, ["true"]), "exitCode": 4, "status": "failed"}],
                },
            },
            "failed",
            '["true"] failed exit 4',
        ),
        (
            {
                **GATE_PACKET,
                "exitCode": 1,
                "report": {
                    **GATE_REPORT,
                    "status": "failed",
                    "checks": [{**gate_check(0, ["true"]), "exitCode": None, "status": "timeout"}],
                },
            },
            "failed",
            '["true"] timeout',
        ),
        (
            {
                **GATE_PACKET,
                "exitCode": 1,
                "report": {
                    **GATE_REPORT,
                    "status": "cancelled",
                    "reason": "Gate interrupted; no acceptance",
                    "checks": [{**gate_check(0, ["true"]), "exitCode": None, "status": "cancelled"}],
                },
            },
            "failed",
            '["true"] cancelled; Gate interrupted',
        ),
        (
            {**GATE_PACKET, "report": {**GATE_REPORT, "checks": GATE_REPORT["checks"][:1]}},
            "failed",
            "1 of 2 registered checks ran",
        ),
        (
            {**GATE_PACKET, "exitCode": 1, "report": {**GATE_REPORT, "status": "failed"}},
            "failed",
            "status failed, exit 1",
        ),
        ({**GATE_PACKET, "exitCode": 1}, "failed", "status passed, exit 1"),
    ],
)
def test_a_gate_that_failed_or_never_arrived_makes_the_run_incomplete(packet, state, reason):
    """Prose, no packet, a helper that printed nothing, a report for another run or another gate, or a gate that ran
    and did not pass: the controller records what it got and the run is incomplete, so nothing can accept it."""
    result = workflow({"gate": GATE}, {COURIER: packet})["result"]
    assert result["incomplete"]
    assert result["gate"]["state"] == state and reason in result["gate"]["reason"], result["gate"]
    assert result["gate"]["report"] == (packet["report"] if state == "failed" else None)
    degraded = next(d for d in result["degraded"] if d["key"] == "gate")
    assert degraded["reason"] == f"degraded: {result['gate']['reason']}"


def test_gate_is_skipped_without_a_courier_when_nothing_can_be_accepted():
    """No registered gate, a tree the fixers changed, or an incomplete review: no gate unit is dispatched and the
    record says why. Only the unregistered case leaves the review complete, and finalize still refuses it."""
    unregistered = workflow()
    assert not any(c["opts"]["label"].startswith("gate:") for c in unregistered["calls"])
    gate = unregistered["result"]["gate"]
    assert gate["state"] == "skipped" and "not registered" in gate["reason"] and gate["courier"] is None
    assert not unregistered["result"]["incomplete"] and gate["report"] is None
    modified = workflow({"fix": True, "gate": GATE}, ONE_FINDING)
    assert not any(c["opts"]["label"].startswith("gate:") for c in modified["calls"])
    assert modified["result"]["requiresPostFixReview"] and not modified["result"]["incomplete"]
    gate = modified["result"]["gate"]
    assert gate["state"] == "skipped" and "fixes were applied" in gate["reason"]
    incomplete = workflow({"gate": GATE, "limits": {"concurrency": 1}}, {"claude-bugs#1": None})
    assert not any(c["opts"]["label"].startswith("gate:") for c in incomplete["calls"])
    gate = incomplete["result"]["gate"]
    assert gate["state"] == "skipped" and "review incomplete" in gate["reason"]
    # A courier the admission cap cancels is missing evidence, recorded with the stop that cancelled it.
    capped = workflow({"gate": GATE, "limits": {"maxAgents": 2}}, {COURIER: GATE_PACKET})
    assert len(capped["calls"]) == 2
    gate = capped["result"]["gate"]
    assert gate["state"] == "missing" and "gate unit not run: Configured agent-attempt limit reached" in gate["reason"]
    assert capped["result"]["incomplete"]


def test_wrapper_dispatches_name_their_actual_executor():
    """The codex boss is a thin sonnet wrapper around codex exec: its dispatch record attributes the ruling to codex
    exec and names the wrapper, so the wrapper's model never reads as the arbiter. Native units carry no such record."""
    ruling = {"rulings": [{"id": 0, "real": True, "severity": "warning"}]}
    data = workflow({"boss": ["gpt-5.6-terra"]}, {**ONE_FINDING, "gpt-5.6:boss:gpt-5.6-terra#1": ruling})
    boss = next(c for c in data["calls"] if c["opts"]["phase"] == "Judge")
    assert boss["opts"]["model"] == "sonnet" and "thin wrapper" in boss["prompt"]
    entry = next(d for d in data["result"]["admission"]["dispatches"] if d["phase"] == "Judge")
    assert entry["executor"] == {"kind": "codex exec", "model": "gpt-5.6-terra", "wrapper": "sonnet"}
    assert not data["result"]["incomplete"] and data["result"]["currentFindings"][0]["severity"] == "warning"
    assert all("executor" not in d for d in data["result"]["admission"]["dispatches"] if d["phase"] == "Review")
