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
    assert (REQUESTED - {"permission-classifier"}) | {"astra-reviewer", "astra-adversarial-reviewer"} <= linked
    assert all(report[name]["agentAgrees"] for name in linked)


JUDGES = ("judge-fable", "judge-astra", "judge-grok")
READ_ONLY_TOOLS = (
    "tools: Read, Grep, Glob, Bash(git -C * diff *), Bash(git -C * show *), "
    "Bash(git -C * ls-files *), Bash(git -C * log *), Bash(git -C * rev-parse *)"
)


def definition(name):
    _, frontmatter, body = (REPO / "agents" / f"{name}.md").read_text().split("---", 2)
    return frontmatter, body


def test_judge_definitions_pin_full_ids_and_share_one_body():
    data = json.loads(roles.ROLES.read_text())
    bodies = set()
    for name in JUDGES:
        frontmatter, body = definition(name)
        assert f"name: {name}\n" in frontmatter and READ_ONLY_TOOLS in frontmatter
        assert "Write" not in frontmatter and "Edit" not in frontmatter
        assert data[name]["agent"] == name
        assert prepare.agent_model(name) == data[name]["model"]
        bodies.add(body)
    assert len(bodies) == 1, "the three judges must be the same brief on different models"
    shared = bodies.pop()
    assert "references/judge-contract.md" in shared and "judging-output.schema.json" in shared
    assert "fresh instance" in shared and "no prior conversation" in shared and "`unreviewed`" in shared
    assert "Do not describe which model you are" in shared


def test_judge_contract_names_the_roles_and_the_dispatch_rule():
    contract = (REPO / "skills/deep-review/references/judge-contract.md").read_text()
    data = json.loads(roles.ROLES.read_text())
    for name in JUDGES:
        assert f"| {name} | `agents/{name}.md` | {data[name]['model']} |" in contract
    assert "`agentType`" in contract and "passes no\n`model`" in contract
    assert "CLAUDE_CODE_SUBAGENT_MODEL_FORCE" in contract and "namedAgents.blocked" in contract
    # Fork mode only lets Claude start forks on its own; the contract must not claim it overrides a definition.
    prose = " ".join(contract.split())
    assert "CLAUDE_CODE_FORK_SUBAGENT`, on by default since 2.1.302) does not override a definition" in prose
    assert "forks the controller" not in prose
    assert "judging-output.schema.json" in contract and "grok-reviewer discovers" in contract
    assert "\u2014" not in contract and "\u2013" not in contract


def test_implementer_and_fixer_pin_the_opus_id_not_the_alias():
    data = json.loads(roles.ROLES.read_text())
    for role, agent in (("implementer-opus", "implementer"), ("fixer-opus", "fixer")):
        assert data[role]["agent"] == agent
        assert prepare.agent_model(agent) == data[role]["model"] == "claude-opus-5-5[1m]"
        _, body = definition(agent)
        assert "fresh instance" in body and "no prior conversation" in body
    assert "hand-back.schema.json" in definition("implementer")[1]


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


def test_dispatch_model_is_read_from_the_definition_and_refused_under_force():
    # A run record (a rebuild's registry row) takes the requested model from the definition
    # itself; when the force switch is on the definition is ignored, so no value is given.
    data = json.loads(roles.ROLES.read_text())
    assert roles.dispatch_model("implementer", env={}) == data["implementer-opus"]["model"]
    assert roles.dispatch_model("fixer", env={"CLAUDE_CODE_SUBAGENT_MODEL_FORCE": "off"}) == data["fixer-opus"]["model"]
    with pytest.raises(RuntimeError, match="CLAUDE_CODE_SUBAGENT_MODEL_FORCE"):
        roles.dispatch_model("implementer", env={"CLAUDE_CODE_SUBAGENT_MODEL_FORCE": "on"})
    with pytest.raises(OSError):
        roles.dispatch_model("no-such-agent", env={})


@pytest.mark.parametrize(
    ("force", "code", "stdout"),
    [("", 0, "claude-opus-5-5[1m]\n"), ("0", 0, "claude-opus-5-5[1m]\n"), ("1", 1, ""), ("Yes", 1, "")],
)
def test_cli_dispatch_prints_only_the_requested_model_or_refuses(force, code, stdout):
    env = {"PATH": "/usr/bin:/bin", "CLAUDE_CODE_SUBAGENT_MODEL_FORCE": force}
    result = subprocess.run(
        [sys.executable, str(SCRIPTS / "model_roles.py"), "--dispatch", "implementer"],
        capture_output=True,
        text=True,
        check=False,
        timeout=30,
        env=env,
    )
    assert (result.returncode, result.stdout) == (code, stdout), result.stderr
    assert ("CLAUDE_CODE_SUBAGENT_MODEL_FORCE" in result.stderr) is (code == 1)


def test_cli_dispatch_of_an_unpackaged_agent_is_a_usage_error():
    missing = subprocess.run(
        [sys.executable, str(SCRIPTS / "model_roles.py"), "--dispatch", "no-such-agent"],
        capture_output=True,
        text=True,
        check=False,
        timeout=30,
        env={"PATH": "/usr/bin:/bin"},
    )
    assert missing.returncode == 2 and missing.stdout == "" and "no-such-agent" in missing.stderr


def test_cli_emits_json_without_network(tmp_path):
    result = subprocess.run(
        [sys.executable, str(SCRIPTS / "model_roles.py")], capture_output=True, text=True, check=False, timeout=30
    )
    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout)["catalog"]["checked"] is False
