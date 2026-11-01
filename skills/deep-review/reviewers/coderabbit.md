# CodeRabbit review (opt-in)

Disabled by default. Run only when reviewer policy explicitly enables
`coderabbit` and preparation finds the CodeRabbit CLI. An installed binary is not
a reason to run this lane, and installation does not prove authentication,
subscription, quota, or a successful review; those are known only when an enabled
review runs. A disabled lane is never probed or dispatched
(references/reviewers.md). Mechanics verified against coderabbit CLI 0.6.4,
originally authored for 0.4.x; the NDJSON `--agent` contract has been stable
across that span.

## Launch prompt

The workflow runs this reviewer with the prompt below:

> Run `coderabbit review --agent` with the appropriate diff scope. The `--agent`
> flag emits NDJSON instead of human-facing markdown — one JSON event per line,
> with `type=finding` events carrying the payload.
>
> **Do NOT invoke the `coderabbit:review` skill** — it IS model-invocable, but it
> wraps the output in a human-facing markdown renderer that throws away the
> structured fields the parser needs. Use the CLI directly.
>
> **Worktree targeting:** pass `--dir "<REPO_ROOT>"` on every invocation. Per
> `coderabbit review --help`, `--dir <path>` reviews git changes inside that
> directory — the targeting equivalent of a `--cwd`/`-C` flag. The Bash tool
> spawns a fresh shell per call, so never rely on a prior `cd`.
>
> - `--base main` mode (default): `coderabbit review --agent --dir "<REPO_ROOT>" --base <base>`
> - `--uncommitted` mode: `coderabbit review --agent --dir "<REPO_ROOT>" -t uncommitted`
> - `--commit <sha>` mode: `coderabbit review --agent --dir "<REPO_ROOT>" --base-commit <sha>^`
>   (the parent, not `<sha>` itself — Phase 1.5 freezes the worktree AT `<sha>`,
>   so its HEAD already equals `<sha>`, and `--base-commit <sha>` would diff
>   `<sha>..HEAD` = empty, a silent no-op)
> - `--range <a>..<b>` mode: CodeRabbit's `--base-commit` reviews from the given
>   commit up to HEAD, not up to a specific upper bound. Use the same
>   detached-HEAD-safe checkout dance as the codex/cubic reviewers, in ONE
>   chained Bash call: `cd "<REPO_ROOT>" && ORIG=$(git symbolic-ref --quiet --short HEAD 2>/dev/null || git rev-parse --verify HEAD) && git checkout <b> && (coderabbit review --agent --dir "<REPO_ROOT>" --base-commit <a>); rc=$?; git checkout "$ORIG"; exit $rc`
>
> Return the raw NDJSON stdout verbatim (plus stderr and the exit code) — do not
> parse, summarise, or reformat beyond mapping into the findings schema below.

## Parsing & degradation

NDJSON: one JSON object per line. Filter to `type == "finding"` events. For each:

- `file` ← `fileName`
- `severity` ← `severity`. Observed values: `"trivial"`, `"minor"`, `"critical"`.
  Map: `trivial` → `nit`, `minor` → `warning`, `critical` → `critical`. Default
  any unknown value to `warning`.
- `description` + `recommendation` ← `codegenInstructions` (the text after the
  leading "Verify each finding..." sentence is the actual guidance)
- `line_start` / `line_end` ← regex from `codegenInstructions`:
  `(?:at line|around lines?)\s*(\d+)(?:\s*-\s*(\d+))?` — group 1 is `line_start`;
  group 2 is `line_end` (equal to `line_start` when only one line is cited).
  If no match, default both to `0`.
- Tag with `source: "coderabbit"`.
- Watch for the trailing `{"type":"complete","findings":N}` event as a sanity
  check on count.

**Rate-limit / degradation detection**: CodeRabbit CLI has per-plan hourly caps
(Free 3/hr, OSS 2/hr, Trial 4/hr, Pro 5/hr, Enterprise 12/hr — see
docs.coderabbit.ai/cli/overview). Check three signals in order, stopping at the
first match:

- **Explicit error event** (preferred, CLI v0.4.x+): any `{"type":"error", ...}`
  line means the CLI itself reported a failure. Mark `degraded` and surface the
  event's message field so the user sees the exact reason (rate limit, auth,
  schema, etc.).
- **Missing `complete` event** (structural fallback): no error event but the
  stream also doesn't terminate with `{"type":"complete",...}` — the review
  didn't finish. Mark `degraded`. Covers crashes, network drops, and failure
  modes that don't emit an error event.
- **Keyword fallback** on stderr / non-JSON stdout lines (case-insensitive):
  `rate limit`, `quota`, `exceeded`, `429`, `401`, `forbidden`, `sign in`.
  If any match, mark `degraded` and surface the message so a billing/auth issue
  is distinguishable from an infra glitch.

Same rule as every external reviewer: a degraded CodeRabbit is NOT a clean pass —
absence of findings from a crashed or rate-limited reviewer is absence of data.
