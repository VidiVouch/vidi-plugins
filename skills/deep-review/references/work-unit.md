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
  against whatever shape its prose implies.
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
  "output": "/abs/run/worker-result.schema.json",
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

`build` reads the task's packet and dependency outcomes through the ledger, so
the packet digest is checked against `init`, merges the shared document, writes
`tasks/<ID>/contract-<role>-<N>.json` read-only, refuses to overwrite an earlier
attempt's contract, and prints the contract. Hand the printed contract to the
worker as its brief. The evidence root must be outside the repository, the stop
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
