"""Reviewer lanes run by explicit policy (disabled, optional, required), never by CLI presence.

The shipped defaults disable every subscription-backed and external lane with a portable
reason; a user's own account state belongs in the per-checkout override. These cases pin
the parts of that contract the per-helper tests leave open: a disabled lane stays
undispatched whatever a caller claims about its binary, a required CLI lane that is missing
or fails makes the run incomplete and keeps its attempt record, an installed CLI is
availability and nothing more, the Codex lanes never stand in for an unavailable Astra
lane, and the external specs no longer tell a reader to run on installation alone.
"""

import json
from unittest.mock import Mock

import pytest
from test_deep_review import CLEAN, FINDING, SKILL, configure, prepare, workflow

DEFAULTS = json.loads((SKILL / "reviewer-defaults.json").read_text())
SUBSCRIPTION = ("cubic", "coderabbit")
EXTERNAL = (*SUBSCRIPTION, "codex-native", "codex-adversarial")
# Every direct lane off, so coverage reads only what the case under test contributes.
NO_DIRECT = {key: {"policy": "disabled", "available": False} for key in prepare.DIRECT}
ALL_TOOLS = {
    "cubic": True,
    "coderabbit": True,
    "codex": True,
    "codexAdversarial": True,
    "codexCompanion": "/plugins/codex/scripts/codex-companion.mjs",
}


@pytest.fixture(autouse=True)
def no_live_capabilities(monkeypatch):
    monkeypatch.setattr(prepare.shutil, "which", lambda name: None)
    monkeypatch.setattr(
        prepare.urllib.request, "build_opener", Mock(side_effect=AssertionError("live network prohibited"))
    )


def review_labels(data):
    return [c["opts"]["label"] for c in data["calls"] if c["opts"]["phase"] == "Review"]


def test_portable_defaults_disable_external_lanes_without_billing_facts():
    for key in EXTERNAL:
        assert DEFAULTS[key]["policy"] == "disabled"
        assert "opt-in" in DEFAULTS[key]["reason"]
    assert DEFAULTS["cubic"]["reason"] == DEFAULTS["coderabbit"]["reason"]
    text = (SKILL / "reviewer-defaults.json").read_text().lower()
    for word in ("expired", "renew", "billing", "account", "cancel", "paid"):
        assert word not in text


def test_defaults_report_disabled_lanes_before_any_probe(tmp_path, monkeypatch):
    probe = Mock(return_value="/usr/local/bin/tool")
    monkeypatch.setattr(prepare.shutil, "which", probe)
    result = prepare.prepare(tmp_path, env={})
    probe.assert_not_called()
    for key in EXTERNAL:
        assert result["reviewers"][key] == {**DEFAULTS[key], "available": False}
    assert result["tools"] == {}


def test_a_disabled_override_with_a_personal_reason_is_reported_not_probed(tmp_path, monkeypatch):
    probe = Mock(return_value="/usr/local/bin/cubic")
    monkeypatch.setattr(prepare.shutil, "which", probe)
    overrides = {key: {"policy": "disabled", "reason": "Subscription expired"} for key in SUBSCRIPTION}
    result = prepare.prepare(tmp_path, configure(tmp_path, overrides), env={})
    probe.assert_not_called()
    for key in SUBSCRIPTION:
        assert result["reviewers"][key] == {"policy": "disabled", "reason": "Subscription expired", "available": False}


@pytest.mark.parametrize("key", EXTERNAL)
def test_a_disabled_lane_is_not_dispatched_even_when_the_caller_claims_its_binary(key):
    reviewers = {**NO_DIRECT, key: {"policy": "disabled", "available": True, "reason": "Subscription expired"}}
    data = workflow({"reviewers": reviewers, "tools": ALL_TOOLS})
    result = data["result"]
    assert review_labels(data) == ["claude-bugs#1", "claude-conventions#1"]
    assert result["coverage"][key]["state"] == "disabled"
    assert result["coverage"][key]["reason"] == "Subscription expired"
    assert {"key": key, "policy": "disabled", "reason": "Subscription expired"} in result["skipped"]
    assert key not in result["attempted"] and key not in result["successful"]
    assert all(a["key"] != key for a in result["attempts"])
    # Disabled is a recorded skip, not a coverage gap, and it never degrades the run.
    assert result["coverageComplete"] and not result["incomplete"]
    assert all(d["key"] != key for d in result["degraded"])


