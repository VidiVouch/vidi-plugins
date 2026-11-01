"""Without --panel one boss adjudicates, exactly as before. The two-model form ([logicBoss, tasteBoss]), which asked
each boss to label a finding's domain, took one boss's voice by that label and ignored a third entry, is retired: a
list of more than one boss is refused at the start by the workflow and by run_manifest.py, so it can never be read
as a three-member consensus; several judges over the same candidates is --panel. Offline, mocked Workflow runtime;
never calls a model."""

import importlib
import sys

import pytest
from conftest import REPO
from test_deep_review import CLEAN, FINDING, workflow
from test_judging_panel import CANDIDATE, PANEL, entry, judged, panel, unanimous

SKILL = REPO / "skills/deep-review"
sys.path.insert(0, str(SKILL / "scripts"))
manifest_api = importlib.import_module("run_manifest")
ONE = {"claude-bugs#1": {**CLEAN, "findings": [FINDING]}}
RULING = {"rulings": [{"id": 0, "real": True, "severity": "critical", "reason": "traced"}], "summary": "no-ship"}
LONG = "x" * 501


# ---- one boss, as before ------------------------------------------------------


@pytest.mark.parametrize("boss, label", [(None, "boss:inherit#1"), ([], "boss:inherit#1"), (["opus"], "boss:opus#1")])
def test_one_boss_adjudicates_the_round_and_the_record_names_it(boss, label):
    """The single-boss path is unchanged: one Judge unit, positional ids, rulings laid onto the findings, the boss
    named in the result, no judges and no judgeResults. The ruling carries no domain label any more."""
    args = {"fix": False} if boss is None else {"fix": False, "boss": boss}
    data = workflow(args, {**ONE, label: RULING})
    (call,) = [c for c in data["calls"] if c["opts"]["phase"] == "Judge"]
    assert call["opts"]["label"] == label
    assert ("model" in call["opts"]) == bool(boss) and call["opts"].get("model") == (boss[0] if boss else None)
    assert "domain" not in call["prompt"] and "taste" not in call["prompt"]
    result = data["result"]
    assert result["boss"] == (boss or ["inherit"]) and result["judges"] == {} and result["judgeResults"] == []
    assert not result["incomplete"]
    (finding,) = result["currentFindings"]
    assert finding["id"] == 0 and finding["ruling"] == "confirmed" and finding["severity"] == "critical"
    assert finding["bossReason"] == "traced" and "domain" not in finding
    assert any(line.endswith("boss: " + (boss[0] if boss else "inherit") + " · report-only") for line in data["logs"])


# ---- refusing a list of bosses -------------------------------------------------


@pytest.mark.parametrize("boss", [["logic", "taste"], ["codex", "opus"], ["codex", "opus", "fable"]])
def test_more_than_one_boss_refuses_to_start(boss):
    """Two or three bosses are not a consensus and not a panel: the run refuses before any unit is dispatched and
    names the list, the retired form and the mode that replaces it."""
    error = workflow({"boss": boss}, ONE, succeeds=False)["error"]
    assert "--boss takes one model, got " + ", ".join(boss) in error, error
    assert "logic,taste merge is retired" in error and "--panel" in error


@pytest.mark.parametrize("boss", ["codex", [1], {"model": "codex"}])
def test_boss_must_be_a_list_of_model_ids(boss):
    assert "boss must be a list of model ids" in workflow({"boss": boss}, ONE, succeeds=False)["error"]


@pytest.mark.parametrize("boss", [["logic", "taste"], ["codex", "opus", "fable"]])
def test_manifest_refuses_more_than_one_boss(tmp_path, boss):
    """run_manifest.py refuses the same list at initialization, so a two-model request never reaches the workflow."""
    args = {"repoRoot": str(tmp_path), "mode": "uncommitted"}
    with pytest.raises(manifest_api.ManifestError, match="boss takes one model"):
        manifest_api.validate_args({**args, "boss": boss}, tmp_path.resolve())
    for one in ([], ["codex"]):
        manifest_api.validate_args({**args, "boss": one}, tmp_path.resolve())
    assert manifest_api.models({"boss": ["codex"], "reviewers": {}})["boss"] == ["codex"]


