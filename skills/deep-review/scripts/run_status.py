#!/usr/bin/env python3
"""Project one review run's durable artifacts into four separate answers: did execution end, what did the
reviewers cover, was anything accepted, and what does recovery need. Never dispatch, decide or accept.

A host completion notification, a controller's returned value and a saved result all arrive as one blob,
and a reader who collapses them into "done" mistakes an execution that ended for a review that was
accepted. This helper answers each question only from the file that is evidence for it: execution and
coverage from the full result at outputs.resultPath, acceptance from acceptance.json and nothing else,
recovery from run_attempt.py classify. It records every dispatch and reviewer in one state vocabulary,
the attempt series the run belongs to, and the obligations still open, then names the files and digests it
read so the projection (status.json) can always be regenerated and checked. The report surface is the
html-report note model, so there is no second dashboard to keep in step. Findings are shown by the final
disposition the controller recorded (fixed, refuted, open, unruled, degraded), never by the historical union
of what some round once confirmed, so a finding fixed in round 1 is not reported as open and a refuted claim
does not disappear.
"""

import argparse
import hashlib
import json
import os
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path

from run_attempt import DECIDED, classify, load_manifest
from run_manifest import ManifestError, parse_json, read_bytes, require
from work_ledger import LedgerError, load, open_units

SCRIPTS = Path(__file__).resolve().parent
# The one vocabulary every recorded unit is projected onto, so a reader never has to know that the controller
# writes "returned" for a dispatch that came back or "pending" for a reviewer that never launched.
STATES = ("queued", "running", "succeeded", "skipped", "failed", "cancelled")
DISPATCH = {"returned": "succeeded"}
REVIEWER = {"disabled": "skipped", "unavailable": "skipped", "pending": "skipped"}
# What a unit that ended without a result means for execution, by the ledger barrier's recorded outcome.
CLOSED = {"failure": "failed", "cancellation": "cancelled", "retry": "cancelled"}
ARTIFACTS = ("manifestPath", "argsFile", "resultPath", "patchPath", "gatePath", "acceptancePath")
# The controller's final disposition of every finding it ever judged (references/disposition.md), in the
# order a reader acts on them: what still needs work first, then what is settled, then what nobody can say.
OUTCOMES = ("open", "degraded", "unruled", "fixed", "refuted")


def now():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def python(name, *args):
    return " ".join(["python3", f'"{SCRIPTS / name}"', *args])


def replace_json(path, data):
    """status.json is a projection, not evidence: it is regenerated and replaced atomically, never appended to."""
    with tempfile.NamedTemporaryFile("w", dir=path.parent, prefix=f".{path.name}.", delete=False) as stream:
        json.dump(data, stream, indent=2)
        stream.write("\n")
        stream.flush()
        os.fsync(stream.fileno())
    os.replace(stream.name, path)


def read_result(manifest, manifest_path):
    """The full stored result when it is this run's; None when missing, unreadable or another run's."""
    path = Path(manifest["outputs"]["resultPath"])
    if not path.is_file():
        return None
    try:
        result = parse_json(read_bytes(path, "result"), "result")
    except ManifestError:
        return None
    expected = {"id": manifest["runId"], "snapshotId": manifest["snapshotId"], "manifestPath": str(manifest_path)}
    return result if isinstance(result, dict) and result.get("run") == expected else None


def execution(result, category):
    """How far the controller's own dispatching got; nothing here is about findings or acceptance."""
    counts = dict.fromkeys(STATES, 0)
    if result is None:
        # No result says nothing ended; unreadable or foreign bytes are a return that proves nothing.
        return {
            "state": "running" if category == "missing" else "failed",
            "started": None,
            "stopReason": None,
            "stop": None,
            "counts": counts,
            "units": [],
        }
    admission = result.get("admission") if isinstance(result.get("admission"), dict) else {}
    units = []
    for entry in admission.get("dispatches") or []:
        if not isinstance(entry, dict):
            continue
        state = DISPATCH.get(entry.get("state"), entry.get("state"))
        if state in counts:
            counts[state] += 1
        unit = {"label": entry.get("label"), "phase": entry.get("phase"), "state": state, "reason": entry.get("reason")}
        if "executor" in entry:
            unit["executor"] = entry["executor"]
        units.append(unit)
    stop = admission.get("stopReason") or None
    if counts["queued"] or counts["running"]:
        state = "running"
    elif counts["cancelled"]:
        state = "cancelled"
    elif stop:
        state = "failed"
    elif admission.get("started") == 0:
        state = "skipped"
    else:
        state = "succeeded"
    return {
        "state": state,
        "started": admission.get("started"),
        "stopReason": stop,
        "stop": admission.get("stop"),
        "counts": counts,
        "units": units,
    }


