# What a saved result means, and when to retry

A result file that parses is not a result that proves anything. The same JSON
shape describes a run that never launched, one that stopped on a quota, one that
replayed an unusable cached value, one that reviewed a tree which has since
changed, and one that applied fixes nobody re-reviewed. `run_gate.py finalize`
refuses all of them, but a refusal is not a plan. `run_attempt.py classify` names
what the run directory proves and what to do next, and `run_attempt.py retry`
is the one way to start the next attempt of the same request.

```bash
python3 "${CLAUDE_PLUGIN_ROOT}/skills/deep-review/scripts/run_attempt.py" classify \
  --manifest "$MANIFEST"
```

It reads the manifest, checks it against the live tree, the workflow, the
contracts and the argument file, inventories the declared `outputs` (args,
result, patch, gate, acceptance), reads the result at `resultPath`, and prints
`category`, `terminal`, `retryable`, `reasons` (every failure reason the run
recorded, in order), `next`, `attempt`, `replacedBy` (later runs that name this
one as their prior) and `evidence` (the result path and the reviewer payloads
that succeeded). Exit 0 means there is nothing to retry or decide: the result is
`complete` and waits for the gate, or a decision is already recorded. Exit 1
means a retry or a decision is needed. Exit 2 means the manifest itself could
not be read.

## The categories

Each category answers two questions: did the attempt end, and would the same
request fare better next time.

- `accepted` and `changes-required`: `acceptance.json` exists for this run. The
  decision is made; a ruling against the tree is repaired under the round cap and
  reviewed again, never retried.
- `complete`: the result passes every check finalize makes short of the file
  comparison, including a passed gate the controller recorded for this run and
  snapshot. Run `run_gate.py finalize`; until then it is still
  pending-external-verification.
- `missing`: no file at `resultPath`. Nothing says the attempt ended. Resume it
  with `run_manifest.py resume` while the tree is unchanged; when the worker is
  known to be gone, `retry --abandon "<why>"` records that decision.
- `partial` with `terminal: false`: the result was written while a dispatch was
  still queued or running. Close the unit first (`work_ledger.py close`) or use
  `--abandon`.
- `stale`: the recorded inputs no longer verify. The tree, the workflow, a
  contract or the argument file changed after the manifest was written. That is a
  new request: initialize a fresh manifest with `run_manifest.py init`. A retry
  is refused because it would review inputs that no longer exist, and a
  restructured script is never promised to replay anything.
- `modified`: `fixed` or `requiresPostFixReview` is true. Keep the patch at
  `patchPath`, create a manifest for the resulting tree and run one fresh
  read-only review (run-lifecycle.md). A retry would repeat the fixer's side
  effects against a tree that already carries them.
- `launch-blocked`: `admission.started` is 0. Nothing was dispatched, so nothing
  can be reused. Retryable only when the reasons are transient; otherwise change
  what blocked the launch and initialize a fresh manifest.
- `blocked`: incomplete, and a reason says the same request would fail again:
  authentication, 401 or 403, an account on hold, a missing CLI or model, a
  preflight that was not established, a dispatch shape the script refused, the
  agent-attempt cap, the token budget, or a gate the controller saw fail, go
  missing or go unregistered. Change credentials, tooling, limits or the tree the
  gate refused, then initialize a fresh manifest.
- `transient`: incomplete, and a reason says the failure can be outlived: usage
  or session limits, quota, rate limits, 429, 502 to 504, overload, a timeout,
  a cancellation, a network error, a worker that returned nothing or failed to
  execute. Retry.
- `partial` with `terminal: true`: incomplete for a reason that is neither
  transient nor blocking, such as invalid boss rulings or a fixer that failed.
  Retry; the bound stops a failure that repeats.
- `malformed`: the file is not JSON, is not this run's result, or claims
  completion but fails a finalize check (missing coverage, a finding without a
  successful source, a payload with unusable findings). A retry dispatches live
  under a new run id so a replayed unusable value cannot recur; if the shape is a
  script mismatch the next attempt fails the same way and the bound stops it. A
  result that identifies another run is not retried: locate this run's own.

When a reason matches both lists, blocking wins, because a quota message beside
an authentication failure still needs the credentials fixed first.

## A retry is a new run id, not a replay

```bash
python3 "${CLAUDE_PLUGIN_ROOT}/skills/deep-review/scripts/run_attempt.py" retry \
  --manifest "$MANIFEST" [--max-attempts 3] [--abandon "<why the worker is gone>"]
```

`retry` classifies the prior attempt and refuses unless it is terminal and
retryable (or `--abandon` names why an attempt that never returned is gone),
the inputs still verify, no later run already names this one as its prior, and
the attempt number is below `--max-attempts` (default 3). It then calls the same
`init` the first attempt used, with the recorded repository, argument file,
workflow, contracts, exclusions and ledger unit, and records in the new manifest
`attempt: {number, priorRunId, cause}`: the next number, the run it replaces, and
the classified cause (`transient: degraded: quota exhausted`, or
`abandoned (missing): harness exited`). `unit` is copied unchanged, because the
ledger's attempt is the worker's, not the review run's; a worker replacement
goes through `work_ledger.py close` and the next ledger attempt.

The prior run directory is not edited, moved or reused. Its manifest, result,
gate logs and successful reviewer payloads stay where `outputs` says, and the
retry's output lists them under `preserved`. Nothing is replayed: the new run id
is in every prompt, so the runtime's prompt cache has no unchanged prefix to
serve, and a fixer that ran in the prior attempt is not run again by anything
here (a `modified` result is not retried at all). The output says `replay:
false` and that is a statement about this helper, not a measurement of the
runtime: the only cache that can replay is the runtime's own, on a `resume` of
the same run id with the same arguments, and result-reuse.md explains why that
replay is still pending-external-verification.

