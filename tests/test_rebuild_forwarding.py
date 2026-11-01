"""The user-level /rebuild entry is a forwarding stub for vidi-skills:rebuild, not a
second implementation: it must carry the plugin's exact argument surface, map or
reject every old argument explicitly, and the helper that installs it must back
the previous entry up and restore it."""

import json
import re
import subprocess
import sys

from conftest import REPO

REBUILD = REPO / "skills" / "rebuild"
ENTRY = REBUILD / "compat" / "user-skill.md"
SCRIPT = REBUILD / "scripts" / "user_skill_forwarding.py"
PLUGIN_SKILL = REBUILD / "SKILL.md"
FRONTMATTER = re.compile(r"^---\n(.*?)\n---\n", re.DOTALL)
OLD_FLAGS = ["--target", "--guide", "--from", "--into"]


def frontmatter(path):
    m = FRONTMATTER.match(path.read_text())
    assert m, f"{path.name} must open with a YAML frontmatter block"
    return dict(line.split(": ", 1) for line in m.group(1).splitlines())


def plugin_flags():
    section = PLUGIN_SKILL.read_text().split("## Arguments", 1)[1].split("\n## ", 1)[0]
    return re.findall(r"^- `(--[a-z-]+)", section, re.M)


def table_rows():
    rows = {}
    for line in ENTRY.read_text().splitlines():
        if line.startswith("| ") and not line.startswith("| Argument "):
            cells = [c.strip() for c in line.strip("|").split("|")]
            rows[cells[0]] = cells[1]
    return rows


def test_entry_is_a_user_skill_named_rebuild_that_only_dispatches():
    fm = frontmatter(ENTRY)
    assert fm["name"] == "rebuild", "the user-level name is what keeps /rebuild available"
    assert fm["allowed-tools"] == "Skill", "a forwarding entry needs nothing but the Skill tool"
    assert "vidi-skills:rebuild" in fm["description"]


def test_entry_mirrors_the_plugin_argument_surface():
    assert frontmatter(ENTRY)["argument-hint"] == frontmatter(PLUGIN_SKILL)["argument-hint"]
    flags = plugin_flags()
    assert set(OLD_FLAGS) <= set(flags), "the plugin must still accept every old flag by name"
    text = ENTRY.read_text()
    for flag in flags:
        assert f"`{flag}" in text, f"forwarding entry does not mention plugin flag {flag}"


def test_old_optional_from_and_into_are_rejected_explicitly_when_missing():
    rows = table_rows()
    for flag in ("--from <base>", "--into <branch>"):
        row = rows[f"`{flag}`"]
        assert "Forwarded unchanged" in row and "Required" in row and "reject" in row, row
        assert "it was optional" in row, "the entry must name the old optional behavior it retires"
    assert "Rejected" in rows["Any other `--flag`"] and "silently" in rows["Any other `--flag`"]
    assert "Rejected" in rows["Positional arguments"]


def test_entry_holds_no_second_implementation():
    text = ENTRY.read_text()
    assert "## Phase" not in text and "cherry-pick" not in text, "procedure belongs to the plugin skill only"
    assert "Skill tool" in text and "Do not improvise" in text
    assert "\u2014" not in text and "\u2013" not in text


def run(*argv, home):
    out = subprocess.run(
        [sys.executable, str(SCRIPT), "--home", str(home), *argv], capture_output=True, text=True, timeout=30
    )
    return out.returncode, (json.loads(out.stdout) if out.stdout else None), out.stderr


def test_helper_status_activate_restore_round_trip(tmp_path):
    home = tmp_path / "home"
    live = home / ".claude" / "skills" / "rebuild" / "SKILL.md"
    code, out, _ = run("status", home=home)
    assert (code, out["state"]) == (0, "missing")

    live.parent.mkdir(parents=True)
    live.write_text("---\ndescription: old full implementation\n---\n## Phase 0\n")
    assert run("status", home=home)[1]["state"] == "legacy"

    code, _, err = run("activate", home=home)
    assert code == 2 and "--backup-dir" in err, "never overwrite a live entry without a backup"
    assert "Phase 0" in live.read_text()

    backups = tmp_path / "backups"
    code, out, _ = run("activate", "--backup-dir", str(backups), home=home)
    assert code == 0 and out["changed"] and out["previousState"] == "legacy" and out["state"] == "forwarding"
    assert live.read_text() == ENTRY.read_text()
    assert (backups / out["backup"].rsplit("/", 1)[1]).read_text().endswith("## Phase 0\n")
    assert "restore" in out["restore"] and out["backup"] in out["restore"]

    code, out, _ = run("activate", "--backup-dir", str(backups), home=home)
    assert code == 0 and out["changed"] is False, "activating twice is a no-op"

    live.write_text(live.read_text() + "\nlocal edit\n")
    assert run("status", home=home)[1]["state"] == "forwarding-outdated"

    backup = next(backups.iterdir())
    code, out, _ = run("restore", "--backup", str(backup), home=home)
    assert code == 0 and run("status", home=home)[1]["state"] == "legacy"
    assert run("restore", "--backup", str(tmp_path / "nope"), home=home)[0] == 2


def test_new_files_use_plain_dashes():
    for path in (ENTRY, SCRIPT, REPO / "tests" / "test_rebuild_forwarding.py"):
        assert not {"\u2014", "\u2013"} & set(path.read_text()), path
