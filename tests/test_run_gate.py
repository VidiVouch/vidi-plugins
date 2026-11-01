"""Exercise real local commands with synthetic repositories, never model calls."""

import importlib
import json
import os
import signal
import subprocess
import sys
from pathlib import Path

import pytest
from conftest import REPO

SCRIPTS = REPO / "skills/deep-review/scripts"
# The workflow's own defaults, which a result must echo when the request registered none.
ADMISSION = {"limits": {"concurrency": 3, "maxAgents": 30, "minRemainingTokens": 0}, "maxRounds": 3}
FINDING = {"file": "file.py", "line_start": 1, "line_end": 1, "severity": "nit", "description": "fixture"}


def command(argv, *, cwd=None):
    return subprocess.run(argv, cwd=cwd, text=True, capture_output=True, timeout=20, check=False)


@pytest.fixture
def run(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    for args in (
        ["init", "-q"],
        ["config", "user.name", "Fixture"],
        ["config", "user.email", "fixture@example.invalid"],
    ):
        assert command(["git", "-C", str(repo), *args]).returncode == 0
    (repo / "file.py").write_text("value = 1\n")
    assert command(["git", "-C", str(repo), "add", "file.py"]).returncode == 0
    assert command(["git", "-C", str(repo), "commit", "-qm", "fixture"]).returncode == 0
    workflow = tmp_path / "workflow.js"
    workflow.write_text("// fixture workflow\n")

    def initialize(commands=None, timeout=5, reviewers=None, workflow_path=None, **extra):
        args = {
            "repoRoot": str(repo),
            "mode": "uncommitted",
            "fix": False,
            "reviewers": reviewers or {"claude-bugs": {"policy": "required", "available": True}},
            "gate": {"commands": commands or [[sys.executable, "-c", "print('checked')"]], "timeoutSeconds": timeout},
            **extra,
        }
        source = tmp_path / f"args-{len(list(tmp_path.glob('args-*.json')))}.json"
        source.write_text(json.dumps(args))
        created = command(
            [
                sys.executable,
                str(SCRIPTS / "run_manifest.py"),
                "init",
                "--repo-root",
                str(repo),
                "--args-file",
                str(source),
                "--workflow",
                str(workflow_path or workflow),
                "--artifact-dir",
                str(tmp_path / "runs"),
            ]
        )
        assert created.returncode == 0, created.stderr
        output = json.loads(created.stdout)
        manifest = Path(output["manifestPath"])
        result = {
            "run": {"id": output["runId"], "snapshotId": output["snapshotId"], "manifestPath": str(manifest)},
            "mode": "uncommitted",
            "rounds": 1,
            "boss": ["inherit"],
            "incomplete": False,
            "fixed": False,
            "requiresPostFixReview": False,
            "admission": {
                **ADMISSION,
                "started": 1,
                "stopReason": "",
                "dispatches": [{"label": "claude-bugs#1", "phase": "Review", "state": "returned"}],
                "barriers": [{"name": "conclude", "dispatched": 1, "open": 0}],
            },
            "coverage": {"claude-bugs": {"policy": "required", "state": "succeeded"}},
            "successful": ["claude-bugs"],
            "attempted": ["claude-bugs"],
            "coverageComplete": True,
            "attempts": [{"key": "claude-bugs", "round": 1, "state": "succeeded", "reason": ""}],
            "currentFindings": [],
            "degraded": [],
            "acceptance": "pending-external-verification",
            "reviewResults": [
                {"key": "claude-bugs", "round": 1, "result": {"findings": [], "degraded": False, "reason": ""}}
            ],
        }
        result_path = manifest.parent / "review-result.json"
        result_path.write_text(json.dumps(result))
        return repo, manifest, result_path

    return initialize


def gate(manifest, operation="run", result=None):
    args = [sys.executable, str(SCRIPTS / "run_gate.py"), operation, "--manifest", str(manifest)]
    if result:
        args += ["--result", str(result)]
    return command(args)


def test_mocked_workflow_result_finalizes_against_real_manifest(run, tmp_path):
    from test_deep_review import SKILL, prepare, workflow

    setup = prepare.prepare(tmp_path, env={})
    _, manifest, result = run(reviewers=setup["reviewers"], workflow_path=SKILL / "deep-review.workflow.js")
    bound_args = json.loads((manifest.parent / "args.json").read_text())
    completed = workflow(bound_args)["result"]
    result.write_text(json.dumps(completed))
    assert gate(manifest).returncode == 0
    accepted = gate(manifest, "finalize", result)
    assert accepted.returncode == 0, accepted.stderr
    assert json.loads(accepted.stdout)["accepted"] is True
    assert json.loads(accepted.stdout)["coverageComplete"] is False


def test_clean_review_needs_measured_gate(run):
    _, manifest, result = run()
    assert gate(manifest, "finalize", result).returncode != 0
    measured = gate(manifest)
    assert measured.returncode == 0, measured.stderr
    assert json.loads(measured.stdout)["checks"][0]["exitCode"] == 0
    accepted = gate(manifest, "finalize", result)
    assert accepted.returncode == 0, accepted.stderr
    assert json.loads(accepted.stdout)["accepted"] is True
    assert gate(manifest).returncode != 0  # no accidental repeated command execution


def test_failing_gate_stops_next_command(run):
    _, manifest, result = run(
        [
            [sys.executable, "-c", "raise SystemExit(4)"],
            [sys.executable, "-c", "raise AssertionError('must not run')"],
        ]
    )
    measured = gate(manifest)
    assert measured.returncode == 1
    payload = json.loads(measured.stdout)
    assert len(payload["checks"]) == 1 and payload["checks"][0]["exitCode"] == 4
    assert gate(manifest, "finalize", result).returncode != 0


def test_gate_cannot_mutate_reviewed_code(run):
    _, manifest, result = run(
        [[sys.executable, "-c", "from pathlib import Path; Path('file.py').write_text('changed')"]]
    )
    measured = gate(manifest)
    assert measured.returncode != 0
    assert json.loads(measured.stdout)["status"] == "failed"
    assert gate(manifest, "finalize", result).returncode != 0


def test_timeout_is_not_a_pass(run):
    _, manifest, result = run([[sys.executable, "-c", "import time; time.sleep(30)"]], timeout=1)
    measured = gate(manifest)
    assert measured.returncode == 1
    assert json.loads(measured.stdout)["checks"][0]["status"] == "timeout"
    assert gate(manifest, "finalize", result).returncode != 0


@pytest.mark.skipif(os.name != "posix", reason="POSIX process groups")
def test_sigterm_cleans_up_and_records_cancellation(run):
    _, manifest, result = run(
        [[sys.executable, "-c", "import os,signal,time; os.kill(os.getppid(),signal.SIGTERM); time.sleep(30)"]]
    )
    measured = gate(manifest)
    assert measured.returncode == 1
    assert json.loads(measured.stdout)["status"] == "cancelled"
    assert not (manifest.parent / "gate.lock").exists()
    assert gate(manifest, "finalize", result).returncode != 0


@pytest.mark.parametrize(
    "patch",
    [
        {"run": {}},
        {"incomplete": True},
        {"fixed": True},
        {"requiresPostFixReview": True},
        {"successful": []},
        {"attempted": []},
        {"attempts": []},
        {"currentFindings": None},
        {"coverage": {"claude-bugs": {"policy": "required", "state": "failed"}}},
        {"coverage": {"claude-bugs": {"policy": "optional", "state": "succeeded"}}},
        {"currentFindings": [{"severity": "bogus"}]},
        {"currentFindings": [{"severity": "nit"}]},
        {"reviewResults": []},
        {"coverageComplete": False},
        {"degraded": [{"key": "boss:inherit", "reason": "failed"}]},
        {"admission": {**ADMISSION, "started": 0, "stopReason": "", "dispatches": []}},
        {"admission": {**ADMISSION, "started": 1, "stopReason": "quota", "dispatches": [{"state": "returned"}]}},
        {"admission": {**ADMISSION, "started": 1, "stopReason": "", "dispatches": [{"state": "running"}]}},
        {"admission": {**ADMISSION, "started": 2, "stopReason": "", "dispatches": [{"state": "returned"}]}},
        # Cleanup barriers: missing, one that found an active unit, or a conclude that did not count every dispatch.
        {
            "admission": {
                **ADMISSION,
                "started": 1,
                "stopReason": "",
                "dispatches": [{"label": "claude-bugs#1", "phase": "Review", "state": "returned"}],
            }
        },
        {
            "admission": {
                **ADMISSION,
                "started": 1,
                "stopReason": "",
                "dispatches": [{"label": "claude-bugs#1", "phase": "Review", "state": "returned"}],
                "barriers": [
                    {"name": "review#1", "dispatched": 1, "open": 1},
                    {"name": "conclude", "dispatched": 1, "open": 0},
                ],
            }
        },
        {
            "admission": {
                **ADMISSION,
                "started": 1,
                "stopReason": "",
                "dispatches": [{"label": "claude-bugs#1", "phase": "Review", "state": "returned"}],
                "barriers": [{"name": "review#1", "dispatched": 0, "open": 0}],
            }
        },
        {"currentFindings": [{**FINDING, "file": "../outside.py", "sources": ["claude-bugs"]}]},
        {"currentFindings": [{**FINDING, "line_end": 999, "sources": ["claude-bugs"]}]},
        # Scope: the result must echo the registered boss lanes, round cap and admission limits.
        {"boss": ["other-boss"]},
        {"boss": "inherit"},
        {"rounds": 0},
        {"rounds": 4},
        {"rounds": None},
        {"admission": {"started": 1, "stopReason": "", "dispatches": [{"state": "returned"}], "barriers": []}},
        # Required evidence: a current finding must be attributed to a reviewer that succeeded.
        {"currentFindings": [FINDING]},
        {"currentFindings": [{**FINDING, "sources": []}]},
        {"currentFindings": [{**FINDING, "sources": ["claude-conventions"]}]},
        # Useful payloads: a successful payload whose findings are unusable is a replayed or truncated value.
        {
            "reviewResults": [
                {
                    "key": "claude-bugs",
                    "round": 1,
                    "result": {"findings": [{"file": "file.py"}], "degraded": False, "reason": ""},
                }
            ]
        },
        {
            "reviewResults": [
                {"key": "claude-bugs", "round": 1, "result": {"findings": [None], "degraded": False, "reason": ""}}
            ]
        },
    ],
)
def test_semantically_invalid_review_is_rejected(run, patch):
    _, manifest, result = run()
    assert gate(manifest).returncode == 0
    result.write_text(json.dumps({**json.loads(result.read_text()), **patch}))
    assert gate(manifest, "finalize", result).returncode != 0
    assert not (manifest.parent / "acceptance.json").exists()


@pytest.mark.parametrize("artifact", ["review", "gate"])
def test_artifact_replacement_during_finalize_cannot_be_certified(run, monkeypatch, artifact):
    _, manifest, result = run()
    assert gate(manifest).returncode == 0
    monkeypatch.syspath_prepend(str(SCRIPTS))
    module = importlib.import_module("run_gate")
    real_verify = module.verify_manifest
    calls = 0

    def replace_after_validation(path):
        nonlocal calls
        verified = real_verify(path)
        calls += 1
        if calls == 2:
            target = result if artifact == "review" else manifest.parent / "gate-result.json"
            data = json.loads(target.read_text())
            data.update({"incomplete": True} if artifact == "review" else {"status": "failed"})
            target.write_text(json.dumps(data))
        return verified

    monkeypatch.setattr(module, "verify_manifest", replace_after_validation)
    with pytest.raises(module.ManifestError, match="changed during finalization"):
        module.finalize(manifest, result)
    assert not (manifest.parent / "acceptance.json").exists()


def test_success_requires_a_matching_returned_review_dispatch(run):
    _, manifest, result = run()
    assert gate(manifest).returncode == 0
    data = json.loads(result.read_text())
    data["admission"]["dispatches"] = [{"label": "unrelated-reviewer#999", "phase": "Review", "state": "failed"}]
    result.write_text(json.dumps(data))
    assert gate(manifest, "finalize", result).returncode != 0
    assert not (manifest.parent / "acceptance.json").exists()


@pytest.mark.skipif(os.name != "posix", reason="POSIX signals and process groups")
def test_repeated_sigterm_cannot_interrupt_owned_process_cleanup(run, tmp_path):
    script = tmp_path / "repeated-cancellation.py"
    script.write_text(
        "import os,signal,time\n"
        "signal.signal(signal.SIGTERM, signal.SIG_IGN)\n"
        "print(os.getpid(), flush=True)\n"
        "os.kill(os.getppid(), signal.SIGTERM)\n"
        "time.sleep(0.2)\n"
        "os.kill(os.getppid(), signal.SIGTERM)\n"
        "time.sleep(30)\n"
    )
    _, manifest, _ = run([[sys.executable, str(script)]])
    measured = gate(manifest)
    pid = int((manifest.parent / "gate-0.stdout").read_text().strip())
    try:
        assert measured.returncode == 1
        assert json.loads(measured.stdout)["status"] == "cancelled"
        with pytest.raises(ProcessLookupError):
            os.kill(pid, 0)
    finally:
        # Repro cleanup must only target this fixture, even if the regression returns.
        process = command(["ps", "-p", str(pid), "-o", "command="])
        if str(script) in process.stdout:
            os.killpg(pid, signal.SIGKILL)


@pytest.mark.skipif(os.name != "posix", reason="POSIX process groups")
def test_gate_children_are_registered_for_the_unit_barrier_and_their_cleanup_is_observable(run, tmp_path, monkeypatch):
    """With --registry every gate child is recorded in its own group before it is waited on; after the gate,
    whether it finished or timed out, the barrier's stop finds nothing of it left and reports it gone."""
    monkeypatch.syspath_prepend(str(SCRIPTS))
    owned = importlib.import_module("owned_processes")
    registry = tmp_path / "processes-implementer-1.json"
    _, manifest, _ = run([[sys.executable, "-c", "import os; print(os.getpid())"]])
    measured = command(
        [sys.executable, str(SCRIPTS / "run_gate.py"), "run", "--manifest", str(manifest), "--registry", str(registry)]
    )
    assert measured.returncode == 0, measured.stderr
    report = json.loads(measured.stdout)
    pid = int((manifest.parent / "gate-0.stdout").read_text().strip())
    assert report["registry"] == str(registry)
    assert report["checks"][0]["process"] == {
        "pid": pid,
        "pgid": pid,
        "started": report["checks"][0]["process"]["started"],
    }
    assert owned.load(registry) == [report["checks"][0]["process"]]
    assert (
        "applies to run"
        in command(
            [
                sys.executable,
                str(SCRIPTS / "run_gate.py"),
                "finalize",
                "--manifest",
                str(manifest),
                "--result",
                "x",
                "--registry",
                "r",
            ]
        ).stderr
    )
    # A timed-out child is stopped by the gate and recorded in the same registry; the barrier then reports both gone.
    _, slow, _ = run(
        [[sys.executable, "-c", "import os; print(os.getpid(), flush=True); import time; time.sleep(30)"]], timeout=1
    )
    measured = command(
        [sys.executable, str(SCRIPTS / "run_gate.py"), "run", "--manifest", str(slow), "--registry", str(registry)]
    )
    assert measured.returncode == 1 and json.loads(measured.stdout)["checks"][0]["status"] == "timeout"
    slow_pid = int((slow.parent / "gate-0.stdout").read_text().strip())
    try:
        with pytest.raises(ProcessLookupError):
            os.kill(slow_pid, 0)
        assert [(p["pid"], p["outcome"]) for p in owned.stop(registry)] == [(pid, "gone"), (slow_pid, "gone")]
        assert owned.load(registry)[1]["pid"] == slow_pid  # the registry stays as evidence
    finally:
        if str(sys.executable) in command(["ps", "-p", str(slow_pid), "-o", "command="]).stdout:
            os.killpg(slow_pid, signal.SIGKILL)


def test_valid_findings_are_changes_required_not_accepted(run):
    _, manifest, result = run()
    assert gate(manifest).returncode == 0
    data = json.loads(result.read_text())
    data["currentFindings"] = [{**FINDING, "severity": "warning", "sources": ["claude-bugs"]}]
    result.write_text(json.dumps(data))
    checked = gate(manifest, "finalize", result)
    assert checked.returncode == 1
    assert json.loads(checked.stdout)["status"] == "changes-required"


def test_registered_limits_and_rounds_are_the_scope_a_result_must_echo(run):
    """A request that registers its own limits and round cap accepts only a result that ran under them."""
    _, manifest, result = run(limits={"concurrency": 1}, maxRounds=2)
    assert gate(manifest).returncode == 0
    data = json.loads(result.read_text())
    refused = gate(manifest, "finalize", result)
    assert refused.returncode == 2 and "Admission limits" in refused.stderr
    data["admission"].update(limits={"concurrency": 1, "maxAgents": 30, "minRemainingTokens": 0}, maxRounds=2)
    data["rounds"] = 2
    # A nit-only current finding attributed to the successful reviewer, with its usable payload, is still clean.
    data["currentFindings"] = [{**FINDING, "sources": ["claude-bugs"]}]
    data["reviewResults"][0]["result"]["findings"] = [FINDING]
    result.write_text(json.dumps(data))
    accepted = gate(manifest, "finalize", result)
    assert accepted.returncode == 0, accepted.stderr
    assert json.loads(accepted.stdout)["accepted"] is True


def test_changed_gate_logs_or_snapshot_invalidate_acceptance(run):
    repo, manifest, result = run()
    assert gate(manifest).returncode == 0
    (manifest.parent / "gate-0.stdout").write_text("not the captured output")
    assert gate(manifest, "finalize", result).returncode != 0
    (repo / "file.py").write_text("different snapshot\n")
    assert gate(manifest, "finalize", result).returncode != 0
