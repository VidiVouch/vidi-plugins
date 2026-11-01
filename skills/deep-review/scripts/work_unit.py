#!/usr/bin/env python3
"""Build and check the work-unit contract one fresh worker receives; never dispatch or record."""

import argparse
import json
import re
import sys
from pathlib import Path
from types import SimpleNamespace

from run_manifest import (
    DIGEST,
    ManifestError,
    absolute,
    check_reference,
    outside_repo,
    reference,
    reject_secrets,
    write_exclusive,
)
from work_ledger import ROLES, LedgerError, load_json, sha256, snapshot_input
from work_ledger import packet as ledger_packet

HEAD = re.compile(r"[0-9a-f]{40}\Z")
# The five gate IDs from references/approvals.md; a stop gate outside this list is a typo, not a new gate.
GATES = ("global-settings", "skill-enablement", "paid-calls", "commits-pushes", "hosted-reports")
SECTIONS = ("purpose", "input", "allowed", "decisions", "contracts", "evidence", "output", "stop")
NOTE_PREFIX = {"implementer": "implement", "verifier": "verify"}


class ContractError(ValueError):
    """A work-unit contract that is incomplete, contradictory, or drifted from its inputs."""


def require(condition, message):
    if not condition:
        raise ContractError(message)


def text(value, label):
    require(isinstance(value, str) and value.strip(), f"{label} must be a non-empty string")
    return value


def texts(value, label, *, non_empty=False):
    require(
        isinstance(value, list) and all(isinstance(v, str) and v.strip() for v in value), f"{label} must list strings"
    )
    require(value or not non_empty, f"{label} must not be empty")
    return value


def first(mapping, keys):
    return next((mapping[k] for k in keys if isinstance(mapping, dict) and mapping.get(k)), None)


def purpose(brief):
    """A packet becomes a unit only when it states what to do and how to tell it is done."""
    return {
        "work": text(first(brief, ("Work", "work")), "packet work statement"),
        "acceptance": text(first(brief, ("Done when", "Acceptance check", "acceptance")), "packet acceptance"),
        "mode": brief.get("mode") if isinstance(brief.get("mode"), str) else None,
        "context": texts(brief.get("sectionContext", []), "packet context"),
    }


def validate(contract):
    require(isinstance(contract, dict) and contract.get("version") == 1, "contract must be a version 1 object")
    require(set(contract) == {"version", "runId", "task", "attempt", "role", *SECTIONS}, "contract has wrong sections")
    reject_secrets(contract)
    text(contract["runId"], "runId")
    text(contract["task"], "task")
    require(isinstance(contract["attempt"], int) and contract["attempt"] >= 1, "attempt numbers start at 1")
    require(contract["role"] in ROLES, "role must be implementer or verifier")
    aim = contract["purpose"]
    require(
        isinstance(aim, dict) and set(aim) == {"work", "acceptance", "mode", "context"},
        "purpose needs work, acceptance, mode, context",
    )
    text(aim["work"], "purpose work")
    text(aim["acceptance"], "purpose acceptance")
    require(aim["mode"] is None or isinstance(aim["mode"], str), "purpose mode must be a string or null")
    texts(aim["context"], "purpose context")
    snap = contract["input"]
    require(
        isinstance(snap, dict) and set(snap) == {"repo", "head", "indexSha256", "snapshotId", "packetSha256"},
        "input snapshot needs repo, head, indexSha256, snapshotId and packetSha256",
    )
    require(HEAD.fullmatch(str(snap["head"])), "input head must be a full commit hash")
    for key in ("indexSha256", "snapshotId", "packetSha256"):
        require(DIGEST.fullmatch(str(snap[key])), f"input {key} must be a sha256 digest")
    repo = absolute(snap["repo"], resolve=False)
    allowed = contract["allowed"]
    require(
        isinstance(allowed, dict) and set(allowed) == {"files", "actions", "forbidden"},
        "allowed needs files, actions, forbidden",
    )
    texts(allowed["files"], "allowed files", non_empty=True)
    texts(allowed["actions"], "allowed actions", non_empty=True)
    texts(allowed["forbidden"], "forbidden actions")
    texts(contract["decisions"], "decisions")
    contracts = contract["contracts"]
    require(
        isinstance(contracts, dict) and set(contracts) == {"shared", "dependencies"},
        "contracts needs shared, dependencies",
    )
    require(isinstance(contracts["shared"], list), "shared contracts must be a list")
    for item in contracts["shared"]:
        check_reference(item, "shared contract")  # Refuses a contract file that changed since the build.
    for dep, outcome in contracts["dependencies"].items():
        require(isinstance(outcome, dict) and "state" in outcome, f"dependency {dep} needs its recorded outcome")
    evidence = contract["evidence"]
    require(
        isinstance(evidence, dict) and set(evidence) == {"dir", "note", "result", "contract"},
        "evidence needs dir, note, result, contract",
    )
    root = absolute(evidence["dir"], resolve=False)
    for key in ("note", "result", "contract"):
        require(absolute(evidence[key], resolve=False).parent == root, "evidence files live in the evidence dir")
    outside_repo(root, repo)  # Evidence must survive a discarded worktree.
    output = contract["output"]
    require(
        isinstance(output, dict) and set(output) == {"path", "sha256", "schema"}, "output needs path, sha256, schema"
    )
    schema_path = check_reference({"path": output["path"], "sha256": output["sha256"]}, "output schema")
    schema = output["schema"]
    require(
        isinstance(schema, dict) and schema.get("required") and isinstance(schema.get("properties"), dict),
        "output schema needs required and properties",
    )
    require(load_json(schema_path, "output schema") == schema, "inlined output schema differs from its file")
    stop = contract["stop"]
    require(isinstance(stop, dict) and set(stop) == {"when", "gates"}, "stop needs when, gates")
    texts(stop["when"], "stop conditions", non_empty=True)
    require(all(g in GATES for g in texts(stop["gates"], "stop gates")), f"stop gates must be among {list(GATES)}")
    return contract


