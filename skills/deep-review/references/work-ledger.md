# Work ledger

Use `skills/deep-review/scripts/work_ledger.py` when a run hands many task IDs to
fresh workers over hours. A Workflow script keeps its progress in memory and the
host journal is a transport log, so neither tells a restarted controller who did
what, from which tree, and whether the result was ever verified. The ledger is
one JSON file keyed by task ID that answers those questions from recorded
evidence and nothing else. It never dispatches, commits, pushes, or publishes;
history changes and publication stay behind the gates in `approvals.md`.

## Shape

Each task holds `deps`, `mode`, the packet digest, every `attempts` record, an
optional `mark`, and derived fields: `state`, `terminal`, `summary`,
`openObligations`, `gatedActions`, `verification`, `artifacts`. An attempt record
has the attempt number, the role (`implementer` or `verifier`), the `owner` string
(agent id and label), `recordedAt`, the `input` snapshot captured before the
worker started, the worker's structured `result`, the short hand-back that
`hand-back.md` describes, and once the unit's cleanup barrier has run, its
`barrier` (outcome, owner, time, the owned processes it stopped, the tree it
preserved). A unit taken through `start` (which `work_unit.py build` does)
also carries `start`: who dispatched it, when, the contract it was given and
the deadline after which it counts as overdue.

State is derived, not asserted. The latest attempt decides: a unit started
and neither returned nor closed is `running`; an implementer
`failed` status is `failed`; an implementer result without a verification is
`in-progress`; a verifier verdict maps to `accepted`, `awaiting-approval`
(verdict `blocked-approval`), `changes-required` or `failed-verification`. An
implementer unit closed without a result is `cancelled`, or `failed` when its
barrier outcome was `failure`; a verifier unit closed without a result leaves
the task `in-progress`. Only
`accepted`, `awaiting-approval`, `failed` and the marked states are terminal.
`changes-required` stays open so a retry is visible as an unmet obligation rather
than a closed task. A `mark` sets the states no worker produces (`pre-recorded`,
`blocked-dependency`, `not-started`, or `failed` when the retry cap closes a
task) and always carries a reason and an owner.

## Commands

```bash
LEDGER="$RUN_DIR/ledger.json"
python3 "$SCRIPTS/work_ledger.py" init --ledger "$LEDGER" --run-id "$RUN_ID" \
  --tasks "$RUN_DIR/args.json" --packets "$RUN_DIR/packets"
python3 "$SCRIPTS/work_ledger.py" snapshot --repo "$REPO" > "$RUN_DIR/tasks/$ID/input-$N.json"
python3 "$SCRIPTS/work_ledger.py" packet --ledger "$LEDGER" --task "$ID" --packets "$RUN_DIR/packets"
python3 "$SCRIPTS/work_ledger.py" record --ledger "$LEDGER" --task "$ID" --attempt "$N" \
  --role implementer --owner "$AGENT" --result "$RESULT_JSON" \
  --input "$RUN_DIR/tasks/$ID/input-$N.json" --packet "$RUN_DIR/packets/$ID.json"
python3 "$SCRIPTS/work_ledger.py" mark --ledger "$LEDGER" --task "$ID" --state blocked-dependency \
  --reason "unmet: $DEP" --owner overseer
python3 "$SCRIPTS/work_ledger.py" summary --ledger "$LEDGER"
```

Run `snapshot` before the worker starts, because after it finishes the tree is
the output, not the input. Record the implementer before the verifier for the
same attempt; a retry gets a new attempt number and the old records stay. A
result is checked as a hand-back before it is stored: exactly the schema's
keys, a disposition the role can return, a note file that exists outside the
repository named by the input snapshot, an artifact range whose commits exist
there, and checks that ran and agree with the disposition. It also goes through
the same credential key filter as run manifests, so a worker cannot leave
environment dumps or tokens in the ledger. `--legacy` admits a `status` or
`verdict` result from a run that started before the hand-back contract and
skips the shape check. Writes replace the file atomically; an
interrupted write leaves the previous ledger intact. One writer at a time is
assumed, which matches sequential dispatch.

## Cleanup barrier

Every unit ends through `close`, whether it returned a hand-back, failed, was
cancelled, or is being replaced by a retry. The barrier exists because the next
unit must start with nothing of the previous one still running, and a worker's
own report of having finished is not evidence of that.

```bash
python3 "$SCRIPTS/work_ledger.py" close --ledger "$LEDGER" --task "$ID" --attempt "$N" \
  --role implementer --outcome success --owner overseer \
  --evidence-root "$RUN_DIR" --repo "$REPO"
python3 "$SCRIPTS/owned_processes.py" register \
  --registry "$RUN_DIR/tasks/$ID/processes-implementer-$N.json" --pid "$CHILD"
```

