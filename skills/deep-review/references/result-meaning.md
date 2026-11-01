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
- `complete`: the result passes every check finalize makes short of the gate.
  Run `run_gate.py run` and `finalize`; until then it is still
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
  agent-attempt cap or the token budget. Change credentials, tooling or limits,
  then initialize a fresh manifest.
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
- artifact existence: the result bytes and the gate report with its captured
  stdout and stderr exist and still hash to what was validated.

A result that fails any of these is `malformed` to classify and refused by
finalize; it never becomes acceptance by being retried, only by a fresh attempt
that passes.
