"""The work-unit contract gives a fresh worker its purpose, exact input snapshot, allowed
files and actions, binding decisions, pinned shared contracts and dependency outcomes,
evidence paths, output schema and stop condition, and nothing about other tasks. These
tests run the real CLI against temporary files."""

import json
import os
import re
import subprocess
import sys
from pathlib import Path

import pytest
from conftest import REPO

SCRIPTS = REPO / "skills/deep-review/scripts"
SCHEMA = REPO / "skills/deep-review/work-unit.schema.json"
REFERENCE = REPO / "skills/deep-review/references/work-unit.md"
APPROVALS = REPO / "skills/deep-review/references/approvals.md"
TASKS = [
    {"id": "A-1", "deps": [], "mode": "implement"},
    {"id": "A-2", "deps": ["A-1"], "mode": "implement"},
    {"id": "B-1", "deps": ["A-1"], "mode": "prepare-only"},
    {"id": "C-1", "deps": ["A-1"], "mode": "implement"},
]
# C-1 is shaped like the plan's COMPONENT packets: no Work, a Component and Historical need instead.
COMPONENT = {
    "Component": "Decision ledger",
    "Historical need": "Portal exports spanned rounds.",
    "Acceptance check": "Stable IDs survive export.",
}
SNAPSHOT = {"repo": "/repo", "head": "a" * 40, "indexSha256": "b" * 64, "snapshotId": "c" * 64}


def cli(script, *args, success=True):
    argv = [sys.executable, str(SCRIPTS / script), *map(str, args)]
    result = subprocess.run(argv, capture_output=True, text=True, timeout=30)
    if success:
        assert result.returncode == 0, result.stderr
        assert result.stderr == ""
        return json.loads(result.stdout)
    assert result.returncode == 1, result.stdout
    assert not result.stdout and "Traceback" not in result.stderr
    return result.stderr


def write(path, value):
    Path(path).write_text(json.dumps(value))
    return path


@pytest.fixture
def run(tmp_path):
    """A ledger with A-1 accepted, a shared document, and a snapshot file."""
    packets = tmp_path / "packets"
    packets.mkdir()
    for task in TASKS:
        brief = {"id": task["id"], "mode": task["mode"], "sectionContext": ["section context"]}
        if task["id"] == "C-1":
            brief |= COMPONENT
        else:
            brief |= {"Work": f"secret work statement of {task['id']}", "Done when": f"criterion of {task['id']}"}
        write(packets / f"{task['id']}.json", brief)
    ledger = tmp_path / "ledger.json"
    cli(
        "work_ledger.py",
        "init",
        "--ledger",
        ledger,
        "--run-id",
        "run-1",
        "--tasks",
        write(tmp_path / "t.json", TASKS),
        "--packets",
        packets,
    )
    impl = {
        "status": "completed",
        "summary": "did it",
        "notePath": "/notes/implement-1.md",
        "openObligations": ["A-2: reuse X"],
    }
    ver = {"verdict": "accepted", "summary": "checked", "issues": [], "checks": [], "notePath": "/notes/verify-1.md"}
    for role, result in (("implementer", impl), ("verifier", ver)):
        args = ("--ledger", ledger, "--task", "A-1", "--attempt", 1, "--role", role, "--owner", "w")
        cli("work_ledger.py", "record", *args, "--result", write(tmp_path / f"{role}.json", result))
    api = tmp_path / "api.md"
    api.write_text("shared API contract\n")
    output = write(
        tmp_path / "result.schema.json", {"required": ["status"], "properties": {"status": {"type": "string"}}}
    )
    shared = write(
        tmp_path / "shared.json",
        {
            "allowed": {"files": ["skills/**"], "actions": ["commit on the current branch"], "forbidden": ["push"]},
            "decisions": ["snapshot identity comes from run_manifest.py"],
            "contracts": [str(api)],
            "output": str(output),
            "stop": {"when": ["criteria met and checks pass", "a closed gate"], "gates": ["commits-pushes"]},
        },
    )
    evidence = tmp_path / "run"
    base = ("build", "--ledger", ledger, "--packets", packets, "--shared", shared, "--evidence-root", evidence)
    return {
        "base": base,
        "shared": shared,
        "api": api,
        "output": output,
        "snap": write(tmp_path / "snap.json", SNAPSHOT),
        "evidence": evidence,
    }


def build(run, *extra, task="A-2", success=True):
    return cli("work_unit.py", *run["base"], "--task", task, "--input", run["snap"], *extra, success=success)


