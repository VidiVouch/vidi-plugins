---
name: rebuild-examine
description: Examine one rebuilt epoch (or the whole branch with --final) — independent deterministic gate, deep-review workflow pass, rebuild-specific judge panel with a shared blocking rubric, ACCEPT/REVISE verdict written to the ledger. Claude-only outer layer over deep-review.
argument-hint: "--ledger <path> --epoch <id> --worktree <path> --target <ref> [ --final ] [ --nofix ] [ --max-rounds <n> ] [ --boss <model> ] [ --judges <model> ]"
allowed-tools: Workflow, Agent, Read, Write, Edit, Glob, Grep, AskUserQuestion, TaskCreate, TaskUpdate, TaskList, Bash(python3 *), Bash(git *), Bash(bash *), Bash(sh *), Bash(jq *), Bash(mkdir *), Bash(echo *), Bash(printf *), Bash(cat *), Bash(ls *), Bash(cp *), Bash(cut *), Bash(sed *), Bash(tr *), Bash(test *), Bash(command *), Bash(wc *), Bash(sort *), Bash(comm *), Bash(tail *), Bash(head *), Bash(node *), Bash(timeout *), Bash(codex *)
---

# Rebuild Examine

Examines a **freshly rebuilt epoch** on a rebuild branch: re-runs the
deterministic gate independently of whatever `rebuild-epoch` reported, runs the
`deep-review` workflow over the epoch's range, commits verified fixes onto the
epoch tip, then convenes a **rebuild-specific judge panel** under a shared
blocking rubric. The verdict — `ACCEPT` (ledger row updated, safety refs
deleted) or `REVISE` (class-scoped findings for `rebuild-epoch --fix`) — is the
skill's output; the `rebuild` orchestrator owns the outer loop.

This is an **outer orchestration layer**. The multi-tool review engine is the
existing `deep-review` workflow (`deep-review.workflow.js`) invoked in
`mode:"range"` — do NOT reimplement fan-out, boss arbitration, or the fix loop
here. This skill owns only: the gate re-run, the workflow call, the fix
commit, the rebuild-specific panel, and the ledger/safety-ref bookkeeping.

## Deterministic context

Reviewer policy/capabilities come from deep-review's packaged preparation helper
in Phase 3. Do not auto-enable services by probing their executables. Native
Codex, the legacy companion, Cubic and CodeRabbit are explicit opt-ins; the direct
Astra/Grok lanes use named plugin agents and proxy catalog checks.

**Toolchain is NOT injected here** — load-time injection runs in the session
cwd, but the code under examination lives in the arg-supplied `--worktree`.
Probe it there, once, in Phase 0 (command below), and map for the workflow:
`rust`→`rust`, `uv-python`→`python`, `none`→`none`.

## Arguments

Parse `$ARGUMENTS`:

- `--ledger <path>` — **required**. The rebuild's `REBUILD-EPOCHS.md`.
- `--epoch <id>` — **required** unless `--final`. The epoch to examine.
- `--worktree <path>` — **required**. The rebuild worktree (owned by the
  orchestrator; this skill never creates or removes one).
- `--target <ref>` — **required**. The original branch being rebuilt (the
  floor; source of truth for no-regression and verbatim-copy comparison).
- `--final` — whole-branch mode instead of a single epoch (see Final mode).
- `--nofix` — report-only: workflow runs with `fix:false`, no fix commit; the
  verdict is still rendered but nothing is written to the ledger.
- `--max-rounds <n>` — the **deep-review workflow's own internal** review↔fix
  round cap (default 3). This is a *different knob* from the orchestrator's
  per-epoch examine↔revise cap: the orchestrator does NOT forward its
  `--max-rounds` here (that would make one flag cripple two independent loops);
  this defaults to 3 unless the operator sets it explicitly for a run.
- `--boss <model>` - passed through to the workflow (deep-review semantics, one
  model only; codex-family ids route via `codex exec`, Claude-family in-process).
- `--judges <model>` — opt-in third, non-Claude panel judge (e.g. `codex`,
  `gpt-5.6-terra`). Without it the panel is 2 Claude judges.

