#!/usr/bin/env python3
"""Inspect, activate and restore the user-level /rebuild forwarding entry.

The plugin's rebuild skill is the only rebuild implementation. The user-level
~/.claude/skills/rebuild/SKILL.md used to hold an older full implementation;
skills/rebuild/compat/user-skill.md is the staged replacement that forwards to
vidi-skills:rebuild. Writing under ~/.claude/skills is gated (global-settings and
skill-enablement in deep-review/references/approvals.md), so `status` is the
only subcommand that runs without approval. `activate` backs the live file up
before replacing it and prints the restore command; `restore` undoes it.
"""

import argparse
import hashlib
import json
import shutil
import sys
from pathlib import Path

STAGED = Path(__file__).resolve().parents[1] / "compat" / "user-skill.md"
MARKER = "<!-- forwarding entry for vidi-skills:rebuild"
LIVE_RELATIVE = Path(".claude") / "skills" / "rebuild" / "SKILL.md"


def digest(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def classify(live, staged_text):
    # legacy: anything that is not a forwarding entry, typically the retired full implementation.
    if not live.is_file():
        return "missing"
    text = live.read_text()
    if text == staged_text:
        return "forwarding"
    return "forwarding-outdated" if MARKER in text else "legacy"


def report(home):
    live = home / LIVE_RELATIVE
    out = {"staged": str(STAGED), "stagedSha256": digest(STAGED), "live": str(live)}
    out["state"] = classify(live, STAGED.read_text())
    if live.is_file():
        out["liveSha256"] = digest(live)
    return out


def status(args):
    print(json.dumps(report(args.home), indent=2))
    return 0


def activate(args):
    out = report(args.home)
    live = args.home / LIVE_RELATIVE
    if out["state"] == "forwarding":
        out["changed"] = False
    else:
        out["previousState"] = out["state"]
        if live.is_file():
            if args.backup_dir is None:
                print(f"{live} exists; pass --backup-dir so the previous entry can be restored", file=sys.stderr)
                return 2
            args.backup_dir.mkdir(parents=True, exist_ok=True)
            backup = args.backup_dir / f"SKILL.md.{out['liveSha256'][:12]}.bak"
            shutil.copy2(live, backup)
            out["backup"] = str(backup)
            out["restore"] = f"python3 {Path(__file__).resolve()} restore --home {args.home} --backup {backup}"
        live.parent.mkdir(parents=True, exist_ok=True)
        live.write_text(STAGED.read_text())
        out["changed"] = True
        out["state"] = "forwarding"
    print(json.dumps(out, indent=2))
    return 0


def restore(args):
    if not args.backup.is_file():
        print(f"backup not found: {args.backup}", file=sys.stderr)
        return 2
    live = args.home / LIVE_RELATIVE
    live.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(args.backup, live)
    print(json.dumps({"live": str(live), "restoredFrom": str(args.backup), "liveSha256": digest(live)}, indent=2))
    return 0


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--home", type=Path, default=Path.home(), help="home directory holding .claude/skills")
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("status", help="report whether the live entry is missing, legacy or forwarding").set_defaults(
        run=status
    )
    act = commands.add_parser("activate", help="gated: back up the live entry and write the staged forwarding entry")
    act.add_argument("--backup-dir", type=Path, help="where to copy the current live entry before replacing it")
    act.set_defaults(run=activate)
    res = commands.add_parser("restore", help="gated: put a backup taken by activate back in place")
    res.add_argument("--backup", type=Path, required=True)
    res.set_defaults(run=restore)
    args = parser.parse_args(argv)
    return args.run(args)


if __name__ == "__main__":
    sys.exit(main())
