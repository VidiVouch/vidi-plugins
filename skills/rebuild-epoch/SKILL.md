---
name: rebuild-epoch
description: Fresh-author exactly ONE epoch of a rebuild (or revise it as a clean replacement range after a REVISE verdict) — spawns an implementer agent bound to the ledger's epoch contract, runs the deterministic rebuild-gate.sh, and accepts ONLY the gate's JSON report, never the implementer's self-report. Returns IMPLEMENTED@sha, REVISED@sha, or PLAN-DEFECT.
argument-hint: "--ledger <path> --epoch <id> --worktree <path> --target <ref> [ --fix <findings-file> ] [ --gate <cmd> ]"
allowed-tools: Agent, Read, Write, Edit, Glob, Grep, AskUserQuestion, TaskCreate, TaskUpdate, TaskList, TaskOutput, Bash(git *), Bash(bash *), Bash(sh *), Bash(mkdir *), Bash(mktemp *), Bash(echo *), Bash(printf *), Bash(cat *), Bash(ls *), Bash(cut *), Bash(sed *), Bash(awk *), Bash(tr *), Bash(test *), Bash(command *), Bash(wc *), Bash(sort *), Bash(head *), Bash(tail *), Bash(grep *), Bash(date *), Bash(python3 *)
---

# Rebuild Epoch

Builds (or revises) **exactly one epoch** of a staged rebuild: reads the epoch's
contract from the ACCEPTED `REBUILD-EPOCHS.md` ledger, spawns **one fresh
implementer agent** to author the epoch's commits on the rebuild branch, then
runs the deterministic gate and treats **its JSON report as the only acceptance
signal**. The `rebuild` orchestrator owns the worktree, the outer loop, and the
watchdog; `rebuild-examine` owns judging. This skill owns only: the epoch
contract handoff, the implementer spawn, the gate run, and the ledger
bookkeeping for this epoch.

Three possible results, returned explicitly (the orchestrator routes on them):

- **`IMPLEMENTED@<sha>`** — the epoch range is built, gate-green, recorded.
- **`REVISED@<sha>`** — a `--fix` re-entry delivered a clean replacement range.
- **`PLAN-DEFECT: <missing dependency>`** — the plan is wrong; route to
  `rebuild-plan --amend`. Never "fixed" here by stubbing or reordering.

## Arguments

Parse `$ARGUMENTS`:

- `--ledger <path>` — **required.** The rebuild's `REBUILD-EPOCHS.md`.
- `--epoch <id>` — **required.** The epoch to build (e.g. `E3`).
- `--worktree <path>` — **required.** The rebuild worktree. This skill **never
  creates or removes worktrees** — the orchestrator owns them; a missing
  worktree is a stop-and-report, not a `git worktree add`.
- `--target <ref>` — **required.** The read-only reference tree being
  re-expressed (the content floor; also the gate's verbatim-copy comparator).
- `--fix <findings-file>` — re-entry after a `REVISE` verdict: the class-scoped
  findings file written by `rebuild-examine`. Switches to **Revision mode**.
- `--gate <cmd>` — custom full-tree gate command, passed through to
  `rebuild-gate.sh --gate-cmd` (portability escape hatch for repos with no
  mapped toolchain; without it the script detects rust / uv-python itself).

Stop with usage if a required arg is missing. All git ops use
`git -C "$WT"` — the Bash tool's cwd is not guaranteed.

## Phase 0: Preconditions (all mechanical, all before any spawn)

```bash
WT="<--worktree>"; LEDGER="<--ledger>"; EPOCH="<--epoch>"; TARGET="<--target>"
test -d "$WT" || stop "worktree missing — the orchestrator owns worktree creation"
git -C "$WT" rev-parse --verify "$TARGET^{commit}" || stop "target does not resolve"
grep -q '^Plan-status: ACCEPTED' "$LEDGER" || stop "plan not ACCEPTED — run rebuild-plan first; no epoch may be built against a DRAFT plan"
git -C "$WT" ls-files --error-unmatch "$(basename "$LEDGER")" 2>/dev/null && stop "ledger is tracked — it must be untracked (.git/info/exclude); fix before building"
```

From the ledger, extract — verbatim, into the implementer brief:

- The **epoch row** (§3): `name / purpose / scope / dependsOn / target-ref /
  acceptance / excludes / file-form / pure-data-allowlist`. A row missing
  `file-form` or `pure-data-allowlist` → stop: the plan is incomplete;
  escalate to `rebuild-plan --amend`, don't improvise the fields.
- The **global rules** (§1), including the `prose-sweep-patterns` and
  `gate-commands` fenced blocks.
- The **dependency structure** (§2) and **coverage matrix** (§4) — the
  implementer's as-of-history ground truth.
- The **rulings** (§5) — standing decisions the implementer must not
  re-litigate.
- The **bookkeeping row** (§7) for this epoch, which also yields the range
  base: `LO` = the previous epoch's `ACCEPTED@…` tip (or the rebuild base for
  the first epoch).

