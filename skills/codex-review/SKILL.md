---
name: codex-review
description: Ask Codex CLI (gpt-5.6-terra, or gpt-5.6-sol for bigger tasks) for an independent code review of uncommitted changes, branch diff, a commit, or a specific implementation. This is how gpt-5.6 is invoked for review work. Use when the user asks Claude to have Codex or gpt-5.6 review work, when the model-selection rubric calls for a gpt-5.6 review perspective, or when Codex should audit a diff, find bugs or regressions, or compare Claude's implementation against requirements. For a review by Claude itself, use the normal review process instead.
---

# Codex Review
Use Codex as an independent reviewer when the user wants a second-pass review or when a change is broad enough that another agent's perspective is useful.
Prefer Claude's normal review process for small local checks. Do not delegate review just to avoid reading the code yourself. Treat Codex's output as evidence, not authority.

## Workflow
1. Identify the review target: uncommitted changes, base branch, commit SHA, PR checkout, or specific files.
2. Create a temporary artifact directory for the Codex report.
3. Run `codex review` with a focused review prompt.
4. Read Codex's report and verify important claims against the code before presenting them.

Use one of these command shapes. Note: codex-cli rejects combining `--uncommitted` or
`--base <branch>` with a custom `[PROMPT]` ("the argument `--uncommitted` cannot be
used with `[PROMPT]`", verified on 0.144.1) — and bare `codex review` with a prompt
does NOT default to the uncommitted diff; it enters free-form custom-instructions
mode with no diff selected. So: use the native targeting flags with the baked-in
review prompt when the stock stance is enough, and embed the diff explicitly via
`codex exec` when you need the custom stance.

```bash
ARTIFACT_DIR="$(mktemp -d "${TMPDIR:-/tmp}/codex-review.XXXXXX")"
REPORT="$ARTIFACT_DIR/report.md"
PROMPT="$ARTIFACT_DIR/prompt.md"   # the review-stance text below
BASE="main"                        # or the caller-selected base branch

# Stock review stance → use the native targeting flags (baked-in review prompt).
codex -C "$PWD" exec review --uncommitted > "$REPORT"     # or: --base "$BASE"

# Custom review stance → codex exec, read-only, prompt + diff on stdin.
# `exec -` reads the whole prompt from stdin (no shell word-splitting), and the
# diff is embedded after the stance so the review target is explicit.
# Uncommitted (include untracked files — `git diff HEAD` alone misses them):
{ cat "$PROMPT"; echo; echo "DIFF UNDER REVIEW:"; git diff HEAD; \
  git ls-files --others --exclude-standard | while IFS= read -r f; do \
    printf '\n--- untracked: %s ---\n' "$f"; cat "$f"; done; } \
  | codex -C "$PWD" exec -s read-only - > "$REPORT"

# Base-branch:
{ cat "$PROMPT"; echo; echo "DIFF UNDER REVIEW:"; git diff "$BASE"...HEAD; } \
  | codex -C "$PWD" exec -s read-only - > "$REPORT"
```

Ask codex to use a code-review stance:
```text
Review these changes for bugs, regressions, missing tests, security issues, and requirement mismatches.
Prioritize findings over summary. For each finding include:
- severity
- file and line reference
- concrete failure mode
- suggested fix direction

If there are no substantive findings, say so and name any residual test gaps.
```

Add task-specific context when useful: requirements, risky areas, expected behavior, relevant tests, or files Claude is unsure about.

## Failure is not a clean pass

The command shapes above redirect stdout to `$REPORT`, so a codex run that
crashed, rate-limited, or died mid-stream leaves a truncated or empty report
that reads exactly like "codex found nothing." An empty report is only a clean
pass if the run itself completed. Before treating any report as review data,
verify per the codex-cli skill's degradation ladder:

1. The codex command exited 0.
2. stderr contains no error (app-server init failure, quota/rate-limit
   keywords, auth prompts).
3. `$REPORT` is non-empty and reads as a review (a verdict or findings), not a
   crash trace or a truncated fragment.

Any miss → the run is **degraded**: report the error verbatim and offer to
review the changes directly instead. Never present a degraded run's empty
output as "no findings."

## Reporting Back

Before relaying a Codex finding, inspect the cited code or diff enough to decide whether the finding is real. In the user-facing response, separate confirmed issues from Codex suggestions you did not verify.

If Codex finds nothing (from a run that passed the checks above), say that clearly and mention what review target it inspected.

If `codex` is not installed, say so and offer to review the changes directly instead.
