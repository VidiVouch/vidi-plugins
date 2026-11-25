"""--panel replaces the boss with three judges on one identical brief. Every judge receives the same frozen
snapshot, candidates, exclusions and rubric and nothing from any other judge; each result is checked against
the candidates it was given; the three records are merged by their evidence (test_evidence_merge.py holds the
merge rules and the fixer's rebuttals). Offline, mocked Workflow runtime; never calls a model."""

import importlib
import json
import struct
import subprocess
import sys
from unittest.mock import Mock

import pytest
from conftest import REPO
from test_deep_review import CLEAN, FINDING, FIX_PACKET, catalog, prepare, workflow
from test_run_gate import gate, measure, run  # noqa: F401  (the gate fixture and helpers run the real helper)

SKILL = REPO / "skills/deep-review"
SCRIPTS = SKILL / "scripts"
sys.path.insert(0, str(SCRIPTS))
manifest_api = importlib.import_module("run_manifest")
SNAPSHOT = "a" * 64
PANEL = ("judge-fable", "judge-astra", "judge-grok")
OTHER = {**FINDING, "file": "src/b.py", "description": "Second reachable defect"}
ONE = {"claude-bugs#1": {**CLEAN, "findings": [FINDING]}}


def fnv1a(text, seed):
    """The workflow's 32-bit FNV-1a over UTF-16 code units, so the test can name the ids the judges will see."""
    value = seed
    for (unit,) in struct.iter_unpack("<H", text.encode("utf-16-le")):
        value = ((value ^ unit) * 16777619) & 0xFFFFFFFF
    return f"{value:08x}"


def candidate_id(finding):
    key = f"{finding['file']}:{finding['line_start']}-{finding['line_end']}:{finding['description'][:80]}"
    return f"c-{fnv1a(key, 0x811C9DC5)}{fnv1a(key, 0x7EE3623A)}"


def candidate(finding, sources=("claude-bugs",)):
    """The candidate record the workflow hands every judge for one deduped finding."""
    return {
        "id": candidate_id(finding),
        "file": finding["file"],
        "line_start": finding["line_start"],
        "line_end": finding["line_end"],
        "severity": finding["severity"],
        "sources": list(sources),
        "description": finding["description"],
        "recommendation": "",
    }


CANDIDATE = candidate(FINDING)


def panel(**changes):
    """The evidence prepare_reviewers.py --panel reports when every judge is advertised, with per-judge overrides."""
    judges = {}
    for name in PANEL:
        model = prepare.agent_model(name)
        judges[name] = {
            "agentType": f"vidi-skills:{name}",
            "requestedModel": model,
            "available": True,
            "catalogModel": prepare.catalog_id(model),
            "reason": "fixture",
            "resolvedModel": None,
            "upstreamModel": None,
        }
    for name, change in changes.items():
        judges[name] = {**judges[name], **change}
    return judges


def entry(cand, disposition, severity=None, reason=None):
    record = {
        "id": cand["id"],
        "file": cand["file"],
        "line_start": cand["line_start"],
        "line_end": cand["line_end"],
        "severity": severity or cand["severity"],
        "disposition": disposition,
        "scenario": "an empty list reaches the loop and the first element is read",
        "evidence": [
            {"kind": "code", "ref": f"{cand['file']}:{cand['line_start']}", "note": "no guard before the read"}
        ],
        "impact": "wrong output for any caller passing an empty list",
        "reason": reason or f"{disposition} by fixture",
        "limits": [],
    }
    if disposition == "unresolved":
        record["limits"] = ["needs the runtime to see whether the caller can pass an empty list"]
    if disposition == "unreviewed":
        record.update(evidence=[], impact="", reason="", limits=["budget exhausted before this candidate"])
    return record


def judged(candidates=(CANDIDATE,), disposition="confirmed", round=1, snapshot=SNAPSHOT, reason=None, **overrides):
    """One judge's result over the candidates: `disposition` is one word for all or a map from id to word."""
    word = (lambda c: disposition) if isinstance(disposition, str) else (lambda c: disposition[c["id"]])
    result = {
        "input": {"snapshotId": snapshot, "round": round},
        "findings": [entry(c, word(c), reason=reason) for c in candidates],
        "limits": [],
        "summary": "fixture judgment",
    }
    result.update(overrides)
    return result