State checks on the bookkeeping row:

- Build mode: status must be `PENDING`/`PLANNED`. `IMPLEMENTED@…` or later →
  stop ("epoch already built — did you mean --fix?").
- Revision mode (`--fix`): status must be `REVISE@<tip>` and the findings file
  must exist and be non-empty. `BAD_HI` = the `<tip>` sha recorded on that row
  by `rebuild-examine` — the **post-fix** tip it examined (which is HEAD after
  examine committed any workflow fixes, so it is NOT the same as the epoch's
  `IMPLEMENTED@` sha; reading it from the ledger, not assuming it, is what
  avoids the "worktree drifted" false stop).
- Either mode: `git -C "$WT" rev-parse HEAD` must equal `LO` (build mode) or
  `BAD_HI` (revision mode). Any other sha → **stop**: the worktree genuinely
  drifted from the ledger; report, never "adopt" the drift.

Write the **agent registry row** (§6) *before* spawning — `epoch, role
(implementer), agentId/taskId, outputPath, spawnedAt, lastKnownCommit=LO` — so
the orchestrator's watchdog and any post-crash recovery read state from the
ledger, never from a file hunt.

## Phase 1: The implementer spawn (build mode)

Spawn **one fresh implementer agent** (session model) with
**`run_in_background: true`**, then await its completion **idly** — return to
the loop / let the turn go idle and resume on the task-completion notification;
do **not** sit in a blocking foreground wait. This is load-bearing for the
watchdog: a synchronous foreground agent (`run_in_background: false`, or a
blocking `TaskOutput`) pins the conversation mid-turn, and the orchestrator's
`ScheduleWakeup`/`CronCreate` stall checks only fire while the loop is idle — a
foreground implementer would re-open the exact silent-stall failure this design
exists to prevent. Record the returned `agentId`/`taskId` in the registry row
(Phase 0) so the watchdog and any recovery read the handle from the ledger.

The brief contains the Phase 0 extracts plus these binding rules, verbatim:

> **Fresh-author only.** Study the target with `git -C <WT> show <TARGET>:<path>`
> and `git -C <WT> ls-tree -r <TARGET> <dir>` — **never** check the target out,
> never copy files, never cherry-pick or paste commits. (Checkout-and-copy also
> trips the stale-virtual-FS footgun: `Read`/`Write` may serve or drop worktree
> content silently — extract via `git show`, verify every write against disk
> with `git diff`/`cat`.) Understand each piece, then write it again. Verbatim
> is legal ONLY for paths on this epoch's `pure-data-allowlist` — the gate
> diffs every changed blob against the target and fails on any other exact
> match.
>
> **Final names, not final wiring.** Use the target's *final* vocabulary from
> the first commit (no rename/refactor commits — fold renames into origins).
> But author each file **correct as of this epoch**: the target's final form
> of a file often imports, calls, registers, or documents things a later epoch
> supplies — strip those. `file-form` says which of your paths are
> *as-of-epoch* (strip forward references) vs genuinely *final*. Before every
> commit, grep your work for symbols, paths, and prose claims that nothing in
> `dependsOn`-closure history supplies.
>
> **Stub nothing; reorder nothing; return PLAN-DEFECT instead.** If something
> in scope genuinely needs a dependency the ledger's plan does not supply by
> this epoch (check the coverage matrix and dependency structure first — most
> "missing" things are later epochs' declared work you must simply not
> reference), stop and return `PLAN-DEFECT: <the missing dependency, named,
> with the ledger evidence>`. Do not stub it, import it early, silently
> reorder, or quietly widen your scope.
>
> **Regenerate, never copy, derived artifacts**: lockfiles (`cargo` /
> `uv lock`), migrations, generated code, wheels — regenerated by their tools
> in this tree, never lifted from the target.
>
> **Floor, not ceiling.** If the target's code has a real bug or a clear
> improvement not scoped to a later epoch: apply the fix/improvement, add a
> row to the ledger's deviations log (§8: `epoch · file:line · was → now ·
> why · test`), and say so in your report. Escalate genuine design/product
> decisions instead of deciding them. Never silently reproduce a known flaw;
> never silently fix one either — every deviation is logged.
>
> **Commit granularly, every commit through the hooks.** Never `--no-verify`.
> Run each `git commit` as its **own isolated Bash call** — the user-wide
> block-no-verify guard matches any `-n`-ish token after `commit` up to the
> next `|`/`;`/`&`, so no other command may share that Bash segment. Commit
> subjects: short, natural, the concrete change — and **no revision-marker
> subjects** (`fix:` chains over your own range, "address review", "re-author"
> — the gate's commit-shape check rejects them).
>
> **Do not touch** the ledger file (except the deviations log rows above), any
> path outside your scope, or any other branch. Do not run the acceptance gate
> "for the record" — your self-report is not the signal; the skill runs the
> gate itself after you return.
>
> Report back: the commit range you authored (`<LO>..<sha>` with subjects),
> deviations logged, anything flagged for the examine panel.

If the implementer returns `PLAN-DEFECT`: verify its ledger citation is real
(read the matrix rows it cites), update the bookkeeping row back to `PLANNED`
with a note, and **return `PLAN-DEFECT: <detail>` upward** — this skill never
amends the plan itself.

## Phase 2: The deterministic gate (the ONLY acceptance signal)

Run the gate yourself, after the implementer returns — **never accept its
self-report of "all gates green"** (every implementer that failed before
self-reported green):

```bash
HI=$(git -C "$WT" rev-parse HEAD)
GATE="${CLAUDE_PLUGIN_ROOT}/skills/rebuild-epoch/scripts/rebuild-gate.sh"
REPORT="$WT/.rebuild/gate-epoch-<id>.json"
mkdir -p "$WT/.rebuild"
bash "$GATE" --repo "$WT" --target "$TARGET" --base "$LO" --tip "$HI" \
  --epoch "$EPOCH" --ledger "$LEDGER" --output "$REPORT" \
  <if --gate given: --gate-cmd "<cmd>"> \
  <in revision mode: --not-descendant-of "$BAD_HI">
```

The gate resolves its toolchain command from `--gate-cmd` if given, else the
ledger's `gate-commands` block (the orchestrator records the run's `--gate`
override there); a tree with no toolchain and no gate command **fails closed**
(nothing verified is not a pass). In **revision mode**, passing
`--not-descendant-of "$BAD_HI"` makes the gate prove the replacement range was
actually reset to `LO` (the rejected tip is not in the new tip's ancestry) —
catching an appended revision that slips innocuous commit subjects past the
shape heuristic.

The script gates from clean `git archive` extractions (never the nested
worktree in place — in-place builds inside `.claude/worktrees/…` produce
spurious failures), checks per-commit buildability, the verbatim-blob sweep
against `pure-data-allowlist`, commit shape, ledger untracked-ness, and
safety-ref declarations; it reads the allowlist / prose-sweep patterns /
safety-ref declarations from the ledger's fenced blocks. Read `$REPORT`
(exit 0 + `"pass": true` = green):

- **Pass** → Phase 3.
- **Fail** → hand the failing checks' `detail` back to a **fresh** implementer
  spawn (never resume the first one — resumed agents go idle; a fresh spawn
  with a written handoff works) with the same brief plus the report excerpt,
  cap at **2** gate-fix rounds, re-run the gate after each. Still failing →
  update the row to `GATED(fail)` and stop with the report; the orchestrator
  escalates.
- Any `skipped`/`not_assessed` check → name it in the final report verbatim
  (absence of data, not a pass). A `skipped` toolchain gate on a repo that
  *has* a toolchain is a stop-and-investigate, not a note.

## Phase 3: Record & hand off

```bash
HI=$(git -C "$WT" rev-parse HEAD)   # re-read: gate-fix rounds may have moved it
```

Update the ledger:

- Bookkeeping row (§7) → **`IMPLEMENTED@<HI>`** with the range `<LO>..<HI>` —
  in **both** build and revision modes. `IMPLEMENTED@<sha>` is the canonical
  state-machine token the plan schema, the orchestrator, and
  `rebuild-examine`'s range reader all key on; a revision produces a fresh
  candidate range that is examined exactly like a first build, so it re-enters
  the machine at the same `IMPLEMENTED@` state. `REVISED@<HI>` is only the
  **spoken return verb** below (it tells the orchestrator "this was a `--fix`
  re-entry, re-examine"), never a ledger status.
- Agent registry row's `lastKnownCommit` → `HI`.
- `HI` **must equal the branch HEAD** at return — load-bearing for
  `rebuild-examine`'s `rangeBIsHead:true` (external reviewers run danceless).

Report (spoken return to the orchestrator): `IMPLEMENTED@<HI>` (build mode) or
`REVISED@<HI>` (revision mode), range `LO..HI`, commit count + subjects, gate
report path + any skipped/not_assessed checks by name, deviations logged, flags
for the examine panel.

## Revision mode (`--fix <findings-file>`)

Re-entry after `rebuild-examine` returned `REVISE`. **The delivery contract is
a replacement range, never appended fix commits**: fixups can't be merged,
split, dropped, or reordered into the clean granular history the rebuild
exists to produce — an appended `fix:` chain on a "complete" range is exactly
the shape the gate's commit-shape check rejects. So:

1. **Safety ref first** (the rejected range is about to become unreachable).
   `BAD_HI` is the rejected tip read from the ledger's `REVISE@<tip>` row in
   Phase 0 — and it must equal the current HEAD (verified there):

   ```bash
   git -C "$WT" tag "safety/<epoch>-r<round>-$(git -C "$WT" rev-parse --short "$BAD_HI")" "$BAD_HI"
   ```

   Add it to the ledger's **safety-ref registry** (§9) *and* its
   ` ```safety-refs ` fenced block **before** the reset — the gate fails on any
   undeclared `safety/*` ref. Lifecycle is fixed: created now, **deleted by
   `rebuild-examine` on ACCEPT** — never deleted here, never left undeclared.