def test_build_writes_every_section_and_only_this_task(run):
    contract = build(run)
    schema = json.loads(SCHEMA.read_text())
    assert set(contract) == set(schema["required"])
    assert contract["purpose"] == {
        "work": "secret work statement of A-2",
        "acceptance": "criterion of A-2",
        "mode": "implement",
        "context": ["section context"],
    }
    assert contract["input"] == SNAPSHOT | {"packetSha256": contract["input"]["packetSha256"]}
    assert contract["allowed"]["forbidden"] == ["push"] and contract["decisions"] == [
        "snapshot identity comes from run_manifest.py"
    ]
    assert [c["path"] for c in contract["contracts"]["shared"]] == [str(run["api"])]
    assert contract["contracts"]["dependencies"]["A-1"]["state"] == "accepted"
    assert contract["contracts"]["dependencies"]["A-1"]["openObligations"] == ["A-2: reuse X"]
    assert contract["output"]["schema"]["required"] == ["status"] and contract["output"]["path"] == str(run["output"])
    assert contract["stop"] == {"when": ["criteria met and checks pass", "a closed gate"], "gates": ["commits-pushes"]}
    text = json.dumps(contract)
    assert "secret work statement of A-1" not in text and "B-1" not in text and "C-1" not in text
    target = Path(contract["evidence"]["contract"])
    assert target == run["evidence"] / "tasks/A-2/contract-implementer-1.json"
    assert json.loads(target.read_text()) == contract and not os.access(target, os.W_OK)
    assert contract["evidence"]["note"].endswith("tasks/A-2/implement-1.md")
    assert contract["evidence"]["result"].endswith("tasks/A-2/result-implementer-1.json")


def test_component_packet_without_work_key_still_yields_a_purpose(run):
    contract = build(run, task="C-1")
    assert contract["purpose"]["work"] == "Component: Decision ledger. Historical need: Portal exports spanned rounds."
    assert contract["purpose"]["acceptance"] == "Stable IDs survive export."
    assert "secret work statement" not in json.dumps(contract)


def test_validate_requires_every_dependency_outcome_key(run, tmp_path):
    contract = json.loads(Path(build(run)["evidence"]["contract"]).read_text())
    assert set(contract["contracts"]["dependencies"]["A-1"]) == set(
        json.loads(SCHEMA.read_text())["properties"]["contracts"]["properties"]["dependencies"]["additionalProperties"][
            "required"
        ]
    )
    del contract["contracts"]["dependencies"]["A-1"]["verifyNote"]
    tampered = write(tmp_path / "tampered.json", contract)
    assert "dependency A-1 needs" in cli("work_unit.py", "validate", "--contract", tampered, success=False)


def test_each_attempt_and_role_gets_its_own_contract_file(run):
    first = build(run)
    assert "exists" in build(run, success=False)
    # A retry is a fresh unit: the same brief against the same inputs, its own evidence
    # files, and nothing from attempt 1's note, result or contract.
    retry = build(run, "--attempt", 2)
    assert retry["attempt"] == 2 and "implementer-1" not in json.dumps(retry["evidence"])
    strip = lambda c: {k: v for k, v in c.items() if k not in ("attempt", "evidence")}  # noqa: E731
    assert strip(retry) == strip(first)
    second = build(run, "--attempt", 2, "--role", "verifier")
    assert second["evidence"]["note"].endswith("tasks/A-2/verify-2.md")
    assert second["evidence"]["contract"].endswith("tasks/A-2/contract-verifier-2.json")


def test_validate_refuses_drifted_shared_contract_or_output_schema(run):
    contract = build(run)["evidence"]["contract"]
    assert cli("work_unit.py", "validate", "--contract", contract)["ok"] is True
    run["api"].write_text("changed API\n")
    assert "shared contract changed" in cli("work_unit.py", "validate", "--contract", contract, success=False)
    run["api"].write_text("shared API contract\n")
    write(run["output"], {"required": ["verdict"], "properties": {}})
    assert "output schema changed" in cli("work_unit.py", "validate", "--contract", contract, success=False)


@pytest.mark.parametrize(
    ("change", "message"),
    [
        ({"allowed": {"files": [], "actions": ["x"]}}, "allowed files must not be empty"),
        ({"stop": {"when": ["done"], "gates": ["pushes"]}}, "stop gates must be among"),
        ({"stop": {"when": [], "gates": []}}, "stop conditions must not be empty"),
        ({"contracts": ["/nowhere/api.md"]}, "cannot read shared contract"),
        ({"env": {"PATH": "x"}}, "prohibited credential or environment key"),
        ({"output": None}, "shared output schema path"),
    ],
)
def test_build_refuses_an_incomplete_shared_document(run, change, message):
    shared = json.loads(run["shared"].read_text()) | change
    write(run["shared"], shared)
    assert message in build(run, success=False)
    assert not (run["evidence"] / "tasks/A-2").exists()


