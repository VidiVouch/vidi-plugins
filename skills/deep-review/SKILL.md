---
name: deep-review
description: Orchestrated multi-tool code review — a deterministic workflow (review → boss-arbitrated judgment → fix → reconverge)
argument-hint: "[ --base <branch> ] [ --uncommitted ] [ --commit <sha> ] [ --range <a>..<b> ] [ --nofix ] [ --max-rounds <n> ] [ --boss <model> ] [ --panel ] [ --reduced-panel <text> ] [ --in-place ] [ --nofix-preflight ] [ --skip-preflight ] [ --focus <text> ] [ --reviewer-config <path> ] [ --fix-strategy workflow|teams ] [ --force ]"
allowed-tools: Bash(python3 *), Bash(mktemp *), Bash(uv run *), Bash(uv lock *), Bash(cargo *), Bash(git diff *), Bash(git log *), Bash(git ls-files *), Bash(git checkout *), Bash(git symbolic-ref *), Bash(git rev-parse *), Bash(git rev-list *), Bash(git hash-object *), Bash(git worktree *), Bash(git fetch *), Bash(git apply *), Bash(git add *), Bash(git write-tree *), Bash(git reset *), Bash(shasum *), Bash(mkdir *), Bash(echo *), Bash(ls *), Bash(cat *), Bash(cp *), Bash(find *), Bash(grep *), Bash(sed *), Bash(paste *), Bash(readlink *), Bash(dirname *), Bash(printf *), Bash(sort *), Bash(tail *), Bash(cut *), Bash(tr *), Bash(command *), Bash(test *), Bash(codex *), Bash(cubic *), Bash(coderabbit *), Bash(grok *), Bash(node *), Workflow, Agent, Read, Glob, Grep, TaskCreate, TaskUpdate, TaskList
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
- Cached reviews: !`R=$(find .review-cache -maxdepth 1 -name '*.hash' 2>/dev/null | sed 's#.*/##;s/\.hash$//' | paste -sd' ' -); echo "${R:-(none)}"`
- Team support: !`[ "${CLAUDE_CODE_EXPERIMENTAL_AGENT_TEAMS:-0}" = "1" ] && echo "available" || echo "unavailable"`

**Grounding — two distinct roots.** `CLAUDE_PROJECT_DIR` is *not* set in this
shell (it is injected only for hook commands), so the stable ground is the
**Live checkout** above, captured here at load in the live repo. Once Phase 1.5
freezes a worktree, the code under review lives at a *second* path:

- `projectDir` = the **Live checkout** — where the skill, reviewer specs, and
  `.agents` tooling actually live (several are untracked, so they exist *only*
  here). The user keeps working here in parallel.
- `repoRoot` = the **frozen worktree** — a snapshot of the review's start state
  that reviewers read and `--fix` edits, so nothing touches the live checkout.

Reviewer selection is policy-first, not executable-first. See Phase -2 and
[reviewer configuration](references/reviewers.md). The shipped named agents run
Astra standard, Astra adversarial, and Grok directly through a configured proxy;
they do not invoke Codex/Grok CLIs or Sonnet wrappers. These are discovery roles,
not the separate boss/judging stage. Native Codex, the legacy companion, Cubic,
and CodeRabbit remain explicit opt-ins. No subscriptions are assumed.

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
- `--boss <model>` - the **arbiter** with final say over the panel (dedup →
  real-vs-noise → severity → verdict). Default: **inherit the session model** (the
  runtime is Claude, always). `--boss codex` = gpt-5.x via `codex exec`, right for
  **logic**-shaped arbitration (correctness/repro; high intelligence, near-free).
  `--boss fable` / `--boss opus` for **taste**-shaped work (design/API/copy), where
  gpt-5.x is weak; a wrong-tiered boss is worse than none. Codex-family ids
  (`codex`,`gpt-5.x`,`sol`,`terra`,`luna`) route to `codex exec -s read-only`;
  Claude-family (`opus`/`fable`/`sonnet`) run in-process. One model only: the
  single-boss form is unchanged, and the earlier two-model form
  (`logicBoss,tasteBoss`, a domain-authoritative merge) is retired. It asked each
  boss to label a finding's domain, took one boss's voice by that label and
  ignored a third entry, so a list of bosses was neither one arbiter nor a panel
  and was never a consensus. The workflow and `run_manifest.py` refuse more than
  one model before any agent starts; several judges over the same candidates is
  `--panel`.