def unanimous(disposition, candidates=(CANDIDATE,), round=1):
    return {f"{name}#{round}": judged(candidates, disposition, round) for name in PANEL}


def brief(prompt):
    """The judge's brief without the per-dispatch identity line (run, snapshot, dispatch ordinal)."""
    identity, body = prompt.split("\n\n", 1)
    assert identity.startswith("Run fixture-run; starting snapshot") and "fresh instance" in identity
    return body


# ---- the brief ----------------------------------------------------------------


def test_panel_dispatches_three_judges_by_definition_on_one_identical_brief():
    """Three Judge units, each selecting its definition by agentType alone, all handed the same brief: the frozen
    snapshot, the candidates with content ids, the REVIEW.md exclusions, the rubric and the contract paths."""
    data = workflow({"judges": panel(), "focus": "the empty-list path"}, {**ONE, **unanimous("confirmed")})
    judges = [c for c in data["calls"] if c["opts"]["phase"] == "Judge"]
    assert [c["opts"]["label"] for c in judges] == [f"{name}#1" for name in PANEL]
    for call in judges:
        name = call["opts"]["label"].split("#")[0]
        assert set(call["opts"]) == {"label", "phase", "schema", "agentType"}, call["opts"]
        assert call["opts"]["agentType"] == f"vidi-skills:{name}"
    briefs = {brief(c["prompt"]) for c in judges}
    assert len(briefs) == 1, "every judge receives the same brief"
    text = briefs.pop()
    assert f"round 1, snapshot {SNAPSHOT}" in text and '"/fixture/repo with spaces"' in text
    assert "/fixture/repo with spaces/.claude/skills/deep-review/reviewers/../references/judge-contract.md" in text
    assert "references/judging.md" in text
    assert json.dumps([CANDIDATE], indent=2) in text, "the candidates, with their content-derived ids, verbatim"
    assert "Exclusions: /fixture/repo with spaces/REVIEW.md" in text and "skip section" in text
    assert "Rubric, for every candidate" in text and "Reviewer focus (from --focus): the empty-list path" in text
    assert "Do not confirm or refute by default" in text and "structured-output tool" in text
    assert f'"input": {{"snapshotId": "{SNAPSHOT}", "round": 1}}' in text
    # Identity lines differ only by dispatch ordinal; the dispatch record names no wrapper, these are native units.
    assert len({c["prompt"].split("\n\n", 1)[0] for c in judges}) == 3
    assert all("executor" not in d for d in data["result"]["admission"]["dispatches"] if d["phase"] == "Judge")
    assert any(line.endswith(f"judges: {', '.join(PANEL)} · report-only") for line in data["logs"])


def test_no_judge_sees_another_judge_or_the_boss_vocabulary():
    """Independence: the brief carries no other judge's result, reasoning or name, no boss ruling and no fixer summary,
    and the three judges are dispatched in one parallel step behind the review barrier."""
    responses = {
        **ONE,
        "judge-fable#1": judged(reason="fable traced the empty-list path at line 2"),
        "judge-astra#1": judged(reason="astra reproduced the read with []"),
        "judge-grok#1": judged(reason="grok found no guard anywhere in the module"),
    }
    data = workflow({"judges": panel(), "fix": True}, {**responses, "fix:bucket1/1#1": FIX_PACKET})
    judges = [c for c in data["calls"] if c["opts"]["phase"] == "Judge" and c["opts"]["label"].endswith("#1")]
    for call in judges:
        for other in PANEL:
            assert other not in call["prompt"], f"{call['opts']['label']} names {other}"
        for phrase in (
            "fable traced",
            "astra reproduced",
            "grok found",
            "rulings",
            "ARBITER",
            "boss",
            FIX_PACKET["summary"],
        ):
            assert phrase not in call["prompt"], phrase
        assert "no other judge's result or reasoning" in call["prompt"]
    barriers = [b["name"] for b in data["result"]["admission"]["barriers"]]
    assert barriers == ["review#1", "judge#1", "fix#1", "review#2", "judge#2", "conclude"]
    assert all(b["open"] == 0 for b in data["result"]["admission"]["barriers"])
    # Round 2 found nothing, so no judge was dispatched for it: two reviewers twice, three judges, one fixer.
    assert len(data["calls"]) == 8 and not data["result"]["incomplete"]


