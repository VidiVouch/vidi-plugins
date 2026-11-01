"""Classifier routing check against fixtures. Never launches Claude Code, a proxy, or a model."""

import importlib
import io
import json
import subprocess
import sys
from unittest.mock import Mock

import pytest
from conftest import REPO

SCRIPTS = REPO / "skills/deep-review/scripts"
sys.path.insert(0, str(SCRIPTS))
check = importlib.import_module("classifier_check")
prepare = importlib.import_module("prepare_reviewers")
INTENDED = check.intended_model()
GOOD_ENV = {
    "CLAUDE_CODE_AUTO_MODE_MODEL": INTENDED,
    "CLAUDE_CODE_AUTO_MODE_SERVER": "0",
    "ANTHROPIC_BASE_URL": "http://127.0.0.1:8319",
    "ANTHROPIC_AUTH_TOKEN": "secret-token-value",
}
STARTED = "2026-11-01T20:00:00Z [DEBUG] classifier_request_started model={model}, actions=1"


@pytest.fixture(autouse=True)
def no_live_calls(monkeypatch):
    monkeypatch.setattr(
        prepare.urllib.request, "build_opener", Mock(side_effect=AssertionError("live network prohibited"))
    )
    monkeypatch.setattr(check, "installed_claude_version", Mock(side_effect=AssertionError("claude must not run")))


def settings(tmp_path, env):
    path = tmp_path / "team.settings.json"
    path.write_text(json.dumps({"env": env}))
    return path


def test_intended_model_is_the_full_sonnet_id_from_the_roles_file():
    assert INTENDED == "claude-sonnet-5"


def test_plan_is_ready_when_model_server_and_version_match(tmp_path):
    report = check.plan(settings(tmp_path, GOOD_ENV), claude_version="2.1.316 (Claude Code)")
    assert report["ready"] and report["reasons"] == []
    assert report["catalog"]["advertised"] is None
    assert "--permission-mode auto" in report["launch"]["print"] and "--debug-file" in report["launch"]["print"]


@pytest.mark.parametrize(
    "change, fragment",
    [
        ({"CLAUDE_CODE_AUTO_MODE_MODEL": None}, "unset"),
        ({"CLAUDE_CODE_AUTO_MODE_MODEL": "claude-opus-5-5"}, "not the intended"),
        ({"CLAUDE_CODE_AUTO_MODE_SERVER": None}, "asks the server first"),
    ],
)
def test_plan_names_each_setting_that_would_not_request_the_intended_model(tmp_path, change, fragment):
    env = {k: v for k, v in {**GOOD_ENV, **change}.items() if v is not None}
    report = check.plan(settings(tmp_path, env), claude_version="2.1.316")
    assert not report["ready"] and any(fragment in r for r in report["reasons"])


def test_plan_rejects_a_claude_code_older_than_the_override(tmp_path):
    report = check.plan(settings(tmp_path, GOOD_ENV), claude_version="2.1.301")
    assert not report["ready"] and "older than 2.1.302" in report["reasons"][0]
    assert not check.plan(settings(tmp_path, GOOD_ENV), claude_version="")["ready"]


def test_plan_checks_the_catalog_only_when_probed(tmp_path, monkeypatch):
    opener = Mock()
    opener.open.return_value = io.BytesIO(json.dumps({"data": [{"id": "claude-opus-5-5"}]}).encode())
    monkeypatch.setattr(prepare.urllib.request, "build_opener", Mock(return_value=opener))
    report = check.plan(settings(tmp_path, GOOD_ENV), claude_version="2.1.316", probe_proxy=True)
    assert report["catalog"]["advertised"] is False and not report["ready"]
    assert opener.open.call_count == 1 and "secret-token-value" not in json.dumps(report)


def test_launch_without_a_request_is_not_a_test(tmp_path):
    log = tmp_path / "debug.log"
    log.write_text("session started\nBash read-only command allowed without review\n")
    report = check.evidence(log, INTENDED)
    assert report["state"] == "none" and report["requests"] == 0


def test_another_model_or_an_ignored_override_is_a_mismatch(tmp_path):
    log = tmp_path / "debug.log"
    log.write_text(STARTED.format(model="claude-opus-5-5") + "\n")
    report = check.evidence(log, INTENDED)
    assert report["state"] == "mismatch" and "claude-opus-5-5" in report["reasons"][0]
    log.write_text(STARTED.format(model=INTENDED) + "\nignoring CLAUDE_CODE_AUTO_MODE_MODEL: org policy\n")
    assert check.evidence(log, INTENDED)["state"] == "mismatch"


def test_failed_requests_are_denials_not_service(tmp_path):
    log = tmp_path / "debug.log"
    log.write_text(STARTED.format(model=INTENDED) + "\nauto mode cannot determine the safety of this action\n")
    assert check.evidence(log, INTENDED)["state"] == "failed"


def test_matching_request_is_served_only_with_proxy_evidence(tmp_path):
    log = tmp_path / "debug.log"
    log.write_text(STARTED.format(model=INTENDED) + "\n" + STARTED.format(model=INTENDED) + "\n")
    report = check.evidence(log, INTENDED)
    assert report["state"] == "requested" and report["requests"] == 2
    proxy = tmp_path / "proxy"
    proxy.mkdir()
    (proxy / "a.log").write_text(
        "=== REQUEST BODY ===\n"
        + json.dumps({"model": INTENDED, "messages": []})
        + '\n=== API RESPONSE ===\nevent: message_start\ndata: {"id": "msg_1", "model": "'
        + INTENDED
        + '"}\n'
    )
    (proxy / "b.log").write_text('=== REQUEST BODY ===\n{"model": "gpt-6-astra"}\n=== API RESPONSE ===\n')
    report = check.evidence(log, INTENDED, proxy)
    assert report["state"] == "served" and report["proxyRequests"] == 1 and report["proxyServed"] == 1


def test_cli_plans_without_network_and_prints_no_credential(tmp_path):
    path = settings(tmp_path, GOOD_ENV)
    out = subprocess.run(
        [
            sys.executable,
            str(SCRIPTS / "classifier_check.py"),
            "plan",
            "--settings",
            str(path),
            "--claude-version",
            "2.1.316",
        ],
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )
    assert out.returncode == 0, out.stderr
    assert json.loads(out.stdout)["ready"] and "secret-token-value" not in out.stdout
