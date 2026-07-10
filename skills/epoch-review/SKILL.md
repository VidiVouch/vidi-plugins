---
name: epoch-review
description: Review a large commit stack epoch by epoch — per-epoch deep-review (range mode), fixes committed onto each epoch, later work rebased forward. Claude-only outer layer over deep-review.
argument-hint: "--base <B> [ --epochs auto|<n>|<sha1,sha2,...> ] [ --nofix ] [ --max-rounds <n> ] [ --boss <model[,model2]> ] [ --focus <text> ] [ --include-uncommitted ]"
allowed-tools: Workflow, Agent, Read, Glob, Grep, AskUserQuestion, TaskCreate, TaskUpdate, TaskList, Bash(git *), Bash(mkdir *), Bash(echo *), Bash(printf *), Bash(cat *), Bash(ls *), Bash(cp *), Bash(cut *), Bash(sed *), Bash(tr *), Bash(test *), Bash(command *), Bash(wc *), Bash(sort *), Bash(tail *), Bash(head *), Bash(node *)
---

# Epoch Review

Reviews a commit stack `B..HEAD` **one epoch at a time**: each epoch's review
sees *only that epoch's own diff* (`git diff lo..hi`, never `B..hi`), verified
fixes are committed onto the epoch, and the rest of the stack is rebased
forward — so by the end the whole stack has been reviewed against the code as
it existed when each piece was written, and the artifact is a **rewritten
branch**, not a flat patch.

This is an **outer orchestration layer**. The per-epoch review engine is the
existing `deep-review` workflow (`deep-review.workflow.js`) invoked in
`mode:"range"` — do NOT reimplement multi-tool review, fan-out, boss
arbitration, or the fix loop here. This skill owns only: segmentation,
worktree isolation, the commit/rebase carry-forward loop, and the final
reconcile.

## Deterministic context (auto-populated at load)

Resolved by the shell when this skill loads — do NOT re-probe with your own Bash:

- Live checkout (PROJ): !`git rev-parse --show-toplevel`
- Current branch: !`git symbolic-ref --quiet --short HEAD 2>/dev/null || git rev-parse --short HEAD`
- HEAD (stack tip): !`git rev-parse HEAD`
- Uncommitted changes: !`test -n "$(git status --porcelain 2>/dev/null)" && echo yes || echo no`
- Toolchain: !`if test -f Cargo.toml; then echo rust; elif test -f pyproject.toml && { test -f uv.lock || grep -q '^\[tool\.uv\]' pyproject.toml 2>/dev/null; }; then echo uv-python; else echo none; fi`
- codex CLI: !`command -v codex >/dev/null 2>&1 && echo yes || echo no`
- cubic CLI: !`command -v cubic >/dev/null 2>&1 && echo yes || echo no`
- coderabbit CLI: !`command -v coderabbit >/dev/null 2>&1 && echo yes || echo no`
- grok CLI: !`command -v grok >/dev/null 2>&1 && echo yes || echo no`
- codex companion: !`C=$(ls "$HOME/.claude/plugins/cache/openai-codex/codex/"*/scripts/codex-companion.mjs 2>/dev/null | sort -V | tail -1); if [ -z "$C" ]; then echo absent; elif ! node --check "$C" 2>/dev/null; then echo "unloadable $C"; else echo "ok $C"; fi`

The tool map and `codexAdversarial`/`codexCompanion` semantics are identical to
deep-review's: `codexAdversarial` is available iff codex CLI is `yes` AND the
companion vet returned `ok <path>`; `unloadable` is surfaced loudly, not
treated as absent. `codexCompanion` in the workflow args is the **path token
after `ok `** — never the literal `ok …`/`absent`/`unloadable …` fact (`""` if
not ok). Map the injected toolchain for the workflow: `rust`→`rust`,
`uv-python`→`python`, `none`→`none`.

## Arguments

Parse `$ARGUMENTS`:

