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

## Permission classifier: an independent route

In auto mode Claude Code sends a separate request to a classifier model
before a shell command, network call or protected-path edit runs. That
request has no agent definition, so nothing in this plugin pins its model.
The permission-modes documentation (read 2026-11-01) sets the rules the check
below relies on:

- `CLAUDE_CODE_AUTO_MODE_MODEL=<full ID>` picks the classifier model from
  Claude Code v2.1.302. Without it the default is Claude Sonnet 5, or the
  value of `ANTHROPIC_DEFAULT_SONNET_MODEL` when that is set. In the team
  profile that variable points at a GPT model, so an unset override there
  would send the classifier to Codex; the judge profile does not set it. The
  roles file names `claude-sonnet-5`.
- An Anthropic-configured classifier model takes precedence over the
  variable; the debug log reports it. A model the provider cannot serve makes
  every checked action fail closed.
- With `ANTHROPIC_BASE_URL` pointing at a proxy, Claude Code v2.1.278 and
  later first asks the server to review as part of the session's own
  requests. A proxy that drops that review makes Claude Code fall back to its
  own classifier requests. `CLAUDE_CODE_AUTO_MODE_SERVER=0` skips the server
  and always sends the client-initiated request; it does not turn
  classification off.
- The debug file written by `claude --permission-mode auto --debug-file <log>`
  records `classifier_request_started` with `model=<id>` per request, and
  `ignoring CLAUDE_CODE_AUTO_MODE_MODEL` with the reason when the variable
  was not honoured. Reads and working-directory edits skip the classifier,
  so a session that only reads sends no request and proves nothing.

`scripts/classifier_check.py plan --settings <profile> [--probe-proxy]`
reads the model keys of a CCS or Claude settings file and says whether an
approved live check could request the intended model: override present and
equal to the roles file, server review off, Claude Code new enough, and
(with the probe) the ID advertised by that profile's catalog. It prints the
launch command and runs nothing. `evidence --debug-log <log>
[--proxy-logs ~/.ccs/cliproxy/logs]` reads the log that check wrote and
reports one of: `none` (no request sent; not a test), `mismatch` (another
model requested or the override ignored), `failed` (requests failed, actions
denied unreviewed), `requested` (every request named the intended model;
proxy evidence absent), `served` (the proxy also received that model and
its upstream answered with it). Only `served` meets the plan's criterion,
and it needs CLIProxy's request log, which is off by default and a
`global-settings` change to turn on. The live check itself is a `paid-calls`
action: one short auto-mode session on the team profile with one shell
command, so a handful of classifier requests.

## Auto mode eligibility: the session model decides, not the classifier

Choosing the classifier model and being allowed into auto mode are two
different rules on the permission-modes page (read 2026-11-01), and only the
second decides whether a session runs with the classifier at all:

- Availability is listed by session model. On the Anthropic API: Claude Opus
  4.6 or later, Sonnet 4.6 or later, Haiku 5.5, or a Fable model. On Bedrock,
  Agent Platform, Foundry and the apps gateway: Sonnet 5 or later, Opus 4.7
  or later, Haiku 5.5 and the Fable models. "Older models, including Sonnet
  4.5, Opus 4.5, Haiku 4.5, and claude-3 models, are not supported on any
  provider." No other vendor's model appears in either list, and the LLM
  gateway page says Anthropic "doesn't support routing Claude Code to
  non-Claude models through any gateway".
- The fallback is Manual, silently. "When the flag, a settings file, or the
  built-in default selects `auto` but auto mode isn't available to the
  session, Claude Code starts the session in Manual instead", and the reasons
  include "a model that doesn't support it". `--permission-mode auto` does
  not override that. In a `-p` run Manual means every tool call outside an
  allow rule is denied, so a non-interactive worker on an ineligible model
  does not prompt, it fails its commands.
- `CLAUDE_CODE_AUTO_MODE_MODEL` is not in the availability requirements; it
  only picks which model answers the classifier requests of a session that
  already has auto mode. Pinning `claude-sonnet-5` therefore never makes a
  `gpt-6-astra` or `grok-4.7` session eligible, and the helper reports that
  pin as `affectsEligibility: false`.
- A subagent runs under the launching session's permission mode. The
  page's "How auto mode handles subagents" accordion says each subagent
  action "goes through the same decision order as in the parent session,
  with the same block and allow rules" and that "any `permissionMode` in the
  subagent's frontmatter is ignored"; it says nothing about the subagent's
  model, and eligibility is stated only for the session model. So a GPT or
  Grok worker dispatched from a Fable session is reviewed by that session's
  classifier like any other tool call, and a GPT or Grok main model is what
  loses auto mode.

