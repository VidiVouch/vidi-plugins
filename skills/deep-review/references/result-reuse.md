# Prompt caching and accepted-result reuse

Two things get called a cache around a review run, and only one of them holds a
decision. Keep them apart, because treating the first as the second is how a
rerun gets accepted without anyone having measured it.

## Prompt caching: the runtime replays return values

The Workflow runtime caches each `agent()` return value keyed by the unchanged
prefix of calls. Resuming a run with the same script and arguments replays
those values instantly instead of dispatching again. That is prompt caching: a
transport convenience that saves tokens on a restart. A replayed value is the
same thing it was the first time, a reviewer's or fixer's claim with
`acceptance: pending-external-verification`. Nothing about replay verifies the
tree, the rubric or the gate, so a resumed run must still go through
`run_gate.py run` and `finalize`, and finalize verifies the manifest against
the live tree before it accepts anything. Do not promise cache reuse after
changing the script, the model or rubric options, or the tree; the first
changed call and everything after it runs live, and the manifest refuses a
changed tree anyway.

The deep-review script never names, forks or resumes an agent for the same
reason: a unit is a fresh instance with a typed result, and the result is what
survives. Nothing a cold agent remembers is evidence.

## Accepted-result caching: acceptance.json is the decision

The durable decision is `acceptance.json`, written by `run_gate.py finalize`
only after a completed read-only review and a measured gate, bound by digest to
the full workflow result and the gate report, and tied to one run ID and one
snapshot ID. The run directory also holds the manifest, the generated
arguments, the result and the gate logs, so a later reader can see what was
reviewed, under which rules, and what the gate printed.

Everything else in a run directory is a marker, not a decision. A manifest with
no result is a run that started. A result with `incomplete: true` or degraded
lanes is a run that stopped or degraded. A gate report without acceptance is a
gate that ran before anyone finalized. An `acceptance.json` with
`status: changes-required` is a ruling against the tree. A `.review-cache`
hash file is a prior-attempt hint. A ledger unit in `running` is a worker that
has not returned. None of these can prove acceptance, and `reuse_result.py`
refuses each of them with the reason.

## Reuse only when input and rubric identity match

A prior acceptance applies to a new request only when it was about the same
input under the same rules:

- input: the new manifest's `snapshotId` equals the prior one. The snapshot is
  HEAD, the index, every working and untracked file and the symlink texts, so
  the same content at another worktree path is the same input and `repoRoot`
  is the one argument the comparison ignores.
- rubric: the workflow digest, the set of contract digests (reviewer specs,
  output schemas, reference prose) and every other original argument (mode,
  target, reviewer policy and availability, boss, focus, limits, gate commands)
  are equal.

The current manifest is verified against the live tree first, so the identity
being matched is what is on disk now, not what a stale manifest says:

```bash
python3 "${CLAUDE_PLUGIN_ROOT}/skills/deep-review/scripts/reuse_result.py" find \
  --manifest "$MANIFEST"
```

`find` checks every sibling run under the manifest's artifact directory, skips
the current run, and prints `reusable` (run ID, acceptance and result paths,
`coverageComplete`) and `refused` (run directory and reason). Exit 0 means at
least one prior acceptance is reusable; present that acceptance instead of
dispatching a new review. Exit 1 means none is, and the reasons say whether a
run started, interrupted or degraded, was ruled changes-required, had its
evidence altered, or reviewed a different input or rubric. `check --run-dir`
asks the same question of one prior run. Pass `--artifact-dir` to look in a
directory other than the manifest's own.

A reusable acceptance with `coverageComplete: false` is still reduced coverage;
say so as you would for a fresh run.

## Never wake a cold agent to recover a decision

When a run was interrupted, the questions are what was reviewed, what was found
and whether it was accepted. All three are answered by files: the manifest, the
result, the gate report and `acceptance.json`, and for multi-task runs the
ledger and each worker's note. Read them. Do not resume, message or restart an
agent from the old run to ask what it decided: it is a cold context that may
have been cut off before it wrote anything, its answer would be a claim rather
than evidence, and if the decision is not in the artifacts it was never made.
If the artifacts are incomplete, create a fresh manifest and run a fresh unit;
`run-lifecycle.md` describes that path.
