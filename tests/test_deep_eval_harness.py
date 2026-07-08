import json
import subprocess
import sys

from conftest import REPO

GRADE = REPO / "skills" / "html-report" / "evals" / "grade.py"
VIEWER = REPO / "skills" / "html-report" / "evals" / "eval-viewer" / "generate_review.py"


def _write_eval_workspace(tmp_path):
    workspace = tmp_path / "iteration-1"
    evdir = workspace / "eval-2-deploy-events-explorer"
    outputs = evdir / "with_skill" / "outputs"
    outputs.mkdir(parents=True)
    (evdir / "without_skill" / "outputs").mkdir(parents=True)
    (evdir / "eval_metadata.json").write_text(
        json.dumps(
            {
                "eval_id": 2,
                "eval_name": "deploy-events-explorer",
                "prompt": "Build a deploy explorer",
                "assertions": [
                    "full dataset embedded",
                    "no fetch or external refs",
                    "filter controls",
                    "copy export",
                    "node check",
                    "theme toggle",
                ],
            }
        )
    )
    html = """
    <!doctype html>
    <html data-theme="dark">
      <body>
        <button onclick="document.documentElement.setAttribute('data-theme','light')">toggle</button>
        <label>repo <select></select></label>
        <label>status <select></select></label>
        <label>deployer <select></select></label>
        <button>copy JSON</button>
        <script>
          const rows = [{repo:"api",status:"ok",deployer:"tom",sha:"abc123"}];
          console.log(rows);
        </script>
      </body>
    </html>
    """
    (outputs / "deploy-events-explorer.html").write_text(html)
    (evdir / "without_skill" / "outputs" / "deploy-events-explorer.html").write_text(html)
    inputs = tmp_path / "inputs"
    inputs.mkdir()
    (inputs / "deploys.jsonl").write_text(json.dumps({"sha": "abc123"}) + "\n")
    return workspace, inputs


def test_grade_writes_summary_with_explicit_inputs(tmp_path):
    workspace, inputs = _write_eval_workspace(tmp_path)

    result = subprocess.run(
        [sys.executable, str(GRADE), str(workspace), "--inputs-dir", str(inputs)],
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode == 0, result.stderr
    grading = json.loads((workspace / "eval-2-deploy-events-explorer" / "with_skill" / "grading.json").read_text())
    assert grading["summary"] == {"passed": 6, "failed": 0, "total": 6, "pass_rate": 1.0}


def test_eval_viewer_writes_static_html(tmp_path):
    workspace, inputs = _write_eval_workspace(tmp_path)
    subprocess.run([sys.executable, str(GRADE), str(workspace), "--inputs-dir", str(inputs)], check=True)
    out = tmp_path / "review.html"

    result = subprocess.run(
        [sys.executable, str(VIEWER), str(workspace), "--skill-name", "html-report", "--static", str(out)],
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode == 0, result.stderr
    html = out.read_text()
    assert "const EMBEDDED_DATA =" in html
    assert "deploy-events-explorer.html" in html
    assert '"skill_name": "html-report"' in html
