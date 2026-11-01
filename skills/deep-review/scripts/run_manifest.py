#!/usr/bin/env python3
"""Create and verify immutable local review inputs; never replay or restore a run."""

import argparse
import hashlib
import json
import os
import re
import stat
import subprocess
import sys
import uuid
from pathlib import Path, PurePosixPath

MODES = {"base", "uncommitted", "commit", "range"}
DIGEST = re.compile(r"[0-9a-f]{64}\Z")
# Where the frozen tree lived is not part of the request: a worktree path changes per
# attempt while its content, the snapshot, does not.
PATH_ONLY = {"repoRoot"}


class ManifestError(ValueError):
    """A missing, unsafe, or changed review input."""


def require(condition, message):
    if not condition:
        raise ManifestError(message)


def encoded(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True, allow_nan=False).encode()


def digest(data):
    return hashlib.sha256(data).hexdigest()


def identity(value):
    return digest(encoded(value))


def absolute(value, *, resolve=True):
    require(isinstance(value, str) and Path(value).is_absolute() and "\x00" not in value, "input path must be absolute")
    return Path(value).resolve() if resolve else Path(value)


def read_bytes(path, label):
    try:
        return path.read_bytes()
    except OSError as exc:
        raise ManifestError(f"cannot read {label}") from exc


def parse_json(data, label):
    def pairs(items):
        result = {}
        for key, value in items:
            require(key not in result, f"duplicate key in {label}")
            result[key] = value
        return result

    def invalid_constant(_):
        raise ManifestError(f"non-finite number in {label}")

    try:
        value = json.loads(data, object_pairs_hook=pairs, parse_constant=invalid_constant)
        encoded(value)  # Also reject overflowing floats such as 1e999.
        return value
    except (ValueError, UnicodeError) as exc:
        raise ManifestError(f"invalid {label} JSON") from exc


def reject_secrets(value):
    if isinstance(value, dict):
        for key, item in value.items():
            normalized = re.sub(r"[^a-z0-9]", "", key.lower())
            forbidden = (
                normalized
                in {
                    "env",
                    "environment",
                    "environ",
                    "envvars",
                    "envvariables",
                    "environmentvariables",
                    "processenv",
                    "envoverrides",
                    "environmentoverrides",
                    "envfile",
                    "auth",
                    "authentication",
                    "authorization",
                }
                or any(word in normalized for word in ("password", "passwd", "secret", "credential"))
                or normalized.endswith(("apikey", "token", "privatekey"))
            )
            require(not forbidden, "args contain a prohibited credential or environment key")
            reject_secrets(item)
    elif isinstance(value, list):
        for item in value:
            reject_secrets(item)


def validate_args(args, repo):
    require(isinstance(args, dict), "args must be a JSON object")
    reject_secrets(args)
    require("run" not in args, "original args must not contain run metadata")
    require(absolute(args.get("repoRoot")) == repo, "args repoRoot does not match repository")
    require(isinstance(args.get("mode"), str) and args["mode"] in MODES, "invalid review mode")
    boss = args.get("boss", [])
    require(isinstance(boss, list) and all(isinstance(m, str) for m in boss), "boss must be a list of model ids")
    reviewers = args.get("reviewers", {})
    require(
        isinstance(reviewers, dict) and all(isinstance(r, dict) for r in reviewers.values()),
        "reviewers must map reviewer keys to policy objects",
    )


def models(args):
    """The requested model and provider lanes, read out of the arguments so a manifest says who was asked.

    The boss list and each reviewer's policy, agent type and requested model are what prepare_reviewers.py
    and the operator chose; verify recomputes this from originalArgs, so it cannot be edited to say otherwise.
    Catalog and self-reported identities are not here because they are not the request."""
    return {
        "boss": args.get("boss") or ["inherit"],
        "reviewers": {
            key: {"policy": r.get("policy"), "agentType": r.get("agentType"), "requestedModel": r.get("requestedModel")}
            for key, r in args.get("reviewers", {}).items()
        },
    }


def rubric(manifest):
    """Everything the run was asked to do: the request arguments, the workflow and the contracts."""
    args = {key: value for key, value in manifest["originalArgs"].items() if key not in PATH_ONLY}
    return {
        "args": args,
        "workflow": manifest["workflow"]["sha256"],
        "contracts": sorted(contract["sha256"] for contract in manifest["contracts"]),
    }