- `--panel` - replace the boss with the three-judge panel: `judge-fable`,
  `judge-astra` and `judge-grok`, one per model family, each selected by its
  definition with its full model ID pinned there. Every round the three judges
  receive one identical brief (the frozen snapshot, the candidates with
  content-derived ids, the REVIEW.md exclusions and the rubric) and rule
  independently; no judge sees another's result. See
  [judge contract](references/judge-contract.md) and
  [judging result](references/judging.md). Exclusive with `--boss`. Off by
  default because it costs three judgments per round; use it when the decision
  is worth a cross-family comparison, not for every small diff. All three judges
  must be advertised by the proxy (Phase -2) or the workflow refuses to start.
  The three results are merged by evidence, not by count: confirmations with
  no refutation confirm at the highest severity given, refutations with no
  confirmation refute, a confirmation beside a refutation is a conflict that
  leaves the candidate unruled and the run incomplete, and unresolved or
  unreviewed entries are recorded but never counted as a verdict
  (judge-contract.md, "Merging and rebuttals"). In both modes a fixer that
  finds a confirmed finding false rebuts it in its packet instead of fixing
  it, and the next round judges the objection; a fixer that finds the cause in
  a file it does not own escalates it in its packet, and one fresh fixer owning
  that file repairs it (disposition.md, "Escalations"). Every judge is required: a
  judge that returns nothing usable is logged as an incomplete panel and
  replaced once by a fresh instance of the same definition on the same brief;
  a judge still missing after that leaves the round unruled and the run
  incomplete, and the panel never falls back to two of three on its own
  (judge-contract.md, "When a judge slot fails").
- `--reduced-panel "<text>"` - with `--panel` only: the user's authorization
  for the two remaining judges to rule when one judge is still missing after
  its replacement. The text is the user's own words allowing a two-judge
  ruling and where they were given, passed through unchanged as
  `reducedPanel`; pass it only when the user's message carries it, never on
  your own judgment and never on a re-run the user did not ask for with it
  (references/approvals.md). The missing judge is then `reduced coverage:`
  under `degraded`, every candidate's `dispositions` says `missing` for it,
  and `run_gate.py finalize` accepts the run only against the manifest that
  registered the same text. It does not let a panel start with a judge the
  proxy does not advertise, and two missing judges are never a panel.
- `--nofix-preflight` — opt out of auto-fixing pre-flight failures (default is to fix)
- `--skip-preflight` — skip Phase 0 CI checks (use when you know they pass)
- `--reviewer-config <path>` - JSON policy overrides; otherwise use `.claude/deep-review.json` in the live checkout if present. See references/reviewers.md.
- `--fix-strategy workflow|teams` - default workflow: fresh fixers in the frozen worktree. Teams is an explicit opt-in to live-tree fixing and requires team support. An enabled teams environment variable does not select this strategy.
- `--focus <text>` - pass focus text to the reviewers
- `--force` - ignore prior-attempt hints; never bypass snapshot or gate verification.
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

## Phase -2: Resolve reviewer policy and capabilities

Run the packaged helper from the live checkout context, passing its absolute
path. Add `--config "<path>"` only when `--reviewer-config` was supplied, and
`--panel` only when `--panel` was passed:

```bash
python3 "${CLAUDE_PLUGIN_ROOT}/skills/deep-review/scripts/prepare_reviewers.py" \
  --repo "<injected live checkout>" --probe-proxy --panel
```

Keep the returned object as `REVIEWER_SETUP`; abort on a nonzero exit. The helper
reads policy first, never probes disabled reviewers, and never runs inference.
With `--panel` it also returns `judges`, the catalog evidence for the three
judge definitions; without it `judges` is null. If any judge is unavailable,
report its reason and stop, because the workflow refuses a panel missing a judge
rather than running with fewer model families than it claims; `--reduced-panel`
does not change this, it covers a judge that was available and failed in the run.
Its bounded model-catalog GET uses the session's configured ANTHROPIC_BASE_URL
and credentials without printing them. Catalog evidence is not proof of tool,
schema, entitlement, or actual upstream support. Enabled CLI lanes have unknown
authentication/subscription/quota until execution; do not call them usable merely
because their executable exists. Do not invent billing probes or renew accounts.