Stop with usage if a required arg is missing. All git ops use
`git -C "$WT"` — the Bash tool's cwd is not guaranteed.

## Phase 0: Resolve state from the ledger

```bash
WT="<--worktree>"; TARGET="<--target>"
git -C "$WT" rev-parse --verify "$TARGET^{commit}" || stop "target does not resolve"
TC=$(if test -f "$WT/Cargo.toml"; then echo rust; elif test -f "$WT/pyproject.toml" && { test -f "$WT/uv.lock" || grep -q '^\[tool\.uv\]' "$WT/pyproject.toml" 2>/dev/null; }; then echo uv-python; else echo none; fi)
```

Read the ledger. For the epoch under examination, extract:

- **Range** `LO..HI`: `HI` from the bookkeeping row's `IMPLEMENTED@sha`, or its
  `REVISE@sha` when explicitly retrying an examination blocked by review-incomplete.
  The latter retries review, not implementation, after resolving the recorded
  capability failure and any retained uncommitted edits. There is no separate
  `REVISED@` ledger status. `LO` = the previous epoch's
  `ACCEPTED@` boundary tip (or the rebuild base for the first epoch). Verify
  `HI == $(git -C "$WT" rev-parse HEAD)` — if not, **stop**: the worktree has
  drifted from the ledger and examining the wrong tip poisons everything
  downstream (this invariant is also what makes `rangeBIsHead:true` valid).
- **Epoch row**: `scope`, `dependsOn`, `acceptance`, `excludes`, `file-form`,
  `pure-data-allowlist`.
- **Rulings** section — verbatim, for the panel prompts.
- **Coverage matrix** + **dependency structure** — for the as-of-history check.
- **Safety-ref registry** — any `safety/*` refs declared for this epoch.

Ledger state must be IMPLEMENTED or a permitted re-examine after REVISE. Unless
`--nofix`, update the row to `JUDGING(round n)` before proceeding (n = prior rounds
+ 1). Every normal failure return must leave a supported resumable state, not a
completed child stranded at JUDGING.

## Phase 1: Independent deterministic gate

Re-run the gate yourself — **never** accept `rebuild-epoch`'s report of it:

```bash
GATE="${CLAUDE_PLUGIN_ROOT}/skills/rebuild-epoch/scripts/rebuild-gate.sh"
REPORT="$WT/.rebuild/gate-epoch-<id>-examine.json"
mkdir -p "$WT/.rebuild"
bash "$GATE" --ledger "<--ledger>" --epoch "<id>" --worktree "$WT" --target "$TARGET" --range "$LO..$HI" --out "$REPORT"
```

The gate runs every check against a clean `git archive` extraction (never the
nested worktree in place — that produced spurious build failures before) and
writes `{pass: bool, checks: [{name, status: ok|fail|skipped, detail}]}` to
`$REPORT`. **The JSON report is the only acceptance signal** — no agent's
self-report substitutes for it.

- `pass: false` → **skip the panel entirely**: deterministic failures need no
  judging. Emit a `REVISE` verdict now (Phase 5) with the failing checks
  rendered class-scoped, update the ledger row to `REVISE@<HI>` (no fix commit
  ran, so the rejected tip is `HI`), and stop.
- Any `skipped` check → carry it into the final report as a named degradation
  (skipped is absence of data, not a pass).

## Phase 2: Prepare the worktree for review

The workflow's reviewers read specs from `repoRoot/.claude/skills/<name>`; the
rebuild worktree doesn't have the plugin's skills. Copy them in (untracked) and
exclude them via a **per-worktree** exclude — never the shared `info/exclude`.
For a linked worktree `git rev-parse --git-path info/exclude` resolves to the
**common** exclude, so writing there would hide `.claude/skills/`,
`.review-cache/`, `.rebuild/` in the user's live checkout too (masking real
untracked files). Use the same `extensions.worktreeConfig` + per-worktree
`core.excludesFile` pattern `epoch-review` uses (the orchestrator sets this up
at worktree creation; this is the idempotent re-assert):

