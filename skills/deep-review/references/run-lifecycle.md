# Run identity, measured gates, and recovery

Use this contract from deep-review, epoch-review, and rebuild-examine. The
Workflow script owns review/adjudication/fixing. Host-side helpers own filesystem
identity and command evidence because Workflow scripts cannot access the shell
or filesystem directly. Do not add an agent whose only task is to claim a gate
passed, or infer acceptance from a completion notification.

## Prepare an immutable run

After freezing the target and copying required tooling, save the complete
workflow arguments as a new JSON file outside the reviewed repository. Keep that
file unchanged. Include the reviewer preparation result, exact target, limits,
and registered gate commands. Gate commands come from the project's documented
checks or an explicit operator choice, never from a reviewer's returned text.

```json
{
  "limits": {"concurrency": 3, "maxAgents": 30, "minRemainingTokens": 0},
  "gate": {
    "commands": [["uv", "run", "ruff", "check"], ["uv", "run", "pytest"]],
    "timeoutSeconds": 120
  }
}
```

This is a partial example, not universal Python policy. Supply the commands for
the actual project. If no gate is defined, report review findings without claiming
automated acceptance. Do not install missing check tools just to get a green gate.

Use an artifact directory outside the resolved review root, including under
--in-place. A persistent location such as `$HOME/.local/state/vidi-reviews` is
preferable to a disposable worktree or temporary directory. The helper rejects
containment through symlinks. Do not put credentials or environment dumps in args.

```bash
python3 "${CLAUDE_PLUGIN_ROOT}/skills/deep-review/scripts/run_manifest.py" init \
  --repo-root "$REVIEW_ROOT" --args-file "$REQUEST_ARGS" \
  --workflow "${CLAUDE_PLUGIN_ROOT}/skills/deep-review/deep-review.workflow.js" \
  --artifact-dir "$ARTIFACT_DIR" \
  --contract "${CLAUDE_PLUGIN_ROOT}/skills/deep-review/review-output.schema.json" \
  --contract "${CLAUDE_PLUGIN_ROOT}/skills/deep-review/references/reviewers.md"
```

Also pass --contract for the reviewer specs/agent definitions and gate helper
used by this run. Explicit --exclude-untracked prefixes may omit generated
.claude/skills, .review-cache, or .rebuild tooling only when those were added by
the orchestrator. Tracked files are never excluded. Record every exclusion;
do not hide untracked application code to make verification pass.

Init returns runId, snapshotId, rubricId, manifestPath, and argsFile. The generated
args contain `run:{id,snapshotId,manifestPath}`. Pass that entire object to Workflow,
not the original unbound args or an abbreviated reconstruction.

The manifest is the run's recovery record, written before anything is dispatched.
Besides the snapshot and the full arguments it holds the workflow and contract
digests and their combined `rubricId` (the identity `reuse_result.py` compares),
`models` (the requested boss list, the judging panel's roles when `--panel` was
requested, and each reviewer's policy, agent type and requested model,
recomputed from the arguments by verify so it cannot be edited to say
otherwise), `unit` (the ledger task and attempt the run serves, from
`--task-id` and `--attempt`), and `outputs` with the declared paths of every
durable artifact: `argsFile`, `resultPath`, `patchPath`, `gatePath` and
`acceptancePath`, all inside the run directory. Save the workflow result at
`resultPath` and a captured patch at `patchPath`, so a reader who finds the
manifest after the worktree is gone knows where each artifact is or that it was
never written. Credentials are refused in the arguments and belong nowhere else.

```bash
python3 "${CLAUDE_PLUGIN_ROOT}/skills/deep-review/scripts/run_manifest.py" verify \
  --manifest "$MANIFEST"
```

Run verify immediately before dispatch and before a resume. It checks the exact
arguments, workflow/contracts, HEAD/index entries, working files, untracked files,
mode bits and symlink link text. It does not follow symlinks into external data.
A changed input refuses replay; do not edit the manifest to make it pass.

