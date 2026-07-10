---
name: deep-review
description: Orchestrated multi-tool code review — a deterministic workflow (review → boss-arbitrated judgment → fix → reconverge)
argument-hint: "[ --base <branch> ] [ --uncommitted ] [ --commit <sha> ] [ --range <a>..<b> ] [ --nofix ] [ --max-rounds <n> ] [ --boss <model[,model2]> ] [ --in-place ] [ --nofix-preflight ] [ --skip-preflight ] [ --focus <text> ] [ --force ]"
allowed-tools: Bash(uv run *), Bash(uv lock *), Bash(cargo *), Bash(git diff *), Bash(git log *), Bash(git ls-files *), Bash(git checkout *), Bash(git symbolic-ref *), Bash(git rev-parse *), Bash(git rev-list *), Bash(git hash-object *), Bash(git worktree *), Bash(git fetch *), Bash(git apply *), Bash(git add *), Bash(git write-tree *), Bash(git reset *), Bash(shasum *), Bash(mkdir *), Bash(echo *), Bash(ls *), Bash(cat *), Bash(cp *), Bash(find *), Bash(grep *), Bash(sed *), Bash(paste *), Bash(readlink *), Bash(dirname *), Bash(printf *), Bash(sort *), Bash(tail *), Bash(cut *), Bash(tr *), Bash(command *), Bash(test *), Bash(codex *), Bash(cubic *), Bash(coderabbit *), Bash(grok *), Bash(node *), Workflow, Agent, Read, Glob, Grep, TaskCreate, TaskUpdate, TaskList
---

# Deep Review

