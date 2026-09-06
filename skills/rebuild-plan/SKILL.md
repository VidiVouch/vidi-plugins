---
name: rebuild-plan
description: MUST be used whenever a rebuild/greenfield re-implementation needs an epoch plan — decomposes a target ref into dependency-ordered epochs, writes the REBUILD-EPOCHS.md ledger, runs a DETERMINISTIC target-tree coverage check (never trusts the architect's claim), and judge-panels the plan to ACCEPTED. Also the return path for mid-build plan defects via --amend. No rebuild code may be written before this skill reports ACCEPTED.
argument-hint: "--target <ref> --from <base> --into <branch> --ledger <path> [ --guide <range> ] [ --amend \"<reason>\" ] [ --max-rounds <n> ]"
allowed-tools: Agent, Read, Write, Edit, Glob, Grep, AskUserQuestion, TaskCreate, TaskUpdate, TaskList, Bash(git *), Bash(mkdir *), Bash(mktemp *), Bash(echo *), Bash(printf *), Bash(cat *), Bash(ls *), Bash(sed *), Bash(awk *), Bash(sort *), Bash(comm *), Bash(cut *), Bash(tr *), Bash(wc *), Bash(head *), Bash(tail *), Bash(grep *), Bash(test *), Bash(date *), Bash(sh *)
---

# Rebuild Plan

Forward epoch decomposition for a clean rebuild: given a **target** ref (the
floor to re-express) and a **base** ref (the clean foundation), produce an
ACCEPTED `REBUILD-EPOCHS.md` ledger — the single durable contract that
`rebuild`, `rebuild-epoch`, and `rebuild-examine` all read and write.

Division of labour, non-negotiable:

- **Deterministic (this skill runs it, in code):** ref validation, target tree
  enumeration, the coverage-matrix check. Coverage is **computed, never
  asserted** — an architect claiming "coverage gaps: NONE" is ignored; only
  the script's output counts.
- **Judgment (delegated to agents):** one architect writes the ledger; a judge
  panel reviews decomposition soundness. Revise-loop to unanimous PASS or the
  round cap, then escalate to the user.
- **Portable:** everything project-specific — jargon, invariant series,
  toolchain gates, prose-sweep patterns, verbatim-data carve-outs — lives **in
  the ledger as data**, authored by the architect for *this* project. This
  skill body stays project-agnostic.

## Deterministic context (auto-populated at load)

Resolved by the shell when this skill loads — do NOT re-probe with your own Bash:

- Live checkout (PROJ): !`git rev-parse --show-toplevel`
- Current branch: !`git symbolic-ref --quiet --short HEAD 2>/dev/null || git rev-parse --short HEAD`
- HEAD: !`git rev-parse HEAD`
- Toolchain: !`if test -f Cargo.toml; then echo rust; elif test -f pyproject.toml && { test -f uv.lock || grep -q '^\[tool\.uv\]' pyproject.toml 2>/dev/null; }; then echo uv-python; else echo none; fi`

All git ops below use `git -C "$PROJ"` — the Bash tool's cwd is not guaranteed.

## Arguments

Parse `$ARGUMENTS`:

- `--target <ref>` — **required.** The read-only reference tree being
  re-expressed (the floor).
- `--from <base>` — **required.** The clean foundation the rebuild branch
  starts from.
- `--into <branch>` — **required.** The rebuild branch name (recorded in the
  ledger header; this skill never creates it — the orchestrator owns worktrees).
- `--ledger <path>` — **required.** Absolute path where the ledger is written
  (normally `<rebuild-worktree>/REBUILD-EPOCHS.md`).
- `--guide <range>` — optional commit range from the original build; the
  architect mines it for grouping/ordering evidence only. **Never** a source of
  code to copy.
- `--amend "<reason>"` — delta mode: revise an existing ACCEPTED ledger (see
  **Amend mode**). Mutually exclusive with producing a fresh plan.
- `--max-rounds <n>` — judge-revise round cap (default 3).

Stop with usage if a required arg is missing.

## Phase 0: Validate

```bash
TARGET=$(git -C "$PROJ" rev-parse --verify "<--target>^{commit}") || stop "target does not resolve"
FROM=$(git -C "$PROJ" rev-parse --verify "<--from>^{commit}")     || stop "base does not resolve"
git -C "$PROJ" merge-base --is-ancestor "$FROM" "$TARGET" || echo "note: base is not an ancestor of target (allowed, but say so in the report)"
```

`--amend` additionally requires the ledger to exist and to contain a
`Plan-status: ACCEPTED` header; a missing/DRAFT ledger → stop ("nothing to
amend — run a fresh plan").

## Phase 1: Deterministic ground truth

Gather the facts the architect and judges work from (attach to their prompts):

```bash
git -C "$PROJ" diff --stat "$FROM..$TARGET"
git -C "$PROJ" ls-tree -r --name-only "$TARGET" | sort   # the FULL enumeration the coverage check runs against
git -C "$PROJ" log --oneline --stat --reverse "<--guide range>"   # only if --guide given
```

## The ledger — normative schema

`REBUILD-EPOCHS.md` is Markdown (machine-parsed ledger, per project doc
conventions). It is **untracked and never committed**: after writing it, append
its basename to the rebuild worktree's `.git/info/exclude`. This is fixed at
skill level and not re-litigable mid-run.

Header (before section 1): target/base/into refs pinned to full shas, and a
machine-greppable status line — exactly one of:

```
Plan-status: DRAFT
Plan-status: ACCEPTED round=<n> date=<YYYY-MM-DD>
```

Required sections, in order. §§1–5 are authored by the architect; §§6–10 are
**initialized empty/PENDING here** and filled by the other skills:

1. **Global rules** — project-specific invariants that apply to every epoch:
   fresh-author-only; final-names-not-final-wiring; the
   full-tree-gate-is-the-forward-ref-check rule; regenerate-not-copy for
   derived artifacts; floor-not-ceiling (fix real bugs, log them, escalate
   design changes). Plus, as fenced data blocks consumed by the deterministic
   gate (`rebuild-gate.sh` in the sibling skills), authored here **once** for
   this project:
   - ` ```prose-sweep-patterns ` — one grep-able pattern per line, with a
     `# comment` — the project's curated present-tense-absent-capability
     patterns (absent-path mentions, "CI asserts X", "regenerate → no diff",
     labels for things later epochs build).
   - ` ```gate-commands ` — the full-tree gate command sequence for this
     toolchain (and any project carve-outs, e.g. "never run advisory checks").
2. **Dependency structure** — module/crate graph, any project invariant-series
   → epoch map, namespace disambiguations. Free prose + tables; judges read
   it, scripts don't.
3. **Epochs** — one entry per epoch, id `E0..En`, each with **all** of:
   `name` · `purpose` · `scope` (prose) · `dependsOn` (epoch ids) ·
   `target-ref` (behaviour/invariants to study — never commits to copy) ·
   `acceptance` (gates that must pass) · `excludes` (boundaries punted to a
   named later epoch) · **`file-form`** (which scope paths are authored
   *as-of-epoch*, forward references stripped, vs land in genuinely *final*
   form — every path must be one or the other, and "final form" is only legal
   when nothing it references lands later) · **`pure-data-allowlist`** (paths
   permitted byte-identical to the target: fixtures, corpora, contract JSON,
   brand CSS, and genuine config/legal files whose one canonical form is the
   target's — `LICENSE`, `.gitignore`, `.editorconfig`. These are data, not
   logic, so allowlisting them is legitimate. Everything else must differ; the
   gate checks this mechanically. Small canonical-trivial *source* lines that
   are inevitably identical — `pub mod x;`, an empty `__init__.py` — do NOT
   need allowlisting: the gate floors them by byte size (< 64 B). Authored as a
   **machine-parsed fenced block inside the epoch's `### E<id>` heading
   section** — the gate extracts it per-section, so it MUST live under that
   heading (format below); a prose list elsewhere is not read.
4. **Coverage matrix** — a human-readable table **plus** the machine-parsed
   fenced block the Phase 3 check consumes (format below). The table is
   commentary; the fenced block is the contract.
5. **Rulings** — running log of user/steerer decisions on ambiguous cases,
   one per line: `R<n> · <date> · <ruling> · <origin>`. **A judge finding
   that contradicts an existing ruling is void unless it presents new
   evidence.** Seed with any rulings already made for this project.
6. **Agent registry** — table `epoch · role · agentId/taskId · outputPath ·
   spawnedAt · lastKnownCommit · lastProgressAt · status`. Initialized empty.
   The spawning skill writes its own agent's row (it holds the handle); the
   orchestrator owns the watchdog columns (`lastProgressAt`, `status`) and the
   Rulings appends.
7. **Bookkeeping (state machine)** — one row per epoch, `Status` strictly one
   of: `PENDING → PLANNED → (IMPLEMENTED@<sha>` when the gate passed **or**
   `GATED(fail)` when it did not) `→ JUDGING(round <n>) → REVISE@<tip> →`
   (re-enters `IMPLEMENTED@<sha'>` after a clean `--fix`) `→ ACCEPTED@<lo..hi>`.
   There is no `GATED(pass)` state — a green gate goes straight to
   `IMPLEMENTED@<sha>`; `GATED(fail)` is the only `GATED` value, and it resumes
   by re-dispatching `rebuild-epoch` (or escalating). `REVISE@<tip>` carries the
   examined post-fix tip so `rebuild-epoch --fix` reads the right rejected sha.
   The post-fix tip sha of an ACCEPTED epoch is the accepted boundary and the
   next epoch's `lo`. Initialized: every epoch `PENDING`.
8. **Deviations log** — one row per deliberate deviation from the target:
   `epoch · file:line · was → now · why · test`. Initialized empty.
9. **Safety-ref registry** — any `safety/*` ref created for a history
   rewrite, with the fixed lifecycle *created before rewrite, deleted on
   ACCEPT*. Two parts: a human-readable table (`ref · created-for ·
   lifecycle`) **and** a machine-parsed ` ```safety-refs ` fenced block whose
   first whitespace-column is the ref name — the gate reads the fenced block
   and fails on any existing `safety/*` ref not declared there. Both
   initialized empty (an empty fenced block is required from the start so the
   gate has a source to read).
10. **Final-examine checklist** — project-specific whole-branch invariants
    verified once before merge (parity diff expectations, invariant-series
    end-to-end checks, regen→no-diff artifacts).

### The coverage-map block (machine contract)

Inside §4, exactly one fenced block:

    ```coverage-map
    # <glob><TAB><epoch-id>   — first match wins; case-glob semantics (* crosses /)
    crates/vidi-core/src/crypto/*	E4
    crates/vidi-core/*	E1
    editors/jetbrains/*	E18
    .github/*	E0
    ...
    ```

Rules: one `<glob>\t<epoch>` per non-comment line; globs are POSIX `case`
patterns matched against full repo-relative paths (`*` matches across `/` —
these are prefix carve-outs, not gitignore rules); **order matters** (first
match wins, so specific rules go above general ones — that is how "exactly one
primary epoch" is made deterministic); every epoch id must exist in §3.

### The per-epoch machine blocks (gate contracts)

The gate (`rebuild-gate.sh`) extracts three fenced-block families verbatim —
author them exactly, tag = the word immediately after the three backticks,
`#` comments and blank lines ignored:

- **`pure-data-allowlist`** — inside each epoch's `### E<id> — <name>` heading
  section (the gate scopes extraction to that section by the heading's second
  whitespace token, so the id must be a bare token: `### E4 — Core crypto`).
  One POSIX `case` glob per line; every listed path may be byte-identical to
  the target, everything else must differ. Omit the block (or leave it empty)
  for an epoch with no pure-data files.

      ```pure-data-allowlist
      # E4 — pure-data carve-outs (byte-identical to target is allowed here)
      schemas/interop/signing-vectors.json
      crates/*/tests/vectors/*.json
      ```

- **`prose-sweep-patterns`** — one global block in §1; one grep ERE per line,
  scanned case-insensitively over every changed text file (present-tense
  absent-capability claims, absent-path mentions, "CI asserts", "regenerate →
  no diff", labels for later-epoch work).
- **`safety-refs`** — one global block in §9; first whitespace-column per line
  is a declared `safety/*` ref name. Starts empty; `rebuild-epoch` appends a
  line when it creates one, `rebuild-examine` marks it deleted on ACCEPT.

## Phase 2: The architect

Spawn ONE architect agent (default: the session model) whose prompt contains:
the Phase 1 facts, this schema (paste the ledger section above), the `--guide`
log if any, and the project docs (`AGENTS.md`, any rebuild notes). If `--guide`
is large, fan the *mining* out over slices with sub-agents, but one architect
writes the ledger. Brief, verbatim requirements:

- Decompose `FROM..TARGET` into dependency-ordered epochs; every epoch buildable
  and fully gate-green **as of itself** (no forward references).
- Fill §§1–5 completely; initialize §§6–10 per the schema.
- `file-form` and `pure-data-allowlist` are REQUIRED per epoch — an epoch
  without them is incomplete.
- The coverage-map must be written path-by-path against the real
  `git ls-tree` output provided — **do not** summarize; your claims are not
  checked, your globs are.
- Never copy code or commit structure from `--guide`; mine grouping/order only.

Write the ledger to `--ledger` with `Plan-status: DRAFT`.

## Phase 3: Deterministic coverage check (run EVERY round, and after every amend)

Run this yourself in Bash. Do not delegate it, do not accept an agent's word
for it, do not skip it because the ledger "looks complete":

```bash
TMP=$(mktemp -d)
git -C "$PROJ" ls-tree -r --name-only "$TARGET" | sort > "$TMP/paths"

# Extract the machine block (strip fences and comments/blank lines)
sed -n '/^```coverage-map$/,/^```$/p' "$LEDGER" | sed '1d;$d' \
  | grep -v '^[[:space:]]*#' | grep -v '^[[:space:]]*$' > "$TMP/map"
test -s "$TMP/map" || { echo "FAIL: no coverage-map block"; exit 1; }

# Epoch ids referenced by the map must exist in §3
cut -f2 "$TMP/map" | sort -u > "$TMP/map_epochs"
grep -oE '^### E[0-9]+' "$LEDGER" | grep -oE 'E[0-9]+' | sort -u > "$TMP/plan_epochs"
comm -23 "$TMP/map_epochs" "$TMP/plan_epochs" > "$TMP/unknown_epochs"
comm -13 "$TMP/map_epochs" "$TMP/plan_epochs" > "$TMP/unmapped_epochs"   # epochs with zero rules

# First-match-wins assignment; every target path must match exactly one rule
sh -c '
  TMP="$1"; fail=0
  while IFS= read -r p; do
    hit=""
    while IFS="$(printf '\''\t'\'')" read -r pat epoch; do
      case "$p" in $pat) hit="$epoch"; break;; esac
    done < "$TMP/map"
    if [ -z "$hit" ]; then echo "UNCOVERED	$p"; fail=1
    else echo "$hit	$p" >> "$TMP/assign"; fi
  done < "$TMP/paths"
  exit $fail
' _ "$TMP" > "$TMP/uncovered"; COVER_OK=$?

# Dead rules: every glob must match >= 1 real target path
sh -c '
  TMP="$1"
  while IFS="$(printf '\''\t'\'')" read -r pat epoch; do
    m=0
    while IFS= read -r p; do case "$p" in $pat) m=1; break;; esac; done < "$TMP/paths"
    [ "$m" = 0 ] && echo "DEAD-RULE	$pat	$epoch"
  done < "$TMP/map"
' _ "$TMP" > "$TMP/dead"

cut -f1 "$TMP/assign" | sort | uniq -c | sort -k2 -V     # per-epoch path counts, for the report
```

**Verdict (all four, mechanically):**
- `$TMP/uncovered` non-empty → **FAIL** — list every uncovered path verbatim.
- `$TMP/unknown_epochs` non-empty → **FAIL** — map references epochs §3 doesn't define.
- `$TMP/unmapped_epochs` non-empty → **FAIL** — an epoch owns zero target paths
  (either a dead epoch or a coverage hole hiding behind prose).
- `$TMP/dead` non-empty → **WARN** (surface to the judges; a dead rule usually
  means a typo'd path that silently un-covers what it meant to cover).

Run the same glob loop over each epoch's `pure-data-allowlist` entries: an
allowlist glob matching zero target paths is a WARN with the same rationale.

On FAIL: hand the exact failing lines back to the architect (a fresh spawn
with a written handoff, not a resumed one) and re-run this phase. The judge
panel never sees a plan that hasn't passed this check.

## Phase 4: The judge panel

Spawn the judges **in parallel** via the Agent tool: judge A = the session
model, judge B = `model: 'fable'`. No external/CLI judges here (planning is
Claude-only; a third perspective is the examine skill's concern). Each judge
gets: the full ledger, the Phase 1 facts, the Phase 3 report (counts,
warnings), and this brief — verbatim:

> Review this rebuild plan for: (1) decomposition soundness — does every epoch
> build green as-of-itself given only its `dependsOn` closure; (2) dependency
> order — no cycle, no epoch consuming a later epoch's output; (3) cohesion &
> granularity — no grab-bag epochs, no artificial splits of tightly-coupled
> work; (4) per-epoch `file-form` / `pure-data-allowlist` consistency — flag
> any path required in "final form" that references capabilities a later epoch
> builds (the plan-baked contradiction class), and any allowlist entry that is
> code rather than pure data; (5) acceptance criteria — testable, not
> narrative. Coverage is ALREADY mechanically verified — do not re-litigate
> path coverage; the attached report is ground truth. A finding that
> contradicts a §5 ruling is void unless you present new evidence — cite the
> ruling id if you override. Return ONLY the JSON result shape.

**Result shape (required from every judge):**

```json
{
  "verdict": "PASS" | "REVISE",
  "findings": [
    {
      "severity": "BLOCKING" | "MAJOR" | "MINOR",
      "epoch": "E<n>" | null,
      "class": "<short pattern label — group siblings, don't drip instances>",
      "description": "<what is wrong>",
      "evidence": "<ledger line / dependency edge / file-form entry cited>"
    }
  ]
}
```

**Shared severity rubric (both judges are told this; disagreement is resolved
by the rubric, not by whoever spoke last):**

- **BLOCKING** — the plan, executed as written, produces a broken epoch:
  dependency cycle or missing `dependsOn` edge; a file-form "final" entry with
  a forward reference; an allowlist entry covering logic; an acceptance
  criterion that cannot be evaluated; an epoch that cannot be gate-green
  as-of-itself.
- **MAJOR** — likely to force a mid-build `--amend` or a judge REVISE
  downstream: incohesive grab-bag epoch, split-across-epochs coupling,
  under-specified `target-ref`, missing `excludes` boundary.
- **MINOR** — logged in the report, never blocks acceptance.

**Decision rule:** ACCEPTED requires **unanimous PASS** with zero open
BLOCKING findings. Any REVISE → collect both judges' findings, dedupe by
`class`, hand the merged class-scoped list to a **fresh** architect spawn to
revise the ledger, re-run Phase 3, re-judge. Round cap = `--max-rounds`
(default 3); on cap exceeded, stop and escalate to the user via
AskUserQuestion with the surviving findings — never self-certify past the cap.
MAJOR findings the architect *rejects* with reasoning go to the user with the
escalation; MINOR ones are appended to the ledger report section and dropped.

## Phase 5: Accept & hand off

On unanimous PASS:

1. Flip the header to `Plan-status: ACCEPTED round=<n> date=<today>`.
2. Verify §7 bookkeeping has one `PENDING` row per §3 epoch (mechanical grep
   count — they must match).
3. Ensure the ledger basename is in the worktree's `.git/info/exclude` and
   `git -C <worktree> ls-files <ledger>` is empty.
4. Report: epoch count, per-epoch path counts from Phase 3, rounds used,
   MINOR findings logged, any user escalations resolved.

**No-code-before-plan-accepted gate (hard contract):** no rebuild
implementation work — no commit on the `--into` branch beyond `--from` —
may exist before this skill reports ACCEPTED. `rebuild` and `rebuild-epoch`
enforce it by grepping the status line
(`grep -q '^Plan-status: ACCEPTED' "$LEDGER"`) **and** verifying
`git rev-list --count <from>..<into>` was 0 at acceptance time; if commits
predate acceptance, stop and escalate — the plan was written to fit code
instead of the code to fit the plan.

## Amend mode (`--amend "<reason>"`)

The return path for a mid-build `PLAN-DEFECT` (a builder hit a dependency the
plan doesn't supply) or a user-directed scope change. Delta, not re-plan:

1. **Scope lock:** only epochs whose §7 status is NOT `ACCEPTED@…` may be
   restructured. Accepted epochs are frozen history — if the defect implicates
   one, stop and escalate to the user (that is a rebuild-level decision, not a
   plan edit).
2. Spawn one architect (fresh) with: the reason, the current ledger, the Phase
   1 facts. It revises **only** the implicated epoch entries, the coverage-map
   lines whose epoch column changed, and (if the defect revealed a missing
   global rule or ruling) §1/§5 appends. Every edit is logged as a dated entry
   under §5 rulings or §8 deviations, whichever fits.
3. Re-run **Phase 3 in full** (it is cheap; a delta coverage check is a false
   economy).
4. Re-judge **the delta only**: both judges get the reason + a diff of the
   ledger (before/after) + the fresh Phase 3 report, same result shape, same
   rubric, same decision rule, cap = `--max-rounds`.
5. On PASS: bump the header to `Plan-status: ACCEPTED round=<n+amend>
   date=<today>` and append an amend line under §5:
   `R<n> · <date> · AMEND: <reason> → <epochs touched> · rebuild-plan --amend`.

## Important rules

- **Coverage is computed, never asserted.** The Phase 3 script is the only
  authority; architect prose about coverage is ignored.
- **The judge panel never sees an unchecked plan.** Phase 3 gates Phase 4,
  every round.
- **Fresh spawns for every revision round** — never resume an architect or
  judge; hand off in writing (findings list + required output shape).
- **Rulings are law.** A finding contradicting §5 without new evidence is
  void; record every user decision made during this run as a new §5 line.
- **This skill writes no code and creates no branches/worktrees.** Its sole
  artifacts are the ledger and the acceptance status.
- **The ledger is never committed.** `.git/info/exclude`, verified at accept.
- **Project specifics live in the ledger.** Prose-sweep patterns, gate
  commands, jargon disambiguation, verbatim-data carve-outs — data in §1–§2,
  never additions to this skill body.
- **Claude-only skill** (`!` injection + Agent); not usable from Codex.
