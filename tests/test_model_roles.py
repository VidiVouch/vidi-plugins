"""Role model IDs against a mocked catalog. Never calls a proxy or a model."""

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
roles = importlib.import_module("model_roles")
prepare = importlib.import_module("prepare_reviewers")
REQUESTED = {
    "judge-fable",
    "judge-astra",
    "judge-grok",
    "grok-reviewer",
    "implementer-opus",
    "fixer-opus",
    "permission-classifier",
}
CATALOG = ["claude-fable-5-1", "gpt-6-astra", "grok-4.7", "claude-opus-5-5", "claude-sonnet-5", "gpt-6.1-sol"]


@pytest.fixture(autouse=True)
def no_live_network(monkeypatch):
    monkeypatch.setattr(
        prepare.urllib.request, "build_opener", Mock(side_effect=AssertionError("live network prohibited"))
    )


def catalog(monkeypatch, ids):
    opener = Mock()
    opener.open.return_value = io.BytesIO(json.dumps({"data": [{"id": i} for i in ids]}).encode())
    monkeypatch.setattr(prepare.urllib.request, "build_opener", Mock(return_value=opener))
    return opener


def test_roles_file_names_every_requested_role_with_a_full_id():
    data = json.loads(roles.ROLES.read_text())
    assert REQUESTED <= set(data)
    for role in data.values():
        assert role["model"] and role["provider"] and role["use"]
        assert "/" not in role["model"] and role["model"] not in ("opus", "sonnet", "haiku", "fable")


def test_agent_linked_roles_agree_with_packaged_definitions():
    report = roles.check()["roles"]
    linked = {name for name, role in json.loads(roles.ROLES.read_text()).items() if "agent" in role}
    assert {"grok-reviewer", "astra-reviewer", "astra-adversarial-reviewer"} <= linked
    assert all(report[name]["agentAgrees"] for name in linked)


@pytest.mark.parametrize(
    ("model", "catalog_id", "extended", "unverified"),
    [
        ("claude-fable-5-1[1m]", "claude-fable-5-1", True, False),
        ("grok-4.7", "grok-4.7", False, False),
        ("gpt-6-astra-fast", "gpt-6-astra-fast", False, False),
        ("gpt-6-astra[1m]-fast", "gpt-6-astra[1m]-fast", False, True),
    ],
)
def test_only_a_trailing_context_suffix_is_stripped(model, catalog_id, extended, unverified):
    item = roles.split_model(model)
    assert item["catalogId"] == catalog_id
    assert item["extendedContext"] is extended
    assert bool(item["unverifiedSuffix"]) is unverified


def test_every_requested_role_is_advertised_by_bare_id(monkeypatch):
    catalog(monkeypatch, CATALOG)
    report = roles.check(probe_proxy=True, env={"ANTHROPIC_BASE_URL": "http://127.0.0.1:8317"})
    assert report["catalog"]["checked"]
    for name in REQUESTED:
        item = report["roles"][name]
        assert item["advertised"] and item["catalogModel"] == item["catalogId"]
        assert item["resolvedModel"] is None and item["upstreamModel"] is None


def test_hyphen_spelling_is_reported_not_substituted(monkeypatch):
    catalog(monkeypatch, CATALOG)
    item = roles.check_model("gpt-6-1-sol[1m]", set(CATALOG), "advertised")
    assert item["advertised"] is False and item["catalogModel"] is None
    assert item["nearMatches"] == ["gpt-6.1-sol"]
    assert "gpt-6-1-sol" in item["reason"]


def test_speed_suffix_is_checked_literally_and_never_assumed(monkeypatch):
    for model in ("gpt-6-astra-fast", "gpt-6-astra[1m]-fast", "gpt-6-astra-fast[1m]"):
        item = roles.check_model(model, set(CATALOG), "advertised")
        assert item["advertised"] is False and item["nearMatches"] == []


def test_settings_check_reads_model_keys_only_and_keeps_secrets_out(tmp_path, monkeypatch):
    settings = tmp_path / "team.settings.json"
    settings.write_text(
        json.dumps(
            {
                "env": {
                    "ANTHROPIC_BASE_URL": "http://127.0.0.1:8319",
                    "ANTHROPIC_AUTH_TOKEN": "fixture-secret",
                    "ANTHROPIC_DEFAULT_SONNET_MODEL": "gpt-6-1-sol[1m]",
                    "CLAUDE_CODE_AUTO_MODE_MODEL": "claude-opus-5-5",
                },
                "modelPicker": {"options": [{"model": "gpt-6.1-sol[1m]", "label": "sol"}]},
            }
        )
    )
    opener = catalog(monkeypatch, CATALOG)
    report = roles.check(settings=[settings], probe_proxy=True, env=json.loads(settings.read_text())["env"])
    assert opener.open.call_args.args[0].get_header("Authorization") == "Bearer fixture-secret"
    assert "fixture-secret" not in json.dumps(report)
    checked = report["settings"][str(settings)]
    assert set(checked) == {"env:ANTHROPIC_DEFAULT_SONNET_MODEL", "env:CLAUDE_CODE_AUTO_MODE_MODEL", "picker:sol"}
    assert checked["env:ANTHROPIC_DEFAULT_SONNET_MODEL"]["nearMatches"] == ["gpt-6.1-sol"]
    assert checked["picker:sol"]["advertised"]


def test_without_probe_nothing_is_claimed_and_no_request_is_made():
    report = roles.check()
    assert not report["catalog"]["checked"]
    assert all(item["advertised"] is None and item["catalogModel"] is None for item in report["roles"].values())


def test_cli_emits_json_without_network(tmp_path):
    result = subprocess.run(
        [sys.executable, str(SCRIPTS / "model_roles.py")], capture_output=True, text=True, check=False, timeout=30
    )
    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout)["catalog"]["checked"] is False
