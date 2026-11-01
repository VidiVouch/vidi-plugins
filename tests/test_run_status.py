"""One run's execution, coverage, acceptance and recovery are read apart from durable files; nothing here dispatches."""

import hashlib
import json
import re
import subprocess
import sys
from pathlib import Path

from conftest import REPO
from test_reuse_result import review_result
from test_run_attempt import case as attempt_case
from test_run_attempt import manifest_of, result_path, retry, script, write_result
from test_work_ledger import TASKS, write

SKILL = REPO / "skills/deep-review"
SCRIPTS = SKILL / "scripts"
RENDER = REPO / "skills/html-report/scripts/render_note.py"
FINDING = {"file": "file.py", "line_start": 1, "line_end": 1, "severity": "warning", "description": "fixture"}
# test_run_attempt's repository, manifest and finalize fixture, registered here under this name.
review_run = attempt_case
del attempt_case


def status(manifest, *flags):
    return script("run_status.py", "status", "--manifest", manifest, *flags)


def read(manifest, *flags):
    produced = status(manifest, *flags)
    assert produced.stderr == "", produced.stderr
    return json.loads(produced.stdout)


def test_the_four_answers_are_separate_and_only_acceptance_json_accepts(review_run):
    repo, init, finalize, _ = review_run
    # Nothing returned: execution is still running, nothing is covered, nothing is accepted, resume is the move.
    fresh = init()
    record = read(fresh)
    assert status(fresh).returncode == 1
    assert record["execution"]["state"] == "running" and record["execution"]["units"] == []
    assert record["coverage"] == {
        "complete": False,
        "rounds": None,
        "payloadsStored": None,
        "reviewers": {},
        "attempts": [],
        "boss": None,
    }
    assert record["acceptance"]["status"] == "pending-external-verification"
    assert record["acceptance"]["gate"] == {"state": "missing", "reason": "no result"}
    assert record["recovery"]["category"] == "missing" and not record["recovery"]["terminal"]
    assert record["recovery"]["blocking"]["stage"] == "result"
    assert f'run_manifest.py" resume --manifest "{fresh}"' in record["recovery"]["blocking"]["command"]
    assert sorted(record["recovery"]["preserved"]) == ["argsFile", "manifestPath"]
    assert sorted(record["sources"]) == ["argsFile", "manifestPath"]
    assert record["history"] == [
        {
            "attempt": 1,
            "runId": record["runId"],
            "priorRunId": None,
            "cause": None,
            "outcome": "missing",
            "current": True,
            "runDir": str(fresh.parent),
        }
    ]
    # A zero-agent return that calls itself accepted and complete: execution skipped, acceptance still pending.
    empty = init()
    write_result(
        empty,
        acceptance="accepted",
        incomplete=False,
        admission={**review_result({})["admission"], "started": 0, "dispatches": []},
    )
    record = read(empty)
    assert record["execution"]["state"] == "skipped" and record["execution"]["started"] == 0
    assert record["acceptance"]["status"] == "pending-external-verification"
    assert record["acceptance"]["resultClaims"] == "accepted" and record["acceptance"]["decidedBy"] is None
    assert record["recovery"]["category"] == "launch-blocked"
    assert record["recovery"]["blocking"]["stage"] == "dispatch"
    assert 'run_manifest.py" init' in record["recovery"]["blocking"]["command"]
    # A complete result: execution succeeded, coverage complete with its payload stored, acceptance still pending
    # until finalize writes the decision, and the blocking gate is exactly that command.
    done = init()
    write_result(done)
    record = read(done)
    assert status(done).returncode == 0
    assert record["execution"]["state"] == "succeeded"
    assert record["execution"]["counts"] == {
        "queued": 0,
        "running": 0,
        "succeeded": 1,
        "skipped": 0,
        "failed": 0,
        "cancelled": 0,
    }
    assert record["execution"]["units"] == [
        {"label": "claude-bugs#1", "phase": "Review", "state": "succeeded", "reason": None}
    ]
    assert record["coverage"]["complete"] and record["coverage"]["payloadsStored"]
    assert record["coverage"]["reviewers"] == {
        "claude-bugs": {"policy": "required", "state": "succeeded", "reason": "", "payloadStored": True}
    }
    assert record["coverage"]["boss"] == {"state": "succeeded", "reason": ""}
    assert record["acceptance"]["status"] == "pending-external-verification"
    assert record["acceptance"]["gate"] == {"state": "passed", "reason": ""}
    assert record["recovery"]["category"] == "complete"
    assert record["recovery"]["blocking"]["stage"] == "finalize"
    assert (
        f'run_gate.py" finalize --manifest "{done}" --result "{result_path(done)}"'
        in record["recovery"]["blocking"]["command"]
    )
    assert finalize(done).returncode == 0
    record = read(done)
    assert record["acceptance"] == {
        "status": "accepted",
        "accepted": True,
        "decidedBy": str(done.parent / "acceptance.json"),
        "gate": {"state": "passed", "reason": ""},
        "resultClaims": "pending-external-verification",
        "openFindings": 0,
    }
    assert record["recovery"]["category"] == "accepted" and record["recovery"]["blocking"] is None
    assert record["history"][0]["outcome"] == "accepted"
    assert sorted(record["sources"]) == ["acceptancePath", "argsFile", "gatePath", "manifestPath", "resultPath"]
    # A ruling against the tree is a decision with open findings, not a block.
    ruled = init()
    write_result(ruled, currentFindings=[{**FINDING, "sources": ["claude-bugs"]}])
    assert json.loads(finalize(ruled).stdout)["status"] == "changes-required"
    record = read(ruled)
    assert record["acceptance"]["status"] == "changes-required" and not record["acceptance"]["accepted"]
    assert record["acceptance"]["openFindings"] == 1 and record["recovery"]["blocking"] is None
    assert record["obligations"]["findings"] == ["warning file.py:1 fixture"]