Do not fork or resume a reviewer instance. Named agents must be discoverable from
the installed plugin; missing definitions are failures, not alias fallbacks. A
named definition is selected by `agentType` alone, with no per-call `model`:
that parameter takes only a family alias and resolves ahead of the definition,
so it would replace the full ID the definition pins (the workflow stops a unit
that sets both). No reviewer or workflow fixer
gets a runtime instance name, because with agent teams enabled a named Agent
call launches a teammate. See [work-unit](references/work-unit.md), "Ordinary
workers and temporary teammates". If `REVIEWER_SETUP.namedAgents.blocked` is
true, `CLAUDE_CODE_SUBAGENT_MODEL_FORCE` is on and every definition's model
would be replaced by the session model; report that and stop, because the
workflow refuses to start with it ([judge-contract](references/judge-contract.md),
"How a judge is selected"). Fork mode (`CLAUDE_CODE_FORK_SUBAGENT`, on by
default) is not a reason to stop: a dispatch by `agentType` is not a fork and
starts fresh from its definition.

## Phase -1: Diff identity and prior-attempt hints

For every mode, including the default `--base main`, compute the diff hash below.
Set `REVIEWER_FINGERPRINT` to `REVIEWER_SETUP.reviewerFingerprint` so changing
policy, model definitions, or availability invalidates an old attempt marker:

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
DIFF_HASH=$(printf '%s\n%s' "$DIFF_PAYLOAD" "$REVIEWER_FINGERPRINT" | shasum -a 256 | cut -d' ' -f1)
CACHE_KEY=$(echo "<mode identifier>" | tr '/' '_' | tr '.' '_')
CACHE_FILE=".review-cache/${CACHE_KEY}.hash"
```

Cache key examples: `main`, `uncommitted`, `commit_abc123`, `range_abc_def`.

An existing .hash file records only a prior attempt. It never proves acceptance
and never short-circuits a requested review. Prior patches are evidence to inspect,
not proof that they still apply. --force remains compatible and ignores prior-run
hints; it cannot bypass manifest or gate verification.

After preflight, recompute uncommitted identity if auto-fixes changed the tree.
The run manifest, created after freezing, is the authoritative snapshot identity.
Leave existing attempt files intact. Accepted-result reuse happens in Phase 2
through `reuse_result.py`, which matches the verified manifest's snapshot and
rubric against prior runs' `acceptance.json`; a diff hash match never replays a
failed or unfinished attempt. See [result reuse](references/result-reuse.md).

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
mkdir -p "$WT_PREFIX"
WT=$(mktemp -d "${WT_PREFIX}/${CACHE_KEY}-XXXXXX")
# Allocate a new owned path; never delete an earlier run to make room.
git -C "$PROJ" worktree add --detach "$WT" "$START" || exit 1

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
printf 'BASE_TREE=%s\n' "$BASE_TREE"
echo "REVIEW_ROOT=$WT"   # this is repoRoot for Phase 2; START is the fork point for Phase 3.5
```

Under `--in-place`, skip all of the above and use `PROJ` as `repoRoot`.

## Phase 2: Launch the workflow

Read [run lifecycle](references/run-lifecycle.md). Build the complete request
arguments below, register the project's actual gate commands, and save the request
outside the review root. Run run_manifest.py init and verify, then pass the entire
generated args.json object to Workflow. Missing or drifted manifests block dispatch.
The workflow owns fan-out, adjudication and bounded fixing; deterministic host-side
helpers verify files and commands. Do not replace those helpers with model claims.

**Fixing strategy (explicit flag, default workflow):**
- `workflow`: the workflow fixes in the frozen worktree through fresh fixer
  subagents. Pass fix per the flags (default true).
- `teams`, explicitly requested and available, with fixing: pass fix:false to the
  workflow and perform main-agent team fixing in Phase 3.6. Reject unavailable
  team mode rather than silently selecting another strategy.
  The **review still runs in the frozen worktree** (repoRoot = REVIEW_ROOT) — only
  the *fix* moves to the live checkout. Do NOT run the review in-place for teams:
  the external reviewers' range-mode checkout/stash dances would execute in the
  live tree, racing the lead's own edits (this bit us once — an adversarial
  reviewer stashed the lead's uncommitted work mid-run). Under `--nofix`, strategy
  is irrelevant — always a single report pass.