def test_judge_schema_matches_the_judging_output_contract():
    schema = json.loads((SKILL / "judging-output.schema.json").read_text())
    data = workflow({"judges": panel()}, {**ONE, **unanimous("confirmed")})
    runtime = next(c for c in data["calls"] if c["opts"]["phase"] == "Judge")["opts"]["schema"]
    finding, native = runtime["properties"]["findings"]["items"], schema["properties"]["findings"]["items"]
    evidence, native_evidence = finding["properties"]["evidence"]["items"], native["properties"]["evidence"]["items"]
    for ours, theirs in ((runtime, schema), (finding, native), (evidence, native_evidence)):
        assert ours["required"] == theirs["required"]
        assert ours["properties"].keys() == theirs["properties"].keys()
    assert runtime["properties"]["input"]["required"] == schema["properties"]["input"]["required"]
    for key in ("severity", "disposition"):
        assert finding["properties"][key]["enum"] == native["properties"][key]["enum"]
    assert evidence["properties"]["kind"]["enum"] == native_evidence["properties"]["kind"]["enum"]


# ---- candidate identity -------------------------------------------------------


def test_candidate_ids_come_from_content_not_position():
    """The same finding gets the same id whatever order the reviewers reported it in, in whichever round it appears,
    and the Python mirror of the derivation names it too."""
    both = [candidate(FINDING), candidate(OTHER)]
    forward = workflow(
        {"judges": panel()}, {"claude-bugs#1": {**CLEAN, "findings": [FINDING, OTHER]}, **unanimous("confirmed", both)}
    )
    backward = workflow(
        {"judges": panel()}, {"claude-bugs#1": {**CLEAN, "findings": [OTHER, FINDING]}, **unanimous("confirmed", both)}
    )
    seen = {
        tuple(sorted(c["id"] for c in data["result"]["judgeResults"][0]["candidates"])) for data in (forward, backward)
    }
    assert seen == {tuple(sorted(c["id"] for c in both))}
    assert forward["result"]["judgeResults"][0]["candidates"] == both
    assert [f["id"] for f in forward["result"]["currentFindings"]] == [c["id"] for c in both]
    # Re-detected after a fix: round 2 names the finding by the round 1 id.
    again = workflow(
        {"judges": panel(), "fix": True, "maxRounds": 2},
        {
            **ONE,
            "claude-bugs#2": {**CLEAN, "findings": [FINDING]},
            **unanimous("confirmed"),
            **unanimous("confirmed", round=2),
        },
    )["result"]
    assert [r["round"] for r in again["judgeResults"]] == [1, 2]
    assert (
        again["judgeResults"][0]["candidates"][0]["id"]
        == again["judgeResults"][1]["candidates"][0]["id"]
        == CANDIDATE["id"]
    )
    assert len(CANDIDATE["id"]) == 18 and CANDIDATE["id"].startswith("c-")


def test_boss_mode_is_unchanged_without_a_panel():
    """judges absent or null: the boss runs with positional ids and the result says so."""
    for args in ({}, {"judges": None}):
        data = workflow(args, ONE)
        assert [c["opts"]["label"] for c in data["calls"] if c["opts"]["phase"] == "Judge"] == ["boss:inherit#1"]
        assert (
            data["result"]["boss"] == ["inherit"]
            and data["result"]["judges"] == {}
            and data["result"]["judgeResults"] == []
        )
        assert data["result"]["currentFindings"][0]["id"] == 0


# ---- the provisional merge ----------------------------------------------------