def unit_of(task_id, attempt):
    """The ledger unit this run serves, so an artifact found on disk names its task and attempt."""
    require((task_id is None) == (attempt is None), "--task-id and --attempt go together")
    if task_id is None:
        return None
    require(isinstance(task_id, str) and task_id.strip() == task_id and task_id, "task id must be a non-empty string")
    require(type(attempt) is int and attempt >= 1, "attempt numbers start at 1")
    return {"taskId": task_id, "attempt": attempt}


def is_run_id(value):
    try:
        return isinstance(value, str) and str(uuid.UUID(value, version=4)) == value
    except ValueError:
        return False


def attempt_of(number, prior_run_id=None, cause=None):
    """This request's attempt in its own series: 1 for a first run, N+1 for a retry of a terminal prior run.

    The ledger's unit.attempt is the worker's attempt; this is the review run's. A retry names the run
    it replaces and why that run ended (run_attempt.py records the classified cause), so a manifest found
    on disk says whether it is a first try or the third, and which artifacts to read for the earlier ones."""
    require(type(number) is int and number >= 1, "attempt numbers start at 1")
    if number == 1:
        require(prior_run_id is None and cause is None, "a first attempt has no prior run")
    else:
        require(is_run_id(prior_run_id), "a retry names the prior run it replaces")
        require(
            isinstance(cause, str) and cause.strip() and len(cause) <= 500,
            "a retry records why the prior attempt ended",
        )
    return {"number": number, "priorRunId": prior_run_id, "cause": cause}


def git(repo, *args):
    # Ignore ambient Git repository/index overrides. These plumbing commands do not
    # invoke diff drivers, textconv, clean/smudge filters, or refresh the index.
    env = {key: value for key, value in os.environ.items() if not key.startswith("GIT_")}
    env.update(GIT_OPTIONAL_LOCKS="0", GIT_NO_REPLACE_OBJECTS="1", GIT_TERMINAL_PROMPT="0")
    result = subprocess.run(
        ["git", "-c", "core.fsmonitor=false", "-C", str(repo), *args], env=env, capture_output=True, check=False
    )
    require(result.returncode == 0, "cannot read Git repository metadata")
    return result.stdout


def exclusions(values):
    require(isinstance(values, list), "invalid untracked exclusions")
    result = []
    for value in values:
        require(isinstance(value, str) and value and "\x00" not in value, "invalid untracked exclusion")
        path = PurePosixPath(value)
        require(not path.is_absolute() and ".." not in path.parts and path.parts, "invalid untracked exclusion")
        require(path.parts[0] != ".git", "cannot exclude Git metadata")
        result.append(str(path))
    return sorted(set(result))


def file_state(repo, relative):
    # Descriptor-relative traversal prevents even a concurrent symlink swap from
    # making us read outside the repository. Symlink targets are never opened.
    parts = PurePosixPath(relative).parts
    require(parts and not PurePosixPath(relative).is_absolute() and ".." not in parts, "invalid snapshot path")
    directory = os.open(repo, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    found = False
    try:
        for part in parts[:-1]:
            mode = os.stat(part, dir_fd=directory, follow_symlinks=False).st_mode
            require(stat.S_ISDIR(mode), "snapshot path has a non-directory ancestor")
            child = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=directory)
            os.close(directory)
            directory = child
        name = parts[-1]
        before = os.stat(name, dir_fd=directory, follow_symlinks=False)
        found = True
        mode = f"{stat.S_IMODE(before.st_mode):04o}"
        hasher = hashlib.sha256()
        if stat.S_ISLNK(before.st_mode):
            hasher.update(os.fsencode(os.readlink(name, dir_fd=directory)))
            kind = "symlink"
            after = os.stat(name, dir_fd=directory, follow_symlinks=False)
        else:
            require(stat.S_ISREG(before.st_mode), "snapshot contains an unsupported file type or submodule")
            fd = os.open(name, os.O_RDONLY | os.O_NOFOLLOW, dir_fd=directory)
            with os.fdopen(fd, "rb") as source:
                opened = os.fstat(source.fileno())
                require(
                    opened.st_ino == before.st_ino and opened.st_dev == before.st_dev, "snapshot changed while reading"
                )
                while chunk := source.read(1024 * 1024):
                    hasher.update(chunk)
                after = os.fstat(source.fileno())
            kind = "file"
        current = os.stat(name, dir_fd=directory, follow_symlinks=False)
        signatures = [
            (s.st_dev, s.st_ino, s.st_mode, s.st_size, s.st_mtime_ns, s.st_ctime_ns) for s in (before, after, current)
        ]
        require(signatures[0] == signatures[1] == signatures[2], "snapshot changed while reading")
        return {"path": relative, "kind": kind, "mode": mode, "sha256": hasher.hexdigest()}
    except FileNotFoundError:
        require(not found, "snapshot changed while reading")
        return {"path": relative, "kind": "deleted"}
    finally:
        os.close(directory)