Three separate facts, never inferred from one another: the injected Team support
line is availability only and gates whether `teams` may be requested at all;
`--fix-strategy` picks who fixes, workflow fixers unless the flag says otherwise;
the edit destination is the frozen worktree unless live editing is explicitly
selected, through `--fix-strategy teams` (fix only) or `--in-place` (review and
fix). A session with teams enabled therefore behaves exactly like one without
until the flag is passed, because a persistent fixer editing the live checkout is
a decision the user makes, not one the environment makes.

```
REQUEST_ARGS = {
    mode:      "base" | "uncommitted" | "commit" | "range",
    base:      "<base branch>",              // base mode
    sha:       "<sha>",                       // commit mode
    rangeA:    "<from>", rangeB: "<to>",      // range mode
    rangeBIsHead: <rangeB resolves to the injected HEAD — checkout dance is a
                   no-op, so the workflow runs range externals parallel, danceless>,
    focus:     "<--focus text or ''>",
    repoRoot:  "<REVIEW_ROOT — frozen worktree from Phase 1.5; PROJ only under --in-place>",
    toolchain: "python" | "rust" | "none",    // injected
    fix:       <true unless --nofix or explicit --fix-strategy teams>,
    maxRounds: <--max-rounds or 3>,
    boss:      <[--boss model] or []; one entry at most, more is refused; [] = inherit the session model; must be empty under --panel>,
    judges: REVIEWER_SETUP.judges,           // null without --panel; with it, the three judges' evidence, unchanged
    reducedPanel: <the --reduced-panel text, or null; refused without judges or when empty>,
    frozenBaseTree: "<BASE_TREE printed by freeze; omit for --in-place>",
    specDir: "${CLAUDE_PLUGIN_ROOT}/skills/deep-review/reviewers",
    reviewers: REVIEWER_SETUP.reviewers,     // policy + capability evidence, unchanged
    tools: REVIEWER_SETUP.tools,             // only enabled legacy adapters need these
    namedAgents: REVIEWER_SETUP.namedAgents, // {blocked, reason}: the workflow refuses to start while blocked
    limits: { concurrency: 3, maxAgents: 30, minRemainingTokens: 0 },
    gate: { commands: <registered argv arrays>, timeoutSeconds: <bounded timeout>,
            registry: "<optional: the ledger unit's processes-<role>-<N>.json>" },
}
```

Initialize/verify the manifest using the run-lifecycle reference. Save its argsFile
path as RUN_ARGS_FILE and load that entire file as RUN_ARGS. Preserve the original
frozenBaseTree when creating a post-fix verification request.

Unless `--force` was passed, ask the artifact directory for a prior acceptance of
this exact input and rubric before dispatching:

```bash
python3 "${CLAUDE_PLUGIN_ROOT}/skills/deep-review/scripts/reuse_result.py" find \
  --manifest "$MANIFEST"
```

Exit 0 lists reusable runs: present the first one's `acceptance.json` and result
in Phase 3 as the accepted review of this snapshot, skip the Workflow, and tear
down the worktree as for a completed run. Exit 1 means no prior run qualifies
(it started, degraded, was ruled changes-required, had its evidence altered, or
reviewed another input or rubric); the reasons are in `refused`. Only
`acceptance.json` is reusable evidence. A Workflow resume replays cached agent
return values, which are claims, not acceptance; see
[result reuse](references/result-reuse.md). Then invoke:

```
Workflow({
  scriptPath: "${CLAUDE_PLUGIN_ROOT}/skills/deep-review/deep-review.workflow.js",
  args: RUN_ARGS, // full generated args.json, including run identity
})
```

The workflow runs in the background and returns a structured result:

```
{ mode, rounds, fixed, boss, judges, reducedPanel, attempted, successful,
  skipped: [{key, policy, reason}], degraded: [{key, reason}],
  coverage: {reviewerKey: {policy, state, reason, agentType, requestedModel,
                         catalogModel, resolvedModel, upstreamModel}},
  attempts: [{key, round, state, reason}], incomplete, coverageComplete,
  run, reviewResults, fixResults, judgeResults, admission, requiresPostFixReview, acceptance,
  termination: {state, round, reason},
  gate: {state, reason, report, executor, courier},
  currentFindings: [...], confirmed: [...],
  disposition: {open: [...], fixed: [...], refuted: [...], unruled: [...], degraded: [...]} }
```

Before teardown, save the full result at the manifest's `outputs.resultPath` and
REVIEWER_SETUP beside it in the run directory outside the review root. Do not
overwrite prior attempt artifacts or truncate structured payloads for display. Use
absolute paths in chat links. If persistence fails, retain the worktree and partial
fixes.

