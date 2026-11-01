"""A hand-back is the short result a worker returns: exact artifact range, the checks
that ran, one disposition, unresolved and refuted findings, deviations, the next action
and the path of the note that holds the detail. These tests run the real CLI."""

import importlib
import json
import re
import subprocess
import sys
from pathlib import Path

import pytest
from conftest import REPO

SCRIPTS = REPO / "skills/deep-review/scripts"
SCHEMA = REPO / "skills/deep-review/hand-back.schema.json"
REFERENCE = REPO / "skills/deep-review/references/hand-back.md"
sys.path.insert(0, str(SCRIPTS))
hand_back = importlib.import_module("hand_back")


def cli(*args, success=True):
    argv = [sys.executable, str(SCRIPTS / "hand_back.py"), "check", *map(str, args)]
    result = subprocess.run(argv, capture_output=True, text=True, timeout=30)
    if success:
        assert result.returncode == 0, result.stderr
        assert result.stderr == ""
        return json.loads(result.stdout)
    assert result.returncode == 1, result.stdout
    assert not result.stdout and "Traceback" not in result.stderr
    return result.stderr


def hand(tmp_path, **overrides):
    note = tmp_path / "implement-1.md"
    note.write_text("# note\n\nthe detail\n")
    result = {
        "disposition": "completed",
        "summary": "did the work",
        "artifact": {"range": "a" * 40 + ".." + "b" * 40, "files": ["skills/x.py"]},
        "checks": [{"command": "uv run pytest -q", "passed": True, "note": "12 passed"}],
        "findings": {"unresolved": [], "refuted": ["claim X was wrong"]},
        "deviations": [],
        "nextAction": "dispatch the verifier against the range",
        "notePath": str(note),
        "openObligations": ["A-2: reuse the helper"],
        "gatedActions": [],
    }
    for key, value in overrides.items():
        if value is ...:
            del result[key]
        else:
            result[key] = value
    path = tmp_path / "result.json"
    path.write_text(json.dumps(result))
    return path


def test_a_complete_hand_back_passes_for_its_role(tmp_path):
    out = cli("--result", hand(tmp_path), "--role", "implementer")
    assert out["ok"] is True and out["disposition"] == "completed"
    verifier = hand(tmp_path, disposition="accepted", findings={"unresolved": ["x"], "refuted": []})
    assert cli("--result", verifier, "--role", "verifier")["disposition"] == "accepted"
    partial = hand(tmp_path, disposition="partial", checks=[{"command": "pytest", "passed": False}])
    assert cli("--result", partial, "--role", "implementer")["ok"] is True


@pytest.mark.parametrize(
    ("change", "message"),
    [
        ({"deviations": ...}, "exactly these keys"),
        ({"extra": 1}, "exactly these keys"),
        ({"disposition": "accepted"}, "implementer disposition must be one of"),
        ({"disposition": "done"}, "implementer disposition must be one of"),
        ({"checks": []}, "completed needs at least one check"),
        ({"checks": [{"command": "pytest", "passed": False}]}, "completed contradicts a failed check"),
        ({"checks": [{"command": "pytest", "passed": "yes"}]}, "passed must be true or false"),
        ({"checks": [{"command": "pytest", "passed": True, "stdout": "..."}]}, "each check needs command"),
        ({"disposition": "blocked-approval"}, "must name the gated action"),
        ({"gatedActions": ["push withheld"]}, "must start with a gate ID"),
        ({"summary": "x" * 501}, "exceeds 500 characters"),
        ({"summary": ""}, "summary must be a non-empty string"),
        ({"nextAction": "   "}, "nextAction must be a non-empty string"),
        ({"artifact": {"range": "abc..def", "files": []}}, "full hashes"),
        ({"artifact": {"range": "a" * 40 + "..worktree"}}, "artifact needs range and files"),
        ({"findings": {"unresolved": []}}, "findings needs unresolved and refuted"),
        ({"findings": {"unresolved": [""], "refuted": []}}, "unresolved findings item"),
        ({"notePath": "/nowhere/implement-1.md"}, "existing, non-empty note"),
        ({"notePath": "relative.md"}, "must be absolute"),
        ({"openObligations": "A-2"}, "openObligations must be a list"),
        ({"deviations": [{"secretToken": "x"}]}, "prohibited credential"),
    ],
)
def test_refuses_an_incomplete_or_contradictory_hand_back(tmp_path, change, message):
    assert message in cli("--result", hand(tmp_path, **change), "--role", "implementer", success=False)


def test_empty_note_and_pre_contract_shape_are_refused(tmp_path):
    path = hand(tmp_path)
    Path(json.loads(path.read_text())["notePath"]).write_text("")
    assert "existing, non-empty note" in cli("--result", path, "--role", "implementer", success=False)
    legacy = tmp_path / "legacy.json"
    legacy.write_text(
        json.dumps(
            {
                "status": "completed",
                "summary": "s",
                "filesChanged": [],
                "checks": [],
                "notePath": str(path),
                "openObligations": [],
                "gatedActions": [],
            }
        )
    )
    assert "exactly these keys" in cli("--result", legacy, "--role", "implementer", success=False)


def test_range_commits_must_exist_in_the_repository_and_the_note_outside_it(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    for args in (["init", "-q"], ["config", "user.email", "t@example.invalid"], ["config", "user.name", "T"]):
        subprocess.run(["git", "-C", str(repo), *args], check=True)
    (repo / "f.txt").write_text("one\n")
    subprocess.run(["git", "-C", str(repo), "add", "."], check=True)
    subprocess.run(["git", "-C", str(repo), "commit", "-qm", "init"], check=True)
    head = subprocess.run(["git", "-C", str(repo), "rev-parse", "HEAD"], capture_output=True, text=True).stdout.strip()
    good = hand(tmp_path, artifact={"range": f"{head}..worktree", "files": ["f.txt"]})
    assert cli("--result", good, "--role", "implementer", "--repo", repo)["ok"] is True
    fake = hand(tmp_path, artifact={"range": f"{head}..{'c' * 40}", "files": []})
    assert "not a commit in" in cli("--result", fake, "--role", "implementer", "--repo", repo, success=False)
    inside = repo / "note.md"
    inside.write_text("detail\n")
    error = cli(
        "--result", hand(tmp_path, notePath=str(inside)), "--role", "implementer", "--repo", repo, success=False
    )
    assert "outside the repository" in error


def test_schema_checker_reference_and_gate_ids_agree():
    schema = json.loads(SCHEMA.read_text())
    assert schema["additionalProperties"] is False
    assert list(hand_back.KEYS) == schema["required"] == list(schema["properties"])
    assert set(schema["properties"]["disposition"]["enum"]) == {
        d for role in hand_back.DISPOSITIONS.values() for d in role
    }
    assert schema["properties"]["summary"]["maxLength"] == hand_back.MAX_CHARS
    assert schema["properties"]["artifact"]["properties"]["range"]["pattern"] == hand_back.RANGE.pattern
    gates = re.findall(r"- `([a-z-]+)`:", (REPO / "skills/deep-review/references/approvals.md").read_text())
    assert list(hand_back.GATES) == gates
    reference = REFERENCE.read_text()
    for key in hand_back.KEYS:
        assert f"`{key}`" in reference, key
    assert "\u2014" not in reference and "\u2013" not in reference, "prose for agents uses plain dashes"
    for sibling in ("work-unit.md", "work-ledger.md"):
        assert "hand-back.md" in (REPO / "skills/deep-review/references" / sibling).read_text()
