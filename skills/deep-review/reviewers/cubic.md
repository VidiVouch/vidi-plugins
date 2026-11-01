# Cubic review (opt-in)

Disabled by default. Run only when reviewer policy explicitly enables `cubic`
and preparation finds the Cubic CLI. An installed binary is not a reason to run
this lane, and installation does not prove authentication, subscription, quota,
or a successful review; those are known only when an enabled review runs. A
disabled lane is never probed or dispatched (references/reviewers.md).

## Launch prompt

Launch a background general-purpose Agent with prompt:

> Run `cubic review --json` with the appropriate diff scope. The `--json` flag emits a single JSON object `{"issues": [...]}` on stdout instead of the interactive TUI.
>
> **Worktree targeting:** cubic has no review-scoped directory flag — the top-level `--dir` belongs to the TUI server command, and `cubic review --dir` just prints the help text and exits 1 (observed v1.7.0). Always chain `cd "<REPO_ROOT>" && ...` in a single Bash call (the Bash tool spawns a fresh shell per call, so a prior `cd` doesn't persist). Prefix every invocation with `CUBIC_DISABLE_AUTOUPDATE=1` so the CLI can't self-update mid-run (cubic's own CI guidance).
>
> **Bad-ref guard:** cubic silently returns `{"issues": []}` with exit 0 when given an unresolvable `--base`/`--commit` ref (observed v1.7.0) — indistinguishable from a clean pass. In `--base`/`--commit`/`--range` modes, validate each ref first with `git rev-parse --verify "<ref>^{commit}"`; if validation fails, return an error note instead of running cubic, so a typo'd ref can't masquerade as a clean review.
>
> - `--base main` mode (default): `cd "<REPO_ROOT>" && CUBIC_DISABLE_AUTOUPDATE=1 cubic review --json --base <base>`
> - `--uncommitted` mode: `cd "<REPO_ROOT>" && CUBIC_DISABLE_AUTOUPDATE=1 cubic review --json` — uncommitted changes are cubic's default scope when no targeting flag is passed, and that scope includes untracked files natively (no extra fetch needed).
> - `--commit <sha>` mode: `cd "<REPO_ROOT>" && CUBIC_DISABLE_AUTOUPDATE=1 cubic review --json --commit <sha>`
> - `--range <a>..<b>` mode: cubic's `--base` reviews from the given ref up to the current tree, so use the same checkout dance as Agents B/C in one chained Bash call: `cd "<REPO_ROOT>" && ORIG=$(git symbolic-ref --quiet --short HEAD 2>/dev/null || git rev-parse --verify HEAD) && git checkout <b> && (CUBIC_DISABLE_AUTOUPDATE=1 cubic review --json --base <a>); rc=$?; git checkout "$ORIG"; exit $rc`.
>
> If `--focus` was provided, append `--prompt "<focus text>"` in `--base`, `--uncommitted`, and `--range` modes — per `cubic review --help` (v1.7.0), `--prompt` can be used alone or combined with `--base`. Omit it in `--commit` mode; the help text does not promise `--prompt` combines with `--commit`.
>
> Return the raw JSON stdout verbatim plus the command's exit code — do not parse, summarise, or reformat. **Exit 1 with valid JSON on stdout means "issues found", not failure** — cubic uses CI-gate exit semantics (0 = clean, 1 = findings; verified v1.7.0). Phase 3 extracts the structured fields.

## Parsing & degradation

The agent returns `{"issues": [...]}` plus the exit code. Each issue (exact shape verified v1.7.0):

```
{
  "priority": "P0" | "P1" | "P2" | "P3",
  "file": str,        # already repo-relative
  "line": int,        # single line — cubic does not emit ranges
  "title": str,
  "description": str
}
```

Normalise each issue as:

- `file` ← `file` (already relative — no stripping needed)
- `line_start` / `line_end` ← `line` (set both to the same value; default `0` if absent)
- `severity` ← map `priority`: `P0` → `"critical"`, `P1` → `"warning"`, `P2`/`P3` → `"nit"` (same collapse as Agent B). Default unknown values to `"warning"`.
- `description` ← `title`
- `recommendation` ← `description` (the explanation embeds the fix — the `--json` output has no dedicated fix field; the TUI's fix prompts are not exposed here)
- Tag with `source: "cubic"`.

**Rate-limit / degradation detection**: check these signals in order, stopping at the first match:

- **Valid JSON with an `issues` array** → Agent F succeeded, REGARDLESS of exit code. cubic exits 1 when issues were found and 0 when clean (CI-gate semantics, verified v1.7.0) — exit 1 with parseable findings is a successful review, NOT a degraded one.
- **Non-JSON stdout or missing `issues` key** → mark `degraded`. Observed failure shape: unrecognised flags make yargs print the help text to stdout and exit 1. Surface the first lines of stdout/stderr in the Phase 4 note.
- **Keyword fallback** on stderr (case-insensitive): `rate limit`, `quota`, `429`, `too many requests`, `sign in`, `unauthorized`, `forbidden`. Usage is unlimited during cubic's alpha but rate caps are planned; credentials live in `~/.local/share/cubic/auth.json` — surface the message so the user can tell billing/auth failures from infra glitches.
- **Silent bad-ref trap**: an unresolvable `--base`/`--commit` ref does NOT error — cubic returns `{"issues": []}` with exit 0. An empty result is only trustworthy when the launch prompt's `git rev-parse --verify` guard passed; if the agent reports the guard failed, mark `degraded` (bad ref), never a clean pass.