Multi-tool orchestrated code review. The heavy lifting — fan-out to every
available reviewer, adversarial per-finding verification, optional fixing, and
the decision to loop — runs as a **deterministic JavaScript workflow**
(`deep-review.workflow.js`), not as model-improvised orchestration. This skill
is the thin, deterministic front end: it populates the environment facts (mostly
at load time via `` !` `` injection), runs the pre-flight CI gate, launches the
workflow, and presents its structured result.

Each reviewer's launch prompt, output parsing, and degradation detection live in
a per-reviewer spec under `reviewers/` — the workflow tells each reviewer
subagent to read its own spec, so the specs stay the single source of truth.

## Deterministic context (auto-populated at load)

These are mode-independent facts, resolved deterministically by the shell when
this skill loads — do NOT re-probe them with your own Bash calls:

- Live checkout (project dir): !`git rev-parse --show-toplevel`
- Current branch: !`git symbolic-ref --quiet --short HEAD 2>/dev/null || git rev-parse --short HEAD`
- HEAD (freeze point): !`git rev-parse HEAD`
- Toolchain: !`if test -f Cargo.toml; then echo rust; elif test -f pyproject.toml && { test -f uv.lock || grep -q '^\[tool\.uv\]' pyproject.toml 2>/dev/null; }; then echo uv-python; else echo none; fi`
- codex CLI: !`command -v codex >/dev/null 2>&1 && echo yes || echo no`
- cubic CLI: !`command -v cubic >/dev/null 2>&1 && echo yes || echo no`
- coderabbit CLI: !`command -v coderabbit >/dev/null 2>&1 && echo yes || echo no`
- grok CLI: !`command -v grok >/dev/null 2>&1 && echo yes || echo no`
- codex companion: !`C=$(ls "$HOME/.claude/plugins/cache/openai-codex/codex/"*/scripts/codex-companion.mjs 2>/dev/null | sort -V | tail -1); if [ -z "$C" ]; then echo absent; elif ! node --check "$C" 2>/dev/null; then echo "unloadable $C"; else echo "ok $C"; fi`
- Cached reviews: !`R=$(find .review-cache -maxdepth 1 -name '*.hash' 2>/dev/null | sed 's#.*/##;s/\.hash$//' | paste -sd' ' -); echo "${R:-(none)}"`
- Cache verdict (default `--base main` only): !`H=$(git diff main...HEAD 2>/dev/null | shasum -a 256 | cut -d' ' -f1); C=$(cat .review-cache/main.hash 2>/dev/null || true); P=$(test -f .review-cache/main.fixes.patch && echo " (prior fixes: .review-cache/main.fixes.patch)"); if [ -n "$H" ] && [ "$H" = "$C" ]; then echo "MATCH ${H} — no changes since last review; STOP unless --force${P}"; else echo "MISS ${H:-<no-diff>} — changed or never reviewed; proceed"; fi`
- Fix strategy: !`[ "${CLAUDE_CODE_EXPERIMENTAL_AGENT_TEAMS:-0}" = "1" ] && echo "teams" || echo "workflow-subagents"`

**Grounding — two distinct roots.** `CLAUDE_PROJECT_DIR` is *not* set in this
shell (it is injected only for hook commands), so the stable ground is the
**Live checkout** above, captured here at load in the live repo. Once Phase 1.5
freezes a worktree, the code under review lives at a *second* path:

- `projectDir` = the **Live checkout** — where the skill, reviewer specs, and
  `.agents` tooling actually live (several are untracked, so they exist *only*
  here). The user keeps working here in parallel.
- `repoRoot` = the **frozen worktree** — a snapshot of the review's start state
  that reviewers read and `--fix` edits, so nothing touches the live checkout.

`codexAdversarial` is available iff `codex CLI` is `yes` AND the `codex
companion` vet returned `ok <path>`. That vet is a cheap **availability** gate
only: `absent` = plugin not installed (skip Agent C quietly); `unloadable
<path>` = present but `node --check` fails (corrupt, or needs a newer node) —
surface loudly in the run header, don't treat it as merely absent. It
deliberately does **not** try to verify the companion's *contract* (subcommand,
flags, output envelope): that can't be done without invoking it, and a static
guess would cry wolf on a working reviewer. Contract drift is caught where it is
actually observable — at run time (a rejected flag/subcommand exits with an
arg-parse error) and post-run (the `codex-adversarial` reviewer spec validates
the `.result.findings` envelope and returns a loud `CONTRACT-DRIFT`
marker instead of a false clean pass). The `codexCompanion` path (the token
after `ok`) and `repoRoot` are threaded into the workflow `args` so each
external-tool reviewer targets the frozen worktree (the Bash tool spawns a fresh
shell per call, so a bare invocation could otherwise silently scan the live
checkout).

## Arguments

Parse the user's args (`$ARGUMENTS`) for:

- `--base <branch>` — base branch to compare against (default: `main`)
- `--uncommitted` — review uncommitted changes (staged + unstaged) instead of branch diff
- `--commit <sha>` — review a single commit
- `--range <from>..<to>` — review a range of commits
- `--nofix` — report only, a single pass, no edits. **Default is `--fix`**: apply
  verified fixes and loop until the tree converges (fixes stay isolated in the
  worktree and come back as an opt-in patch — the live checkout is never touched).
- `--max-rounds <n>` — cap on review↔fix rounds (default: 3)
- `--boss <model[,model2]>` — the **arbiter** with final say over the panel (dedup →
  real-vs-noise → severity → verdict). Default: **inherit the session model** (the
  runtime is Claude, always). `--boss codex` = gpt-5.x via `codex exec` — right for
  **logic**-shaped arbitration (correctness/repro; high intelligence, near-free).
  `--boss fable` / `--boss opus` for **taste**-shaped (design/API/copy) — gpt-5.x's
  taste is weak, and a wrong-tiered boss is worse than none. Two comma-separated
  models = `logicBoss,tasteBoss` (domain-authoritative merge). Codex-family ids
  (`codex`,`gpt-5.x`,`sol`,`terra`,`luna`) route to `codex exec -s read-only`;
  Claude-family (`opus`/`fable`/`sonnet`) run in-process.
- `--nofix-preflight` — opt out of auto-fixing pre-flight failures (default is to fix)
- `--skip-preflight` — skip Phase 0 CI checks (use when you know they pass)
- `--focus <text>` — pass focus text to the reviewers
- `--force` — run even if diff is unchanged since last review
- `--in-place` — skip the frozen worktree; review (and, with `--fix`, edit) the
  live checkout directly. Default is to freeze into a worktree so the user can
  keep working in parallel.

If no args are provided, default to `--base main`.

**Diff modes** — the mode determines the diff command and the `args.mode` value
passed to the workflow:

| Flag                    | `args.mode`   | Git diff command            |
|-------------------------|---------------|-----------------------------|
| `--base main` (default) | `base`        | `git diff main...HEAD`      |
| `--uncommitted`         | `uncommitted` | `git diff HEAD`             |
| `--commit abc123`       | `commit`      | `git diff abc123^..abc123`  |
| `--range abc..def`      | `range`       | `git diff abc..def`         |

In `--uncommitted` mode, `git diff HEAD` alone does **not** see untracked files;
Phase -1 mixes per-file content hashes into the dedup hash so new/edited
untracked files invalidate the cache, and the workflow's reviewer prompts fetch
untracked contents explicitly (this is documented in the reviewer specs).

## Phase -1: Diff-hash deduplication

**Default invocation (no args → `--base main`):** the **Cache verdict** injected
above already decided this deterministically at load — if it says `MATCH` and
`--force` was not passed, stop now with that message (point the user at the prior
`.fixes.patch` if the verdict names one — same diff means the same fixes); if
`MISS`, proceed straight to Phase 0. Skip the bash below.

**Any explicit mode** (`--uncommitted` / `--commit` / `--range`, or a non-`main`
base): the hash is mode-dependent and can't be injected at load, so compute it: 

```bash
mkdir -p .review-cache
DIFF_PAYLOAD="$(<diff command for the selected mode>)"
if [ "$MODE" = "uncommitted" ]; then
    UNTRACKED_META=$(git ls-files --others --exclude-standard | sort | while IFS= read -r f; do
        # hash-object errors on a symlink-to-directory (`git ls-files` lists the
        # link, hash-object can't blob a dir). Fall back to the link target so a
        # retarget is still caught, and swallow the stderr either way.
        h=$(git hash-object -- "$f" 2>/dev/null || printf 'link:%s' "$(readlink "$f" 2>/dev/null)")
        printf '%s %s\n' "$h" "$f"
    done)
    DIFF_PAYLOAD="$DIFF_PAYLOAD"$'\n'"UNTRACKED:"$'\n'"$UNTRACKED_META"
fi
DIFF_HASH=$(printf '%s' "$DIFF_PAYLOAD" | shasum -a 256 | cut -d' ' -f1)
CACHE_KEY=$(echo "<mode identifier>" | tr '/' '_' | tr '.' '_')
CACHE_FILE=".review-cache/${CACHE_KEY}.hash"
```

Cache key examples: `main`, `uncommitted`, `commit_abc123`, `range_abc_def`.

If the cache file exists and contains the same hash, and `--force` was not
passed, tell the user "No changes since last review (hash: `<short-hash>`). Use
--force to re-review." and stop. `--force` is the single override for every mode;
if a `.review-cache/<key>.fixes.patch` exists, point the user at it — the diff is
unchanged, so the previously computed fixes still apply.

Otherwise, **write the hash NOW, before reviewing** (`echo "$DIFF_HASH" >
"$CACHE_FILE"`), then continue. Recording it at the *start* — not the end — is
what makes the cache do its job: it marks "this exact diff has been taken on for
review," so a repeat invocation (or a fix round that produced a no-op tree
identical to one already reviewed) sees the same hash and stops re-doing
identical work. Writing at the end would leave an interrupted or crashed run
unrecorded, inviting a full re-review of a diff already reviewed.

**Exception — `--uncommitted` with a real toolchain:** Phase 0's auto-fixes
rewrite the very tree that `git diff HEAD` hashes, so a hash recorded before
preflight describes a tree the panel never reviews (and a wrong skip is
constructible if the preflight changes are later reverted). In that one case,
defer the write: after Phase 0 completes, re-run this phase's hash computation
(diff + untracked block) and write *that* post-preflight hash — still before any
review work starts. Every other mode hashes committed state, which preflight
cannot change; write immediately as above.

(Default `--base main`: the injected **Cache verdict** already computed the hash;
on `MISS`, run `mkdir -p .review-cache`, then write it to `.review-cache/main.hash`
here at the start, same as above.)

**Tradeoff — `--force` is the escape hatch.** Writing at the start means a run
that *degraded* (a reviewer crashed / rate-limited / hung) is still recorded as
reviewed, so a plain re-run will skip it. That's intentional: re-review is
explicit via `--force`, which ignores the cache in every mode. If a degraded run
needs redoing, re-run with `--force`.

## Phase 0: Pre-flight CI checks

Before spending tokens, verify the code passes the same checks as CI. **Run this
in the live checkout** (before Phase 1.5 freezes the worktree): pre-flight
auto-fixes are just formatting/lint, low-risk, and belong in the user's tree —
unlike the review loop's logic fixes, which stay isolated in the worktree. The
worktree is then frozen *from the post-preflight state*, so it inherits a clean
baseline.

This establishes the **clean baseline invariant** the workflow relies on: if
pre-flight passes, lint/types are clean before any review-loop fix runs, so any
lint/type error the fix loop later surfaces is provably fix-induced and in scope.

Detect the toolchain from the injected **Toolchain** fact. This is a capability
gate, not a best-effort command probe:

- `rust` means `Cargo.toml` exists; run the Rust pre-flight below.
- `uv-python` means `pyproject.toml` exists and the repo has uv evidence
  (`uv.lock` or `[tool.uv]`); run the uv pre-flight below.
- `none` means no recognized Rust/uv project. Skip Phase 0 entirely, proceed to
  Phase 1, and include a visible run note: `No recognized Rust/uv toolchain;
  review ran without CI preflight.`

Do not run Cargo without `Cargo.toml`. Do not run `uv lock` merely because a
repo has `pyproject.toml`; that can create lock state in a non-uv project. Do
not fall back to bare `python`, `pip`, `pytest`, `ruff`, or ad hoc tool
commands. A missing toolchain degrades to skipped pre-flight; a recognized
toolchain whose command fails is a real pre-flight failure.

**Default (auto-fix) — `--nofix-preflight` NOT passed.** Run the fixing
variants sequentially; they no-op when clean. Stop at the first failure.

```bash
# Rust
cargo fmt --all
cargo clippy --all-targets --all-features -- -D warnings
# uv-python
uv lock
uv run ruff check --fix
uv run ruff format .
uv run basedpyright
```

If the lint/type gate fails, report the errors and stop — type errors need
manual intervention.

**`--nofix-preflight` passed.** Run the check-only variants (`--check` / `--locked`
/ `uv lock --check`). Stop at the first failure and tell the user to fix manually.

**Working-tree side effects.** The fixing variants rewrite files in place
(`uv.lock`, `.py`/`.pyi`, unformatted Rust). The workflow's reviewers see the
post-fix tree. Pass `--nofix-preflight` or commit first if you want reviewers to
see only your own changes.

**If all checks pass**, proceed to Phase 1.

## Phase 1: Read review context

1. Read `REVIEW.md` from the repository root (if it exists) — you need it for Phase 4 synthesis.
2. Run a stat/log summary appropriate to the mode (for the Phase 4 header):
  - `base`: `git diff <base>...HEAD --stat` and `git log --oneline <base>..HEAD`
  - `uncommitted`: `git diff HEAD --stat` and `git diff --cached --stat`
  - `commit`: `git diff <sha>^..<sha> --stat` and `git log --oneline <sha>^..<sha>`
  - `range`: `git diff <a>..<b> --stat` and `git log --oneline <a>..<b>`

## Phase 1.5: Freeze into a worktree

Unless `--in-place` was passed, snapshot the review's start state into a git
worktree so the user can keep working (and you can `--fix`) without the two
colliding. `PROJ` is the injected **Live checkout**; `DIFF_HASH`/`CACHE_KEY`
come from Phase -1. Do every git op with `git -C "$PROJ"` — the Bash tool's cwd
is not guaranteed to be the repo.

```bash
PROJ="<injected live checkout>"
# Freeze at the REVIEW TARGET, not always HEAD: --commit / --range must snapshot
# the requested revision so reviewers/fixers inspect the RIGHT tree (base and
# uncommitted review from HEAD).
case "$MODE" in
  commit) START=$(git -C "$PROJ" rev-parse "$SHA") ;;
  range)  START=$(git -C "$PROJ" rev-parse "$RANGEB") ;;
  *)      START=$(git -C "$PROJ" rev-parse HEAD) ;;
