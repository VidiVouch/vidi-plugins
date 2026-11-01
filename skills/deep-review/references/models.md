# Role model IDs and routing evidence

`model-roles.json` names the full model ID for every role the plan requests.
Role definitions (MODEL-2) and run records (MODEL-3) read that file; they do not
carry family aliases, because an alias such as `opus` or `sonnet` resolves to
whatever the launching profile's `ANTHROPIC_DEFAULT_*_MODEL` says, and in the
user's CCS profiles those point at other vendors' models.

## Verified roles

Evidence recorded 2026-11-01 from the live CLIProxy `/v1/models` catalog on
ports 8317 (default) and 8318 (judge variant), CCS 8.10.0, Claude Code 2.1.316.
The catalog advertises an ID; it does not prove tools, structured output,
entitlement, or which upstream model answers. Those stay `resolvedModel: null`
and `upstreamModel: null` until a trusted harness or proxy record fills them.

| Role | Requested ID | Catalog ID advertised | Provider in catalog |
| --- | --- | --- | --- |
| judge-fable | claude-fable-5-1[1m] | claude-fable-5-1 | claude |
| judge-astra | gpt-6-astra[1m] | gpt-6-astra | codex |
| judge-grok | grok-4.7 | grok-4.7 | xai |
| astra-reviewer, astra-adversarial-reviewer | gpt-6-astra | gpt-6-astra | codex |
| grok-reviewer | grok-4.7 | grok-4.7 | xai |
| implementer-opus, fixer-opus | claude-opus-5-5[1m] | claude-opus-5-5 | claude |
| permission-classifier | claude-sonnet-5 | claude-sonnet-5 | claude |

Routes: all three providers are served by the same CLIProxy instance on each
port. The judge variant (8318) runs on the `team@graze.ai` Claude account, the
team variant (8319) on `tom@curinglab.com`; 8319 was not listening when checked,
so its catalog is unverified. Account routing uses fill-first with session
affinity. Which account or upstream answered a request is not visible in the
catalog; MODEL-3 records it from harness and proxy evidence.

## Spelling: gpt-6.1-sol, not gpt-6-1-sol

The catalog advertises `gpt-6.1-sol` (and the older `gpt-6-sol`). It does not
advertise `gpt-6-1-sol`. The hyphen spelling exists only in
`~/.ccs/claude-team.settings.json` as `ANTHROPIC_DEFAULT_SONNET_MODEL`, while the
same file's picker row uses `gpt-6.1-sol[1m]`. CCS normalizes dotted versions to
hyphens only for `claude-*` IDs on Antigravity routes
(`model-id-normalizer.js`), so the hyphen spelling reaches the Codex route
unchanged and is not a served model. Correcting that one value is a
`global-settings` gated change; `model_roles.py --settings` reports it as a near
match rather than fixing it.

## Suffix rules

- `[1m]` at the end of an ID is Claude Code's extended-context marker. Claude Code
  strips it before sending the ID to the provider, and behind a proxy it is the
  only thing that lifts the 200K clamp (CCS `extended-context-utils.js`). The
  catalog is therefore checked for the bare ID. xAI's API rejects the suffix, so
  Grok roles carry none.
- Anything else is part of the ID and must be advertised literally. No
  `gpt-6-astra-fast`, `gpt-6.1-sol-fast`, or `-high` variant is advertised by
  the catalog; `grok-4.7-build-fast` is a distinct advertised model, not a speed
  tier of `grok-4.7`. CCS parses a trailing `-fast` on Codex IDs as a service
  tier, but whether the upstream serves it is unverified and needs an approved
  live call. Do not add a speed suffix to a role until that call is recorded.
- `gpt-6-astra[1m]-fast` (present in the judge and team pickers) puts text after
  the context suffix. Neither Claude Code's documentation nor CCS describes that
  spelling; the helper reports it as an unverified suffix.

## Role definitions and how they are selected

Every role with a packaged definition is dispatched by `agentType`
(`vidi-skills:<agent>`) and never with a per-call `model`. The Agent call's
model parameter takes only a family alias and Claude Code resolves it ahead of
the definition's `model` field, so passing one would replace the pinned ID with
whatever the launching profile maps the alias to. The deep-review workflow
stops a unit that sets both.

| Role | Definition | Model field |
| --- | --- | --- |
| judge-fable, judge-astra, judge-grok | `agents/judge-<name>.md`, one shared body (judge-contract.md) | as in the table above |
| astra-reviewer, astra-adversarial-reviewer, grok-reviewer | `agents/<name>.md` | gpt-6-astra, grok-4.7 |
| implementer-opus | `agents/implementer.md` | claude-opus-5-5[1m] |
| fixer-opus | `agents/fixer.md` | claude-opus-5-5[1m] |
| permission-classifier | none; the env key `CLAUDE_CODE_AUTO_MODE_MODEL` (MODEL-4) | claude-sonnet-5 |

Subagent model precedence, from the Claude Code sub-agents documentation for
2.1.251 and later: the per-invocation parameter, then the definition's
`model`, then `CLAUDE_CODE_SUBAGENT_MODEL`, then the session model. The
environment default therefore cannot override a definition on its own.
`CLAUDE_CODE_SUBAGENT_MODEL_FORCE` (2.1.257 and later) makes Claude Code
ignore the `model` field in every definition and refuse the per-call
parameter. `prepare_reviewers.py` reports it under `namedAgents.blocked` for
every value Claude Code reads as on (`1`, `true`, `yes`, `on`, any casing), the
three callers pass that object to the workflow, and the workflow refuses to
start while it is blocked, so a forced model never runs under a definition's
name. `CLAUDE_CODE_FORK_SUBAGENT` is not part of that check: fork mode, on by
default since 2.1.302, only lets Claude start a fork of its own conversation on
its own, and a non-fork subagent starts fresh from its definition with the
definition's model, so a dispatch by `agentType` is unaffected. `model_roles.py`
reports `agentAgrees: false` when a definition's `model` drifts from the roles
file.

## Re-verifying

```
python3 skills/deep-review/scripts/model_roles.py \
  --settings ~/.ccs/claude-judge.settings.json --probe-proxy
```

The helper reads only model keys and picker rows out of a settings file, sends
the file's token once to its own `ANTHROPIC_BASE_URL` for a single catalog GET,
and prints no credentials. Without `--probe-proxy` it makes no request and
claims nothing. Near matches are evidence for a human; the helper never
substitutes one. A role whose packaged agent definition disagrees with the
roles file is reported with `agentAgrees: false`.