A retry of an attempt that already has a replacement is refused, because
retrying attempt 1 twice would give two attempt 2 runs and the bound would
count neither. When the bound is reached the message says so; a further attempt
is an operator decision made with a fresh manifest, which starts a new series at
attempt 1 and leaves the old series' artifacts as evidence.

## What finalize checks beyond shape

`run_gate.py finalize` is the only writer of `acceptance.json`, and it
requires, besides the identity, snapshot, coverage, dispatch and barrier checks:

- scope: the result's `boss` equals the manifest's requested boss lanes, its
  `admission.limits` and `admission.maxRounds` equal the registered limits with
  the workflow's defaults filled in, and `rounds` lies within the cap;
- required evidence: every current finding names `sources`, all of them
  reviewers that succeeded in this run, and cites the verified tree;
- useful payloads: every successful reviewer's full payload is present, not
  degraded, and each of its findings has a usable file, line range, severity
  and description;
- gate transport: the result's `gate.state` is `passed` and `gate.report`, the
  report the controller received from its courier, equals `gate-result.json` on
  disk, so the controller's decision rested on the durable file and not on a
  paraphrase, an invented report or a display-only text;
- artifact existence: the result bytes and the gate report with its captured
  stdout and stderr exist and still hash to what was validated.

A result that fails any of these is `malformed` to classify and refused by
finalize; it never becomes acceptance by being retried, only by a fresh attempt
that passes.

## The whole state of a run, four answers kept apart

A completion notification from the host, the value the controller returned and
the result file on disk all arrive as one blob, and a reader who collapses them
into "done" mistakes an execution that ended for a review that was accepted.
`run_status.py status` reads the run directory and answers four questions
separately, each only from the file that is evidence for it.

```bash
python3 "${CLAUDE_PLUGIN_ROOT}/skills/deep-review/scripts/run_status.py" status \
  --manifest "$MANIFEST" [--ledger "$LEDGER"] [--write] [--format note]
```

- `execution`: how far the controller's own dispatching got, from the full
  result at `resultPath`. Every dispatch is recorded under one vocabulary,
  `queued`, `running`, `succeeded`, `skipped`, `failed` or `cancelled`, with its
  label, phase, reason and executor, plus the counts, `started`, and the stop
  record. The run-level state is `running` while no result exists or a dispatch
  was still active, `skipped` when nothing was dispatched, `cancelled` when any
  unit was cut off by a stop, `failed` when dispatch was stopped, otherwise
  `succeeded`. Execution that succeeded says nothing about findings or acceptance.
- `coverage`: each reviewer lane's policy and state in the same vocabulary (a
  lane that was disabled, unavailable or never launched is `skipped`), the
  per-round `attempts` history, the boss outcome, and `payloadStored` for every
  lane that succeeded: a succeeded lane with no payload under `reviewResults` is
  a result that was summarised before it was saved, which finalize refuses.
- `acceptance`: `status` and `accepted` come from `acceptance.json` for this run
  and from nothing else; without that file the status is
  `pending-external-verification` whatever the result claims. The result's own
  word is shown as `resultClaims`, the gate the controller recorded as `gate`,
  and the current findings as `openFindings`. A zero-agent return, a host
  notification or a result that calls itself accepted never changes this field.
- `recovery`: the classify category, `terminal`, `retryable`, `treeDrift`,
  `replacedBy`, the artifacts preserved under the declared outputs, and
  `blocking`: the precise stage the run is stuck at (`result`, `execution`,
  `dispatch`, `coverage`, `adjudication`, `gate`, `finalize`, `post-fix review`
  or `inputs`), its reason, and the one command that moves it (resume, retry,
  abandon, finalize or a fresh manifest). It is null once a decision exists.
- `history`: every attempt of this request, found by following `priorRunId`
  backwards and replacements forwards through the sibling manifests, each with
  its outcome (`missing`, `returned`, `replaced` with the recorded cause,
  `accepted` or `changes-required`) and which one is the current run.
- `disposition`: every finding the run judged, each under one final outcome
  (`open`, `degraded`, `unruled`, `fixed`, `refuted`) with its rounds and the
  reason, copied from the result's own record (references/disposition.md). A
  result without that record reports `recorded: false`; the helper never derives
  a current state from the historical `confirmed` union.
- `obligations`: the open findings of the last review, and with `--ledger` the
  unit the manifest names: its ledger state, the attempt's implementer and
  verifier records (started, returned, disposition, barrier outcome), the open
  units, open obligations, gated actions and the verifier's unresolved findings.
  A unit the barrier closed without a result also sets execution to `failed` or
  `cancelled` from the recorded outcome, because the ledger is then the only
  record of how the worker ended.
- `sources`: the path and sha256 of every artifact the status was read from, so
  a status can be checked against the bytes and regenerated at any time.

`--write` stores the projection as `status.json` beside the manifest; unlike
every other file in the run directory it is replaced on each run, because it is
derived from the others and names them. `--format note` prints the html-report
note model instead, which `skills/html-report/scripts/render_note.py` renders
to HTML or, with `--format md`, to Markdown; there is no separate dashboard to
keep in step. Exit codes follow classify: 0 when the run is complete or decided,
1 when a retry or decision is needed, 2 when the manifest or ledger cannot be
read. The helper writes nothing else and never dispatches, resumes or decides.
