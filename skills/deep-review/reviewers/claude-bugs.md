# Agent D: Claude bug scanner

Always runs — no external tool required. The workflow launches this reviewer;
its model and effort are set in the `REGISTRY` in `deep-review.workflow.js` (the
single source of truth — deep bug reasoning currently gets the strongest tier).

## Launch prompt

The workflow runs this reviewer with the prompt below:

> Fetch the diff for the selected mode:
> - `--base <branch>`: `git diff <base>...HEAD`
> - `--uncommitted`: `git diff HEAD` to get tracked uncommitted changes, then `git ls-files --others --exclude-standard` and `Read` each untracked file's full contents (`git diff HEAD` does NOT see untracked files — a newly created file would otherwise be invisible to the review).
> - `--commit <sha>`: `git diff <sha>^..<sha>`
> - `--range <a>..<b>`: `git diff <a>..<b>`
>
> Read REVIEW.md at the repository root for project-specific rules and known false positives.
>
> Review the diff. Focus exclusively on:
> 1. Code that will fail at runtime (missing imports, wrong signatures, type errors)
> 2. Logic errors that produce wrong results regardless of input
> 3. Resource leaks (unclosed files, connections, missing finally blocks)
> 4. Exception handling gaps (bare except, swallowed errors, missing re-raise)
>
> Do NOT flag anything in the REVIEW.md "Skip" section. Do NOT flag style issues, missing docstrings, or potential issues that depend on specific inputs.
>
> Return findings as a JSON array — nothing else. No markdown fence, no prose wrapper, no preamble. Each finding must have these fields:
>
> - `file` (string): path relative to repo root
> - `line_start` (int): first affected line number
> - `line_end` (int): last affected line number (equal to `line_start` for single-line findings)
> - `severity` (one of `"critical"`, `"warning"`, `"nit"`)
> - `description` (string): what's wrong, grounded in the diff
> - `recommendation` (string): the concrete fix
>
> Return `[]` (empty array) if nothing is found. Output ONLY the JSON array — do not add commentary before or after it.

## Parsing & degradation

The agent returns a JSON array already in the target shape. Tag each element with `source: "claude-bugs"` and merge.