`termination` says why the loop ended, never inferred from counts. `state` is
one of `converged` (a round found nothing actionable), `report-only` (fixing
off, findings reported), `repeated-failure` (a finding re-detected after two
fix attempts is not sent to a third fixer; it stays open with its attempts on
record), `round-cap` (the cap was reached right after a fix stage, so that fix
is unreviewed), `fixer-failed`, `judge-failed`, `review-failed`, `stopped`
(admission stopped: a cancellation, a limit or a required lane lost; `reason`
is `admission.stopReason`) or `no-reviewers`. The round cap is one bound on the
fix loop and the attempt bound is the other; neither is a reason to start more
rounds by hand.

Under `--panel`, `boss` is `[]`, `judges` names the three definitions with their
requested models, and `judgeResults` holds per round the `candidates` every judge
was given and each judge's checked `result` or the `error` that made it no
result, plus under `replaced` the first instance of any judge a fresh one took
over from. A judge still missing after its replacement is `degraded` under
`judge:<name>`: `degraded:` means the round was unruled and the run is
incomplete, `reduced coverage:` means the other two judges ruled under the
`--reduced-panel` authorization the request carried; report it as absent
data in either case, never as a judge that ruled. The workflow checked shape,
snapshot, ids, anchors and what each disposition must carry; it cannot read
files. Before teardown, write each round's `candidates` and each non-null
`result` (the replaced first instances too) to files in the run directory and
run, per judge and round, with the worktree still present:

```bash
python3 "${CLAUDE_PLUGIN_ROOT}/skills/deep-review/scripts/judging.py" check \
  --result "$RUN_DIR/judge-<name>-round-<n>.json" \
  --candidates "$RUN_DIR/candidates-round-<n>.json" \
  --snapshot "<run.snapshotId>" --repo "$REVIEW_ROOT"
```

This confirms every code reference names a file and line range that exist in
the judged tree. A result that fails is not evidence: report that judge as
degraded, do not accept the run, and start a fresh attempt. To resume an interrupted Workflow, run `run_manifest.py resume --manifest
"$MANIFEST"` and pass its `args` with `resumeFromRunId`; it refuses a changed tree
or missing arguments, which is what would otherwise yield an empty run. For a run
that returned but did not complete, `run_attempt.py classify --manifest "$MANIFEST"`
says whether the failure was transient, blocked, stale or partial, and
`run_attempt.py retry` starts the next bounded attempt under a new run id
(references/result-meaning.md). `run_status.py status --manifest "$MANIFEST"
--write` keeps execution, coverage, acceptance and recovery apart in
`status.json` and names the blocking stage; `--format note` feeds
`skills/html-report/scripts/render_note.py`, so report the run through that
rather than a hand-written summary.

Follow run-lifecycle.md before acceptance: incomplete stops progression; a run
that changed code needs one fresh fix:false review with a new manifest for the
resulting snapshot. The workflow's Conclude phase has already run `run_gate.py run`
through its gate-courier unit on the unchanged snapshot and recorded the report as
`result.gate` (a failed or missing gate also sets `incomplete`); run run_gate.py
finalize only, never `run` again. Only its acceptance.json accepted=true permits
automated acceptance, and it refuses a result whose recorded report differs from
the gate file. A gate failure, changes-required, or missing gate pauses for
recovery/repair under the existing cap, not an unbounded outer loop. No
model-generated test report substitutes for the measured gate.

`confirmed` is already deduped (overlapping line ranges clustered, cross-tool
agreement in `sources`), **ruled by the boss** (real-vs-noise, final severity),
and sorted severity-first. When the workflow fixed, `confirmed` is the
union of everything confirmed across all rounds, including findings fixed since;
`disposition` says where each one stands now (references/disposition.md). (Under the `teams` strategy the
workflow was report-only — fixing happens in Phase 3.6.)

`incomplete` blocks acceptance and further fixing/commit progression: a required
reviewer or gate failed, no reviewer succeeded, or final fixes remain unreviewed.
`coverageComplete` is separate: optional missing/failed lanes mean reduced
coverage even when required lanes completed. Disabled lanes are explicitly skipped,
never successful evidence. Preserve `attempts` so later success does not erase
an earlier failure. Model catalog IDs are not upstream verification; unresolved
identity fields stay null rather than being filled from model self-reports.

