"""Named definitions are selected by agentType with no per-call model override, and a session setting
that would override every definition (CLAUDE_CODE_SUBAGENT_MODEL_FORCE, CLAUDE_CODE_FORK_SUBAGENT)
stops the run before any dispatch instead of being applied silently. Offline; never calls a model."""

from unittest.mock import Mock

import pytest
from conftest import REPO
from test_deep_review import CLEAN, FINDING, SKILL, direct, prepare, workflow

CALLERS = ("deep-review", "epoch-review", "rebuild-examine")


@pytest.fixture(autouse=True)
def no_live_capabilities(monkeypatch):
    monkeypatch.setattr(prepare.shutil, "which", lambda name: None)
    monkeypatch.setattr(
        prepare.urllib.request, "build_opener", Mock(side_effect=AssertionError("live network prohibited"))
    )


@pytest.mark.parametrize(
    ("env", "named"),
    [
        ({}, ""),
        ({"CLAUDE_CODE_SUBAGENT_MODEL": "haiku"}, ""),
        ({"CLAUDE_CODE_SUBAGENT_MODEL_FORCE": "0"}, ""),
        ({"CLAUDE_CODE_SUBAGENT_MODEL_FORCE": "1"}, "CLAUDE_CODE_SUBAGENT_MODEL_FORCE"),
        ({"CLAUDE_CODE_SUBAGENT_MODEL_FORCE": "true"}, "CLAUDE_CODE_SUBAGENT_MODEL_FORCE"),
        ({"CLAUDE_CODE_FORK_SUBAGENT": "1"}, "CLAUDE_CODE_FORK_SUBAGENT"),
    ],
)
def test_prepare_reports_a_setting_that_overrides_definitions(tmp_path, env, named):
    """CLAUDE_CODE_SUBAGENT_MODEL alone is a default below the definition; only the force and fork switches block."""
    result = prepare.prepare(tmp_path, env=env)["namedAgents"]
    assert result["blocked"] is bool(named)
    assert named in result["reason"] and (result["reason"] == "") is not bool(named)


def test_both_switches_are_named_in_one_reason(tmp_path):
    env = {"CLAUDE_CODE_SUBAGENT_MODEL_FORCE": "1", "CLAUDE_CODE_FORK_SUBAGENT": "1"}
    reason = prepare.prepare(tmp_path, env=env)["namedAgents"]["reason"]
    assert "CLAUDE_CODE_SUBAGENT_MODEL_FORCE" in reason and "CLAUDE_CODE_FORK_SUBAGENT" in reason


def test_workflow_refuses_to_start_while_definitions_are_overridden(tmp_path):
    setup = prepare.prepare(tmp_path, env={"CLAUDE_CODE_SUBAGENT_MODEL_FORCE": "1"})
    data = workflow({"reviewers": setup["reviewers"], "namedAgents": setup["namedAgents"]}, succeeds=False)
    assert "would be overridden" in data["error"] and "CLAUDE_CODE_SUBAGENT_MODEL_FORCE" in data["error"]


def test_workflow_runs_when_nothing_overrides_definitions(tmp_path):
    setup = prepare.prepare(tmp_path, env={})
    assert setup["namedAgents"] == {"blocked": False, "reason": ""}
    result = workflow({"reviewers": setup["reviewers"], "namedAgents": setup["namedAgents"]})["result"]
    assert result["attempted"] == ["claude-bugs", "claude-conventions"] and not result["incomplete"]


@pytest.mark.parametrize("bad", [None, [], "forced", {"blocked": "yes", "reason": ""}, {"blocked": True}])
def test_malformed_named_agent_evidence_is_refused(bad):
    assert "invalid named-agent evidence" in workflow({"namedAgents": bad}, succeeds=False)["error"]


def test_typed_dispatches_select_the_definition_and_omit_the_model_override():
    data = workflow({"reviewers": {"grok": direct()}, "fix": True}, {"grok#1": {**CLEAN, "findings": [FINDING]}})
    typed = [c for c in data["calls"] if "agentType" in c["opts"]]
    assert {c["opts"]["agentType"] for c in typed} == {"fixer", direct()["agentType"]}
    for call in typed:
        assert "model" not in call["opts"], call["opts"]
    assert data["result"]["fixed"]


def test_a_typed_dispatch_with_a_model_override_stops_the_run(tmp_path):
    """A future edit that adds a per-call alias to a definition dispatch cannot run it on the alias."""
    source = (SKILL / "deep-review.workflow.js").read_text()
    marker = "phase: 'Fix', schema: FIX_OUT, agentType: 'fixer'"
    assert source.count(marker) == 1
    patched = tmp_path / "patched.workflow.js"
    patched.write_text(source.replace(marker, marker + ", model: 'opus'"))
    data = workflow(
        {"reviewers": {"grok": direct()}, "fix": True}, {"grok#1": {**CLEAN, "findings": [FINDING]}}, path=patched
    )
    result = data["result"]
    assert result["incomplete"]
    assert (
        "per-call model override" in result["admission"]["stopReason"] and "fixer" in result["admission"]["stopReason"]
    )
    assert not any(c["opts"].get("agentType") == "fixer" for c in data["calls"])
    assert any(d["label"].startswith("fix:") and d["state"] == "cancelled" for d in result["admission"]["dispatches"])
    # `fixed` stays true by design (a dispatched fixer may have edited); the record says the fixer never returned.
    assert any(d["key"] == "fix#1" and "fixer failed" in d["reason"] for d in result["degraded"])


@pytest.mark.parametrize("name", CALLERS)
def test_every_caller_passes_named_agent_evidence_through(name):
    assert "namedAgents: REVIEWER_SETUP.namedAgents" in (REPO / "skills" / name / "SKILL.md").read_text()