2. **Reset to the epoch base** (the replacement range starts clean):

   ```bash
   git -C "$WT" reset --hard "$LO"
   ```

3. **Fresh implementer spawn — never resume the original.** The transcript
   record is 3-for-3: resumed agents go idle without finishing; a fresh spawn
   with a written handoff works every time. The handoff = the full Phase 1
   brief **plus**, verbatim, the class-scoped findings file: each class names
   a pattern and its known instances and requires fixing **every instance and
   sweeping the whole epoch for siblings of the pattern** — not just the
   listed instances (instance-by-instance fixing is how each round only fixed
   the previous round's literal list). The brief states plainly: *you are
   re-authoring this epoch from its base; the prior attempt is gone; deliver
   the complete epoch again with the finding classes resolved throughout.*

4. Phases 2–3 run identically. On success the bookkeeping row → `IMPLEMENTED@<HI>`
   (the canonical state token — a re-examine follows exactly as for a first
   build); the spoken return verb is `REVISED@<HI>` so the orchestrator knows
   to re-examine. The safety ref stays declared until `rebuild-examine`
   deletes it on ACCEPT.

A `--fix` round that surfaces a genuine plan defect returns `PLAN-DEFECT`
exactly as in build mode — the findings don't change the routing rule.

## Important rules

- **The gate's JSON report is the only acceptance signal.** No implementer
  self-report, ever — the skill runs `rebuild-gate.sh` itself and reads the
  file.
- **One epoch per invocation. Scope is the ledger row.** Nothing from
  `excludes` or later epochs; everything in `scope`.
- **Revisions are replacement ranges** — safety-ref, reset to `LO`, fresh
  commits. Appended fix commits are a contract violation the gate rejects.
- **Fresh spawn for every implementer round** (initial, gate-fix, revision) —
  never resume; hand off in writing.
- **`PLAN-DEFECT` is a first-class result**, not a failure to route around.
  Verify the citation, return it upward; never stub, reorder, or amend here.
- **Final names, not final wiring** — target vocabulary from the first commit;
  forward references stripped per `file-form`.
- **Regenerate derived artifacts; never copy them.**
- **Every deviation from the target is logged** in the ledger's §8 — bug
  fixes and improvements included; silent in either direction is a violation.
- **This skill never creates/removes worktrees, never merges, never touches
  the target ref or any other branch.**
- **Isolate `git commit` calls** (block-no-verify guard gotcha) and never use
  `--no-verify`; never delete safety refs (that is `rebuild-examine`'s ACCEPT
  step).
- **The ledger stays untracked** — verified in Phase 0; the gate re-verifies.
- Approval gates. Epoch commits on the rebuild branch are the invoked contract.
  Pushes, merges, user settings edits, and paid calls outside the run follow
  deep-review/references/approvals.md and wait for the user.
- **Claude-only skill** (Agent orchestration); not usable from Codex.