def coverage(result):
    """Which reviewer lanes produced evidence, whether their full payloads were stored, and the boss outcome."""
    if result is None:
        return {
            "complete": False,
            "rounds": None,
            "payloadsStored": None,
            "reviewers": {},
            "attempts": [],
            "boss": None,
        }
    stored = {
        payload.get("key")
        for payload in result.get("reviewResults") or []
        if isinstance(payload, dict) and isinstance(payload.get("result"), dict)
    }
    reviewers = {}
    for key, lane in (result.get("coverage") or {}).items():
        if not isinstance(lane, dict):
            continue
        state = REVIEWER.get(lane.get("state"), lane.get("state"))
        reviewers[key] = {
            "policy": lane.get("policy"),
            "state": state,
            "reason": lane.get("reason") or "",
            # A succeeded lane whose payload is absent was summarised before it was saved; that is a truncated result.
            "payloadStored": (key in stored) if state == "succeeded" else None,
        }
    boss = [
        d for d in result.get("degraded") or [] if isinstance(d, dict) and str(d.get("key", "")).startswith("boss:")
    ]
    return {
        "complete": result.get("coverageComplete") is True,
        "rounds": result.get("rounds"),
        "payloadsStored": all(r["payloadStored"] for r in reviewers.values() if r["state"] == "succeeded"),
        "reviewers": reviewers,
        "attempts": [
            {"key": a.get("key"), "round": a.get("round"), "state": a.get("state"), "reason": a.get("reason") or ""}
            for a in result.get("attempts") or []
            if isinstance(a, dict)
        ],
        "boss": {"state": "failed", "reason": boss[0].get("reason")} if boss else {"state": "succeeded", "reason": ""},
    }


def acceptance(manifest, result):
    """Only acceptance.json decides. The result's own word and the gate it recorded are shown, not believed."""
    path = Path(manifest["outputs"]["acceptancePath"])
    record = {"status": "pending-external-verification", "accepted": False, "decidedBy": None}
    if path.is_file():
        try:
            decision = parse_json(read_bytes(path, "acceptance"), "acceptance")
        except ManifestError:
            decision = None
        if (
            isinstance(decision, dict)
            and decision.get("runId") == manifest["runId"]
            and decision.get("status") in {"accepted", "changes-required"}
        ):
            record = {
                "status": decision["status"],
                "accepted": decision.get("accepted") is True,
                "decidedBy": str(path),
            }
    gate = result.get("gate") if result is not None else None
    record["gate"] = (
        {"state": gate.get("state"), "reason": gate.get("reason") or ""}
        if isinstance(gate, dict)
        else {"state": "missing", "reason": "no result" if result is None else "the result records no gate"}
    )
    record["resultClaims"] = result.get("acceptance") if result is not None else None
    record["openFindings"] = len(result.get("currentFindings") or []) if result is not None else 0
    return record


def blocking(report, result, manifest_path):
    """The precise gate the run is stuck at, and the one command that moves it; None once a decision exists."""
    category = report["category"]
    reasons = report["reasons"]
    manifest = f'--manifest "{manifest_path}"'
    first = reasons[0] if reasons else None
    if category in {"accepted", "changes-required"}:
        return None
    if category == "complete":
        result_path = report["evidence"]["resultPath"]
        command = python("run_gate.py", "finalize", manifest, f'--result "{result_path}"')
        return {
            "stage": "finalize",
            "reason": "a passed gate is recorded and acceptance.json is not written",
            "command": command,
        }
    if category == "missing":
        if report["treeDrift"]:
            return {
                "stage": "inputs",
                "reason": report["treeDrift"],
                "command": python("run_manifest.py", "init", "..."),
            }
        return {
            "stage": "result",
            "reason": "no result at resultPath; nothing says the attempt ended",
            "command": python("run_manifest.py", "resume", manifest),
        }
    if category == "partial" and not report["terminal"]:
        reason = first or "a dispatch was still active when the result was written"
        return {
            "stage": "execution",
            "reason": reason,
            "command": python("run_attempt.py", "retry", manifest, '--abandon "<why the worker is gone>"'),
        }
    if category == "stale":
        return {"stage": "inputs", "reason": first, "command": python("run_manifest.py", "init", "...")}
    if category == "modified":
        reason = "fixes were applied; the tree no longer matches the manifest and needs one fresh read-only review"
        return {"stage": "post-fix review", "reason": reason, "command": python("run_manifest.py", "init", "...")}
    retry = (
        python("run_attempt.py", "retry", manifest) if report["retryable"] else python("run_manifest.py", "init", "...")
    )
    if category == "launch-blocked":
        return {"stage": "dispatch", "reason": first or "nothing was dispatched", "command": retry}
    if category == "malformed":
        return {"stage": "result", "reason": first, "command": retry}
    gate = result.get("gate") if isinstance(result.get("gate"), dict) else {}
    if gate.get("state") in {"failed", "missing"}:
        return {"stage": "gate", "reason": gate.get("reason"), "command": retry}
    lanes = set(result.get("coverage") or {})
    degraded = [d for d in result.get("degraded") or [] if isinstance(d, dict)]
    for stage, match in (("coverage", lambda key: key in lanes), ("adjudication", lambda key: key.startswith("boss:"))):
        hit = next((d for d in degraded if match(str(d.get("key", "")))), None)
        if hit:
            return {"stage": stage, "reason": hit.get("reason"), "command": retry}
    stop = (result.get("admission") or {}).get("stopReason") if isinstance(result.get("admission"), dict) else None
    return {"stage": "execution", "reason": stop or first or "incomplete", "command": retry}


