"""Result meaning and bounded retries against real manifests, gates and mocked workflow runs; never a live agent."""

import hashlib
import json
import re
import subprocess
import sys
from pathlib import Path

import pytest
from conftest import REPO
from test_reuse_result import review_result

SKILL = REPO / "skills/deep-review"
SCRIPTS = SKILL / "scripts"
FINDING = {"file": "file.py", "line_start": 1, "line_end": 1, "severity": "warning", "description": "fixture"}


def command(argv):
    return subprocess.run(argv, text=True, capture_output=True, timeout=30, check=False)


def script(name, *args):
    return command([sys.executable, str(SCRIPTS / name), *map(str, args)])


def git(repo, *args):
    assert command(["git", "-C", str(repo), *args]).returncode == 0


def classify(manifest):
    return script("run_attempt.py", "classify", "--manifest", manifest)


def retry(manifest, *flags):
    return script("run_attempt.py", "retry", "--manifest", manifest, *flags)


def manifest_of(path):
    return json.loads(Path(path).read_text())


def result_path(manifest):
    return Path(manifest_of(manifest)["outputs"]["resultPath"])


def write_result(manifest, **patch):
    stored = manifest_of(manifest)
    run = {"id": stored["runId"], "snapshotId": stored["snapshotId"], "manifestPath": str(manifest)}
    path = result_path(manifest)
    path.unlink(missing_ok=True)
    path.write_text(json.dumps({**review_result(run), **patch}))
    return path


def tree_digest(directory):
    return {p.name: hashlib.sha256(p.read_bytes()).hexdigest() for p in Path(directory).iterdir() if p.is_file()}


