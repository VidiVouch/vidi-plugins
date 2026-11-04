# Reviewer configuration and migration

## Shipped lanes

| Key | Default policy | Execution |
| --- | --- | --- |
| claude-bugs | required | Existing opus-alias agent |
| claude-conventions | required | Existing sonnet-alias agent |
| astra-standard | optional | vidi-skills:astra-reviewer, model gpt-6-astra |
| astra-adversarial | optional | vidi-skills:astra-adversarial-reviewer, model gpt-6-astra |
| grok | optional | vidi-skills:grok-reviewer, model grok-4.7 |
| codex-native | disabled | Sonnet wrapper running the native Codex CLI reviewer |
| codex-adversarial | disabled | Sonnet wrapper running the legacy Codex companion |
| cubic | disabled | Sonnet wrapper running Cubic CLI |
| coderabbit | disabled | Sonnet wrapper running CodeRabbit CLI |

Model IDs for direct agents live in packaged `agents/*.md`, not family aliases.
The preparation helper reads those definitions; `model-roles.json` and models.md
hold the verified full IDs for every role, judges and implementers included.
No context-window or speed suffix
is assumed: the exact configured ID must appear in the proxy catalog. Changing
an ID requires changing its definition and rechecking the route. Local aliases
can still remap the existing Claude lanes; their names do not prove model identity.
The direct agent definitions restrict tools to file inspection and selected Git
inspection commands. This is not a replacement for the host permission/sandbox
boundary or proof of upstream tool compatibility.

Standard and adversarial Astra reviews are independent instances with distinct
prompts, not independent model families. They use our owned review instructions
inside Claude Code; neither claims to reproduce Codex's native harness. Grok is
also a direct discovery reviewer, not a boss or a member of an adjudication panel.

## Policy overrides

The helper merges shipped `reviewer-defaults.json` with the live repository's
`.claude/deep-review.json`, or the explicit `--reviewer-config <path>` file instead.
Each override replaces one complete policy object. Unknown keys, policies, or
fields are errors. No network call or wrapper is launched for a disabled lane.

```json
{
  "grok": {"policy": "required"},
  "cubic": {"policy": "disabled", "reason": "Subscription expired"},
  "coderabbit": {"policy": "disabled", "reason": "Subscription expired"}
}
```

- `disabled`: do not probe or dispatch; report the explicit skip reason.
- `optional`: attempt when the cheap capability check succeeds. Unavailability
  or failure reduces coverage, without itself invalidating required lanes.
- `required`: failed preflight prevents review dispatch; failed execution stops
  the fixing loop and makes the result incomplete. Never silently substitute.

To request Codex's native harness, explicitly set `codex-native` to optional or
required. Disable astra-standard if replacing it rather than intentionally
requesting both. The legacy companion requires its own explicit enablement.
Do not enable subscription-backed reviewers because their binaries are installed.
The shipped defaults disable them with a portable reason and carry no account
state: an expired or renewed subscription is recorded in the per-checkout
override, as in the example above, so the plugin never embeds one user's billing
facts and a fresh install needs no edit to stay safe.
Renewal, billing changes, and live configuration edits require user approval;
approvals.md defines the gates (`skill-enablement`, `paid-calls`, `global-settings`)
and what counts as authorization.

## Capability checks and failure

`scripts/prepare_reviewers.py --repo <absolute root> --probe-proxy` reads policy,
then performs at most one bounded GET to the configured proxy's `/v1/models`.
It uses `ANTHROPIC_BASE_URL` and `ANTHROPIC_AUTH_TOKEN` (or ANTHROPIC_API_KEY), does
not follow redirects or print credentials, and requires HTTPS for remote hosts.
No inference is performed. Without `--probe-proxy`, direct-lane availability is
unknown and the helper makes no network request. Disabled CLI lanes are not probed.

The catalog is only evidence that an ID is advertised. It does not prove tools,
StructuredOutput, entitlement, or actual upstream identity. If the catalog is
unsupported, report unavailable; do not guess a route or fall back to an alias.
A forced subagent model (`CLAUDE_CODE_SUBAGENT_MODEL_FORCE` on, in any spelling
Claude Code accepts) blocks the direct lanes rather than silently changing their
identity. The helper also returns it as `namedAgents` (`blocked`, `reason`);
callers pass that object through, and the workflow refuses to start while it is
blocked, because the fixer, courier, judge and implementer definitions pin their
models the same way (judge-contract.md, "How a judge is selected"). Fork mode
(`CLAUDE_CODE_FORK_SUBAGENT`, on by default) is not checked: it only lets Claude
start forks on its own, and a dispatch by `agentType` starts fresh from its
definition regardless.

