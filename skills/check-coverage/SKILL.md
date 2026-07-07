---
name: check-coverage
description: Two-phase diff-scoped coverage check — "did the tests I wrote cover the code I wrote?"
argument-hint: "[--base <branch>] [--uncommitted] [--commit <sha>] [--range <a>..<b>]"
allowed-tools: Bash(uv run *), Bash(git diff *), Bash(git ls-files *), Bash(git checkout *), Bash(git symbolic-ref *), Bash(git rev-parse *), Bash(mkdir *), Bash(grep *), Bash(echo *), Bash(printf *), Bash(sort *), Bash(cat *), Bash(rm *), Read
---

# Check Coverage

Close-the-loop coverage check for code added in this branch. Runs pytest
with coverage twice — once with the full suite, once with *only* the
test files modified in the diff — and reports diff-scoped missing lines
for each phase.

## Why two phases                                                                              |

Phase 1 alone misses the "wrote tests, but they test the wrong thing" failure. Phase 2 alone misses the "code is already covered by existing tests" case. You need both.

| Phase 1 | Phase 2 | Diagnosis                                                                                                       |
|---------|---------|-----------------------------------------------------------------------------------------------------------------|
| High    | High    | New tests cover the new code. Ship it.                                                                          |
| High    | **Low** | **Existing tests happen to exercise the new code; Claude's new tests don't. Probably testing the wrong thing.** |
| Low     | Low     | Real coverage gaps. Write more tests.                                                                           |
| Low     | High    | Impossible (strict subset).                                                                                     |

## Arguments

Parse `$ARGUMENTS` for:

- `--base <branch>` — base branch to compare against (default: `main`)
- `--uncommitted` — review uncommitted changes (staged + unstaged + untracked) instead of branch diff
- `--commit <sha>` — review a single commit
- `--range <from>..<to>` — review a range of commits

The four modes are mutually exclusive. If no mode flag is provided, default to `--base main`.

**Diff modes** — diff command and diff-cover invocation adapt per mode:

| Mode                    | Files-changed command                             | diff-cover compare-branch | `--include-untracked` | Working-tree checkout?  |
|-------------------------|---------------------------------------------------|---------------------------|-----------------------|-------------------------|
| `--base main` (default) | `git diff main...HEAD` + working tree + untracked | `main`                    | yes                   | no                      |
| `--uncommitted`         | `git diff HEAD` + `--cached` + untracked          | `HEAD`            <br/>        | yes                   | no                      |
| `--commit abc123`       | `git diff abc123^..abc123`                        | `abc123^`                 | no                    | yes (checkout `abc123`) |
| `--range abc..def`      | `git diff abc..def`                               | `abc`                     | no                    | yes (checkout `def`)    |

In `--commit` and `--range` modes, pytest must run with the working tree
matching the upper-bound commit so coverage line numbers align with the
diff. The working tree must be **clean** before entering these modes —
stash or commit your changes first. Phase 0 will refuse to proceed if the
tree is dirty.

`diff-cover` natively sums committed (`<compare>...HEAD`) + staged + unstaged
diffs, so `--compare-branch=HEAD --include-untracked` captures the full
working-tree picture for `--uncommitted` mode without needing `--diff-file`.

## Phase 0: Preflight

Parse `$ARGUMENTS` and derive per-mode shell variables:

