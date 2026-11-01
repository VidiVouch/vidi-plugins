"""The work ledger is keyed by task ID, keeps every attempt, derives state only from
recorded evidence or an explicit mark, and hands a worker exactly its own packet plus
its dependencies' outcomes. These tests run the real CLI against temporary files."""

import json
import subprocess
import sys
from pathlib import Path

import pytest
from conftest import REPO

SCRIPT = REPO / "skills/deep-review/scripts/work_ledger.py"
TASKS = [
    {"id": "A-1", "deps": [], "mode": "implement"},
    {"id": "A-2", "deps": ["A-1"], "mode": "implement"},
    {"id": "B-1", "deps": ["A-1"], "mode": "prepare-only"},
]


def cli(*args, success=True):
    result = subprocess.run([sys.executable, str(SCRIPT), *map(str, args)], capture_output=True, text=True, timeout=30)
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


def note(tmp_path, name):
    path = tmp_path / f"{name}.md"
    path.write_text("# note\n\ndetail\n")
    return str(path)


def implement(tmp_path, name="impl", **overrides):
    result = {
        "disposition": "completed",
        "summary": "did the work",
        "artifact": {"range": "a" * 40 + "..worktree", "files": ["/repo/file.py"]},
        "checks": [{"command": "pytest", "passed": True}],
        "findings": {"unresolved": [], "refuted": []},
        "deviations": [],
        "nextAction": "verify",
        "notePath": note(tmp_path, "implement-1"),
        "openObligations": ["A-2: reuse the helper"],
        "gatedActions": ["commits-pushes: push withheld"],
    }
    return write(tmp_path / f"{name}.json", result | overrides)


def verify(tmp_path, name="ver", **overrides):
    result = {
        "disposition": "accepted",
        "summary": "checked",
        "artifact": {"range": "a" * 40 + "..worktree", "files": ["/repo/file.py"]},
        "checks": [{"command": "pytest", "passed": True}],
        "findings": {"unresolved": [], "refuted": []},
        "deviations": [],
        "nextAction": "close",
        "notePath": note(tmp_path, "verify-1"),
        "openObligations": [],
        "gatedActions": ["commits-pushes: push withheld"] if overrides.get("disposition") == "blocked-approval" else [],
    }
    return write(tmp_path / f"{name}.json", result | overrides)


@pytest.fixture
def case(tmp_path):
    packets = tmp_path / "packets"
    packets.mkdir()
    for task in TASKS:
        write(packets / f"{task['id']}.json", {"id": task["id"], "Work": f"secret work statement of {task['id']}"})
    ledger = tmp_path / "ledger.json"
    cli(
        "init",
        "--ledger",
        ledger,
        "--run-id",
        "run-1",
        "--tasks",
        write(tmp_path / "tasks.json", {"tasks": TASKS}),
        "--packets",
        packets,
    )
    return ledger, packets


def load(ledger):
    return json.loads(Path(ledger).read_text())["tasks"]


def test_init_creates_pending_entries_keyed_by_task_id_and_refuses_overwrite(case, tmp_path):
    ledger, packets = case
    tasks = load(ledger)
    assert list(tasks) == ["A-1", "A-2", "B-1"]
    assert all(t["state"] == "pending" and t["terminal"] is False and t["attempts"] == [] for t in tasks.values())
    assert tasks["A-2"]["deps"] == ["A-1"] and tasks["B-1"]["mode"] == "prepare-only"
    assert len(tasks["A-1"]["packetSha256"]) == 64
    assert "already exists" in cli(
        "init", "--ledger", ledger, "--run-id", "x", "--tasks", tmp_path / "tasks.json", success=False
    )
    bad = write(tmp_path / "bad.json", [{"id": "X", "deps": ["missing"]}])
    assert "unknown task" in cli(
        "init", "--ledger", tmp_path / "other.json", "--run-id", "x", "--tasks", bad, success=False
    )


