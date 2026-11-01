"""Accepted-result reuse against real manifests, gates and acceptance files; never a model or an agent."""

import json
import re
import shutil
import subprocess
import sys
from pathlib import Path

import pytest
from conftest import REPO

SKILL = REPO / "skills/deep-review"
SCRIPTS = SKILL / "scripts"


def command(argv, *, cwd=None):
    return subprocess.run(argv, cwd=cwd, text=True, capture_output=True, timeout=20, check=False)


def git(repo, *args):
    assert command(["git", "-C", str(repo), *args]).returncode == 0


def script(name, *args):
    return command([sys.executable, str(SCRIPTS / name), *args])


def review_result(run, findings=()):
    return {
        "run": run,
        "mode": "uncommitted",
        "rounds": 1,
        "boss": ["inherit"],
        "incomplete": False,
        "fixed": False,
        "requiresPostFixReview": False,
        "admission": {
            "limits": {"concurrency": 3, "maxAgents": 30, "minRemainingTokens": 0},
            "maxRounds": 3,
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
        "currentFindings": [{**f, "sources": ["claude-bugs"]} for f in findings],
        "degraded": [],
        "acceptance": "pending-external-verification",
        "reviewResults": [
            {"key": "claude-bugs", "round": 1, "result": {"findings": list(findings), "degraded": False, "reason": ""}}
        ],
    }


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
    runs = tmp_path / "runs"
    counter = iter(range(1000))

    def init(repo_root=repo, workflow_path=workflow, contracts=(contract,), findings=(), **overrides):
        args = {
            "repoRoot": str(repo_root),
            "mode": "uncommitted",
            "fix": False,
            "reviewers": {"claude-bugs": {"policy": "required", "available": True}},
            "gate": {"commands": [[sys.executable, "-c", "print('checked')"]], "timeoutSeconds": 5},
            **overrides,
        }
        source = tmp_path / f"args-{next(counter)}.json"
        source.write_text(json.dumps(args))
        argv = ["init", "--repo-root", str(repo_root), "--args-file", str(source)]
        argv += ["--workflow", str(workflow_path), "--artifact-dir", str(runs)]
        for path in contracts:
            argv += ["--contract", str(path)]
        created = script("run_manifest.py", *argv)
        assert created.returncode == 0, created.stderr
        output = json.loads(created.stdout)
        manifest = Path(output["manifestPath"])
        run = {"id": output["runId"], "snapshotId": output["snapshotId"], "manifestPath": str(manifest)}
        (manifest.parent / "review-result.json").write_text(json.dumps(review_result(run, findings)))
        return manifest

    def accept(manifest):
        assert script("run_gate.py", "run", "--manifest", str(manifest)).returncode == 0
        finalized = script(
            "run_gate.py",
            "finalize",
            "--manifest",
            str(manifest),
            "--result",
            str(manifest.parent / "review-result.json"),
        )
        return finalized

    return repo, init, accept


def check(manifest, prior):
    return script("reuse_result.py", "check", "--manifest", str(manifest), "--run-dir", str(prior.parent))


def find(manifest, *extra):
    return script("reuse_result.py", "find", "--manifest", str(manifest), *extra)


def test_accepted_prior_run_is_reused_only_for_the_same_input_and_rubric(case, tmp_path):
    repo, init, accept = case
    prior = init()
    assert json.loads(accept(prior).stdout)["accepted"] is True
    current = init()
    one = check(current, prior)
    assert one.returncode == 0, one.stderr
    report = json.loads(one.stdout)
    assert report["reusable"] is True and report["coverageComplete"] is True
    assert report["runId"] == json.loads(prior.read_text())["runId"]
    assert report["acceptancePath"] == str(prior.parent / "acceptance.json")
    assert Path(report["resultPath"]).read_bytes() == (prior.parent / "review-result.json").read_bytes()
    many = find(current)
    assert many.returncode == 0, many.stderr
    listing = json.loads(many.stdout)
    assert [r["runDir"] for r in listing["reusable"]] == [str(prior.parent)]
    assert listing["refused"] == [] and listing["runId"] == json.loads(current.read_text())["runId"]
    # The same content at another path is the same input: the frozen worktree's location is not the request.
    copy = tmp_path / "copy"
    shutil.copytree(repo, copy, symlinks=True)
    elsewhere = init(repo_root=copy)
    assert check(elsewhere, prior).returncode == 0
    # The current run is never its own prior result, and a start marker alone is refused.
    own = check(current, current)
    assert own.returncode == 1 and "current run" in own.stderr
    unfinalized = check(elsewhere, current)
    assert unfinalized.returncode == 1 and "started, interrupted or degraded" in unfinalized.stderr


@pytest.mark.parametrize(
    "shape, expected",
    [
        ("started", "started, interrupted or degraded run proves nothing"),
        ("degraded", "started, interrupted or degraded run proves nothing"),
        ("gated-only", "started, interrupted or degraded run proves nothing"),
        ("changes-required", "not accepted: changes-required"),
        ("result-replaced", "changed since acceptance"),
        ("gate-replaced", "changed since acceptance"),
        ("acceptance-forged", "changed since acceptance"),
        ("manifest-edited", "prior manifest is not intact"),
    ],
)
def test_markers_and_altered_evidence_cannot_prove_acceptance(case, shape, expected):
    _, init, accept = case
    prior = init(
        findings=[{"file": "file.py", "line_start": 1, "line_end": 1, "severity": "warning", "description": "x"}]
        if shape == "changes-required"
        else ()
    )
    result = prior.parent / "review-result.json"
    if shape == "degraded":
        degraded = {
            **json.loads(result.read_text()),
            "incomplete": True,
            "degraded": [{"key": "claude-bugs", "reason": "quota"}],
        }
        result.write_text(json.dumps(degraded))
    elif shape == "gated-only":
        assert script("run_gate.py", "run", "--manifest", str(prior)).returncode == 0
    elif shape == "changes-required":
        assert json.loads(accept(prior).stdout)["status"] == "changes-required"
    elif shape in {"result-replaced", "gate-replaced", "acceptance-forged"}:
        assert json.loads(accept(prior).stdout)["accepted"] is True
        if shape == "result-replaced":
            # A replaced result that still looks clean: the digest binding, not the content, decides.
            result.write_text(json.dumps({**json.loads(result.read_text()), "replayed": True}))
        elif shape == "gate-replaced":
            gate = prior.parent / "gate-result.json"
            gate.write_text(gate.read_text() + "\n")
        else:
            acceptance = prior.parent / "acceptance.json"
            forged = {**json.loads(acceptance.read_text()), "resultSha256": "0" * 64}
            acceptance.write_text(json.dumps(forged))
    elif shape == "manifest-edited":
        assert json.loads(accept(prior).stdout)["accepted"] is True
        edited = json.loads(prior.read_text())
        edited["originalArgs"]["focus"] = "later"
        prior.chmod(0o644)
        prior.write_text(json.dumps(edited))
    current = init()
    refused = check(current, prior)
    assert refused.returncode == 1 and expected in refused.stderr, refused.stderr
    listing = find(current)
    assert listing.returncode == 1
    assert json.loads(listing.stdout)["refused"] == [
        {"runDir": str(prior.parent), "reason": refused.stderr.split(": ", 1)[1].strip()}
    ]


@pytest.mark.parametrize(
    "change, expected",
    [
        ("input", "input differs"),
        ("contract", "rubric differs"),
        ("workflow", "rubric differs"),
        ("reviewers", "rubric differs"),
        ("focus", "rubric differs"),
        ("gate", "rubric differs"),
    ],
)
def test_changed_input_or_rubric_refuses_reuse(case, tmp_path, change, expected):
    repo, init, accept = case
    prior = init()
    assert json.loads(accept(prior).stdout)["accepted"] is True
    overrides = {}
    if change == "input":
        (repo / "file.py").write_text("value = 2\n")
    elif change == "contract":
        other = tmp_path / "contract-2.md"
        other.write_text("# stricter rubric\n")
        overrides["contracts"] = (other,)
    elif change == "workflow":
        other = tmp_path / "workflow-2.js"
        other.write_text("// restructured\n")
        overrides["workflow_path"] = other
    elif change == "reviewers":
        overrides["reviewers"] = {
            "claude-bugs": {"policy": "required", "available": True},
            "grok": {"policy": "optional", "available": False},
        }
    elif change == "focus":
        overrides["focus"] = "concurrency"
    else:
        overrides["gate"] = {"commands": [[sys.executable, "-c", "print('other')"]], "timeoutSeconds": 5}
    current = init(**overrides)
    refused = check(current, prior)
    assert refused.returncode == 1 and expected in refused.stderr, refused.stderr
    assert json.loads(find(current).stdout)["refused"][0]["reason"].startswith(expected)


def test_reuse_needs_the_current_manifest_verified_against_the_live_tree(case):
    repo, init, accept = case
    prior = init()
    assert json.loads(accept(prior).stdout)["accepted"] is True
    current = init()
    (repo / "file.py").write_text("value = 3\n")
    drifted = check(current, prior)
    assert drifted.returncode == 1 and "drift" in drifted.stderr
    assert find(current).returncode == 1


def test_find_scans_only_sibling_runs_and_honours_an_explicit_artifact_dir(case, tmp_path):
    _, init, accept = case
    prior = init()
    assert json.loads(accept(prior).stdout)["accepted"] is True
    current = init()
    empty = tmp_path / "elsewhere"
    empty.mkdir()
    (empty / "note.txt").write_text("not a run\n")
    nothing = find(current, "--artifact-dir", str(empty))
    assert nothing.returncode == 1 and json.loads(nothing.stdout) == {
        "runId": json.loads(current.read_text())["runId"],
        "snapshotId": json.loads(current.read_text())["snapshotId"],
        "reusable": [],
        "refused": [],
    }


def test_helper_reads_artifacts_and_never_wakes_an_agent():
    source = (SCRIPTS / "reuse_result.py").read_text()
    imports = set(re.findall(r"^(?:from (\S+) import|import (\S+))", source, flags=re.M))
    modules = {a or b for a, b in imports}
    assert modules == {"argparse", "hashlib", "json", "sys", "pathlib", "run_manifest"}
    assert "subprocess" not in source
    for text in (source, (SKILL / "references/result-reuse.md").read_text()):
        assert "\u2014" not in text and "\u2013" not in text  # no em or en dashes in agent prose or code


def test_reference_distinguishes_prompt_caching_from_accepted_results():
    reference = (SKILL / "references/result-reuse.md").read_text()
    for phrase in (
        "prompt caching",
        "acceptance.json",
        "reuse_result.py",
        "started, interrupted or degraded",
        "Never wake a cold agent",
        "snapshot",
        "rubric",
        "running",
    ):
        assert phrase in reference, phrase
    skill = (SKILL / "SKILL.md").read_text()
    assert "references/result-reuse.md" in skill and "reuse_result.py" in skill
    assert "Automatic accepted-result reuse is deferred" not in skill
    lifecycle = (SKILL / "references/run-lifecycle.md").read_text()
    assert "result-reuse.md" in lifecycle
