"""Guards on the skills' non-Python payloads: JSON assets stay parseable,
HTML assets stay self-contained (SKILL.md non-negotiable #1: no CDN/external
resources), and the deep-review workflow script stays syntactically valid."""

import json
import re
import shutil
import subprocess
from html.parser import HTMLParser
from textwrap import dedent

import pytest
from conftest import REPO

SKILLS = REPO / "skills"
EXTERNAL = re.compile(r"@import url\(['\"]?https?://|https://cdn\.|fonts\.googleapis")


@pytest.mark.parametrize("path", sorted(SKILLS.rglob("*.json")), ids=lambda p: str(p.relative_to(SKILLS)))
def test_json_assets_parse(path):
    json.loads(path.read_text())


@pytest.mark.parametrize(
    "relative_path",
    [".claude-plugin/plugin.json", ".claude-plugin/marketplace.json", ".codex-plugin/plugin.json"],
)
def test_plugin_metadata_parses(relative_path):
    data = json.loads((REPO / relative_path).read_text())
    assert data["name"] and data["description"]


def test_plugin_metadata_agrees():
    claude = json.loads((REPO / ".claude-plugin/plugin.json").read_text())
    codex = json.loads((REPO / ".codex-plugin/plugin.json").read_text())
    marketplace = json.loads((REPO / ".claude-plugin/marketplace.json").read_text())
    assert claude["name"] == codex["name"]
    assert claude["version"] == codex["version"]
    entries = [entry for entry in marketplace["plugins"] if entry["name"] == claude["name"]]
    assert len(entries) == 1
    assert entries[0]["source"] == "./"
    assert (REPO / codex["skills"]).is_dir()


def test_evals_shape():
    data = json.loads((SKILLS / "html-report" / "evals" / "evals.json").read_text())
    assert data["skill_name"] == "html-report"
    assert data["evals"], "eval suite must not be empty"
    for case in data["evals"]:
        assert case["prompt"] and case["expected_output"]


@pytest.mark.parametrize("path", sorted(SKILLS.rglob("*.html")), ids=lambda p: str(p.relative_to(SKILLS)))
def test_html_assets_are_self_contained(path):
    assert not EXTERNAL.search(path.read_text()), f"{path.name} references external resources"


@pytest.mark.parametrize("data_script", [False, True], ids=["executable", "json-data"])
@pytest.mark.parametrize(
    "value",
    [
        "ordinary text & quotes: \"hello\"",
        '</ScRiPt><script>/* inert test */</script><!--<script>',
        "__DATA__ __META__ __GEN__ café\nnext line",
    ],
    ids=["plain", "script-boundary", "literal-markers"],
)
def test_html_report_generator_example_preserves_data(value, data_script):
    class ScriptParser(HTMLParser):
        def __init__(self):
            super().__init__()
            self.scripts = []
            self.in_script = False

        def handle_starttag(self, tag, attrs):
            if tag == "script":
                self.scripts.append("")
                self.in_script = True

        def handle_data(self, data):
            if self.in_script:
                self.scripts[-1] += data

        def handle_endtag(self, tag):
            if tag == "script":
                self.in_script = False

    doc = (SKILLS / "html-report" / "references" / "generation.md").read_text()
    code = re.search(r"```python\n(.*?)\n\s*```", doc, re.S)
    assert code is not None, "generation.md must contain its executable Python example"
    example = dedent(code.group(1))
    expected = {"DATA": [{"label": value}], "META": {"title": value}, "GEN": value}
    if data_script:
        template = '<script type="application/json">{"DATA":__DATA__,"META":__META__,"GEN":__GEN__}</script>'
    else:
        template = "<script>\nconst DATA = __DATA__;\nconst META = __META__;\nconst GEN = __GEN__;\n</script>"
    namespace = {"TEMPLATE": template, "rows": expected["DATA"], "meta": expected["META"], "gen_stamp": value}
    exec(compile(example, "generation.md", "exec"), namespace)
    parser = ScriptParser()
    parser.feed(namespace["html"])
    assert len(parser.scripts) == 1
    script = parser.scripts[0]
    assert "<" not in script
    actual = (
        json.loads(script)
        if data_script
        else {name: json.loads(data) for name, data in re.findall(r"const (\w+) = (.*?);\n", script)}
    )
    assert actual == expected


@pytest.mark.skipif(shutil.which("node") is None, reason="node not installed")
def test_deep_review_workflow_js_parses():
    # The Workflow harness strips the `export` and runs the body inside an async
    # function (top-level `await`/`return` are legal there); mirror that here.
    src = (SKILLS / "deep-review" / "deep-review.workflow.js").read_text()
    wrapped = "async () => {" + src.replace("export const meta", "const meta", 1) + "}"
    out = subprocess.run(
        ["node", "--input-type=module", "--check"], input=wrapped.encode(), capture_output=True, timeout=30, check=False
    )
    assert out.returncode == 0, out.stderr.decode()
