#!/usr/bin/env python3
"""Register the child processes one unit owns and stop exactly those at its cleanup barrier; never scan or stop anything else."""

import argparse
import json
import os
import signal
import subprocess
import sys
import time
from pathlib import Path


class ProcessError(ValueError):
    """A registry that cannot be trusted or a process the run does not own."""


def require(condition, message):
    if not condition:
        raise ProcessError(message)


def inspect(pid):
    """Return the process group, state and start time ps reports for pid, or None when no such process exists."""
    result = subprocess.run(
        ["ps", "-o", "pgid=,stat=,lstart=", "-p", str(pid)], capture_output=True, text=True, check=False
    )
    line = result.stdout.strip()
    if result.returncode != 0 or not line:
        return None
    pgid, state, started = line.split(None, 2)
    return {"pgid": int(pgid), "state": state, "started": started}


def load(path):
    path = Path(path)
    if not path.exists():
        return []
    try:
        entries = json.loads(path.read_text())
    except (OSError, ValueError) as exc:
        raise ProcessError("cannot read process registry") from exc
    require(
        isinstance(entries, list)
        and all(isinstance(e, dict) and {"pid", "pgid", "started"} <= set(e) for e in entries),
        "invalid process registry",
    )
    return entries


def register(path, pid):
    """Record a child the run started in its own process group; its start time is the identity a later stop checks."""
    require(isinstance(pid, int) and pid > 1, "pid must be a positive integer")
    info = inspect(pid)
    require(info is not None, f"no process {pid}")
    # A child in the caller's own group would take the caller's shell or session down with it at the barrier.
    require(
        info["pgid"] not in (os.getpgrp(), 1),
        f"process {pid} shares the caller's process group; start owned children in their own session",
    )
    entry = {"pid": pid, "pgid": info["pgid"], "started": info["started"]}
    entries = load(path)
    if entry not in entries:
        entries.append(entry)
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        Path(path).write_text(json.dumps(entries, indent=1) + "\n")
    return entry


def members(pgid):
    """Pids of the processes still running in a group. A group id is not reallocated while the group has
    members, so once the registered leader has exited these are still the children it left behind."""
    result = subprocess.run(["ps", "-axo", "pid=,pgid=,stat="], capture_output=True, text=True, check=False)
    rows = (line.split() for line in result.stdout.splitlines())
    return [int(pid) for pid, group, state, *_ in rows if int(group) == pgid and not state.startswith("Z")]


def owned(entry):
    """True while the registered process is alive and still the one we registered; None once gone; False if reused."""
    info = inspect(entry["pid"])
    if info is None or info["state"].startswith("Z"):
        return None  # exited, or a zombie waiting for its parent to reap it
    if info["pgid"] != entry["pgid"] or info["started"] != entry["started"]:
        return False  # the pid now belongs to a process the run never started
    return True


def running(entry):
    """True while the registered process is ours and alive, or, once it has exited, while its group still has members."""
    state = owned(entry)
    if state is None:
        return bool(members(entry["pgid"]))
    return state


def stop(path, grace=1.0):
    """Stop each registered process group still alive and report every entry; the registry stays as evidence."""
    report = []
    for entry in load(path):
        if owned(entry) is False:
            outcome = "reused"  # a reused pid says nothing about the group, so it is not consulted
        elif entry["pgid"] in (os.getpgrp(), 1):
            outcome = "refused"  # never the caller's own session, however it got into the registry
        elif not running(entry):
            outcome = "gone"
        else:
            for signum in (signal.SIGTERM, signal.SIGKILL):
                try:
                    os.killpg(entry["pgid"], signum)
                except ProcessLookupError:
                    break
                deadline = time.monotonic() + grace
                while running(entry) and time.monotonic() < deadline:
                    time.sleep(0.05)
                if not running(entry):
                    break
            outcome = "survived" if running(entry) else "stopped"
        report.append({**entry, "outcome": outcome})
    return report


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    add = commands.add_parser("register", help="record one child the run started in its own session")
    add.add_argument("--registry", required=True, help="JSON list kept in the unit's evidence directory")
    add.add_argument("--pid", type=int, required=True)
    end = commands.add_parser("stop", help="stop the registered processes still alive and report each one")
    end.add_argument("--registry", required=True)
    options = parser.parse_args(argv)
    try:
        if options.command == "register":
            result = register(options.registry, options.pid)
        else:
            result = stop(options.registry)
    except ProcessError as exc:
        print(f"owned-processes: {exc}", file=sys.stderr)
        return 1
    print(json.dumps(result, indent=1, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