esac
# All deep-review worktrees live under ONE distinctive prefix. This lets teardown
# (Phase 3.5) structurally refuse to remove anything outside it, and lets a scoped
# permission rule allow ONLY these removals (see .claude/settings.json).
# Strip TMPDIR's trailing slash before composing: macOS TMPDIR ends with "/",
# and a double-slash prefix breaks the Phase 3.5 string-prefix guard (git
# normalizes paths on creation; the case-pattern match does not).
WT_BASE="${TMPDIR:-/tmp}"; WT_PREFIX="${WT_BASE%/}/deep-review-wt"
WT="${WT_PREFIX}/${CACHE_KEY}-$(printf '%s' "$DIFF_HASH" | cut -c1-12)"
# Clean any prior worktree at this path WITHOUT `rm`/`git clean` — both are gated
# (rm is in the `ask` list; `git clean` is DENIED to agents in settings, since an
# unscoped clean is what once wiped the untracked skills). worktree remove +
# add --force need neither.
git -C "$PROJ" worktree remove --force "$WT" 2>/dev/null || true
git -C "$PROJ" worktree prune
git -C "$PROJ" worktree add --force --detach "$WT" "$START"

# uncommitted mode: carry the (post-preflight) dirty + untracked state into the
# frozen worktree — a worktree at HEAD does not include either. `cp -a` (not -p)
# so symlinks-to-directories are reproduced as links rather than erroring.
if [ "$MODE" = "uncommitted" ]; then
    # FAIL CLOSED: if the apply fails, the frozen tree is bare HEAD and the whole
    # panel would review the WRONG code while reporting a normal run. Abort loudly.
    if ! git -C "$PROJ" diff HEAD | git -C "$WT" apply --whitespace=nowarn; then
        echo "FATAL: could not reproduce uncommitted changes in the frozen worktree — aborting review" >&2
        git -C "$PROJ" worktree remove --force "$WT"; git -C "$PROJ" worktree prune
        exit 1
    fi
    # Feed the loop via process substitution, NOT a pipeline — a piped `while`
    # runs in a subshell, where `exit 1` kills only the subshell and the parent
    # continues fail-OPEN with a tree missing untracked files.
    while IFS= read -r -d '' f; do
        mkdir -p "$WT/$(dirname "$f")"
        if ! cp -a "$PROJ/$f" "$WT/$f" 2>/dev/null; then
            echo "FATAL: could not reproduce untracked file '$f' in the frozen worktree — aborting review" >&2
            git -C "$PROJ" worktree remove --force "$WT"; git -C "$PROJ" worktree prune
            exit 1
        fi
    done < <(git -C "$PROJ" ls-files --others --exclude-standard -z)
