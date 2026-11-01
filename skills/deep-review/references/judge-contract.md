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
  from position; a candidate the previous round's fixer rebutted instead of
  fixing also carries `rebuttal` (the round, the fixer's reason and its
  evidence), and the brief then says that the objection is a claim to verify,
  not a ruling
- the exclusions: the path of the repository's REVIEW.md and the rule that a
  candidate its skip section covers is `refuted` with that document as evidence
- the rubric: the questions to answer per candidate (does the scenario occur in
  this snapshot, what is its impact, what severity follows) and any run-specific
  focus
- the paths of this contract and of judging.md

Not in the message, by design: other judges' results or reasoning, earlier
rounds' rulings or transcripts, fixer summaries, the controller's conversation,
or any instruction to continue earlier work. Independence is what makes
agreement between judges informative; a judge that has read another's verdict
is a second vote for that verdict, not a second examination. A judge that finds
any of these in its brief returns every candidate as `unreviewed` with the
reason as a limit. A rebuttal is not one of these: it is part of the claim
under examination, carries no judge's verdict, and the judge checks it against
the tree exactly as it checks the candidate's own description.

## What every judge returns

Exactly the shape of `judging-output.schema.json`, through the structured-output
tool: one entry per candidate with the id and anchor unchanged, a disposition
that is one of `confirmed`, `refuted`, `unresolved` or `unreviewed`, and the
scenario, evidence, impact, reason and limits that disposition needs
(judging.md lists them). The workflow checks every result in the run against
the candidates it sent, with the rules of `scripts/judging.py check` short of
the tree: exact shape, the snapshot and round echoed under `input`, one entry
per candidate id at the candidate's own anchor, and the fields each
disposition must carry. The host then runs `scripts/judging.py check --result
<file> --candidates <file> --snapshot <id> --repo <review root>` on the saved
results, which also confirms that every code reference names a file and line
range that exist in the judged tree, something the Workflow runtime cannot
read. A result that fails either check is recorded as no result from that
judge, not downgraded and not read partially.

## Fail closed

- A judge that cannot read the review root, lacks a tool it needs, or runs out
  of budget returns the affected candidates as `unreviewed` with the cause as a
  limit. It does not guess and it does not return an empty findings list.
- A null, invalid or cancelled result is recorded by the orchestrator as a
  missing judge result. It is never read as "no real findings", and it never
  becomes a set of refuted entries.
- No judge is retried on a different model or harness. A replacement judge is
  a new fresh instance of the same definition, dispatched the same way, and
  the next section says when one is dispatched.

## When a judge slot fails

Every judge of the panel is required; there is no optional third. The older
rebuild acceptance panel (rebuild-examine, Phase 5) treats its non-Claude judge
as optional and proceeds two of two when that judge degrades. This panel does
not inherit that fallback: a judge that returns nothing usable is a family the
panel lacks, and a ruling made without it is a different ruling than the one
`--panel` asked for. The workflow therefore does the following, in order, each
round:

- When a slot's instance returns null, is cancelled, or returns a result the
  check refuses, the workflow logs the panel as incomplete, naming the judge
  and the cause, and dispatches one fresh instance of the same definition on
  the same brief, by `agentType` alone. It does this only after every first
  instance has returned, so the replacement receives the identical brief and
  no result can reach it. The first instance stays on record under
  `judgeResults[].replaced[<judge>]` with its raw packet and the error; the
  slot's standing result is under `results[<judge>]`.
- A slot filled by its replacement is a complete slot: its status is `ok`, the
  merge runs on three checked results, and nothing about the failed first
  instance is hidden, because `replaced` and the dispatch record keep it.
- A slot still empty after the replacement is not filled again. Two fresh
  instances of one definition failing on the same brief is a capability
  problem (the model, the proxy, the budget) that the attempt series handles
  (run-lifecycle.md), not something a third call in the same round would
  answer, and every call is a paid one. By default the round's candidates are
  all `unruled` with `missing` for that judge in `dispositions`, the judge's
  key is `degraded` with both errors, and the run is incomplete: nothing is
  fixed or cleared on a partial panel.