def test_each_interruption_names_its_blocking_gate(review_run):
    repo, init, _, _ = review_run
    base = review_result({})["admission"]
    stopped = {
        **base,
        "stopReason": "Required reviewer claude-bugs failed",
        "stop": {"reason": "Required reviewer claude-bugs failed", "dispatched": 1, "running": 0, "queued": 1},
        "dispatches": [
            {"label": "claude-bugs#1", "phase": "Review", "state": "failed", "reason": "degraded: quota exhausted"},
            {
                "label": "boss:inherit#1",
                "phase": "Adjudicate",
                "state": "cancelled",
                "reason": "Required reviewer claude-bugs failed",
            },
        ],
    }
    failed_lane = {"claude-bugs": {"policy": "required", "state": "failed", "reason": "quota exhausted"}}
    shapes = {
        "quota": (
            {
                "incomplete": True,
                "coverageComplete": False,
                "admission": stopped,
                "coverage": failed_lane,
                "degraded": [{"key": "claude-bugs", "reason": "degraded: quota exhausted"}],
                "attempts": [{"key": "claude-bugs", "round": 1, "state": "failed", "reason": "quota exhausted"}],
                "reviewResults": [],
            },
            {
                "execution": "cancelled",
                "category": "transient",
                "stage": "coverage",
                "reason": "degraded: quota exhausted",
            },
        ),
        "gate": (
            {
                "incomplete": True,
                "gate": {"state": "failed", "reason": 'gate did not pass (["true"] failed exit 4)', "report": None},
                "degraded": [{"key": "gate", "reason": 'degraded: gate did not pass (["true"] failed exit 4)'}],
            },
            {
                "execution": "succeeded",
                "category": "blocked",
                "stage": "gate",
                "reason": 'gate did not pass (["true"] failed exit 4)',
            },
        ),
        "boss": (
            {
                "incomplete": True,
                "degraded": [{"key": "boss:inherit", "reason": "degraded: missing or invalid boss rulings"}],
            },
            {
                "execution": "succeeded",
                "category": "partial",
                "stage": "adjudication",
                "reason": "degraded: missing or invalid boss rulings",
            },
        ),
        "active": (
            {
                "admission": {
                    **base,
                    "dispatches": [{"label": "claude-bugs#1", "phase": "Review", "state": "running", "reason": ""}],
                    "barriers": [{"name": "conclude", "dispatched": 1, "open": 1}],
                }
            },
            {
                "execution": "running",
                "category": "partial",
                "stage": "execution",
                "reason": "a dispatch was still active when the result was written",
            },
        ),
        "modified": (
            {"fixed": True, "requiresPostFixReview": True},
            {"execution": "succeeded", "category": "modified", "stage": "post-fix review", "reason": None},
        ),
        "malformed": (
            {"coverage": {}},
            {"execution": "succeeded", "category": "malformed", "stage": "result", "reason": None},
        ),
    }
    records = {}
    for name, (patch, expected) in shapes.items():
        manifest = init()
        write_result(manifest, **patch)
        record = records[name] = read(manifest)
        assert status(manifest).returncode == 1, name
        assert record["execution"]["state"] == expected["execution"], (name, record["execution"])
        assert record["recovery"]["category"] == expected["category"], (name, record["recovery"])
        gate = record["recovery"]["blocking"]
        assert gate["stage"] == expected["stage"], (name, gate)
        if expected["reason"]:
            assert gate["reason"] == expected["reason"], (name, gate)
        assert record["acceptance"]["status"] == "pending-external-verification", name
        if record["recovery"]["retryable"]:
            assert f'run_attempt.py" retry --manifest "{manifest}"' in gate["command"], name
        elif name == "active":
            assert "--abandon" in gate["command"]
        else:
            assert 'run_manifest.py" init' in gate["command"], name
    # The quota run: dispatch, reviewer and attempt records all keep the one vocabulary.
    quota = records["quota"]
    assert quota["execution"]["counts"]["failed"] == 1 and quota["execution"]["counts"]["cancelled"] == 1
    assert quota["execution"]["stopReason"] == "Required reviewer claude-bugs failed"
    assert quota["execution"]["stop"]["queued"] == 1
    assert quota["coverage"]["reviewers"]["claude-bugs"] == {
        "policy": "required",
        "state": "failed",
        "reason": "quota exhausted",
        "payloadStored": None,
    }
    assert quota["coverage"]["payloadsStored"] is True and not quota["coverage"]["complete"]
    assert quota["coverage"]["attempts"] == [
        {"key": "claude-bugs", "round": 1, "state": "failed", "reason": "quota exhausted"}
    ]
    assert quota["recovery"]["preserved"].keys() == {"manifestPath", "argsFile", "resultPath"}
    # A succeeded lane whose full payload is missing is a result summarised before it was saved.
    truncated = init()
    write_result(truncated, reviewResults=[])
    record = read(truncated)
    assert record["coverage"]["reviewers"]["claude-bugs"]["payloadStored"] is False
    assert record["coverage"]["payloadsStored"] is False
    assert record["recovery"]["category"] == "malformed"
    # Unreadable bytes: a return that proves nothing; the tree moving on: stage inputs.
    broken = init()
    result_path(broken).write_text("{not json")
    record = read(broken)
    assert record["execution"]["state"] == "failed" and record["recovery"]["blocking"]["stage"] == "result"
    stale = init()
    write_result(stale)
    (repo / "file.py").write_text("value = 2\n")
    record = read(stale)
    assert record["recovery"]["category"] == "stale" and record["recovery"]["blocking"]["stage"] == "inputs"
    assert "drift" in record["recovery"]["blocking"]["reason"]


