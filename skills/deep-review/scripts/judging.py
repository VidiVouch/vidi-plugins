#!/usr/bin/env python3
"""Check one judge's result against the candidates it was given and the tree it judged; never merge or dispatch."""

import argparse
import json
import re
import sys
from pathlib import Path

from run_manifest import ManifestError, absolute, reject_secrets

SCHEMA = Path(__file__).resolve().parent.parent / "judging-output.schema.json"
KEYS = ("input", "findings", "limits", "summary")
INPUT_KEYS = ("snapshotId", "round")
FINDING_KEYS = (
    "id",
    "file",
    "line_start",
    "line_end",
    "severity",
    "disposition",
    "scenario",
    "evidence",
    "impact",
    "reason",
    "limits",
)
EVIDENCE_KEYS = ("kind", "ref", "note")
SEVERITIES = ("critical", "warning", "nit")
DISPOSITIONS = ("confirmed", "refuted", "unresolved", "unreviewed")
KINDS = ("code", "command", "document")
# What a disposition must carry before it counts as that disposition. A verdict without the thing
# that lets a reader check it is a claim, and a claim is unresolved at best.
NEEDS = {
    "confirmed": ("scenario", "evidence", "impact", "reason"),
    "refuted": ("scenario", "evidence", "reason"),
    "unresolved": ("scenario", "impact", "reason", "limits"),
    "unreviewed": ("limits",),
}
MAX_CHARS = 500  # The result is the record; the judge's working goes in evidence refs, not prose.
ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,63}$")
SNAPSHOT = re.compile(r"^[0-9a-f]{64}$")
CODE_REF = re.compile(r"^(?P<path>[^:\r\n\0]+):(?P<start>[1-9][0-9]*)(?:-(?P<end>[1-9][0-9]*))?$")


class JudgingError(ValueError):
    """A judging result that is incomplete, contradictory, or not about the candidates it was given."""


def require(condition, message):
    if not condition:
        raise JudgingError(message)


def short(value, label, *, allow_empty=True):
    require(isinstance(value, str) and (allow_empty or value.strip()), f"{label} must be a non-empty string")
    require(len(value) <= MAX_CHARS, f"{label} exceeds {MAX_CHARS} characters")
    return value


def shorts(value, label):
    require(isinstance(value, list), f"{label} must be a list")
    for item in value:
        short(item, f"{label} item", allow_empty=False)
    return value


def relative(value, label):
    """A repo-relative path: not absolute, no parent segments, no control characters."""
    short(value, label, allow_empty=False)
    require(
        not re.match(r"^(/|\\\\|[A-Za-z]:)", value)
        and ".." not in re.split(r"[\\/]", value)
        and not re.search(r"[\r\n\0]", value),
        f"{label} must be a repo-relative path",
    )
    return value


def anchor(finding):
    return f"{finding.get('file')}:{finding.get('line_start')}-{finding.get('line_end')}"


def check_evidence(item, label, root):
    require(isinstance(item, dict) and set(item) == set(EVIDENCE_KEYS), f"{label} needs exactly kind, ref and note")
    require(item["kind"] in KINDS, f"{label} kind must be one of {list(KINDS)}")
    short(item["ref"], f"{label} ref", allow_empty=False)
    short(item["note"], f"{label} note")
    if item["kind"] != "code":
        return
    ref = CODE_REF.fullmatch(item["ref"])
    require(ref, f"{label} code ref must be <repo-relative path>:<line> or <path>:<start>-<end>")
    relative(ref["path"], f"{label} code ref path")
    start, end = int(ref["start"]), int(ref["end"] or ref["start"])
    require(end >= start, f"{label} code ref ends before it starts")
    if root is None:
        return
    target = (root / ref["path"]).resolve()
    require(root in target.parents and target.is_file(), f"{label} cites {ref['path']}, which is not a file in {root}")
    lines = len(target.read_bytes().splitlines())
    require(end <= lines, f"{label} cites {ref['path']}:{start}-{end}, beyond its {lines} lines")


def check_finding(finding, root):
    require(
        isinstance(finding, dict) and set(finding) == set(FINDING_KEYS),
        f"each finding needs exactly these keys: {', '.join(FINDING_KEYS)}",
    )
    require(
        ID.fullmatch(str(finding["id"])),
        "finding id must be 1 to 64 characters of letters, digits, '.', '_', ':' or '-'",
    )
    label = f"finding {finding['id']}"
    relative(finding["file"], f"{label} file")
    for key in ("line_start", "line_end"):
        require(
            isinstance(finding[key], int) and not isinstance(finding[key], bool) and finding[key] >= 1,
            f"{label} {key} must be a positive integer",
        )
    require(finding["line_end"] >= finding["line_start"], f"{label} line_end is before line_start")
    require(finding["severity"] in SEVERITIES, f"{label} severity must be one of {list(SEVERITIES)}")
    outcome = finding["disposition"]
    require(outcome in DISPOSITIONS, f"{label} disposition must be one of {list(DISPOSITIONS)}")
    short(finding["scenario"], f"{label} scenario")
    short(finding["impact"], f"{label} impact")
    short(finding["reason"], f"{label} reason")
    require(isinstance(finding["evidence"], list), f"{label} evidence must be a list")
    for index, item in enumerate(finding["evidence"]):
        check_evidence(item, f"{label} evidence[{index}]", root)
    shorts(finding["limits"], f"{label} limits")
    for key in NEEDS[outcome]:
        require(
            finding[key].strip() if isinstance(finding[key], str) else finding[key],
            f"{label} is {outcome} but has no {key}; {outcome} needs {', '.join(NEEDS[outcome])}",
        )
    if outcome == "unreviewed":
        require(
            not finding["evidence"],
            f"{label} is unreviewed but cites evidence; a candidate that was examined is confirmed, refuted or unresolved",
        )