@pytest.fixture
def case(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    git(repo, "init", "-q")
    git(repo, "config", "user.name", "Fixture")
    git(repo, "config", "user.email", "fixture@example.invalid")
    (repo / "file.py").write_text("value = 1\n")
    git(repo, "add", "file.py")
    git(repo, "commit", "-qm", "fixture")
    workflow = tmp_path / "workflow.js"
    workflow.write_text("// fixture workflow\n")
    contract = tmp_path / "contract.md"
    contract.write_text("# rubric\n")
    counter = iter(range(1000))

    def init(*extra, workflow_path=workflow, **overrides):
        args = {
            "repoRoot": str(repo),
            "mode": "uncommitted",
            "fix": False,
            "reviewers": {"claude-bugs": {"policy": "required", "available": True}},
            "gate": {"commands": [[sys.executable, "-c", "print('checked')"]], "timeoutSeconds": 5},
            **overrides,
        }
        source = tmp_path / f"args-{next(counter)}.json"
        source.write_text(json.dumps(args))
        argv = ["init", "--repo-root", repo, "--args-file", source, "--workflow", workflow_path]
        argv += ["--artifact-dir", tmp_path / "runs", "--contract", contract, *extra]
        created = script("run_manifest.py", *argv)
        assert created.returncode == 0, created.stderr
        return Path(json.loads(created.stdout)["manifestPath"])

    def finalize(manifest):
        script("run_gate.py", "run", "--manifest", manifest)
        return script("run_gate.py", "finalize", "--manifest", manifest, "--result", result_path(manifest))

    return repo, init, finalize, contract


def test_each_result_meaning_is_named_and_none_but_complete_reaches_acceptance(case):
    repo, init, finalize, _ = case
    blocked_launch = {
        "started": 0,
        "stopReason": "",
        "dispatches": [],
        "barriers": [{"name": "conclude", "dispatched": 0, "open": 0}],
    }
    quota = {"incomplete": True, "degraded": [{"key": "claude-bugs", "reason": "degraded: quota exhausted"}]}
    shapes = {
        "transient": (quota, True, True),
        "transient-cancelled": (
            {
                "incomplete": True,
                "admission": {**review_result({})["admission"], "stopReason": "request cancelled by the runtime"},
            },
            True,
            True,
        ),
        "blocked": (
            {"incomplete": True, "degraded": [{"key": "claude-bugs", "reason": "degraded: 401 unauthorized; quota"}]},
            True,
            False,
        ),
        "partial": (
            {
                "incomplete": True,
                "degraded": [{"key": "boss:inherit", "reason": "degraded: missing or invalid boss rulings"}],
            },
            True,
            True,
        ),
        "launch-blocked": (
            {
                "incomplete": True,
                "degraded": [{"key": "grok", "reason": "degraded: Proxy catalog not checked"}],
                "admission": blocked_launch,
            },
            True,
            False,
        ),
        "launch-blocked-transient": (
            {"incomplete": True, "admission": {**blocked_launch, "stopReason": "runtime cancelled before dispatch"}},
            True,
            True,
        ),
        "modified": ({"fixed": True, "requiresPostFixReview": True}, True, False),
        "malformed": ({"coverage": {}}, True, True),
        "complete": ({}, True, False),
    }
    for name, (patch, terminal, retryable) in shapes.items():
        manifest = init()
        write_result(manifest, **patch)
        report = json.loads(classify(manifest).stdout)
        expected = name.split("-transient")[0].split("-cancelled")[0]
        assert report["category"] == expected, (name, report)
        assert (report["terminal"], report["retryable"]) == (terminal, retryable), (name, report)
        assert report["attempt"] == {"number": 1, "priorRunId": None, "cause": None}
        assert report["artifacts"] == {"args": True, "result": True, "patch": False, "gate": False, "acceptance": False}
        assert report["evidence"]["successfulPayloads"] == [{"key": "claude-bugs", "round": 1}]
        assert report["next"]
        if patch.get("degraded"):
            assert report["reasons"][0] == patch["degraded"][0]["reason"]
        if name == "complete":
            assert classify(manifest).returncode == 0
            accepted = finalize(manifest)
            assert accepted.returncode == 0, accepted.stderr
            assert json.loads(classify(manifest).stdout)["category"] == "accepted"
            assert json.loads(classify(manifest).stdout)["artifacts"]["acceptance"] is True
        else:
            assert classify(manifest).returncode == 1
            assert finalize(manifest).returncode != 0, name
            assert not (manifest.parent / "acceptance.json").exists(), name
            assert json.loads(classify(manifest).stdout)["category"] == expected, name
    # A ruling against the tree is a decision, not a retry.
    ruled = init()
    write_result(ruled, currentFindings=[{**FINDING, "sources": ["claude-bugs"]}])
    assert json.loads(finalize(ruled).stdout)["status"] == "changes-required"
    report = json.loads(classify(ruled).stdout)
    assert report["category"] == "changes-required" and report["terminal"] and not report["retryable"]
    assert classify(ruled).returncode == 0
    # Missing: nothing says the attempt ended. Malformed bytes or another run's result: not evidence for this run.
    missing = init()
    report = json.loads(classify(missing).stdout)
    assert report["category"] == "missing" and not report["terminal"] and not report["retryable"]
    assert report["artifacts"]["result"] is False and "--abandon" in report["next"]
    result_path(missing).write_text("{not json")
    assert json.loads(classify(missing).stdout)["category"] == "malformed"
    foreign = init()
    result_path(foreign).write_text(json.dumps(review_result(manifest_of(missing)["runId"])))
    report = json.loads(classify(foreign).stdout)
    assert report["category"] == "malformed" and not report["retryable"] and "another run" in report["reasons"][0]
    # Partial with a dispatch still active is not terminal; stale is terminal but never retried.
    active = init()
    write_result(
        active,
        admission={**review_result({})["admission"], "barriers": [{"name": "conclude", "dispatched": 1, "open": 1}]},
    )
    report = json.loads(classify(active).stdout)
    assert report["category"] == "partial" and not report["terminal"]
    stale = init()
    write_result(stale)
    (repo / "file.py").write_text("value = 2\n")
    report = json.loads(classify(stale).stdout)
    assert report["category"] == "stale" and report["terminal"] and not report["retryable"]
    assert "drift" in report["treeDrift"] and "fresh manifest" in report["next"]
    assert finalize(stale).returncode != 0


def test_mocked_workflow_results_are_classified_from_their_own_record(case, tmp_path):
    from test_deep_review import FINDING as REVIEW_FINDING
    from test_deep_review import direct, prepare, workflow

    _, init, finalize, _ = case
    setup = prepare.prepare(tmp_path, env={})
    runs = {
        "complete": {},
        "transient": {"claude-bugs#1": {"findings": [], "degraded": True, "reason": "quota exhausted"}},
        "partial": {
            "claude-bugs#1": {"findings": [REVIEW_FINDING], "degraded": False, "reason": ""},
            "boss:inherit#1": {"rulings": []},
        },
    }
    for name, responses in runs.items():
        manifest = init(reviewers=setup["reviewers"], workflow_path=SKILL / "deep-review.workflow.js")
        bound = json.loads((manifest.parent / "args.json").read_text())
        result_path(manifest).write_text(json.dumps(workflow(bound, responses)["result"]))
        report = json.loads(classify(manifest).stdout)
        assert report["category"] == name, (name, report)
        assert report["terminal"] is True
        assert report["retryable"] is (name != "complete")
        if name == "complete":
            assert finalize(manifest).returncode == 0
        else:
            assert finalize(manifest).returncode != 0
        if name == "transient":
            # The stop message comes first in the record; the cause names the reason that made it transient.
            assert report["reasons"][0] == "Required reviewer claude-bugs failed"
            created = json.loads(retry(manifest).stdout)
            assert created["cause"] == "transient: degraded: quota exhausted"
            assert created["preserved"]["successfulPayloads"] == [{"key": "claude-conventions", "round": 1}]
    blocked = init(
        reviewers={"grok": direct(policy="required", available=False)}, workflow_path=SKILL / "deep-review.workflow.js"
    )
    bound = json.loads((blocked.parent / "args.json").read_text())
    result_path(blocked).write_text(json.dumps(workflow(bound)["result"]))
    report = json.loads(classify(blocked).stdout)
    assert report["category"] == "launch-blocked" and report["terminal"] and not report["retryable"]
    assert report["evidence"]["successfulPayloads"] == []
    assert retry(blocked).returncode == 2


def test_retry_gives_the_next_attempt_a_new_identity_only_after_a_terminal_failure(case):
    repo, init, finalize, _ = case
    first = init("--task-id", "FLOW-2", "--attempt", "4")
    # Not terminal: no result yet, so nothing says the worker is gone.
    refused = retry(first)
    assert refused.returncode == 2 and "not terminal (missing)" in refused.stderr
    assert not any(p.is_dir() for p in first.parent.parent.iterdir() if p != first.parent)
    write_result(first, incomplete=True, degraded=[{"key": "claude-bugs", "reason": "degraded: HTTP 429 rate limit"}])
    before = tree_digest(first.parent)
    created = retry(first)
    assert created.returncode == 0, created.stderr
    second = json.loads(created.stdout)
    assert second["attempt"] == 2 and second["priorRunId"] == manifest_of(first)["runId"]
    assert second["priorCategory"] == "transient" and second["cause"] == "transient: degraded: HTTP 429 rate limit"
    assert second["replay"] is False
    assert second["preserved"] == {
        "runDir": str(first.parent),
        "resultPath": str(result_path(first)),
        "successfulPayloads": [{"key": "claude-bugs", "round": 1}],
    }
    assert second["runId"] != manifest_of(first)["runId"]
    # The prior run directory is evidence: nothing in it was touched, and the new one is a verified manifest.
    assert tree_digest(first.parent) == before
    stored = manifest_of(second["manifestPath"])
    assert stored["attempt"] == {"number": 2, "priorRunId": manifest_of(first)["runId"], "cause": second["cause"]}
    assert stored["unit"] == {"taskId": "FLOW-2", "attempt": 4}
    assert (
        stored["snapshotId"] == manifest_of(first)["snapshotId"]
        and stored["rubricId"] == manifest_of(first)["rubricId"]
    )
    verified = script("run_manifest.py", "verify", "--manifest", second["manifestPath"])
    assert verified.returncode == 0 and json.loads(verified.stdout)["attempt"]["number"] == 2
    args = json.loads(Path(second["argsFile"]).read_text())
    assert args["run"]["id"] == second["runId"] and args["reviewers"] == manifest_of(first)["originalArgs"]["reviewers"]
    # Repeating the retry of attempt 1 would be a second replacement, which is how a bound gets bypassed.
    report = json.loads(classify(first).stdout)
    assert report["category"] == "transient" and report["replacedBy"] == [second["runId"]]
    again = retry(first)
    assert again.returncode == 2 and "already has a replacement" in again.stderr
    second_manifest = Path(second["manifestPath"])
    assert json.loads(classify(second_manifest).stdout)["replacedBy"] == []
    assert retry(second_manifest, "--max-attempts", "2").returncode == 2
    write_result(second_manifest, incomplete=True, degraded=[{"key": "claude-bugs", "reason": "degraded: quota"}])
    capped = retry(second_manifest, "--max-attempts", "2")
    assert capped.returncode == 2 and "last of 2" in capped.stderr
    third = json.loads(retry(second_manifest).stdout)
    assert third["attempt"] == 3 and third["priorRunId"] == second["runId"]
    write_result(
        Path(third["manifestPath"]), incomplete=True, degraded=[{"key": "claude-bugs", "reason": "degraded: quota"}]
    )
    assert "last of 3" in retry(third["manifestPath"]).stderr
    assert "positive integer" in retry(third["manifestPath"], "--max-attempts", "0").stderr
    # A valid prior result is kept, not replayed: a complete or accepted attempt is never retried.
    done = init()
    write_result(done)
    assert "complete result is not retried" in retry(done).stderr
    assert finalize(done).returncode == 0
    assert "accepted result is not retried" in retry(done).stderr
    # Fixes applied or a tree that moved on: the recovery decision is a fresh manifest, never a retry.
    modified = init()
    write_result(modified, fixed=True, requiresPostFixReview=True)
    assert "modified result is not retried" in retry(modified).stderr
    stale = init()
    write_result(stale, incomplete=True, degraded=[{"key": "claude-bugs", "reason": "degraded: quota"}])
    (repo / "file.py").write_text("value = 2\n")
    refused = retry(stale)
    assert refused.returncode == 2 and "stale result is not retried" in refused.stderr
    assert "--abandon" not in refused.stderr


def test_abandon_is_the_explicit_decision_for_an_attempt_that_never_returned(case):
    repo, init, _, _ = case
    gone = init()
    assert "--abandon needs" in retry(gone, "--abandon", " ").stderr
    created = retry(gone, "--abandon", "harness exited before the result was saved")
    assert created.returncode == 0, created.stderr
    report = json.loads(created.stdout)
    assert report["priorCategory"] == "missing"
    assert report["cause"] == "abandoned (missing): harness exited before the result was saved"
    assert manifest_of(report["manifestPath"])["attempt"]["cause"] == report["cause"]
    # A result that has returned is classified, not abandoned; a moved tree is a fresh manifest either way.
    write_result(gone, incomplete=True, degraded=[{"key": "claude-bugs", "reason": "degraded: quota"}])
    assert "has not returned; this one is transient" in retry(gone, "--abandon", "x").stderr
    lost = init()
    (repo / "file.py").write_text("value = 3\n")
    refused = retry(lost, "--abandon", "x")
    assert refused.returncode == 2 and "inputs changed" in refused.stderr and "snapshot drift" in refused.stderr


def test_a_changed_rubric_or_request_cannot_silently_continue_the_series(case, monkeypatch):
    """A restructured script, an edited contract or a changed argument file is a new request. The series does not
    continue under it: the retry is refused and the explicit decision is a fresh manifest with run_manifest.py init."""
    _, init, _, contract = case
    first = init()
    write_result(first, incomplete=True, degraded=[{"key": "claude-bugs", "reason": "degraded: quota"}])
    stored = manifest_of(first)
    workflow = Path(stored["workflow"]["path"])
    source = Path(stored["sourceArgs"]["path"])
    original = source.read_text()
    for label, path, edited in (
        ("contract", contract, "# stricter rubric\n"),
        ("workflow", workflow, "// restructured workflow\n"),
        ("source args", source, json.dumps({**json.loads(original), "focus": "later"})),
    ):
        keep = path.read_text()
        path.write_text(edited)
        report = json.loads(classify(first).stdout)
        assert report["category"] == "stale" and f"{label} changed" in report["treeDrift"], (label, report)
        refused = retry(first)
        assert refused.returncode == 2 and "stale result is not retried" in refused.stderr
        assert "fresh manifest" in refused.stderr
        path.write_text(keep)
        assert json.loads(classify(first).stdout)["category"] == "transient"
    assert not any(p.is_dir() for p in first.parent.parent.iterdir() if p != first.parent)
    # Inputs that move between the classification and the new manifest make the new directory a record of a
    # different request, named in the refusal and never returned as the replacement.
    monkeypatch.syspath_prepend(str(SCRIPTS))
    import run_attempt

    real = run_attempt.classify

    def then_edit(path):
        report = real(path)
        contract.write_text("# edited during the retry\n")
        return report

    monkeypatch.setattr(run_attempt, "classify", then_edit)
    with pytest.raises(run_attempt.ManifestError, match="is not a replacement"):
        run_attempt.retry(first)
    contract.write_text("# rubric\n")
    orphan = [p for p in first.parent.parent.iterdir() if p.is_dir() and p != first.parent]
    assert len(orphan) == 1
    assert json.loads(classify(first).stdout)["replacedBy"] == [manifest_of(orphan[0] / "manifest.json")["runId"]]
    assert "already has a replacement" in retry(first).stderr


def test_helper_reads_artifacts_and_never_wakes_an_agent():
    source = (SCRIPTS / "run_attempt.py").read_text()
    imports = set(re.findall(r"^(?:from (\S+) import|import (\S+))", source, flags=re.M))
    modules = {a or b for a, b in imports}
    assert modules == {"argparse", "json", "re", "sys", "pathlib", "types", "run_gate", "run_manifest"}
    # It names run_manifest.py resume as a next step for a missing result; it never performs one.
    for word in ("resumeFromRunId", "resume(", "agent(", "subprocess", "requests", "urllib"):
        assert word not in source, word


def test_reference_names_each_meaning_and_the_retry_rule():
    reference = (SKILL / "references/result-meaning.md").read_text()
    for phrase in (
        'run_attempt.py" classify',
        'run_attempt.py" retry',
        "--max-attempts",
        "--abandon",
        "replacedBy",
        "`transient`",
        "`partial`",
        "`launch-blocked`",
        "`blocked`",
        "`stale`",
        "`modified`",
        "`malformed`",
        "`missing`",
        "`complete`",
        "`accepted`",
        "`changes-required`",
        "priorRunId",
        "new run id",
        "acceptance.json",
    ):
        assert phrase in reference, phrase
    assert "result-meaning.md" in (SKILL / "references/run-lifecycle.md").read_text()
    assert "result-meaning.md" in (SKILL / "references/result-reuse.md").read_text()
    assert "run_attempt.py" in (SKILL / "SKILL.md").read_text()
    for path in (SCRIPTS / "run_attempt.py", SKILL / "references/result-meaning.md", Path(__file__)):
        assert not {"\u2014", "\u2013"} & set(path.read_text()), path