def test_history_follows_the_attempt_series_from_any_member(review_run):
    _, init, _, _ = review_run
    first = init("--task-id", "FLOW-5", "--attempt", "1")
    write_result(first, incomplete=True, degraded=[{"key": "claude-bugs", "reason": "degraded: HTTP 429 rate limit"}])
    second = Path(json.loads(retry(first).stdout)["manifestPath"])
    stranger = init()
    for member in (first, second):
        record = read(member)
        assert [h["attempt"] for h in record["history"]] == [1, 2]
        assert [h["outcome"] for h in record["history"]] == ["replaced", "missing"]
        assert [h["current"] for h in record["history"]] == [member == first, member == second]
        assert record["history"][1]["priorRunId"] == manifest_of(first)["runId"]
        assert record["history"][1]["cause"] == "transient: degraded: HTTP 429 rate limit"
        assert manifest_of(stranger)["runId"] not in {h["runId"] for h in record["history"]}
        assert record["unit"] == {"taskId": "FLOW-5", "attempt": 1}
    assert read(first)["recovery"]["replacedBy"] == [manifest_of(second)["runId"]]
    alone = read(stranger)["history"]
    assert len(alone) == 1 and alone[0]["outcome"] == "missing" and alone[0]["priorRunId"] is None


def test_the_ledger_unit_is_projected_and_a_closed_unit_ends_execution(review_run, tmp_path):
    _, init, _, _ = review_run
    ledger = tmp_path / "ledger.json"
    ledger_cli = [sys.executable, str(SCRIPTS / "work_ledger.py")]
    tasks = write(tmp_path / "tasks.json", {"tasks": TASKS})

    def ledger_run(*args):
        done = subprocess.run([*ledger_cli, *map(str, args)], capture_output=True, text=True, timeout=30)
        assert done.returncode == 0, done.stderr
        return json.loads(done.stdout)

    ledger_run("init", "--ledger", ledger, "--run-id", "run-1", "--tasks", tasks)
    ledger_run(
        "start", "--ledger", ledger, "--task", "A-1", "--attempt", "1", "--role", "implementer", "--owner", "lead"
    )
    manifest = init("--task-id", "A-1", "--attempt", "1")
    record = read(manifest, "--ledger", ledger)
    owed = record["obligations"]["ledger"]
    assert owed["task"] == "A-1" and owed["attempt"] == 1 and owed["state"] == "running" and not owed["terminal"]
    assert owed["units"] == [
        {"role": "implementer", "started": True, "returned": False, "disposition": None, "barrier": None}
    ]
    assert owed["openUnits"] == ["A-1 attempt 1 implementer"]
    assert owed["openObligations"] == [] and owed["gatedActions"] == [] and owed["unresolved"] == []
    assert record["execution"]["state"] == "running"
    # The barrier closed the unit without a result: the ledger is the only record of how execution ended.
    ledger_run(
        "close",
        "--ledger",
        ledger,
        "--task",
        "A-1",
        "--attempt",
        "1",
        "--role",
        "implementer",
        "--outcome",
        "cancellation",
        "--owner",
        "lead",
    )
    record = read(manifest, "--ledger", ledger)
    assert record["execution"]["state"] == "cancelled"
    assert record["obligations"]["ledger"]["state"] == "cancelled"
    assert record["obligations"]["ledger"]["units"][0]["barrier"] == "cancellation"
    assert record["obligations"]["ledger"]["openUnits"] == []
    assert record["recovery"]["category"] == "missing" and "--abandon" in record["recovery"]["next"]
    # A manifest with no unit cannot be joined to a ledger; an unknown ledger task is refused the same way.
    unbound = init()
    refused = status(unbound, "--ledger", ledger)
    assert refused.returncode == 2 and "names no ledger unit" in refused.stderr
    other = init("--task-id", "Z-9", "--attempt", "1")
    refused = status(other, "--ledger", ledger)
    assert refused.returncode == 2 and "unknown task Z-9" in refused.stderr