def test_unanimous_confirmed_is_confirmed_and_fixed_with_the_judges_severity_and_reasons():
    responses = {
        **ONE,
        "judge-fable#1": judged(reason="traced"),
        "judge-astra#1": {
            **judged(reason="reproduced"),
            "findings": [entry(CANDIDATE, "confirmed", "critical", "reproduced")],
        },
        "judge-grok#1": judged(reason="no guard"),
        "fix:bucket1/1#1": FIX_PACKET,
    }
    data = workflow({"judges": panel(), "fix": True}, responses)
    result = data["result"]
    assert result["rounds"] == 2 and not result["incomplete"] and result["boss"] == []
    assert result["judges"] == {
        name: {
            "agentType": f"vidi-skills:{name}",
            "requestedModel": prepare.agent_model(name),
            "catalogModel": prepare.catalog_id(prepare.agent_model(name)),
            "resolvedModel": None,
            "upstreamModel": None,
        }
        for name in PANEL
    }
    (fixed,) = result["disposition"]["fixed"]
    assert fixed["severity"] == "critical", "a confirmed finding takes the highest severity the judges gave it"
    assert fixed["dispositions"] == {name: "confirmed" for name in PANEL}
    assert fixed["rounds"][0]["reason"] == "judge-fable: traced; judge-astra: reproduced; judge-grok: no guard"
    (record,) = result["judgeResults"]
    assert record["round"] == 1 and record["candidates"] == [CANDIDATE]
    assert record["results"] == {name: {"result": responses[f"{name}#1"], "error": ""} for name in PANEL}
    assert not any(d["key"].startswith("judge:") for d in result["degraded"])


def test_unanimous_refuted_is_a_refuted_claim_with_every_judges_reason():
    data = workflow({"judges": panel(), "fix": True}, {**ONE, **unanimous("refuted")})
    result = data["result"]
    assert not result["incomplete"] and not result["fixed"] and result["rounds"] == 1
    (refuted,) = result["disposition"]["refuted"]
    assert sum(len(v) for v in result["disposition"].values()) == 1
    assert refuted["outcomeReason"] == "round 1: " + "; ".join(f"{name}: refuted by fixture" for name in PANEL)
    assert refuted["severity"] == "warning" and result["confirmed"] == [] and result["currentFindings"] == []
    assert any("1 refuted" in line for line in data["logs"])


@pytest.mark.parametrize(
    "words, why",
    [
        (("confirmed", "refuted", "confirmed"), "evidence conflicts"),
        (("unresolved", "unresolved", "unresolved"), "no judge demonstrated or refuted it"),
    ],
)
def test_conflicting_or_absent_evidence_rules_nothing_and_stops_the_run(words, why):
    """A confirmation beside a refutation, or no verdict at all, leaves the candidate unruled: no fixer runs, the run
    is incomplete, and the record shows what each judge said. The merge rules live in test_evidence_merge.py."""
    responses = {**ONE, **{f"{name}#1": judged(disposition=word) for name, word in zip(PANEL, words, strict=True)}}
    data = workflow({"judges": panel(), "fix": True}, responses)
    result = data["result"]
    assert result["incomplete"] and not result["fixed"] and result["rounds"] == 1
    assert not any(c["opts"]["phase"] == "Fix" for c in data["calls"])
    (unruled,) = result["disposition"]["unruled"]
    assert unruled["dispositions"] == dict(zip(PANEL, words, strict=True))
    said = ", ".join(f"{name} {word}" for name, word in zip(PANEL, words, strict=True))
    assert unruled["outcomeReason"].startswith(f"round 1: UNRULED: {why} ({said})")
    degraded = {d["key"]: d["reason"] for d in result["degraded"]}
    assert degraded == {
        "judge:panel": "degraded: 1 candidate(s) with conflicting or no evidence; nothing is fixed or cleared until a reader resolves them"
    }
    assert all(result["judgeResults"][0]["results"][name]["error"] == "" for name in PANEL)
    assert len(result["confirmed"]) == 1  # fail-closed history, as for an unruled boss finding