After saving the result, record who actually answered each unit:

```bash
python3 "${CLAUDE_PLUGIN_ROOT}/skills/deep-review/scripts/model_evidence.py" \
  --result "$RUN_DIR/result.json" \
  --workflow-dir "<the Workflow run's transcript directory, from the Workflow tool result>" \
  --out "$RUN_DIR/model-evidence.json"
```

Add `--proxy-logs ~/.ccs/cliproxy/logs` when CLIProxy request logging is on.
The helper reads the runtime journal and each unit's transcript metadata, never
a unit's text, and reports per unit `requestedModel`, `resolvedModel`,
`upstreamModel` and a state. `trial.state` is `valid` only when every unit was
answered by the model it asked for. Present a `fallback` unit as a substituted
model, and never present an `invalid` or `unverified` run as a cross-family
comparison (references/models.md, "Run-record identity: three sources").

## Phase 3: Present

Report from `result.disposition` (references/disposition.md): every judged finding
sits in exactly one of `open`, `fixed`, `refuted`, `unruled` or `degraded`, with
`outcomeReason` and its per-round `rounds`. `open` is the work left; `fixed` was
confirmed, fixed and not re-detected by the lanes that reported it; `refuted` is a
claim the boss (or, under `--panel`, every judge) ruled noise, kept with its
reason; `unruled` had no boss ruling, or under `--panel` conflicting or no
evidence from the judges (`dispositions` says what each judge said); `degraded`
had a fix, or a fixer's rebuttal, that nothing re-reviewed, so its state is
unknown. A finding judged with a fixer's objection carries `rebuttal`, and its
reason says whether the judges upheld it or confirmed over it. `confirmed`
is history (everything any round confirmed) and `currentFindings` is the last
round's view; never present either as the list of what is still open or as what
was resolved. Include acceptance.json or explain why verification is
pending/blocked. Deduplication and adjudication already ran; do not repeat them
during presentation or count agreement alone as proof.

Map severities to sections: `critical` → Critical, `warning` → Warning, `nit` →
Nit. Render `sources` as the Sources column (multiple = high confidence).

## Phase 3.5: Reconcile the worktree

Capture partial fixes and results on failure, but retain the worktree. Teardown
requires verified acceptance, a completed read-only report with recoverable input
and no unpreserved edits, or an explicit discard decision. Incomplete work never
authorizes automatic cleanup; changes-required is not acceptance.

Skip entirely under `--in-place` (there is no worktree; fixes are already in the
live tree). Otherwise, the review froze at `START` while the user kept working —
so check whether the fork point moved, hand back the fixes, then tear down.

