#!/usr/bin/env python3
"""Grade html-report eval iteration runs against eval_metadata.json assertions.

Mechanical checks are computed; judgment-flavored ones get heuristic evidence
for human review. The script writes grading.json into each configuration run
directory and prints a compact pass/fail summary.
"""

from __future__ import annotations

import argparse
import json
import pathlib
import re
import shutil
import subprocess
import sys
import tempfile

DEFAULT_WORKSPACE = pathlib.Path.home() / ".agents/skills/html-report-workspace/iteration-1"
DEFAULT_INPUTS = pathlib.Path.home() / ".agents/skills/html-report-workspace/inputs"


def load_html(outdir: pathlib.Path) -> tuple[pathlib.Path | None, str | None]:
    files = sorted(outdir.glob("*.html"))
    if not files:
        return None, None
    f = files[0]
    return f, f.read_text()


def node_check(html: str) -> tuple[bool, str]:
    if shutil.which("node") is None:
        return False, "node not found; embedded script syntax was not checked"

    scripts = re.findall(r"<script[^>]*>(.*?)</script>", html, re.S)
    bad = []
    for i, s in enumerate(scripts):
        if not s.strip():
            continue
        tmp_path = None
        try:
            with tempfile.NamedTemporaryFile("w", suffix=".js", delete=False) as t:
                t.write(s)
                tmp_path = pathlib.Path(t.name)
            r = subprocess.run(["node", "--check", str(tmp_path)], capture_output=True, text=True, check=False)
        finally:
            if tmp_path is not None:
                tmp_path.unlink(missing_ok=True)
        if r.returncode != 0:
            bad.append(f"script#{i}: {r.stderr.strip()[:200]}")
    return (not bad), (f"{len(scripts)} blocks checked" if not bad else "; ".join(bad))


def external_refs(html: str) -> tuple[bool, str]:
    pats = [
        r'<script[^>]+src=["\']https?://',
        r'<link[^>]+href=["\']https?://',
        r'@import\s+url\(\s*["\']?https?://',
        r"fonts\.googleapis",
        r"\bcdn\.",
    ]
    hits = [p for p in pats if re.search(p, html, re.I)]
    return (not hits), ("none found" if not hits else f"external refs matched: {hits}")


def theme_toggle(html: str) -> tuple[bool, str]:
    has_dark = bool(
        re.search(r'data-theme=["\']?dark', html)
        or re.search(r"\[data-theme=", html)
        or re.search(r"\.dark\b|prefers-color-scheme", html, re.I)
    )
    has_toggle = bool(re.search(r"setAttribute\(\s*['\"]data-theme", html)) or bool(
        re.search(r"toggle", html, re.I) and "data-theme" in html
    )
    ok = has_dark and has_toggle
    return ok, f"data-theme overrides: {has_dark}, toggle mechanism: {has_toggle}"


def grade_eval0(html: str) -> list[tuple[str, bool, str]]:
    checks = []
    checks.append(("html-not-md", True, "HTML file present"))
    checks.append(("self-contained",) + external_refs(html))
    checks.append(("themes-toggle",) + theme_toggle(html))
    sev = all(s.lower() in html.lower() for s in ["critical", "high", "medium"])
    pre = bool(re.search(r"pre-?existing", html, re.I))
    checks.append(
        ("severity-taxonomy", sev and pre, f"severity labels present: {sev}, pre-existing distinguished: {pre}")
    )
    ev = {
        t: (t in html)
        for t in ["4f2a91c", "233", "18/18", "pipeline/promote.py", "ledger/writer.py", "tests/arch/test_layers.py"]
    }
    checks.append(("evidence-inline", all(ev.values()), json.dumps(ev)))
    checks.append(("node-check",) + node_check(html))
    anchor_count = len(re.findall(r'\bid=["\']', html))
    loop = bool(re.search(r"clipboard|claude-cli://|copy|download", html, re.I)) or anchor_count >= 5
    checks.append(
        (
            "close-the-loop",
            loop,
            f"clipboard/copy/deep-link/download present or >=5 anchors ({anchor_count} ids)",
        )
    )
    return checks


def grade_eval1(html: str) -> list[tuple[str, bool, str]]:
    checks = []
    checks.append(("html-comparison-page", True, "single HTML file"))
    # variant count heuristic: look for 4+ option/variant labels
    n_opts = len(set(re.findall(r"(?:Option|Variant)\s*[1-4A-D]", html, re.I)))
    checks.append(
        (
            "four-named-variants",
            n_opts >= 4,
            f"distinct Option/Variant labels found: {n_opts} (verify visually: live-rendered, realistic content)",
        )
    )
    checks.append(("no-webfonts",) + external_refs(html))
    checks.append(("light-dark",) + theme_toggle(html))
    dec = bool(re.search(r"decision|pick|verdict|choose", html, re.I))
    checks.append(("decision-place", dec, f"decision/pick element keyword present: {dec}"))
    checks.append(("node-check",) + node_check(html))
    return checks


def load_jsonl_rows(path: pathlib.Path) -> tuple[list[dict], str | None]:
    try:
        return [json.loads(line) for line in path.read_text().splitlines() if line.strip()], None
    except (OSError, json.JSONDecodeError) as exc:
        return [], str(exc)


