---
name: codex-cli
description: Shared mechanics for invoking the Codex CLI (gpt-5.6-terra, gpt-5.6-sol, or gpt-6-astra for critical design work and decisions) reliably — repo targeting, JSONL output parsing, timeouts, sandboxing, and failure-vs-clean detection. Read this before running any `codex` command; the codex-review and codex-computer-use skills build on it.
---

# Codex CLI mechanics

This skill covers the Codex CLI harness, not every route to its underlying
models. A configured compatible proxy can also serve Astra directly to a named
Claude Code agent; that does not reproduce Codex's native review behavior. Do not
assume the user's CLI default or proxy catalog. Pass an explicit supported model
when the task requires one: gpt-5.6-terra for routine work, gpt-5.6-sol for larger
tasks, and gpt-6-astra for critical decisions, subject to actual availability.
The historical gpt-6-terra/gpt-6-sol names below were rejected, not aliases.
Three CLI invocation paths:

- `codex exec` — non-interactive agent run with a self-contained prompt
  (implementation, investigation, data analysis). Use `-s read-only` for
  anything that shouldn't edit files.
- `codex exec review` — the native reviewer; the review prompt is baked into
  the binary. Review-specific mechanics live in the codex-review skill.
- `codex-companion.mjs` — the Codex plugin's app-server wrapper; owns the
  adversarial review prompt and JSON-schema output (used by deep-review
  Agent C). Resolve it with
  `ls "$HOME/.claude/plugins/cache/openai-codex/codex/"*/scripts/codex-companion.mjs | sort -V | tail -1`.

Codex runs are metered. A run the user asked for in the current request is
authorized by that request. Evaluation campaigns, repeated trials, model
comparisons, extra reviewers, and smoke tests the task did not name need their
own approval first; see deep-review/references/approvals.md, gate `paid-calls`.
Inexpensive is not free, and `--dangerously-bypass-approvals-and-sandbox` is
never the answer to a closed gate.

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

A named agent definition can select a full provider model ID through the
configured proxy. Select its agent type and omit the per-call alias override.
Deep-review uses this for its Astra standard/adversarial and Grok discovery lanes.
Their structured results come from the workflow's schema contract, not Codex.

A wrapper is still used when deliberately invoking the Codex CLI harness from a
workflow. Keep that role mechanical, preserve the CLI's result and failure
signals, and never substitute direct model review silently. Native Codex and the
legacy companion are separate opt-in lanes in deep-review.

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
- **codex-cli 0.153.4** (2026-09-06): `--model gpt-6-astra`, `--model gpt-5.6-terra`, and
  `--model gpt-5.6-sol` all verified working (`codex exec -s read-only --model
  <m> --json` returned a clean `turn.completed`; workspace-write also verified
  for sol). On 0.142.5 the same names were rejected server-side (400: "requires
  a newer version of Codex") — if either model errors that way, run
  `codex update` first (the real subcommand — `codex upgrade` does not exist).
- **codex-cli 0.153.4** (2026-09-06): `gpt-6-terra` and `gpt-6-sol` do not
  exist. Both emit a "Model metadata for `<name>` not found" item and then fail
  with HTTP 400 "The '<name>' model is not supported when using Codex with a
  ChatGPT account". `gpt-6-astra` is the only gpt-6 name that resolves.
- **Environment-dependent failure** (observed 2026-07-11 on another machine,
  NOT reproducible on this one at 0.144.1): `codex exec -m gpt-6-astra` can die
  with `failed to initialize in-process app-server client: Operation not
  permitted` under the codex sandbox — the gpt-6 and gpt-5.6 models are served via an
  in-process app-server that some sandbox configurations block. This is a
  degraded run (no data), never a clean pass. Recovery order: `codex update`;
  retry; if it persists, fall back to the CLI default model or have the user
  run the command unsandboxed themselves — do NOT reach for
  `--dangerously-bypass-approvals-and-sandbox`.