With `--panel` the helper also returns `judges`: the same catalog evidence for
the three judge definitions (`judge-fable`, `judge-astra`, `judge-grok`) that a
`--panel` run dispatches instead of the boss, each with `agentType`,
`requestedModel`, `available`, `catalogModel` and a reason. The catalog is
checked for the bare ID because Claude Code strips the `[1m]` marker before the
ID reaches the provider. Without the flag `judges` is null. Requesting the panel
changes `reviewerFingerprint`, and the workflow refuses a panel with any judge
unavailable (judge-contract.md, "The opt-in").

An enabled CLI's presence establishes only local installation. No trustworthy
subscription endpoint has been integrated for Cubic or CodeRabbit. Authentication,
quota, and entitlement remain unknown until an explicitly enabled review runs.
The service adapter must report those failures, not emit an empty clean review.

Workflow agent(schema=...) uses the runtime StructuredOutput tool. The workflow
also validates envelope types, severities, relative paths, positive line ranges,
and descriptions before aggregation. Null, malformed, partial, and degraded
results cannot contribute findings. Real clean results require a valid completed
response. Every attempt is fresh; no runtime instance name, fork, or resume is
used. Terminal runtime failures are surfaced, not retried through another model.

## Result contract

Legacy fields remain: mode, rounds, fixed, boss, attempted, degraded, confirmed.
Under `--panel`, `boss` is empty and `judges` names the three judge definitions
with their requested models; `judgeResults` holds, per round, the candidates
every judge was given, each judge's checked result or the error that made it no
result, and under `replaced` the first instance a fresh one took over from.
`reducedPanel` echoes the request's authorization for a two-judge ruling, or
null; a judge still missing after its replacement is `degraded` under its key,
as `reduced coverage:` when that authorization let the other two rule and as
`degraded:` when the run stopped incomplete instead (judge-contract.md, "When a
judge slot fails").
`attempted` now means actually dispatched, not merely configured. `fixed` records
that fixer work was attempted and may have left edits, not proof of a passing fix.

Additional fields:

- `successful`: reviewers completing the last attempted round successfully.
- `skipped`: disabled/unavailable/not-started lanes with policy and reason.
- `coverage`: policy, final state, reason, agent type and identity evidence by key.
- `attempts`: per-round success/failure history; a later success does not erase it.
- `incomplete`: required review/gate failure, no successful reviews, or unverified
  final fixes. Consumers must block acceptance and further automatic fixing.
- `coverageComplete`: all enabled reviewers succeeded. Optional gaps make it false.
- `run`: immutable run ID, starting snapshot ID, and manifest path.
- `reviewResults`: full successful reviewer payloads for durable recovery.
- `admission`: limits, actual starts, stop reason, budget availability, and dispatch states.
- `currentFindings`: the last review's findings; confirmed remains historical.
- `requiresPostFixReview`: code changed, so a fresh read-only snapshot review is required.
- `acceptance`: pending-external-verification; only run_gate.py finalization establishes acceptance.

requestedModel/catalogModel are not resolvedModel/upstreamModel. The latter stay
null in the result; `scripts/model_evidence.py` fills them afterwards from the
harness transcript and the proxy request log into `model-evidence.json` beside
the result (models.md, "Run-record identity: three sources"). Never fill them
from a reviewer's self-description. Disabled or skipped lanes never count as
attestation.

## Migration and verification

Every caller must now initialize and verify a run manifest, then pass its full
generated arguments, including run identity. Calls without it fail before dispatch.
The old tools booleans never auto-enable external services. Maintained callers pass
the preparation helper's reviewers/tools objects and an absolute plugin spec
directory. See run-lifecycle.md for measured gates and recovery. Agent definitions
are resolved from the installed plugin, not copied into the target repository.
Reload the plugin
in a fresh session after approved deployment; adding files does not register them
in an already-running session.

Use deterministic mocked tests first. Before promoting direct lanes to required,
run an approved smoke test for route identity, Git/file tools, StructuredOutput,
valid empty results, unavailable models, quota, timeout, and malformed results.
Compare standard and adversarial lanes separately against their previous harness.
The native Codex output-schema capability and companion replacement need their
own public-CLI compatibility checks; this change does not establish model-quality
parity or remove the legacy adapter.