def test_record_keeps_owner_input_attempt_artifacts_verification_and_state(case, tmp_path):
    ledger, packets = case
    snap = write(tmp_path / "snap.json", {"head": "abc", "snapshotId": "s1"})
    first = cli(
        "record",
        "--ledger",
        ledger,
        "--task",
        "A-1",
        "--attempt",
        1,
        "--role",
        "implementer",
        "--owner",
        "agent-1 A-1 implement#1",
        "--result",
        implement(tmp_path),
        "--input",
        snap,
        "--packet",
        packets / "A-1.json",
    )
    assert first == {"task": "A-1", "state": "in-progress", "terminal": False}
    task = load(ledger)["A-1"]
    assert task["attempts"][0]["owner"] == "agent-1 A-1 implement#1"
    assert task["attempts"][0]["input"] == {"head": "abc", "snapshotId": "s1"}
    assert task["attempts"][0]["attempt"] == 1 and task["attempts"][0]["recordedAt"]
    assert task["artifacts"] == sorted([str(tmp_path / "implement-1.md"), "/repo/file.py"])
    assert task["openObligations"] == ["A-2: reuse the helper"] and task["gatedActions"] == [
        "commits-pushes: push withheld"
    ]
    assert task["verification"] is None
    second = cli(
        "record",
        "--ledger",
        ledger,
        "--task",
        "A-1",
        "--attempt",
        1,
        "--role",
        "verifier",
        "--owner",
        "agent-2",
        "--result",
        verify(tmp_path),
    )
    assert second == {"task": "A-1", "state": "accepted", "terminal": True}
    task = load(ledger)["A-1"]
    assert task["verification"]["disposition"] == "accepted" and task["verification"]["unresolved"] == []
    assert task["verification"]["notePath"] == str(tmp_path / "verify-1.md")
    assert task["verification"]["owner"] == "agent-2" and task["summary"] == "checked"
    assert str(tmp_path / "verify-1.md") in task["artifacts"] and len(task["attempts"]) == 2


@pytest.mark.parametrize(
    ("verdict", "state", "terminal"),
    [
        ("accepted", "accepted", True),
        ("blocked-approval", "awaiting-approval", True),
        ("changes-required", "changes-required", False),
        ("failed-verification", "failed-verification", False),
    ],
)
def test_terminal_state_follows_the_verifier_not_the_implementer(case, tmp_path, verdict, state, terminal):
    ledger, _ = case
    cli(
        "record",
        "--ledger",
        ledger,
        "--task",
        "A-1",
        "--attempt",
        1,
        "--role",
        "implementer",
        "--owner",
        "w",
        "--result",
        implement(tmp_path),
    )
    cli(
        "record",
        "--ledger",
        ledger,
        "--task",
        "A-1",
        "--attempt",
        1,
        "--role",
        "verifier",
        "--owner",
        "v",
        "--result",
        verify(tmp_path, disposition=verdict),
    )
    task = load(ledger)["A-1"]
    assert (task["state"], task["terminal"]) == (state, terminal)


def test_retry_needs_a_new_attempt_and_the_latest_attempt_decides(case, tmp_path):
    ledger, _ = case
    cli(
        "record",
        "--ledger",
        ledger,
        "--task",
        "A-1",
        "--attempt",
        1,
        "--role",
        "implementer",
        "--owner",
        "w",
        "--result",
        implement(tmp_path),
    )
    cli(
        "record",
        "--ledger",
        ledger,
        "--task",
        "A-1",
        "--attempt",
        1,
        "--role",
        "verifier",
        "--owner",
        "v",
        "--result",
        verify(tmp_path, disposition="changes-required"),
    )
    error = cli(
        "record",
        "--ledger",
        ledger,
        "--task",
        "A-1",
        "--attempt",
        1,
        "--role",
        "implementer",
        "--owner",
        "w",
        "--result",
        implement(tmp_path),
        success=False,
    )
    assert "already recorded" in error
    error = cli(
        "record",
        "--ledger",
        ledger,
        "--task",
        "A-1",
        "--attempt",
        2,
        "--role",
        "verifier",
        "--owner",
        "v",
        "--result",
        verify(tmp_path),
        success=False,
    )
    assert "implementer record for the same attempt" in error
    cli(
        "record",
        "--ledger",
        ledger,
        "--task",
        "A-1",
        "--attempt",
        2,
        "--role",
        "implementer",
        "--owner",
        "w2",
        "--result",
        implement(tmp_path, disposition="failed"),
    )
    task = load(ledger)["A-1"]
    assert task["state"] == "failed" and task["terminal"] is True and len(task["attempts"]) == 3


