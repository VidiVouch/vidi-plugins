# Codex native review (opt-in)

Run only when reviewer policy explicitly enables `codex-native` and preparation
finds the Codex CLI. Installation does not prove authentication, quota, or success.
This retains Codex's own native review harness; `astra-standard` uses a different
owned prompt in Claude Code and is not an equivalent implementation by definition.

## Launch prompt

Read `<REPO_ROOT>/.claude/skills/codex-cli/SKILL.md` for event/failure handling.
Do not invoke a slash command or delegate again. Check the installed
`codex exec review --help` before using the structured flags below. CLI help
inspected on 2026-10-31 exposes --commit, --base, --uncommitted, --json,
--output-schema, --output-last-message, --model, and --ephemeral. That establishes
flag availability, not a successful model run or compatibility of every combination.
This spec supersedes older claims that native review has no structured output.

Use explicit `codex -C "<REPO_ROOT>"` targeting and read-only sandboxing:

```bash
codex -C "<REPO_ROOT>" exec --sandbox read-only review \
  --model gpt-6.2-astra --ephemeral --json \
  --output-schema "<absolute path to deep-review/review-output.schema.json>" \
  --output-last-message "<unique durable result path>" \
  <target flag> > "<unique durable JSONL events path>"
```

Use --base <base>, --uncommitted, or --commit <sha> as requested. For range mode,
--base <a> is acceptable only when the worktree HEAD equals <b> and <a> is an
ancestor of <b>, so merge-base and the requested two-dot range agree. Verify
those conditions with explicit `git -C` reads. Otherwise return degraded for
unsupported scope; do not checkout, stash, or silently change targets. Native
committed-target review is not proof that subsequent uncommitted fixes were
reviewed: if the combined repair scope cannot be covered, return degraded.

Do not mix a custom prompt with targeting flags unless that exact combination
has been verified for the installed version. If --focus is nonempty and cannot
be honored by native review's supported invocation, report degraded rather than
claiming the requested scoped review. Do not change to ordinary exec or a direct
agent silently. Preserve logs outside disposable worktrees before cleanup.

## Parsing & degradation

--json is the execution-event stream; --output-schema is the final result
contract. They serve different purposes. Check exit status, fatal error and
turn.failed events, missing final result, JSON/schema validity, and exact scope.
Any unsupported flag, timeout, quota/auth failure, truncated result, or wrong
scope is degraded with no findings. Never use empty stdout as a clean result.

Return the validated final findings/degraded/reason object to the workflow's
structured-output tool, not raw JSONL or a prose summary. A valid completed
findings=[] result is clean. Preserve the native event/result artifacts for
inspection; the wrapper must not invent findings or reinterpret an error as a
code defect. Live native schema/target compatibility remains a rollout check.
