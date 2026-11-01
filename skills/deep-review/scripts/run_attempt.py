#!/usr/bin/env python3
"""Say what a saved review result means, and start a bounded fresh attempt when its failure was transient.

JSON shape is not meaning. A result file can parse and still be a run that never
launched, stopped on a quota, replayed an unusable value, reviewed a tree that has
since changed, or applied fixes nobody re-reviewed. classify names each of those
with what to do next, and none of them is acceptance: acceptance.json is written
only by run_gate.py finalize. retry creates a new manifest with a new run id and
the next attempt number, only after the prior attempt is terminal and only under
the same request: a changed tree, script, contract or argument set is a new
request that starts its own series with run_manifest.py init. The prior run
directory is never edited or reused; it stays as evidence, and the new run id
changes every prompt, so the runtime replays nothing from the failed attempt and
no fixer side effect is repeated.
"""

import argparse
import json
import re
import sys
from pathlib import Path
from types import SimpleNamespace

from run_gate import validate_review
from run_manifest import ManifestError, attempt_of, identity, init, parse_json, read_bytes, require, verify_manifest

# Failure reasons a later attempt with the same request can outlive: provider load, quota windows,
# a runtime cancellation, a worker that returned nothing. These are what makes an attempt retryable.
TRANSIENT = re.compile(
    r"usage.limit|session.limit|quota|rate.limit|\b429\b|\b50[234]\b|overload|timed?.?out|cancell?ed|canceled"
    r"|no agent result|agent execution failed|network|connection",
    re.I,
)
# Reasons the same request would hit again: credentials, a missing tool or model, a refused dispatch
# shape, a budget or attempt cap. The request, the environment or the cap must change first.
BLOCKING = re.compile(
    r"authenticat|unauthori[sz]ed|\b401\b|\b403\b|account.on.hold|preflight|capability was not established"
    r"|not advertised|cli missing|not installed|not checked|absent or unloadable|conflicts with|misconfigur"
    r"|non-fresh agent|no result schema|agent-attempt limit|token budget"
    r"|gate (did not pass|evidence missing|not registered)",
    re.I,
)
ARTIFACTS = (("args", "argsFile"), ("result", "resultPath"), ("patch", "patchPath"), ("gate", "gatePath"))
DECIDED = {"accepted", "changes-required", "complete"}
NEXT = {
    "accepted": "the decision is recorded in acceptance.json; nothing to retry",
    "changes-required": "a ruling against this tree is recorded; repair under the round cap and review again",
    "complete": "pending finalization: the controller recorded a passed gate, run run_gate.py finalize",
    "missing": (
        "no result at resultPath: the attempt has not returned or was interrupted before the result was saved;"
        " resume it with run_manifest.py resume, or once the worker is known to be gone pass --abandon"
    ),
    "stale": (
        "the recorded inputs no longer verify (the tree, the workflow, a contract or the arguments changed);"
        " that is a new request, initialize a fresh manifest, a retry would review inputs that no longer exist"
    ),
    "modified": (
        "fixes were applied: keep the patch at patchPath and run one fresh read-only review of the resulting"
        " tree (run-lifecycle.md); nothing replays the fixer"
    ),
    "launch-blocked": "nothing was dispatched; change what blocked the launch, then initialize a fresh manifest",
    "blocked": (
        "the same request would fail again; change credentials, tooling, limits or the tree a gate refused,"
        " then initialize a fresh manifest"
    ),
    "transient": "retry: run_attempt.py retry starts the next attempt with a new run id and the same request",
    "partial": "retry: run_attempt.py retry starts the next attempt with a new run id and the same request",
    "malformed": "retry: a fresh run id replays nothing; if the shape is a script mismatch the next attempt fails the same way",
}


def load_manifest(path):
    """The manifest as written, intact by digest but not yet checked against the tree."""
    data = read_bytes(path, "manifest")
    manifest = parse_json(data, "manifest")
    require(
        isinstance(manifest, dict)
        and isinstance(manifest.get("manifestSha256"), str)
        and identity({key: value for key, value in manifest.items() if key != "manifestSha256"})
        == manifest["manifestSha256"]
        and isinstance(manifest.get("attempt"), dict)
        and {"runId", "snapshotId", "unit", "outputs", "sourceArgs", "workflow", "contracts", "snapshot"}
        <= set(manifest),
        "manifest is not intact",
    )
    return manifest


def replacements(manifest):
    """Sibling runs that name this run as their prior: a retry that already happened."""
    root = Path(manifest["outputs"]["artifactDir"])
    found = []
    for path in sorted(root.iterdir()) if root.is_dir() else []:
        if path.is_dir() and (path / "manifest.json").is_file():
            try:
                sibling = load_manifest(path / "manifest.json")
            except ManifestError:
                continue
            if sibling["attempt"].get("priorRunId") == manifest["runId"]:
                found.append(sibling["runId"])
    return found