def recovery(report, result, manifest, manifest_path):
    outputs = manifest["outputs"]
    preserved = {key: outputs[key] for key in ARTIFACTS if Path(outputs[key]).is_file()}
    return {
        "category": report["category"],
        "terminal": report["terminal"],
        "retryable": report["retryable"],
        "treeDrift": report["treeDrift"],
        "replacedBy": report["replacedBy"],
        "blocking": blocking(report, result, manifest_path),
        "next": report["next"],
        "preserved": preserved,
    }


def history(manifest):
    """Every attempt of this request, in order: the chain of priorRunId backwards and replacements forwards."""
    root = Path(manifest["outputs"]["artifactDir"])
    by_id = {manifest["runId"]: manifest}
    for path in sorted(root.iterdir()) if root.is_dir() else []:
        if path.is_dir() and (path / "manifest.json").is_file():
            try:
                sibling = load_manifest(path / "manifest.json")
            except ManifestError:
                continue
            by_id.setdefault(sibling["runId"], sibling)
    series = {manifest["runId"]: manifest}
    current = manifest
    while current["attempt"].get("priorRunId") in by_id and current["attempt"]["priorRunId"] not in series:
        current = by_id[current["attempt"]["priorRunId"]]
        series[current["runId"]] = current
    frontier = list(series)
    while frontier:
        run_id = frontier.pop()
        for sibling in by_id.values():
            if sibling["attempt"].get("priorRunId") == run_id and sibling["runId"] not in series:
                series[sibling["runId"]] = sibling
                frontier.append(sibling["runId"])
    entries = []
    for run_id, entry in series.items():
        outputs = entry["outputs"]
        decided = Path(outputs["acceptancePath"]).is_file()
        replaced = any(s["attempt"].get("priorRunId") == run_id for s in series.values())
        if decided:
            try:
                decision = parse_json(read_bytes(Path(outputs["acceptancePath"]), "acceptance"), "acceptance")
                outcome = decision.get("status") if isinstance(decision, dict) else "malformed"
            except ManifestError:
                outcome = "malformed"
        elif replaced:
            outcome = "replaced"
        elif Path(outputs["resultPath"]).is_file():
            outcome = "returned"
        else:
            outcome = "missing"
        entries.append(
            {
                "attempt": entry["attempt"]["number"],
                "runId": run_id,
                "priorRunId": entry["attempt"].get("priorRunId"),
                "cause": entry["attempt"].get("cause"),
                "outcome": outcome,
                "current": run_id == manifest["runId"],
                "runDir": outputs["runDir"],
            }
        )
    return sorted(entries, key=lambda e: (e["attempt"], e["runId"]))


def ledger_unit(ledger_path, unit):
    """The ledger's view of the unit this run serves: its state, its attempt records and what is still owed."""
    require(isinstance(unit, dict), "the manifest names no ledger unit; init it with --task-id and --attempt")
    ledger = load(ledger_path)
    require(unit["taskId"] in ledger["tasks"], f"unknown task {unit['taskId']} in {ledger_path}")
    task = ledger["tasks"][unit["taskId"]]
    records = [
        {
            "role": a["role"],
            "started": a.get("start") is not None,
            "returned": a.get("result") is not None,
            "disposition": (a.get("result") or {}).get("disposition"),
            "barrier": (a.get("barrier") or {}).get("outcome"),
        }
        for a in task["attempts"]
        if a["attempt"] == unit["attempt"]
    ]
    verification = task.get("verification") or {}
    return {
        "ledger": str(ledger_path),
        "task": unit["taskId"],
        "attempt": unit["attempt"],
        "state": task["state"],
        "terminal": task["terminal"],
        "units": records,
        "openUnits": open_units(ledger),
        "openObligations": task.get("openObligations") or [],
        "gatedActions": task.get("gatedActions") or [],
        "unresolved": verification.get("unresolved") or [],
    }


