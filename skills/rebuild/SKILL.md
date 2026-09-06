---
name: rebuild
description: Thin resumable orchestrator for a fresh-authoring rebuild — sets up the worktree/branch and ledger, then loops one epoch at a time through rebuild-plan / rebuild-epoch / rebuild-examine with a stall watchdog and round-cap escalation. Use to rebuild a target ref's end-state as clean, verified history on a fresh base. It dispatches and bookkeeps; it never judges code and never merges.
argument-hint: "--target <ref> --from <base> --into <branch> [--guide <range>] [--continue] [--epoch <id>] [--gate <cmd>] [--max-rounds <n>]"
allowed-tools: Skill, Agent, Workflow, Read, Write, Edit, Glob, Grep, AskUserQuestion, TaskCreate, TaskUpdate, TaskList, TaskOutput, TaskStop, SendMessage, ScheduleWakeup, CronCreate, CronDelete, CronList, Bash(git *), Bash(mkdir *), Bash(ls *), Bash(cat *), Bash(echo *), Bash(printf *), Bash(grep *), Bash(test *), Bash(find *), Bash(stat *), Bash(date *), Bash(wc *), Bash(sort *), Bash(head *), Bash(tail *), Bash(sed *)
---

# Rebuild (orchestrator)

Fresh-author a `--target` ref's end-state as clean, granular, examined history
on `--into`, built from `--from`. This skill is the **steerer only**: it owns
setup, the epoch state-machine loop, dispatch to the three child skills, the
watchdog, and escalation. It does **not** plan epochs, author code, run
reviews, or adjudicate findings — child skills do, and their verdicts plus the
deterministic gate reports are **authoritative**. If you catch yourself
re-litigating a child verdict or triaging a finding's severity inline, stop:
that work belongs to `rebuild-examine`'s rubric or to the user.

## Deterministic context (auto-populated at load)

- Repo root: !`git rev-parse --show-toplevel`
- Current branch: !`git symbolic-ref --quiet --short HEAD 2>/dev/null || git rev-parse --short HEAD`
- Existing worktrees: !`git worktree list --porcelain | grep '^worktree ' | sed 's/^worktree //'`

## Child skills and how to dispatch them

| Skill | Contract |
|---|---|
| `rebuild-plan` | Writes/amends the ledger's epoch decomposition; runs the deterministic coverage-matrix check + plan judge panel. Args: `--target --from --into [--guide] --ledger [--amend "<reason>"]`. |
| `rebuild-epoch` | Fresh-authors exactly one epoch (or delivers a revision as a clean **replacement range**). Args: `--ledger --epoch <id> --worktree <path> --target [--fix <findings-file>]`. Returns `IMPLEMENTED@<sha>` (build), `REVISED@<sha>` (a `--fix` re-entry — re-examine it), `GATED(fail)` (gate red after its own fix rounds), or `PLAN-DEFECT` naming the missing dependency. |
| `rebuild-examine` | Deterministic gate + deep-review workflow + rebuild judge panel for one epoch (or `--final` whole-branch). Returns `ACCEPTED@<lo..hi>` or `REVISE@<tip>` with class-scoped findings. |

**Dispatch mechanics (honest ones — no invented skill-to-skill calls):**
child skills execute in *this* conversation. If the sibling appears in the
available-skills list (installed via the same plugin), invoke it with the
`Skill` tool, passing its args string. Otherwise Read
`${CLAUDE_PLUGIN_ROOT}/skills/<name>/SKILL.md` and follow its contract
exactly as written — it is the executable spec. Either way the *long-running
work* (implementer agents, judge panels, the deep-review workflow) is spawned
by the child contract as background `Agent`/`Workflow` tasks; those spawned
tasks are what the registry and watchdog below track. Never inline-improvise
a substitute for a missing/unreadable sibling — stop and report which one.

## Arguments

Parse `$ARGUMENTS`:

- `--target <ref>` — the ref whose final tree is the content source of truth.
- `--from <base>` — the clean base commit/branch to build on.
- `--into <branch>` — the branch the rebuild is authored on.
- `--guide <range>` — optional old history to mine for grouping/order only
  (passed through to `rebuild-plan`; its code is assumed stale).
