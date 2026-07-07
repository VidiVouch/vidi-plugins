# Agent B: Codex standard review

Skip entirely if the `codex` CLI is not installed (`CODEX=no`).

The mechanics for this reviewer live in the project **codex-review** skill
(`.claude/skills/codex-review/SKILL.md`) — the `codex review` command shapes,
the review-stance prompt, and the verify-before-relaying discipline. That skill
documents two diff targets only: `--uncommitted` and `--base <branch>`. It has
no `--commit` / `--range` support and no machine-readable output format, so this
spec tells the agent how to cover the other targets and how to normalise the
output itself. This spec only wires the reviewer into the deep-review pipeline.

## Launch prompt

Launch a background general-purpose Agent with prompt:

> Read the file `<REPO_ROOT>/.claude/skills/codex-review/SKILL.md` and follow
> its `codex review` mechanics and review-stance prompt. Use
> `REPO_ROOT=<REPO_ROOT>`. Run codex from inside `REPO_ROOT`.
>
> Read that file directly — do NOT invoke `/codex:review` or any plugin slash
> command; the plugin's review commands are `disable-model-invocation: true`
> and are a different thing from the project skill.
>
> Select the diff target:
> - `--uncommitted` or `--base <branch>` → use the skill's command shape as-is.
> - `--commit <sha>` or `--range <a>..<b>` → the skill's `codex review` does NOT
>   support these. Build the diff yourself with git (`git diff <a>..<b>` or
>   `git show <sha>`) and hand it to codex for review directly (e.g. embed the
>   diff in the review-stance prompt via `codex exec`). Do not silently fall
>   back to `--uncommitted`.
> - Any target you cannot cover, or codex output you cannot parse into findings
>   → report it as **degraded** (below), do NOT improvise a clean pass.
>
> Return exactly three things, no prose wrapper:
> 1. the findings as a JSON array in the Phase 3 common shape — one object per
>    finding: `{file, line_start, line_end, severity, description,
>    recommendation, source: "codex-review"}`, mapped from the codex report's
>    file:line-range / severity / failure-mode / fix-direction fields
>    (`severity` is one of `critical` / `warning` / `nit`; use the same integer
>    for `line_start` and `line_end` when codex cites a single line)
> 2. the verdict sentence verbatim (codex's no-ship / residual-gap line)
> 3. a degradation status: `ok`, or `degraded: <reason>` (codex not installed,
>    command failed, target unsupported, or output unparseable)

## Parsing & degradation

The agent returns findings already normalised into the Phase 3 common shape —
merge them directly; no further parsing needed. Keep the verdict sentence
aside as the ship/no-ship narrative for Phase 4.

If the agent reports `degraded`, mark Agent B `degraded` and surface the
reason in the Phase 4 note. Do NOT treat a degraded run as a clean pass — the
absence of findings is an absence of data, not an absence of problems.

**Non-determinism caveat** (from the codex-review skill, restated because it
affects synthesis): two runs on the same diff can produce very different
findings. Don't dismiss single-tool Agent B findings just because they didn't
show up on a previous run — verify against the actual code in Step 3d.
