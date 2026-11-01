"""Deterministic failure fixtures across the run contract and lifecycle helpers; nothing here calls a model.

Each fixture reproduces one state the plan names (a wrong worktree, an unusable result, a timeout, a missing
reviewer or gate, an unexpected route, a cancellation, a duplicate retry, missing artifacts, missing resume
arguments, a changed script or snapshot, a replayed transient failure, a zero-agent completion, display
truncation, queued work after a stop, a crash at an epoch commit or rebase boundary, a crash at a ledger
transition) with real manifests, gates, the mocked Workflow runtime and scratch Git repositories, then checks
the one rule every helper shares: failure never appears as clean acceptance. The codex adapter's own cases
are added with that adapter; the direct-review lanes are covered here.
"""

import json
import os
import re
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest
from conftest import REPO
from test_deep_review import CLEAN, SKILL, direct, prepare, workflow
from test_reuse_result import review_result
from test_run_attempt import FINDING, classify, manifest_of, result_path, retry, script, write_result
from test_run_attempt import case as attempt_case
from test_work_ledger import case as ledger_case
from test_work_ledger import close, implement, record, start
from test_work_ledger import verify as verifier_result

SCRIPTS = SKILL / "scripts"
REBUILD_GATE = REPO / "skills/rebuild-epoch/scripts/rebuild-gate.sh"
# The two shared fixtures, registered here under their roles (pytest names fixtures by attribute).
review_run = attempt_case
ledger_run = ledger_case
del attempt_case, ledger_case


def git(repo, *args, check=True):
    done = subprocess.run(
        ["git", "-C", str(repo), *args],
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
        env={**os.environ, "GIT_EDITOR": "true", "GIT_SEQUENCE_EDITOR": "true"},
    )
    if check:
        assert done.returncode == 0, done.stderr
    return done


def rev(repo, ref="HEAD"):
    return git(repo, "rev-parse", "--verify", ref).stdout.strip()


def status_of(manifest):
    produced = script("run_status.py", "status", "--manifest", manifest)
    assert produced.stderr == "", produced.stderr
    return json.loads(produced.stdout)


def finalize_only(manifest):
    """finalize without measuring first: what a caller sees when the gate never ran or its evidence is gone."""
    return script("run_gate.py", "finalize", "--manifest", manifest, "--result", result_path(manifest))


def measured(manifest):
    """Run the gate and record its report in the result as the courier would; the result is left unaccepted."""
    report = script("run_gate.py", "run", "--manifest", manifest)
    assert report.returncode == 0, report.stderr
    data = json.loads(result_path(manifest).read_text())
    data["gate"]["report"] = json.loads(report.stdout)
    result_path(manifest).write_text(json.dumps(data))
    return json.loads(report.stdout)


def measured_finalize(manifest):
    """What a live caller does: the gate is measured once (the courier records the report when the result can
    hold one), then finalize reads the files. A result that is not even JSON is handed to finalize as it is."""
    report = script("run_gate.py", "run", "--manifest", manifest)
    path = result_path(manifest)
    if report.returncode == 0 and path.is_file():
        try:
            data = json.loads(path.read_text())
        except ValueError:
            data = None
        if isinstance(data, dict) and isinstance(data.get("gate"), dict):
            data["gate"]["report"] = json.loads(report.stdout)
            path.write_text(json.dumps(data))
    return finalize_only(manifest)


def never_accepted(manifest, finalize, category, retryable=None, refusal=None):
    """The one rule: the state is named, finalize refuses it, nothing writes acceptance.json, status stays pending."""
    report = json.loads(classify(manifest).stdout)
    assert report["category"] == category, report
    if retryable is not None:
        assert report["retryable"] is retryable, report
    refused = finalize(manifest)
    assert refused.returncode != 0, refused.stdout
    if refusal:
        assert refusal in refused.stderr, refused.stderr
    assert not (manifest.parent / "acceptance.json").exists()
    state = status_of(manifest)
    assert state["acceptance"]["status"] == "pending-external-verification" and state["acceptance"]["decidedBy"] is None
    return report, state


# ---- artifact-level fixtures --------------------------------------------------
# Each builder leaves one run directory in the named state and returns what the helpers must say about it:
# the classify category, whether it is retryable, and the finalize refusal. The builder may replace the
# finalize used (when measuring the gate first would itself change the state).

STOPPED_EMPTY = {
    "started": 0,
    "stopReason": "",
    "dispatches": [],
    "barriers": [{"name": "conclude", "dispatched": 0, "open": 0}],
}
TIMED_OUT = "request timed out after 600 s"


def failed_lane(reason, stop="Required reviewer claude-bugs failed"):
    base = review_result({})["admission"]
    return {
        "incomplete": True,
        "coverageComplete": False,
        "successful": [],
        "reviewResults": [],
        "coverage": {"claude-bugs": {"policy": "required", "state": "failed", "reason": reason}},
        "attempts": [{"key": "claude-bugs", "round": 1, "state": "failed", "reason": reason}],
        "degraded": [{"key": "claude-bugs", "reason": f"degraded: {reason}"}],
        "admission": {
            **base,
            "stopReason": stop,
            "dispatches": [{"label": "claude-bugs#1", "phase": "Review", "state": "failed", "reason": reason}],
        },
    }