```bash
# 1. Did the base advance since we forked? (base mode; best-effort fetch if a remote exists)
git -C "$PROJ" fetch --quiet origin "<base>" 2>/dev/null || true
NEW_ON_BASE=$(git -C "$PROJ" rev-list --count "$START..<base>" 2>/dev/null || echo 0)
# Also surface user commits added to the live checkout since the freeze:
NEW_LOCAL=$(git -C "$PROJ" rev-list --count "$START..HEAD" 2>/dev/null || echo 0)

# 2. The run directory holds the result (saved at outputs.resultPath in Phase 2) before
#    anything else happens here; a worktree whose result was never saved is retained.
#    A result that says fixed=true also gets its patch there, the diff of the final
#    worktree index against the frozen tree, because the patch is the only durable copy
#    of the fixers' edits once the worktree is gone. `fixed` is read from the saved
#    result, not from the --fix flag: the flag says what was requested, the result says
#    whether a fixer ran (a report-only pass under the teams strategy writes no patch).
RUN_DIR="$(dirname "$RUN_ARGS_FILE")"
test -s "$RUN_DIR/result.json" || { echo "result.json not saved; retaining $WT" >&2; exit 1; }
FIXED=$(python3 -c 'import json,sys; print(json.load(open(sys.argv[1]))["fixed"])' "$RUN_DIR/result.json")
if [ "$FIXED" = "True" ]; then
    BASE_TREE=$(python3 -c 'import json,sys; print(json.load(open(sys.argv[1]))["frozenBaseTree"])' "$RUN_ARGS_FILE")
    git -C "$WT" add -A
    PATCH="$RUN_DIR/fixes.patch"   # the manifest's outputs.patchPath
    # Exclusive output: an earlier captured patch is never overwritten. A second pass that
    # finds one stops here and retains the worktree for a reader to reconcile the two.
    (set -C; git -C "$WT" diff --cached --binary --full-index "$BASE_TREE" > "$PATCH") || exit 1
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
- If --fix ran and the patch is nonempty, link the absolute PATCH path in the
  durable run directory. Recommend git apply --check before applying it to the
  live checkout, and rebase first if the base moved. Do not apply it automatically.

## Phase 3.6: Team fixing (teams strategy only)

Runs only with explicit --fix-strategy teams and fixing (not --nofix). The
workflow returned findings from the frozen worktree. The lead owns the team phase;
team availability alone never opts into live-tree edits. This is the only place
deep-review holds a live discussion: everywhere else disagreement travels as
evidence in packets (rebuttals, escalations, judge results), because a Workflow
unit has no address and no message tool. See [work-unit](references/work-unit.md),
"How units disagree". Keep the phase on record with
`${CLAUDE_PLUGIN_ROOT}/skills/deep-review/scripts/team_phase.py` and
`TEAM_RECORD="$RUN_DIR/team-phases.json"`: it never spawns, messages or stops
anything, but it refuses a close while a teammate is standing, so the next
review cannot start over a tree a teammate may still edit.

1. If `result.incomplete`, stop and surface the missing reviewer/gate; do not fix or claim acceptance. Otherwise take `confirmed` findings with `severity != nit`, grouped by file.
2. Size the team deterministically per the agent-teams heuristic (3–5 workers,
   ~5 files each): `teamSize = clamp(ceil(fileCount / 5), 1, 5)`. Partition the
   files into `teamSize` **disjoint** buckets — no two teammates share a file.
3. Spawn `teamSize` teammates **using the `fixer` agent type** (the same subagent
   definition the workflow uses as `agentType`), one per bucket, each given its
   repo root (**PROJ — the live checkout**) and its file→findings. The fix is the
   ONLY part of the teams strategy that touches the live tree; the review stayed
   in the frozen worktree. Warn each fixer about any files that carry uncommitted
   changes so it locates code by content, not stale line numbers. These are the
   only temporary teammates deep-review creates: pass `subagent_type` fixer plus
   a runtime `name` unique to this phase, such as `fixer-r<round>-b<bucket>`;
   with teams enabled the name is what makes the call a teammate. First open
   the phase: `team_phase.py open --record "$TEAM_RECORD" --round <round>`,
   refused while an earlier phase is still open. Before each Agent call run
   `team_phase.py spawn --record "$TEAM_RECORD" --name <name> --files <bucket>`;
   it refuses a name without this round's `-r<round>-`, a name any earlier
   phase used, and a file a standing teammate owns. Discussion is `SendMessage`
   between you and one teammate: answer its question or redirect it, and never
   ask teammates to message each other; their buckets are disjoint, and a cause
   in another file is an escalation in the packet. Each teammate returns the fix
   packet in `fix-output.schema.json`, the same typed result the workflow's
   fixer lane returns; read that packet, not the transcript, and treat a packet
   naming files outside its bucket as a failed bucket. Shut every teammate down
   by name once its bucket returns, then run `team_phase.py shutdown --record
   "$TEAM_RECORD" --name <name>`. A packet's `escalations` route a finding to
   the owner of the one file each names: after every teammate of the stage is
   shut down, spawn one fresh teammate owning only that file (never the files
   the evidence cites) for the escalated findings, once per finding per run,
   recorded and shut down the same way. A later round spawns new teammates
   under new names; never message one from an earlier round or run. Close the
   phase before step 4 with `team_phase.py close --record "$TEAM_RECORD"`; it
   prints the teammates still standing and refuses until each has a recorded
   shutdown.
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
**Reviewers completed:** <result.successful>
**Skipped:** <result.skipped, each with policy and reason>
**Degraded:** <result.degraded, including gate failures>
**Incomplete:** <result.incomplete; true blocks acceptance>
**Ended:** <result.termination.state> in round <result.termination.round>: <result.termination.reason>
**Full enabled coverage:** <result.coverageComplete>
**Findings:** <n> open | <n> fixed | <n> refuted | <n> unruled | <n> degraded

### Open: Critical (fix before merge)
| # | File:Line | Finding | Sources | Rounds |
|---|-----------|---------|---------|--------|

### Open: Warning (should fix)
| # | File:Line | Finding | Sources | Rounds |
|---|-----------|---------|---------|--------|

### Open: Nit (minor)
| # | File:Line | Finding | Sources | Rounds |
|---|-----------|---------|---------|--------|

### Fixed in this run (not re-detected after the fix)
| # | File:Line | Finding | Sources | Why |
|---|-----------|---------|---------|-----|

### Refuted (boss ruled not real)
| # | File:Line | Claim | Sources | Why |
|---|-----------|-------|---------|-----|

### Unruled (no boss ruling; neither confirmed nor cleared)
| # | File:Line | Finding | Sources | Why |
|---|-----------|---------|---------|-----|

### Degraded (fix applied but not verified)
| # | File:Line | Finding | Sources | Why |
|---|-----------|---------|---------|-----|
```