- `--base <B>` — **required**. The stack is `B..HEAD`. Stop with usage if missing.
- `--epochs auto` (default) — an agent proposes cohesive boundaries; confirm
  with the user before looping (Phase 1).
- `--epochs <n>` — split `B..HEAD` into N contiguous epochs by commit count.
- `--epochs <sha1,sha2,...>` — explicit epoch *upper-boundary* commits, oldest
  first (the last epoch implicitly ends at HEAD if the final sha isn't HEAD).
- `--nofix` — report-only: no fixing, no committing, no rebasing; just
  sequential per-epoch reports.
- `--max-rounds <n>` — per-epoch review↔fix round cap, passed through to the
  workflow (default 3).
- `--boss <model[,model2]>` — passed through to the workflow (same semantics
  as deep-review; default inherits the session model).
- `--focus <text>` — passed through to every epoch's reviewers.
- `--include-uncommitted` — after the last epoch, carry the live checkout's
  uncommitted changes into the worktree and review them as a final
  `mode:"uncommitted"` pass. Off by default: if the tree is dirty and the flag
  is absent, note in the report that uncommitted work was NOT reviewed.

## Phase 0: Validate the stack

All git ops use `git -C "$PROJ"` — the Bash tool's cwd is not guaranteed.

```bash
PROJ="<injected live checkout>"
B=$(git -C "$PROJ" rev-parse --verify "<--base value>^{commit}") || stop "base does not resolve"
ORIG_TIP=$(git -C "$PROJ" rev-parse HEAD)
TOTAL=$(git -C "$PROJ" rev-list --count "$B..$ORIG_TIP")
MERGES=$(git -C "$PROJ" rev-list --merges --count "$B..$ORIG_TIP")
```

- `TOTAL` = 0 → nothing to review; stop.
- `MERGES` > 0 → **stop**: the carry-forward rebase assumes a linear stack.
  Tell the user to linearize first (or run plain `deep-review --range`).
- `B` not an ancestor of HEAD (`git merge-base --is-ancestor "$B" "$ORIG_TIP"`
  fails) → stop with the explanation.

## Phase 1: Segmentation

Epochs are tracked as **commit counts, not shas** — `COUNTS=(n1 n2 ... nK)`
with `sum = TOTAL`. Counts survive the history rewrites; shas do not
(boundaries are re-derived from counts after every rebase in Phase 3).

- `--epochs <n>`: reject `n > TOTAL` (an epoch of zero commits is meaningless);
  otherwise split `TOTAL` into n near-equal contiguous counts (remainder
  spread over the earliest epochs). Every count must be ≥ 1, whatever the
  source of the segmentation.
- `--epochs <sha1,...,shaK>`: verify each sha is inside `B..HEAD` and in
  ancestor order; convert to counts: `n_i = git rev-list --count prev..sha_i`
  (prev starts at `B`). If the last sha isn't `ORIG_TIP`, append a final epoch
  with the remaining count.
- `--epochs auto` (default): spawn ONE agent (model: `sonnet` — segmentation
  is pattern reasoning, don't burn opus/fable on it) with the output of
  `git -C "$PROJ" log --oneline --stat --reverse "$B..HEAD"` and this brief:
  *"Propose contiguous epoch boundaries for reviewing this commit stack in
  cohesive chunks. Group by topic and file-overlap; aim for 5–15 commits per
  epoch; never split a tightly-coupled run of commits (same feature/files).
  Return JSON: {"epochs": [{"count": <int>, "label": "<short topic>"}, ...]}
  where counts sum to exactly <TOTAL> and are in stack order (oldest first)."*
  Validate the sum; then **present the proposed epochs to the user (counts,
  labels, boundary commit subjects) and require confirmation** via
  AskUserQuestion before looping. This confirmation is deliberate — a bad
  segmentation wastes the whole run.

## Phase 2: Freeze into a worktree

ALL rewriting happens on a throwaway branch in a dedicated worktree — the
user's real branch is **never** touched; they keep working in parallel.

```bash
RUN_ID="$(git -C "$PROJ" rev-parse --short "$B")-$(git -C "$PROJ" rev-parse --short "$ORIG_TIP")"
TIPBRANCH="epoch-review/$RUN_ID"
WT_BASE="${TMPDIR:-/tmp}"; WT_PREFIX="${WT_BASE%/}/epoch-review-wt"   # strip trailing slash: macOS TMPDIR ends in "/"
WT="${WT_PREFIX}/${RUN_ID}"
# No `rm`, no `git clean` — both are gated. worktree remove/add --force suffice.
git -C "$PROJ" worktree remove --force "$WT" 2>/dev/null || true
git -C "$PROJ" worktree prune
git -C "$PROJ" branch -f "$TIPBRANCH" "$ORIG_TIP"
git -C "$PROJ" worktree add --force "$WT" "$TIPBRANCH"

# The workflow's reviewers read specs from repoRoot/.claude/skills/<name>; a
# fresh worktree doesn't have the plugin's skills. Copy them in (untracked).
for s in deep-review codex-cli codex-review; do
    src="${CLAUDE_PLUGIN_ROOT}/skills/$s"
    [ -e "$src" ] && { mkdir -p "$WT/.claude/skills"; cp -R "$src" "$WT/.claude/skills/"; }
done

# Hide the copies from git entirely — a PER-WORKTREE exclude, not the shared
# info/exclude (that would leak into the live checkout). This matters beyond
# tidy `add`s: the mode:"uncommitted" tail pass tells reviewers to enumerate
# untracked files via `ls-files --others --exclude-standard`, and without this
# they'd review the skill's own spec copies as if they were the user's work.
# The exclude file lives in the per-worktree gitdir, so it needs no
# self-exclusion and dies with `worktree prune`.
git -C "$PROJ" config extensions.worktreeConfig true
EXCL="$(git -C "$WT" rev-parse --absolute-git-dir)/epoch-review.exclude"
printf '%s\n' '.claude/skills/deep-review/' '.claude/skills/codex-cli/' '.claude/skills/codex-review/' '.review-cache/' > "$EXCL"
# core.excludesFile is single-valued and worktree scope WINS — the override
# would otherwise shadow the user's global ignores (.DS_Store etc.) inside
# $WT, and step 3's `add -A` could sweep that junk into a fix commit. Append
# the global patterns so both apply.
G=$(git -C "$PROJ" config --get core.excludesFile 2>/dev/null || true)
[ -n "$G" ] && [ -f "${G/#\~/$HOME}" ] && cat "${G/#\~/$HOME}" >> "$EXCL"
git -C "$WT" config --worktree core.excludesFile "$EXCL"
```

The copied skills are untracked *and excluded* in `$WT`; the `add`/`status`
pathspecs below are belt-and-braces on top.

## Phase 3: The epoch loop

Track progress with TaskCreate/TaskUpdate — one task per epoch. `CURSOR`
starts at `$B`. For each epoch `k` with count `n_k`:

**1. Derive the boundary and check out.** Counting forward from the cursor is
what makes boundaries rebase-proof (fix commits sit *behind* the cursor):

```bash
HI=$(git -C "$WT" rev-list --reverse "$CURSOR..$TIPBRANCH" | sed -n "${n_k}p")
test -n "$HI" || stop "epoch $k: boundary count fell off the stack — counts drifted (bug), do not continue"
LO="$CURSOR"
git -C "$WT" checkout --detach "$HI"
```

**2. Run the deep-review workflow — range mode, this epoch's diff only.**
Wait for it to complete before touching git again (the loop is strictly
sequential; never run two epochs' workflows concurrently — they share `$WT`).

```
Workflow({
  scriptPath: "${CLAUDE_PLUGIN_ROOT}/skills/deep-review/deep-review.workflow.js",
  args: {
    mode: "range", rangeA: "<LO>", rangeB: "<HI>",
    rangeBIsHead: true,            // WT is checked out AT HI — externals run danceless/parallel
    focus: "<--focus text or ''>",
    repoRoot: "<WT>",
    toolchain: "python" | "rust" | "none",   // mapped from the injected fact
    fix: <true unless --nofix>,
    maxRounds: <--max-rounds or 3>,
    boss: <--boss split on ',' or []>,
    tools: { codex, codexAdversarial, cubic, coderabbit, grok, codexCompanion },  // from the injected facts
  },
})
```

Collect the result (`confirmed`, `degraded`, `rounds`) per epoch for the final
report. Under `--nofix`, set `CURSOR=$HI` and continue to the next epoch —
steps 3–4 are skipped entirely.

**3. Commit the fixes onto the epoch (if any).**

```bash
git -C "$WT" status --porcelain -- ':(exclude).claude/skills' ':(exclude).review-cache'
```

Empty → no fixes landed: `CURSOR=$HI`, next epoch. Otherwise:

```bash
git -C "$WT" add -A -- ':(exclude).claude/skills' ':(exclude).review-cache'
```

Then commit **as its own isolated Bash call** (a compound command containing
`git commit` plus `-n`-looking tokens false-positives the user-wide
block-no-verify guard) — and NEVER pass `--no-verify`; if a pre-commit hook
rejects the fixes, stop and surface the hook output:

```bash
git -C "$WT" commit -m "epoch-review: fixes for epoch <k>/<K> (<lo7>..<hi7>)"
```

`HI_PRIME=$(git -C "$WT" rev-parse HEAD)`.

**4. Rebase the rest of the stack forward.**

```bash
git -C "$WT" rebase --empty=keep --onto "$HI_PRIME" "$HI" "$TIPBRANCH"
```

`--empty=keep` preserves commits a fix made redundant, so epoch **counts stay
valid**. On success: `CURSOR=$HI_PRIME`, and `$WT` is back on `$TIPBRANCH`
(rebase checks it out) — continue to the next epoch.

**On conflict: STOP THE LOOP.** Never auto-resolve, never `rebase --abort`,
never `rebase --continue`. Report the conflicting epoch, the fix commit sha,
and the conflicted files (`git -C "$WT" diff --name-only --diff-filter=U`);
leave `$WT` intact mid-rebase for inspection and hand off to the user with:
resume = resolve in `$WT` then `git -C "$WT" rebase --continue` and re-invoke
this skill with `--base <HI_PRIME>` for the remaining epochs; discard = `git
-C "$WT" rebase --abort`.

## Phase 4: Uncommitted tail (only with `--include-uncommitted`)

The injected **Uncommitted changes** fact is load-time state and the user
keeps working during the run — re-probe now: `git -C "$PROJ" status
--porcelain` (empty → nothing to do). After the last epoch, `$WT` is on
`$TIPBRANCH`:

```bash
git -C "$PROJ" diff HEAD | git -C "$WT" apply --whitespace=nowarn
```

If the apply fails (history rewrites can invalidate the patch), skip the pass
with a **loud note** — do not review a half-applied tree. If it applies, also
copy untracked files (`git -C "$PROJ" ls-files --others --exclude-standard`,
`cp` each; on any copy failure, abort this pass loudly). Then run the workflow
once more with `mode:"uncommitted"`, same `repoRoot`/`tools`/`fix` args. Fixes
stay uncommitted in `$WT` — they ride along at adoption.

## Phase 5: Reconcile & present

When fixes landed, the artifact is the **rewritten branch** `$TIPBRANCH` —
not a patch. The user's real branch is untouched until they adopt. When no
fix commit was created (`--nofix`, or every epoch came back clean),
`$TIPBRANCH == $ORIG_TIP`: skip the range-diff and the adopt/discard section
entirely (there is nothing to adopt), delete `$TIPBRANCH`, and present the
findings report alone.

```bash
NEW_TIP=$(git -C "$PROJ" rev-parse "$TIPBRANCH")
git -C "$PROJ" range-diff "$B..$ORIG_TIP" "$B..$NEW_TIP"     # old vs new, per commit
NEW_LOCAL=$(git -C "$PROJ" rev-list --count "$ORIG_TIP..HEAD")  # user kept working?
```

Teardown (success only; on conflict Phase 3 already kept `$WT`): the branch
survives worktree removal, and the prefix guard structurally refuses to remove
anything else:

```bash
case "$WT" in
  "$WT_PREFIX"/*) git -C "$PROJ" worktree remove --force "$WT"; git -C "$PROJ" worktree prune ;;
  *) echo "refusing to remove worktree outside $WT_PREFIX: $WT" >&2 ;;
esac
```

(Skip teardown when Phase 4 left uncommitted fixes in `$WT` — removing the
worktree would destroy them; say so and give the adopt-from-worktree path.)

**Report.** For runs with more than 2 epochs or substantial findings, render
via the `html-report` skill (per-epoch sections cross-linked to the
range-diff); otherwise inline markdown:

```markdown
## Epoch Review: <branch> (<K> epochs, B=<base7> → tip <orig7> ⇒ <new7>)

**Commits:** <TOTAL> | **Fix commits added:** <count> | **Pre-flight:** skipped (epoch mode)
**Per epoch:** <k>: <label> — <n> confirmed (<rounds> rounds)<, degraded: …>

### Findings   (one table per epoch, deep-review's severity sections)
…

### Range-diff (old stack vs rewritten stack)
…

### Adopt / discard
- Adopt:   `git checkout <branch> && git reset --hard <TIPBRANCH>`   <!-- only offer verbatim when NEW_LOCAL == 0 -->
- Discard: `git branch -D <TIPBRANCH>`
```

- `NEW_LOCAL > 0` → the user committed on the real branch since the freeze;
  the reset command above would lose that work. Say so and recommend
  `git rebase --onto <TIPBRANCH> <ORIG_TIP> <branch>` instead — their call,
  do NOT run it.
- Surface every epoch's `degraded` reviewers — degraded is absence of data,
  not a clean pass.
- Dirty tree (re-probe `git -C "$PROJ" status --porcelain` now — don't trust
  the load-time fact) without `--include-uncommitted` → note the uncommitted
  work was not reviewed.

## Important rules

- **The epoch-diff invariant:** each epoch reviews `lo..hi` only — never
  `B..hi`. This is exactly deep-review's `mode:"range"`; rely on it.
- **The workflow owns the review.** No inline reviewers, no inline boss, no
  inline fixing — this skill only segments, checks out, commits, rebases.
- **Never rewrite the user's branch.** Everything happens on `$TIPBRANCH`
  inside `$WT`; adoption is the user's explicit command.
- **Conflicts stop the loop.** No auto-resolve, ever.
- **No `rm`, no `git clean`** — worktree remove/prune + `--force` add cover
  every cleanup this skill needs; teardown is prefix-guarded.
- **Isolate `git commit` calls** (block-no-verify guard gotcha) and never use
  `--no-verify`.
- **No skill-level preflight.** deep-review's Phase 0 gates the *tip* of a
  branch; mid-stack commits may legitimately fail tip-era lint. Consequence:
  the workflow's post-fix typecheck gate can surface *pre-existing* mid-stack
  errors alongside fix-induced ones — report them, they get fixed and carried
  forward, which is usually what you want, but don't claim the clean-baseline
  invariant deep-review has. It also enlarges fix commits beyond the epoch's
  own scope, raising rebase-conflict odds; if mid-stack lint noise is expected,
  passing `toolchain: "none"` suppresses the gate for the run.
- **Claude-only skill** (`!` injection + Workflow); not usable from Codex.
