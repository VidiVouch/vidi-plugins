# Work-unit contract

One work unit is the whole brief a fresh worker gets: a review reviewer, a
rebuild epoch implementer, a plan task implementer or its verifier. The worker
starts from its agent definition and this contract, nothing else. It does not
get the parent conversation, sibling transcripts, the ledger, or the whole plan,
because those make workers inherit stale context and invent interfaces that
only match the chat they happened to read. The contract is one JSON file whose
shape is `skills/deep-review/work-unit.schema.json`; `scripts/work_unit.py`
builds and checks it. Review and rebuild use the same shape so a worker moved
from one to the other reads the same sections in the same places.

## Sections

- `purpose`: the work statement, the acceptance criterion, the packet mode and
  the plan section's context. The work statement is the packet's `Work`; a
  component packet has none, so its `Component` and `Historical need` are
  joined into one instead. Acceptance is `Done when` or `Acceptance check`. A
  packet missing either one is refused; a worker cannot stop correctly without
  both.
- `input`: the exact snapshot the worker starts from: repository path, HEAD,
  index digest, the snapshot id `run_manifest.py` would compute, and the digest
  of the packet. Capture it before dispatch (`work_ledger.py snapshot`), because
  after the worker finishes the tree is the output, not the input.
- `allowed`: `files` the worker may change, `actions` it may take (stage,
  commit on the current branch, spawn agents), and `forbidden` actions. These
  come from the run's shared rules, not from the task, so every worker in the
  run has the same boundary.
- `decisions`: binding decisions already made for the run or by accepted
  dependencies. A worker does not re-litigate them; if one blocks the task it
  reports that under open obligations.
- `contracts.shared`: paths and digests of the shared API and product contracts
  every worker must build against (output schemas, reviewer policy, gate IDs,
  reference documents). `validate` refuses the contract when any of those files
  changed, so two workers cannot quietly work against different revisions.
- `contracts.dependencies`: for each dependency task, the ledger's recorded
  outcome: state, summary, note paths, open obligations and gated actions. This
  is the projection `work_ledger.py packet` prints, and nothing about other
  tasks.
- `evidence`: the directory outside the repository that holds this attempt's
  note, structured result and the contract file itself. The contract is written
  there at build time, so a restarted controller can show what the worker was
  told.
- `output`: the schema of the structured result the worker returns, inlined and
  pinned by path and digest. A worker's result is checked against this, not
  against whatever shape its prose implies. When the shared document names no
  `output`, a task unit gets `hand-back.schema.json`, the short hand-back that
  `hand-back.md` describes; review units pin their findings schema instead.
- `stop`: `when` lists the conditions that end the unit (criteria met and
  checks pass; a closed gate; work that belongs to another task), and `gates`
  lists the approval gate IDs from `approvals.md` the worker must stop at rather
  than route around.

## Building one

The run owner writes one shared document per run and reuses it for every unit:

```json
{
  "allowed": {"files": ["skills/**", "tests/**"], "actions": ["stage", "commit on the current branch"],
              "forbidden": ["push", "stash", "reset --hard", "checkout another branch"]},
  "decisions": ["Reuse run_manifest.py identity for every snapshot."],
  "contracts": ["/abs/skills/deep-review/references/approvals.md", "/abs/skills/deep-review/review-output.schema.json"],
  "stop": {"when": ["Every acceptance criterion is met and the registered checks pass."],
           "gates": ["commits-pushes", "hosted-reports"]}
}
```

```bash
python3 "$SCRIPTS/work_ledger.py" snapshot --repo "$REPO" > "$RUN_DIR/tasks/$ID/input-$N.json"
python3 "$SCRIPTS/work_unit.py" build --ledger "$RUN_DIR/ledger.json" --task "$ID" --attempt "$N" \
  --role implementer --packets "$RUN_DIR/packets" --shared "$RUN_DIR/shared.json" \
  --evidence-root "$RUN_DIR" --input "$RUN_DIR/tasks/$ID/input-$N.json"
python3 "$SCRIPTS/work_unit.py" validate --contract "$RUN_DIR/tasks/$ID/contract-implementer-$N.json"
```