fi

# Copy live tooling into the frozen-code worktree so reviewers + the boss find
# their specs/mechanics. The plugin's skills live outside the reviewed repo (under
# $CLAUDE_PLUGIN_ROOT/skills) and are absent from a worktree at HEAD. Frozen CODE +
# live RULES/TOOLING is exactly what we want. Includes codex-cli (boss + codex
# reviewers read it) and codex-review. Source = this plugin's install dir;
# destination = .claude/skills/<name> in the worktree, where each reviewer spec
# expects to read it (see reviewers/*.md, which reference <REPO_ROOT>/.claude/skills).
for s in deep-review codex-cli codex-review; do
    src="${CLAUDE_PLUGIN_ROOT}/skills/$s"
    [ -e "$src" ] && { mkdir -p "$WT/.claude/skills"; cp -R "$src" "$WT/.claude/skills/"; }
done

# Record the frozen tree so Phase 3.5 can isolate the fixes (index-only, no
# commit — avoids pre-commit hooks and the --no-verify block). `write-tree`
# needs everything staged; `reset -q` afterwards unstages WITHOUT touching the
# working tree, so the reviewers still see uncommitted edits as unstaged and
# new files as untracked (`git ls-files --others`) — exactly the live state.
git -C "$WT" add -A
BASE_TREE=$(git -C "$WT" write-tree)
git -C "$WT" reset -q
printf '%s\n' "$BASE_TREE" > "$PROJ/.review-cache/${CACHE_KEY}.basetree"
echo "REVIEW_ROOT=$WT"   # this is repoRoot for Phase 2; START is the fork point for Phase 3.5
```

Under `--in-place`, skip all of the above and use `PROJ` as `repoRoot`.

## Phase 2: Launch the workflow

Build the `args` object from the parsed flags and the injected context, then call
`Workflow` with the committed script. Do **not** re-implement the orchestration
inline and do **not** invoke reviewers yourself — the workflow owns fan-out,
boss arbitration, fixing, and the loop.

**Fixing strategy (from the injected `Fix strategy`):**
- `workflow-subagents` (env unset) — the workflow fixes in the frozen worktree via
  `agentType: 'fixer'` subagents. Pass `fix` per the flags (default true).
- `teams` (`CLAUDE_CODE_EXPERIMENTAL_AGENT_TEAMS=1`) **and** fixing — teams are
  lead-only (a workflow can't spawn one), so pass **`fix: false`** (workflow
  reviews + boss-judges only) and do the fix as a main-agent team in **Phase 3.6**.
  The **review still runs in the frozen worktree** (repoRoot = REVIEW_ROOT) — only
  the *fix* moves to the live checkout. Do NOT run the review in-place for teams:
  the external reviewers' range-mode checkout/stash dances would execute in the
  live tree, racing the lead's own edits (this bit us once — an adversarial
  reviewer stashed the lead's uncommitted work mid-run). Under `--nofix`, strategy
  is irrelevant — always a single report pass.

```
Workflow({
  scriptPath: "${CLAUDE_PLUGIN_ROOT}/skills/deep-review/deep-review.workflow.js",  // plugin install dir — the workflow script ships alongside this skill
  args: {
    mode:      "base" | "uncommitted" | "commit" | "range",
    base:      "<base branch>",              // base mode
    sha:       "<sha>",                       // commit mode
    rangeA:    "<from>", rangeB: "<to>",      // range mode
    rangeBIsHead: <rangeB resolves to the injected HEAD — checkout dance is a
                   no-op, so the workflow runs range externals parallel, danceless>,
    focus:     "<--focus text or ''>",
    repoRoot:  "<REVIEW_ROOT — frozen worktree from Phase 1.5; PROJ only under --in-place>",
    toolchain: "python" | "rust" | "none",    // injected
    fix:       <true, UNLESS --nofix OR (Fix strategy == teams AND fixing) → false>,
    maxRounds: <--max-rounds or 3>,
    boss:      <--boss split on ',' → [str…]; omit/[] = inherit the session model>,
    tools: {
      codex:            <injected codex CLI == yes>,
      codexAdversarial: <codex yes AND companion vet started with "ok ">,
      cubic:            <injected cubic CLI == yes>,
      coderabbit:       <injected coderabbit CLI == yes>,
      grok:             <injected grok CLI == yes>,
      codexCompanion:   "<companion vet path: the token AFTER "ok " — NOT the literal "ok "/"absent"/"unloadable …"; "" if not ok>",
    },
  },
})
```

The workflow runs in the background and returns a structured result:

```
{ mode, rounds, fixed, boss: [str…], attempted: [key…], degraded: [{key, reason}…],
  confirmed: [{file, line_start, line_end, severity, sources: [key…], description, recommendation, domain}…] }