def test_enabled_cli_availability_is_not_review_success_or_identity(tmp_path, monkeypatch):
    monkeypatch.setattr(prepare.shutil, "which", lambda name: f"/usr/local/bin/{name}")
    config = configure(tmp_path, {key: {"policy": "optional"} for key in SUBSCRIPTION})
    prepared = prepare.prepare(tmp_path, config, env={})
    for key in SUBSCRIPTION:
        item = prepared["reviewers"][key]
        assert (
            item["available"] and item["reason"] == "CLI installed; authentication, subscription and quota unverified"
        )
        assert not {"catalogModel", "resolvedModel", "upstreamModel"} & set(item)
    assert prepared["tools"] == {"cubic": True, "coderabbit": True}

    reviewers = {**NO_DIRECT, **{key: prepared["reviewers"][key] for key in SUBSCRIPTION}}
    responses = {"cubic#1": CLEAN, "coderabbit#1": {"findings": [], "degraded": True, "reason": "Subscription expired"}}
    result = workflow({"reviewers": reviewers, "tools": prepared["tools"]}, responses)["result"]
    assert result["coverage"]["cubic"]["state"] == "succeeded" and "cubic" in result["successful"]
    assert result["coverage"]["coderabbit"]["state"] == "failed" and "coderabbit" not in result["successful"]
    assert set(result["attempted"]) >= set(SUBSCRIPTION)
    for key in SUBSCRIPTION:
        c = result["coverage"][key]
        assert c["catalogModel"] is None and c["resolvedModel"] is None and c["upstreamModel"] is None
    assert not result["incomplete"] and not result["coverageComplete"]
    assert {"key": "coderabbit", "reason": "degraded: Subscription expired"} in result["degraded"]


def test_a_required_cli_lane_that_is_missing_blocks_every_dispatch():
    reviewers = {**NO_DIRECT, "cubic": {"policy": "required", "available": False, "reason": "cubic CLI missing"}}
    data = workflow({"reviewers": reviewers})
    result = data["result"]
    assert not data["calls"]
    assert result["incomplete"] and not result["successful"]
    assert result["coverage"]["cubic"]["state"] == "unavailable"
    assert {"key": "cubic", "reason": "degraded: cubic CLI missing"} in result["degraded"]
    assert result["termination"] == {
        "state": "review-failed",
        "round": 0,
        "reason": "cubic: degraded: cubic CLI missing",
    }
    for key in ("claude-bugs", "claude-conventions"):
        assert {"key": key, "policy": "required", "reason": "Not launched: required preflight failed"} in result[
            "skipped"
        ]


def test_a_required_cli_lane_that_fails_makes_the_run_incomplete_and_keeps_the_attempt():
    reviewers = {**NO_DIRECT, "cubic": {"policy": "required", "available": True}}
    responses = {
        "cubic#1": {"findings": [], "degraded": True, "reason": "Subscription expired"},
        "claude-bugs#1": {**CLEAN, "findings": [FINDING]},
    }
    data = workflow({"reviewers": reviewers, "tools": {"cubic": True}, "fix": True}, responses)
    result = data["result"]
    assert result["incomplete"] and not result["coverageComplete"]
    assert result["coverage"]["cubic"] == {
        **result["coverage"]["cubic"],
        "policy": "required",
        "state": "failed",
        "reason": "Subscription expired",
    }
    assert {"key": "cubic", "round": 1, "state": "failed", "reason": "Subscription expired"} in result["attempts"]
    assert "cubic" in result["attempted"] and "cubic" not in result["successful"]
    assert result["termination"]["state"] == "stopped"
    assert result["admission"]["stopReason"] == "Required reviewer cubic failed"
    assert not any(c["opts"]["phase"] == "Fix" for c in data["calls"])
    assert result["acceptance"] == "pending-external-verification"