@pytest.mark.parametrize(
    "bad, reason",
    [
        (None, "judge failed or returned no result"),
        ("__throw__:fixture error", "judge failed or returned no result"),
        ({"rulings": [{"id": 0, "real": True, "severity": "warning"}]}, "invalid judging result"),
        ({**judged(), "summary": ""}, "invalid judging result"),
        ({**judged(), "extra": 1}, "invalid judging result"),
        (judged(snapshot="b" * 64), f"judged snapshot {'b' * 64} round 1, not snapshot {SNAPSHOT} round 1"),
        (judged(round=2), "round 2, not"),
        (judged(findings=[]), f"candidates without a disposition: {CANDIDATE['id']}"),
        (
            judged(findings=[entry(CANDIDATE, "confirmed"), entry({**CANDIDATE, "id": "c-new"}, "confirmed")]),
            "unknown candidate c-new",
        ),
        (judged(findings=[entry(CANDIDATE, "confirmed"), entry(CANDIDATE, "refuted")]), "has two dispositions"),
        (
            judged(findings=[entry({**CANDIDATE, "line_end": 9}, "confirmed")]),
            "moved from src/a.py:2-3 to src/a.py:2-9",
        ),
        (judged(findings=[{**entry(CANDIDATE, "confirmed"), "evidence": []}]), "is confirmed but has no evidence"),
        (judged(findings=[{**entry(CANDIDATE, "refuted"), "scenario": " "}]), "is refuted but has no scenario"),
        (judged(findings=[{**entry(CANDIDATE, "unresolved"), "limits": []}]), "is unresolved but has no limits"),
        (
            judged(
                findings=[
                    {**entry(CANDIDATE, "unreviewed"), "evidence": [{"kind": "code", "ref": "src/a.py:2", "note": ""}]}
                ]
            ),
            "is unreviewed but cites evidence",
        ),
        (judged(findings=[{**entry(CANDIDATE, "confirmed"), "severity": "high"}]), "invalid judging result"),
        (judged(findings=[{**entry(CANDIDATE, "confirmed"), "disposition": "real"}]), "invalid judging result"),
        (
            judged(
                findings=[{**entry(CANDIDATE, "confirmed"), "evidence": [{"kind": "guess", "ref": "x", "note": ""}]}]
            ),
            "invalid judging result",
        ),
        (judged(findings=[{**entry(CANDIDATE, "confirmed"), "limits": [""]}]), "invalid judging result"),
    ],
)
def test_a_missing_or_unbacked_judge_result_is_no_result_and_leaves_the_round_unruled(bad, reason):
    """One judge fails its check: that judge is degraded with the cause, its raw packet is kept beside the error under
    `replaced`, one fresh instance fills the slot (test_judge_replacement.py; here the mock's default packet fails the
    same check), the candidate is unruled with `missing` for that judge, no fixer runs and the run is incomplete."""
    data = workflow({"judges": panel(), "fix": True}, {**ONE, **unanimous("confirmed"), "judge-astra#1": bad})
    result = data["result"]
    assert result["incomplete"] and not result["fixed"]
    assert not any(c["opts"]["phase"] == "Fix" for c in data["calls"])
    degraded = {d["key"]: d["reason"] for d in result["degraded"]}
    assert set(degraded) == {"judge:judge-astra"} and reason in degraded["judge:judge-astra"], degraded
    (unruled,) = result["disposition"]["unruled"]
    assert unruled["dispositions"] == {"judge-fable": "confirmed", "judge-astra": "missing", "judge-grok": "confirmed"}
    assert "judge-astra missing" in unruled["outcomeReason"]
    astra = result["judgeResults"][0]["replaced"]["judge-astra"]
    assert reason in astra["error"] and astra["result"] == (bad if isinstance(bad, dict) else None)
    assert result["judgeResults"][0]["results"]["judge-astra"]["error"] == "invalid judging result"
    assert result["judgeResults"][0]["results"]["judge-fable"] == {
        "result": unanimous("confirmed")["judge-fable#1"],
        "error": "",
    }


def test_a_judge_cancelled_by_admission_is_a_missing_result():
    data = workflow({"judges": panel(), "limits": {"maxAgents": 4}}, {**ONE, **unanimous("confirmed")})
    result = data["result"]
    assert len(data["calls"]) == 4 and result["incomplete"]
    assert result["judgeResults"][0]["results"]["judge-grok"] == {
        "result": None,
        "error": "judge failed or returned no result",
    }
    assert next(d for d in result["admission"]["dispatches"] if d["label"] == "judge-grok#1")["state"] == "cancelled"
    assert result["disposition"]["unruled"][0]["dispositions"]["judge-grok"] == "missing"