`close` does three things and nothing else. It stops the child processes the
run registered for the unit under `tasks/<ID>/processes-<role>-<N>.json`:
each entry is checked by pid, process group and start time, so a pid the
operating system has reused is skipped and reported as `reused`, and a group
that is the caller's own is refused rather than killed; processes the run never
registered are never touched, which is how unrelated sessions stay up. When the
registered process has already exited, the barrier still looks for members of
its group and stops them, because a crashed or timed-out child often leaves a
tool server or helper behind and a group id is not reallocated while the group
has members; only an entry with nothing left in its group is `gone`. Register
only children started in their own session (`start_new_session`, `setsid`); a
child in the caller's group is refused at `register`. Agents the Workflow
runtime ran are stopped by the runtime; the deep-review script records its own
barriers in `admission.barriers` so the gate can see every dispatch was
terminal before the run returned.

It keeps what the unit produced. With `--repo` and `--evidence-root` the
tracked diff of the worktree is written once to
`tasks/<ID>/patch-<role>-<N>.diff` and the barrier records HEAD, the index
digest, the patch digest and the untracked paths, so a later cleanup or reset
of the worktree cannot lose the unit's edits. Notes, hand-backs, commits,
decisions and the process registry are left exactly as they are; `close` never
deletes, resets or commits.

It records why the unit ended: `outcome` is `success`, `failure`,
`cancellation` or `retry`, with the owner and time. A unit that never returned
(the agent was cancelled, crashed, or was replaced after a stall) is closed
the same way; the ledger then holds an attempt with no result, state
`cancelled` (or `failed` for a `failure` outcome), and that attempt number is
spent: a late result for it is refused, and the replacement takes the next
number. `success` needs the unit's recorded result.

Until a started or recorded unit is closed it is an open unit. `summary` lists
them under `openUnits`, and neither `start` nor `work_unit.py build` will take
a new unit while one exists, in any task, because sequential dispatch means an
open unit is a worker that is still running or whose barrier has not run. A
barrier whose owned process survived SIGKILL refuses too; inspect that process
before going on.

## Retries and replacement

A worker becomes visible the moment it could exist, not when it returns.
`work_unit.py build` records the unit through `start` before it writes the
contract, and a dispatcher that briefs a worker some other way runs `start`
itself first. The record takes the attempt number and role, so a second
build or start of the same unit is refused by the ledger whether or not the
contract file is still there, and a `running` unit blocks every other start
until it is closed. That is what keeps a retry or a watchdog replacement from
producing two workers on one task: the replacement cannot be built while the
old attempt is open.

```bash
python3 "$SCRIPTS/work_ledger.py" start --ledger "$LEDGER" --task "$ID" --attempt "$N" \
  --role implementer --owner overseer --input "$RUN_DIR/tasks/$ID/input-$N.json" --timeout 5400
python3 "$SCRIPTS/work_ledger.py" close --ledger "$LEDGER" --task "$ID" --attempt "$N" \
  --role implementer --outcome retry --owner overseer --evidence-root "$RUN_DIR" --repo "$REPO"
```

Replacing a unit is always the same three steps in this order: close the old
attempt with `--outcome retry` (its barrier stops the processes it owned and
keeps its patch), build or start the next attempt number, dispatch. The old
attempt is then terminal in the ledger before the new worker exists, its
number is spent, and a hand-back the old worker returns later is refused as
a late result, so the two attempts can never both be recorded. `record`
fills the started entry rather than adding a second one; an input snapshot
given at `record` must equal the one recorded at `start`.

Wait for the completion notification, not for a clock. The Agent tool's task
notification, the Workflow runtime's settled `agent()` promise and a child's
exit status each say the worker is done; when one arrives, record the
hand-back and close the unit. Do not poll the ledger, the transcript or the
process table on an interval to find out, because polling spends the
controller's context on nothing and still misses the moment. The timeout is
the fallback for a notification that never comes: `--timeout` records a
deadline with the start, `summary` lists open units past it under `overdue`,
and a watchdog reads that list once, when its single timer or `until` wait
fires, not in a loop. An overdue unit is not evidence that the work failed;
it is the signal to run the barrier, which decides what survived, and only
then to build the replacement.

## What a worker receives

`packet` prints the task's own packet plus, for each dependency, its state,
summary, note paths, open obligations and gated actions. It prints nothing about
other tasks. Give a worker that output, the shared constraints and the notes it
chooses to read; do not give it the ledger, the plan, or sibling transcripts.
The packet digest recorded at `init` is checked on every `packet` and `record`,
so a packet edited mid-run is refused instead of silently changing a task.
`scripts/work_unit.py build` wraps this output in the full work-unit contract
(snapshot, allowed actions, decisions, pinned shared contracts, evidence paths,
output schema, stop condition); work-unit.md describes it.

## Vocabulary

Use the five gate IDs from `approvals.md` (`global-settings`, `skill-enablement`,
`paid-calls`, `commits-pushes`, `hosted-reports`) at the start of each
`gatedActions` string so the ledger's gated list and the approval record match;
the hand-back check refuses any other prefix.