- Ruling with the two remaining judges is reduced coverage and requires
  explicit authorization in the request: the argument `reducedPanel` (SKILL.md
  `--reduced-panel "<text>"`), carrying the user's own words that allow a
  two-judge ruling and where they were given (approvals.md, "What counts as
  authorization"). No agent supplies that text, and no result, report or
  completed review stands in for it. With it registered and exactly one slot
  empty, the two remaining judges rule by the merge below, each candidate's
  `dispositions` says `missing` for the absent judge, the judge's key is
  `reduced coverage: ...` under `degraded` (the report shows it as absent
  data, never as a judge that ruled), and the run is not incomplete for it.
  `run_gate.py finalize` accepts a `judge:` entry under `degraded` only when
  the manifest registered `reducedPanel` and the result echoes the same text,
  so a result cannot claim an authorization the request did not carry, and an
  acceptance under reduced coverage is never reused for a request without it
  because the authorization is part of the rubric.
- Two empty slots are never a panel, authorization or not. One judge is a boss
  run; request one with `--boss`. A judge the proxy does not advertise before
  the run is not a failed slot either: the workflow still refuses to start
  (The opt-in), because a panel known to be short a family is a different
  request, and `reducedPanel` covers a judge that was available and failed.

## The opt-in

The panel is off by default and turned on per run with `--panel` (deep-review
SKILL.md). It costs three judgments per round, so it is for decisions worth a
cross-family comparison, not for every small diff. The flag does three things:

- `prepare_reviewers.py --panel` adds `judges` to its output: for each of the
  three definitions, the `agentType`, the `requestedModel` from the
  definition, whether the proxy catalog advertises the bare ID (the `[1m]`
  marker is stripped before the ID reaches the provider, models.md),
  `catalogModel`, the reason, and `resolvedModel` and `upstreamModel` as null.
  Without the flag `judges` is null and the workflow runs its boss stage.
- The workflow receives that object as `judges` and refuses to start when it
  names anything but the three roles, when any judge is unavailable, when a
  definition lacks catalog evidence, or when `boss` is also given. A panel that
  quietly ran with two families, or a boss and a panel over the same
  candidates, is not the mode that was asked for. The run manifest records
  the requested judges under `models.judges` with an empty boss list, and
  finalize refuses a result whose judges differ from it.
- Each round, the workflow derives every candidate's id from its content,
  builds one brief, dispatches the three judges on it in parallel by
  `agentType` alone, checks each result as described above, replaces a failed
  judge once (When a judge slot fails), and keeps the candidates, every
  checked result or error and every replaced first instance under
  `judgeResults`.

## Merging and rebuttals

The three checked results are merged per candidate from their evidence, never
by count, because a vote cannot tell a judge that traced a defect from two that
did not look closely enough. Only `confirmed` and `refuted` entries are
verdicts: each is an examination with evidence the checker required. An
`unresolved` or `unreviewed` entry is recorded in the finding's `dispositions`
and the merged reason but carries no verdict, so it neither confirms, refutes
nor blocks. The rules, in order:

- A judge still missing after its one replacement leaves every candidate
  unruled, the run incomplete and nothing fixed or cleared, unless the request
  registered `reducedPanel` and exactly one judge is missing, in which case the
  rules below run on the two remaining results (When a judge slot fails).
- A confirmation beside a refutation is conflicting evidence. The candidate
  stays unruled and open, with both sides in the record, the loop stops and the
  run is incomplete. A demonstrated defect is not outvoted by refutations and a
  refutation is not overruled by a majority of confirmations; a conflict is for
  a reader of both sides, not for a count.
- Confirmations with no refutation confirm, at the highest severity any
  confirming judge gave, so a judge that demonstrated a blocker is not
  downgraded by one that rated it lower or could not decide.
- Refutations with no confirmation refute, with every judge's reason kept, so a
  claim nobody could demonstrate is not confirmed by default.
- No verdict at all (every entry unresolved or unreviewed) leaves the candidate
  unruled and the run incomplete; an undecided claim is neither confirmed nor
  cleared.

A fixer that finds a confirmed candidate false does not fix it: it returns the
objection under `rebuttals` in its fix packet, naming the finding's id with a
reason and evidence in the shape a judge's evidence takes, once per finding per
run. The next round judges that candidate again with the objection attached,
whether or not a reviewer re-detected it, so the rebuttal is answered by fresh
judges rather than by sending a second fixer for the same claim. Judges that
uphold it refute the finding with their reasons; judges that confirm over it
send it back to a fixer that now sees both the rebuttal and the ruling and may
not rebut again. This is why a false claim dies in one fix step instead of
costing a round per fixer that declines it.

## Outside this contract

The run-record identity fields `resolvedModel` and `upstreamModel` (MODEL-3)
are specified by that task and stay null until it lands.

Without `--panel` one boss adjudicates and the boss stage keeps its `rulings`
shape; judging.md says how to read it through this contract. The earlier
two-model boss form (one boss per domain, merged by taking the voice of
whichever boss the finding's self-reported domain pointed at, with any third
entry ignored) is retired and refused at the start by the workflow and by
`run_manifest.py`: a list of bosses was neither one arbiter nor a panel, and
it must never be read as a consensus of three. The panel is the one
multi-judge mode, selected by definition and merged from identical records;
the alias-selected boss was not generalized into a second one.
