# Agent E: Claude convention compliance

Always runs — no external tool required. The workflow launches this reviewer;
its model and effort are set in the `REGISTRY` in `deep-review.workflow.js` (the
single source of truth).

## Launch prompt

The workflow runs this reviewer with the prompt below:

> Fetch the diff for the selected mode:
> - `--base <branch>`: `git diff <base>...HEAD`
> - `--uncommitted`: `git diff HEAD` to get tracked uncommitted changes, then `git ls-files --others --exclude-standard` and `Read` each untracked file's full contents (`git diff HEAD` does NOT see untracked files — a newly created file would otherwise be invisible to the review).
> - `--commit <sha>`: `git diff <sha>^..<sha>`
> - `--range <a>..<b>`: `git diff <a>..<b>`
>
> Read both CLAUDE.md and REVIEW.md at the repository root.
>
> Review the diff for compliance with CLAUDE.md and REVIEW.md rules. Check:
> 1. New code follows conventions documented in CLAUDE.md
> 2. REVIEW.md "Always check" rules are satisfied
> 3. No REVIEW.md "Skip" items are being violated
> 4. Dependencies are properly declared in the project manifest (pyproject.toml / Cargo.toml)
> 5. Lint suppressions (ruff per-file-ignores / clippy allows) are justified
>
> Return violations as a JSON array — nothing else. No markdown fence, no prose wrapper, no preamble. Each violation must have these fields:
>
> - `file` (string): path relative to repo root
> - `line_start` (int): first affected line number
> - `line_end` (int): last affected line number (equal to `line_start` for single-line findings)
> - `severity` (one of `"critical"`, `"warning"`, `"nit"`)
> - `rule` (string): the specific CLAUDE.md or REVIEW.md rule violated, quoted verbatim
> - `description` (string): why the code violates the rule
>
> Return `[]` (empty array) if nothing is found. Output ONLY the JSON array — do not add commentary before or after it.

## Parsing & degradation

Each element has `{file, line_start, line_end, severity, rule, description}`. The `rule` field is unique to Agent E (the specific CLAUDE.md/REVIEW.md rule quoted verbatim). Map it into the normalised shape as:

- `file`, `line_start`, `line_end`, `severity`, `description` ← pass through
- `recommendation` ← `rule` (the rule itself IS the fix guidance)
- Tag with `source: "claude-conventions"` and preserve the original `rule` field in `raw` for the final report.