```bash
for s in deep-review codex-cli codex-review; do
    src="${CLAUDE_PLUGIN_ROOT}/skills/$s"
    [ -e "$src" ] && { mkdir -p "$WT/.claude/skills"; cp -R "$src" "$WT/.claude/skills/"; }
done
git -C "$WT" config extensions.worktreeConfig true
WEXCL="$(git -C "$WT" rev-parse --absolute-git-dir)/rebuild.exclude"
for p in '.claude/skills/' '.review-cache/' '.rebuild/'; do
    grep -qxF "$p" "$WEXCL" 2>/dev/null || echo "$p" >> "$WEXCL"
done
# core.excludesFile is single-valued; append the user's global patterns so both
# apply, then point THIS worktree's config at the combined file.
G=$(git -C "$WT" config --get core.excludesFile 2>/dev/null || true)
[ -n "$G" ] && [ -f "${G/#\~/$HOME}" ] && cat "${G/#\~/$HOME}" >> "$WEXCL"
git -C "$WT" config --worktree core.excludesFile "$WEXCL"
```

## Phase 3: The deep-review workflow call

Before dispatch, run the plugin's `skills/deep-review/scripts/prepare_reviewers.py`
with `python3`, `--repo "<WT>" --probe-proxy`. Use the worktree's
`.claude/deep-review.json`, or add `--config "<absolute policy path>"` when the
caller supplies an explicit policy file. Do not infer enablement from installed
CLIs. Keep the output as `REVIEWER_SETUP`. On a nonzero exit, take the
review-incomplete return path below, recording a preflight failure rather than
inventing a workflow result. Do not dispatch reviewers. This is the same bounded,
no-inference preflight documented in deep-review/references/reviewers.md.

Exactly `epoch-review`'s Phase-3 pattern. No skill-level preflight (the gate
already ran the full-tree toolchain checks). Render `focus` from the ledger row
— this is what stops generic reviewers manufacturing "missing later work"
false positives:

> This is epoch `<id>` of a staged rebuild. In scope: `<scope>`. Built on:
> `<dependsOn>`. Explicitly excluded / supplied by later epochs: `<excludes>`.
> Acceptance invariants: `<acceptance>`. Judge the code AS OF this epoch —
> anything the exclusions list as later work is out of scope and not a finding.

```
REQUEST_ARGS = {
    mode: "range", rangeA: "<LO>", rangeB: "<HI>",
    rangeBIsHead: true,            // verified in Phase 0 — externals run danceless/parallel
    focus: "<the rendered focus text>",
    repoRoot: "<WT>",
    toolchain: "python" | "rust" | "none",   // mapped from the Phase 0 probe
    fix: <true unless --nofix>,
    maxRounds: <--max-rounds or 3>,
    boss: <[--boss model] or []>,
    specDir: "${CLAUDE_PLUGIN_ROOT}/skills/deep-review/reviewers",
    reviewers: REVIEWER_SETUP.reviewers,
    tools: REVIEWER_SETUP.tools,
    namedAgents: REVIEWER_SETUP.namedAgents,
    limits: { concurrency: 3, maxAgents: 30, minRemainingTokens: 0 },
    gate: { commands: <registered current-tree checks as argv arrays>, timeoutSeconds: <bounded timeout> },
}
```

Follow deep-review/references/run-lifecycle.md. Save REQUEST_ARGS outside WT,
initialize and verify run_manifest.py, then load the entire generated args.json
as RUN_ARGS. Manifest preparation failures use the review-incomplete return path.

```
Workflow({
  scriptPath: "${CLAUDE_PLUGIN_ROOT}/skills/deep-review/deep-review.workflow.js",
  args: RUN_ARGS,
})
```

The workflow's fixers are the packaged `fixer` definition (`agents/fixer.md`,
pinned to `claude-opus-5-5[1m]`, role `fixer-opus` in
`skills/deep-review/model-roles.json`), selected by agent type with no
per-call `model`: the workflow stops a unit that sets both and refuses to start
while `namedAgents.blocked` says `CLAUDE_CODE_SUBAGENT_MODEL_FORCE` is on. Each
fixer's entry under `admission.dispatches` in the saved result records its
`agentType` and a null `model`; that entry is the fixer's run record for this
examination. This skill dispatches no fixer of its own and never passes a
model for one.