def wrong_worktree_moved(c):
    """The reviewed checkout is gone, then another tree stands at its path: neither is the frozen input."""
    manifest = c.init()
    write_result(manifest)
    elsewhere = c.repo.with_name("moved-elsewhere")
    c.repo.rename(elsewhere)
    report = json.loads(classify(manifest).stdout)
    assert report["category"] == "stale" and "cannot read Git repository metadata" in report["treeDrift"]
    assert c.finalize(manifest).returncode != 0
    c.repo.mkdir()
    git(c.repo, "init", "-q")
    git(c.repo, "config", "user.email", "fixture@example.invalid")
    git(c.repo, "config", "user.name", "Fixture")
    (c.repo / "file.py").write_text("value = 'another tree'\n")
    git(c.repo, "add", "file.py")
    git(c.repo, "commit", "-qm", "another repository at the same path")
    return "stale", False, "snapshot drift"


def wrong_worktree_result(c):
    """A result produced for one checkout's run lands under the manifest of a second checkout of the same commit."""
    other = c.tmp_path / "other-worktree"
    git(c.repo, "worktree", "add", "-q", "--detach", str(other))
    first = c.init()
    args = {**manifest_of(first)["originalArgs"], "repoRoot": str(other)}
    source = c.tmp_path / "args-other.json"
    source.write_text(json.dumps(args))
    # The repository given to init and the one the arguments name must be the same checkout.
    mismatch = script(
        "run_manifest.py", "init", "--repo-root", c.repo, "--args-file", source,
        "--workflow", c.tmp_path / "workflow.js", "--artifact-dir", c.tmp_path / "runs", "--contract", c.contract,
    )  # fmt: skip
    assert mismatch.returncode != 0 and "args repoRoot does not match repository" in mismatch.stderr
    created = script(
        "run_manifest.py", "init", "--repo-root", other, "--args-file", source,
        "--workflow", c.tmp_path / "workflow.js", "--artifact-dir", c.tmp_path / "runs", "--contract", c.contract,
    )  # fmt: skip
    assert created.returncode == 0, created.stderr
    second = Path(json.loads(created.stdout)["manifestPath"])
    assert manifest_of(second)["snapshotId"] == manifest_of(first)["snapshotId"]  # same content, another checkout
    stored = manifest_of(first)
    run = {"id": stored["runId"], "snapshotId": stored["snapshotId"], "manifestPath": str(first)}
    result_path(second).write_text(json.dumps(review_result(run)))
    c.manifest = second
    return "malformed", False, "does not identify this exact run"


def malformed_bytes(c):
    manifest = c.init()
    result_path(manifest).write_text("{not json")
    return "malformed", False, "JSONDecodeError"


def malformed_not_an_object(c):
    manifest = c.init()
    result_path(manifest).write_text("[]")
    return "malformed", False, "does not identify this exact run"


def schema_valid_but_unusable(c):
    """Every key parses; the successful payload's finding has no location, so it is a replayed or truncated value."""
    manifest = c.init()
    payload = {"findings": [{"file": "file.py"}], "degraded": False, "reason": ""}
    write_result(manifest, reviewResults=[{"key": "claude-bugs", "round": 1, "result": payload}])
    return "malformed", True, "Invalid current finding"


def cites_outside_the_tree(c):
    manifest = c.init()
    write_result(manifest, currentFindings=[{**FINDING, "line_end": 999, "sources": ["claude-bugs"]}])
    return "malformed", True, "does not cite the verified working tree"


def truncated_payload(c):
    """A lane reads succeeded but its full payload was never stored: a display summary saved as the result."""
    manifest = c.init()
    write_result(manifest, reviewResults=[])
    state = status_of(manifest)
    assert state["coverage"]["reviewers"]["claude-bugs"]["payloadStored"] is False
    assert state["coverage"]["payloadsStored"] is False
    return "malformed", True, "lacks its full valid payload"


def timeout_reviewer(c):
    manifest = c.init()
    write_result(manifest, **failed_lane(TIMED_OUT))
    return "transient", True, "Incomplete or modifying run"


def timeout_gate(c):
    manifest = c.init(gate={"commands": [[sys.executable, "-c", "import time; time.sleep(30)"]], "timeoutSeconds": 1})
    report = script("run_gate.py", "run", "--manifest", manifest)
    assert report.returncode == 1 and json.loads(report.stdout)["checks"][0]["status"] == "timeout"
    reason = 'gate did not pass ([..."time.sleep(30)"] timeout)'
    write_result(
        manifest,
        incomplete=True,
        gate={"state": "failed", "reason": reason, "report": json.loads(report.stdout)},
        degraded=[{"key": "gate", "reason": f"degraded: {reason}"}],
    )
    return "blocked", False, "Incomplete or modifying run"


def cancelled_gate(c):
    """The gate helper is terminated while a check runs: a cancelled report, recorded by the controller as failed."""
    command = [sys.executable, "-c", "import os,signal,time; os.kill(os.getppid(),signal.SIGTERM); time.sleep(30)"]
    manifest = c.init(gate={"commands": [command], "timeoutSeconds": 10})
    report = script("run_gate.py", "run", "--manifest", manifest)
    assert report.returncode == 1 and json.loads(report.stdout)["status"] == "cancelled"
    assert not (manifest.parent / "gate.lock").exists()
    reason = "gate did not pass (cancelled; Gate interrupted; no acceptance)"
    write_result(
        manifest,
        incomplete=True,
        gate={"state": "failed", "reason": reason, "report": json.loads(report.stdout)},
        degraded=[{"key": "gate", "reason": f"degraded: {reason}"}],
    )
    return "blocked", False, "Incomplete or modifying run"


def missing_required_reviewer(c):
    manifest = c.init()
    write_result(manifest, **failed_lane("agent failed or returned no object"))
    return "partial", True, "Incomplete or modifying run"