def snapshot(repo, excluded):
    root = os.fsdecode(git(repo, "rev-parse", "--show-toplevel").removesuffix(b"\n"))
    require(Path(root).resolve() == repo, "repo-root must be the Git working-tree root")
    head = git(repo, "rev-parse", "--verify", "HEAD").decode().strip()
    index = git(repo, "ls-files", "--stage", "-z")
    tracked = set()
    for entry in index.split(b"\0"):
        if not entry:
            continue
        metadata, name = entry.split(b"\t", 1)
        mode, _, stage = metadata.split()
        require(stage == b"0", "repository has unresolved merges")
        require(mode != b"160000", "submodules are not supported by run manifests")
        tracked.add(os.fsdecode(name))
    for entry in git(repo, "ls-tree", "-r", "-z", "HEAD").split(b"\0"):
        if entry:
            metadata, name = entry.split(b"\t", 1)
            require(not metadata.startswith(b"160000 "), "submodules are not supported by run manifests")
            tracked.add(os.fsdecode(name))
    untracked = {
        os.fsdecode(name) for name in git(repo, "ls-files", "--others", "--exclude-standard", "-z").split(b"\0") if name
    }
    untracked = {
        name for name in untracked if not any(name == prefix or name.startswith(prefix + "/") for prefix in excluded)
    }
    files = [file_state(repo, name) for name in sorted(tracked | untracked)]
    require(git(repo, "rev-parse", "--verify", "HEAD").decode().strip() == head, "HEAD changed while reading snapshot")
    require(git(repo, "ls-files", "--stage", "-z") == index, "index changed while reading snapshot")
    return {"head": head, "indexSha256": digest(index), "excludeUntracked": excluded, "files": files}


def reference(path, label):
    return {"path": str(path), "sha256": digest(read_bytes(path, label))}


def check_reference(value, label):
    require(isinstance(value, dict) and set(value) == {"path", "sha256"}, f"invalid {label} reference")
    require(isinstance(value["sha256"], str) and DIGEST.fullmatch(value["sha256"]), f"invalid {label} digest")
    path = absolute(value["path"], resolve=False)
    require(str(path) == value["path"], f"{label} path changed")
    require(reference(path, label) == value, f"{label} changed")
    return path


def outputs(artifact_dir, run_id):
    """Every durable artifact of a run has its path declared before dispatch, all outside the reviewed tree.

    The worktree is disposable; the run directory is not. A recovering reader opens these files and
    never needs the worktree or the agent that produced them."""
    run_dir = artifact_dir / run_id
    return {
        "artifactDir": str(artifact_dir),
        "runDir": str(run_dir),
        "manifestPath": str(run_dir / "manifest.json"),
        "argsFile": str(run_dir / "args.json"),
        "resultPath": str(run_dir / "result.json"),
        "patchPath": str(run_dir / "fixes.patch"),
        "gatePath": str(run_dir / "gate-result.json"),
        "acceptancePath": str(run_dir / "acceptance.json"),
    }


def outside_repo(path, repo):
    require(path != repo and repo not in path.parents, "artifact directory must be outside the repository")