Collect the result, including `confirmed`, `degraded`, `successful`, `skipped`,
`coverage`, `attempts`, `incomplete`, and `coverageComplete`. Carry skipped and
failed lanes by name into the final report; neither is clean review evidence.
If `incomplete`, stop before committing or convening an acceptance panel. Preserve
any partial fixes and the worktree. Unless `--nofix`, write the complete workflow
result to `$WT/.rebuild/review-incomplete-epoch-<id>-round-<n>.json`, write a findings
file with **Class: review-incomplete** and recovery instructions, and set the ledger
row to `REVISE@<HI>` with both artifact paths. HEAD has not moved because Phase 4
has not run; verify that before writing the token. Do not leave JUDGING behind or
represent unavailable review as a code defect. Return this blocked class to the
dispatcher; it must pause instead of sending it to an implementer. Under `--nofix`,
report the same outcome without ledger writes. After capability recovery, explicitly
resolve any retained uncommitted edits without automatic discard/commit, then retry
this examination against the recorded tip. Optional gaps (coverageComplete=false)
remain explicit reduced coverage; the panel must not claim those reviewers succeeded.

The workflow does not supply acceptance from a typecheck agent. Follow the shared
run-lifecycle protocol: save full results outside WT; if requiresPostFixReview,
create a new manifest and run a fresh fix:false review of the resulting snapshot.
The workflow's gate unit executed run_gate.py run with the registered current-tree
checks and recorded `result.gate`; execute run_gate.py finalize only.
Only acceptance.json accepted=true allows Phase 4 or the acceptance panel. Gate
failure uses review-incomplete. For valid remaining findings, verify HEAD still
equals HI, set TIP=HI, and return class-scoped REVISE with the candidate snapshot
and partial-patch evidence; do not pretend those uncommitted changes are in HI.
The dispatcher must reconcile retained edits before another implementation attempt.
Under --nofix, report these outcomes without changing the ledger.

This current-tree gate is separate from rebuild-gate.sh's historical per-commit
checks, which still run against clean git archive extractions. Do not substitute
one for the other. Record both artifact paths and the exact epoch tip.

## Phase 4: Commit the fixes (if any)

**The workflow edits but never commits.** Check for edits and commit them onto
the epoch tip:

```bash
git -C "$WT" status --porcelain -- ':(exclude).claude/skills' ':(exclude).review-cache' ':(exclude).rebuild'
```

Empty → `TIP=$HI`, proceed to Phase 5. Otherwise:

```bash
git -C "$WT" add -A -- ':(exclude).claude/skills' ':(exclude).review-cache' ':(exclude).rebuild'
```

Then commit **as its own isolated Bash call** — the user-wide block-no-verify
guard matches any `-n`-ish token after `commit` up to the next `|`/`;`/`&`, so
no other command may share that Bash segment — and NEVER pass `--no-verify`;
if a hook rejects the fixes, stop and surface the hook output:

```bash
git -C "$WT" commit -m "examine fixes for epoch <id> (<lo7>..<hi7>)"
```

`TIP=$(git -C "$WT" rev-parse HEAD)`. When a fix commit landed, **re-run the
Phase-1 gate** against `$LO..$TIP` — fixes must not regress the deterministic
checks; a fail here is a `REVISE` exactly as in Phase 1. The **post-fix tip is
the epoch boundary**: on ACCEPT it is recorded as `ACCEPTED@$LO..$TIP` and
becomes the next epoch's `LO`. (A fix commit on top of the epoch is fine here
— the no-append rule binds `rebuild-epoch`'s *revision* delivery, not the
examine step's verified fixes.)

## Phase 5: The rebuild judge panel