def unavailable_required_reviewer(c):
    manifest = c.init()
    write_result(
        manifest,
        incomplete=True,
        coverageComplete=False,
        successful=[],
        attempted=[],
        attempts=[],
        reviewResults=[],
        coverage={"claude-bugs": {"policy": "required", "state": "unavailable", "reason": "Proxy catalog not checked"}},
        degraded=[{"key": "claude-bugs", "reason": "degraded: Proxy catalog not checked"}],
        admission={**review_result({})["admission"], **STOPPED_EMPTY},
    )
    return "launch-blocked", False, "Incomplete or modifying run"


def unregistered_gate(c):
    """No gate.commands in the request: the controller records a skipped gate and nothing can accept the run."""
    manifest = c.init(gate=None)
    write_result(manifest, gate={"state": "skipped", "reason": "gate not registered", "report": None})
    return "malformed", True, "passed gate"


def missing_gate_report(c):
    """The result claims a passed gate but the helper never wrote gate-result.json: finalize has nothing to bind."""
    manifest = c.init()
    write_result(manifest)
    c.finalize = finalize_only
    return "complete", False, "FileNotFoundError"


def missing_gate_log(c):
    manifest = c.init()
    write_result(manifest)
    measured(manifest)
    (manifest.parent / "gate-0.stdout").unlink()
    c.finalize = finalize_only
    return "complete", False, "Gate output evidence missing or changed"


def missing_args(c):
    manifest = c.init()
    write_result(manifest)
    Path(manifest_of(manifest)["argsFile"]).unlink()
    resumed = script("run_manifest.py", "resume", "--manifest", manifest)
    assert resumed.returncode != 0 and "cannot read generated args" in resumed.stderr
    return "stale", False, "cannot read generated args"


def missing_result(c):
    manifest = c.init()
    assert not result_path(manifest).exists()
    return "missing", False, "FileNotFoundError"


def unexpected_boss_route(c):
    """The result says a boss the request never registered adjudicated it."""
    manifest = c.init()
    write_result(manifest, boss=["gpt-5.6-terra"])
    return "malformed", True, "Boss lanes disagree with the registered request"


def unexpected_policy_route(c):
    manifest = c.init()
    write_result(manifest, coverage={"claude-bugs": {"policy": "optional", "state": "succeeded"}})
    return "malformed", True, "Reviewer policy changed during review"


def unexpected_lane_route(c):
    """A reviewer the request never registered reports success: evidence from a lane nobody asked for."""
    manifest = c.init()
    write_result(manifest, successful=["claude-bugs", "grok"], attempted=["claude-bugs", "grok"])
    return "malformed", True, "Reviewer execution records disagree"


def changed_workflow(c):
    manifest = c.init()
    write_result(manifest)
    Path(manifest_of(manifest)["workflow"]["path"]).write_text("// restructured workflow\n")
    return "stale", False, "workflow changed"


def changed_contract(c):
    manifest = c.init()
    write_result(manifest)
    c.contract.write_text("# stricter rubric\n")
    return "stale", False, "contract changed"


def changed_snapshot(c):
    manifest = c.init()
    write_result(manifest)
    (c.repo / "file.py").write_text("value = 2\n")
    return "stale", False, "snapshot drift"


def changed_arguments(c):
    manifest = c.init()
    write_result(manifest)
    source = Path(manifest_of(manifest)["sourceArgs"]["path"])
    source.write_text(json.dumps({**json.loads(source.read_text()), "focus": "later"}))
    return "stale", False, "source args changed"


def zero_agent_completion(c):
    """Nothing was dispatched and the result still calls itself complete and accepted."""
    manifest = c.init()
    write_result(manifest, acceptance="accepted", admission={**review_result({})["admission"], **STOPPED_EMPTY})
    state = status_of(manifest)
    assert state["execution"]["state"] == "skipped" and state["acceptance"]["resultClaims"] == "accepted"
    return "launch-blocked", False, "Stopped, pending, or empty dispatch"


ARTIFACT_FIXTURES = {
    "wrong-worktree-moved": wrong_worktree_moved,
    "wrong-worktree-result": wrong_worktree_result,
    "malformed-bytes": malformed_bytes,
    "malformed-not-an-object": malformed_not_an_object,
    "schema-valid-but-unusable": schema_valid_but_unusable,
    "cites-outside-the-tree": cites_outside_the_tree,
    "truncated-payload": truncated_payload,
    "timeout-reviewer": timeout_reviewer,
    "timeout-gate": timeout_gate,
    "cancelled-gate": cancelled_gate,
    "missing-required-reviewer": missing_required_reviewer,
    "unavailable-required-reviewer": unavailable_required_reviewer,
    "unregistered-gate": unregistered_gate,
    "missing-gate-report": missing_gate_report,
    "missing-gate-log": missing_gate_log,
    "missing-args": missing_args,
    "missing-result": missing_result,
    "unexpected-boss-route": unexpected_boss_route,
    "unexpected-policy-route": unexpected_policy_route,
    "unexpected-lane-route": unexpected_lane_route,
    "changed-workflow": changed_workflow,
    "changed-contract": changed_contract,
    "changed-snapshot": changed_snapshot,
    "changed-arguments": changed_arguments,
    "zero-agent-completion": zero_agent_completion,
}


@pytest.fixture
def context(review_run, tmp_path):
    repo, init, _, contract = review_run
    c = SimpleNamespace(
        repo=repo, init=init, finalize=measured_finalize, contract=contract, tmp_path=tmp_path, manifest=None
    )

    def remembered(*extra, **overrides):
        c.manifest = init(*extra, **overrides)
        return c.manifest

    c.init = remembered
    # Every lane the controller will run must be registered, or the result's coverage is a different request.
    c.lanes = prepare.prepare(tmp_path, env={})["reviewers"]
    return c