These helpers currently require POSIX Git repositories with an existing HEAD.
Submodules, nested repository directory entries, special files, and obstructed
tracked paths fail explicitly. A repository with a rebase, merge, cherry-pick or
revert in progress is refused by init and verify: HEAD is then neither endpoint
of the caller's operation, and a run frozen on that half-state would read as a
review of the epoch. Finish or abort the operation first. A manifest detects drift; it does not lock a tree
against concurrent edits. Keep the reviewed worktree exclusively owned until
verification completes. Local checksums are integrity checks, not signed proof
against an owner who deliberately rewrites all hashes.

## Admission and results

The script is a semaphore: each unit acquires one of `limits.concurrency` slots
and only then, immediately before calling agent(), checks `limits.maxAgents`,
the optional runtime token budget and whether dispatch has been stopped. A unit
that queued before a stop is cancelled at that check and never reaches agent().
A host may queue that call again; this is not proof of provider start time. Keep
the limit below verified host capacity and test the actual runtime before
unattended use. Required reviewer failures, recognized terminal
auth/quota/cancellation errors (including an agent() call the runtime rejects
as cancelled) and the attempt or round cap stop queued work. Already-running
agents finish through the host runtime and count in `admission.started`; the
first stop is recorded as `admission.stop` with its reason and how many units
were `running` and `queued` at that moment, and the conclude barrier shows the
running ones settled. A null optional result without a classified terminal
cause is recorded as failed rather than guessed to be account exhaustion.

`minRemainingTokens` applies only when the runtime provides a finite token budget.
It is not a five-hour subscription usage reading, monetary limit, or reservation
for in-flight work. With no trusted budget, the result says tokenBudgetKnown=false
and `remainingTokens` null, and configured concurrency/attempt/round limits still
apply; when the budget is known, `remainingTokens` is the reading taken at the
last launch check, a token count and not a dollar estimate, because the script
has no provider-specific pricing. Do not promise hard billing limits or copy
another user's 90%/95% threshold. Long unattended jobs still need approved live
cancellation/cleanup checks.

Save the full workflow result outside the review root before summarizing it.
`reviewResults` retains full successful payloads; `attempts` records review outcomes;
`admission.dispatches` records transport states, including returned versus accepted.
`currentFindings` describes the last review; `confirmed` is historical and must
not be presented as the list of currently open defects. `disposition` is the
current state of every judged finding, one of open, fixed, refuted, unruled or
degraded with its reason (references/disposition.md); report from it. Every result remains
acceptance=pending-external-verification until the gate helper finalizes it.

`termination` records why the loop ended as `{state, round, reason}`, set once
at the first terminal decision so a reader never infers the ending from counts:
`converged` when a round found nothing actionable, `report-only` when fixing was
off, `repeated-failure`, `round-cap`, `fixer-failed`, `judge-failed`,
`review-failed`, `stopped` (admission stopped, with `admission.stopReason` as the
reason: a cancellation, a limit or a required lane lost) or `no-reviewers`. The
fix loop has two bounds. The round cap limits rounds. The attempt bound limits
work on one finding: a finding that two fresh fixers were asked to fix and two
review rounds still saw is not sent to a third, because each fixer was a new
instance on the same brief and a third would repeat the same work until the
cap. The loop ends as `repeated-failure` with that finding `open` and its
attempts in `rounds`; a fixer that rebutted or escalated a finding left the code
alone, so that does not count as an attempt. A fixing run skips its own gate,
`gate.state` is `skipped` with the reason, and the gate that counts is the one
the fresh read-only run of the resulting snapshot records, which is why
finalize requires `fixed=false` and a gate report naming that run's snapshot.
Before any worktree teardown the saved result's `fixed` decides whether a patch
is captured at `patchPath` (deep-review SKILL.md, Phase 3.5); a worktree whose
result or patch is not in the run directory is retained.