Runs **in parallel** (one `Agent` call per judge, same message), covering what
generic reviewers don't. Every judge is a fresh instance for this one
examination round, per `rebuild`'s fresh-spawn rule, one handoff file per
judge: for each judge write `$WT/.rebuild/handoff-<epoch>-judge-<n>.md` first
(`<n>` = one more than the number of `judge` rows the epoch already has in the
registry, so a panel writes one file per judge with the same content and
consecutive numbers, a watchdog respawn of one judge takes the next number
alone, and no two judges ever share a file), open it with `You are a fresh
instance with no prior conversation; this message and your agent definition
are your whole brief.`, write that judge's registry row (`requestedModel` as
the call below makes it: `session` for Judge 1, `fable` for Judge 2, `sonnet`
for the Judge 3 wrapper, whose real judge is the `codex exec` model its label
names; none of these selects a packaged definition), then spawn. A later
round, a retry after review-incomplete, and the final examination (unit
`final`, see Final mode) each convene a new panel from new files; a judge from
an earlier round is never messaged or resumed, and nothing from its transcript
enters a new file beyond what the ledger records. Panel composition:

- **Judge 1** — the session model (`Agent`, no `model` override).
- **Judge 2** — `fable` (`Agent`, `model: fable`).
- **Judge 3 (only with `--judges <model>`)** — a thin Claude wrapper agent
  (`model: sonnet`, low effort, label prefixed with the real model, e.g.
  `gpt-5.5:judge3`) that writes a self-contained judging prompt, runs
  `codex exec -s read-only` per the codex-cli skill mechanics **synchronously
  in the foreground with an explicit timeout** (`timeout 900 codex exec …` —
  never backgrounded-then-polled), and returns the parsed JSON. On timeout,
  rate-limit, or parse failure it returns `degraded` with the reason — the
  panel then proceeds **2-of-2** rather than blocking epoch acceptance.

Each judge receives: the epoch row (scope/dependsOn/acceptance/excludes/
file-form/pure-data-allowlist), the ledger's Rulings section, the coverage
matrix + dependency structure, the gate report JSON, the workflow's `confirmed`
findings, and `git -C "$WT" diff $LO..$TIP` (plus read access to `$WT` and the
target via `git -C "$WT" show $TARGET:<path>`). Each judges the **four
rebuild dimensions only** (generic bug-hunting already happened in Phase 3):

1. **Target-floor / no-regression** — does the rebuilt code do everything the
   target's corresponding code does? Spot-check behavior against
   `git show $TARGET:<path>`; a capability present in the target's version of
   an in-scope path and absent here is a regression unless the ledger's
   deviations log or a ruling covers it. Improvements beyond the floor are
   fine and should be logged, not reverted.
2. **Fresh-authorship quality** — beyond the gate's mechanical blob check: is
   this genuinely re-authored (own structure, naming, decomposition), or a
   near-transcription with cosmetic edits? Paths on the `pure-data-allowlist`
   are exempt.
3. **Plan-scope conformance** — everything in `scope` is present in the range;
   nothing from `excludes` or later epochs' scope was built; `file-form` is
   honored (as-of-epoch paths carry no forward references; declared-final
   paths are complete).