def test_status_json_is_a_replaceable_projection_that_names_its_sources(review_run):
    _, init, finalize, _ = review_run
    manifest = init()
    written = manifest.parent / "status.json"
    first = read(manifest, "--write")
    stored = json.loads(written.read_text())
    assert {k: v for k, v in stored.items() if k != "generatedAt"} == {
        k: v for k, v in first.items() if k != "generatedAt"
    }
    write_result(manifest)
    assert finalize(manifest).returncode == 0
    again = read(manifest, "--write")
    stored = json.loads(written.read_text())
    assert stored["acceptance"]["status"] == "accepted" and stored["generatedAt"] == again["generatedAt"]
    for key, source in stored["sources"].items():
        path = Path(source["path"])
        assert path == Path(manifest_of(manifest)["outputs"][key])
        assert source["sha256"] == hashlib.sha256(path.read_bytes()).hexdigest()
    assert "status.json" not in {Path(s["path"]).name for s in stored["sources"].values()}
    # The projection never disturbs the evidence: classify and the run directory's other files are unchanged.
    assert json.loads(script("run_attempt.py", "classify", "--manifest", manifest).stdout)["category"] == "accepted"
    assert not list(manifest.parent.glob(".status.json.*"))


def test_the_note_renders_through_the_existing_report_surface(review_run, tmp_path):
    _, init, _, _ = review_run
    manifest = init()
    write_result(
        manifest,
        incomplete=True,
        gate={"state": "failed", "reason": 'gate did not pass (["true"] failed exit 4)', "report": None},
        degraded=[{"key": "gate", "reason": 'degraded: gate did not pass (["true"] failed exit 4)'}],
    )
    produced = status(manifest, "--format", "note")
    note = json.loads(produced.stdout)
    assert set(note) == {"meta", "blocks"} and note["meta"]["flavor"] == "utilitarian"
    assert [b["id"] for b in note["blocks"]] == [
        "state",
        "blocking",
        "dispatches",
        "reviewers",
        "history",
        "obligations",
        "sources",
    ]
    path = tmp_path / "note.json"
    path.write_text(produced.stdout)
    markdown = subprocess.run(
        [sys.executable, str(RENDER), str(path), "--format", "md"], capture_output=True, text=True, timeout=30
    )
    assert markdown.returncode == 0, markdown.stderr
    run_id = manifest_of(manifest)["runId"]
    for phrase in (
        f"# Review run {run_id} attempt 1",
        "| execution | succeeded |",
        "| coverage | complete |",
        "| acceptance | pending-external-verification |",
        "| recovery | blocked |",
        "Stage `gate`: gate did not pass",
        'run_manifest.py" init',
        "| claude-bugs#1 | Review | succeeded |",
        "| claude-bugs | required | succeeded | True |",
        f"| 1 | {run_id} (this run) | returned |",
        "manifestPath",
    ):
        assert phrase in markdown.stdout, phrase
    page = subprocess.run([sys.executable, str(RENDER), str(path)], capture_output=True, text=True, timeout=30)
    assert page.returncode == 0 and "<title>" in page.stdout and run_id in page.stdout
    assert "unknown block" not in page.stdout