```bash
mkdir -p .review-cache

# Stale-sentinel recovery: a previous invocation crashed in a checkout
# mode before reaching its restore step. Restore HEAD to the saved ref
# now (before doing any new work), then clear the sentinel so this run
# starts from a known-clean state.
if [ -f .review-cache/orig-ref ]; then
    STALE_REF=$(cat .review-cache/orig-ref)
    echo "Recovering from prior crashed run; restoring HEAD to $STALE_REF"
    git checkout "$STALE_REF"
    rm -f .review-cache/orig-ref
fi

# This skill is intentionally uv-backed. Do not create uv state in unrelated
# repos, and do not fall back to bare python/pytest/pip.
if [ ! -f pyproject.toml ] || { [ ! -f uv.lock ] && ! grep -q '^\[tool\.uv\]' pyproject.toml 2>/dev/null; }; then
    echo "ERROR: /check-coverage requires a uv Python project (pyproject.toml plus uv.lock or [tool.uv])." >&2
    echo "Skipping coverage instead of creating uv lock/project state in an unrelated repo." >&2
    exit 2
fi

# Set MODE, BASE, COMMIT, RANGE_FROM, RANGE_TO from $ARGUMENTS, then:
case "$MODE" in
  base)
    COMPARE_BRANCH="$BASE"
    INCLUDE_UNTRACKED="--include-untracked"
    NEED_CHECKOUT=""
    SCOPE_DESC="vs $BASE"
    ;;
  uncommitted)
    COMPARE_BRANCH="HEAD"
    INCLUDE_UNTRACKED="--include-untracked"
    NEED_CHECKOUT=""
    SCOPE_DESC="working tree vs HEAD"
    ;;
  commit)
    if ! git rev-parse --verify "${COMMIT}^" >/dev/null 2>&1; then
      echo "ERROR: ${COMMIT} is a root commit (no parent); coverage diff requires a parent." >&2
      echo "       diff-cover's symmetric-diff form rejects the empty-tree workaround," >&2
      echo "       so use --range with an explicit RANGE_FROM, or amend onto a parent first." >&2
      exit 2
    fi
    COMPARE_BRANCH="${COMMIT}^"
    INCLUDE_UNTRACKED=""
    NEED_CHECKOUT="$COMMIT"
    SCOPE_DESC="commit $COMMIT"
    ;;
  range)
    COMPARE_BRANCH="$RANGE_FROM"
    INCLUDE_UNTRACKED=""
    NEED_CHECKOUT="$RANGE_TO"
    SCOPE_DESC="$RANGE_FROM..$RANGE_TO"
    ;;
esac

# Collect every file in scope. The set of sources matches what diff-cover
# will measure, so Phase 1b's .py discovery and Phase 2's new-tests discovery
# both filter against the same union.
case "$MODE" in
  base)
    ALL_CHANGED_FILES=$(
        {
            git diff --name-only --diff-filter=AMR "${BASE}"...HEAD
            git diff --name-only --diff-filter=AMR HEAD
            git diff --name-only --diff-filter=AMR --cached
            git ls-files --others --exclude-standard
        } 2>/dev/null | sort -u
    )
    ;;
  uncommitted)
    ALL_CHANGED_FILES=$(
        {
            git diff --name-only --diff-filter=AMR HEAD
            git diff --name-only --diff-filter=AMR --cached
            git ls-files --others --exclude-standard
        } 2>/dev/null | sort -u
    )
    ;;
  commit)
    ALL_CHANGED_FILES=$(git diff --name-only --diff-filter=AMR "${COMMIT}^..${COMMIT}" | sort -u)
    ;;
  range)
    ALL_CHANGED_FILES=$(git diff --name-only --diff-filter=AMR "${RANGE_FROM}..${RANGE_TO}" | sort -u)
    ;;
esac

if [ -z "$ALL_CHANGED_FILES" ]; then
    echo "No changes for ${SCOPE_DESC}. Nothing to check."
    exit 0
fi

# In checkout modes, refuse to clobber a dirty working tree.
if [ -n "$NEED_CHECKOUT" ]; then
    if ! git diff --quiet HEAD || ! git diff --quiet --cached \
       || [ -n "$(git ls-files --others --exclude-standard)" ]; then
        echo "Working tree is not clean. --commit and --range modes checkout"
        echo "the target ref to align line numbers with coverage data."
        echo "Stash, commit, or clean your changes first."
        exit 1
    fi
    ORIG_REF=$(git symbolic-ref --quiet --short HEAD 2>/dev/null \
               || git rev-parse --verify HEAD)
    # Persist for later phases — see "Restoring HEAD" below.
    printf '%s\n' "$ORIG_REF" > .review-cache/orig-ref
    git checkout "$NEED_CHECKOUT"
fi

echo "Scope: $SCOPE_DESC"
echo "$ALL_CHANGED_FILES"
```