def test_build_refuses_a_packet_without_acceptance_and_evidence_inside_the_repo(run, tmp_path):
    packets = Path(run["base"][4])
    write(packets / "B-1.json", json.loads((packets / "B-1.json").read_text()) | {"Done when": ""})
    assert "packet changed since init" in build(run, task="B-1", success=False)
    ledger = tmp_path / "ledger2.json"
    cli("work_ledger.py", "init", "--ledger", ledger, "--run-id", "run-2", "--tasks", tmp_path / "t.json")
    args = list(run["base"])
    args[args.index("--ledger") + 1] = ledger
    assert "packet acceptance" in cli("work_unit.py", *args, "--task", "B-1", "--input", run["snap"], success=False)
    inside = write(tmp_path / "inside.json", SNAPSHOT | {"repo": str(run["evidence"].parent)})
    assert "outside the repository" in cli(
        "work_unit.py", *run["base"], "--task", "A-2", "--input", inside, success=False
    )


def test_measured_snapshot_matches_the_ledger_identity(run, tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    for args in (["init", "-q"], ["config", "user.email", "t@example.invalid"], ["config", "user.name", "T"]):
        subprocess.run(["git", "-C", str(repo), *args], check=True)
    (repo / "f.txt").write_text("one\n")
    subprocess.run(["git", "-C", str(repo), "add", "."], check=True)
    subprocess.run(["git", "-C", str(repo), "commit", "-qm", "init"], check=True)
    expected = cli("work_ledger.py", "snapshot", "--repo", repo)
    contract = cli("work_unit.py", *run["base"], "--task", "A-2", "--repo", repo)
    assert contract["input"] == expected | {"packetSha256": contract["input"]["packetSha256"]}


def test_schema_validator_reference_and_gate_ids_agree():
    schema = json.loads(SCHEMA.read_text())
    source = (SCRIPTS / "work_unit.py").read_text()
    sections = re.search(r"SECTIONS = \((.*?)\)", source, re.S).group(1)
    assert set(re.findall(r'"([a-z]+)"', sections)) == set(schema["required"]) - {
        "version",
        "runId",
        "task",
        "attempt",
        "role",
    }
    dependency_keys = re.search(r"DEPENDENCY_KEYS = \((.*?)\)", source, re.S).group(1)
    assert (
        re.findall(r'"([A-Za-z]+)"', dependency_keys)
        == (schema["properties"]["contracts"]["properties"]["dependencies"]["additionalProperties"]["required"])
    )
    gates = re.findall(r"- `([a-z-]+)`:", APPROVALS.read_text())
    assert schema["properties"]["stop"]["properties"]["gates"]["items"]["enum"] == gates
    assert re.findall(r'"([a-z-]+)"', re.search(r"GATES = \((.*?)\)", source, re.S).group(1)) == gates
    reference = REFERENCE.read_text()
    for section in schema["required"][5:]:
        assert f"`{section}" in reference, section
    assert "\u2014" not in reference and "\u2013" not in reference, "prose for agents uses plain dashes"
    assert "work-unit.md" in (REPO / "skills/deep-review/references/work-ledger.md").read_text()


def test_reference_separates_ordinary_workers_from_temporary_teammates():
    """A definition name selects a type; a runtime instance name makes a teammate under agent teams."""
    section = " ".join(REFERENCE.read_text().split("## Ordinary workers and temporary teammates")[1].split())
    assert "`agentType`" in section and "`subagent_type`" in section
    assert "never requires an instance name" in section
    assert "CLAUDE_CODE_EXPERIMENTAL_AGENT_TEAMS=1" in section and "launches a teammate" in section
    assert "team availability alone changes nothing" in section
    assert "refuses `name` outright" in section
    assert "phase the lead owns" in section and "shuts" in section and "replacement gets a new name" in section
    fixer = (REPO / "agents/fixer.md").read_text()
    assert "not a standing identity" in fixer and "new teammate, not you resumed" in fixer
    skill = " ".join((REPO / "skills/deep-review/SKILL.md").read_text().split())
    assert "Ordinary workers and temporary teammates" in skill
    assert "`subagent_type` fixer plus" in skill and "Shut every" in skill