def test_helper_reads_artifacts_and_never_wakes_an_agent():
    source = (SCRIPTS / "run_status.py").read_text()
    imports = set(re.findall(r"^(?:from (\S+) import|import (\S+))", source, flags=re.M))
    modules = {a or b for a, b in imports}
    assert modules == {
        "argparse",
        "hashlib",
        "json",
        "os",
        "sys",
        "tempfile",
        "datetime",
        "pathlib",
        "run_attempt",
        "run_manifest",
        "work_ledger",
    }
    for word in ("resumeFromRunId", "resume(", "agent(", "subprocess", "requests", "urllib"):
        assert word not in source, word
    # It never writes acceptance, a result, a manifest or the ledger: the one file it may write is status.json.
    assert source.count("replace_json(") == 2 and "status.json" in source
    for forbidden in ("write_exclusive", "write_json", "save(", "finalize("):
        assert forbidden not in source, forbidden


def test_references_name_the_status_helper_and_keep_the_vocabulary():
    meaning = (SKILL / "references/result-meaning.md").read_text()
    for phrase in (
        'run_status.py" status',
        "--ledger",
        "--write",
        "--format note",
        "status.json",
        "queued",
        "running",
        "succeeded",
        "skipped",
        "failed",
        "cancelled",
        "`execution`",
        "`coverage`",
        "`acceptance`",
        "`recovery`",
        "`history`",
        "`obligations`",
        "`sources`",
        "blocking",
        "render_note.py",
    ):
        assert phrase in meaning, phrase
    assert "run_status.py" in (SKILL / "references/run-lifecycle.md").read_text()
    assert "run_status.py" in (SKILL / "SKILL.md").read_text()
    for path in (SCRIPTS / "run_status.py", SKILL / "references/result-meaning.md", Path(__file__)):
        assert not {"\u2014", "\u2013"} & set(path.read_text()), path