def write_exclusive(path, value):
    temporary = path.with_name("." + uuid.uuid4().hex + ".tmp")
    try:
        with temporary.open("xb") as target:
            target.write(encoded(value) + b"\n")
            target.flush()
            os.fchmod(target.fileno(), 0o444)
            os.fsync(target.fileno())
        # Unlike rename/replace, link fails rather than overwriting an existing run.
        os.link(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def init(options, attempt=None):
    """Freeze one run. `attempt` is only passed by run_attempt.py retry, after the prior run is terminal."""
    repo = absolute(options.repo_root)
    artifact_dir = absolute(options.artifact_dir)
    outside_repo(artifact_dir, repo)
    source = absolute(options.args_file, resolve=False)
    args_data = read_bytes(source, "source args")
    args = parse_json(args_data, "args")
    validate_args(args, repo)
    workflow = reference(absolute(options.workflow, resolve=False), "workflow")
    contracts = [reference(absolute(path, resolve=False), "contract") for path in options.contract]
    excluded = exclusions(options.exclude_untracked)
    state = snapshot(repo, excluded)
    snapshot_id = identity(state)
    run_id = str(uuid.uuid4())
    paths = outputs(artifact_dir, run_id)
    run = {"id": run_id, "snapshotId": snapshot_id, "manifestPath": paths["manifestPath"]}
    generated = {**args, "run": run}
    manifest = {
        "version": 3,
        "runId": run_id,
        "snapshotId": snapshot_id,
        "repoRoot": str(repo),
        "unit": unit_of(options.task_id, options.attempt),
        "attempt": attempt or attempt_of(1),
        "originalArgs": args,
        "sourceArgs": {"path": str(source), "sha256": digest(args_data)},
        "workflow": workflow,
        "contracts": contracts,
        "models": models(args),
        "snapshot": state,
        "outputs": paths,
        "argsFile": paths["argsFile"],
        "argsSha256": digest(encoded(generated) + b"\n"),
    }
    manifest["rubricId"] = identity(rubric(manifest))
    manifest["manifestSha256"] = identity(manifest)
    artifact_dir.mkdir(parents=True, exist_ok=True)
    require(artifact_dir.resolve() == artifact_dir, "artifact directory changed during initialization")
    Path(paths["runDir"]).mkdir(mode=0o700)
    write_exclusive(Path(paths["argsFile"]), generated)
    write_exclusive(Path(paths["manifestPath"]), manifest)
    return {
        "runId": run_id,
        "snapshotId": snapshot_id,
        "rubricId": manifest["rubricId"],
        "manifestPath": paths["manifestPath"],
        "argsFile": paths["argsFile"],
    }


def verify_manifest(path):
    """Return a validated manifest dict, or raise ManifestError (a ValueError).

    originalArgs holds the complete pre-run input; argsFile points at its exact
    generated counterpart with run metadata. This never executes the workflow.
    """
    try:
        return _verify_manifest(path)
    except (OSError, RuntimeError, RecursionError) as exc:
        raise ManifestError("cannot read run inputs; check paths and permissions") from exc


def _verify_manifest(path):
    path = absolute(os.fspath(path))
    manifest_data = read_bytes(path, "manifest")
    manifest = parse_json(manifest_data, "manifest")
    require(manifest_data == encoded(manifest) + b"\n", "manifest bytes changed")
    keys = {
        "version",
        "runId",
        "snapshotId",
        "repoRoot",
        "unit",
        "attempt",
        "originalArgs",
        "sourceArgs",
        "workflow",
        "contracts",
        "models",
        "snapshot",
        "outputs",
        "argsFile",
        "argsSha256",
        "rubricId",
        "manifestSha256",
    }
    require(isinstance(manifest, dict) and set(manifest) == keys, "invalid manifest schema")
    require(type(manifest["version"]) is int and manifest["version"] == 3, "unsupported manifest version")
    run_id = manifest["runId"]
    require(is_run_id(run_id), "invalid run ID")
    require(
        identity({key: value for key, value in manifest.items() if key != "manifestSha256"})
        == manifest["manifestSha256"],
        "manifest digest mismatch",
    )
    repo = absolute(manifest["repoRoot"])
    require(str(repo) == manifest["repoRoot"], "repository path changed")
    outside_repo(path.parent.parent, repo)
    paths = outputs(path.parent.parent, run_id)
    require(
        manifest["outputs"] == paths
        and str(path) == paths["manifestPath"]
        and manifest["argsFile"] == paths["argsFile"],
        "manifest output paths mismatch",
    )
    validate_args(manifest["originalArgs"], repo)
    require(manifest["models"] == models(manifest["originalArgs"]), "requested models disagree with the arguments")
    unit = manifest["unit"]
    require(
        unit is None or (isinstance(unit, dict) and unit == unit_of(unit.get("taskId"), unit.get("attempt"))),
        "invalid unit",
    )
    attempt = manifest["attempt"]
    require(
        isinstance(attempt, dict)
        and attempt == attempt_of(attempt.get("number"), attempt.get("priorRunId"), attempt.get("cause"))
        and attempt["priorRunId"] != run_id,
        "invalid attempt",
    )
    source = check_reference(manifest["sourceArgs"], "source args")
    require(
        parse_json(read_bytes(source, "source args"), "source args") == manifest["originalArgs"],
        "original args mismatch",
    )
    check_reference(manifest["workflow"], "workflow")
    require(isinstance(manifest["contracts"], list), "invalid contracts schema")
    for contract in manifest["contracts"]:
        check_reference(contract, "contract")
    require(identity(rubric(manifest)) == manifest["rubricId"], "rubric identity mismatch")
    args_data = read_bytes(Path(paths["argsFile"]), "generated args")
    require(digest(args_data) == manifest["argsSha256"], "generated args digest mismatch")
    expected = {
        **manifest["originalArgs"],
        "run": {"id": run_id, "snapshotId": manifest["snapshotId"], "manifestPath": str(path)},
    }
    require(parse_json(args_data, "generated args") == expected, "generated args mismatch")
    state = manifest["snapshot"]
    require(
        isinstance(state, dict) and set(state) == {"head", "indexSha256", "excludeUntracked", "files"},
        "invalid snapshot schema",
    )
    require(identity(state) == manifest["snapshotId"], "snapshot identity mismatch")
    excluded = exclusions(state["excludeUntracked"])
    require(excluded == state["excludeUntracked"], "invalid snapshot exclusions")
    require(snapshot(repo, excluded) == state, "repository snapshot drift detected")
    return manifest


def verify(options):
    manifest = verify_manifest(options.manifest)
    return {
        "valid": True,
        "runId": manifest["runId"],
        "snapshotId": manifest["snapshotId"],
        "rubricId": manifest["rubricId"],
        "unit": manifest["unit"],
        "attempt": manifest["attempt"],
        "argsFile": manifest["argsFile"],
    }


def resume(options):
    """Return the exact generated arguments to resend, after the identity they bind to is verified.

    A resume that omits the arguments, or passes a reconstruction, gives the runtime nothing to
    replay against and yields an empty run; a resume against a changed tree replays claims about
    another snapshot. Both are refused here, before any agent is contacted."""
    manifest = verify_manifest(options.manifest)
    return {
        **verify(options),
        "outputs": manifest["outputs"],
        "args": parse_json(read_bytes(Path(manifest["argsFile"]), "generated args"), "generated args"),
    }


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    create = commands.add_parser("init", help="freeze one run's inputs and declare its artifact paths")
    for name in ("repo-root", "args-file", "workflow", "artifact-dir"):
        create.add_argument("--" + name, required=True)
    create.add_argument("--contract", action="append", default=[])
    create.add_argument("--exclude-untracked", action="append", default=[])
    create.add_argument("--task-id", help="the ledger task this run serves; needs --attempt")
    create.add_argument("--attempt", type=int, help="the ledger attempt this run serves; needs --task-id")
    check = commands.add_parser("verify", help="refuse a changed input, workflow, contract or argument set")
    check.add_argument("--manifest", required=True)
    again = commands.add_parser("resume", help="verify, then print the exact generated args a resume must resend")
    again.add_argument("--manifest", required=True)
    options = parser.parse_args(argv)
    handlers = {"init": init, "verify": verify, "resume": resume}
    try:
        result = handlers[options.command](options)
    except ManifestError as exc:
        print(f"run-manifest: {exc}", file=sys.stderr)
        return 1
    except (OSError, RuntimeError, RecursionError):
        print("run-manifest: cannot read or publish run inputs; check paths and permissions", file=sys.stderr)
        return 1
    print(encoded(result).decode())
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
