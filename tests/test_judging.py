"""A judging result is one judge's disposition per candidate finding, each backed by the scenario,
evidence, impact and limits the disposition needs. Confirmed, refuted, unresolved and unreviewed are
distinct and the checker refuses one that lacks what it claims. These tests run the real CLI."""

import importlib
import json
import subprocess
import sys

import pytest
from conftest import REPO

SCRIPTS = REPO / "skills/deep-review/scripts"
SCHEMA = REPO / "skills/deep-review/judging-output.schema.json"
REFERENCE = REPO / "skills/deep-review/references/judging.md"
sys.path.insert(0, str(SCRIPTS))
judging = importlib.import_module("judging")
SNAPSHOT = "ab" * 32


def cli(*args, success=True):
    argv = [sys.executable, str(SCRIPTS / "judging.py"), "check", *map(str, args)]
    result = subprocess.run(argv, capture_output=True, text=True, timeout=30)
    if success:
        assert result.returncode == 0, result.stderr
        assert result.stderr == ""
        return json.loads(result.stdout)
    assert result.returncode == 1, result.stdout
    assert not result.stdout and "Traceback" not in result.stderr
    return result.stderr


def code(ref, note="shows the path"):
    return {"kind": "code", "ref": ref, "note": note}


def finding(disposition, **overrides):
    base = {
        "id": f"F-{disposition}",
        "file": "app/items.py",
        "line_start": 38,
        "line_end": 44,
        "severity": "warning",
        "disposition": disposition,
        "scenario": "POST /items with quantity=-1 passes validate() and the row is inserted",
        "evidence": [code("app/items.py:40-41", "the check tests only the upper bound")],
        "impact": "stock totals go negative for any authenticated user",
        "reason": "traced the request path; no other guard exists",
        "limits": [],
    }
    if disposition == "unresolved":
        base["limits"] = ["needs the database fixture to see whether the column rejects negatives"]
    if disposition == "unreviewed":
        base.update(evidence=[], reason="", impact="", limits=["budget exhausted after three candidates"])
    base.update(overrides)
    return base


def judgment(tmp_path, findings=None, **overrides):
    result = {
        "input": {"snapshotId": SNAPSHOT, "round": 1},
        "findings": [finding(d) for d in judging.DISPOSITIONS] if findings is None else findings,
        "limits": ["static reading only, no command was run"],
        "summary": "one real defect, one false claim, one open question, one not examined",
    }
    for key, value in overrides.items():
        if value is ...:
            del result[key]
        else:
            result[key] = value
    path = tmp_path / "judge-1.json"
    path.write_text(json.dumps(result))
    return path


def candidates(tmp_path, items=None):
    if items is None:
        items = [
            {
                "id": f["id"],
                "file": f["file"],
                "line_start": f["line_start"],
                "line_end": f["line_end"],
                "severity": "nit",
            }
            for f in (finding(d) for d in judging.DISPOSITIONS)
        ]
    path = tmp_path / "candidates.json"
    path.write_text(json.dumps(items))
    return path


def test_a_complete_result_passes_and_counts_each_disposition_separately(tmp_path):
    out = cli("--result", judgment(tmp_path), "--snapshot", SNAPSHOT)
    assert out["ok"] is True and out["round"] == 1
    assert out["counts"] == {"confirmed": 1, "refuted": 1, "unresolved": 1, "unreviewed": 1}
    empty = judgment(tmp_path, findings=[], summary="no candidates this round")
    assert cli("--result", empty)["counts"] == dict.fromkeys(judging.DISPOSITIONS, 0)


@pytest.mark.parametrize(
    ("change", "message"),
    [
        ({"summary": ...}, "exactly these keys"),
        ({"judge": "boss"}, "exactly these keys"),
        ({"input": {"snapshotId": SNAPSHOT}}, "input needs exactly"),
        ({"input": {"snapshotId": "abc", "round": 1}}, "64-hex snapshot id"),
        ({"input": {"snapshotId": SNAPSHOT, "round": 0}}, "round must be a positive integer"),
        ({"limits": "none"}, "limits must be a list"),
        ({"limits": [""]}, "limits item must be a non-empty string"),
        ({"summary": ""}, "summary must be a non-empty string"),
        ({"summary": "x" * 501}, "exceeds 500 characters"),
        ({"findings": {}}, "findings must be a list"),
    ],
)
def test_refuses_a_malformed_result(tmp_path, change, message):
    assert message in cli("--result", judgment(tmp_path, **change), success=False)