- `--continue` — resume an existing rebuild. Only `--into` is then required;
  everything else is re-derived from the ledger. If other flags are also
  passed and contradict the ledger, **stop** and surface the mismatch.
- `--epoch <id>` — run exactly one epoch's loop-iteration, then stop with a
  status summary. The id must be the first non-ACCEPTED row (out-of-order
  builds break the as-of-history model); otherwise stop and say so.
- `--gate <cmd>` — full-tree gate override for repos without a mapped
  toolchain. Recorded once in the ledger's Global rules; child skills read it
  from there and hand it to `rebuild-gate.sh`.
- `--max-rounds <n>` — per-epoch examine↔revise round cap (default 3).

Fresh start requires `--target`, `--from`, `--into`. Stop with usage if
missing.

## Phase 0 — Idempotent state probe (ALWAYS first, every invocation)

Never bootstrap before probing. A re-invocation — `--continue`, a re-pasted
prompt, a session restart mid-run — must resume, not duplicate. The probe is
read-only:

```bash
PROJ=<injected repo root>
SLUG=$(printf '%s' "<--into>" | tr '/' '-')
WT="$PROJ/.claude/worktrees/rebuild-$SLUG"
LEDGER="$WT/REBUILD-EPOCHS.md"
```

1. **Worktree**: is `$WT` in the injected worktree list (or
   `git -C "$PROJ" worktree list`)? Does the `--into` branch exist?
2. **Ledger**: does `$LEDGER` exist? If yes, read it: epoch rows + statuses,
   agent registry, recorded target/from/gate/max-rounds.
3. **Tasks**: `TaskList` — do tasks for this rebuild (subject-prefixed
   `rebuild/<into>:`) already exist? Reuse them; never create duplicates.
4. **Live agents**: for every registry row with status `active`, check the
   task/agent is genuinely alive (`TaskOutput` non-blocking, or the
   background-task list). Alive → do NOT respawn; attach the watchdog and
   wait. Dead/lost handle → treat per the watchdog's respawn rule.

Route on the probe result:

| Probe result | Action |
|---|---|
| No worktree, no ledger | Phase 1 (fresh setup) |
| Worktree exists, no ledger | Phase 1 step 3 only (plan) — never re-`worktree add` |
| Ledger exists, no ACCEPTED-all | Phase 2, resuming at the first non-ACCEPTED epoch, at its recorded status (e.g. `IMPLEMENTED@sha` resumes at the examine step, `REVISE` at the fix dispatch — never redo finished work) |
| Ledger exists, all ACCEPTED | Phase 3 (final examine / report), or report done |
| Ledger/worktree half-states that contradict each other | **Stop and escalate** — never "repair" by re-running setup |

## Phase 1 — Setup (once, only when the probe says fresh)

1. Validate refs: `--target`, `--from` resolve; `--into` does not already
   exist (if it does with commits beyond `--from`, that is a resume — go back
   to Phase 0's routing, don't recreate).
2. Create branch + worktree:
   ```bash
   git -C "$PROJ" branch "<--into>" "<--from>"
   git -C "$PROJ" worktree add "$WT" "<--into>"
   ```