## Verify and accept

1. If incomplete, preserve the result and any partial edits, and stop automatic
   progression. Do not commit, rebase, or advance a ledger. Recovery is explicit.
2. If requiresPostFixReview=true, preserve the patch and original result. Create
   a new manifest for the resulting tree with fix=false, then run one fresh
   read-only review of that exact snapshot. Do not relax the old snapshot check.
   If it finds actionable defects, return changes-required; do not start an
   unbounded outer repair loop. Further repairs remain under the owner's round
   cap and require fresh attempts.
3. For a completed read-only result, the workflow's Conclude phase has already
   run the registered gates. The Workflow runtime has no shell, and no shell
   primitive is invented for it: the controller dispatches one `gate-courier`
   unit whose only command is `run_gate.py run --manifest "$MANIFEST"`, under
   the normal permission system, and reads the report that command printed as
   the courier's typed return value. It accepts nothing from a log line or a
   display-only hook text. The report must name this run id, this snapshot and
   the registered commands; the controller records it as `gate` in the result
   (`state` passed, failed, missing or skipped, `reason`, `report`, the
   `executor` `run_gate.py`, and the `courier` named separately so the wrapper's
   model is never the author of record) and marks the result incomplete when the
   gate did not pass or never arrived. A run that applied fixes skips the gate
   because the tree no longer matches the manifest; its fresh read-only run is
   gated instead. Then finalize:

```bash
python3 "${CLAUDE_PLUGIN_ROOT}/skills/deep-review/scripts/run_gate.py" finalize \
  --manifest "$MANIFEST" --result "$RESULT_JSON"
```

Do not run `run_gate.py run` by hand after the workflow: the run directory already
holds `gate-result.json`, a second run is refused, and finalize accepts only a
result whose recorded `gate.report` equals that file. That equality is the
transport check: a courier that paraphrased the report, a report invented
without the helper, or a file replaced after the controller read it cannot be
finalized, and the next attempt is a new run id.

The gate runner uses argv arrays, an explicit cwd, timeouts and owned process
groups; it records stdout/stderr separately, names its own process as the
`executor`, and checks the snapshot before and after commands. A failed,
cancelled, mutating, missing, or wrong-snapshot gate cannot pass. Existing gate
attempts are not overwritten or automatically rerun. Keep the product of
`gate.commands` and `timeoutSeconds` within what the courier's shell can wait
for (its prompt asks for at most 600 seconds); a helper the shell kills writes a
cancelled report, which is not a pass. A hard-killed process may leave
gate.lock; inspect ownership and retained logs before any manual recovery.
Never delete another run's lock to force progress. When the gate runs inside a
ledger unit, set `gate.registry` in the request arguments to that unit's
`tasks/<ID>/processes-<role>-<N>.json`; the courier's command then carries
`--registry`, each check's child is registered with `owned_processes.py` before
it is waited on and recorded under the check as `process`, so the unit's
`work_ledger.py close` barrier stops a child that a hard-killed gate left behind
and its report shows the gate's own children as `gone` rather than leaving
their cleanup unobserved.

Finalization checks exact run identity, current reviewer policy and terminal
coverage, required participants, attempt/payload evidence, current findings, the
recorded gate against the durable report, gate commands and output hashes. A
clean result writes acceptance.json with accepted=true. A valid actionable
finding yields changes-required, not acceptance. Missing evidence refuses
finalization. Both helpers may fail; successful workflow completion alone does
not satisfy this protocol.

## Resume and interrupted work

Keep the manifest, original request, generated arguments, full result and gate
artifacts. Resume with the original arguments only after verify passes:

```bash
python3 "${CLAUDE_PLUGIN_ROOT}/skills/deep-review/scripts/run_manifest.py" resume \
  --manifest "$MANIFEST"
```