def test_an_optional_cli_lane_keeps_its_failed_attempt_after_a_later_success():
    reviewers = {**NO_DIRECT, "cubic": {"policy": "optional", "available": True}}
    responses = {
        "claude-bugs#1": {**CLEAN, "findings": [FINDING]},
        # A non-terminal failure: a quota or rate-limit reason would stop queued starts instead (terminalFailure).
        "cubic#1": {"findings": [], "degraded": True, "reason": "help text printed instead of JSON"},
    }
    data = workflow({"reviewers": reviewers, "tools": {"cubic": True}, "fix": True}, responses)
    result = data["result"]
    attempts = [a for a in result["attempts"] if a["key"] == "cubic"]
    assert [(a["round"], a["state"], a["reason"]) for a in attempts] == [
        (1, "failed", "help text printed instead of JSON"),
        (2, "succeeded", ""),
    ]
    assert "cubic" in result["successful"]
    assert result["termination"]["state"] == "converged"


def test_codex_lanes_never_replace_an_unavailable_astra_lane(tmp_path, monkeypatch):
    probe = Mock(return_value="/usr/local/bin/codex")
    monkeypatch.setattr(prepare.shutil, "which", probe)
    prepared = prepare.prepare(tmp_path, env={})  # no catalog probe: every direct lane is unavailable
    assert not prepared["reviewers"]["astra-standard"]["available"]
    assert not prepared["reviewers"]["astra-adversarial"]["available"]
    probe.assert_not_called()
    for key in ("codex-native", "codex-adversarial"):
        assert prepared["reviewers"][key]["policy"] == "disabled" and not prepared["reviewers"][key]["available"]
    assert "codexCompanion" not in prepared["tools"]

    # The helper's own output goes to the workflow, as a live caller passes it, with every CLI claimed present.
    data = workflow({"reviewers": prepared["reviewers"], "tools": ALL_TOOLS})
    result = data["result"]
    assert review_labels(data) == ["claude-bugs#1", "claude-conventions#1"]
    for key in ("codex-native", "codex-adversarial"):
        assert result["coverage"][key]["state"] == "disabled"
        assert {"key": key, "policy": "disabled", "reason": DEFAULTS[key]["reason"]} in result["skipped"]
    assert result["coverage"]["astra-standard"]["state"] == "unavailable"
    assert not result["coverageComplete"] and not result["incomplete"]


def test_a_required_codex_lane_is_an_explicit_request_not_a_fallback(tmp_path, monkeypatch):
    monkeypatch.setattr(prepare.shutil, "which", lambda name: f"/usr/local/bin/{name}" if name == "codex" else None)
    overrides = {"codex-native": {"policy": "required"}, "astra-standard": {"policy": "disabled"}}
    prepared = prepare.prepare(tmp_path, configure(tmp_path, overrides), env={})
    assert prepared["reviewers"]["codex-native"]["policy"] == "required"
    assert prepared["reviewers"]["codex-native"]["available"]
    assert (
        prepared["reviewers"]["codex-native"]["reason"]
        == "CLI installed; authentication, subscription and quota unverified"
    )
    assert prepared["reviewers"]["astra-standard"] == {
        "policy": "disabled",
        "reason": "Disabled by reviewer policy",
        "available": False,
    }
    assert prepared["tools"] == {"codex": True}


@pytest.mark.parametrize("name", EXTERNAL)
def test_external_specs_run_on_policy_not_installation(name):
    text = (SKILL / "reviewers" / f"{name}.md").read_text()
    head = text.split("## Launch prompt")[0]
    assert "Skip entirely if" not in text and "=no)" not in text
    assert "explicitly enables" in head
    assert "does not prove authentication" in head