3. Exclude the ledger, report, and skill scratch dirs from git via a
   **per-worktree** exclude — **not** the shared `info/exclude` (for a linked
   worktree `--git-common-dir` resolves to the common one, which would hide
   these paths in the user's live checkout and mask real untracked files there).
   Fixed policy, not re-litigable mid-run:
   ```bash
   git -C "$WT" config extensions.worktreeConfig true
   WEXCL="$(git -C "$WT" rev-parse --absolute-git-dir)/rebuild.exclude"
   for f in REBUILD-EPOCHS.md REBUILD-REPORT.md .rebuild/ .claude/skills/ .review-cache/; do
     grep -qxF "$f" "$WEXCL" 2>/dev/null || echo "$f" >> "$WEXCL"
   done
   G=$(git -C "$WT" config --get core.excludesFile 2>/dev/null || true)
   [ -n "$G" ] && [ -f "${G/#\~/$HOME}" ] && cat "${G/#\~/$HOME}" >> "$WEXCL"
   git -C "$WT" config --worktree core.excludesFile "$WEXCL"
   ```
   The ledger is **never committed**, on any branch, ever. `.rebuild/` (gate
   reports, findings, handoffs) is excluded from the first setup so a builder's
   `git add -A` can never sweep a gate report into a commit.
4. Create one tracking task per known phase (`rebuild/<into>: plan`,
   `…: epochs`, `…: final examine`) — per-epoch tasks are added after the
   plan exists, one per epoch row.
5. Dispatch `rebuild-plan --target … --from … --into … [--guide …] --ledger
   "$LEDGER"`. When `--gate <cmd>` was passed, record it into the ledger's §1
   ` ```gate-commands ` fenced block (one command per line) — that is the single
   source `rebuild-gate.sh` reads for the toolchain override; no skill threads
   `--gate` further. Record `--max-rounds` in the Global-rules prose. The
   no-code-before-plan gate holds by construction here (the branch sits at
   `--from`, so `git rev-list --count <from>..<into>` is 0); verify it is still
   0 when the plan reports ACCEPTED, and stop if not (code was written against a
   DRAFT plan). Do not proceed to Phase 2 until `rebuild-plan` reports the
   ledger ACCEPTED (its judge panel's verdict, not yours).

## The ledger — what this skill owns

`$LEDGER` is the single durable inter-skill contract (global rules,
dependency structure, epoch rows, coverage matrix, rulings, agent registry,
bookkeeping, deviations, safety-refs, final-examine checklist — full section
spec lives in `rebuild-plan`, which authors it). The orchestrator **owns the watchdog columns and the Rulings appends** and only
ever *reads* the rest:

**Agent registry** — one row per spawn, written **before** dispatch so a lost
handle is a ledger read, not a file-hunt:

```
| epoch | role        | taskId/agentId | outputPath | spawnedAt | lastKnownCommit | lastProgressAt | status |
| E3    | implementer | a1b2…          | <path>     | <iso8601> | <sha or ->      | <iso8601>      | active |
```

`role` ∈ `architect | plan-judge | implementer | examiner | judge`. `status`
∈ `active | done | stalled | stopped`. **The skill that spawns an agent writes
that agent's row** (it is the one holding the fresh `agentId`/`taskId`) — so
`rebuild-epoch` writes the implementer row, `rebuild-examine` its examiner/judge
rows, and the orchestrator the rows for agents it spawns directly (the plan
architect via `rebuild-plan`). The **orchestrator owns the watchdog columns**
(`lastKnownCommit`/`lastProgressAt`/`status`) on every check and on
completion/stop — those it updates on any row regardless of who created it. Two
writers never touch the same column.

**Rulings** (appends only) — when the user resolves an escalation, append the
decision here verbatim. Child judges treat rulings as binding: a finding
contradicting one is void without new evidence.

**Bookkeeping state machine** (read here, written by children) — one row per
epoch:

```
PENDING → PLANNED → IMPLEMENTED@<sha>   (gate passed)
                  ↘ GATED(fail)         (gate failed — resume by re-dispatching rebuild-epoch, or escalate)
IMPLEMENTED@<sha> → JUDGING(round n) → REVISE@<tip> → IMPLEMENTED@<sha'> (after --fix) → ACCEPTED@<lo..hi>
```

There is no `GATED(pass)` — a green gate lands directly on `IMPLEMENTED@<sha>`.
`GATED(fail)` is the only `GATED` value and is a resumable state (re-dispatch
`rebuild-epoch`, capped like a revision round, or escalate). `REVISE@<tip>`
carries the examined post-fix tip.

| Transition | Writer |
|---|---|
| row created / `PENDING→PLANNED` | `rebuild-plan` |
| `PLANNED→IMPLEMENTED@sha` or `→GATED(fail)` | `rebuild-epoch` (from the gate's JSON, never a self-report) |
| `→JUDGING(n)`, `→REVISE@tip`, `→ACCEPTED@range` | `rebuild-examine` |
| `REVISE@tip→IMPLEMENTED@sha'` | `rebuild-epoch --fix` |
| Watchdog columns (`lastProgressAt`, `status`), Rulings appends | **`rebuild` (this skill)** |
| An agent's own registry row | the skill that **spawns** that agent (it holds the handle) |

The orchestrator never edits a verdict or status it doesn't own. If a child
skill failed to write its transition (crashed mid-run), re-dispatch that
child; don't forge the row. The `ACCEPTED` post-fix tip sha is the accepted
boundary and becomes the next epoch's `lo` — read it from the ledger, never
recompute it from `git log` guesswork.

## Phase 2 — The epoch loop (strictly sequential)

**Concurrency invariant: at most one epoch's agents exist at a time.** Build
and examine share the single worktree by design — examining the just-built
tip with `rangeBIsHead:true` is what lets external reviewers run without
checkout dances. Never dispatch epoch N+1 (or its examine) while any agent
for epoch N is `active`. Parallel epochs are not a supported mode; do not
invent one.

For each epoch, in ledger order, starting from the first non-ACCEPTED row
(with `--epoch <id>`: just that one iteration, then stop):

1. **Implement.** Dispatch `rebuild-epoch --ledger "$LEDGER" --epoch <id>
   --worktree "$WT" --target <target>` (it writes its own implementer registry
   row before spawning — the orchestrator does not, to avoid two writers). Arm
   the watchdog; update the watchdog columns on that row. Await its result:
   - `IMPLEMENTED@<sha>` → step 2.
   - `GATED(fail)` (gate red after its own fix rounds) → treat as a revision
     round (step 3), capped identically.
   - `PLAN-DEFECT` → step 4.
   - stall → watchdog protocol.
2. **Examine.** Dispatch `rebuild-examine --ledger "$LEDGER" --epoch <id>
   --worktree "$WT" --target <target>` (it writes its own examiner/judge
   registry rows; do **not** forward `--max-rounds` — the orchestrator's cap
   governs this outer loop, examine's own default governs the deep-review
   workflow's internal fix loop; conflating them cripples one). Await its
   verdict:
   - `ACCEPTED@<lo..hi>` → mark the epoch's task completed; next epoch.
   - `REVISE@<tip>` + class-scoped findings file → step 3.
3. **Revision loop.** Increment the epoch's round counter (tracked in the
   epoch's task metadata; the JUDGING row carries the round). If rounds ≤
   `--max-rounds`: dispatch `rebuild-epoch --fix <findings-file>` as a
   **fresh spawn** with the class-scoped findings (never the flat instance
   list — the classes come from `rebuild-examine` verbatim; don't reshape
   them), then re-run step 2. The fix contract is a **replacement range**
   (reset to epoch base + fresh commits); if the delivered range shows an
   add-then-fix chain, that is a `rebuild-examine` gate failure, not your
   call to wave through. If rounds exceed the cap → **escalate** via
   `AskUserQuestion`: present the surviving finding classes and offer
   (a) one more round, (b) accept with the findings logged in Deviations,
   (c) `rebuild-plan --amend` (the epoch may be mis-scoped), (d) stop and
   leave state for inspection. Append the answer to Rulings, then act on it.
   Option (b) is enacted by re-dispatching `rebuild-examine` with the
   accept-ruling now in Rulings (a finding matching a ruling is void there) —
   the orchestrator never writes an `ACCEPTED` row itself; the panel does,
   honoring the ruling.
4. **PLAN-DEFECT route.** The implementer names a dependency the plan doesn't
   supply. Do not let it stub, reorder, or guess. Dispatch
   `rebuild-plan --amend "<the named defect>" --ledger "$LEDGER"` (delta
   re-judge only, per its contract), then resume the **same epoch** at step 1
   against the amended ledger. **Cap PLAN-DEFECT bounces at 2 per epoch**
   (tracked alongside the revision-round counter): a third defect on the same
   epoch means the decomposition is wrong in a way delta-amend can't fix — stop
   and escalate with all cited defects rather than looping architect + judges +
   implementer indefinitely. If an amendment changed already-ACCEPTED epochs'
   scope, stop and escalate — accepted history is not silently reopened.

Between iterations, update the epoch's tracking task and keep the registry
current. Nothing in this loop involves you reading the epoch's diff and
forming an opinion on it.

## Fresh-spawn rule (dispatch-layer, non-negotiable)

Every implementer dispatch — first attempt, `--fix` round, or
watchdog-respawn — is a **fresh agent with a written handoff**, never a
resumed one. Resumed long-context agents go idle-without-finishing; a fresh
spawn with a short handoff note works. The handoff (a file next to the
findings, path recorded in the registry) states: the epoch id + ledger path,
the current candidate range (or "none"), the findings/classes to address,
and the required output shape (`IMPLEMENTED@sha` transition + gate JSON). No
"please continue where you left off" — the old agent's context is gone and
that is the point.

## Watchdog (stall detection, while any registry row is `active`)

Silent stalls are the single biggest wall-clock loss this design exists to
prevent; the *user* must never be the one who catches them.

- **Arm:** after any dispatch, prefer a session-only `CronCreate` every ~20 min
  as the stall check — it is the general primitive that fires on every idle
  tick regardless of loop mode (`ScheduleWakeup` is `/loop`-dynamic-mode
  specific and silently no-ops outside it). Harness notifications on task
  completion remain the primary progress signal; the cron is the stall
  fallback. Delete the cron (`CronDelete`) whenever no registry row is
  `active`.
- **Progress check on each wakeup:** compare, per active row —
  latest worktree commit (`git -C "$WT" log -1 --format='%H %ct'`) vs the
  registry's `lastKnownCommit`; pre-first-commit, newest file mtime under
  `$WT` (`find "$WT" -newer <marker> …`) vs `lastProgressAt`; and the agent's
  task output tail for new content. Any movement → update
  `lastKnownCommit`/`lastProgressAt`, re-arm, done.
- **No progress (one interval):** nudge **once** — `SendMessage` to the agent
  asking for a one-line status and its next concrete action. Note the nudge
  in the registry row.
- **No progress (second consecutive interval, nudge already sent):**
  `TaskStop` the agent, set its registry row `status: stalled→stopped`, and
  respawn **fresh** per the fresh-spawn rule, handoff note included. Do not
  wait longer, do not nudge twice, do not resume.
- Judges/examiners get the same treatment; a degraded judge slot is
  `rebuild-examine`'s documented 2-of-2 fallback, not a reason to block.
- Disarm (delete crons, no further wakeups) whenever no registry row is
  `active`.

## Phase 3 — Final examine and report

When every epoch row is `ACCEPTED`:

1. Dispatch `rebuild-examine --final --ledger "$LEDGER" --worktree "$WT"
   --target <target>` (whole-branch `--from..tip` review + ls-tree parity
   diff + the ledger's Final-examine checklist).
2. Write `$WT/REBUILD-REPORT.md` (untracked, already excluded): epoch list
   with accepted ranges, deviations log, rulings, degraded reviewers/judges
   surfaced verbatim (degraded ≠ clean), residual gaps and open escalations,
   and the parity-diff outcome.
3. **Never merge. Never touch `--from`'s branch or the user's checkout.**
   Present the report, the branch name, and the worktree path; adoption (and
   worktree removal) is the user's explicit action. Leave the worktree and
   ledger in place.

## Escalation policy

Escalate to the user (via `AskUserQuestion`, answer appended to Rulings) for:
genuine design/product decisions a child returns as such, round-cap
exhaustion, contradictory ledger/worktree state, an amendment that would
reopen accepted epochs, and any child skill that is missing or whose
contract can't be followed. Do **not** escalate — because they are not your
calls at all — finding severity (rubric lives in `rebuild-examine`),
environment-vs-real gate failures (the gate script's clean-extraction JSON
decides), or plan coverage (the coverage script decides).

## Rules (non-negotiable)

- **Probe before bootstrap, every invocation.** Re-invocation resumes from
  the ledger; a duplicate `worktree add` or duplicate task set is a bug.
- **One epoch's agents at a time.** One worktree, sequential loop, no
  parallel epochs.
- **Child verdicts and deterministic reports are authoritative.** The
  orchestrator dispatches, bookkeeps, watches, escalates — it never reviews
  code, never re-scores findings, never accepts an epoch itself.
- **Fresh spawn for every revision and respawn; written handoff; never
  resume a stalled agent.**
- **Registry row before every dispatch.** A spawn that isn't in the ledger
  doesn't exist.
- **The ledger is never committed.** Excluded at setup; not re-litigable.
- **Never merge; never rewrite accepted history silently.** Accepted epoch
  boundaries come from the ledger; reopening one is a user decision.
- **Claude-only skill** (`!` injection, Skill/Agent/Workflow dispatch); not
  usable from Codex.
