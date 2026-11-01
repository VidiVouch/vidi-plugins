# Hand-back contract

A hand-back is the short structured result a fresh worker returns when its unit
ends. The lead reads it to decide the next dispatch, and the ledger stores it,
so it must be small enough to read in one glance and exact enough that every
claim in it can be followed to a file, a commit or a check that ran. Everything
else, the reasoning, the full command output, the list of things tried, goes in
the worker's note, which is a file outside the repository. The shape is
`skills/deep-review/hand-back.schema.json`; `scripts/hand_back.py check` refuses
a hand-back that breaks the rules below, and `scripts/work_ledger.py record`
runs the same check before storing one. The work-unit contract pins this schema
as a task unit's `output` unless the run names another result shape.

## Fields

Every field is required, and no other field is allowed, so detail cannot leak
into the record instead of the note.

- `disposition`: one word for how the unit ended. An implementer returns
  `completed`, `partial`, `blocked-approval` or `failed`; a verifier returns
  `accepted`, `changes-required`, `blocked-approval` or `failed-verification`.
  The ledger derives task state from this word and nothing else in the result.
- `summary`: one or two sentences, at most 500 characters. Longer text is the
  note's job.
- `artifact`: `range` and `files`. The range is `<commit>..<commit>` with full
  40-character hashes, or `<commit>..worktree` when the work is uncommitted
  against that commit; both ends must be commits in the unit's repository. A
  unit that changed nothing in the tree still names its range, with the same
  hash on both sides. `files` lists the paths changed, or for a verifier the
  paths inspected, so the next worker knows exactly what to diff.
- `checks`: the commands that actually ran, each with `passed` and an optional
  short `note`. A check that was skipped, not run, or run against a different
  tree is not listed; listing it would turn the hand-back into a self-report. A
  `completed`, `accepted` or `blocked-approval` disposition needs at least one
  check and no failed one, because a clean disposition with no evidence, or
  with contradicting evidence, is exactly what the lead must never accept.
- `findings`: `unresolved` lists what is known to be wrong or unproven and was
  not fixed, in or out of scope; `refuted` lists claims from the brief, a prior
  attempt, or a verifier that this worker checked and found false. Both are
  lists of one-line strings; the note carries the reasoning. Confirmed and
  fixed issues are not findings, they are in the summary and the files.
- `deviations`: where the worker departed from the brief or the expected
  approach, including choosing a simpler design, touching a file outside the
  expected set, or leaving a criterion for another task.
- `nextAction`: what the lead should do with this result: dispatch the
  verifier against the named range, approve a named gate, retry with a stated
  change, or close the task. One sentence.
- `notePath`: absolute path to the detailed note. The file must exist and be
  non-empty when the hand-back is checked, and it must lie outside the
  repository so a discarded worktree does not take the evidence with it.
- `openObligations`: work that belongs to another task ID, one line each.
- `gatedActions`: steps withheld because an approval gate from `approvals.md`
  covers them. Each starts with the gate ID and a colon, for example
  `commits-pushes: push of 563a48d withheld`, so the ledger's list and the
  approval record use the same vocabulary. A `blocked-approval` disposition
  must name at least one.

## Checking and recording

```bash
python3 "$SCRIPTS/hand_back.py" check --result "$RUN_DIR/tasks/$ID/result-implementer-$N.json" \
  --role implementer --repo "$REPO"
python3 "$SCRIPTS/work_ledger.py" record --ledger "$LEDGER" --task "$ID" --attempt "$N" \
  --role implementer --owner "$AGENT" --result "$RUN_DIR/tasks/$ID/result-implementer-$N.json" \
  --input "$RUN_DIR/tasks/$ID/input-$N.json" --packet "$RUN_DIR/packets/$ID.json"
```

`record` takes the repository from the input snapshot, so a range naming a
commit that does not exist, or a note inside the repository, is refused at the
ledger as well. Pass `--legacy` only for results recorded by a run that started
before this contract existed; those carry `status` or `verdict` instead of
`disposition` and their shape is not checked, but the derived state still
works. A new run never uses `--legacy`.

## Why short

A lead that reads a long hand-back has inherited the worker's context, which is
the thing the work-unit contract exists to prevent. A lead that reads a short
one and needs more opens the note, which is durable, addressable by path, and
visible to a restarted controller or a later verifier without waking the
worker. Keeping the two apart is what lets a worker be shut down the moment it
returns.
