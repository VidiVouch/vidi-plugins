"""Offline run identity tests using disposable Git repositories only."""

import importlib.util
import json
import os
import stat
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest
from conftest import REPO

SCRIPT = REPO / "skills/deep-review/scripts/run_manifest.py"
spec = importlib.util.spec_from_file_location("run_manifest", SCRIPT)
assert spec and spec.loader
manifest_api = importlib.util.module_from_spec(spec)
spec.loader.exec_module(manifest_api)


def git(repo, *args, check=True):
    return subprocess.run(["git", "-C", str(repo), *args], capture_output=True, check=check)


def cli(*args, success=True, env=None):
    result = subprocess.run([sys.executable, str(SCRIPT), *map(str, args)], capture_output=True, text=True, env=env)
    if success:
        assert result.returncode == 0, result.stderr
        assert result.stderr == ""
        assert len(result.stdout.splitlines()) == 1
        return json.loads(result.stdout)
    assert result.returncode != 0, result.stdout
    assert not result.stdout
    assert "Traceback" not in result.stderr
    return result.stderr


@pytest.fixture
def case(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    git(repo, "init", "-q")
    git(repo, "config", "user.email", "test@example.invalid")
    git(repo, "config", "user.name", "Manifest Test")
    (repo / "tracked.txt").write_text("original content\n")
    (repo / ".gitignore").write_text("ignored/\n")
    git(repo, "add", ".")
    git(repo, "commit", "-qm", "initial")
    args = tmp_path / "input.json"
    original = {
        "repoRoot": str(repo),
        "mode": "uncommitted",
        "focus": "all paths",
        "tools": {"codex": False},
        "boss": [],
    }
    args.write_text(json.dumps(original))
    workflow = tmp_path / "workflow.js"
    workflow.write_text("export const meta = {};\n")
    contract = tmp_path / "contract.json"
    contract.write_text('{"type":"object"}\n')
    return SimpleNamespace(
        repo=repo, args=args, workflow=workflow, contract=contract, artifact=tmp_path / "runs", original=original
    )


def initialize(case, *extra, success=True, env=None):
    return cli(
        "init",
        "--repo-root",
        case.repo,
        "--args-file",
        case.args,
        "--workflow",
        case.workflow,
        "--artifact-dir",
        case.artifact,
        "--contract",
        case.contract,
        *extra,
        success=success,
        env=env,
    )


def verify(run, success=True):
    return cli("verify", "--manifest", run["manifestPath"], success=success)


def load_manifest(run):
    return json.loads(Path(run["manifestPath"]).read_text())


def overwrite(path, value):
    path = Path(path)
    path.chmod(0o600)
    path.write_bytes(manifest_api.encoded(value) + b"\n")


def test_unchanged_run_and_public_api(case):
    run = initialize(case)
    assert set(run) == {"runId", "snapshotId", "rubricId", "manifestPath", "argsFile"}
    assert Path(run["manifestPath"]).parent.parent == case.artifact
    assert Path(run["manifestPath"]).parent.name == run["runId"]
    assert verify(run) == {
        "valid": True,
        "runId": run["runId"],
        "snapshotId": run["snapshotId"],
        "rubricId": run["rubricId"],
        "unit": None,
        "attempt": {"number": 1, "priorRunId": None, "cause": None},
        "argsFile": run["argsFile"],
    }
    stored = load_manifest(run)
    assert manifest_api.verify_manifest(Path(run["manifestPath"])) == stored
    assert stored["originalArgs"] == case.original
    assert stored["version"] == 3
    assert stored["repoRoot"] == str(case.repo)
    assert stored["argsFile"] == run["argsFile"]
    # Every durable artifact has a declared path in the run directory, outside the reviewed tree.
    run_dir = Path(run["manifestPath"]).parent
    assert stored["outputs"] == {
        "artifactDir": str(case.artifact),
        "runDir": str(run_dir),
        "manifestPath": run["manifestPath"],
        "argsFile": run["argsFile"],
        "resultPath": str(run_dir / "result.json"),
        "patchPath": str(run_dir / "fixes.patch"),
        "gatePath": str(run_dir / "gate-result.json"),
        "acceptancePath": str(run_dir / "acceptance.json"),
    }
    assert stored["rubricId"] == manifest_api.identity(manifest_api.rubric(stored))
    assert stored["models"] == {"boss": ["inherit"], "reviewers": {}}
    assert stored["unit"] is None
    assert stored["workflow"]["path"] == str(case.workflow)
    assert stored["contracts"][0]["path"] == str(case.contract)
    assert json.loads(Path(run["argsFile"]).read_text()) == {
        **case.original,
        "run": {"id": run["runId"], "snapshotId": run["snapshotId"], "manifestPath": run["manifestPath"]},
    }
    for path in (run["manifestPath"], run["argsFile"]):
        assert stat.S_IMODE(Path(path).stat().st_mode) == 0o444
    assert "original content" not in Path(run["manifestPath"]).read_text()
    second = initialize(case)
    assert second["runId"] != run["runId"]
    assert second["snapshotId"] == run["snapshotId"]
    assert verify(run)["valid"]


@pytest.mark.parametrize("target", ["args", "workflow", "contract"])
@pytest.mark.parametrize("mutation", ["change", "delete"])
def test_referenced_input_drift(case, target, mutation):
    run = initialize(case)
    path = getattr(case, target)
    if mutation == "change":
        path.write_bytes(path.read_bytes() + b" \n")
    else:
        path.unlink()
    assert target in verify(run, success=False)


def test_generated_args_drift(case):
    run = initialize(case)
    overwrite(run["argsFile"], {**case.original, "focus": "changed"})
    assert "generated args" in verify(run, success=False)


def test_generated_args_missing(case):
    run = initialize(case)
    Path(run["argsFile"]).unlink()
    assert "generated args" in verify(run, success=False)


@pytest.mark.parametrize("mutation", ["content", "untracked", "mode", "deleted", "staged-deleted", "head", "index"])
def test_repository_drift(case, mutation):
    run = initialize(case)
    path = case.repo / "tracked.txt"
    if mutation == "content":
        path.write_text("changed content\n")
    elif mutation == "untracked":
        (case.repo / "new.txt").write_text("new\n")
    elif mutation == "mode":
        path.chmod(path.stat().st_mode | stat.S_IXUSR)
    elif mutation == "deleted":
        path.unlink()
    elif mutation == "staged-deleted":
        git(case.repo, "rm", "tracked.txt")
    elif mutation == "head":
        git(case.repo, "commit", "--allow-empty", "-qm", "new head")
    else:
        path.write_text("staged content\n")
        git(case.repo, "add", "tracked.txt")
        path.write_text("original content\n")
    assert "snapshot drift" in verify(run, success=False)


@pytest.mark.parametrize("staged", [False, True])
def test_deleted_tracked_file_is_part_of_initial_snapshot(case, staged):
    (case.repo / "tracked.txt").unlink()
    if staged:
        git(case.repo, "add", "-u")
    run = initialize(case)
    assert {"path": "tracked.txt", "kind": "deleted"} in load_manifest(run)["snapshot"]["files"]
    assert verify(run)["valid"]
    (case.repo / "tracked.txt").write_text("original content\n")
    assert "snapshot drift" in verify(run, success=False)


def test_symlink_link_text_without_reading_target(case, tmp_path):
    outside = tmp_path / "outside.txt"
    outside.write_text("private target contents")
    link = case.repo / "link"
    link.symlink_to(outside)
    git(case.repo, "add", "link")
    run = initialize(case)
    recorded = next(item for item in load_manifest(run)["snapshot"]["files"] if item["path"] == "link")
    assert recorded["kind"] == "symlink"
    assert recorded["sha256"] == manifest_api.digest(os.fsencode(str(outside)))
    outside.unlink()
    assert verify(run)["valid"]  # Dangling symlinks remain valid; targets aren't inputs.
    link.unlink()
    link.symlink_to(tmp_path / "different-missing-target")
    assert "snapshot drift" in verify(run, success=False)


def test_symlink_ancestor_does_not_read_outside_repo(case, tmp_path):
    directory = case.repo / "folder"
    directory.mkdir()
    (directory / "tracked.txt").write_text("inside")
    git(case.repo, "add", "folder")
    (directory / "tracked.txt").unlink()
    directory.rmdir()
    directory.symlink_to(tmp_path, target_is_directory=True)
    assert "non-directory ancestor" in initialize(case, success=False)


def test_exclude_only_explicit_untracked_prefixes(case):
    generated = case.repo / "generated"
    generated.mkdir()
    (generated / "tracked.py").write_text("tracked = True\n")
    git(case.repo, "add", "generated/tracked.py")
    (generated / "output.json").write_text("{}")
    run = initialize(case, "--exclude-untracked", "generated/", "--exclude-untracked", "generated")
    stored = load_manifest(run)
    assert stored["snapshot"]["excludeUntracked"] == ["generated"]
    assert "generated/output.json" not in {entry["path"] for entry in stored["snapshot"]["files"]}
    (generated / "output.json").write_text("changed")
    (generated / "new.json").write_text("new")
    assert verify(run)["valid"]
    (generated / "tracked.py").write_text("tracked = False\n")
    assert "snapshot drift" in verify(run, success=False)


def test_exclusions_respect_path_boundaries_and_default_is_no_exclusion(case):
    folder = case.repo / "generated"
    folder.mkdir()
    (folder / "output").write_text("one")
    run = initialize(case)
    (folder / "output").write_text("two")
    assert "snapshot drift" in verify(run, success=False)
    excluded = initialize(case, "--exclude-untracked", "generated")
    (case.repo / "generated-other").write_text("not excluded")
    assert "snapshot drift" in verify(excluded, success=False)


def test_ignored_untracked_files_are_not_inputs(case):
    run = initialize(case)
    ignored = case.repo / "ignored"
    ignored.mkdir()
    (ignored / "secret").write_text("ignored contents")
    assert verify(run)["valid"]


@pytest.mark.parametrize("value", ["../outside", "/absolute", ".", "", ".git"])
def test_invalid_exclusions(case, value):
    assert "exclusion" in initialize(case, "--exclude-untracked", value, success=False) or value == ".git"


def test_unresolved_merge_rejected(case):
    run = initialize(case)
    start = git(case.repo, "rev-parse", "HEAD").stdout.decode().strip()
    git(case.repo, "checkout", "-qb", "side")
    (case.repo / "tracked.txt").write_text("side\n")
    git(case.repo, "commit", "-qam", "side")
    git(case.repo, "checkout", "-qb", "other", start)
    (case.repo / "tracked.txt").write_text("other\n")
    git(case.repo, "commit", "-qam", "other")
    assert git(case.repo, "merge", "side", check=False).returncode != 0
    assert "unresolved merges" in initialize(case, success=False)
    assert "unresolved merges" in verify(run, success=False)


@pytest.mark.parametrize(
    "replacement",
    [
        [],
        {},
        {"mode": "unknown"},
        {"mode": []},
        {"repoRoot": "/not-this-repo"},
        {"run": {"id": "old"}},
        {"boss": "codex"},
        {"boss": [1]},
        {"reviewers": ["claude-bugs"]},
        {"reviewers": {"claude-bugs": "required"}},
    ],
)
def test_invalid_args(case, replacement):
    value = {**case.original, **replacement} if isinstance(replacement, dict) and replacement else replacement
    case.args.write_text(json.dumps(value))
    assert initialize(case, success=False)
    assert not case.artifact.exists()


@pytest.mark.parametrize(
    "key",
    [
        "env",
        "environment",
        "apiKey",
        "OPENAI_API_KEY",
        "password",
        "accessToken",
        "credentials",
        "clientSecret",
        "private_key",
        "authorization",
        "GITHUB_TOKEN",
        "processEnv",
        "envOverrides",
    ],
)
def test_credentials_are_rejected_recursively_without_leaking(case, key):
    marker = "DO-NOT-RECORD-THIS-SECRET"
    case.args.write_text(json.dumps({**case.original, "tools": [{"nested": {key: marker}}]}))
    error = initialize(case, success=False)
    assert "credential or environment key" in error
    assert marker not in error
    assert not case.artifact.exists()


@pytest.mark.parametrize(
    "content", ['{"mode":"uncommitted","mode":"base"}', '{"mode":NaN}', '{"mode":1e999}', "not json"]
)
def test_invalid_json(case, content):
    case.args.write_text(content)
    assert "JSON" in initialize(case, success=False)


@pytest.mark.parametrize("kind", ["root", "child", "symlink"])
def test_artifact_path_must_be_outside_repository(case, tmp_path, kind):
    if kind == "root":
        case.artifact = case.repo
    elif kind == "child":
        case.artifact = case.repo / "artifacts"
    else:
        alias = tmp_path / "alias"
        alias.symlink_to(case.repo, target_is_directory=True)
        case.artifact = alias / "artifacts"
    assert "outside the repository" in initialize(case, success=False)
    assert not (case.repo / "artifacts").exists()


def test_index_and_worktree_are_unchanged(case):
    (case.repo / "tracked.txt").write_text("staged\n")
    git(case.repo, "add", "tracked.txt")
    (case.repo / "tracked.txt").write_text("unstaged\n")
    (case.repo / "untracked.txt").write_text("untracked\n")
    index = case.repo / ".git/index"
    status = git(case.repo, "status", "--porcelain=v1", "-z").stdout
    # status may refresh the index: measure the final state before the helper.
    before = index.read_bytes(), index.stat().st_mtime_ns
    run = initialize(case)
    verify(run)
    assert (index.read_bytes(), index.stat().st_mtime_ns) == before
    assert git(case.repo, "status", "--porcelain=v1", "-z").stdout == status
    assert (case.repo / "tracked.txt").read_text() == "unstaged\n"
    assert not (case.repo / ".git/index.lock").exists()


def test_harmless_git_status_index_refresh_preserves_snapshot(case):
    run = initialize(case)
    index = case.repo / ".git/index"
    before = index.read_bytes()
    tracked = case.repo / "tracked.txt"
    metadata = tracked.stat()
    os.utime(tracked, ns=(metadata.st_atime_ns, metadata.st_mtime_ns + 2_000_000_000))
    assert git(case.repo, "status", "--porcelain=v1").stdout == b""
    assert index.read_bytes() != before  # Git refreshed stat data, not staged entries.
    assert verify(run)["valid"]
    assert load_manifest(run)["snapshot"]["indexSha256"] == manifest_api.digest(
        git(case.repo, "ls-files", "--stage", "-z").stdout
    )


def test_external_diff_and_filters_are_not_invoked_and_git_env_is_ignored(case, tmp_path):
    marker = tmp_path / "invoked"
    command = f"touch {marker}; exit 1"
    git(case.repo, "config", "diff.external", command)
    git(case.repo, "config", "filter.hostile.clean", command)
    git(case.repo, "config", "filter.hostile.smudge", command)
    (case.repo / ".gitattributes").write_text("*.txt filter=hostile\n")
    env = {**os.environ, "GIT_DIR": "/missing/repo", "GIT_INDEX_FILE": "/missing/index", "GIT_EXTERNAL_DIFF": command}
    run = initialize(case, env=env)
    assert verify(run)["valid"]
    assert not marker.exists()


@pytest.mark.parametrize("mutation", ["version", "unknown", "identity", "args-identity", "schema"])
def test_manifest_integrity_and_schema(case, mutation):
    run = initialize(case)
    stored = load_manifest(run)
    if mutation == "version":
        stored["version"] = True
    elif mutation == "unknown":
        stored["unexpected"] = "value"
    elif mutation == "identity":
        stored["snapshotId"] = "bad"
    elif mutation == "args-identity":
        # Even with recomputed outer digests, generated run identity must match.
        args = json.loads(Path(run["argsFile"]).read_text())
        args["run"]["id"] = "different"
        overwrite(run["argsFile"], args)
        stored["argsSha256"] = manifest_api.digest(Path(run["argsFile"]).read_bytes())
        stored["manifestSha256"] = manifest_api.identity(
            {key: value for key, value in stored.items() if key != "manifestSha256"}
        )
    else:
        stored = []
    overwrite(run["manifestPath"], stored)
    assert verify(run, success=False)
    with pytest.raises(manifest_api.ManifestError):
        manifest_api.verify_manifest(run["manifestPath"])


@pytest.mark.parametrize("error", [PermissionError, FileNotFoundError])
def test_read_failures_are_not_clean_snapshots(case, monkeypatch, error):
    run = initialize(case)
    original = manifest_api.os.open

    def denied(path, flags, *args, **kwargs):
        if Path(path).name == "tracked.txt":
            raise error("secret details must not escape")
        return original(path, flags, *args, **kwargs)

    monkeypatch.setattr(manifest_api.os, "open", denied)
    with pytest.raises(manifest_api.ManifestError) as caught:
        manifest_api.verify_manifest(run["manifestPath"])
    assert "secret details" not in str(caught.value)


def test_exclusive_write_never_replaces_existing_file(tmp_path):
    destination = tmp_path / "existing.json"
    destination.write_text("unchanged")
    with pytest.raises(FileExistsError):
        manifest_api.write_exclusive(destination, {"replace": True})
    assert destination.read_text() == "unchanged"
    assert list(tmp_path.iterdir()) == [destination]


@pytest.mark.parametrize("target", ["args", "workflow", "contract"])
def test_missing_symlinked_input_is_detected(case, tmp_path, target):
    original = getattr(case, target)
    alias = tmp_path / (target + "-alias")
    alias.symlink_to(original)
    setattr(case, target, alias)
    run = initialize(case)
    assert verify(run)["valid"]
    alias.unlink()
    assert target in verify(run, success=False)


def test_manifest_whitespace_mutation_is_detected(case):
    run = initialize(case)
    path = Path(run["manifestPath"])
    path.chmod(0o600)
    path.write_bytes(path.read_bytes() + b"\n")
    assert "manifest bytes changed" in verify(run, success=False)


def test_submodules_fail_closed(case):
    head = git(case.repo, "rev-parse", "HEAD").stdout.decode().strip()
    git(case.repo, "update-index", "--add", "--cacheinfo", "160000," + head + ",submodule")
    assert "submodules are not supported" in initialize(case, success=False)


def resign(stored):
    """Recompute the outer digest so only the semantic checks can refuse the edited manifest."""
    stored["manifestSha256"] = manifest_api.identity({k: v for k, v in stored.items() if k != "manifestSha256"})
    return stored


def test_unit_models_and_rubric_are_recorded_and_cannot_be_rewritten(case):
    reviewers = {
        "claude-bugs": {"policy": "required", "available": True, "requestedModel": "opus", "catalogModel": None},
        "astra-standard": {
            "policy": "optional",
            "available": True,
            "agentType": "vidi-skills:astra-reviewer",
            "requestedModel": "gpt-6-astra",
            "catalogModel": "gpt-6-astra",
        },
        "codex-native": {"policy": "disabled", "available": False},
    }
    case.args.write_text(json.dumps({**case.original, "boss": ["codex", "opus"], "reviewers": reviewers}))
    run = initialize(case, "--task-id", "FLOW-1", "--attempt", "2")
    stored = load_manifest(run)
    assert stored["unit"] == {"taskId": "FLOW-1", "attempt": 2}
    assert verify(run)["unit"] == {"taskId": "FLOW-1", "attempt": 2}
    # The request's model lanes are readable without the arguments, and only the request: no catalog claims.
    assert stored["models"] == {
        "boss": ["codex", "opus"],
        "reviewers": {
            "claude-bugs": {"policy": "required", "agentType": None, "requestedModel": "opus"},
            "astra-standard": {
                "policy": "optional",
                "agentType": "vidi-skills:astra-reviewer",
                "requestedModel": "gpt-6-astra",
            },
            "codex-native": {"policy": "disabled", "agentType": None, "requestedModel": None},
        },
    }
    # The rubric identity changes with the request and the contracts, never with the worktree path.
    other = initialize(case)
    assert other["rubricId"] == run["rubricId"]
    contract = case.contract.read_bytes()
    case.contract.write_text('{"type":"object","stricter":true}\n')
    assert initialize(case)["rubricId"] != run["rubricId"]
    case.contract.write_bytes(contract)
    assert "--attempt" in initialize(case, "--task-id", "FLOW-1", success=False)
    assert "attempt numbers" in initialize(case, "--task-id", "FLOW-1", "--attempt", "0", success=False)
    assert "non-empty" in initialize(case, "--task-id", " ", "--attempt", "1", success=False)
    for key, value, message in (
        ("models", {"boss": ["inherit"], "reviewers": stored["models"]["reviewers"]}, "requested models"),
        ("unit", {"taskId": "FLOW-2", "attempt": 2, "role": "verifier"}, "invalid unit"),
        ("unit", {}, "invalid unit"),
        ("rubricId", "0" * 64, "rubric identity"),
        # The run's own attempt number: a first attempt has no prior, a retry names one that is not itself.
        ("attempt", {"number": 2, "priorRunId": None, "cause": "transient: quota"}, "prior run"),
        ("attempt", {"number": 1, "priorRunId": run["runId"], "cause": None}, "no prior run"),
        ("attempt", {"number": 2, "priorRunId": run["runId"], "cause": "transient: quota"}, "invalid attempt"),
        ("attempt", {"number": 2, "priorRunId": other["runId"], "cause": ""}, "why the prior attempt ended"),
        ("attempt", {"number": 0, "priorRunId": None, "cause": None}, "attempt numbers"),
        ("attempt", {"number": 1}, "invalid attempt"),
        ("attempt", None, "invalid attempt"),
    ):
        overwrite(run["manifestPath"], resign({**stored, key: value}))
        assert message in verify(run, success=False)
    assert manifest_api.attempt_of(2, other["runId"], "transient: quota") == {
        "number": 2,
        "priorRunId": other["runId"],
        "cause": "transient: quota",
    }


def test_resume_resends_the_exact_generated_args_or_refuses(case):
    run = initialize(case, "--task-id", "FLOW-1", "--attempt", "1")
    result = cli("resume", "--manifest", run["manifestPath"])
    generated = json.loads(Path(run["argsFile"]).read_text())
    assert result["args"] == generated
    assert result["args"]["run"] == {
        "id": run["runId"],
        "snapshotId": run["snapshotId"],
        "manifestPath": run["manifestPath"],
    }
    assert result["unit"] == {"taskId": "FLOW-1", "attempt": 1}
    assert result["attempt"] == {"number": 1, "priorRunId": None, "cause": None}
    assert result["outputs"]["resultPath"].startswith(str(Path(run["manifestPath"]).parent))
    assert manifest_api.resume(SimpleNamespace(manifest=run["manifestPath"]))["args"] == generated
    # No arguments to resend: nothing can be replayed, so nothing is handed to the runtime.
    saved = Path(run["argsFile"]).read_bytes()
    Path(run["argsFile"]).unlink()
    assert "generated args" in cli("resume", "--manifest", run["manifestPath"], success=False)
    Path(run["argsFile"]).write_bytes(saved)
    assert cli("resume", "--manifest", run["manifestPath"])["args"] == generated
    # A changed tree is another snapshot; the recorded arguments would resume a run about the old one.
    (case.repo / "tracked.txt").write_text("edited after dispatch\n")
    assert "snapshot drift" in cli("resume", "--manifest", run["manifestPath"], success=False)


def test_newline_paths_are_snapshotted_without_expanding_stdout(case):
    path = case.repo / "line\nbreak.txt"
    path.write_text("exact contents")
    run = initialize(case)
    assert verify(run)["valid"]
    assert "line\nbreak.txt" in {entry["path"] for entry in load_manifest(run)["snapshot"]["files"]}
    path.write_text("different contents")
    assert "snapshot drift" in verify(run, success=False)


def test_reference_describes_the_recovery_record_and_resume():
    reference = (REPO / "skills/deep-review/references/run-lifecycle.md").read_text()
    for phrase in ('run_manifest.py" resume', "rubricId", "`models`", "`unit`", "resultPath", "patchPath", "empty run"):
        assert phrase in reference, phrase
    assert "resume --manifest" in (REPO / "skills/deep-review/SKILL.md").read_text()
    for path in (SCRIPT, REPO / "skills/deep-review/references/run-lifecycle.md", Path(__file__)):
        assert not {"\u2014", "\u2013"} & set(path.read_text()), path