def build(options):
    shared = load_json(options.shared, "shared contract")
    require(isinstance(shared, dict), "shared contract must be a JSON object")
    reject_secrets(shared)
    unit = ledger_packet(
        SimpleNamespace(ledger=options.ledger, task=options.task, packets=options.packets, attempt=options.attempt)
    )
    snap = load_json(options.input, "input snapshot") if options.input else snapshot_input(options)
    require(isinstance(snap, dict), "input snapshot must be a JSON object")
    root = absolute(options.evidence_root, resolve=False) / "tasks" / options.task
    role, attempt = options.role, options.attempt
    stop = shared.get("stop") if isinstance(shared.get("stop"), dict) else {}
    contract = {
        "version": 1,
        "runId": unit["runId"],
        "task": options.task,
        "attempt": attempt,
        "role": role,
        "purpose": purpose(unit["packet"]),
        "input": {
            "repo": snap.get("repo"),
            "head": snap.get("head"),
            "indexSha256": snap.get("indexSha256"),
            "snapshotId": snap.get("snapshotId"),
            "packetSha256": sha256(Path(options.packets) / f"{options.task}.json"),
        },
        "allowed": {"forbidden": []} | (shared.get("allowed") if isinstance(shared.get("allowed"), dict) else {}),
        "decisions": shared.get("decisions", []),
        "contracts": {
            "shared": [reference(absolute(p, resolve=False), "shared contract") for p in shared.get("contracts", [])],
            "dependencies": unit["dependencies"],
        },
        "evidence": {
            "dir": str(root),
            "note": str(root / f"{NOTE_PREFIX[role]}-{attempt}.md"),
            "result": str(root / f"result-{role}-{attempt}.json"),
            "contract": str(root / f"contract-{role}-{attempt}.json"),
        },
        "output": {
            **reference(
                absolute(text(shared.get("output"), "shared output schema path"), resolve=False), "output schema"
            ),
            "schema": load_json(shared["output"], "output schema"),
        },
        "stop": {"when": stop.get("when", []), "gates": stop.get("gates", [])},
    }
    validate(contract)
    target = Path(contract["evidence"]["contract"])
    require(not target.exists(), f"{target} exists; a new attempt gets a new contract file")
    target.parent.mkdir(parents=True, exist_ok=True)
    write_exclusive(target, contract)
    return contract


def check(options):
    contract = validate(load_json(options.contract, "contract"))
    return {"contract": options.contract, "task": contract["task"], "attempt": contract["attempt"], "ok": True}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    make = commands.add_parser(
        "build", help="write one worker's contract from the ledger packet, snapshot and shared rules"
    )
    make.add_argument("--ledger", required=True)
    make.add_argument("--task", required=True)
    make.add_argument("--packets", required=True, help="directory of <ID>.json packets registered in the ledger")
    make.add_argument("--shared", required=True, help="run-level JSON: allowed, decisions, contracts, output, stop")
    make.add_argument("--evidence-root", required=True, help="run directory outside the repository; gets tasks/<ID>/")
    make.add_argument("--attempt", type=int, default=1)
    make.add_argument("--role", default="implementer", choices=ROLES)
    source = make.add_mutually_exclusive_group(required=True)
    source.add_argument("--input", help="snapshot JSON already captured by work_ledger.py snapshot")
    source.add_argument("--repo", help="measure the snapshot now, before the worker starts")
    verify = commands.add_parser(
        "validate", help="check a contract's shape and that its referenced files are unchanged"
    )
    verify.add_argument("--contract", required=True)
    options = parser.parse_args(argv)
    try:
        result = {"build": build, "validate": check}[options.command](options)
    except (ContractError, LedgerError, ManifestError) as exc:
        print(f"work-unit: {exc}", file=sys.stderr)
        return 1
    print(json.dumps(result, indent=1, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