```

`confirmed` is already deduped (overlapping line ranges clustered, cross-tool
agreement in `sources`), **ruled by the boss** (real-vs-noise, final severity,
`domain`), and sorted severity-first. When the workflow fixed, `confirmed` is the
union of everything confirmed across all rounds. (Under the `teams` strategy the
workflow was report-only — fixing happens in Phase 3.6.)

## Phase 3: Present

Format the workflow's `confirmed` findings into the report below. The synthesis
work (dedup, verification, false-positive culling, REVIEW.md skip-list) is
already done in code — your job here is presentation and any final human-level
judgment on borderline findings, reading the cited lines if a `critical`
single-source finding looks surprising.

Map severities to sections: `critical` → Critical, `warning` → Warning, `nit` →
Nit. Render `sources` as the Sources column (multiple = high confidence).

## Phase 3.5: Reconcile the worktree

Skip entirely under `--in-place` (there is no worktree; fixes are already in the
live tree). Otherwise, the review froze at `START` while the user kept working —
so check whether the fork point moved, hand back the fixes, then tear down.

```bash
# 1. Did the base advance since we forked? (base mode; best-effort fetch if a remote exists)
git -C "$PROJ" fetch --quiet origin "<base>" 2>/dev/null || true
NEW_ON_BASE=$(git -C "$PROJ" rev-list --count "$START..<base>" 2>/dev/null || echo 0)
# Also surface user commits added to the live checkout since the freeze:
NEW_LOCAL=$(git -C "$PROJ" rev-list --count "$START..HEAD" 2>/dev/null || echo 0)