# ---- refusing to start ---------------------------------------------------------


@pytest.mark.parametrize(
    "judges, boss, message",
    [
        (panel(), ["codex"], "replaces the boss"),
        ({k: v for k, v in panel().items() if k != "judge-grok"}, [], "exactly judge-fable, judge-astra, judge-grok"),
        ({**panel(), "judge-sonnet": panel()["judge-fable"]}, [], "exactly judge-fable"),
        ([], [], "exactly judge-fable"),
        ({**panel(), "judge-grok": "available"}, [], "invalid judge evidence for judge-grok"),
        (
            panel(
                **{
                    "judge-grok": {
                        "available": False,
                        "reason": "Required model ID grok-4.7 is not advertised by the proxy",
                    }
                }
            ),
            [],
            "judge-grok is unavailable (Required model ID grok-4.7 is not advertised by the proxy)",
        ),
        (panel(**{"judge-fable": {"catalogModel": "claude-fable-5-1[1m]"}}), [], "catalog evidence for judge-fable"),
        (panel(**{"judge-fable": {"catalogModel": None}}), [], "catalog evidence for judge-fable"),
        (panel(**{"judge-astra": {"agentType": "vidi-skills:astra-reviewer"}}), [], "catalog evidence for judge-astra"),
        (panel(**{"judge-astra": {"requestedModel": ""}}), [], "catalog evidence for judge-astra"),
    ],
)
def test_a_panel_missing_a_judge_or_beside_a_boss_refuses_to_start(judges, boss, message):
    """A panel with fewer than three advertised judges, the wrong judges, a definition without catalog evidence, or a
    boss beside it is not the mode that was asked for; nothing is dispatched."""
    error = workflow({"judges": judges, "boss": boss}, ONE, succeeds=False)["error"]
    assert message in error, error


# ---- preparation evidence ----------------------------------------------------


def test_prepare_reports_the_three_judges_against_the_bare_catalog_ids(tmp_path, monkeypatch):
    opener = catalog(monkeypatch, {"data": [{"id": "claude-fable-5-1"}, {"id": "gpt-6.2-astra"}, {"id": "grok-4.7"}]})
    env = {"ANTHROPIC_BASE_URL": "http://127.0.0.1:8319", "ANTHROPIC_AUTH_TOKEN": "fixture-secret"}
    setup = prepare.prepare(tmp_path, probe_proxy=True, env=env, panel=True)
    opener.open.assert_called_once()
    assert "fixture-secret" not in json.dumps(setup)
    judges = setup["judges"]
    assert list(judges) == list(PANEL) == list(prepare.JUDGES)
    for name in PANEL:
        model = prepare.agent_model(name)
        assert judges[name] == {
            "agentType": f"vidi-skills:{name}",
            "requestedModel": model,
            "available": True,
            "catalogModel": model.removesuffix("[1m]"),
            "reason": "Catalog advertised model; execution unverified",
            "resolvedModel": None,
            "upstreamModel": None,
        }
    assert (
        judges["judge-fable"]["requestedModel"].endswith("[1m]") and judges["judge-grok"]["catalogModel"] == "grok-4.7"
    )
    # The panel evidence is what the workflow admits.
    assert workflow({"judges": judges}, {**ONE, **unanimous("confirmed")})["result"]["boss"] == []


def test_prepare_marks_an_unadvertised_judge_unavailable_and_the_workflow_refuses_that_panel(tmp_path, monkeypatch):
    catalog(monkeypatch, {"data": [{"id": "gpt-6.2-astra"}, {"id": "grok-4.7"}]})
    setup = prepare.prepare(
        tmp_path, probe_proxy=True, env={"ANTHROPIC_BASE_URL": "http://localhost:8319/v1"}, panel=True
    )
    fable = setup["judges"]["judge-fable"]
    assert not fable["available"] and fable["catalogModel"] is None
    assert fable["reason"] == "Required model ID claude-fable-5-1 is not advertised by the proxy"
    assert setup["judges"]["judge-astra"]["available"] and setup["judges"]["judge-grok"]["available"]
    error = workflow({"judges": setup["judges"]}, ONE, succeeds=False)["error"]
    assert "judge-fable is unavailable (Required model ID claude-fable-5-1 is not advertised" in error