def reasons_of(result):
    """Every failure reason the run recorded, in the order the record gives them."""
    admission = result.get("admission") or {}
    found = [admission.get("stopReason")]
    found += [d.get("reason") for d in result.get("degraded") or [] if isinstance(d, dict)]
    found += [
        a.get("reason") for a in result.get("attempts") or [] if isinstance(a, dict) and a.get("state") != "succeeded"
    ]
    found += [
        d.get("reason")
        for d in admission.get("dispatches") or []
        if isinstance(d, dict) and d.get("state") in {"failed", "cancelled"}
    ]
    unique = []
    for reason in found:
        if isinstance(reason, str) and reason.strip() and reason not in unique:
            unique.append(reason)
    return unique


def settled(result):
    admission = result.get("admission")
    if not isinstance(admission, dict):
        return False
    barriers = admission.get("barriers")
    dispatches = admission.get("dispatches")
    return (
        isinstance(barriers, list)
        and bool(barriers)
        and isinstance(barriers[-1], dict)
        and barriers[-1].get("name") == "conclude"
        and barriers[-1].get("open") == 0
        and isinstance(dispatches, list)
        and not any(isinstance(d, dict) and d.get("state") in {"queued", "running"} for d in dispatches)
    )


def classify(manifest_path):
    """Return what the run directory of manifest_path proves and what to do next; never raise for a bad result."""
    manifest_path = Path(manifest_path).resolve()
    manifest = load_manifest(manifest_path)
    outputs = manifest["outputs"]
    report = {
        "runId": manifest["runId"],
        "snapshotId": manifest["snapshotId"],
        "attempt": manifest["attempt"],
        "unit": manifest["unit"],
        "artifacts": {name: Path(outputs[key]).is_file() for name, key in ARTIFACTS},
        "replacedBy": replacements(manifest),
        "treeDrift": None,
        "category": None,
        "terminal": False,
        "retryable": False,
        "reasons": [],
        "evidence": {"resultPath": outputs["resultPath"], "successfulPayloads": []},
    }
    try:
        verify_manifest(manifest_path)
    except ManifestError as exc:
        report["treeDrift"] = str(exc)
    acceptance_path = Path(outputs["acceptancePath"])
    report["artifacts"]["acceptance"] = acceptance_path.is_file()

    def conclude(category, *, terminal=True, retryable=False, reasons=()):
        report.update(
            category=category, terminal=terminal, retryable=retryable, reasons=list(reasons), next=NEXT[category]
        )
        return report

    if acceptance_path.is_file():
        acceptance = parse_json(read_bytes(acceptance_path, "acceptance"), "acceptance")
        status = acceptance.get("status") if isinstance(acceptance, dict) else None
        if status in {"accepted", "changes-required"} and acceptance.get("runId") == manifest["runId"]:
            return conclude(status)
        return conclude("malformed", reasons=["acceptance.json does not record a decision for this run"])
    if not report["artifacts"]["result"]:
        return conclude("missing", terminal=False)
    try:
        result = parse_json(read_bytes(Path(outputs["resultPath"]), "result"), "result")
    except ManifestError as exc:
        return conclude("malformed", reasons=[str(exc)])
    if not isinstance(result, dict):
        return conclude("malformed", reasons=["result is not an object"])
    expected = {"id": manifest["runId"], "snapshotId": manifest["snapshotId"], "manifestPath": str(manifest_path)}
    if result.get("run") != expected:
        return conclude("malformed", reasons=["result identifies another run; locate this run's own result"])
    report["evidence"]["successfulPayloads"] = [
        {"key": p.get("key"), "round": p.get("round")}
        for p in result.get("reviewResults") or []
        if isinstance(p, dict) and isinstance(p.get("result"), dict) and p["result"].get("degraded") is False
    ]
    reasons = reasons_of(result)
    if not settled(result):
        return conclude(
            "partial", terminal=False, reasons=reasons or ["a dispatch was still active when the result was written"]
        )
    if report["treeDrift"]:
        return conclude("stale", reasons=[report["treeDrift"]])
    if result.get("fixed") is not False or result.get("requiresPostFixReview") is not False:
        # The patch is the only durable copy of the fixes once the worktree is gone; say when it is absent.
        if not report["artifacts"]["patch"]:
            reasons.append("no patch at patchPath; the fixes exist only in the worktree until Phase 3.5 captures them")
        return conclude("modified", reasons=reasons)
    text = "\n".join(reasons)
    transient = bool(TRANSIENT.search(text)) and not BLOCKING.search(text)
    if (result.get("admission") or {}).get("started") == 0:
        return conclude("launch-blocked", retryable=transient, reasons=reasons)
    if result.get("incomplete") is not False:
        if BLOCKING.search(text):
            return conclude("blocked", reasons=reasons)
        return conclude("transient" if transient else "partial", retryable=True, reasons=reasons)
    try:
        validate_review(manifest, result)
    except (ManifestError, KeyError, TypeError) as exc:
        message = str(exc) if isinstance(exc, ManifestError) else type(exc).__name__
        return conclude("malformed", retryable=True, reasons=[message])
    return conclude("complete")