If the diff is empty, report it and stop. No point running coverage on
zero changes.

**Restoring HEAD in checkout modes.** `$ORIG_REF` lives in Phase 0's shell
and is gone by Phase 1, so Phase 0 above persists it to
`.review-cache/orig-ref`. The sentinel is the authoritative store: any
later step that needs to restore HEAD reads from it.

The guard runs **only on exit paths** — never at the start of a phase, or
the restore would undo Phase 0's intentional checkout before pytest runs
in checkout modes. Specifically, run it:

- as the **last step of Phase 3**, on success;
- on every **early-exit / failure path** (e.g. coverage XML missing in
  Phase 1, pytest or diff-cover non-zero exit) before stopping;
- (NOT at the start of Phases 1 / 2 / 3 — that's how you'd defeat the
  checkout you just did).

Crash recovery is handled separately at the top of Phase 0: if a sentinel
already exists when a fresh invocation starts, that's leftover from a
previous run that died before its exit-path restore, and Phase 0 restores

+ clears it before doing any new work.

```bash
if [ -f .review-cache/orig-ref ]; then
    ORIG_REF=$(cat .review-cache/orig-ref)
    CUR=$(git symbolic-ref --quiet --short HEAD 2>/dev/null \
          || git rev-parse --verify HEAD)
    if [ "$CUR" != "$ORIG_REF" ]; then
        git checkout "$ORIG_REF"
    fi
    rm -f .review-cache/orig-ref
fi
```

The fallback uses `git rev-parse --verify HEAD` (full SHA) so it matches
the form Phase 0 stored. Using `--short` here would always compare unequal
when the original HEAD was detached (`1234567` vs the stored
`1234567abcdef…`), causing a redundant checkout on every guard call.

(Long-term, switching to `git worktree add` would eliminate the
sentinel/guard dance by isolating the checkout in a throwaway directory —
worth a follow-up refactor.)

**Source config preflight.** `pytest-cov` with a bare `--cov` flag only
tracks files *imported* during the test run. If the project has source
packages but `[tool.coverage.run].source` in `pyproject.toml` is empty
or commented out, a brand-new module that no test imports will be
*invisible* to coverage.xml — and this command will silently report it
as "not in the diff's coverage data" rather than "0% covered". Don't take
the config on faith — read it and ground the run in what's actually set:

```bash
uv run python -c "
import pathlib, tomllib
p = pathlib.Path('pyproject.toml')
src = (tomllib.loads(p.read_text()).get('tool', {}).get('coverage', {}).get('run', {}).get('source')
       if p.is_file() else None)
if src:
    print(f'coverage source = {src} - unimported modules WILL be measured')
else:
    print('⚠️  [tool.coverage.run].source is unset - modules that no test '
          'imports are INVISIBLE to coverage.xml; Phase 1b is the only signal.')
"
```

If the ⚠️ fires, surface it up front in the Phase 3 report (before the
coverage tables): brand-new modules that no test imports won't appear in
coverage at all, and Phase 1b is the only check that catches them — the fix
is to add the package to `[tool.coverage.run].source`. If a source list is
printed, proceed normally.

**Default-branch sanity (base mode).** The default base is `main`. Surface
the repo's real default branch so comparing against the wrong one is visible
before pytest runs:

```bash
DEFAULT_REF=$(git symbolic-ref --quiet --short refs/remotes/origin/HEAD 2>/dev/null || true)
DEFAULT_BRANCH=${DEFAULT_REF#origin/}
echo "repo default branch: ${DEFAULT_BRANCH:-<no origin/HEAD detected>}"
```

In default (`--base`) mode, if this isn't `main`, re-run with
`--base "$DEFAULT_BRANCH"`. Informational — it does not change the default.

## Phase 1: Coverage by ALL tests

Run the full suite with XML coverage output:

```bash
uv run pytest --cov --cov-report=xml:.review-cache/coverage-all.xml
```

**If `.review-cache/coverage-all.xml` is missing after this runs** —
pytest collected no tests (common on a brand-new downstream project
before the first test lands). pytest-cov exits 0 with a "No data to
report" warning but doesn't write the XML file. Tell the user:

> No coverage data — pytest collected no tests. Add tests to the
> project and re-run `/check-coverage`, or run
> `uv run pytest --collect-only` to verify test discovery.

Then run the Restoring HEAD guard (above) before stopping. Do not proceed
to Phase 1b or Phase 2 — both require the coverage XML.

Otherwise, run diff-cover against the per-mode compare-branch:

```bash
# shellcheck disable=SC2086 — $INCLUDE_UNTRACKED is empty or "--include-untracked"
uv run diff-cover .review-cache/coverage-all.xml \
    --compare-branch="$COMPARE_BRANCH" \
    --json-report=.review-cache/diff-cover-all.json \
    $INCLUDE_UNTRACKED
```

`--include-untracked` is included for `--base` and `--uncommitted` modes
so a brand-new file Claude hasn't staged is still measured. In `--commit`
and `--range` modes the working tree is checked out to match the target
ref, so there are no untracked files to include and the flag is omitted.

Read `.review-cache/diff-cover-all.json`. It has this structure:

```json
{
  "total_num_lines": 42,
  "total_num_violations": 7,
  "total_percent_covered": 83,
  "src_stats": {
    "scrapers/new_thing.py": {
      "percent_covered": 75.0,
      "violation_lines": [
        12,
        15,
        28,
        47
      ]
    }
  }
}
```

The `violation_lines` array is the per-file list of uncovered changed lines.

### Phase 1b: Defensive check — .py files absent from coverage data

`diff-cover` only reports on files that appear in `coverage.xml`. A new
`.py` module that *no test imports* is completely absent from the XML, so
Phase 1 silently skips it — the file shows up nowhere in `src_stats`,
`diff-cover-all.json` looks clean, and the "close the loop" workflow
passes without flagging that the code was never exercised at all. This
is the exact failure mode the **Source config preflight** in Phase 0 warns
about, and the fix is to cross-check the diff's `.py` file list against
`coverage.xml`'s tracked-file list and report the difference:

```bash
# List .py files added/modified/renamed in the diff (using ALL_CHANGED_FILES
# from Phase 0, which already encodes the per-mode scope). Pass them via
# environment variable to a Python one-liner that loads coverage.xml,
# computes the set difference, and prints any files missing from coverage.
NEW_PY=$(printf '%s\n' "$ALL_CHANGED_FILES" | grep -E '\.py$' || true)

if [ -n "$NEW_PY" ]; then
    NEW_PY="$NEW_PY" uv run python -c "
import os
import xml.etree.ElementTree as ET

diff_files = {line.strip() for line in os.environ['NEW_PY'].splitlines() if line.strip()}
tracked = {c.get('filename') for c in ET.parse('.review-cache/coverage-all.xml').iter('class')}
missing = sorted(diff_files - tracked)
if missing:
    print('⚠️  .py files in diff but NOT in coverage.xml:')
    for f in missing:
        print(f'  - {f}')
"
fi
```

If the script prints any files, **call each one out in the Phase 3 report
as "no coverage data"** — don't lump them in with "0% covered". They represent
a distinct failure mode: the file exists, but no test imports it, so
coverage has literally nothing to say about it. The fix is usually one of:

- Write a test that imports and exercises the module
- Add the module's package to `[tool.coverage.run].source` so coverage
  measures it even when no test imports it
- If the module is intentionally unreachable (plugin entry point, CLI
  only triggered in production), mark it with `# pragma: no cover` at
  module level

## Phase 2: Coverage by NEW tests only

Find test files that were added, modified, or renamed in the diff. The
regex matches only pytest-discoverable test *modules* — `test_*.py` and
`*_test.py` — and deliberately excludes `conftest.py`, `__init__.py`,
and fixture/helper files under `tests/`. Passing `conftest.py` as a
positional argument to pytest collects zero items and fails the run,
which would break Phase 2 for any diff that touches only fixtures.