`scripts/classifier_check.py eligibility --settings <profile>` reads the
session model a settings file would launch with (`ANTHROPIC_MODEL`, then the
`model` setting, then `ANTHROPIC_DEFAULT_MODEL`; an alias follows the file's
`ANTHROPIC_DEFAULT_<FAMILY>_MODEL`), grades it and every picker row against
the lists above, and prints `expectedStart`: `auto`, `manual`, or `unknown`
when the file sets no model or an alias Claude Code resolves elsewhere. It
also lists the roles whose provider is not `claude`. Exit 0 only for `auto`.

Recorded on this machine (2026-11-01, model keys only): the team profile
(`claude-fable-5-1[1m]`) is eligible, and so are the user settings while
their `model` alias is `fable`; that alias follows `/model`, and once it read
`opus` the same file graded `unknown`, because the helper never guesses what
Claude Code resolves a bare alias to. The
judge profile (`gpt-6-astra`), the codex profile (`gpt-6-astra[1m]`) and the
xai profile (`grok-4.7`) are expected to start in Manual whatever classifier
they pin, and switching a team session to a GPT or Grok picker row would put
it in the same position. So a judge-profile session is not a way to run the
Astra judge in auto mode: run the panel from a Fable or Opus session and
dispatch the judges as subagents, which is what the workflow does. The
Manual fallback itself has not been observed live here; confirming it is one
launch of `claude --settings ~/.ccs/claude-judge.settings.json
--permission-mode auto` in a terminal and reading the mode indicator or
`/status` before sending anything, which costs no model call. The `-p` form
with a shell command instead spends one turn on the GPT model and is a
`paid-calls` action.
A proxy session has no row of its own in the availability list; the helper
applies the Anthropic API list and names the stricter floor for Opus 4.6 and
Sonnet 4.6, the only IDs the two lists disagree on.

## Run-record identity: three sources

A unit's model is recorded three times, from three sources that never stand in
for each other:

- `requestedModel` is what the dispatch asked for. For a dispatch by
  `agentType` it is the definition's `model`, a full ID (`pinned: true`); for
  a per-call alias such as `opus`, or the inherited session model, it is that
  alias (`pinned: false`), because the harness resolves an alias and the
  request pins nothing. The workflow writes this into every entry of
  `admission.dispatches` (`agentType`, `model`).
- `resolvedModel` is what Claude Code's own transcript for the unit says. The
  runtime journal maps each dispatch label to an agent id, and that agent's
  transcript records per API call the model sent (`api-request.params.model`,
  `assistant.requestedModel`) and the model the response named
  (`assistant.message.model`). Only those fields are read; message content is
  never opened, so a model's description of itself cannot reach the record.
- `upstreamModel` is what the proxy's request log says it received and what
  its upstream answered with, joined to the transcript by the response message
  id. CLIProxy writes that log only with `request-log: true`, a settings change
  (approvals.md, `global-settings`). Without it the field stays null and the
  record says why.

`scripts/model_evidence.py` fills the record after a run:

```
python3 skills/deep-review/scripts/model_evidence.py \
  --result "$RUN_DIR/result.json" --workflow-dir <transcript directory of the Workflow run> \
  --proxy-logs ~/.ccs/cliproxy/logs --out "$RUN_DIR/model-evidence.json"
```

The workflow result itself keeps `resolvedModel` and `upstreamModel` null; the
evidence file beside it carries the filled values with their sources. A unit is
`verified` when every source agrees with the request (`[1m]` is stripped on
both sides, since the provider never sees it), `fallback` when any source
disagrees (another ID sent or answered, more than one model answering, the
runtime running a different definition than the one selected, the proxy
receiving or answering with something else), and `unverified` when it has no
transcript or no answered turn. The label only locates the transcript: a unit
labelled `judge-grok#1` whose transcript names `claude-fable-5-1` is a
fallback, not a Grok ruling.

A model-comparison trial (a `--panel` run read as a cross-family comparison, a
deep-eval baseline against a variant, a routing smoke) is `valid` only when
every unit that ran is verified. One fallback makes it `invalid` and one
unverified unit makes it `unverified`; the helper exits nonzero for anything
but `valid`, so a comparison harness cannot count a substituted model under the
requested name. `upstreamVerified` is reported separately and is true only when
the proxy log covered every unit without disagreement; a comparison between
providers behind the proxy should require it once the log is on.

Why the rule exists: subagent transcripts on this machine already show the
harness sending `grok-4.5` and the response naming `grok-4.5-build` across
more than a hundred turns, a substitution nothing in a run result would have
shown. A trial that counted those turns as Grok 4.5 would have compared a model
that never answered.