def test_mark_records_states_no_worker_produces(case, tmp_path):
    ledger, _ = case
    out = cli(
        "mark",
        "--ledger",
        ledger,
        "--task",
        "B-1",
        "--state",
        "blocked-dependency",
        "--reason",
        "unmet: A-1",
        "--owner",
        "overseer",
    )
    assert out == {"task": "B-1", "state": "blocked-dependency", "terminal": True}
    assert load(ledger)["B-1"]["mark"]["reason"] == "unmet: A-1"
    assert "mark state" in cli(
        "mark",
        "--ledger",
        ledger,
        "--task",
        "B-1",
        "--state",
        "accepted",
        "--reason",
        "x",
        "--owner",
        "o",
        success=False,
    )


@pytest.mark.parametrize(
    "bad",
    [
        {"disposition": "done"},
        {"disposition": None},
        {"checks": []},
        {"notePath": "/nowhere/implement-1.md"},
        {"deviations": [{"apiKey": "sk-DO-NOT-STORE"}]},
    ],
)
def test_results_are_checked_as_hand_backs_and_credentials_rejected_before_storing(case, tmp_path, bad):
    ledger, _ = case
    before = Path(ledger).read_bytes()
    error = cli(
        "record",
        "--ledger",
        ledger,
        "--task",
        "A-1",
        "--attempt",
        1,
        "--role",
        "implementer",
        "--owner",
        "w",
        "--result",
        implement(tmp_path, **bad),
        success=False,
    )
    assert "DO-NOT-STORE" not in error
    assert Path(ledger).read_bytes() == before


def test_record_checks_the_range_against_the_input_repository(case, tmp_path):
    ledger, _ = case
    repo = tmp_path / "repo"
    repo.mkdir()
    for args in (["init", "-q"], ["config", "user.email", "t@example.invalid"], ["config", "user.name", "T"]):
        subprocess.run(["git", "-C", str(repo), *args], check=True)
    (repo / "f.txt").write_text("one\n")
    subprocess.run(["git", "-C", str(repo), "add", "."], check=True)
    subprocess.run(["git", "-C", str(repo), "commit", "-qm", "init"], check=True)
    snap = write(tmp_path / "snap.json", cli("snapshot", "--repo", repo))
    args = ("record", "--ledger", ledger, "--task", "A-1", "--attempt", 1, "--role", "implementer", "--owner", "w")
    fake = implement(tmp_path, artifact={"range": "a" * 40 + "..worktree", "files": []})
    assert "not a commit in" in cli(*args, "--result", fake, "--input", snap, success=False)
    real = implement(tmp_path, artifact={"range": json.loads(snap.read_text())["head"] + "..worktree", "files": []})
    assert cli(*args, "--result", real, "--input", snap)["state"] == "in-progress"