`build` first refuses if any unit in the ledger is open, whether it is still
running or has finished without passing its cleanup barrier (`work_ledger.py
close`, described in work-ledger.md), because a new unit must not start while
a worker or its child processes may still be active. It then
reads the task's packet and dependency outcomes through the ledger, so
the packet digest is checked against `init`, merges the shared document,
records the unit as started in the ledger (`--owner` names the dispatcher,
`--timeout` sets the deadline after which it counts as overdue), writes
`tasks/<ID>/contract-<role>-<N>.json` read-only, refuses to overwrite an earlier
attempt's contract, and prints the contract. Hand the printed contract to the
worker as its brief. From that moment the ledger shows the unit as `running`,
and a second build of the same attempt is refused by that record even if the
contract file is gone; a retry is built only after the old attempt is closed.
The evidence root must be outside the repository, the stop
gates must be the five IDs from `approvals.md`, and the contract is run through
the same credential filter as run manifests.

## How review and rebuild map onto it

In deep-review a unit is one reviewer or one fix bucket: `purpose` is the review
or fix prompt and the degraded-run rule, `input` is the frozen worktree
snapshot, `contracts.shared` pins `review-output.schema.json` and the reviewer
spec, `output` is the findings schema, `stop` says a degraded tool run is not a
clean pass. In rebuild a unit is one epoch build or revision: `purpose` is the
ledger's epoch row, `decisions` are the rulings, `contracts.shared` pins the
global rules and coverage matrix, `allowed.files` is the epoch scope and
`stop.when` includes returning `PLAN-DEFECT` rather than stubbing. Neither
dispatcher pastes a previous worker's conversation into a new unit; a retry gets
a new attempt number and a freshly built contract against the current snapshot.

Every dispatch, including a repair round and a replacement after a stall, is a
new instance started from its agent definition and its brief, and a
replacement is built only after the stalled unit is closed (work-ledger.md,
"Retries and replacement"). A completed agent
is never handed a second unit, and no unit is a fork of the controller, because
a fork carries the controller's conversation, which is the context this contract
exists to exclude. The deep-review workflow enforces this in its dispatch layer:
only label, phase, schema, model, effort and agent type reach the runtime, and
every brief opens by telling the agent it is a fresh instance. The agent
definitions under `agents/` say the same, so a worker that is asked to continue
earlier work reports a wrong brief instead of guessing. The rebuild skills keep
their own fresh-spawn rule for implementers and fixers.

## Ordinary workers and temporary teammates

Two different things are called a name. A definition name (`name` in the
frontmatter under `agents/`, passed as `agentType` to the Workflow runtime or
`subagent_type` to the Agent tool) selects which definition the instance starts
from. A runtime instance name (`name` on the Agent tool call) labels one running
instance so the caller can message, resume or shut it down later. Selecting a
named definition never requires an instance name: `fixer` or
`vidi-skills:grok-reviewer` is a type, and an ordinary worker is that type plus
its brief, with no instance name at all.

The instance name is what turns a worker into a teammate. With
`CLAUDE_CODE_EXPERIMENTAL_AGENT_TEAMS=1` in an interactive session, an Agent
call from the lead that carries `name` launches a teammate instead of a
subagent, unless the call is a fork or passes `isolation` itself; team
availability alone changes nothing. A teammate stays addressable after it
returns, is brought back with its saved conversation when messaged, and must be
shut down by name. Each of those is the opposite of a fresh unit, so the
dispatch layer refuses `name` outright rather than admitting it when teams are
off: the rule must not depend on how the lead was started, and a named instance
in a `-p` session is still a standing address a later caller could resume.

A temporary teammate is allowed only inside a phase the lead owns, where the
work needs discussion or live-tree edits that a returning worker cannot give
(in deep-review, Phase 3.6 under the explicit `teams` fix strategy). The lead
spawns each one from its definition with a name unique to that phase (role,
bucket and round), hands it the same brief an ordinary worker would get, shuts
every teammate down when the phase ends, and never messages a teammate from an
earlier phase or run; a replacement gets a new name. The Workflow runtime never
creates a teammate, so a review or fix lane inside the workflow is always an
ordinary worker.

