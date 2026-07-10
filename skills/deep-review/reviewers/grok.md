# Agent G: Grok review

Skip entirely if the `grok` CLI is not installed (`GROK=no`).
(Mechanics verified against grok CLI 0.2.93, model `grok-4.5`, 2026-07-10:
`-p` single-turn mode, `--output-format json` envelope, `--json-schema`
structured output, `--sandbox read-only`, and `--cwd` targeting all observed
working. `grok models` may print "You are not authenticated" while `-p` runs
still succeed on cached credentials — trust the run, not the models listing.)

## Launch prompt

The workflow runs this reviewer with the prompt below:

> Run the Grok CLI headless as an independent reviewer. Single chained Bash
> call, no reliance on prior `cd` (the Bash tool spawns a fresh shell per call):
>
> ```bash
> grok -p "<review prompt, see below>" -m grok-4.5 \
>   --sandbox read-only --cwd "<REPO_ROOT>" \
>   --output-format json \
>   --json-schema '{"type":"object","required":["findings","degraded"],"properties":{"findings":{"type":"array","items":{"type":"object","required":["file","line_start","line_end","severity","description","recommendation"],"properties":{"file":{"type":"string"},"line_start":{"type":"integer"},"line_end":{"type":"integer"},"severity":{"type":"string","enum":["critical","warning","nit"]},"description":{"type":"string"},"recommendation":{"type":"string"}}}},"degraded":{"type":"boolean"},"degraded_reason":{"type":"string"}}}'
> ```
>
> **Review prompt**: instruct grok to review the diff for the selected mode,
> reading files from the working tree as needed, and to report only real
> correctness/security/convention problems in the diff (not pre-existing code),
> with `file` paths relative to the repo root:
> - `--base <branch>` mode (default): review `git diff <base>...HEAD`
> - `--uncommitted` mode: review `git diff HEAD` **plus** untracked files
>   (`git ls-files --others --exclude-standard`) — `git diff HEAD` alone does
>   not see them
> - `--commit <sha>` mode: review `git diff <sha>^..<sha>`
> - `--range <a>..<b>` mode: review `git diff <a>..<b>` (the frozen worktree's
>   HEAD is already `<b>`, so no checkout is needed)
>
> The sandbox is read-only, so grok cannot mutate the tree — no worktree
> isolation or checkout dance is required in any mode.
>
> Return the raw JSON stdout verbatim (plus stderr and the exit code) — do not
> parse, summarise, or reformat beyond mapping into the findings schema below.

## Parsing & degradation

`--output-format json` emits ONE JSON envelope on stdout:
`{text, stopReason, sessionId, requestId, thought, structuredOutput}`. The
payload is `structuredOutput` — already in the Phase 3 common shape thanks to
`--json-schema`. Tag each finding with `source: "grok"`.

Check these signals in order, stopping at the first match:

1. **Non-zero exit code** → degraded; surface stderr.
2. **stdout is not a JSON object** (auth prompt, crash trace, plain text) →
   degraded; surface the raw output head.
3. **`structuredOutput` missing or malformed** → degraded with a
   `CONTRACT-DRIFT` reason (the model answered but the schema constraint was
   not honoured). Malformed means any of: `structuredOutput` is not a non-null
   object; `findings` is not an array; `degraded` is not a boolean; or any
   finding lacks a string `file`, integer `line_start`/`line_end`, `severity`
   in {`critical`,`warning`,`nit`}, or string `description`/`recommendation`.
   Do NOT pass partially-valid findings downstream — the whole run degrades.
4. **`structuredOutput.degraded == true`** → degraded; surface
   `degraded_reason` (grok itself could not complete the review — e.g. the
   diff command failed inside its sandbox; if `degraded_reason` is missing,
   report "degraded without reason").
5. **Keyword fallback** on stderr / non-JSON stdout (case-insensitive):
   `not authenticated`, `sign in`, `rate limit`, `quota`, `429`, `401`.

Same rule as every external reviewer: a degraded Grok is NOT a clean pass —
absence of findings from a crashed or unauthenticated reviewer is absence of
data.
