#!/usr/bin/env python3
"""Reuse a prior run's accepted result only when its input and rubric match the current request.

Two caches exist and only one holds a decision. The Workflow runtime's resume
replays cached agent() return values for an unchanged prompt prefix; that is
prompt caching, a transport convenience, and a replayed value is still
pending-external-verification. The accepted result is acceptance.json, written
by run_gate.py finalize after the measured gate and bound by digest to the full
result and the gate report. This helper reads only those artifacts: it never
resumes, messages or wakes an agent, because a decision that is already in
artifacts needs no agent to recover it.
"""

import argparse
import hashlib
import json
import sys
from pathlib import Path

from run_manifest import ManifestError, identity, parse_json, read_bytes, rubric, verify_manifest


class ReuseError(ValueError):
    """The prior run is not a reusable acceptance for this request."""


def require(condition, message):
    if not condition:
        raise ReuseError(message)


def load(path, label):
    data = read_bytes(path, label)
    return data, parse_json(data, label)


def check(manifest, run_dir):
    """Return the reusable acceptance of run_dir for the verified current manifest, or raise ReuseError."""
    run_dir = Path(run_dir)
    try:
        _, prior = load(run_dir / "manifest.json", "prior manifest")
        require(
            isinstance(prior, dict)
            and identity({key: value for key, value in prior.items() if key != "manifestSha256"})
            == prior["manifestSha256"],
            "prior manifest is not intact",
        )
        require(prior["runId"] != manifest["runId"], "this is the current run, not a prior result")
        require(
            prior["snapshotId"] == manifest["snapshotId"],
            f"input differs: the prior run reviewed snapshot {prior['snapshotId']}",
        )
        require(rubric(prior) == rubric(manifest), "rubric differs: request arguments, workflow or contracts changed")
        acceptance_path = run_dir / "acceptance.json"
        # A manifest, a result or a gate report without acceptance.json is a run that started,
        # stopped or degraded. None of those is a decision.
        require(
            acceptance_path.is_file(), "no acceptance artifact: a started, interrupted or degraded run proves nothing"
        )
        _, acceptance = load(acceptance_path, "acceptance")
        require(
            isinstance(acceptance, dict)
            and acceptance["runId"] == prior["runId"]
            and acceptance["snapshotId"] == prior["snapshotId"],
            "acceptance does not identify the prior run",
        )
        require(
            acceptance["accepted"] is True and acceptance["status"] == "accepted",
            f"prior run was not accepted: {acceptance['status']}",
        )
        result_path = Path(acceptance["resultPath"])
        result_bytes, result = load(result_path, "accepted result")
        gate_bytes = read_bytes(run_dir / "gate-result.json", "gate report")
        require(
            hashlib.sha256(result_bytes).hexdigest() == acceptance["resultSha256"]
            and hashlib.sha256(gate_bytes).hexdigest() == acceptance["gateSha256"],
            "accepted result or gate evidence changed since acceptance",
        )
        require(
            isinstance(result, dict)
            and result["run"]
            == {"id": prior["runId"], "snapshotId": prior["snapshotId"], "manifestPath": str(run_dir / "manifest.json")}
            and result["incomplete"] is False,
            "accepted result does not describe a completed run of the prior manifest",
        )
    except (KeyError, TypeError) as exc:
        raise ReuseError("prior run artifacts are malformed") from exc
    except ManifestError as exc:
        raise ReuseError(str(exc)) from exc
    return {
        "reusable": True,
        "runId": prior["runId"],
        "snapshotId": prior["snapshotId"],
        "runDir": str(run_dir),
        "acceptancePath": str(acceptance_path),
        "resultPath": str(result_path),
        "coverageComplete": acceptance.get("coverageComplete") is True,
    }


def find(manifest, artifact_dir=None):
    """Check every sibling run under the artifact directory; the current run is never a prior result."""
    root = Path(artifact_dir) if artifact_dir else Path(manifest["outputs"]["artifactDir"])
    report = {"runId": manifest["runId"], "snapshotId": manifest["snapshotId"], "reusable": [], "refused": []}
    for run_dir in sorted(path for path in root.iterdir() if path.is_dir() and (path / "manifest.json").is_file()):
        if run_dir.name == manifest["runId"]:
            continue
        try:
            report["reusable"].append(check(manifest, run_dir))
        except ReuseError as exc:
            report["refused"].append({"runDir": str(run_dir), "reason": str(exc)})
    return report


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    commands = parser.add_subparsers(dest="command", required=True)
    one = commands.add_parser("check", help="is this prior run's acceptance reusable for the current manifest")
    one.add_argument("--manifest", required=True, help="the current request's verified manifest")
    one.add_argument("--run-dir", required=True, help="a prior run directory under the artifact directory")
    many = commands.add_parser("find", help="list reusable and refused prior runs for the current manifest")
    many.add_argument("--manifest", required=True)
    many.add_argument("--artifact-dir", help="defaults to the manifest's own artifact directory")
    options = parser.parse_args(argv)
    try:
        # The current manifest is verified against the tree first: the identity being matched must be live.
        manifest = verify_manifest(options.manifest)
        if options.command == "check":
            report = check(manifest, options.run_dir)
            reusable = True
        else:
            report = find(manifest, options.artifact_dir)
            reusable = bool(report["reusable"])
    except (ReuseError, ManifestError) as exc:
        print(f"reuse-result: {exc}", file=sys.stderr)
        return 1
    except OSError:
        print("reuse-result: cannot read run artifacts; check paths and permissions", file=sys.stderr)
        return 1
    print(json.dumps(report))
    return 0 if reusable else 1


if __name__ == "__main__":
    raise SystemExit(main())
