#!/usr/bin/env python3
"""Keep a durable work ledger keyed by task ID; never dispatch, commit, or publish."""

import argparse
import hashlib
import json
import os
import sys
import tempfile
from collections import Counter
from datetime import datetime, timedelta, timezone
from pathlib import Path

from hand_back import DISPOSITIONS, HandBackError, check, disposition, files, unresolved
from owned_processes import ProcessError, stop
from run_manifest import ManifestError, absolute, digest, git, identity, reject_secrets, snapshot

ROLES = tuple(DISPOSITIONS)
# States an overseer sets by hand because no worker ever produces them.
MARK_STATES = {"pre-recorded", "blocked-dependency", "not-started", "failed"}
TERMINAL = {"accepted", "awaiting-approval", "failed", "pre-recorded", "blocked-dependency", "not-started"}
# Why a unit ended. The barrier records it for every unit, including one that never returned a result.
OUTCOMES = ("success", "failure", "cancellation", "retry")
# A unit that ended without a result: a failure outcome is a failed attempt, the other two leave the task open.
UNRETURNED = {"failure": "failed", "cancellation": "cancelled", "retry": "cancelled"}


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
        if impl["result"] is None:
            # Started and neither returned nor closed is a running worker; closed without a result follows the barrier.
            state = UNRETURNED[impl["barrier"]["outcome"]] if impl.get("barrier") else "running"
        elif disposition(impl["result"]) == "failed":
            state = "failed"
        elif ver is None or ver["result"] is None:
            state = "running" if ver is not None and not ver.get("barrier") else "in-progress"
        else:
            verdict = disposition(ver["result"])
            state = {"accepted": "accepted", "blocked-approval": "awaiting-approval"}.get(verdict, verdict)
    impls = [a for a in attempts if a["role"] == "implementer"]
    vers = [a for a in attempts if a["role"] == "verifier" and a["result"] is not None]
    latest_impl = (impls[-1]["result"] if impls else None) or {}
    latest_ver = vers[-1] if vers else None
    task.update(
        state=state,
        terminal=state in TERMINAL,
        summary=(latest_ver["result"] if latest_ver else latest_impl).get("summary"),
        openObligations=latest_impl.get("openObligations", []),
        gatedActions=latest_impl.get("gatedActions", []),
        verification=(
            {k: latest_ver[k] for k in ("attempt", "owner", "recordedAt")}
            | {
                "disposition": disposition(latest_ver["result"]),
                "unresolved": unresolved(latest_ver["result"]),
                "checks": latest_ver["result"].get("checks"),
                "notePath": latest_ver["result"].get("notePath"),
            }
            if latest_ver
            else None
        ),
        artifacts=sorted(
            {
                p
                for a in attempts
                if a["result"] is not None
                for p in [a["result"].get("notePath"), *files(a["result"])]
                if p
            }
        ),
    )
    return task


def unit(task_id, attempt, role):
    return f"{task_id} attempt {attempt} {role}"


def open_units(ledger):
    """Units started or recorded whose cleanup barrier has not run; no new unit starts while one exists."""
    return [
        unit(task_id, a["attempt"], a["role"])
        for task_id, entry in ledger["tasks"].items()
        for a in entry["attempts"]
        if not a.get("barrier")
    ]


def overdue_units(ledger, at=None):
    """Open units whose recorded deadline has passed without a result: the watchdog's fallback signal."""
    at = at or datetime.now(timezone.utc)
    return [
        unit(task_id, a["attempt"], a["role"])
        for task_id, entry in ledger["tasks"].items()
        for a in entry["attempts"]
        if a["result"] is None
        and not a.get("barrier")
        and (a.get("start") or {}).get("deadline")
        and datetime.fromisoformat(a["start"]["deadline"]) <= at
    ]


def require_closed(ledger):
    unclosed = open_units(ledger)
    require(not unclosed, f"close every open unit before the next starts (work_ledger.py close): {', '.join(unclosed)}")


def find_unit(task, attempt, role):
    return next((a for a in task["attempts"] if a["attempt"] == attempt and a["role"] == role), None)


def has_implementer(task, attempt):
    return find_unit(task, attempt, "implementer") is not None


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