def test_the_two_slot_merge_is_gone_from_the_source():
    """No code path reads a second boss or a domain label: the only multi-judge merge left is the panel's."""
    source = (SKILL / "deep-review.workflow.js").read_text()
    assert "const BOSS = " in source and "BOSS.length > 1" in source
    for token in ("byBoss", "bosses.length", "rulings.find", "enum: ['logic', 'taste']", "domain:", "'domain'"):
        assert token not in source, token


# ---- the in-run judge check mirrors judging.py's tree-independent rules --------------


@pytest.mark.parametrize(
    "bad, reason",
    [
        ({**judged(), "summary": LONG}, "invalid judging result"),
        ({**judged(), "limits": [LONG]}, "invalid judging result"),
        (judged(findings=[{**entry(CANDIDATE, "confirmed"), "scenario": LONG}]), "invalid judging result"),
        (judged(findings=[{**entry(CANDIDATE, "confirmed"), "limits": [LONG]}]), "invalid judging result"),
        (
            judged(
                findings=[{**entry(CANDIDATE, "confirmed"), "evidence": [{"kind": "code", "ref": "x:1", "note": LONG}]}]
            ),
            "invalid judging result",
        ),
        *[
            (
                judged(
                    findings=[{**entry(CANDIDATE, "confirmed"), "evidence": [{"kind": "code", "ref": ref, "note": ""}]}]
                ),
                f"cites code {ref!r}; a code ref is <repo-relative path>:<line> or <path>:<start>-<end>".replace(
                    "'", '"'
                ),
            )
            for ref in ("src/a.py", "src/a.py:0", "src/a.py:5-3", "/abs/a.py:2", "../a.py:2", "src/a.py:2:3")
        ],
    ],
)
def test_a_judge_result_the_host_check_would_refuse_is_refused_in_the_run(bad, reason):
    """judging.py refuses prose over 500 characters and a code reference that is not path:line or path:start-end.
    The in-run check refuses the same result, so it cannot rule and dispatch a fixer before the host rejects it."""
    data = workflow({"judges": panel(), "fix": True}, {**ONE, **unanimous("confirmed"), "judge-astra#1": bad})
    result = data["result"]
    assert result["incomplete"] and not any(c["opts"]["phase"] == "Fix" for c in data["calls"])
    # The refused first instance is on record under `replaced`; the fresh instance that filled the slot (the
    # mock's default review packet here) is the slot's standing result, and it fails the same check.
    assert reason in result["judgeResults"][0]["replaced"]["judge-astra"]["error"]
    assert result["judgeResults"][0]["results"]["judge-astra"]["error"] == "invalid judging result"
    assert result["disposition"]["unruled"][0]["dispositions"]["judge-astra"] == "missing"


@pytest.mark.parametrize("ref", ["src/a.py:2", "src/a.py:2-3", "deep/dir/a.py:10-10"])
def test_a_well_formed_code_reference_passes(ref):
    narrow = judged(
        findings=[{**entry(CANDIDATE, "confirmed"), "evidence": [{"kind": "code", "ref": ref, "note": "n"}]}]
    )
    result = workflow({"judges": panel()}, {**ONE, **unanimous("confirmed"), "judge-astra#1": narrow})["result"]
    assert not result["incomplete"] and result["judgeResults"][0]["results"]["judge-astra"]["error"] == ""
    assert result["disposition"]["open"][0]["dispositions"] == dict.fromkeys(PANEL, "confirmed")


# ---- the documents -------------------------------------------------------------


def test_documents_retire_the_two_model_form_and_keep_the_single_boss():
    skill = (SKILL / "SKILL.md").read_text()
    assert "[ --boss <model> ]" in skill and "model[,model2]" not in skill
    assert "is retired" in skill and "`run_manifest.py` refuse" in skill and "one entry at most" in skill
    for other in ("rebuild-examine", "epoch-review"):
        text = (REPO / "skills" / other / "SKILL.md").read_text()
        assert "[ --boss <model> ]" in text and "model[,model2]" not in text and "split on ','" not in text
    contract = (SKILL / "references/judge-contract.md").read_text()
    assert "retired and refused at the start" in contract and "consensus of three" in contract
    assert "(REVIEW-3, REVIEW-4)" not in contract
    assert "a request naming more\nthan one boss is refused" in (SKILL / "references/judging.md").read_text()
    for path in (
        SKILL / "references/judge-contract.md",
        SKILL / "references/judging.md",
        REPO / "tests/test_boss_compat.py",
    ):
        assert not {"\u2014", "\u2013"} & set(path.read_text()), path
