#!/usr/bin/env python3
"""Check the short hand-back one worker returns against its note and its artifact; never record or dispatch."""

import argparse
import json
import re
import sys
from pathlib import Path

from run_manifest import ManifestError, absolute, git, reject_secrets

SCHEMA = Path(__file__).resolve().parent.parent / "hand-back.schema.json"
# The five gate IDs from references/approvals.md; every gated action starts with one so ledger and approvals agree.
GATES = ("global-settings", "skill-enablement", "paid-calls", "commits-pushes", "hosted-reports")
KEYS = (
    "disposition",
    "summary",
    "artifact",
    "checks",
    "findings",
    "deviations",
    "nextAction",
    "notePath",
    "openObligations",
    "gatedActions",
)
DISPOSITIONS = {
    "implementer": ("completed", "partial", "blocked-approval", "failed"),
    "verifier": ("accepted", "changes-required", "blocked-approval", "failed-verification"),
}
# A clean disposition claims the work stands; it needs at least one check that ran and none that failed.
CLEAN = ("completed", "accepted", "blocked-approval")
MAX_CHARS = 500  # Anything longer belongs in the note, which is why the hand-back has a note path at all.
RANGE = re.compile(r"^([0-9a-f]{40})\.\.([0-9a-f]{40}|worktree)$")


class HandBackError(ValueError):
    """A hand-back that is incomplete, contradictory, or not backed by its note or artifact."""


def require(condition, message):
    if not condition:
        raise HandBackError(message)


def short(value, label, *, allow_empty=False):
    require(isinstance(value, str) and (allow_empty or value.strip()), f"{label} must be a non-empty string")
    require(len(value) <= MAX_CHARS, f"{label} exceeds {MAX_CHARS} characters; put the detail in the note")
    return value


def shorts(value, label):
    require(isinstance(value, list), f"{label} must be a list")
    for item in value:
        short(item, f"{label} item")
    return value


def disposition(result):
    """Hand-backs carry one disposition; records made before this contract carried status or verdict."""
    return result.get("disposition") or result.get("status") or result.get("verdict")


def files(result):
    artifact = result.get("artifact")
    return artifact["files"] if isinstance(artifact, dict) and "files" in artifact else result.get("filesChanged", [])


def unresolved(result):
    findings = result.get("findings")
    return (
        findings["unresolved"] if isinstance(findings, dict) and "unresolved" in findings else result.get("issues", [])
    )


def check(result, role, *, repo=None):
    """Refuse a hand-back whose claims cannot be followed to a file, a commit, or a check that ran."""
    require(isinstance(result, dict), "hand-back must be a JSON object")
    require(role in DISPOSITIONS, "role must be implementer or verifier")
    require(set(result) == set(KEYS), f"hand-back needs exactly these keys: {', '.join(KEYS)}")
    reject_secrets(result)
    outcome = result["disposition"]
    require(outcome in DISPOSITIONS[role], f"{role} disposition must be one of {list(DISPOSITIONS[role])}")
    short(result["summary"], "summary")
    short(result["nextAction"], "nextAction")
    artifact = result["artifact"]
    require(isinstance(artifact, dict) and set(artifact) == {"range", "files"}, "artifact needs range and files")
    span = RANGE.fullmatch(str(artifact["range"]))
    require(span, "artifact range must be <commit>..<commit> or <commit>..worktree with full hashes")
    shorts(artifact["files"], "artifact files")
    checks = result["checks"]
    require(isinstance(checks, list), "checks must be a list")
    for item in checks:
        require(
            isinstance(item, dict) and {"command", "passed"} <= set(item) <= {"command", "passed", "note"},
            "each check needs command and passed, optionally note",
        )
        short(item["command"], "check command")
        require(isinstance(item["passed"], bool), "check passed must be true or false")
        if "note" in item:
            short(item["note"], "check note", allow_empty=True)
    if outcome in CLEAN:
        require(checks, f"{outcome} needs at least one check that actually ran")
        require(all(c["passed"] for c in checks), f"{outcome} contradicts a failed check")
    if outcome == "blocked-approval":
        require(result["gatedActions"], "blocked-approval must name the gated action it is waiting on")
    findings = result["findings"]
    require(
        isinstance(findings, dict) and set(findings) == {"unresolved", "refuted"},
        "findings needs unresolved and refuted",
    )
    shorts(findings["unresolved"], "unresolved findings")
    shorts(findings["refuted"], "refuted findings")
    shorts(result["deviations"], "deviations")
    shorts(result["openObligations"], "openObligations")
    for action in shorts(result["gatedActions"], "gatedActions"):
        require(any(action.startswith(f"{g}:") for g in GATES), f"gated action must start with a gate ID from {GATES}")
    note = absolute(result["notePath"], resolve=False)
    require(note.is_file() and note.stat().st_size > 0, "notePath must name an existing, non-empty note file")
    if repo is not None:
        root = absolute(repo)
        require(
            note != root and root not in note.parents,
            "notePath must lie outside the repository so the note survives a discarded worktree",
        )
        for commit in (c for c in span.groups() if c != "worktree"):
            try:
                git(root, "cat-file", "-e", f"{commit}^{{commit}}")
            except ManifestError:
                raise HandBackError(f"artifact range names {commit}, which is not a commit in {root}") from None
    return result


def run(options):
    try:
        result = json.loads(Path(options.result).read_text())
    except (OSError, ValueError) as exc:
        raise HandBackError("cannot read hand-back JSON") from exc
    check(result, options.role, repo=options.repo)
    return {"result": options.result, "role": options.role, "disposition": result["disposition"], "ok": True}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    verify = commands.add_parser("check", help="refuse a hand-back that is long, contradictory, or unbacked")
    verify.add_argument("--result", required=True, help="the worker's structured result JSON file")
    verify.add_argument("--role", required=True, choices=sorted(DISPOSITIONS))
    verify.add_argument("--repo", help="repository the artifact range refers to; its commits must exist there")
    options = parser.parse_args(argv)
    try:
        result = run(options)
    except (HandBackError, ManifestError) as exc:
        print(f"hand-back: {exc}", file=sys.stderr)
        return 1
    print(json.dumps(result, indent=1, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