The three Open sections come from `result.disposition.open` split by severity;
the other four sections are the remaining lists, each row's Why being its
`outcomeReason`. Omit an empty section with a one-line "none" instead of leaving
it out, so a reader can tell "nothing was fixed" from "fixed items were not
reported". Never fill any section from `confirmed`.

List skipped and degraded lanes separately, with reasons. Gate failures are not
reviewers, so do not divide the whole degraded array by attempted.length. An
expired subscription is missing coverage, never a clean review.

With no open critical/warning findings, say "No open issues found by <successful
lanes>" only when at least one reviewer succeeded, and still show what was fixed,
refuted, unruled or degraded. If incomplete, explicitly state that acceptance is
blocked. If coverageComplete=false, identify the missing enabled lanes rather
than claiming full coverage. If none succeeded, say "No review data."

When fixes ran, state what changed and whether the final tree was re-reviewed.
A round-cap or failed fixer can leave unverified edits; never claim verification
when result.incomplete or a final-verification degradation says otherwise. A
`repeated-failure` ending means two fixers did not make a finding go away; it
is open, with the rounds on record, and the next step is a reader, not a third
round started by hand.

Retain the complete run and acceptance artifacts. A historical diff hash is not
an acceptance marker.

## Important rules

- **Never skip Phase 0** unless `--skip-preflight` is passed. Pre-flight also
  establishes the clean-baseline invariant the fix loop depends on.
- **The workflow owns orchestration.** Don't launch reviewers, run the boss,
  dedup, or fix inline (except the Phase 3.6 team fix, which is lead-only by
  necessity) — that is the nondeterminism this design removed.
- **Three roles.** Runtime = Claude (the workflow, always). Workers = the panel.
  Boss = the arbiter (`--boss`, default inherits the session model) with final say,
  or with `--panel` three judges on one identical brief, ruling independently.
- **The lead stays thin.** This skill loads into your own context: it is a front
  end, not isolation. Investigation goes to the workflow's workers or the
  deterministic helpers, every child returns a typed packet (`reviewResults`,
  `fixResults`, rulings), and no standing coordinator sits between you and them.
  See [work-unit](references/work-unit.md), "The lead stays thin".
- Units do not talk to each other. Judges, reviewers and workflow fixers
  disagree through evidence in their packets, read by fresh units in the next
  round; the Workflow runtime offers them nothing else. Live discussion exists
  only in Phase 3.6, between you and one teammate, inside a phase
  `team_phase.py` records and refuses to close while a teammate is standing.
- Default workflow fixing stays in the frozen worktree, with one fresh fixer per
  disjoint file bucket that returns one packet and terminates. A fixer never
  edits outside its bucket: a cause in another file is escalated in the packet
  and repaired by a fresh fixer owning that file. Final acceptance needs a fresh read-only snapshot review
  and measured external gates. Preserve fixes as a binary-capable patch in the run
  directory. Only explicit teams mode opts into live-tree edits. --nofix reports
  findings without making changes.
- **Never delete outside the worktree prefix.** Worktrees live under `WT_PREFIX`
  (`deep-review-wt`); teardown refuses anything else, `git clean` is denied to
  agents, and no `rm` is used.
- **Always read REVIEW.md** — the convention reviewer and the boss both consult it,
  and you need it for Phase 4 judgment.
- **Degraded ≠ clean.** Surface every degraded reviewer in the output.
- Approval gates. Enabling a reviewer or external service, choosing teams
  live-tree fixing, editing permission rules or user settings, running paid
  reviewer calls the user did not request, and any commit or push each need
  their own authorization. Follow [approval gates](references/approvals.md); a
  completed workflow or a clean gate never grants one.