def disposition(result):
    """Where each judged finding stands now, copied from the result's own disposition record.

    Nothing here is derived from `confirmed`: that list is the union of every round's confirmations and cannot
    say which were fixed since. A result that carries no record (an older controller) reports none rather than
    presenting the union as all open or all resolved.
    """
    counts = dict.fromkeys(OUTCOMES, 0)
    items = []
    record = result.get("disposition") if result is not None else None
    if not isinstance(record, dict):
        return {"recorded": False, "counts": counts, "items": items}
    for outcome in OUTCOMES:
        for f in record.get(outcome) or []:
            if not isinstance(f, dict):
                continue
            counts[outcome] += 1
            items.append(
                {
                    "outcome": outcome,
                    "severity": f.get("severity"),
                    "file": f.get("file"),
                    "line": f.get("line_start"),
                    "description": str(f.get("description", ""))[:120],
                    "sources": [str(s) for s in f.get("sources") or []],
                    "rounds": [r.get("round") for r in f.get("rounds") or [] if isinstance(r, dict)],
                    "reason": f.get("outcomeReason") or "",
                }
            )
    return {"recorded": True, "counts": counts, "items": items}


def obligations(result, ledger):
    findings = [
        f"{f.get('severity')} {f.get('file')}:{f.get('line_start')} {str(f.get('description', ''))[:120]}"
        for f in (result.get("currentFindings") if result is not None else None) or []
        if isinstance(f, dict)
    ]
    return {"findings": findings, "ledger": ledger}


def sources(manifest):
    found = {}
    for key in ARTIFACTS:
        path = Path(manifest["outputs"][key])
        if path.is_file():
            found[key] = {"path": str(path), "sha256": hashlib.sha256(path.read_bytes()).hexdigest()}
    return found


def status(manifest_path, ledger_path=None):
    """Compose the projection; raise ManifestError or LedgerError only when an input cannot be read."""
    manifest_path = Path(manifest_path).resolve()
    report = classify(manifest_path)
    manifest = load_manifest(manifest_path)
    result = read_result(manifest, manifest_path)
    ledger = ledger_unit(ledger_path, manifest["unit"]) if ledger_path else None
    run = execution(result, report["category"])
    if result is None and ledger:
        # The worker never returned; the barrier that closed its unit is the only record of how it ended.
        closed = next((u["barrier"] for u in ledger["units"] if u["role"] == "implementer" and u["barrier"]), None)
        if closed in CLOSED:
            run["state"] = CLOSED[closed]
    return {
        "version": 1,
        "generatedAt": now(),
        "runId": manifest["runId"],
        "snapshotId": manifest["snapshotId"],
        "rubricId": manifest["rubricId"],
        "attempt": manifest["attempt"],
        "unit": manifest["unit"],
        "execution": run,
        "coverage": coverage(result),
        "acceptance": acceptance(manifest, result),
        "recovery": recovery(report, result, manifest, manifest_path),
        "history": history(manifest),
        "disposition": disposition(result),
        "obligations": obligations(result, ledger),
        "sources": sources(manifest),
    }


def table(columns, rows):
    return {
        "type": "table",
        "columns": columns,
        "rows": [[str(c if c is not None else "") for c in row] for row in rows],
    }