def retry(manifest_path, max_attempts=3, abandon=None):
    """Create the next attempt's manifest for the same request, after the prior attempt is terminal."""
    require(type(max_attempts) is int and max_attempts >= 1, "--max-attempts must be a positive integer")
    manifest_path = Path(manifest_path).resolve()
    report = classify(manifest_path)
    manifest = load_manifest(manifest_path)
    category = report["category"]
    if abandon is not None:
        require(
            isinstance(abandon, str) and abandon.strip(), "--abandon needs the reason the attempt is known to be gone"
        )
        require(
            not report["terminal"], f"--abandon applies to an attempt that has not returned; this one is {category}"
        )
        cause = f"abandoned ({category}): {abandon.strip()}"
    else:
        require(report["terminal"], f"prior attempt is not terminal ({category}): {report['next']}")
        require(report["retryable"], f"a {category} result is not retried: {report['next']}")
        # The cause names the reason that decided the category, not merely the first one the run recorded.
        decisive = next((r for r in report["reasons"] if category != "transient" or TRANSIENT.search(r)), None)
        cause = f"{category}: {decisive or 'no reason recorded'}"
    # Every refusal below is a reason the next attempt would not be the same request, or would be a second
    # replacement of the same attempt, which is how a bound gets bypassed.
    require(
        not report["treeDrift"], f"the inputs changed since the prior attempt ({report['treeDrift']}): {NEXT['stale']}"
    )
    require(
        not report["replacedBy"],
        f"attempt {manifest['attempt']['number']} already has a replacement ({', '.join(report['replacedBy'])});"
        " classify and retry that run instead",
    )
    number = manifest["attempt"]["number"]
    require(
        number < max_attempts,
        f"attempt {number} is the last of {max_attempts}; a further attempt is an operator decision with a fresh manifest",
    )
    unit = manifest["unit"] or {}
    options = SimpleNamespace(
        repo_root=manifest["repoRoot"],
        args_file=manifest["sourceArgs"]["path"],
        workflow=manifest["workflow"]["path"],
        artifact_dir=manifest["outputs"]["artifactDir"],
        contract=[c["path"] for c in manifest["contracts"]],
        exclude_untracked=manifest["snapshot"]["excludeUntracked"],
        task_id=unit.get("taskId"),
        attempt=unit.get("attempt"),
    )
    created = init(options, attempt=attempt_of(number + 1, manifest["runId"], cause[:500]))
    # A retry is the same request about the same input. If either moved between classify and init, the new
    # directory is a different run; it is left in place as a record and named here, never promoted.
    require(
        created["snapshotId"] == manifest["snapshotId"] and created["rubricId"] == manifest["rubricId"],
        f"the inputs changed during the retry; {created['manifestPath']} is not a replacement, initialize a fresh manifest",
    )
    return {
        **created,
        "attempt": number + 1,
        "priorRunId": manifest["runId"],
        "priorCategory": category,
        "cause": cause[:500],
        "replay": False,
        "preserved": {"runDir": manifest["outputs"]["runDir"], **report["evidence"]},
    }


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    commands = parser.add_subparsers(dest="command", required=True)
    what = commands.add_parser("classify", help="what a run directory proves: accepted, complete, or which failure")
    what.add_argument("--manifest", required=True)
    again = commands.add_parser("retry", help="start the next attempt of the same request after a terminal failure")
    again.add_argument("--manifest", required=True, help="the prior attempt's manifest")
    again.add_argument("--max-attempts", type=int, default=3, help="attempts allowed in the series, default 3")
    again.add_argument("--abandon", help="reason an attempt that never returned is known to be gone")
    options = parser.parse_args(argv)
    try:
        if options.command == "classify":
            report = classify(options.manifest)
            code = 0 if report["category"] in DECIDED else 1
        else:
            report = retry(options.manifest, options.max_attempts, options.abandon)
            code = 0
    except ManifestError as exc:
        print(f"run-attempt: {exc}", file=sys.stderr)
        return 2
    except (OSError, RuntimeError, RecursionError):
        print("run-attempt: cannot read or publish run artifacts; check paths and permissions", file=sys.stderr)
        return 2
    print(json.dumps(report))
    return code


if __name__ == "__main__":
    raise SystemExit(main())