@pytest.mark.parametrize(
    ("disposition", "change", "message"),
    [
        ("confirmed", {"reason": ...}, "exactly these keys"),
        ("confirmed", {"claim": "x"}, "exactly these keys"),
        ("confirmed", {"id": "#1"}, "finding id must be"),
        ("confirmed", {"id": "x" * 65}, "finding id must be"),
        ("confirmed", {"file": "/abs/items.py"}, "repo-relative path"),
        ("confirmed", {"file": "../items.py"}, "repo-relative path"),
        ("confirmed", {"line_start": 0}, "line_start must be a positive integer"),
        ("confirmed", {"line_end": 37}, "line_end is before line_start"),
        ("confirmed", {"severity": "high"}, "severity must be one of"),
        ("confirmed", {"disposition": "real"}, "disposition must be one of"),
        ("confirmed", {"disposition": True}, "disposition must be one of"),
        ("confirmed", {"scenario": ""}, "confirmed but has no scenario"),
        ("confirmed", {"evidence": []}, "confirmed but has no evidence"),
        ("confirmed", {"impact": "  "}, "confirmed but has no impact"),
        ("confirmed", {"reason": ""}, "confirmed but has no reason"),
        ("confirmed", {"evidence": [{"kind": "code", "ref": "app/items.py:40"}]}, "exactly kind, ref and note"),
        ("confirmed", {"evidence": [{"kind": "hunch", "ref": "x", "note": ""}]}, "kind must be one of"),
        ("confirmed", {"evidence": [code("app/items.py")]}, "code ref must be"),
        ("confirmed", {"evidence": [code("app/items.py:0")]}, "code ref must be"),
        ("confirmed", {"evidence": [code("app/items.py:9-4")]}, "ends before it starts"),
        ("confirmed", {"evidence": [code("/etc/passwd:1")]}, "repo-relative path"),
        ("confirmed", {"limits": ["x" * 501]}, "exceeds 500 characters"),
        ("refuted", {"evidence": []}, "refuted but has no evidence"),
        ("refuted", {"scenario": ""}, "refuted but has no scenario"),
        ("unresolved", {"limits": []}, "unresolved but has no limits"),
        ("unresolved", {"impact": ""}, "unresolved but has no impact"),
        ("unreviewed", {"limits": []}, "unreviewed but has no limits"),
        ("unreviewed", {"evidence": [code("app/items.py:40")]}, "unreviewed but cites evidence"),
    ],
)
def test_each_disposition_must_carry_what_it_claims(tmp_path, disposition, change, message):
    entry = finding(disposition)
    for key, value in change.items():
        if value is ...:
            del entry[key]
        else:
            entry[key] = value
    assert message in cli("--result", judgment(tmp_path, findings=[entry]), success=False)


def test_refuted_and_unreviewed_may_leave_impact_empty_but_a_duplicate_id_is_refused(tmp_path):
    refuted = finding("refuted", impact="")
    assert cli("--result", judgment(tmp_path, findings=[refuted]))["counts"]["refuted"] == 1
    twice = judgment(tmp_path, findings=[finding("confirmed"), finding("refuted", id="F-confirmed")])
    assert "appears twice" in cli("--result", twice, success=False)


def test_snapshot_and_credential_keys_are_refused(tmp_path):
    error = cli("--result", judgment(tmp_path), "--snapshot", "cd" * 32, success=False)
    assert "judged snapshot" in error and "not " + "cd" * 32 in error
    leaked = judgment(tmp_path, limits=[], summary="s", input={"snapshotId": SNAPSHOT, "round": 1})
    data = json.loads(leaked.read_text())
    data["findings"][0]["evidence"][0] = {"kind": "command", "ref": "x", "note": ""}
    data["findings"][0]["limits"] = []
    data["apiToken"] = "x"
    leaked.write_text(json.dumps(data))
    assert "exactly these keys" in cli("--result", leaked, success=False)
    data.pop("apiToken")
    data["input"] = {"snapshotId": SNAPSHOT, "round": 1, "authToken": "x"}
    leaked.write_text(json.dumps(data))
    assert "prohibited credential" in cli("--result", leaked, success=False)