It verifies the manifest and prints `args`, the exact generated object to pass
as the Workflow `args` beside `resumeFromRunId`, plus `unit` and `outputs`. A
resume that omits the arguments gives the runtime nothing to replay against and
returns an empty run; a changed tree or a missing args file is refused before
any agent is contacted; and an empty or stopped run cannot be finalized because
the gate helper requires at least one returned dispatch. Do not
promise cache reuse after restructuring the script, changing model/rubric options,
or changing the tree. If a cached value is invalid or transient, retain it as
failure evidence and create a fresh manifest/attempt: `run_attempt.py classify`
names what a saved result proves (transient, blocked, launch-blocked, stale,
modified, partial, malformed, missing, complete, or already decided) and
`run_attempt.py retry` starts the next attempt under a new run id with the same
request, only after the prior attempt is terminal and within `--max-attempts`;
result-meaning.md defines each category. `run_status.py status` reads the same
files and keeps four answers apart: how far execution got (every dispatch as
queued, running, succeeded, skipped, failed or cancelled), what the reviewers
covered, whether acceptance.json decided anything, and the precise stage that
blocks recovery with the command that moves it, plus the attempt series and the
ledger unit's open obligations. Old agents are not reused
for a different task, and no agent is woken to recover a decision; the decision
is `acceptance.json` or it was not made. A prior acceptance is reused only when
`scripts/reuse_result.py` matches the current verified manifest's snapshot and
rubric identity against it; result-reuse.md separates that from the runtime's
prompt cache.

For a run interrupted during edits, the starting manifest will no longer match.
Preserve the work and patch, reconcile the actual tree, then create a fresh
read-only verification run. Do not use an old completed-node result to approve
new code. If only a host notification survives, inspect the run journal before
claiming any work completed.

## Caller-owned history and ledger transitions

The caller retains control over history changes; these helpers never commit,
rebase, reset, publish, or rewrite a rebuild ledger. History changes, pushes,
and publication are approval gates; approvals.md says what authorizes them.
For multi-task runs, record each worker's owner, input snapshot, attempt, result
and verification per task ID with `scripts/work_ledger.py`; work-ledger.md
explains the derived states and what a worker is given. Close every unit with
`work_ledger.py close` before the next starts: its cleanup barrier stops only
the child processes the run registered with `scripts/owned_processes.py`,
keeps the unit's patch and records, and no new unit is built while a finished
one is open. The deep-review script records its own barriers in
`admission.barriers`; finalization refuses a result that ended with a dispatch
still active. Brief each fresh
worker with the contract `scripts/work_unit.py build` writes (work-unit.md):
the same sections for review units and rebuild epochs, never a prior
worker's conversation.

- epoch-review records LO/HI, cursor, branch tip, manifest, acceptance artifact,
  and the pending operation before fix-commit/rebase/advance. It records the new
  tip after each operation. On restart, compare actual refs with both recorded
  endpoints before repeating anything. An ambiguous pending operation pauses for
  reconciliation; it does not automatically repeat a commit or rebase. Do not
  parallelize epochs sharing one worktree.
- rebuild-examine records result and acceptance paths with the epoch. Incomplete
  review follows its review-incomplete/REVISE return contract, not a stranded
  JUDGING state. Only the owning child writes its transition.
- rebuild dispatches fresh children and reads their durable results. Missing gate
  evidence or reviewer availability is not an implementation defect. It pauses
  rather than turning infrastructure failure into a speculative source rewrite.

The offline fixtures in `tests/test_failure_fixtures.py` reproduce each named
failure (a wrong worktree, unusable or truncated results, timeouts, missing
reviewers, gates and artifacts, unexpected routes, cancellation, duplicate
retries, replayed failures, zero-agent returns, queued work after a stop, crashes
at the epoch commit and rebase boundaries and at ledger transitions) and check
that none of them finalizes. Live Git mutation crash recovery and host/runtime
process cleanup still need fault-injection and integration tests on the installed
runtime. Do not describe the written caller protocol as a fully automated
recovery engine.