def grade_eval2(html: str, jsonl: pathlib.Path) -> list[tuple[str, bool, str]]:
    checks = []
    rows, err = load_jsonl_rows(jsonl)
    if err:
        checks.append(("full-dataset-embedded", False, f"could not load deploy jsonl at {jsonl}: {err}"))
        rows = []
    else:
        shas = [r["sha"] for r in rows]
        missing = [s for s in shas if s not in html]
        checks.append(
            (
                "full-dataset-embedded",
                not missing,
                f"{len(shas) - len(missing)}/{len(shas)} shas found" + (f"; missing {missing[:3]}" if missing else ""),
            )
        )
    fetches = bool(re.search(r"\bfetch\(|XMLHttpRequest", html)) and not bool(
        re.search(r"/api/feedback", html)
    )  # feedback fetch w/ blob fallback is fine
    ext_ok, ext_ev = external_refs(html)
    checks.append(
        (
            "no-fetch-no-external",
            ext_ok and not fetches,
            f"external refs ok: {ext_ok} ({ext_ev}); data fetch present: {fetches}",
        )
    )
    dims = {d: bool(re.search(d, html, re.I)) for d in ["repo", "status", "deployer"]}
    filt = bool(re.search(r"filter|chip|select", html, re.I))
    checks.append(
        ("filter-controls", all(dims.values()) and filt, f"dimensions mentioned: {dims}, filter mechanism: {filt}")
    )
    copy = bool(re.search(r"clipboard|copy", html, re.I))
    checks.append(("copy-export", copy, f"clipboard/copy mechanism present: {copy}"))
    checks.append(("node-check",) + node_check(html))
    checks.append(("themes-toggle",) + theme_toggle(html))
    return checks


GRADERS = {
    "eval-0-audit-findings-report": grade_eval0,
    "eval-1-typography-variant-gallery": grade_eval1,
    "eval-2-deploy-events-explorer": grade_eval2,
}


def summarize(expectations: list[dict]) -> dict:
    passed = sum(1 for e in expectations if e["passed"])
    total = len(expectations)
    return {
        "passed": passed,
        "failed": total - passed,
        "total": total,
        "pass_rate": round(passed / total, 4) if total else 0.0,
    }


def grade_case(evdir: pathlib.Path, cfg: str, deploys_jsonl: pathlib.Path) -> tuple[int, int, str]:
    meta = json.loads((evdir / "eval_metadata.json").read_text())
    outdir = evdir / cfg / "outputs"
    f, html = load_html(outdir) if outdir.exists() else (None, None)
    if html is None:
        expectations = [
            {"text": assertion, "passed": False, "evidence": "no HTML output produced"}
            for assertion in meta["assertions"]
        ]
    else:
        if evdir.name == "eval-2-deploy-events-explorer":
            checks = grade_eval2(html, deploys_jsonl)
        else:
            checks = GRADERS[evdir.name](html)
        expectations = [
            {"text": meta["assertions"][i], "passed": bool(check[1]), "evidence": str(check[2])}
            for i, check in enumerate(checks)
        ]
    grading = {"expectations": expectations, "summary": summarize(expectations)}
    target = evdir / cfg / "grading.json"
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(json.dumps(grading, indent=2) + "\n")
    return grading["summary"]["passed"], grading["summary"]["total"], f.name if f else "NO OUTPUT"


def grade_workspace(workspace: pathlib.Path, deploys_jsonl: pathlib.Path) -> int:
    if not workspace.is_dir():
        print(f"error: workspace not found: {workspace}", file=sys.stderr)
        return 2
    found = 0
    for evdir in sorted(workspace.iterdir()):
        if not evdir.is_dir() or evdir.name not in GRADERS:
            continue
        found += 1
        for cfg in ["with_skill", "without_skill"]:
            passed, total, artifact = grade_case(evdir, cfg, deploys_jsonl)
            print(f"{evdir.name}/{cfg}: {passed}/{total} ({artifact})")
    if found == 0:
        print(f"error: no known eval directories found in {workspace}", file=sys.stderr)
        return 2
    return 0


def parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Grade html-report eval iteration outputs")
    parser.add_argument("workspace", nargs="?", type=pathlib.Path, default=DEFAULT_WORKSPACE)
    parser.add_argument(
        "--inputs-dir",
        type=pathlib.Path,
        default=None,
        help="Directory containing deploys.jsonl; defaults to <workspace>/../inputs, then the legacy home workspace",
    )
    parser.add_argument("--deploys-jsonl", type=pathlib.Path, default=None, help="Explicit deploy-events JSONL fixture")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(sys.argv[1:] if argv is None else argv)
    workspace = args.workspace.expanduser().resolve()
    if args.deploys_jsonl:
        deploys_jsonl = args.deploys_jsonl.expanduser().resolve()
    else:
        inputs_dir = args.inputs_dir.expanduser().resolve() if args.inputs_dir else (workspace.parent / "inputs")
        deploys_jsonl = inputs_dir / "deploys.jsonl"
        if not deploys_jsonl.exists():
            deploys_jsonl = DEFAULT_INPUTS / "deploys.jsonl"
    return grade_workspace(workspace, deploys_jsonl)


if __name__ == "__main__":
    raise SystemExit(main())
