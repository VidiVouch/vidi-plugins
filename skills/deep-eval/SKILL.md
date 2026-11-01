---
name: deep-eval
description: Run and review skill evaluation suites with deterministic graders, side-by-side with-skill/without-skill outputs, generated benchmark summaries, and an interactive/static review viewer. Use when evaluating whether a skill improves agent outputs, comparing eval iterations, grading HTML/report artifacts, or preparing a human review page for eval results.
argument-hint: "[ <skill-name> ] [ --workspace <path> ] [ --static <path> ] [ --previous-workspace <path> ]"
---

# Deep Eval

Use this skill to evaluate another skill against concrete task prompts and
assertions. Keep the workflow evidence-first: every score should point to
mechanical checks, artifacts, or explicit human notes.

## Workflow

1. Find the target skill's eval suite:
   - Built-in example: `skills/html-report/evals/evals.json`.
   - Inputs live under `skills/<skill>/evals/inputs/`.
   - Iteration outputs should be generated outside git or under ignored
     `skills/<skill>/evals/iteration-*` directories.
2. Run each eval prompt in both configurations when possible:
   - `with_skill`: explicitly invoke the target skill.
   - `without_skill`: ask for the same deliverable without invoking the skill.
3. Save each run as:

    ```text
    <workspace>/<eval-slug>/<configuration>/outputs/<artifact files>
    <workspace>/<eval-slug>/eval_metadata.json
    ```

4. Run the deterministic grader for suites that have one. For the bundled
   `html-report` suite:

    ```bash
    uv run python skills/html-report/evals/grade.py <workspace> \
      --inputs-dir skills/html-report/evals/inputs
    ```

5. Generate a review page:

    ```bash
    uv run python skills/html-report/evals/eval-viewer/generate_review.py <workspace> \
      --skill-name html-report \
      --benchmark <workspace>/benchmark.json \
      --static <workspace>/review.html
    ```

6. Review the generated page. Treat heuristic assertions as prompts for human
   verification, not proof. Add human feedback in the viewer or as notes next
   to the run.

## Rules

- Do not commit bulky generated iteration outputs unless the user explicitly
  wants a benchmark snapshot in git.
- Keep graders deterministic and local. No network, no external CDNs, no hidden
  services.
- A failed grader setup is not a clean pass. Surface missing inputs, missing
  outputs, parser errors, or unavailable tools directly in the grading evidence.
- Prefer static review HTML for sharing; use the local server only while
  actively reviewing and saving feedback.
- Paid evaluation runs are an approval gate. Agree inputs, model, trial count,
  stopping condition, and total cost before any metered call, and get explicit
  approval for that scope; see deep-review/references/approvals.md, gate
  `paid-calls`. Hosting a review page beyond localhost follows `hosted-reports`.
