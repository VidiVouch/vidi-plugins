# Shared judge contract

A judge is a fresh worker that rules on the candidate findings of one review
round: it confirms, refutes, or leaves open each candidate from evidence in the
frozen snapshot, and returns one judging result (judging.md). Three judge roles
exist, one per model family, so an adjudication panel can compare rulings
across families; a single boss is the same role run once. This file is the one
brief they all share. It exists so that every judge in a panel is asked the
same question on the same inputs, and so that the record of who judged what is
read from definitions and dispatch options rather than from a model's own
account of itself.

## Roles

| Role | Agent definition | Requested model |
| --- | --- | --- |
| judge-fable | `agents/judge-fable.md` | claude-fable-5-1[1m] |
| judge-astra | `agents/judge-astra.md` | gpt-6-astra[1m] |
| judge-grok | `agents/judge-grok.md` | grok-4.7 |

The IDs come from `model-roles.json` and were verified against the proxy
catalog (models.md); `scripts/model_roles.py` reports `agentAgrees: false` when
a definition drifts from the roles file. The three definitions carry the same
body; only the name, description and model differ. judge-grok adjudicates and
grok-reviewer discovers; they are separate lanes with separate definitions and
one never stands in for the other.

## How a judge is selected

Dispatch a judge by its definition and nothing else: the Workflow `agent()`
call or Agent call sets `agentType` to `vidi-skills:judge-<name>` and passes no
`model`. The per-call model parameter is restricted to a family alias
(`sonnet`, `opus`, `haiku`, `fable`), and Claude Code resolves it ahead of the
definition's `model` field; an alias in turn follows the launching profile's
`ANTHROPIC_DEFAULT_*_MODEL`, which in the user's CCS profiles points at other
vendors' models. A per-call override would therefore silently replace the full
ID the definition pins. The deep-review workflow refuses to start a unit that
sets both `agentType` and `model`.

`CLAUDE_CODE_SUBAGENT_MODEL` is a default below the definition and does not
override it. `CLAUDE_CODE_SUBAGENT_MODEL_FORCE` makes Claude Code ignore the
`model` field in every definition, so a judge would run on the session model
under its own name. `scripts/prepare_reviewers.py` reports that switch under
`namedAgents.blocked` with the reason, for every spelling Claude Code reads as
on (`1`, `true`, `yes`, `on`, any casing), and the workflow refuses to start
while it is set, so the setting is never applied silently. Turn the setting off
for the session, or do not run named lanes; do not work around it with a
per-call model.

Fork mode (`CLAUDE_CODE_FORK_SUBAGENT`, on by default since 2.1.302) does not
override a definition and is not a reason to refuse: it only lets Claude start
a fork of its own conversation on its own, and a dispatch by `agentType` is not
a fork, so the judge still starts fresh from its definition with the
definition's model. What the contract forbids is a fork, named or resumed
dispatch of a judge, and the workflow refuses any dispatch option that would
make one.

The requested model of a judge is the definition's `model`. What actually
served the request is recorded separately (`resolvedModel`, `upstreamModel`,
MODEL-3) and stays null until trusted harness or proxy evidence fills it. A
judge's own statement about its identity is not evidence and the definitions
tell judges not to make one.

## What every judge receives

One task message, identical for every judge in the same round:

- the run id, the round number and the 64-hex snapshot id from the run
  manifest, which the result must echo under `input`
- the review root, a frozen worktree at that snapshot, and the instruction to
  read it with `git -C <review root>` and the read-only tools the definition
  allows
- the candidates: a JSON array in which each entry has `id`, `file`,
  `line_start`, `line_end`, `severity`, `sources`, `description` and
  `recommendation`, with ids assigned by the orchestrator from content, never
  from position
- the exclusions: the path of the repository's REVIEW.md and the rule that a
  candidate its skip section covers is `refuted` with that document as evidence
- the rubric: the questions to answer per candidate (does the scenario occur in
  this snapshot, what is its impact, what severity follows) and any run-specific
  focus
- the paths of this contract and of judging.md

Not in the message, by design: other judges' results or reasoning, earlier
rounds' transcripts, fixer summaries, the controller's conversation, or any
instruction to continue earlier work. Independence is what makes agreement
between judges informative; a judge that has read another's verdict is a second
vote for that verdict, not a second examination. A judge that finds any of these
in its brief returns every candidate as `unreviewed` with the reason as a limit.

## What every judge returns

Exactly the shape of `judging-output.schema.json`, through the structured-output
tool: one entry per candidate with the id and anchor unchanged, a disposition
that is one of `confirmed`, `refuted`, `unresolved` or `unreviewed`, and the
scenario, evidence, impact, reason and limits that disposition needs
(judging.md lists them). Before any merge, the orchestrator runs
`scripts/judging.py check --result <file> --candidates <file> --snapshot <id>
--repo <review root>`; a result that fails the check is recorded as no result
from that judge, not downgraded and not read partially.

## Fail closed

- A judge that cannot read the review root, lacks a tool it needs, or runs out
  of budget returns the affected candidates as `unreviewed` with the cause as a
  limit. It does not guess and it does not return an empty findings list.
- A null, invalid or cancelled result is recorded by the orchestrator as a
  missing judge result. It is never read as "no real findings", and it never
  becomes a set of refuted entries.
- No judge is retried on a different model or harness. A replacement judge is
  a new fresh instance of the same definition, dispatched the same way.

## Outside this contract

Panel membership and the opt-in that turns three judges on (REVIEW-2), how
disagreeing results are merged and rebutted (REVIEW-3, REVIEW-4), what happens
when a required judge fails (REVIEW-5), and the run-record identity fields
(MODEL-3) are specified by those tasks. Until REVIEW-2 lands, the workflow's
boss stage keeps its `rulings` shape; judging.md says how to read it through
this contract.