@pytest.mark.parametrize("name", list(ARTIFACT_FIXTURES))
def test_each_failed_state_is_named_and_refused(context, name):
    category, retryable, refusal = ARTIFACT_FIXTURES[name](context)
    never_accepted(context.manifest, context.finalize, category, retryable, refusal)


def test_the_invariant_check_accepts_only_the_clean_control(context):
    """The same check passes a complete, measured, unmodified run, so the refusals above are not vacuous."""
    manifest = context.init()
    write_result(manifest)
    assert json.loads(classify(manifest).stdout)["category"] == "complete"
    accepted = context.finalize(manifest)
    assert accepted.returncode == 0 and json.loads(accepted.stdout)["accepted"] is True
    assert status_of(manifest)["acceptance"]["status"] == "accepted"
    with pytest.raises(AssertionError):
        never_accepted(manifest, context.finalize, "accepted")


def test_a_duplicate_retry_is_refused_and_an_unreturned_retry_is_not_retried(context):
    """Two retries of one attempt would spend two identities on one failure; the bound counts neither."""
    first = context.init()
    write_result(first, **failed_lane(TIMED_OUT))
    second = Path(json.loads(retry(first).stdout)["manifestPath"])
    again = retry(first)
    assert again.returncode == 2 and "already has a replacement" in again.stderr
    assert json.loads(classify(first).stdout)["replacedBy"] == [manifest_of(second)["runId"]]
    # The replacement has not returned: it is neither retried nor abandoned by accident.
    waiting = retry(second)
    assert waiting.returncode == 2 and "not terminal (missing)" in waiting.stderr
    assert [p.name for p in sorted(first.parent.parent.iterdir())] == sorted([first.parent.name, second.parent.name])
    never_accepted(first, context.finalize, "transient", True)
    never_accepted(second, finalize_only, "missing", False)


def test_a_replayed_transient_failure_stays_a_failure_under_every_identity(context):
    """The cached value of a failed attempt is copied into its retry: still transient, never acceptance."""
    first = context.init()
    write_result(first, **failed_lane("HTTP 429 rate limit"))
    cached = result_path(first).read_bytes()
    second = Path(json.loads(retry(first).stdout)["manifestPath"])
    # Byte-for-byte replay: the result names the first run, so it is not evidence for the second.
    result_path(second).write_bytes(cached)
    report, _ = never_accepted(second, context.finalize, "malformed", False)
    assert "another run" in report["reasons"][0]
    # A replay rewritten to the new identity is the same failure again; it earns one more bounded attempt.
    stored = manifest_of(second)
    replay = json.loads(cached)
    replay["run"] = {"id": stored["runId"], "snapshotId": stored["snapshotId"], "manifestPath": str(second)}
    result_path(second).unlink()
    result_path(second).write_text(json.dumps(replay))
    never_accepted(second, context.finalize, "transient", True)
    third = json.loads(retry(second).stdout)
    assert third["attempt"] == 3 and third["cause"] == "transient: degraded: HTTP 429 rate limit"
    assert "last of 3" in retry(third["manifestPath"]).stderr or "not terminal" in retry(third["manifestPath"]).stderr
    # No member of the series is a reusable acceptance for the next one.
    found = script("reuse_result.py", "find", "--manifest", third["manifestPath"])
    assert found.returncode == 1 and json.loads(found.stdout)["reusable"] == []
    assert len(json.loads(found.stdout)["refused"]) == 2


# ---- mocked-runtime fixtures --------------------------------------------------
# The real deep-review.workflow.js runs under the Node mock from test_deep_review, bound to a real manifest,
# and its result is saved where the manifest declares it; the helpers then read that file like any other.

DIRECT = {key: direct(key) for key in prepare.DIRECT}


def mocked(c, responses=None, *, path=None, reviewers=None, **args):
    lanes = {**c.lanes, **(reviewers or {})}
    manifest = c.init(workflow_path=SKILL / "deep-review.workflow.js", reviewers=lanes, **args)
    bound = json.loads((manifest.parent / "args.json").read_text())
    data = workflow(bound, responses, path=path)
    result_path(manifest).write_text(json.dumps(data["result"]))
    return manifest, data


def test_queued_work_never_starts_after_a_runtime_cancellation_and_the_run_is_retryable(context, tmp_path):
    cancelled = "__throw__:request cancelled by the runtime"
    manifest, data = mocked(context, {"astra-standard#1": cancelled}, reviewers=DIRECT, limits={"concurrency": 1})
    labels = [call["opts"]["label"] for call in data["calls"]]
    assert labels == ["claude-bugs#1", "claude-conventions#1", "astra-standard#1"]
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
    report, state = never_accepted(manifest, context.finalize, "transient", True, "Incomplete or modifying run")
    assert report["reasons"][0] == "Terminal failure: request cancelled by the runtime"
    assert state["execution"]["state"] == "cancelled" and state["execution"]["counts"]["cancelled"] == 2
    created = json.loads(retry(manifest).stdout)
    assert created["cause"] == "transient: Terminal failure: request cancelled by the runtime"
    assert created["preserved"]["successfulPayloads"] == [
        {"key": "claude-bugs", "round": 1},
        {"key": "claude-conventions", "round": 1},
    ]
    # Mutant: without the launch-time stop check the queued units start after the stop. The controller is the
    # first line; finalize still refuses the stopped run, so the mutant cannot reach acceptance either.
    source = (SKILL / "deep-review.workflow.js").read_text()
    marker = "if (stopReason) { Object.assign(entry, { state: 'cancelled', reason: stopReason }); return null }"
    assert marker in source
    patched = tmp_path / "no-launch-check.workflow.js"
    patched.write_text(source.replace(marker, ""))
    leaked, data = mocked(
        context, {"astra-standard#1": cancelled}, reviewers=DIRECT, limits={"concurrency": 1}, path=patched
    )
    assert len(data["calls"]) == 5 and not any(
        d["state"] == "cancelled" for d in data["result"]["admission"]["dispatches"]
    )
    never_accepted(leaked, context.finalize, "transient", True, "Incomplete or modifying run")


