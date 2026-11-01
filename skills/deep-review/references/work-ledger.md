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
worker started, and the worker's structured `result`, the short hand-back that
`hand-back.md` describes.

State is derived, not asserted. The latest attempt decides: an implementer
`failed` status is `failed`; an implementer result without a verification is
`in-progress`; a verifier verdict maps to `accepted`, `awaiting-approval`
(verdict `blocked-approval`), `changes-required` or `failed-verification`. Only
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
