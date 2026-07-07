---
name: codex-cli
description: Shared mechanics for invoking the Codex CLI (gpt-5.5) reliably — repo targeting, JSONL output parsing, timeouts, sandboxing, and failure-vs-clean detection. Read this before running any `codex` command; the codex-review and codex-computer-use skills build on it.
---

# Codex CLI mechanics

gpt-5.5 is only reachable through the Codex CLI (`~/.codex/config.toml` defaults
to gpt-5.5). Three invocation paths:

- `codex exec` — non-interactive agent run with a self-contained prompt
  (implementation, investigation, data analysis). Use `-s read-only` for
  anything that shouldn't edit files.
- `codex exec review` — the native reviewer; the review prompt is baked into
  the binary. Review-specific mechanics live in the codex-review skill.
- `codex-companion.mjs` — the Codex plugin's app-server wrapper; owns the
  adversarial review prompt and JSON-schema output (used by deep-review
  Agent C). Resolve it with
  `ls "$HOME/.claude/plugins/cache/openai-codex/codex/"*/scripts/codex-companion.mjs | sort -V | tail -1`.

## Repo targeting

Never rely on the shell's cwd. The Bash tool spawns a fresh shell per call, so
a `cd` from a previous call doesn't persist — a worktree-spawned codex run can
silently retarget the main repo and operate on the wrong tree.

- Resolve the target once: `REPO_ROOT=$(git rev-parse --show-toplevel)`.
- Pass `-C "$REPO_ROOT"` to `codex` / `codex exec`, positioned BEFORE any
  subcommand (it's a top-level flag). The companion script takes
  `--cwd "$REPO_ROOT"` instead.

## Timeouts and long runs

Codex runs routinely exceed the Bash tool's 10-minute default timeout. Either
pass an explicit `timeout` on the Bash call, or run codex in the background
writing to a report file and poll for completion. Prefer the background +
report-file pattern for anything expected to take more than a few minutes.

## Structured output

Prefer `--json` wherever the subcommand supports it: it emits a JSONL event
stream on stdout instead of human-facing prose. Parse events, not prose —
plain stdout mixes the payload with progress chatter and hides errors. Event
types that matter:

- `{"type":"error", ...}` — fatal error, `message` field carries the reason
- `{"type":"turn.failed", ...}` — turn aborted, reason in `error.message`
- `item.completed` events — carry the actual payloads

## Failure is not a clean pass

A codex run that crashed, hit quota, or targeted the wrong directory produced
**no data** — never present its empty output as "no findings" or "success".
Check these signals in order, stopping at the first match:

1. **Top-level `error` event** in the JSONL stream → degraded; surface `message`.
2. **`turn.failed` event** → degraded; surface `error.message`.
3. **Missing payload event** — the stream ended without the `item.completed`
   event the subcommand should produce → degraded (no data).
4. **Exit code + keyword fallback** — codex exits 1 on terminal failure vs. 0
   for a clean run. As a last resort, lowercase the output and check for:
   "usage limit", "hit your usage limit", "upgrade to Pro", "try again at",
   "rate limit", "rate limited", "quota", "quota exceeded", "over quota",
   "insufficient quota", "too many requests", "429".

Codex flattens the underlying reason (quota / auth / credit depletion) into
free-text `message` fields — the structured events reliably detect *that* a
failure happened; classifying *why* requires reading the message text.

## Parallel runs

Parallel codex agents that can edit files must each run in their own git
worktree (`isolation: 'worktree'` for wrapper agents) so edits don't collide
in the shared checkout. Read-only runs can share a checkout.

## Inside workflows and subagents

The Agent/Workflow `model` parameter only takes Claude models, so codex runs
inside workflows go through a thin Claude wrapper agent — see the "Using
gpt-5.5 inside workflows and subagents" section of CLAUDE.md for the wrapper
pattern, labeling convention, and budget caveats.

## Verified behaviors

Record observed CLI behavior here with the version it was seen on, the way
deep-review's reviewer specs do — unverified command shapes copied from
examples are how broken invocations creep in.

- **codex-cli 0.142.5** (2026-07-06): a custom `[PROMPT]` argument to
  `codex exec review` is mutually exclusive with `--base` and `--uncommitted`
  (arg-parse error: "the argument '--base <BRANCH>' cannot be used with
  '[PROMPT]'"). The native baked-in review prompt is what runs whenever a
  targeting flag is passed; custom review stances need the companion script's
  adversarial path instead.
- **codex-cli 0.142.5**: both `codex review` and `codex exec review` exist and
  are non-interactive; this repo standardizes on `codex exec review --json`
  (the battle-tested shape from deep-review).
