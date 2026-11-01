#!/usr/bin/env python3
"""Run registered checks and verify review acceptance outside the model runtime."""

import argparse
import hashlib
import json
import os
import signal
import subprocess
import sys
import tempfile
from pathlib import Path

from owned_processes import register
from run_manifest import ManifestError, verify_manifest


def write_json(path, data):
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(mode="w", dir=path.parent, prefix=f".{path.name}.", delete=False) as stream:
            temporary = Path(stream.name)
            json.dump(data, stream, indent=2)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.link(temporary, path)  # exclusive atomic publication; never overwrite evidence
        descriptor = os.open(path.parent, os.O_RDONLY)
        try:
            os.fsync(descriptor)
        finally:
            os.close(descriptor)
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


def interrupt(_signum, _frame):
    raise KeyboardInterrupt


def digest(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def gate_config(manifest):
    config = manifest["originalArgs"].get("gate", {})
    commands = config.get("commands") if isinstance(config, dict) else None
    timeout = config.get("timeoutSeconds", 120) if isinstance(config, dict) else None
    if (
        not isinstance(commands, list)
        or not commands
        or len(commands) > 16
        or any(
            not isinstance(c, list) or not c or any(not isinstance(a, str) or not a or "\x00" in a for a in c)
            for c in commands
        )
        or type(timeout) is not int
        or not 1 <= timeout <= 3600
    ):
        raise ManifestError(
            "Register nonempty gate.commands argv lists and a 1..3600 second timeout in the original arguments"
        )
    return commands, timeout


def stop_group(process):
    # Defer repeated cancellation until the owned group has been killed and reaped.
    previous_mask = signal.pthread_sigmask(signal.SIG_BLOCK, {signal.SIGTERM, signal.SIGINT})
    try:
        try:
            os.killpg(process.pid, signal.SIGTERM)
        except ProcessLookupError:
            pass
        try:
            process.wait(timeout=1)
        except subprocess.TimeoutExpired:
            pass
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        process.wait()
    finally:
        signal.pthread_sigmask(signal.SIG_SETMASK, previous_mask)


def run_gate(manifest_path, registry=None):
    """Run the registered checks. With a registry, each child is recorded for the unit's cleanup barrier
    (owned_processes.py) before it is waited on, so a child a hard-killed gate left behind is still stopped."""
    if os.name != "posix":
        raise ManifestError("Gate process-group cleanup currently requires a POSIX host")
    manifest_path = Path(manifest_path).resolve()
    manifest = verify_manifest(manifest_path)
    commands, timeout = gate_config(manifest)
    directory = manifest_path.parent
    output = directory / "gate-result.json"
    if output.exists():
        raise ManifestError("Gate result already exists; inspect it or initialize a fresh attempt")
    lock = directory / "gate.lock"
    with lock.open("x") as stream:
        stream.write(str(os.getpid()))
    report = {
        "version": 1,
        "runId": manifest["runId"],
        "snapshotId": manifest["snapshotId"],
        "status": "failed",
        # Who ran the checks: this host process, never the agent that carried the report into the controller.
        "executor": {"helper": "run_gate.py", "pid": os.getpid()},
        "registry": str(Path(registry).resolve()) if registry else None,
        "checks": [],
    }
    previous_handler = signal.signal(signal.SIGTERM, interrupt)
    try:
        for index, command in enumerate(commands):
            verify_manifest(manifest_path)
            stdout = directory / f"gate-{index}.stdout"
            stderr = directory / f"gate-{index}.stderr"
            check = {
                "command": command,
                "exitCode": None,
                "status": "failed",
                "stdout": str(stdout),
                "stderr": str(stderr),
            }
            report["checks"].append(check)
            with stdout.open("xb") as out, stderr.open("xb") as err:
                process = subprocess.Popen(
                    command, cwd=manifest["repoRoot"], stdout=out, stderr=err, start_new_session=True
                )
                try:
                    if registry:
                        check["process"] = register(registry, process.pid)
                    check["exitCode"] = process.wait(timeout=timeout)
                    check["status"] = "passed" if check["exitCode"] == 0 else "failed"
                except subprocess.TimeoutExpired:
                    check["status"] = "timeout"
                except KeyboardInterrupt:
                    check["status"] = "cancelled"
                    raise
                finally:
                    stop_group(process)
            check["stdoutSha256"] = digest(stdout)
            check["stderrSha256"] = digest(stderr)
            if check["status"] != "passed":
                break
        verify_manifest(manifest_path)
        if len(report["checks"]) == len(commands) and all(c["status"] == "passed" for c in report["checks"]):
            report["status"] = "passed"
    except KeyboardInterrupt:
        report["status"] = "cancelled"
        report["reason"] = "Gate interrupted; no acceptance"
    except (OSError, ValueError) as exc:
        report["status"] = "failed"
        report["reason"] = f"Gate or snapshot validation failed ({type(exc).__name__})"
    finally:
        signal.signal(signal.SIGTERM, previous_handler)
        try:
            write_json(output, report)
        finally:
            lock.unlink()
    return report


SEVERITIES = {"critical", "warning", "nit"}


def check_finding(finding):
    """A finding is usable only when it names a file inside the tree, a positive line range and a description."""
    if not isinstance(finding, dict) or finding.get("severity") not in SEVERITIES:
        raise ManifestError("Invalid current finding")
    name, start, end = finding.get("file"), finding.get("line_start"), finding.get("line_end")
    if (
        not isinstance(name, str)
        or not name
        or "\\" in name
        or Path(name).is_absolute()
        or any(part in {"", ".", ".."} for part in name.split("/"))
        or type(start) is not int
        or type(end) is not int
        or start < 1
        or end < start
        or not isinstance(finding.get("description"), str)
        or not finding["description"].strip()
    ):
        raise ManifestError("Invalid finding location or evidence")
    return name, end


def registered_scope(args):
    """The round and admission limits the request registered, with the workflow's own defaults."""
    limits = args.get("limits")
    max_rounds = args.get("maxRounds")
    if (limits is not None and not isinstance(limits, dict)) or (
        max_rounds is not None and type(max_rounds) is not int
    ):
        raise ManifestError("Registered limits are not a limits object and a round count")
    return {
        "concurrency": 3,
        "maxAgents": 30,
        "minRemainingTokens": 0,
        **(limits or {}),
    }, 3 if max_rounds is None else max_rounds


def validate_review(manifest, result):
    if not isinstance(result, dict) or result.get("run") != {
        "id": manifest["runId"],
        "snapshotId": manifest["snapshotId"],
        "manifestPath": str(Path(manifest["outputs"]["manifestPath"]).resolve()),
    }:
        raise ManifestError("Review result does not identify this exact run and snapshot")
    if result.get("mode") != manifest["originalArgs"]["mode"]:
        raise ManifestError("Review mode mismatch")
    # Scope: the result must say it ran the registered request, not a wider or narrower one.
    if result.get("boss") != manifest["models"]["boss"]:
        raise ManifestError("Boss lanes disagree with the registered request")
    limits, max_rounds = registered_scope(manifest["originalArgs"])
    rounds = result.get("rounds")
    if (
        result.get("incomplete") is not False
        or result.get("fixed") is not False
        or result.get("requiresPostFixReview") is not False
    ):
        raise ManifestError("Incomplete or modifying run requires a fresh read-only review, not acceptance")
    admission = result.get("admission")
    if (
        not isinstance(admission, dict)
        or admission.get("stopReason") != ""
        or type(admission.get("started")) is not int
        or admission["started"] < 1
        or not isinstance(admission.get("dispatches"), list)
        or not admission["dispatches"]
        or any(
            not isinstance(d, dict) or d.get("state") not in {"returned", "failed", "cancelled"}
            for d in admission["dispatches"]
        )
        or admission["started"] != sum(d["state"] != "cancelled" for d in admission["dispatches"])
    ):
        raise ManifestError("Stopped, pending, or empty dispatch cannot be accepted")
    if admission.get("limits") != limits or admission.get("maxRounds") != max_rounds:
        raise ManifestError("Admission limits disagree with the registered request")
    if type(rounds) is not int or not 1 <= rounds <= max_rounds:
        raise ManifestError("Round count is outside the registered round cap")
    barriers = admission.get("barriers")
    if (
        not isinstance(barriers, list)
        or not barriers
        or any(not isinstance(b, dict) or b.get("open") != 0 for b in barriers)
        or barriers[-1].get("name") != "conclude"
        or barriers[-1].get("dispatched") != admission["started"]
    ):
        raise ManifestError("Run did not pass its cleanup barriers; a dispatched unit may still have been active")
    coverage = result.get("coverage")
    expected = manifest["originalArgs"].get("reviewers")
    successful = result.get("successful")
    attempted = result.get("attempted")
    if (
        not isinstance(coverage, dict)
        or not isinstance(expected, dict)
        or not expected
        or set(coverage) != set(expected)
    ):
        raise ManifestError("Reviewer policy/coverage mismatch")
    if not isinstance(successful, list) or not successful or not isinstance(attempted, list):
        raise ManifestError("No successful review evidence")
    if (
        any(not isinstance(key, str) for key in successful + attempted)
        or len(set(successful)) != len(successful)
        or len(set(attempted)) != len(attempted)
    ):
        raise ManifestError("Invalid reviewer identities")
    if not set(successful) <= set(attempted) <= set(expected):
        raise ManifestError("Reviewer execution records disagree")
    succeeded = set()
    for key, policy in expected.items():
        if not isinstance(policy, dict) or policy.get("policy") not in {"disabled", "optional", "required"}:
            raise ManifestError("Invalid registered reviewer policy")
        entry = coverage[key]
        if not isinstance(entry, dict) or entry.get("policy") != policy.get("policy"):
            raise ManifestError("Reviewer policy changed during review")
        state = entry.get("state")
        if state not in {"disabled", "unavailable", "succeeded", "failed", "cancelled"}:
            raise ManifestError("Reviewer has no terminal state")
        if policy["policy"] == "disabled" and (state != "disabled" or key in attempted):
            raise ManifestError("Disabled reviewer counted as evidence")
        if policy["policy"] == "required" and state != "succeeded":
            raise ManifestError("Required reviewer did not succeed")
        if state == "succeeded":
            if policy.get("available") is not True:
                raise ManifestError("Unavailable reviewer counted as successful")
            succeeded.add(key)
    complete = all(p["policy"] == "disabled" or coverage[k]["state"] == "succeeded" for k, p in expected.items())
    if result.get("coverageComplete") is not complete:
        raise ManifestError("Coverage summary disagrees with reviewer states")
    degraded = result.get("degraded")
    if not isinstance(degraded, list) or any(
        not isinstance(d, dict)
        or d.get("key") not in expected
        or expected[d["key"]]["policy"] != "optional"
        or coverage[d["key"]]["state"] not in {"failed", "cancelled"}
        for d in degraded
    ):
        raise ManifestError("Required review or gate failure cannot be accepted")
    if succeeded != set(successful):
        raise ManifestError("Successful reviewer list disagrees with coverage")
    findings = result.get("currentFindings")
    if not isinstance(findings, list):
        raise ManifestError("Missing current findings; historical findings are not final dispositions")
    for finding in findings:
        name, end = check_finding(finding)
        sources = finding.get("sources")
        if not isinstance(sources, list) or not sources or not set(sources) <= set(successful):
            raise ManifestError("Current finding is not attributed to a successful reviewer")
        path = (Path(manifest["repoRoot"]) / name).resolve()
        if (
            not path.is_relative_to(Path(manifest["repoRoot"]))
            or not path.is_file()
            or end > len(path.read_bytes().splitlines())
        ):
            raise ManifestError("Current finding does not cite the verified working tree")
    attempts = result.get("attempts")
    if (
        not isinstance(attempts, list)
        or not attempts
        or any(
            not isinstance(a, dict)
            or a.get("key") not in expected
            or a.get("state") not in {"succeeded", "failed", "cancelled"}
            for a in attempts
        )
    ):
        raise ManifestError("Missing or invalid attempt history")
    payloads = result.get("reviewResults")
    if not isinstance(payloads, list) or any(not isinstance(p, dict) for p in payloads):
        raise ManifestError("Missing full reviewer payloads")
    for key in successful:
        last = next((a for a in reversed(attempts) if a["key"] == key), None)
        if not last or last["state"] != "succeeded" or type(last.get("round")) is not int or last["round"] < 1:
            raise ManifestError("Success lacks a successful final attempt")
        dispatches = [d for d in admission["dispatches"] if d.get("label") == f"{key}#{last['round']}"]
        if len(dispatches) != 1 or dispatches[0].get("phase") != "Review" or dispatches[0]["state"] != "returned":
            raise ManifestError("Successful review lacks a matching returned dispatch")
        payload = next(
            (
                p.get("result")
                for p in reversed(payloads)
                if p.get("key") == key and p.get("round") == last.get("round")
            ),
            None,
        )
        if (
            not isinstance(payload, dict)
            or payload.get("degraded") is not False
            or not isinstance(payload.get("findings"), list)
            or not isinstance(payload.get("reason"), str)
        ):
            raise ManifestError("Successful attempt lacks its full valid payload")
        # A payload that parses but holds unusable findings is a replayed or truncated value, not evidence.
        for finding in payload["findings"]:
            check_finding(finding)
    # The controller's own decision rested on a gate report for this exact run and snapshot; a run whose
    # gate was skipped, failed, or never reached the controller has no such record and is not complete.
    gate = result.get("gate")
    report = gate.get("report") if isinstance(gate, dict) else None
    if (
        not isinstance(gate, dict)
        or gate.get("state") != "passed"
        or not isinstance(report, dict)
        or report.get("runId") != manifest["runId"]
        or report.get("snapshotId") != manifest["snapshotId"]
        or report.get("status") != "passed"
    ):
        raise ManifestError("Controller did not record a passed gate for this run and snapshot")
    return not any(f["severity"] != "nit" for f in findings)


def finalize(manifest_path, result_path):
    manifest_path = Path(manifest_path).resolve()
    manifest = verify_manifest(manifest_path)
    result_path = Path(result_path).resolve()
    result_bytes = result_path.read_bytes()
    result_hash = hashlib.sha256(result_bytes).hexdigest()
    result = json.loads(result_bytes)
    clean = validate_review(manifest, result)
    gate_path = manifest_path.parent / "gate-result.json"
    gate_bytes = gate_path.read_bytes()
    gate_hash = hashlib.sha256(gate_bytes).hexdigest()
    gate = json.loads(gate_bytes)
    commands, _ = gate_config(manifest)
    if (
        gate.get("runId") != manifest["runId"]
        or gate.get("snapshotId") != manifest["snapshotId"]
        or gate.get("status") != "passed"
        or len(gate.get("checks", [])) != len(commands)
    ):
        raise ManifestError("Missing, failed, or wrong-snapshot gate")
    # Transport check: the controller decided on exactly the report this file holds. A courier that
    # paraphrased, a display-only success text, or a report replaced after the run cannot be finalized.
    if result["gate"]["report"] != gate:
        raise ManifestError("Gate evidence the controller recorded disagrees with the durable gate report")
    for index, (check, command) in enumerate(zip(gate["checks"], commands, strict=True)):
        if check.get("command") != command or check.get("status") != "passed" or check.get("exitCode") != 0:
            raise ManifestError("Registered gate command did not pass")
        for stream in ("stdout", "stderr"):
            path = manifest_path.parent / f"gate-{index}.{stream}"
            if check.get(stream) != str(path) or not path.is_file() or check.get(f"{stream}Sha256") != digest(path):
                raise ManifestError("Gate output evidence missing or changed")
    verify_manifest(manifest_path)
    if digest(result_path) != result_hash or digest(gate_path) != gate_hash:
        raise ManifestError("Review or gate evidence changed during finalization")
    report = {
        "version": 1,
        "runId": manifest["runId"],
        "snapshotId": manifest["snapshotId"],
        "accepted": clean,
        "status": "accepted" if clean else "changes-required",
        "resultPath": str(result_path),
        "resultSha256": result_hash,
        "gateSha256": gate_hash,
        "coverageComplete": result.get("coverageComplete") is True,
    }
    write_json(manifest_path.parent / "acceptance.json", report)
    return report


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=["run", "finalize"])
    parser.add_argument("--manifest", required=True)
    parser.add_argument("--result")
    parser.add_argument(
        "--registry", help="run: owned-process registry (owned_processes.py) the unit's cleanup barrier stops"
    )
    args = parser.parse_args()
    try:
        if args.command == "finalize" and not args.result:
            raise ManifestError("finalize requires the full workflow --result JSON artifact")
        if args.command == "finalize" and args.registry:
            raise ManifestError("--registry applies to run; finalize starts no process")
        report = (
            run_gate(args.manifest, args.registry) if args.command == "run" else finalize(args.manifest, args.result)
        )
        print(json.dumps(report))
        return 0 if report.get("status") in {"passed", "accepted"} else 1
    except (OSError, ValueError, KeyError, TypeError) as exc:
        reason = str(exc) if isinstance(exc, ManifestError) else type(exc).__name__
        print(f"Gate/result verification refused: {reason}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