def note(record):
    """The html-report note model for one run: render_note.py turns it into HTML or Markdown."""
    run, cover, accept, recover = record["execution"], record["coverage"], record["acceptance"], record["recovery"]
    gate = accept["gate"]
    block = recover["blocking"]
    dimensions = table(
        ["dimension", "state", "detail"],
        [
            ["execution", run["state"], f"{run['started'] or 0} dispatched; {run['stopReason'] or 'no stop recorded'}"],
            [
                "coverage",
                "complete" if cover["complete"] else "incomplete",
                ", ".join(f"{k} {v['state']}" for k, v in cover["reviewers"].items()) or "no reviewer record",
            ],
            [
                "acceptance",
                accept["status"],
                f"gate {gate['state']}; decided by {accept['decidedBy'] or 'nothing yet'}",
            ],
            ["recovery", recover["category"], recover["next"]],
        ],
    )
    blocking_text = (
        f"Stage `{block['stage']}`: {block['reason']}. Next command: `{block['command']}`"
        if block
        else f"None: the decision is recorded ({accept['status']})."
    )
    final = record["disposition"]
    if final["recorded"]:
        summary = ", ".join(f"{final['counts'][o]} {o}" for o in OUTCOMES)
        findings_body = [
            {"type": "prose", "md": f"Each finding appears once, under where it stands now: {summary}."},
            table(
                ["outcome", "severity", "file:line", "finding", "sources", "rounds", "why"],
                [
                    [
                        i["outcome"],
                        i["severity"],
                        f"{i['file']}:{i['line']}",
                        i["description"],
                        ", ".join(i["sources"]),
                        ", ".join(str(r) for r in i["rounds"]),
                        i["reason"],
                    ]
                    for i in final["items"]
                ],
            ),
        ]
    else:
        findings_body = [
            {
                "type": "prose",
                "md": "No final disposition is recorded in this result; the historical `confirmed` list is not a current state.",
            }
        ]
    owed = record["obligations"]
    lines = [f"finding: {f}" for f in owed["findings"]]
    if owed["ledger"]:
        lines += [
            f"ledger {owed['ledger']['task']} attempt {owed['ledger']['attempt']}: state {owed['ledger']['state']}"
        ]
        lines += [f"obligation: {o}" for o in owed["ledger"]["openObligations"]]
        lines += [f"gated: {g}" for g in owed["ledger"]["gatedActions"]]
        lines += [f"unresolved: {u}" for u in owed["ledger"]["unresolved"]]
    sections = [
        ("state", "Four separate answers", [dimensions]),
        ("blocking", "Blocking gate", [{"type": "prose", "md": blocking_text}]),
        (
            "dispatches",
            "Dispatches",
            [
                table(
                    ["label", "phase", "state", "reason"],
                    [[u["label"], u["phase"], u["state"], u["reason"]] for u in run["units"]],
                )
            ],
        ),
        (
            "reviewers",
            "Reviewer coverage",
            [
                table(
                    ["reviewer", "policy", "state", "payload stored", "reason"],
                    [
                        [k, v["policy"], v["state"], v["payloadStored"], v["reason"]]
                        for k, v in cover["reviewers"].items()
                    ],
                )
            ],
        ),
        ("disposition", "Findings by final disposition", findings_body),
        (
            "history",
            "Attempts of this request",
            [
                table(
                    ["attempt", "run", "outcome", "cause"],
                    [
                        [h["attempt"], h["runId"] + (" (this run)" if h["current"] else ""), h["outcome"], h["cause"]]
                        for h in record["history"]
                    ],
                )
            ],
        ),
        (
            "obligations",
            "Unresolved obligations",
            [{"type": "prose", "md": line} for line in lines] or [{"type": "prose", "md": "None recorded."}],
        ),
        (
            "sources",
            "Files this status was read from",
            [
                table(
                    ["artifact", "path", "sha256"], [[k, v["path"], v["sha256"]] for k, v in record["sources"].items()]
                )
            ],
        ),
    ]
    title = f"Review run {record['runId']} attempt {record['attempt']['number']}"
    return {
        "meta": {"title": title, "flavor": "utilitarian", "theme": "auto"},
        "blocks": [{"type": "section", "id": sid, "title": heading, "body": body} for sid, heading, body in sections],
    }


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    commands = parser.add_subparsers(dest="command", required=True)
    show = commands.add_parser("status", help="execution, coverage, acceptance and recovery of one run, kept apart")
    show.add_argument("--manifest", required=True)
    show.add_argument(
        "--ledger", help="work ledger holding the unit the manifest names; adds its state and obligations"
    )
    show.add_argument("--write", action="store_true", help="also write status.json beside the manifest")
    show.add_argument(
        "--format", choices=("json", "note"), default="json", help="note prints the html-report note model"
    )
    options = parser.parse_args(argv)
    try:
        record = status(options.manifest, options.ledger)
    except (ManifestError, LedgerError) as exc:
        print(f"run-status: {exc}", file=sys.stderr)
        return 2
    except (OSError, RuntimeError, RecursionError):
        print("run-status: cannot read run artifacts; check paths and permissions", file=sys.stderr)
        return 2
    if options.write:
        replace_json(Path(options.manifest).resolve().parent / "status.json", record)
    print(json.dumps(record if options.format == "json" else note(record)))
    return 0 if record["recovery"]["category"] in DECIDED else 1


if __name__ == "__main__":
    raise SystemExit(main())