def start_unit(ledger_path, task_id, attempt, role, owner, snap=None, contract=None, timeout=None):
    """Record a unit before its worker is dispatched, so the attempt number is taken and a duplicate is refused.

    The record is the only thing that makes a running worker visible to a restarted controller, and the
    deadline it carries is the fallback a watchdog reads when no completion notification ever arrives."""
    ledger = load(ledger_path)
    task = task_entry(ledger, task_id)
    require(role in ROLES, "role must be implementer or verifier")
    require(attempt >= 1, "attempt numbers start at 1")
    require(snap is None or isinstance(snap, dict), "input snapshot must be a JSON object")
    require(timeout is None or timeout >= 0, "timeout must be a number of seconds")
    require(
        find_unit(task, attempt, role) is None,
        "this attempt and role was already started; a replacement takes the next attempt number",
    )
    require_closed(ledger)  # the old attempt is terminal, and nothing else is running, before this one starts
    require(
        role == "implementer" or has_implementer(task, attempt),
        "a verifier unit needs the implementer record for the same attempt",
    )
    started = datetime.now(timezone.utc)
    entry = {
        "attempt": attempt,
        "role": role,
        "owner": None,
        "recordedAt": None,
        "input": snap,
        "result": None,
        "start": {
            "owner": owner,
            "startedAt": started.isoformat(timespec="seconds"),
            "contract": contract,
            "deadline": (
                (started + timedelta(seconds=timeout)).isoformat(timespec="seconds") if timeout is not None else None
            ),
        },
    }
    task["attempts"].append(entry)
    derive(task)
    save(ledger_path, ledger)
    return {
        "task": task_id,
        "unit": unit(task_id, attempt, role),
        "state": task["state"],
        "deadline": entry["start"]["deadline"],
    }


def start(options):
    snap = load_json(options.input, "input snapshot") if options.input else None
    return start_unit(
        options.ledger,
        options.task,
        options.attempt,
        options.role,
        options.owner,
        snap,
        options.contract,
        options.timeout,
    )


def record(options):
    ledger = load(options.ledger)
    task = task_entry(ledger, options.task)
    require(options.role in ROLES, "role must be implementer or verifier")
    require(options.attempt >= 1, "attempt numbers start at 1")
    existing = find_unit(task, options.attempt, options.role)
    require(
        existing is None or not existing.get("barrier"),
        "this unit was closed by its cleanup barrier; a late result is not recorded, start a new attempt",
    )
    require(
        existing is None or existing["result"] is None,
        "this attempt and role is already recorded; a retry needs a new attempt number",
    )
    if options.role == "verifier":
        require(
            has_implementer(task, options.attempt),
            "a verification needs the implementer record for the same attempt first",
        )
    result = load_json(options.result, "result")
    require(isinstance(result, dict), "result must be a JSON object")
    reject_secrets(result)
    snap = load_json(options.input, "input snapshot") if options.input else None
    if existing is not None and existing["input"] is not None:
        # A started unit already knows what its worker was given; a different snapshot now is a different unit.
        require(snap is None or snap == existing["input"], "input snapshot differs from the one recorded at start")
        snap = existing["input"]
    if options.legacy:
        require(disposition(result) in DISPOSITIONS[options.role], f"{options.role} result needs a known disposition")
    else:
        # The ledger stores only hand-backs whose claims point at a note, a range and checks that ran.
        check(result, options.role, repo=snap.get("repo") if isinstance(snap, dict) else None)
    entry = existing if existing is not None else {"attempt": options.attempt, "role": options.role}
    entry.update(owner=options.owner, recordedAt=now(), input=snap, result=result)
    if options.packet:
        entry["packetSha256"] = sha256(options.packet)
        require(entry["packetSha256"] == task.get("packetSha256", entry["packetSha256"]), "packet changed since init")
    if existing is None:
        task["attempts"].append(entry)
    derive(task)
    save(options.ledger, ledger)
    return {"task": options.task, "state": task["state"], "terminal": task["terminal"]}


def preserve_tree(repo, patch_path):
    """Keep what the unit left in the worktree: HEAD, index identity, the tracked diff as a patch, untracked paths."""
    root = absolute(repo)
    head = git(root, "rev-parse", "--verify", "HEAD").decode().strip()
    index = git(root, "ls-files", "--stage", "-z")
    patch = git(root, "diff-index", "-p", "--binary", "--no-ext-diff", "HEAD")
    untracked = sorted(
        os.fsdecode(n) for n in git(root, "ls-files", "--others", "--exclude-standard", "-z").split(b"\0") if n
    )
    with patch_path.open("xb") as stream:  # exclusive: an earlier barrier's patch is never overwritten
        stream.write(patch)
    return {
        "head": head,
        "indexSha256": digest(index),
        "patch": str(patch_path),
        "patchSha256": digest(patch),
        "untracked": untracked,
    }


