"""A unit's process registry holds only children the run started in their own session; the barrier
stops exactly those, identified by pid, group and start time, and never anything else."""

import json
import os
import subprocess
import sys

import pytest
from conftest import REPO

SCRIPT = REPO / "skills/deep-review/scripts/owned_processes.py"
SLEEPER = [sys.executable, "-c", "import time; time.sleep(60)"]

pytestmark = pytest.mark.skipif(os.name != "posix", reason="POSIX process groups")


def cli(*args, success=True):
    result = subprocess.run([sys.executable, str(SCRIPT), *map(str, args)], capture_output=True, text=True, timeout=30)
    if success:
        assert result.returncode == 0, result.stderr
        return json.loads(result.stdout)
    assert result.returncode == 1 and not result.stdout and "Traceback" not in result.stderr
    return result.stderr


@pytest.fixture
def children():
    started = []

    def start(own_session=True):
        child = subprocess.Popen(SLEEPER, start_new_session=own_session)
        started.append(child)
        return child

    yield start
    for child in started:
        if child.poll() is None:
            child.kill()
            child.wait()


def test_register_refuses_a_child_in_the_callers_own_group(tmp_path, children):
    registry = tmp_path / "processes.json"
    shared = children(own_session=False)  # same group as this test and the CLI it runs
    error = cli("register", "--registry", registry, "--pid", shared.pid, success=False)
    assert "shares the caller's process group" in error and not registry.exists()
    assert "no process" in cli("register", "--registry", registry, "--pid", 2**22 - 1, success=False)
    assert shared.poll() is None


def test_stop_ends_registered_sessions_and_leaves_everything_else(tmp_path, children):
    registry = tmp_path / "processes.json"
    owned, unrelated = children(), children()
    entry = cli("register", "--registry", registry, "--pid", owned.pid)
    assert entry["pgid"] == owned.pid and entry["started"]
    assert cli("register", "--registry", registry, "--pid", owned.pid) == entry  # idempotent
    assert json.loads(registry.read_text()) == [entry]
    report = cli("stop", "--registry", registry)
    assert [r["outcome"] for r in report] == ["stopped"]
    assert owned.wait(timeout=5) != 0 and unrelated.poll() is None
    assert cli("stop", "--registry", registry) == [{**entry, "outcome": "gone"}]
    assert cli("stop", "--registry", tmp_path / "missing.json") == []


def test_stop_skips_a_pid_that_now_belongs_to_another_process(tmp_path, children):
    registry = tmp_path / "processes.json"
    alive = children()
    entry = cli("register", "--registry", registry, "--pid", alive.pid)
    registry.write_text(json.dumps([{**entry, "started": "Thu  1 Jan 00:00:00 1970"}]))
    assert [r["outcome"] for r in cli("stop", "--registry", registry)] == ["reused"]
    assert alive.poll() is None  # the identity did not match, so the run did not own it
    registry.write_text("not json")
    assert "cannot read" in cli("stop", "--registry", registry, success=False)
