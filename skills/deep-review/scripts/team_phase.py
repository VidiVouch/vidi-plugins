#!/usr/bin/env python3
"""Record one lead-owned team phase: which temporary teammates were spawned, which were shut down, and refuse to
close while any is still standing. Never spawns, messages or stops an agent; the lead does that in its own session."""

import argparse
import json
import sys
from datetime import datetime, timezone
from pathlib import Path


class PhaseError(ValueError):
    """A phase record that cannot be trusted, or a step taken out of order."""


def require(condition, message):
    if not condition:
        raise PhaseError(message)


def now():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def load(path):
    path = Path(path)
    if not path.exists():
        return {"version": 1, "phases": []}
    try:
        record = json.loads(path.read_text())
    except (OSError, ValueError) as exc:
        raise PhaseError("cannot read team phase record") from exc
    require(
        isinstance(record, dict) and record.get("version") == 1 and isinstance(record.get("phases"), list),
        "invalid team phase record",
    )
    return record


def save(path, record):
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    Path(path).write_text(json.dumps(record, indent=1, sort_keys=True) + "\n")


def current(record):
    """The one open phase, or None. Two phases never overlap: the next report-only review waits for the close."""
    open_phases = [p for p in record["phases"] if p["closedAt"] is None]
    return open_phases[0] if open_phases else None


def standing(phase):
    return [t["name"] for t in phase["teammates"] if t["shutDownAt"] is None]


def view(phase):
    return {**phase, "standing": standing(phase)}


def open_phase(path, round_number):
    record = load(path)
    require(isinstance(round_number, int) and round_number > 0, "round must be a positive integer")
    open_now = current(record)
    require(open_now is None, f"phase for round {open_now and open_now['round']} is still open; close it first")
    require(all(p["round"] != round_number for p in record["phases"]), f"round {round_number} already has a phase")
    phase = {"round": round_number, "openedAt": now(), "closedAt": None, "teammates": []}
    record["phases"].append(phase)
    save(path, record)
    return view(phase)


def spawn(path, name, files):
    """Record a teammate the lead is about to spawn. The name carries the round so a stale name from an earlier phase
    can never be messaged by mistake, and no two standing teammates own the same file."""
    record = load(path)
    phase = current(record)
    require(phase is not None, "no open phase; run open first")
    require(isinstance(name, str) and name.strip() == name and name, "name must be a non-empty string")
    require(f"-r{phase['round']}-" in name, f"name must carry this phase's round as -r{phase['round']}-")
    used = {t["name"] for p in record["phases"] for t in p["teammates"]}
    require(name not in used, f"name {name} was already used; a replacement gets a new name")
    require(files and len(set(files)) == len(files), "a teammate owns at least one file, each listed once")
    owned = {f: t["name"] for t in phase["teammates"] if t["shutDownAt"] is None for f in t["files"]}
    for f in sorted(files):
        require(f not in owned, f"{f} is owned by standing teammate {owned.get(f)}; shut it down first")
    phase["teammates"].append({"name": name, "files": list(files), "spawnedAt": now(), "shutDownAt": None})
    save(path, record)
    return view(phase)


def shutdown(path, name):
    record = load(path)
    phase = current(record)
    require(phase is not None, "no open phase")
    match = [t for t in phase["teammates"] if t["name"] == name]
    require(match, f"no teammate {name} in the open phase")
    require(match[0]["shutDownAt"] is None, f"teammate {name} is already shut down")
    match[0]["shutDownAt"] = now()
    save(path, record)
    return view(phase)


def close(path):
    """The phase barrier: refused while a teammate is standing, because the next review must see a tree nobody edits."""
    record = load(path)
    phase = current(record)
    require(phase is not None, "no open phase")
    left = standing(phase)
    require(not left, f"teammates still standing: {', '.join(left)}; shut each one down by name first")
    phase["closedAt"] = now()
    save(path, record)
    return view(phase)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    for name, help_text in (
        ("open", "start the team phase for one round; refused while another phase is open"),
        ("spawn", "record a teammate before spawning it by name"),
        ("shutdown", "record that a teammate was shut down by name"),
        ("close", "end the phase; refused while any teammate is standing"),
    ):
        command = commands.add_parser(name, help=help_text)
        command.add_argument("--record", required=True, help="JSON record kept in the run directory")
        if name == "open":
            command.add_argument("--round", type=int, required=True)
        if name in ("spawn", "shutdown"):
            command.add_argument("--name", required=True)
        if name == "spawn":
            command.add_argument("--files", nargs="+", required=True, help="the disjoint bucket this teammate owns")
    options = parser.parse_args(argv)
    try:
        if options.command == "open":
            result = open_phase(options.record, options.round)
        elif options.command == "spawn":
            result = spawn(options.record, options.name, options.files)
        elif options.command == "shutdown":
            result = shutdown(options.record, options.name)
        else:
            result = close(options.record)
    except PhaseError as exc:
        print(f"team-phase: {exc}", file=sys.stderr)
        return 1
    print(json.dumps(result, indent=1, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