def check_candidates(findings, candidates):
    """Every candidate gets exactly one disposition at its own anchor; nothing else is a judgment."""
    require(
        isinstance(candidates, list) and all(isinstance(c, dict) for c in candidates),
        "candidates must be a JSON array of objects",
    )
    expected = {}
    for candidate in candidates:
        require(
            ID.fullmatch(str(candidate.get("id"))) and candidate["id"] not in expected,
            "each candidate needs a unique id",
        )
        expected[candidate["id"]] = candidate
    ids = {f["id"] for f in findings}
    missing = sorted(set(expected) - ids)
    require(
        not missing,
        f"candidates without a disposition: {', '.join(missing)}; a candidate the judge did not examine is unreviewed, not absent",
    )
    extra = sorted(ids - set(expected))
    require(
        not extra,
        f"dispositions for unknown candidates: {', '.join(extra)}; a new defect is a candidate for the next review round, not a judgment",
    )
    for finding in findings:
        candidate = expected[finding["id"]]
        require(
            anchor(finding) == anchor(candidate),
            f"finding {finding['id']} moved from {anchor(candidate)} to {anchor(finding)}; the anchor is the identity, cite narrower lines as code evidence",
        )


def check(result, *, candidates=None, snapshot=None, repo=None):
    """Refuse a judging result whose dispositions are not backed by what the schema says each one needs."""
    require(
        isinstance(result, dict) and set(result) == set(KEYS),
        f"judging result needs exactly these keys: {', '.join(KEYS)}",
    )
    reject_secrets(result)
    given = result["input"]
    require(isinstance(given, dict) and set(given) == set(INPUT_KEYS), f"input needs exactly {', '.join(INPUT_KEYS)}")
    require(
        SNAPSHOT.fullmatch(str(given["snapshotId"])),
        "input snapshotId must be the 64-hex snapshot id the judge was given",
    )
    require(
        isinstance(given["round"], int) and not isinstance(given["round"], bool) and given["round"] >= 1,
        "input round must be a positive integer",
    )
    if snapshot is not None:
        require(given["snapshotId"] == snapshot, f"result judged snapshot {given['snapshotId']}, not {snapshot}")
    root = absolute(repo) if repo is not None else None
    require(isinstance(result["findings"], list), "findings must be a list")
    seen = set()
    for finding in result["findings"]:
        check_finding(finding, root)
        require(finding["id"] not in seen, f"finding id {finding['id']} appears twice; one disposition per candidate")
        seen.add(finding["id"])
    if candidates is not None:
        check_candidates(result["findings"], candidates)
    shorts(result["limits"], "limits")
    short(result["summary"], "summary", allow_empty=False)
    return result


def counts(result):
    return {d: sum(1 for f in result["findings"] if f["disposition"] == d) for d in DISPOSITIONS}


def load(path, label):
    try:
        return json.loads(Path(path).read_text())
    except (OSError, ValueError) as exc:
        raise JudgingError(f"cannot read {label} JSON") from exc


def run(options):
    result = load(options.result, "judging result")
    candidates = load(options.candidates, "candidates") if options.candidates else None
    check(result, candidates=candidates, snapshot=options.snapshot, repo=options.repo)
    return {"result": options.result, "round": result["input"]["round"], "counts": counts(result), "ok": True}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    verify = commands.add_parser(
        "check", help="refuse a judging result that is unbacked, contradictory, or about other candidates"
    )
    verify.add_argument("--result", required=True, help="the judge's structured result JSON file")
    verify.add_argument(
        "--candidates",
        help="JSON array of the candidate findings the judge was given, each with id, file, line_start, line_end",
    )
    verify.add_argument("--snapshot", help="the 64-hex snapshot id the judge was given; the result must name it")
    verify.add_argument("--repo", help="the judged tree; code evidence must cite files and lines that exist in it")
    options = parser.parse_args(argv)
    try:
        result = run(options)
    except (JudgingError, ManifestError) as exc:
        print(f"judging: {exc}", file=sys.stderr)
        return 1
    print(json.dumps(result, indent=1, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