## How units disagree

Units in the Workflow runtime never talk to each other. `agent()` gives a unit
no address, no message tool and no teammate: it starts from its definition and
one prompt, returns one result and is gone, and the runtime's resume replays
each call from its prompt alone, so a result that depended on a conversation
nobody recorded could not be replayed. deep-review therefore carries every
disagreement as evidence in a typed packet that a fresh unit reads later. Three
judges rule on one identical brief and are merged by evidence, never by count
(judge-contract.md, "Merging and rebuttals"). A fixer that finds a confirmed
finding false rebuts it in its fix packet, with evidence, and the next round's
fresh judges answer the objection. A fixer whose cause lies in a file it does
not own escalates in the packet, and a fresh fixer owning that file repairs it.
Each exchange is one write and one later read of a durable record, so a
restarted controller sees the same argument the live one did, and no party's
conclusion reaches another's brief before that party has formed its own. This
is the default for every workflow unit and needs no team support.

Live discussion is for the one case a returning packet cannot serve: a fix on
the live checkout where the lead must answer a teammate's question or redirect
it before it returns. That is Phase 3.6 under the explicit `teams` strategy,
and the lead owns it, never the Workflow. The lead opens the phase in
`scripts/team_phase.py`, records each teammate before spawning it by name,
messages it with SendMessage, records each shutdown, and closes the phase,
which the script refuses while any teammate is still standing. Discussion runs
between the lead and one teammate; teammates do not message each other,
because their buckets are disjoint and anything that crosses a bucket is an
escalation in the packet. The script never spawns, messages or stops an agent
(the Agent tool and SendMessage do that, in the lead's session); it is the
record that turns "I shut them all down" into a list a reader can check, and
the barrier that keeps the next report-only review from starting while a
teammate could still edit the tree.

## The lead stays thin

The lead is the session that owns a run: it builds units, dispatches them and
reads what comes back. It stays thin because everything it reads becomes
context it carries into every later decision, and a lead that has investigated
one unit's problem in its own window is no longer a neutral reader of the next
unit's result. Four rules keep it thin.

Child execution returns a typed result packet. Every unit is dispatched with
the schema of its result pinned before it starts: a reviewer returns the
findings schema, the boss the rulings schema, a fixer the fix packet
(`fix-output.schema.json`, the hand-back's words for one bucket), a task
worker the hand-back (`hand-back.schema.json`). The lead reads the packet and,
when it needs more, the note the packet names; it does not read the worker's
transcript. The deep-review dispatch layer refuses a unit with no schema the
way it refuses a named or forked one, and `fixResults` in the workflow result
keeps every fix packet beside `reviewResults`.

Loading a Skill is not context isolation. The Skill tool and a slash command
inject a skill's instructions into the caller's own context; the files that
procedure reads and the commands it runs land in the lead's window exactly as
if the lead had typed them. A skill is a procedure the lead follows, not a
worker, so a skill the lead loads should be a thin front end that gathers
facts, builds arguments and dispatches, which is what deep-review's SKILL.md
is. Only a dispatched agent (the Agent tool, or the Workflow runtime's
`agent()`, started from a definition with its brief) has a context of its own,
and only a script has none.

Heavy investigation stays in workers or deterministic runners. Reading a diff
end to end, tracing a defect, running a long command, comparing two trees:
each of those goes to a fresh worker that returns a packet, or to a
deterministic runner (`run_manifest.py`, `run_gate.py`, `work_ledger.py`,
`hand_back.py`) that returns a small JSON object. The lead's own tool calls
are for the packet, the note it names, and the runner's output.

No extra permanent coordinators. The state a coordinator would hold already
lives in the ledger, the manifest, the contracts and the notes, so a standing
agent between the lead and its workers, or a teammate kept alive across phases
to remember things, is a second lead that would also need keeping thin, with
nothing to show for it. The Workflow script is the one coordinator a run has,
and it is a program with no context at all. A temporary teammate exists inside
one phase and is shut down with it, as the previous section says.