@pytest.mark.parametrize(
    "reason, category, retryable",
    [
        ("quota exhausted", "transient", True),
        ("HTTP 429 too many requests", "transient", True),
        ("authentication failed (401)", "blocked", False),
    ],
)
def test_an_optional_lane_terminal_failure_names_its_cause_and_decides_the_retry(context, reason, category, retryable):
    """An optional direct lane that hits a terminal failure stops the queue; the recorded stop carries the cause
    the controller saw, so the classifier can tell a quota window from a credential problem."""
    failure = {"findings": [], "degraded": True, "reason": reason}
    manifest, data = mocked(context, {"astra-standard#1": failure}, reviewers=DIRECT, limits={"concurrency": 2})
    assert data["result"]["admission"]["stop"]["reason"] == f"Terminal reviewer failure: {reason}"
    assert len(data["calls"]) == 4 and data["result"]["coverage"]["grok"]["state"] == "cancelled"
    report, _ = never_accepted(manifest, context.finalize, category, retryable, "Incomplete or modifying run")
    assert report["reasons"][0] == f"Terminal reviewer failure: {reason}"
    if retryable:
        assert json.loads(retry(manifest).stdout)["cause"] == f"transient: Terminal reviewer failure: {reason}"
    else:
        assert "blocked result is not retried" in retry(manifest).stderr


def test_a_timed_out_lane_is_incomplete_when_required_and_explicit_reduced_coverage_when_optional(context):
    manifest, data = mocked(context, {"claude-bugs#1": {"findings": [], "degraded": True, "reason": "timeout"}})
    assert data["result"]["incomplete"] and data["result"]["coverage"]["claude-bugs"]["state"] == "failed"
    report, _ = never_accepted(manifest, context.finalize, "transient", True)
    assert json.loads(retry(manifest).stdout)["cause"] == "transient: degraded: timeout"
    # Optional: the run completes, the gap stays visible as coverageComplete false, and acceptance says so.
    reduced, data = mocked(
        context, {"grok#1": {"findings": [], "degraded": True, "reason": "timeout"}}, reviewers=DIRECT
    )
    assert not data["result"]["incomplete"] and not data["result"]["coverageComplete"]
    grok = data["result"]["coverage"]["grok"]
    assert (grok["policy"], grok["state"], grok["reason"]) == ("optional", "failed", "timeout")
    accepted = context.finalize(reduced)
    assert accepted.returncode == 0, accepted.stderr
    assert json.loads(accepted.stdout) == {**json.loads(accepted.stdout), "accepted": True, "coverageComplete": False}


def test_a_direct_lane_runs_only_on_its_registered_route_or_not_at_all(context, monkeypatch):
    """The dispatch carries exactly the registered agent definition and no model override; a catalog that does not
    advertise the requested model makes the lane unavailable instead of routing it elsewhere, and a required lane
    that is unavailable blocks the launch."""
    manifest, data = mocked(context, reviewers={"grok": direct("grok")})
    call = next(c for c in data["calls"] if c["opts"]["label"] == "grok#1")
    assert call["opts"]["agentType"] == DIRECT["grok"]["agentType"] and "model" not in call["opts"]
    assert manifest_of(manifest)["models"]["reviewers"]["grok"]["agentType"] == call["opts"]["agentType"]
    assert json.loads(classify(manifest).stdout)["category"] == "complete"
    monkeypatch.setattr(prepare.shutil, "which", lambda name: None)
    opener = SimpleNamespace(open=lambda request, timeout=None: _bytes({"data": [{"id": "some-other-model"}]}))
    monkeypatch.setattr(prepare.urllib.request, "build_opener", lambda *handlers: opener)
    config = context.tmp_path / "reviewers.json"
    config.write_text(json.dumps({"grok": {"policy": "required"}}))
    setup = prepare.prepare(
        context.tmp_path, config, probe_proxy=True, env={"ANTHROPIC_BASE_URL": "http://127.0.0.1:8319"}
    )
    assert setup["reviewers"]["grok"]["policy"] == "required" and not setup["reviewers"]["grok"]["available"]
    blocked, data = mocked(context, reviewers={"grok": setup["reviewers"]["grok"]})
    assert data["calls"] == [] and data["result"]["admission"]["started"] == 0
    never_accepted(blocked, context.finalize, "launch-blocked", False, "Incomplete or modifying run")


def _bytes(payload):
    import io

    return io.BytesIO(json.dumps(payload).encode())


def test_a_resume_without_the_bound_arguments_cannot_produce_a_run(context):
    """The arguments a resume resends are the manifest's generated object; nothing else is replayable."""
    manifest = context.init(workflow_path=SKILL / "deep-review.workflow.js")
    bound = json.loads((manifest.parent / "args.json").read_text())
    assert "manifest required" in workflow({**bound, "run": None}, succeeds=False)["error"]
    never_accepted(manifest, finalize_only, "missing", False)
    # A reconstruction that borrows another run's identity produces a result that is not this run's.
    other = context.init(workflow_path=SKILL / "deep-review.workflow.js")
    borrowed = {**bound, "run": json.loads((other.parent / "args.json").read_text())["run"]}
    result_path(manifest).write_text(json.dumps(workflow(borrowed)["result"]))
    report, _ = never_accepted(manifest, context.finalize, "malformed", False, "does not identify this exact run")
    assert "another run" in report["reasons"][0]
    resumed = json.loads(script("run_manifest.py", "resume", "--manifest", other).stdout)
    assert resumed["args"] == json.loads((other.parent / "args.json").read_text())