def test_every_candidate_gets_exactly_one_disposition_at_its_own_anchor(tmp_path):
    given = candidates(tmp_path)
    out = cli("--result", judgment(tmp_path), "--candidates", given)
    assert out["counts"]["unreviewed"] == 1, "a re-rated severity is allowed; the candidates said nit"
    three = [finding(d) for d in judging.DISPOSITIONS if d != "unreviewed"]
    assert "F-unreviewed; a candidate the judge did not examine is unreviewed, not absent" in cli(
        "--result", judgment(tmp_path, findings=three), "--candidates", given, success=False
    )
    five = [finding(d) for d in judging.DISPOSITIONS] + [finding("confirmed", id="F-new")]
    assert "unknown candidates: F-new; a new defect is a candidate for the next review round" in cli(
        "--result", judgment(tmp_path, findings=five), "--candidates", given, success=False
    )
    moved = [finding(d) for d in judging.DISPOSITIONS]
    moved[0]["line_end"] = 60
    error = cli("--result", judgment(tmp_path, findings=moved), "--candidates", given, success=False)
    assert "F-confirmed moved from app/items.py:38-44 to app/items.py:38-60" in error
    bad = candidates(tmp_path, [{"id": "F-confirmed"}, {"id": "F-confirmed"}])
    assert "unique id" in cli("--result", judgment(tmp_path), "--candidates", bad, success=False)
    assert "candidates must be a JSON array" in cli(
        "--result", judgment(tmp_path), "--candidates", judgment(tmp_path), success=False
    )


def test_code_evidence_must_cite_lines_that_exist_in_the_judged_tree(tmp_path):
    repo = tmp_path / "repo"
    (repo / "app").mkdir(parents=True)
    (repo / "app/items.py").write_text("".join(f"line {n}\n" for n in range(1, 45)))
    assert cli("--result", judgment(tmp_path), "--repo", repo)["ok"] is True
    beyond = [finding("confirmed", evidence=[code("app/items.py:44-45")])]
    assert "app/items.py:44-45, beyond its 44 lines" in cli(
        "--result", judgment(tmp_path, findings=beyond), "--repo", repo, success=False
    )
    missing = [finding("confirmed", evidence=[code("app/other.py:1")])]
    assert "app/other.py, which is not a file in" in cli(
        "--result", judgment(tmp_path, findings=missing), "--repo", repo, success=False
    )
    (repo / "app/link.py").symlink_to(tmp_path / "judge-1.json")
    escaped = [finding("confirmed", evidence=[code("app/link.py:1")])]
    assert "app/link.py, which is not a file in" in cli(
        "--result", judgment(tmp_path, findings=escaped), "--repo", repo, success=False
    )
    unchecked = [
        finding(
            "confirmed",
            evidence=[{"kind": "command", "ref": "uv run pytest -q tests/test_items.py", "note": "1 failed"}],
        )
    ]
    assert cli("--result", judgment(tmp_path, findings=unchecked), "--repo", repo)["counts"]["confirmed"] == 1


def test_unreadable_inputs_fail_cleanly(tmp_path):
    assert "cannot read judging result JSON" in cli("--result", tmp_path / "missing.json", success=False)
    broken = tmp_path / "broken.json"
    broken.write_text("{")
    assert "cannot read candidates JSON" in cli("--result", judgment(tmp_path), "--candidates", broken, success=False)


def test_schema_checker_and_reference_agree():
    schema = json.loads(SCHEMA.read_text())
    entry = schema["properties"]["findings"]["items"]
    evidence = entry["properties"]["evidence"]["items"]
    for node in (schema, schema["properties"]["input"], entry, evidence):
        assert node["additionalProperties"] is False
        assert node["required"] == list(node["properties"])
    assert list(judging.KEYS) == schema["required"]
    assert list(judging.INPUT_KEYS) == schema["properties"]["input"]["required"]
    assert list(judging.FINDING_KEYS) == entry["required"]
    assert list(judging.EVIDENCE_KEYS) == evidence["required"]
    assert list(judging.SEVERITIES) == entry["properties"]["severity"]["enum"]
    assert list(judging.DISPOSITIONS) == entry["properties"]["disposition"]["enum"]
    assert list(judging.KINDS) == evidence["properties"]["kind"]["enum"]
    assert set(judging.NEEDS) == set(judging.DISPOSITIONS) and len(set(judging.DISPOSITIONS)) == 4
    assert entry["properties"]["id"]["pattern"] == judging.ID.pattern
    assert schema["properties"]["input"]["properties"]["snapshotId"]["pattern"] == judging.SNAPSHOT.pattern
    for node in (schema["properties"]["summary"], entry["properties"]["scenario"], evidence["properties"]["note"]):
        assert node["maxLength"] == judging.MAX_CHARS
    reference = REFERENCE.read_text()
    for key in (*judging.KEYS, *judging.INPUT_KEYS, *judging.FINDING_KEYS, *judging.EVIDENCE_KEYS):
        assert f"`{key}`" in reference, key
    for word in (*judging.DISPOSITIONS, *judging.KINDS):
        assert f"`{word}`" in reference, word
    assert "\u2014" not in reference and "\u2013" not in reference, "prose for agents uses plain dashes"
    assert "judging.md" in (REPO / "skills/deep-review/references/hand-back.md").read_text()
