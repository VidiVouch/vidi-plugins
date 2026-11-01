#!/usr/bin/env python3
"""Keep a durable work ledger keyed by task ID; never dispatch, commit, or publish."""

import argparse
import hashlib
import json
import os
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path

from run_manifest import ManifestError, identity, reject_secrets, snapshot

ROLES = ("implementer", "verifier")
IMPLEMENT_STATUSES = {"completed", "partial", "blocked-approval", "failed"}
VERIFY_VERDICTS = {"accepted", "changes-required", "blocked-approval", "failed-verification"}
# States an overseer sets by hand because no worker ever produces them.
MARK_STATES = {"pre-recorded", "blocked-dependency", "not-started", "failed"}
TERMINAL = {"accepted", "awaiting-approval", "failed", "pre-recorded", "blocked-dependency", "not-started"}


class LedgerError(ValueError):
    """A missing, malformed, or contradictory ledger entry."""


def require(condition, message):
    if not condition:
        raise LedgerError(message)


def now():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def sha256(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def load_json(path, label):
    try:
        return json.loads(Path(path).read_text())
    except (OSError, ValueError) as exc:
        raise LedgerError(f"cannot read {label} JSON") from exc


def load(path):
    ledger = load_json(path, "ledger")
    require(
        isinstance(ledger, dict) and ledger.get("version") == 1 and isinstance(ledger.get("tasks"), dict),
        "invalid ledger",
    )
    return ledger


def save(path, ledger):
    # Rewrite atomically so an interrupted write leaves the previous ledger intact.
    path = Path(path)
    ledger["updatedAt"] = now()
    with tempfile.NamedTemporaryFile("w", dir=path.parent, prefix=f".{path.name}.", delete=False) as stream:
        json.dump(ledger, stream, indent=1, sort_keys=True)
        stream.write("\n")
        stream.flush()
        os.fsync(stream.fileno())
    os.replace(stream.name, path)


def task_entry(ledger, task_id):
    require(task_id in ledger["tasks"], f"unknown task {task_id}")
    return ledger["tasks"][task_id]


def derive(task):
    """State follows the latest attempt's evidence, or an explicit mark; nothing else."""
    attempts = task["attempts"]
    if task.get("mark"):
        state = task["mark"]["state"]
    elif not attempts:
        state = "pending"
    else:
        last = max(a["attempt"] for a in attempts)
        impl = next((a for a in attempts if a["attempt"] == last and a["role"] == "implementer"), None)
        ver = next((a for a in attempts if a["attempt"] == last and a["role"] == "verifier"), None)
        if impl["result"]["status"] == "failed":
            state = "failed"
        elif ver is None:
            state = "in-progress"
        else:
            verdict = ver["result"]["verdict"]
            state = {"accepted": "accepted", "blocked-approval": "awaiting-approval"}.get(verdict, verdict)
    impls = [a for a in attempts if a["role"] == "implementer"]
    vers = [a for a in attempts if a["role"] == "verifier"]
    latest_impl = impls[-1]["result"] if impls else {}
    latest_ver = vers[-1] if vers else None
    task.update(
        state=state,
        terminal=state in TERMINAL,
        summary=(latest_ver["result"] if latest_ver else latest_impl).get("summary"),
        openObligations=latest_impl.get("openObligations", []),
        gatedActions=latest_impl.get("gatedActions", []),
        verification=(
            {k: latest_ver[k] for k in ("attempt", "owner", "recordedAt")}
            | {k: latest_ver["result"].get(k) for k in ("verdict", "issues", "checks", "notePath")}
            if latest_ver
            else None
        ),
        artifacts=sorted(
            {p for a in attempts for p in [a["result"].get("notePath"), *a["result"].get("filesChanged", [])] if p}
        ),
    )
    return task


def init(options):
    path = Path(options.ledger)
    require(not path.exists(), "ledger already exists; record into it instead of recreating it")
    spec = load_json(options.tasks, "tasks")
    tasks = spec.get("tasks") if isinstance(spec, dict) else spec
    require(isinstance(tasks, list) and tasks, "tasks must be a non-empty list or an object with a tasks list")
    ledger = {"version": 1, "runId": options.run_id, "createdAt": now(), "tasks": {}}
    for task in tasks:
        task_id = task.get("id") if isinstance(task, dict) else None
        require(isinstance(task_id, str) and task_id and task_id not in ledger["tasks"], "tasks need unique string ids")
        entry = {"deps": list(task.get("deps", [])), "mode": task.get("mode"), "attempts": [], "mark": None}
        if options.packets:
            packet = Path(options.packets) / f"{task_id}.json"
            require(packet.is_file(), f"missing packet for {task_id}")
            entry["packetSha256"] = sha256(packet)
        ledger["tasks"][task_id] = derive(entry)
    for entry in ledger["tasks"].values():
        require(all(dep in ledger["tasks"] for dep in entry["deps"]), "a dependency names an unknown task")
    path.parent.mkdir(parents=True, exist_ok=True)
    save(path, ledger)
    return {"ledger": str(path), "tasks": len(ledger["tasks"])}


def snapshot_input(options):
    repo = Path(options.repo).resolve()
    state = snapshot(repo, [])
    return {
        "repo": str(repo),
        "head": state["head"],
        "indexSha256": state["indexSha256"],
        "snapshotId": identity(state),
    }


def record(options):
    ledger = load(options.ledger)
    task = task_entry(ledger, options.task)
    require(options.role in ROLES, "role must be implementer or verifier")
    require(options.attempt >= 1, "attempt numbers start at 1")
    require(
        not any(a["attempt"] == options.attempt and a["role"] == options.role for a in task["attempts"]),
        "this attempt and role is already recorded; a retry needs a new attempt number",
    )
    if options.role == "verifier":
        require(
            any(a["attempt"] == options.attempt and a["role"] == "implementer" for a in task["attempts"]),
            "a verification needs the implementer record for the same attempt first",
        )
    result = load_json(options.result, "result")
    require(isinstance(result, dict), "result must be a JSON object")
    reject_secrets(result)
    if options.role == "implementer":
        require(result.get("status") in IMPLEMENT_STATUSES, "implementer result needs a known status")
    else:
        require(result.get("verdict") in VERIFY_VERDICTS, "verifier result needs a known verdict")
    entry = {
        "attempt": options.attempt,
        "role": options.role,
        "owner": options.owner,
        "recordedAt": now(),
        "input": load_json(options.input, "input snapshot") if options.input else None,
        "result": result,
    }
    if options.packet:
        entry["packetSha256"] = sha256(options.packet)
        require(entry["packetSha256"] == task.get("packetSha256", entry["packetSha256"]), "packet changed since init")
    task["attempts"].append(entry)
    derive(task)
    save(options.ledger, ledger)
    return {"task": options.task, "state": task["state"], "terminal": task["terminal"]}


def mark(options):
    ledger = load(options.ledger)
    task = task_entry(ledger, options.task)
    require(options.state in MARK_STATES, f"mark state must be one of {sorted(MARK_STATES)}")
    task["mark"] = {"state": options.state, "reason": options.reason, "owner": options.owner, "recordedAt": now()}
    derive(task)
    save(options.ledger, ledger)
    return {"task": options.task, "state": task["state"], "terminal": task["terminal"]}


def packet(options):
    """Return exactly what one worker needs: its own packet and its dependencies' outcomes."""
    ledger = load(options.ledger)
    task = task_entry(ledger, options.task)
    brief = load_json(Path(options.packets) / f"{options.task}.json", "packet")
    if "packetSha256" in task:
        require(
            sha256(Path(options.packets) / f"{options.task}.json") == task["packetSha256"], "packet changed since init"
        )
    dependencies = {}
    for dep in task["deps"]:
        entry = ledger["tasks"][dep]
        dependencies[dep] = {
            "state": entry["state"],
            "summary": entry.get("summary"),
            "notePath": next(
                (a["result"].get("notePath") for a in reversed(entry["attempts"]) if a["role"] == "implementer"), None
            ),
            "verifyNote": entry["verification"]["notePath"] if entry.get("verification") else None,
            "openObligations": entry.get("openObligations", []),
            "gatedActions": entry.get("gatedActions", []),
        }
    return {
        "runId": ledger["runId"],
        "task": options.task,
        "attempt": options.attempt,
        "packet": brief,
        "dependencies": dependencies,
    }


def summary(options):
    ledger = load(options.ledger)
    counts = {}
    for entry in ledger["tasks"].values():
        counts[entry["state"]] = counts.get(entry["state"], 0) + 1
    return {
        "runId": ledger["runId"],
        "updatedAt": ledger.get("updatedAt"),
        "counts": dict(sorted(counts.items())),
        "open": sorted(t for t, e in ledger["tasks"].items() if not e["terminal"]),
        "openObligations": {t: e["openObligations"] for t, e in ledger["tasks"].items() if e.get("openObligations")},
        "gatedActions": {t: e["gatedActions"] for t, e in ledger["tasks"].items() if e.get("gatedActions")},
    }


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    create = commands.add_parser("init", help="create a ledger with one pending entry per task")
    create.add_argument("--ledger", required=True)
    create.add_argument("--run-id", required=True)
    create.add_argument(
        "--tasks", required=True, help="JSON list of {id, deps, mode} or an object holding one as tasks"
    )
    create.add_argument(
        "--packets", help="directory of <ID>.json packets whose digests become the input brief identity"
    )
    snap = commands.add_parser("snapshot", help="print the repository input identity to record before dispatch")
    snap.add_argument("--repo", required=True)
    rec = commands.add_parser("record", help="append one worker result to a task")
    rec.add_argument("--ledger", required=True)
    rec.add_argument("--task", required=True)
    rec.add_argument("--attempt", type=int, required=True)
    rec.add_argument("--role", required=True, choices=ROLES)
    rec.add_argument("--owner", required=True, help="who did the work, for example the agent id and label")
    rec.add_argument("--result", required=True, help="the worker's structured result JSON file")
    rec.add_argument("--input", help="snapshot JSON captured before the worker started")
    rec.add_argument("--packet", help="the packet the worker received; its digest must match init")
    close = commands.add_parser("mark", help="set a state no worker produces, with a reason")
    close.add_argument("--ledger", required=True)
    close.add_argument("--task", required=True)
    close.add_argument("--state", required=True)
    close.add_argument("--reason", required=True)
    close.add_argument("--owner", required=True)
    pack = commands.add_parser("packet", help="print one worker's packet plus its dependencies' outcomes, nothing else")
    pack.add_argument("--ledger", required=True)
    pack.add_argument("--task", required=True)
    pack.add_argument("--packets", required=True)
    pack.add_argument("--attempt", type=int, default=1)
    show = commands.add_parser("summary", help="state counts, non-terminal tasks, open obligations and gated actions")
    show.add_argument("--ledger", required=True)
    options = parser.parse_args(argv)
    handlers = {
        "init": init,
        "snapshot": snapshot_input,
        "record": record,
        "mark": mark,
        "packet": packet,
        "summary": summary,
    }
    try:
        result = handlers[options.command](options)
    except (LedgerError, ManifestError) as exc:
        print(f"work-ledger: {exc}", file=sys.stderr)
        return 1
    print(json.dumps(result, indent=1, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