def test_display_shortening_never_shortens_the_stored_result(context):
    """A finding far longer than any log line survives in full in the stored result and in its payload."""
    long_description = "reachable input produces the wrong output " * 200
    finding = {**FINDING, "description": long_description, "recommendation": "guard the empty input"}
    manifest, data = mocked(context, {"claude-bugs#1": {**CLEAN, "findings": [finding]}})
    assert all(len(line) < 400 for line in data["logs"])
    assert not any(long_description in line for line in data["logs"])
    stored = json.loads(result_path(manifest).read_text())
    assert stored["currentFindings"][0]["description"] == long_description
    payload = next(p for p in stored["reviewResults"] if p["key"] == "claude-bugs")
    assert payload["result"]["findings"][0]["description"] == long_description
    assert status_of(manifest)["coverage"]["payloadsStored"] is True
    # A valid finding is a ruling against the tree, which is a decision and not acceptance.
    decided = context.finalize(manifest)
    assert decided.returncode == 1 and json.loads(decided.stdout)["status"] == "changes-required"
    assert json.loads(classify(manifest).stdout)["category"] == "changes-required"


def test_a_run_that_dispatched_nothing_is_launch_blocked_not_complete(context):
    defaults = json.loads((SKILL / "reviewer-defaults.json").read_text())
    nobody = {key: {"policy": "disabled", "available": False} for key in defaults}
    manifest, data = mocked(context, reviewers=nobody)
    assert data["calls"] == [] and data["result"]["incomplete"]
    report, state = never_accepted(manifest, context.finalize, "launch-blocked", False)
    assert state["execution"]["state"] == "skipped" and state["recovery"]["blocking"]["stage"] == "dispatch"


# ---- ledger-transition fixtures ------------------------------------------------


