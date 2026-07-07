"""Guards on the skills' non-Python payloads: JSON assets stay parseable,
HTML assets stay self-contained (SKILL.md non-negotiable #1: no CDN/external
resources), and the deep-review workflow script stays syntactically valid."""

import json
import re
import shutil
import subprocess

import pytest
from conftest import REPO

SKILLS = REPO / "skills"
EXTERNAL = re.compile(r"@import url\(['\"]?https?://|https://cdn\.|fonts\.googleapis")


@pytest.mark.parametrize("path", sorted(SKILLS.rglob("*.json")), ids=lambda p: str(p.relative_to(SKILLS)))
def test_json_assets_parse(path):
    json.loads(path.read_text())


def test_evals_shape():
    data = json.loads((SKILLS / "html-report" / "evals" / "evals.json").read_text())
    assert data["skill_name"] == "html-report"
    assert data["evals"], "eval suite must not be empty"
    for case in data["evals"]:
        assert case["prompt"] and case["expected_output"]


@pytest.mark.parametrize("path", sorted(SKILLS.rglob("*.html")), ids=lambda p: str(p.relative_to(SKILLS)))
def test_html_assets_are_self_contained(path):
    assert not EXTERNAL.search(path.read_text()), f"{path.name} references external resources"


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