def close(options):
    """The cleanup barrier: stop what the unit owned, keep what it produced, and record why it ended."""
    ledger = load(options.ledger)
    task = task_entry(ledger, options.task)
    require(options.role in ROLES, "role must be implementer or verifier")
    require(options.outcome in OUTCOMES, f"outcome must be one of {list(OUTCOMES)}")
    require(not options.repo or options.evidence_root, "--repo needs --evidence-root to hold the patch")
    entry = find_unit(task, options.attempt, options.role)
    require(entry is None or not entry.get("barrier"), "this unit is already closed")
    require(
        options.outcome != "success" or (entry is not None and entry["result"] is not None),
        "success needs the unit's recorded result; record it first",
    )
    if entry is None:
        # A unit that never returned still gets its record, so the barrier is visible and the number is not reused.
        require(
            options.role == "implementer" or has_implementer(task, options.attempt),
            "a verifier unit needs the implementer record for the same attempt",
        )
        entry = {
            "attempt": options.attempt,
            "role": options.role,
            "owner": options.owner,
            "recordedAt": now(),
            "input": None,
            "result": None,
        }
        task["attempts"].append(entry)
    barrier = {"outcome": options.outcome, "owner": options.owner, "closedAt": now(), "processes": [], "tree": None}
    if options.evidence_root:
        unit_dir = absolute(options.evidence_root, resolve=False) / "tasks" / options.task
        barrier["processes"] = stop(unit_dir / f"processes-{options.role}-{options.attempt}.json")
        require(
            all(p["outcome"] != "survived" for p in barrier["processes"]),
            "an owned process survived the barrier; inspect it before starting the next unit",
        )
        if options.repo:
            unit_dir.mkdir(parents=True, exist_ok=True)
            barrier["tree"] = preserve_tree(options.repo, unit_dir / f"patch-{options.role}-{options.attempt}.diff")
    entry["barrier"] = barrier
    derive(task)
    save(options.ledger, ledger)
    return {
        "task": options.task,
        "unit": unit(options.task, options.attempt, options.role),
        "outcome": options.outcome,
        "state": task["state"],
        "processes": dict(Counter(p["outcome"] for p in barrier["processes"])),
        "patch": barrier["tree"]["patch"] if barrier["tree"] else None,
        "openUnits": open_units(ledger),
    }


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
                (
                    a["result"].get("notePath")
                    for a in reversed(entry["attempts"])
                    if a["role"] == "implementer" and a["result"] is not None
                ),
                None,
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
        "openUnits": open_units(ledger),
        "overdue": overdue_units(ledger),
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
    begin = commands.add_parser(
        "start", help="record a unit before dispatch so its attempt number is taken and a duplicate is refused"
    )
    begin.add_argument("--ledger", required=True)
    begin.add_argument("--task", required=True)
    begin.add_argument("--attempt", type=int, required=True)
    begin.add_argument("--role", required=True, choices=ROLES)
    begin.add_argument("--owner", required=True, help="who is dispatching the unit, normally the run owner")
    begin.add_argument("--input", help="snapshot JSON captured before the worker starts")
    begin.add_argument("--contract", help="the work-unit contract the worker receives")
    begin.add_argument(
        "--timeout",
        type=float,
        help="seconds until the unit counts as overdue when no completion notification has arrived; not a poll interval",
    )
    rec = commands.add_parser("record", help="append one worker result to a task")
    rec.add_argument("--ledger", required=True)
    rec.add_argument("--task", required=True)
    rec.add_argument("--attempt", type=int, required=True)
    rec.add_argument("--role", required=True, choices=ROLES)
    rec.add_argument("--owner", required=True, help="who did the work, for example the agent id and label")
    rec.add_argument("--result", required=True, help="the worker's structured result JSON file")
    rec.add_argument("--input", help="snapshot JSON captured before the worker started")
    rec.add_argument("--packet", help="the packet the worker received; its digest must match init")
    rec.add_argument(
        "--legacy",
        action="store_true",
        help="accept a status/verdict result from before hand-back.schema.json; its shape is not checked",
    )
    end = commands.add_parser(
        "close", help="cleanup barrier: stop the unit's owned processes, keep its patch, record why it ended"
    )
    end.add_argument("--ledger", required=True)
    end.add_argument("--task", required=True)
    end.add_argument("--attempt", type=int, required=True)
    end.add_argument("--role", required=True, choices=ROLES)
    end.add_argument("--outcome", required=True, choices=OUTCOMES)
    end.add_argument("--owner", required=True, help="who ran the barrier, normally the run owner")
    end.add_argument(
        "--evidence-root", help="run directory with tasks/<ID>/; holds the unit's process registry and gets its patch"
    )
    end.add_argument("--repo", help="repository the unit worked in; its tracked diff is kept as the unit's patch")
    flag = commands.add_parser("mark", help="set a state no worker produces, with a reason")
    flag.add_argument("--ledger", required=True)
    flag.add_argument("--task", required=True)
    flag.add_argument("--state", required=True)
    flag.add_argument("--reason", required=True)
    flag.add_argument("--owner", required=True)
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
        "start": start,
        "record": record,
        "close": close,
        "mark": mark,
        "packet": packet,
        "summary": summary,
    }
    try:
        result = handlers[options.command](options)
    except (LedgerError, HandBackError, ManifestError, ProcessError) as exc:
        print(f"work-ledger: {exc}", file=sys.stderr)
        return 1
    print(json.dumps(result, indent=1, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