# 2. Isolate the fixes as a patch (diff of final worktree index vs the frozen tree).
#    Empty in report-only runs — nothing was written.
if [ "$FIX" = "yes" ]; then
    BASE_TREE=$(cat "$PROJ/.review-cache/${CACHE_KEY}.basetree")
    git -C "$WT" add -A
    PATCH="$PROJ/.review-cache/${CACHE_KEY}.fixes.patch"
    git -C "$WT" diff --cached "$BASE_TREE" > "$PATCH"
fi

# 3. Tear down — the patch is the durable artifact, so the worktree is disposable.
#    GUARD: structurally refuse to remove anything outside WT_PREFIX, so a bad
#    $WT can never nuke an unrelated worktree even if permissions are loose.
case "$WT" in
  "$WT_PREFIX"/*) git -C "$PROJ" worktree remove --force "$WT"; git -C "$PROJ" worktree prune ;;
  *) echo "refusing to remove worktree outside $WT_PREFIX: $WT" >&2 ;;
esac
```

Report in Phase 4:

- If `NEW_ON_BASE > 0` (or `NEW_LOCAL > 0`): the review is against a **frozen
  state that is now behind**. Say so and recommend rebasing the patch before
  applying — do NOT auto-rebase (it can conflict; that is the user's call).
- If `--fix` ran and the patch is non-empty: tell the user the fixes are isolated
  at `.review-cache/<key>.fixes.patch` and apply with
  `git apply .review-cache/<key>.fixes.patch` (rebase first if base moved). Their
  live checkout was never touched by the loop.

## Phase 3.6: Team fixing (teams strategy only)

Runs only when the injected **Fix strategy** is `teams` AND fixing (not `--nofix`).
The workflow returned boss-confirmed findings report-only under `--in-place`; now
fix them as a main-agent **team** — you are the lead.

1. Take `confirmed` findings with `severity != nit`, grouped by file.
2. Size the team deterministically per the agent-teams heuristic (3–5 workers,
   ~5 files each): `teamSize = clamp(ceil(fileCount / 5), 1, 5)`. Partition the
   files into `teamSize` **disjoint** buckets — no two teammates share a file.
3. Spawn `teamSize` teammates **using the `fixer` agent type** (the same subagent
   definition the workflow uses as `agentType`), one per bucket, each given its
   repo root (**PROJ — the live checkout**) and its file→findings. The fix is the
   ONLY part of the teams strategy that touches the live tree; the review stayed
   in the frozen worktree. Warn each fixer about any files that carry uncommitted
   changes so it locates code by content, not stale line numbers.
4. When the team finishes, **re-run the workflow report-only** (`fix:false`) —
   fresh freeze so the re-review sees the fixed tree — to confirm the fixes
   resolved the findings and introduced nothing new; loop up to `--max-rounds`
   while actionable findings remain.
5. No `.fixes.patch` here — edits are already in the live tree. Say plainly in
   Phase 4 what changed. (Phase 3.5 ran normally on the review worktree; its
   patch step is a no-op because the workflow was report-only.)

## Phase 4: Output

```markdown
## Deep Review: <branch> → <base>  (<rounds> round(s)<, fixes applied if fixed>)

**Commits:** <count> | **Files changed:** <count> | **Pre-flight:** passed
**Tools used:** <attempted minus degraded>
**Degraded:** <comma-separated `key (reason)` from result.degraded; omit line if none>

### Critical (fix before merge)
| # | File:Line | Finding | Sources |
|---|-----------|---------|---------|

### Warning (should fix)
| # | File:Line | Finding | Sources |
|---|-----------|---------|---------|

### Nit (minor)
| # | File:Line | Finding | Sources |
|---|-----------|---------|---------|
```

When `result.degraded` is non-empty, append:

> **Note:** `<n>` of `<attempted.length>` reviewer(s) were degraded. A degraded
> reviewer is absence of data, not a clean pass — re-run after quota resets /
> missing tools are installed for full cross-tool coverage.

If there are no critical or warning findings:

> No issues found. Checked with `<tools used>` across `<rounds>` round(s).

When `--fix` ran, state plainly what was changed (the tree now differs from when
the review started) and that the final round re-reviewed the fixed tree.

(The diff hash was already written at the *start* of the review in Phase -1 — see
there for why. Nothing to write here.)

## Important rules

- **Never skip Phase 0** unless `--skip-preflight` is passed. Pre-flight also
  establishes the clean-baseline invariant the fix loop depends on.
- **The workflow owns orchestration.** Don't launch reviewers, run the boss,
  dedup, or fix inline (except the Phase 3.6 team fix, which is lead-only by
  necessity) — that is the nondeterminism this design removed.
- **Three roles.** Runtime = Claude (the workflow, always). Workers = the panel.
  Boss = the arbiter (`--boss`, default inherits the session model) with final say.
- **Fixing never touches the live checkout under the default (`workflow-subagents`)
  strategy.** Fixes are made in the frozen worktree by `fixer` subagents — one per
  disjoint file bucket, deterministically sized, type-gated each round — and handed
  back as an opt-in `.fixes.patch`. The `teams` strategy fixes in-place via a
  main-agent `fixer` team (Phase 3.6). `--nofix` gives the cheap single report pass.
- **Never delete outside the worktree prefix.** Worktrees live under `WT_PREFIX`
  (`deep-review-wt`); teardown refuses anything else, `git clean` is denied to
  agents, and no `rm` is used.
- **Always read REVIEW.md** — the convention reviewer and the boss both consult it,
  and you need it for Phase 4 judgment.
- **Degraded ≠ clean.** Surface every degraded reviewer in the output.