def test_a_crash_at_a_ledger_transition_leaves_the_prior_state_and_never_reads_as_accepted(
    ledger_run, tmp_path, monkeypatch
):
    ledger, _ = ledger_run
    # Crash between start and the worker's return: the unit stays visible and open, nothing else may start.
    start(ledger, "A-1", 1, "implementer")
    assert "A-1 attempt 1 implementer" in start(ledger, "A-2", 1, "implementer", success=False)
    assert close(ledger, "A-1", 1, "implementer", "retry")["state"] == "cancelled"
    assert "late result is not recorded" in record(ledger, "A-1", 1, "implementer", implement(tmp_path), success=False)
    # Crash between the implementer's record and its barrier: the verifier cannot start until the barrier runs.
    assert record(ledger, "A-1", 2, "implementer", implement(tmp_path))["state"] == "in-progress"
    assert "close every open unit" in start(ledger, "A-1", 2, "verifier", success=False)
    close(ledger, "A-1", 2, "implementer", "success")
    assert start(ledger, "A-1", 2, "verifier")["state"] == "running"
    # The verifier crashes: its unit is closed without a verdict, the task is not accepted, a late verdict is refused.
    assert close(ledger, "A-1", 2, "verifier", "cancellation")["state"] == "in-progress"
    assert "late result is not recorded" in record(
        ledger, "A-1", 2, "verifier", verifier_result(tmp_path), success=False
    )
    tasks = json.loads(Path(ledger).read_text())["tasks"]
    assert tasks["A-1"]["state"] == "in-progress" and tasks["A-1"]["verification"] is None
    # A crash after the temp file and before the atomic replace: the ledger on disk is the previous one.
    monkeypatch.syspath_prepend(str(SCRIPTS))
    import work_ledger

    before = Path(ledger).read_bytes()
    with monkeypatch.context() as patch:
        patch.setattr(work_ledger.os, "replace", lambda *args: (_ for _ in ()).throw(OSError("crash")))
        with pytest.raises(OSError, match="crash"):
            work_ledger.main(["start", "--ledger", str(ledger), "--task", "A-1", "--attempt", "3", "--role", "implementer", "--owner", "lead"])  # fmt: skip
    assert Path(ledger).read_bytes() == before
    assert list(Path(ledger).parent.glob(".ledger.json.*")), "the interrupted write is left beside the ledger"
    assert start(ledger, "A-1", 3, "implementer")["state"] == "running"  # the transition was never taken
    # A ledger left half-written by something without the atomic step is refused by every command.
    intact = Path(ledger).read_bytes()
    Path(ledger).write_bytes(intact[: len(intact) // 2])
    for command in (("summary",), ("close", "--task", "A-1", "--attempt", "3", "--role", "implementer", "--outcome", "success", "--owner", "o")):  # fmt: skip
        refused = subprocess.run(
            [sys.executable, str(SCRIPTS / "work_ledger.py"), command[0], "--ledger", str(ledger), *command[1:]],
            capture_output=True,
            text=True,
            timeout=30,
        )
        assert refused.returncode == 1 and "cannot read ledger JSON" in refused.stderr
    Path(ledger).write_bytes(intact)
    assert json.loads(Path(ledger).read_text())["tasks"]["A-1"]["state"] == "running"


# ---- epoch commit, rebase and rebuild ledger boundaries ------------------------


def scratch_stack(root):
    """A linear stack base, c1, c2, c3 on branch `stack`, checked out detached at c1 (epoch 1's HI)."""
    repo = root / "repo"
    repo.mkdir()
    git(repo, "init", "-q")
    git(repo, "config", "user.email", "fixture@example.invalid")
    git(repo, "config", "user.name", "Fixture")
    (repo / "a.txt").write_text("l1\nl2\nl3\n")
    git(repo, "add", "a.txt")
    git(repo, "commit", "-qm", "base")
    base = rev(repo)
    (repo / "a.txt").write_text("l1 epoch1\nl2\nl3\n")
    git(repo, "commit", "-qam", "c1: epoch 1")
    c1 = rev(repo)
    (repo / "b.txt").write_text("x\n")
    git(repo, "add", "b.txt")
    git(repo, "commit", "-qm", "c2: epoch 2")
    (repo / "a.txt").write_text("l1 epoch1\nl2\nl3 c3\n")
    git(repo, "commit", "-qam", "c3: epoch 2")
    git(repo, "branch", "stack")
    git(repo, "checkout", "-q", "--detach", c1)
    return repo, base, c1, rev(repo, "stack")


def freeze(root, repo, name, **args):
    """A manifest for the tree as it stands, with the catalog's trivial gate."""
    source = root / f"{name}-args.json"
    source.write_text(
        json.dumps(
            {
                "repoRoot": str(repo),
                "mode": "uncommitted",
                "fix": False,
                "reviewers": {"claude-bugs": {"policy": "required", "available": True}},
                "gate": {"commands": [[sys.executable, "-c", "print('checked')"]], "timeoutSeconds": 5},
                **args,
            }
        )
    )
    workflow_file = root / "workflow.js"
    workflow_file.write_text("// fixture workflow\n")
    created = script(
        "run_manifest.py", "init", "--repo-root", repo, "--args-file", source,
        "--workflow", workflow_file, "--artifact-dir", root / "runs",
    )  # fmt: skip
    return created


def in_progress(repo):
    git_dir = Path(git(repo, "rev-parse", "--absolute-git-dir").stdout.strip())
    return (git_dir / "rebase-merge").exists() or (git_dir / "rebase-apply").exists()


def reconcile(repo, checkpoint):
    """The written restart rule (epoch-review SKILL.md): compare actual refs with both recorded endpoints before
    acting. Only a clearly completed or clearly unstarted operation may proceed; anything else pauses."""
    pending = checkpoint["pending"]
    if pending is None:
        return "idle"
    if in_progress(repo) or git(repo, "ls-files", "-u").stdout:
        return "paused"
    head, tip = rev(repo), rev(repo, "stack")
    if pending["op"] == "commit":
        if head == pending["before"]["head"]:
            return "not-started"
        parent = rev(repo, head + "^")
        subject = git(repo, "log", "-1", "--format=%s", head).stdout.strip()
        return "completed" if parent == pending["before"]["head"] and subject == pending["subject"] else "paused"
    if tip == pending["before"]["tip"] and head == pending["before"]["head"]:
        return "not-started"
    ancestor = git(repo, "merge-base", "--is-ancestor", pending["onto"], tip, check=False).returncode == 0
    count = int(git(repo, "rev-list", "--count", f"{pending['onto']}..{tip}").stdout)
    return "completed" if ancestor and count == pending["commits"] else "paused"


def test_a_crash_at_an_epoch_commit_or_rebase_boundary_never_carries_acceptance_forward(tmp_path):
    repo, base, c1, c3 = scratch_stack(tmp_path)
    created = freeze(tmp_path, repo, "epoch1")
    assert created.returncode == 0, created.stderr
    epoch1 = Path(json.loads(created.stdout)["manifestPath"])
    write_result(epoch1)
    measured(epoch1)
    accepted = script("run_gate.py", "finalize", "--manifest", epoch1, "--result", result_path(epoch1))
    assert accepted.returncode == 0 and json.loads(accepted.stdout)["accepted"] is True
    checkpoint = {"lo": base, "hi": c1, "cursor": base, "tip": c3, "manifest": str(epoch1), "pending": None}
    # Boundary 1: the fix commit lands, then the process dies before the checkpoint records the new tip.
    subject = "epoch-review: fixes for epoch 1/2"
    checkpoint["pending"] = {"op": "commit", "before": {"head": c1, "tip": c3}, "subject": subject}
    (repo / "a.txt").write_text("l1 epoch1\nl2\nl3 fixed\n")
    git(repo, "commit", "-qam", subject)
    hi_prime = rev(repo)
    assert reconcile(repo, checkpoint) == "completed"
    assert git(repo, "rev-list", "--count", f"{c1}..HEAD").stdout.strip() == "1"  # not repeated on restart
    # The epoch's acceptance belongs to the snapshot at c1; the fixed tree is another input.
    refused = script("run_manifest.py", "verify", "--manifest", epoch1)
    assert refused.returncode != 0 and "snapshot drift" in refused.stderr
    assert finalize_only(epoch1).returncode != 0
    assert json.loads(classify(epoch1).stdout)["category"] == "accepted"  # the old decision stands for the old tree
    fresh = Path(json.loads(freeze(tmp_path, repo, "post-fix").stdout)["manifestPath"])
    reuse = script("reuse_result.py", "check", "--manifest", fresh, "--run-dir", epoch1.parent)
    assert reuse.returncode != 0 and "input differs" in reuse.stderr
    assert json.loads(classify(fresh).stdout)["category"] == "missing"
    checkpoint.update(pending=None, after=hi_prime)
    # Boundary 2: the carry-forward rebase stops partway (one commit replayed, the process gone).
    checkpoint["pending"] = {"op": "rebase", "onto": hi_prime, "before": {"head": hi_prime, "tip": c3}, "commits": 2}
    stopped = git(repo, "rebase", "--onto", hi_prime, c1, "stack", "-x", "false", check=False)
    assert stopped.returncode != 0 and in_progress(repo) and rev(repo, "stack") == c3
    assert reconcile(repo, checkpoint) == "paused"
    half = freeze(tmp_path, repo, "mid-rebase")
    assert half.returncode != 0 and "rebase in progress" in half.stderr
    verified = script("run_manifest.py", "verify", "--manifest", fresh)
    assert verified.returncode != 0 and "rebase in progress" in verified.stderr
    assert "rebase in progress" in json.loads(classify(fresh).stdout)["treeDrift"]
    # Continuing reaches the conflicting pick: still paused, still no frozen input, the tip still untouched.
    conflicted = git(repo, "rebase", "--continue", check=False)
    assert conflicted.returncode != 0 and git(repo, "ls-files", "-u").stdout
    assert reconcile(repo, checkpoint) == "paused" and rev(repo, "stack") == c3
    assert freeze(tmp_path, repo, "conflicted").returncode != 0
    # The owner resolves by hand and finishes; the process dies again before the checkpoint records the tip.
    (repo / "a.txt").write_text("l1 epoch1\nl2\nl3 fixed c3\n")
    git(repo, "add", "a.txt")
    for _ in range(4):
        if not in_progress(repo):
            break
        git(repo, "rebase", "--continue", check=False)
    assert not in_progress(repo) and rev(repo, "stack") != c3
    assert reconcile(repo, checkpoint) == "completed"
    assert git(repo, "rev-list", "--count", f"{hi_prime}..stack").stdout.strip() == "2"
    assert git(repo, "log", "--format=%s", f"{c1}..stack").stdout.split("\n").count(subject) == 1
    # Nothing on the rebased stack inherits the epoch's acceptance: the next epoch is its own frozen input.
    next_epoch = Path(json.loads(freeze(tmp_path, repo, "epoch2").stdout)["manifestPath"])
    found = script("reuse_result.py", "find", "--manifest", next_epoch)
    assert found.returncode == 1 and json.loads(found.stdout)["reusable"] == []


def test_a_rebuild_ledger_transition_follows_only_a_complete_green_gate_report(tmp_path):
    """IMPLEMENTED@sha is written from rebuild-gate.sh's JSON; a report a crash cut short, a ledger a builder's
    add -A swept into the range, or a missing ledger each leave the gate red or unreadable, so no transition."""
    repo = tmp_path / "repo"
    repo.mkdir()
    git(repo, "init", "-q")
    git(repo, "config", "user.email", "fixture@example.invalid")
    git(repo, "config", "user.name", "Fixture")
    (repo / "a.txt").write_text("base\n")
    git(repo, "add", "a.txt")
    git(repo, "commit", "-qm", "base")
    base = rev(repo)
    (repo / "a.txt").write_text("target version of a\n")
    git(repo, "commit", "-qam", "target work")
    git(repo, "branch", "target")
    git(repo, "checkout", "-q", "-b", "rebuilt", base)
    (repo / "a.txt").write_text("rebuilt a\n")
    git(repo, "commit", "-qam", "E1: rebuild a")
    ledger = repo / "REBUILD-EPOCHS.md"
    ledger.write_text("# ledger\n")
    (repo / ".git/info/exclude").write_text("REBUILD-EPOCHS.md\n.rebuild\n")
    report = repo / ".rebuild/gate-epoch-E1.json"

    def gate(ledger_path=ledger):
        done = subprocess.run(
            [
                "bash",
                str(REBUILD_GATE),
                "--repo",
                str(repo),
                "--target",
                "target",
                "--range",
                f"{base}..HEAD",
                "--ledger",
                str(ledger_path),
                "--gate-cmd",
                "true",
                "--no-per-commit",
                "--output",
                str(report),
                "--epoch",
                "E1",
            ],  # fmt: skip
            capture_output=True,
            text=True,
            timeout=60,
        )
        return done.returncode, json.loads(report.read_text())

    code, green = gate()
    assert code == 0 and green["pass"] is True
    assert {c["name"]: c["status"] for c in green["checks"]}["ledger_untracked"] == "ok"
    # A crash while the report was being written: the file is not a report, so no status can be read from it.
    report.write_text('{"pass": true, "checks": [')
    with pytest.raises(json.JSONDecodeError):
        json.loads(report.read_text())
    code, rerun = gate()
    assert code == 0 and rerun["pass"] is True
    # A builder's add -A swept the ledger into the range: the gate is red and names the ledger.
    git(repo, "add", "-f", "REBUILD-EPOCHS.md")
    git(repo, "commit", "-qm", "E1: more")
    code, red = gate()
    assert code == 1 and red["pass"] is False
    check = next(c for c in red["checks"] if c["name"] == "ledger_untracked")
    assert check["status"] == "fail" and "REBUILD-EPOCHS.md" in check["detail"]
    # The ledger file itself is gone: nothing to transition, the gate says so instead of passing.
    code, missing = gate(tmp_path / "nowhere/REBUILD-EPOCHS.md")
    assert code == 1 and missing["pass"] is False
    assert "not found" in next(c for c in missing["checks"] if c["name"] == "ledger_untracked")["detail"]


def test_fixtures_and_references_stay_free_of_dashes_and_name_the_live_gap():
    reference = (SKILL / "references/run-lifecycle.md").read_text()
    assert "test_failure_fixtures.py" in reference and "in progress" in reference
    pattern = re.compile("[\\u2013\\u2014]")
    for path in (Path(__file__), SKILL / "references/run-lifecycle.md", SCRIPTS / "run_manifest.py"):
        assert not pattern.search(path.read_text()), path