def test_without_the_panel_request_judges_are_null_and_nothing_extra_is_probed(tmp_path, monkeypatch):
    probe = Mock(side_effect=AssertionError("must not probe"))
    monkeypatch.setattr(prepare, "proxy_models", probe)
    disabled = tmp_path / "reviewers.json"
    disabled.write_text(json.dumps({key: {"policy": "disabled"} for key in prepare.DIRECT}))
    plain = prepare.prepare(tmp_path, disabled, probe_proxy=True, env={})
    assert plain["judges"] is None and "judges" in plain
    probe.assert_not_called()
    # Requesting the panel is what makes the catalog worth asking, even with every direct lane disabled.
    monkeypatch.setattr(
        prepare, "proxy_models", lambda env: ({"claude-fable-5-1", "gpt-6.2-astra", "grok-4.7"}, "advertised")
    )
    with_panel = prepare.prepare(tmp_path, disabled, probe_proxy=True, env={}, panel=True)
    assert all(j["available"] for j in with_panel["judges"].values())
    assert plain["reviewerFingerprint"] != with_panel["reviewerFingerprint"]


def test_a_forced_subagent_model_blocks_the_judges_too(tmp_path, monkeypatch):
    probe = Mock(side_effect=AssertionError("must not probe"))
    monkeypatch.setattr(prepare, "proxy_models", probe)
    setup = prepare.prepare(tmp_path, probe_proxy=True, env={"CLAUDE_CODE_SUBAGENT_MODEL_FORCE": "yes"}, panel=True)
    probe.assert_not_called()
    assert setup["namedAgents"]["blocked"]
    for judge in setup["judges"].values():
        assert (
            not judge["available"]
            and judge["reason"] == "Forced subagent model conflicts with independent named reviewers"
        )


def test_cli_panel_flag_reports_judges_without_a_probe(tmp_path):
    result = subprocess.run(
        [sys.executable, str(SCRIPTS / "prepare_reviewers.py"), "--repo", str(tmp_path), "--panel"],
        capture_output=True,
        text=True,
        check=False,
        timeout=30,
        env={"PATH": ""},
    )
    assert result.returncode == 0, result.stderr
    judges = json.loads(result.stdout)["judges"]
    assert set(judges) == set(PANEL)
    assert all(not j["available"] and j["reason"] == "Proxy catalog not checked" for j in judges.values())
    plain = subprocess.run(
        [sys.executable, str(SCRIPTS / "prepare_reviewers.py"), "--repo", str(tmp_path)],
        capture_output=True,
        text=True,
        check=False,
        timeout=30,
        env={"PATH": ""},
    )
    assert json.loads(plain.stdout)["judges"] is None


# ---- the run record -------------------------------------------------------------


def test_manifest_records_the_requested_judges_and_no_boss():
    models = manifest_api.models({"judges": panel(), "reviewers": {}})
    assert models["boss"] == [] and models["judges"] == {
        name: {"agentType": f"vidi-skills:{name}", "requestedModel": prepare.agent_model(name)} for name in PANEL
    }
    assert manifest_api.models({"reviewers": {}}) == {"boss": ["inherit"], "judges": {}, "reviewers": {}}
    assert manifest_api.models({"judges": None, "boss": ["codex"]})["boss"] == ["codex"]


@pytest.mark.parametrize(
    "args, message",
    [
        ({"judges": panel(), "boss": ["codex"]}, "one or the other"),
        ({"judges": ["judge-fable"]}, "judges must map"),
        ({"judges": {"judge-fable": "ok"}}, "judges must map"),
    ],
)
def test_manifest_refuses_a_panel_beside_a_boss_or_malformed_judges(tmp_path, args, message):
    with pytest.raises(manifest_api.ManifestError, match=message):
        manifest_api.validate_args({"repoRoot": str(tmp_path), "mode": "uncommitted", **args}, tmp_path.resolve())