def test_legacy_results_need_the_flag_and_still_derive_state(case, tmp_path):
    ledger, _ = case
    legacy = write(
        tmp_path / "legacy.json",
        {
            "status": "completed",
            "summary": "old shape",
            "filesChanged": ["/repo/old.py"],
            "checks": [],
            "notePath": "/notes/implement-1.md",
            "openObligations": [],
            "gatedActions": [],
        },
    )
    args = ("record", "--ledger", ledger, "--task", "A-1", "--attempt", 1, "--owner", "w")
    assert "exactly these keys" in cli(*args, "--role", "implementer", "--result", legacy, success=False)
    assert cli(*args, "--role", "implementer", "--result", legacy, "--legacy")["state"] == "in-progress"
    old_verdict = write(tmp_path / "legacy-ver.json", {"verdict": "accepted", "summary": "ok", "issues": ["i"]})
    assert cli(*args, "--role", "verifier", "--result", old_verdict, "--legacy")["state"] == "accepted"
    task = load(ledger)["A-1"]
    assert task["artifacts"] == ["/notes/implement-1.md", "/repo/old.py"]
    assert task["verification"]["disposition"] == "accepted" and task["verification"]["unresolved"] == ["i"]
    bad = write(tmp_path / "bad.json", {"status": "done"})
    assert "known disposition" in cli(
        "record",
        "--ledger",
        ledger,
        "--task",
        "A-2",
        "--attempt",
        1,
        "--owner",
        "w",
        "--role",
        "implementer",
        "--result",
        bad,
        "--legacy",
        success=False,
    )


def test_packet_gives_a_worker_only_its_task_and_dependency_outcomes(case, tmp_path):
    ledger, packets = case
    cli(
        "record",
        "--ledger",
        ledger,
        "--task",
        "A-1",
        "--attempt",
        1,
        "--role",
        "implementer",
        "--owner",
        "w",
        "--result",
        implement(tmp_path),
    )
    cli(
        "record",
        "--ledger",
        ledger,
        "--task",
        "A-1",
        "--attempt",
        1,
        "--role",
        "verifier",
        "--owner",
        "v",
        "--result",
        verify(tmp_path),
    )
    out = cli("packet", "--ledger", ledger, "--task", "A-2", "--packets", packets)
    assert out["task"] == "A-2" and out["packet"]["Work"] == "secret work statement of A-2"
    assert out["dependencies"] == {
        "A-1": {
            "state": "accepted",
            "summary": "checked",
            "notePath": str(tmp_path / "implement-1.md"),
            "verifyNote": str(tmp_path / "verify-1.md"),
            "openObligations": ["A-2: reuse the helper"],
            "gatedActions": ["commits-pushes: push withheld"],
        }
    }
    text = json.dumps(out)
    assert "secret work statement of A-1" not in text and "B-1" not in text
    (packets / "A-2.json").write_text("{}")
    assert "packet changed" in cli("packet", "--ledger", ledger, "--task", "A-2", "--packets", packets, success=False)


def test_summary_lists_counts_open_tasks_obligations_and_gated_actions(case, tmp_path):
    ledger, _ = case
    cli(
        "record",
        "--ledger",
        ledger,
        "--task",
        "A-1",
        "--attempt",
        1,
        "--role",
        "implementer",
        "--owner",
        "w",
        "--result",
        implement(tmp_path),
    )
    cli(
        "mark",
        "--ledger",
        ledger,
        "--task",
        "B-1",
        "--state",
        "not-started",
        "--reason",
        "run stopped",
        "--owner",
        "overseer",
    )
    out = cli("summary", "--ledger", ledger)
    assert out["counts"] == {"in-progress": 1, "not-started": 1, "pending": 1}
    assert out["open"] == ["A-1", "A-2"]
    assert out["openObligations"] == {"A-1": ["A-2: reuse the helper"]}
    assert out["gatedActions"] == {"A-1": ["commits-pushes: push withheld"]}


def test_snapshot_identity_matches_run_manifest(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    for args in (["init", "-q"], ["config", "user.email", "t@example.invalid"], ["config", "user.name", "T"]):
        subprocess.run(["git", "-C", str(repo), *args], check=True)
    (repo / "f.txt").write_text("one\n")
    subprocess.run(["git", "-C", str(repo), "add", "."], check=True)
    subprocess.run(["git", "-C", str(repo), "commit", "-qm", "init"], check=True)
    first = cli("snapshot", "--repo", repo)
    assert set(first) == {"repo", "head", "indexSha256", "snapshotId"} and len(first["snapshotId"]) == 64
    (repo / "f.txt").write_text("two\n")
    assert cli("snapshot", "--repo", repo)["snapshotId"] != first["snapshotId"]