4. **Semantic acceptance invariants** — each item in the epoch's `acceptance`
   holds *semantically* (demonstrably true of the code's behavior), not merely
   "it compiles". Cite evidence per invariant.

### Shared blocking rubric (binds every judge — cite the category by name)

**BLOCKING** (must be fixed before ACCEPT):

- B1 — regression vs the target floor: in-scope capability lost or behavior
  wrong relative to the target, not covered by a deviation/ruling.
- B2 — a semantic acceptance invariant that does not hold.
- B3 — verbatim or near-transcribed authorship outside the
  `pure-data-allowlist`.
- B4 — scope violation: `excludes`/later-epoch work built, or in-scope work
  missing (subject to the as-of-history check below).
- B5 — forward reference (code or prose) to work no earlier-or-this epoch
  supplies, in a path `file-form` marks as-of-epoch.
- B6 — any `fail` in the gate report (already handled deterministically;
  listed for completeness — judges never overrule the gate in either
  direction).

**NON-BLOCKING** (logged follow-up, never blocks ACCEPT): style/naming
preferences, improvements the target also lacked, prose polish, nits,
"could be simpler" observations, and anything the workflow's boss already
ruled `nit`. A judge who wants to block must name a B-category; a finding
with no B-category is a follow-up by definition — severity debates end here.

### Validity filters (apply before counting anything as blocking)

- **As-of-history / supplied-later check**: any "missing X / undefined Y /
  unimplemented Z" finding is **void** unless it cites the coverage matrix or
  dependency structure showing no later epoch supplies it. "It'll come in
  E<n>" is a complete rebuttal when the matrix says so.
- **Rulings check**: any finding contradicting an entry in the ledger's
  Rulings section is **void** unless it presents new evidence not before the
  original ruling. Void findings are listed as such in the report, not argued.

### Judge output (JSON, per judge)

```json
{ "verdict": "PASS" | "REVISE",
  "blocking":  [{ "category": "B1..B6", "file": "...", "detail": "...", "evidence": "..." }],
  "followups": [{ "detail": "..." }],
  "voided":    [{ "detail": "...", "voidedBy": "as-of-history | ruling <ref>" }],
  "degraded": false, "reason": "" }
```

### Acceptance rule

**ACCEPT** iff **2 of 3 declared judges** return PASS (**2 of 2** when the
third-party judge degraded) **and** no blocking finding from *any*
non-degraded judge survives the validity filters unaddressed. A surviving
blocking finding from a lone dissenting judge still blocks — the vote settles
overall PASS/REVISE judgment calls, never a concrete cited B-category defect.
Fewer than 2 non-degraded judges → escalate to the user via `AskUserQuestion`;
never accept on a single opinion.

## Phase 6: Verdict & bookkeeping

**On ACCEPT:**

1. Update the ledger row: `ACCEPTED@$LO..$TIP` (the post-fix tip is the
   accepted boundary and the next epoch's `LO`).
2. Delete every safety ref the ledger's Safety-ref registry **declares for
   this epoch** — and only those (`git -C "$WT" tag -d safety/<…>` or
   `update-ref -d`); mark them deleted in the registry. Never touch
   undeclared refs.
3. Append judge follow-ups to the ledger's deviations/follow-ups log.

**On REVISE:** update the epoch's bookkeeping row to **`REVISE@<TIP>`** where
`TIP` is the **post-fix tip** examined here (HEAD after any Phase-4 workflow-fix
commit — which is NOT the epoch's `IMPLEMENTED@` sha). `rebuild-epoch --fix`
reads `BAD_HI` from this `REVISE@<tip>` and verifies HEAD matches it, so a
fix-committed-then-REVISEd epoch resumes cleanly instead of tripping a
"worktree drifted" stop. Then write the findings file for `rebuild-epoch --fix`
at `$WT/.rebuild/findings-epoch-<id>-round-<n>.md`, **class-scoped, not
instance-scoped**: group findings by pattern class, name every known instance
under it, and require a verification sweep per class —

> **Class: <pattern>** (<k> known instances) — <instances> — Fix every
> instance AND sweep the whole epoch range for siblings of this pattern;
> report the sweep command/result.

— never a flat instance list (that's how each fix round only fixed the prior
round's literal instances). The ledger row is set to `REVISE@<TIP>` (per the
paragraph above) with the findings-file path recorded alongside. Under
`--nofix`, render the verdict but write nothing.

**Report** (inline markdown; both verdicts):

```markdown
## Rebuild Examine: epoch <id> (<lo7>..<tip7>) — <ACCEPT|REVISE>

**Gate:** <pass/fail> (<skipped checks, if any>) | **Workflow:** <n> confirmed, <rounds> round(s)<, fixes committed @<tip7>>
**Panel:** J1 <session>: <verdict> · J2 fable: <verdict> · J3 <model>: <verdict|DEGRADED (reason)>
**Degraded (named, absence of data — not clean):** <workflow degraded keys + skipped gate checks + degraded judges; omit if none>

### Blocking (by rubric category)      ### Follow-ups      ### Voided (with grounds)
…
```

## Final mode (`--final`)

Whole-branch examination before merge. Same phases with these substitutions:

- **Range** = `<rebuild base>..<branch tip>` (from the ledger; every epoch must
  be `ACCEPTED`, else stop and name the ones that aren't).
- **Phase 1 gate** runs over the whole range with **`--final`** added to the
  gate call (per-commit buildability included). `--final` relaxes the
  `commit_shape` check to revision-marker subjects only, skipping the
  add-then-fix heuristic — across an already-accepted whole branch a legitimate
  `fix: <target bug>` deviation commit (encouraged by the floor-not-ceiling
  rule) that touches only earlier-authored files is a false positive, not an
  appended revision.
- **Phase 3** is one workflow call over the full range, `rangeBIsHead:true`
  (verify tip == HEAD as in Phase 0); `focus` = "final whole-branch
  examination of a completed rebuild" + the ledger's global rules.
- **Add a parity diff** before the panel:
  ```bash
  git -C "$WT" ls-tree -r --name-only "$TARGET" | sort > /tmp/re-target.txt
  git -C "$WT" ls-tree -r --name-only HEAD      | sort > /tmp/re-branch.txt
  comm -23 /tmp/re-target.txt /tmp/re-branch.txt   # in target, missing from branch
  comm -13 /tmp/re-target.txt /tmp/re-branch.txt   # in branch, absent from target
  ```
  Every line must be accounted for by the ledger (excludes, deviations log,
  regenerated files) — unaccounted lines go to the panel as explicit questions.
- **Phase 5 panel** additionally receives the ledger's **Final-examine
  checklist**, each item posed as an explicit judge question; an unmet
  checklist item is B2 (semantic invariant). Final mode has no epoch, so its
  unit is `final`: each judge's registry row carries `final` in the epoch
  column and its handoff file is `$WT/.rebuild/handoff-final-judge-<n>.md`,
  numbered from the registry's `final` rows exactly as an epoch's are.
- **On ACCEPT**: mark the rebuild `FINAL-ACCEPTED@<tip>` in the ledger and
  delete any remaining declared safety refs. **Never merge** — that is the
  user's call, via the orchestrator's report.
- **On REVISE in final mode**: a whole-branch finding implicates
  already-ACCEPTED history, which no single `rebuild-epoch --fix` owns —
  **escalate to the user** via `AskUserQuestion` (which epoch to reopen, or
  accept-with-logged-deviations), append the decision to the ledger's Rulings,
  and return the verdict to the orchestrator. Never silently reopen an accepted
  epoch.

## Important rules

- **The gate's JSON is the only deterministic-acceptance signal.** Never
  substitute any agent's self-report; never let a judge overrule a gate `fail`.
- **The workflow owns the generic review.** No inline reviewers, no inline
  boss, no inline fixing; the panel judges only the four rebuild dimensions.
- **Isolate `git commit` calls** (block-no-verify guard gotcha) and never use
  `--no-verify`.
- **This skill never creates/removes worktrees, never resets history, never
  merges.** Revision delivery (replacement ranges) is `rebuild-epoch`'s job.
- **Post-fix tip = epoch boundary.** Written explicitly on ACCEPT so range
  accounting can't drift.
- **Safety refs die only on ACCEPT**, and only the ones the ledger declares.
- **Degraded ≠ clean.** Every degraded reviewer, skipped gate check, and
  degraded judge is named in the report.
- **Fresh judges, each from its own written handoff file, every round.** No
  judge is resumed, messaged after it returns, or reused across rounds or
  epochs.
- **Blocking needs a B-category; missing-work needs a matrix citation;
  ruled-on needs new evidence.** Everything else is a follow-up or void.
- Approval gates. Fix commits and ACCEPT bookkeeping are the invoked contract.
  Pushing, enabling reviewers, editing user settings, and paid reviewer calls
  beyond the run follow deep-review/references/approvals.md and wait for the user.
- **Claude-only skill** (`!` injection + Workflow); not usable from Codex.