def test_finalize_binds_the_result_to_the_registered_panel(run):  # noqa: F811 (the imported gate fixture)
    """A panel request accepts only a result that names the same three judges and no boss; a boss result against it, a
    panel result without its judges, and a panel result against a boss request are all refused."""
    _, manifest, result_path = run(judges=panel())
    assert measure(manifest, result_path).returncode == 0
    recorded = json.loads(result_path.read_text())
    assert json.loads(manifest.read_text())["models"]["boss"] == []
    refused = gate(manifest, "finalize", result_path)
    assert refused.returncode == 2 and "Boss lanes disagree" in refused.stderr
    recorded["boss"] = []
    result_path.write_text(json.dumps(recorded))
    refused = gate(manifest, "finalize", result_path)
    assert refused.returncode == 2 and "Judging panel disagrees" in refused.stderr
    recorded["judges"] = {
        name: {
            "agentType": f"vidi-skills:{name}",
            "requestedModel": prepare.agent_model(name),
            "catalogModel": None,
            "resolvedModel": None,
            "upstreamModel": None,
        }
        for name in PANEL
    }
    result_path.write_text(json.dumps(recorded))
    accepted = gate(manifest, "finalize", result_path)
    assert accepted.returncode == 0, accepted.stderr
    assert json.loads(accepted.stdout)["accepted"] is True
    # The reverse: a boss request does not accept a result that claims a panel judged it.
    _, boss_manifest, boss_result = run()
    assert measure(boss_manifest, boss_result).returncode == 0
    data = json.loads(boss_result.read_text())
    data["judges"] = recorded["judges"]
    boss_result.write_text(json.dumps(data))
    refused = gate(boss_manifest, "finalize", boss_result)
    assert refused.returncode == 2 and "Judging panel disagrees" in refused.stderr


def test_status_helper_reads_a_degraded_judge_as_an_adjudication_failure():
    status = importlib.import_module("run_status")
    result = {
        "coverageComplete": True,
        "coverage": {"claude-bugs": {"policy": "required", "state": "succeeded", "reason": ""}},
        "degraded": [{"key": "judge:judge-astra", "reason": "degraded: judge failed or returned no result"}],
        "reviewResults": [{"key": "claude-bugs", "result": CLEAN}],
    }
    assert status.coverage(result)["boss"] == {
        "state": "failed",
        "reason": "degraded: judge failed or returned no result",
    }


# ---- the documents --------------------------------------------------------------


def test_skill_and_references_describe_the_opt_in_and_the_identical_brief():
    skill = (SKILL / "SKILL.md").read_text()
    assert "`--panel`" in skill and "judges: REVIEWER_SETUP.judges" in skill and "--probe-proxy --panel" in skill
    assert "references/judging.md" in skill and "references/judge-contract.md" in skill
    assert "judgeResults" in skill and "judging.py" in skill
    contract = (SKILL / "references/judge-contract.md").read_text()
    assert "Until REVIEW-2 lands" not in contract and "`--panel`" in contract and "`judges`" in contract
    judging = (SKILL / "references/judging.md").read_text()
    assert "`--panel`" in judging and "FNV-1a" in judging and "`judgeResults`" in judging
    disposition = (SKILL / "references/disposition.md").read_text()
    assert "`--panel`" in disposition and "evidence conflicts" in disposition
    assert "`judges`" in (SKILL / "references/reviewers.md").read_text()
    for path in (
        SKILL / "references/judge-contract.md",
        SKILL / "references/judging.md",
        SKILL / "references/disposition.md",
        REPO / "tests/test_judging_panel.py",
    ):
        assert not {"\u2014", "\u2013"} & set(path.read_text()), path
    source = (SKILL / "deep-review.workflow.js").read_text()
    assert "phase: 'Judge', schema: JUDGE_OUT, agentType: `vidi-skills:${key}`" in source
    assert "const prompt = judgePrompt(candidates, roundNo)" in source, "one brief built once for every judge"