```bash
# Filter ALL_CHANGED_FILES (from Phase 0) for test modules. The shared
# variable already captures whichever scope the selected mode targets,
# so untracked tests in --base/--uncommitted modes flow through alongside
# committed tests in --commit/--range modes.
NEW_TESTS=$(printf '%s\n' "$ALL_CHANGED_FILES" | \
    grep -E '(^|/)test_[^/]*\.py$|(^|/)[^/]*_test\.py$' || true)
```

If `NEW_TESTS` is empty, skip Phase 2 and report "no new test files". This
isn't automatically a problem — Claude may not have written any tests this
turn, the diff may only touch fixtures/conftest (which don't run as
standalone tests), or the tests may have gone into a different branch.

Otherwise run pytest with only those files, producing a second coverage
report, and diff-cover that one too:

```bash
# shellcheck disable=SC2086 — $NEW_TESTS / $INCLUDE_UNTRACKED are word-split intentionally
uv run pytest --cov --cov-report=xml:.review-cache/coverage-new.xml $NEW_TESTS
uv run diff-cover .review-cache/coverage-new.xml \
    --compare-branch="$COMPARE_BRANCH" \
    --json-report=.review-cache/diff-cover-new.json \
    $INCLUDE_UNTRACKED
```

## Phase 3: Synthesis

Read both JSON reports (or just Phase 1 if Phase 2 was skipped). If the
mode required a checkout, run the Restoring HEAD guard (above) BEFORE
producing the report output so the user lands back on their working
branch even if synthesis errors. Then produce a summary for the user:

1. **Coverage table** — two rows (or one if Phase 2 was skipped):

   | Phase         | Coverage | Missing lines             |
   |---------------|----------|---------------------------|
   | 1 (all tests) | N%       | list of `file:line-range` |
   | 2 (new tests) | N%       | list of `file:line-range` |

2. **Phase delta diagnostic** — if Phase 1 coverage is materially higher
   than Phase 2 (e.g. >10 percentage points), call it out explicitly:

   > Phase 1 is X% but Phase 2 is Y%. The new code is covered by *existing*
   > tests but not by the new tests in this diff. The new tests may be
   > testing the wrong thing (wrong import, wrong class, wrong fixture) —
   > read the new test files and verify they actually exercise the code
   > you just wrote.

3. **Per-file action list** — for each file with missing lines, think
   about each uncovered line before proposing a fix:

   - Is this a path that could be buggy? Add a test that would **catch**
     the bug, not just exercise the line.
   - Are there edge cases at this line (None, empty, negative, overflow,
     boundary, off-by-one) that the current tests don't hit?
   - Is this an error path? Make sure a test asserts the error is
     actually raised/handled, not just tolerated.
   - Is this intentionally uncovered (logging, trivial getter, debug
     print)? If so, flag it for `pragma: no cover` or
     `exclude_lines` config — don't force a test that adds no value.

4. **Do not chase 100%**. Coverage is necessary but not sufficient.
   Academic research (arxiv 2412.14137) shows that LLMs filtering for
   "tests that pass" systematically **miss real bugs** — tests written to
   satisfy coverage targets encode whatever behavior the code has, bugs
   included. If the code under test has a bug, a passing test is worse
   than no test. Re-read the code before writing tests for a gap, and
   only add tests that would *catch the bug if the implementation were
   wrong*.

## What this command is not

- **Not a pass/fail gate.** No `--fail-under` anywhere. Report, don't block.
- **Not a test generator.** It tells Claude what's missing; Claude writes
  the tests. Tools like Qodo-Cover and CoverUp generate autonomously, but
  they inherit the "tests designed to pass" failure mode.
- **Not a replacement for thinking.** The per-file action list above is a
  checklist, not a substitute for understanding the code.

## Variants

Common scoping invocations:

- "compare against a different base" — `/check-coverage --base develop`
- "only working-tree changes" — `/check-coverage --uncommitted`
- "only the last commit" — `/check-coverage --commit HEAD`
- "a specific commit" — `/check-coverage --commit abc123`
- "the last N commits" — `/check-coverage --range HEAD~N..HEAD`
- "an arbitrary range" — `/check-coverage --range abc123..def456`

For "staged only" there isn't a slash-command flag — run the Phase 1
command directly with `diff-cover --compare-branch=HEAD --ignore-unstaged`.
