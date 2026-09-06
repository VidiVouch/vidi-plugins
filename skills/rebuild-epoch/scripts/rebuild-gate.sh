#!/usr/bin/env bash
# rebuild-gate.sh — deterministic acceptance gate for one rebuilt epoch range.
#
# A plain CLI with real exit codes and a JSON report file — deliberately NOT a
# reuse of .claude/hooks/review-gate.sh (that is a Stop-hook script driven by
# the harness's stdin/stdout hook protocol and always exits 0). Both
# rebuild-epoch and rebuild-examine invoke this directly via Bash and read the
# JSON report; no implementer or judge self-report ever substitutes for it.
#
# Checks (JSON checks[].name, in report order):
#   refs                base/tip/target resolve; base is an ancestor of tip;
#                       range is non-empty and linear (no merges)
#   ledger_untracked    the ledger file is not tracked, not in the tip tree,
#                       and not touched by any commit in the range
#   commit_shape        heuristic: no appended fix/re-author/address-judge
#                       chain inside the candidate range (revisions must be
#                       replacement ranges). Commits whose subject starts with
#                       "examine fixes for epoch " are exempt — those are
#                       rebuild-examine's own verified-fix commits, which are
#                       legitimate appends by contract.
#   verbatim_blobs      no file changed in the range is byte-identical to the
#                       target's blob unless allowlisted (pure data)
#   toolchain_gate      full-tree toolchain gate at the tip, run from a clean
#                       `git archive` extraction (never the worktree in place)
#   per_commit_gate     the same gate for every other commit in the range
#   forbidden_patterns  supplied prose/forward-reference patterns scanned over
#                       the range's changed text files; not_assessed when no
#                       pattern source is supplied (no fake universal parser)
#   safety_refs         every existing safety/* ref is declared in the supplied
#                       declaration source; undeclared safety refs fail
#   dependency_staging  always not_assessed — unused-dependency staging is a
#                       semantic judgment the judge panel owns, not something
#                       this script can decide deterministically
#
# Statuses: ok | fail | skipped | not_assessed.   pass == no "fail" entries.
# Exit codes: 0 = pass · 1 = at least one check failed · 2 = usage/env error.
#
# Machine inputs (this script never semantically parses arbitrary Markdown):
#   --allowlist-file / --safety-refs-file / --forbidden-patterns-file are
#   newline-separated files (globs / refs / ERE patterns; `#` comments and
#   blank lines ignored). Alternatively the ledger may carry clearly delimited
#   fenced blocks the script extracts verbatim:
#     - a pure-data-allowlist block inside the epoch's `### E<id> ...` section
#       (whole-ledger scope when --epoch is not given, e.g. final mode)
#     - a safety-refs block (first whitespace-separated column = the ref)
#     - a prose-sweep-patterns block (the rebuild-plan schema's global block)
#   Fence tag = the block name immediately after the three backticks.
#
# Compatibility: macOS bash 3.2 (no associative arrays, no mapfile). JSON is
# emitted via python3 (the plugin assumes Python >= 3.10); no jq dependency.

set -u

usage() {
  cat <<'EOF'
usage: rebuild-gate.sh --repo <dir> --target <ref> (--base <ref> --tip <ref> | --range <lo>..<hi>)
                       [--epoch <id>] [--ledger <path>] [--output <file>]
                       [--gate-cmd <cmd>] [--no-per-commit]
                       [--allowlist-file <file>] [--safety-refs-file <file>]
                       [--forbidden-patterns-file <file>]

  --repo | --worktree     the rebuild worktree/checkout the range lives in
  --target                the original ref being rebuilt (verbatim-copy floor)
  --base / --tip          the candidate epoch range lo..hi (tip inclusive)
  --range <lo>..<hi>      shorthand for --base/--tip
  --epoch <id>            epoch id, used for labels and for scoping the
                          ledger's pure-data-allowlist block extraction
  --ledger <path>         the REBUILD-EPOCHS.md ledger (untracked check +
                          fenced-block extraction)
  --output | --out        JSON report path (default <repo>/.rebuild/gate-report.json)
  --gate-cmd | --gate     custom full-tree gate command (sh -c, cwd = clean
                          extraction; overrides toolchain detection)
  --no-per-commit         run the toolchain gate at the tip only
  --allow-no-gate         permit a toolchain-less repo (no gate command found) to
                          PASS the toolchain check; without it, "nothing gated" FAILS
  --final                 whole-branch mode: relax commit_shape to revision-marker
                          subjects only (skip the add-then-fix heuristic, which
                          false-positives against legitimately-accepted deviation
                          commits across already-accepted epochs)
  --not-descendant-of <sha>  revision-reset check: FAIL commit_shape if <sha> (the
                          rejected tip) is an ancestor of the new tip — proves the
                          revision was reset to base, not appended onto the reject
  --verbatim-min-bytes N  identical blobs smaller than N bytes (default 64) are
                          treated as canonical-trivial, not fresh-authorship
                          violations (e.g. `pub mod x;`, empty __init__.py)
  --allowlist-file        newline-separated globs allowed byte-identical to target
  --safety-refs-file      newline-separated declared safety/* refs
  --forbidden-patterns-file  newline-separated ERE patterns (prose sweep)
EOF
}

die() { echo "rebuild-gate: $*" >&2; exit 2; }

REPO="" BASE="" TIP="" TARGET="" EPOCH="" LEDGER="" OUT="" GATE_CMD=""
ALLOW_FILE="" SAFETY_FILE="" PATTERNS_FILE="" PER_COMMIT=1
ALLOW_NO_GATE=0 VERBATIM_MIN=64 FINAL=0 NOT_DESC_OF=""

need_val() { [ -n "${2-}" ] || die "$1 requires a value"; }
while [ $# -gt 0 ]; do
  case "$1" in
    --repo|--worktree)  need_val "$1" "${2-}"; REPO="$2"; shift 2 ;;
    --base)             need_val "$1" "${2-}"; BASE="$2"; shift 2 ;;
    --tip)              need_val "$1" "${2-}"; TIP="$2"; shift 2 ;;
    --range)            need_val "$1" "${2-}"; BASE="${2%%..*}"; TIP="${2##*..}"; shift 2 ;;
    --target)           need_val "$1" "${2-}"; TARGET="$2"; shift 2 ;;
    --epoch)            need_val "$1" "${2-}"; EPOCH="$2"; shift 2 ;;
    --ledger)           need_val "$1" "${2-}"; LEDGER="$2"; shift 2 ;;
    --output|--out)     need_val "$1" "${2-}"; OUT="$2"; shift 2 ;;
    --gate-cmd|--gate)  need_val "$1" "${2-}"; GATE_CMD="$2"; shift 2 ;;
    --allowlist-file)   need_val "$1" "${2-}"; ALLOW_FILE="$2"; shift 2 ;;
    --safety-refs-file) need_val "$1" "${2-}"; SAFETY_FILE="$2"; shift 2 ;;
    --forbidden-patterns-file) need_val "$1" "${2-}"; PATTERNS_FILE="$2"; shift 2 ;;
    --not-descendant-of) need_val "$1" "${2-}"; NOT_DESC_OF="$2"; shift 2 ;;
    --verbatim-min-bytes) need_val "$1" "${2-}"; VERBATIM_MIN="$2"; shift 2 ;;
    --no-per-commit)    PER_COMMIT=0; shift ;;
    --allow-no-gate)    ALLOW_NO_GATE=1; shift ;;
    --final)            FINAL=1; shift ;;
    -h|--help)          usage; exit 0 ;;
    *)                  usage >&2; die "unknown argument: $1" ;;
  esac
done

[ -n "$REPO" ]   || { usage >&2; die "--repo/--worktree is required"; }
[ -n "$TARGET" ] || { usage >&2; die "--target is required"; }
[ -n "$BASE" ] && [ -n "$TIP" ] || { usage >&2; die "a range is required (--base+--tip or --range lo..hi)"; }
[ -d "$REPO" ] || die "repo dir not found: $REPO"
git -C "$REPO" rev-parse --is-inside-work-tree >/dev/null 2>&1 || die "not a git checkout: $REPO"
command -v python3 >/dev/null 2>&1 || die "python3 is required (JSON emission)"
[ -n "$ALLOW_FILE" ]    && [ ! -f "$ALLOW_FILE" ]    && die "--allowlist-file not found: $ALLOW_FILE"
[ -n "$SAFETY_FILE" ]   && [ ! -f "$SAFETY_FILE" ]   && die "--safety-refs-file not found: $SAFETY_FILE"
[ -n "$PATTERNS_FILE" ] && [ ! -f "$PATTERNS_FILE" ] && die "--forbidden-patterns-file not found: $PATTERNS_FILE"
[ -n "$OUT" ] || OUT="$REPO/.rebuild/gate-report.json"

RES=$(mktemp -d "${TMPDIR:-/tmp}/rebuild-gate.XXXXXX") || die "mktemp failed"
trap 'rm -rf "$RES"' EXIT
CHECKS_TSV="$RES/checks.tsv"; : > "$CHECKS_TSV"
DETAIL_N=0

# record <name> <status>   — detail body on stdin (heredoc: no subshell, so the
# counter survives; never pipe into this function).
record() {
  DETAIL_N=$((DETAIL_N+1))
  local df="detail-$DETAIL_N.txt"
  cat > "$RES/$df"
  printf '%s\t%s\t%s\n' "$1" "$2" "$df" >> "$CHECKS_TSV"
}

# glob_match <path> <patterns-file> — 0 iff any glob line matches (POSIX case
# semantics: * crosses /, same as rebuild-plan's coverage-map globs).
glob_match() {
  local p="$1" pat
  while IFS= read -r pat; do
    [ -z "$pat" ] && continue
    # shellcheck disable=SC2254
    case "$p" in $pat) return 0 ;; esac
  done < "$2"
  return 1
}

# extract_block <tag> — stdin: markdown; stdout: the fenced block's lines with
# `# comments` and blanks stripped. Handles multiple same-tag blocks. Fence
# lines are matched after trimming trailing whitespace so a stray space after
# the tag/closer doesn't silently empty the block.
extract_block() {
  awk -v tag="$1" '
    { line = $0; sub(/[ \t]+$/, "", line) }
    line == "```" tag { inb = 1; next }
    inb && line == "```" { inb = 0; next }
    inb { print }
  ' | grep -v '^[[:space:]]*#' | grep -v '^[[:space:]]*$' || true
}

# epoch_slice — stdout: the ledger lines of this epoch's `### E<id> ...`
# section (heading token 2 == the epoch id), ending at the next ###/## heading.
epoch_slice() {
  awk -v e="$EPOCH" '
    /^### / { ins = ($2 == e) }
    /^## /  { ins = 0 }
    ins { print }
  ' "$LEDGER"
}

emit_and_exit() {
  python3 - "$RES" "$OUT" "${EPOCH:--}" "$RANGE_LABEL" <<'PY'
import json, os, sys
res, out, epoch, rng = sys.argv[1], sys.argv[2], sys.argv[3], sys.argv[4]
checks = []
with open(os.path.join(res, "checks.tsv")) as f:
    for line in f:
        name, status, dfile = line.rstrip("\n").split("\t")
        p = os.path.join(res, dfile)
        detail = ""
        if os.path.exists(p):
            with open(p, errors="replace") as d:
                detail = d.read().rstrip("\n")
        checks.append({"name": name, "status": status, "detail": detail})
ok = all(c["status"] != "fail" for c in checks)
report = {"pass": ok, "epoch": epoch, "range": rng, "checks": checks}
outdir = os.path.dirname(os.path.abspath(out))
os.makedirs(outdir, exist_ok=True)
with open(out, "w") as f:
    json.dump(report, f, indent=2)
    f.write("\n")
w = max((len(c["name"]) for c in checks), default=0)
print("rebuild-gate: epoch %s  range %s" % (epoch, rng))
for c in checks:
    first = c["detail"].splitlines()[0] if c["detail"] else ""
    print("  %-*s  %-12s %s" % (w, c["name"], c["status"].upper(), first))
print("%s  (report: %s)" % ("PASS" if ok else "FAIL", out))
sys.exit(0 if ok else 1)
PY
  exit $?
}

RANGE_LABEL="$BASE..$TIP"

# ---------------------------------------------------------------- refs -------
REFS_FAIL=""
BASE_SHA=$(git -C "$REPO" rev-parse --verify --quiet "$BASE^{commit}") \
  || REFS_FAIL="base '$BASE' does not resolve"
TIP_SHA=$(git -C "$REPO" rev-parse --verify --quiet "$TIP^{commit}") \
  || REFS_FAIL="${REFS_FAIL:+$REFS_FAIL; }tip '$TIP' does not resolve"
TARGET_SHA=$(git -C "$REPO" rev-parse --verify --quiet "$TARGET^{commit}") \
  || REFS_FAIL="${REFS_FAIL:+$REFS_FAIL; }target '$TARGET' does not resolve"
if [ -z "$REFS_FAIL" ]; then
  RANGE_LABEL="$(git -C "$REPO" rev-parse --short "$BASE_SHA")..$(git -C "$REPO" rev-parse --short "$TIP_SHA")"
  if ! git -C "$REPO" merge-base --is-ancestor "$BASE_SHA" "$TIP_SHA" 2>/dev/null; then
    REFS_FAIL="base is not an ancestor of tip"
  else
    COUNT=$(git -C "$REPO" rev-list --count "$BASE_SHA..$TIP_SHA")
    MERGES=$(git -C "$REPO" rev-list --merges --count "$BASE_SHA..$TIP_SHA")
    [ "$COUNT" -ge 1 ] || REFS_FAIL="empty range (base == tip)"
    [ "$MERGES" -eq 0 ] || REFS_FAIL="${REFS_FAIL:+$REFS_FAIL; }range contains $MERGES merge commit(s) — a linear stack is required"
  fi
fi
if [ -n "$REFS_FAIL" ]; then
  record refs fail <<EOF
$REFS_FAIL
EOF
  for c in ledger_untracked commit_shape verbatim_blobs toolchain_gate per_commit_gate forbidden_patterns safety_refs; do
    record "$c" skipped <<EOF
skipped: refs check failed — nothing downstream is meaningful
EOF
  done
  record dependency_staging not_assessed <<EOF
semantic check; owned by the judge panel, never mechanized here
EOF
  emit_and_exit
fi
HEAD_SHA=$(git -C "$REPO" rev-parse HEAD)
HEAD_NOTE="tip == worktree HEAD"
[ "$HEAD_SHA" = "$TIP_SHA" ] || HEAD_NOTE="NOTE: tip is NOT the worktree HEAD ($(git -C "$REPO" rev-parse --short "$HEAD_SHA")) — rangeBIsHead review assumptions do not hold"
record refs ok <<EOF
base/tip/target resolve; base is ancestor of tip; $COUNT commit(s), no merges; $HEAD_NOTE
EOF

# ------------------------------------------------------ ledger_untracked -----
if [ -z "$LEDGER" ]; then
  record ledger_untracked not_assessed <<EOF
no --ledger supplied
EOF
elif [ ! -f "$LEDGER" ]; then
  record ledger_untracked fail <<EOF
ledger file not found: $LEDGER
EOF
  LEDGER=""   # downstream block extraction has nothing to read
else
  REPO_TOP=$(git -C "$REPO" rev-parse --show-toplevel)
  LREL=$(python3 -c 'import os,sys;print(os.path.relpath(os.path.realpath(sys.argv[1]), os.path.realpath(sys.argv[2])))' "$LEDGER" "$REPO_TOP")
  case "$LREL" in
    ..*)
      record ledger_untracked ok <<EOF
ledger lives outside the checkout ($LEDGER) — cannot be committed here
EOF
      ;;
    *)
      PROBS=""
      git -C "$REPO" ls-files --error-unmatch -- "$LREL" >/dev/null 2>&1 \
        && PROBS="tracked in the index"
      git -C "$REPO" rev-parse --verify --quiet "$TIP_SHA:$LREL" >/dev/null 2>&1 \
        && PROBS="${PROBS:+$PROBS; }present in the tip tree"
      TOUCHED=$(git -C "$REPO" log --oneline "$BASE_SHA..$TIP_SHA" -- "$LREL" 2>/dev/null | head -5)
      [ -n "$TOUCHED" ] && PROBS="${PROBS:+$PROBS; }touched by range commits"
      if [ -n "$PROBS" ]; then
        record ledger_untracked fail <<EOF
$LREL: $PROBS — the ledger is never committed (add it to .git/info/exclude and purge it from the range)
$TOUCHED
EOF
      else
        record ledger_untracked ok <<EOF
$LREL is untracked, absent from the tip tree, untouched by the range
EOF
      fi
      ;;
  esac
fi

# ---------------------------------------------------------- commit_shape -----
# Heuristic, not semantics: reject the obvious appended-revision shapes the
# replacement-range contract forbids. Two triggers:
#   1. revision-marker subjects (fixup!/squash!/re-author/address judge|review/
#      judge feedback/…) anywhere in the range;
#   2. a `fix…` subject whose commit touches ONLY files already authored by
#      earlier commits in the same range (the add-then-fix shape).
# Exempt: "examine fixes for epoch …" — rebuild-examine's own verified-fix
# commits are legitimate appends by contract.
STRONG_RE='fixup!|squash!|re-?author|address(es|ing)? (the )?(judge|review|finding)|judge (feedback|round)|post-review fix|review feedback|per (the )?(judge|review)'
EXEMPT_RE='^examine fixes for epoch '
PRIOR="$RES/prior-files.txt"; : > "$PRIOR"
SHAPE_VIOL="$RES/shape-viol.txt"; : > "$SHAPE_VIOL"
FIRST=1
for C in $(git -C "$REPO" rev-list --reverse "$BASE_SHA..$TIP_SHA"); do
  SUBJ=$(git -C "$REPO" log -1 --format=%s "$C")
  SHORT=$(git -C "$REPO" rev-parse --short "$C")
  CUR="$RES/cur-files.txt"
  git -C "$REPO" diff-tree --no-commit-id --name-only -r "$C" > "$CUR"
  if printf '%s\n' "$SUBJ" | grep -Eq "$EXEMPT_RE"; then
    :
  elif printf '%s\n' "$SUBJ" | grep -Eiq "$STRONG_RE"; then
    printf '%s  %s  — revision-marker subject (revisions must be delivered as replacement ranges, never appended)\n' "$SHORT" "$SUBJ" >> "$SHAPE_VIOL"
  elif [ $FINAL -eq 0 ] && printf '%s\n' "$SUBJ" | grep -Eiq '^fix[:!( ]' && [ $FIRST -eq 0 ] && [ -s "$CUR" ]; then
    # add-then-fix heuristic — SKIPPED in --final mode: across an
    # already-accepted whole branch, a legitimate `fix: <target bug>` deviation
    # commit (encouraged by the floor-not-ceiling rule) that touches only
    # earlier-authored files is a false positive, not an appended revision.
    ALL_PRIOR=1
    while IFS= read -r F; do
      [ -z "$F" ] && continue
      grep -Fxq "$F" "$PRIOR" || { ALL_PRIOR=0; break; }
    done < "$CUR"
    if [ $ALL_PRIOR -eq 1 ]; then
      printf '%s  %s  — fix commit touching only files already authored earlier in this range (add-then-fix shape)\n' "$SHORT" "$SUBJ" >> "$SHAPE_VIOL"
    fi
  fi
  cat "$CUR" >> "$PRIOR"
  FIRST=0
done
# Revision-reset proof: if the caller names the rejected tip (--not-descendant-of),
# the new range must NOT descend from it — an appended revision keeps the reject
# in its ancestry, a true replacement range (reset to LO) does not. This catches
# the innocuous-subject append that the subject heuristics above miss.
if [ -n "$NOT_DESC_OF" ]; then
  ND_SHA=$(git -C "$REPO" rev-parse --verify --quiet "$NOT_DESC_OF^{commit}" || true)
  if [ -n "$ND_SHA" ] && git -C "$REPO" merge-base --is-ancestor "$ND_SHA" "$TIP_SHA" 2>/dev/null; then
    printf 'rejected tip %s is an ancestor of the new tip — the revision was appended, not reset to the epoch base\n' \
      "$(git -C "$REPO" rev-parse --short "$ND_SHA")" >> "$SHAPE_VIOL"
  fi
fi
if [ -s "$SHAPE_VIOL" ]; then
  record commit_shape fail <<EOF
appended-revision shape detected — deliver a clean replacement range (reset to the epoch base, fresh commits):
$(cat "$SHAPE_VIOL")
EOF
else
  record commit_shape ok <<EOF
$COUNT commit(s), no revision-marker subjects$([ $FINAL -eq 0 ] && echo ", no add-then-fix chain")$([ -n "$NOT_DESC_OF" ] && echo ", range reset to base (not descended from the reject)") (heuristic)
EOF
fi

# -------------------------------------------------------- verbatim_blobs -----
ALLOW="$RES/allow.txt"; : > "$ALLOW"
if [ -n "$ALLOW_FILE" ]; then
  grep -v '^[[:space:]]*#' "$ALLOW_FILE" | grep -v '^[[:space:]]*$' >> "$ALLOW" || true
fi
if [ -n "$LEDGER" ]; then
  if [ -n "$EPOCH" ]; then
    epoch_slice | extract_block pure-data-allowlist >> "$ALLOW"
  else
    extract_block pure-data-allowlist < "$LEDGER" >> "$ALLOW"
  fi
fi
CHANGED="$RES/changed.txt"
git -C "$REPO" diff --name-only "$BASE_SHA" "$TIP_SHA" > "$CHANGED"
VIO="$RES/verbatim.txt"; : > "$VIO"
COMPARED=0; IDENTICAL=0; TRIVIAL=0
while IFS= read -r P; do
  [ -z "$P" ] && continue
  TB=$(git -C "$REPO" rev-parse --verify --quiet "$TIP_SHA:$P" 2>/dev/null) || continue   # deleted at tip
  GB=$(git -C "$REPO" rev-parse --verify --quiet "$TARGET_SHA:$P" 2>/dev/null) || continue # not in target
  COMPARED=$((COMPARED+1))
  if [ "$TB" = "$GB" ]; then
    IDENTICAL=$((IDENTICAL+1))
    glob_match "$P" "$ALLOW" && continue
    # Canonical-trivial floor: a file below the byte floor has one obvious
    # spelling (`pub mod x;`, empty __init__.py, a one-line .gitignore) — there
    # is no authorship to fake, so identity is not a violation. Genuine data
    # files (LICENSE, corpora, contract JSON) belong on the allowlist instead.
    SZ=$(git -C "$REPO" cat-file -s "$TB" 2>/dev/null || echo 999999)
    if [ "$SZ" -lt "$VERBATIM_MIN" ]; then TRIVIAL=$((TRIVIAL+1)); continue; fi
    printf '%s\n' "$P" >> "$VIO"
  fi
done < "$CHANGED"
ALLOW_COUNT=$(wc -l < "$ALLOW" | tr -d ' ')
if [ -s "$VIO" ]; then
  record verbatim_blobs fail <<EOF
byte-identical to the target and NOT on the pure-data allowlist ($ALLOW_COUNT allowlist glob(s), $TRIVIAL under the ${VERBATIM_MIN}-byte trivial floor) — fresh-authorship violation:
$(cat "$VIO")
EOF
else
  record verbatim_blobs ok <<EOF
$COMPARED changed path(s) compared against target; $IDENTICAL identical — all allowlisted ($ALLOW_COUNT glob(s)) or under the ${VERBATIM_MIN}-byte canonical-trivial floor ($TRIVIAL)
EOF
fi

# ------------------------------------------- toolchain / per-commit gates ----
detect_toolchain() { # $1 = extracted tree
  if [ -f "$1/Cargo.toml" ]; then echo rust
  elif [ -f "$1/pyproject.toml" ] && { [ -f "$1/uv.lock" ] || grep -q '^\[tool\.uv\]' "$1/pyproject.toml" 2>/dev/null; }; then echo uv-python
  else echo none; fi
}

run_cmd() { # run_cmd <dir> <log> <cmd...>
  local d="$1" l="$2"; shift 2
  { printf '\n$ %s\n' "$*"; } >> "$l"
  ( cd "$d" && "$@" ) >> "$l" 2>&1
}

# run_gate_at <sha> — 0 ok / 1 fail / 2 no-toolchain. Extracts a CLEAN tree
# via `git archive` (never gates the nested worktree in place — the outer
# workspace's .claude/worktrees capture produced spurious build failures) and
# runs the full-tree gate there. Sets GATE_LOG and GATE_EXT.
run_gate_at() {
  local SHA="$1" SHORT RC=0
  SHORT=$(git -C "$REPO" rev-parse --short "$SHA")
  GATE_EXT="$RES/ext-$SHORT"; GATE_LOG="$RES/gate-$SHORT.log"
  mkdir -p "$GATE_EXT"; : > "$GATE_LOG"
  if ! git -C "$REPO" archive -o "$RES/archive.tar" "$SHA" 2>> "$GATE_LOG"; then
    echo "git archive failed for $SHORT" >> "$GATE_LOG"; return 1
  fi
  if ! tar -xf "$RES/archive.tar" -C "$GATE_EXT" 2>> "$GATE_LOG"; then
    echo "tar extraction failed for $SHORT" >> "$GATE_LOG"; return 1
  fi
  if [ -n "$GATE_CMD" ]; then
    echo "custom gate (--gate-cmd): $GATE_CMD" >> "$GATE_LOG"
    ( cd "$GATE_EXT" && sh -c "$GATE_CMD" ) >> "$GATE_LOG" 2>&1 || RC=1
    return $RC
  fi
  case "$(detect_toolchain "$GATE_EXT")" in
    rust)
      # Shared target dir: cargo fingerprints per-source, so per-commit runs
      # reuse compatible artifacts instead of cold-building every commit.
      export CARGO_TARGET_DIR="$RES/cargo-target"
      run_cmd "$GATE_EXT" "$GATE_LOG" cargo fmt --all -- --check || RC=1
      [ $RC -eq 0 ] && { run_cmd "$GATE_EXT" "$GATE_LOG" cargo clippy --all-targets --all-features -- -D warnings || RC=1; }
      [ $RC -eq 0 ] && { run_cmd "$GATE_EXT" "$GATE_LOG" cargo test --quiet || RC=1; }
      if [ $RC -eq 0 ] && [ -f "$GATE_EXT/deny.toml" ]; then
        if command -v cargo-deny >/dev/null 2>&1; then
          # bans/licenses/sources ONLY — never advisories (or bare `check`,
          # which includes them): a fresh RustSec advisory is environmental
          # and must not fail a gate with zero code change (project policy).
          run_cmd "$GATE_EXT" "$GATE_LOG" cargo deny check bans licenses sources || RC=1
        else
          echo "note: deny.toml present but cargo-deny not installed — deny step NOT run" >> "$GATE_LOG"
        fi
      fi
      ;;
    uv-python)
      run_cmd "$GATE_EXT" "$GATE_LOG" uv lock --check || RC=1
      [ $RC -eq 0 ] && { run_cmd "$GATE_EXT" "$GATE_LOG" uv run ruff check || RC=1; }
      [ $RC -eq 0 ] && { run_cmd "$GATE_EXT" "$GATE_LOG" uv run ruff format --check || RC=1; }
      [ $RC -eq 0 ] && { run_cmd "$GATE_EXT" "$GATE_LOG" uv run basedpyright || RC=1; }
      [ $RC -eq 0 ] && { run_cmd "$GATE_EXT" "$GATE_LOG" uv run pytest -q -x || RC=1; }
      ;;
    none)
      return 2
      ;;
  esac
  return $RC
}

# Gate-command resolution: an explicit --gate-cmd wins; otherwise fall back to
# the ledger's `gate-commands` fenced block (the orchestrator records its --gate
# override there, and rebuild-plan authors a project default). Multiple lines are
# chained with && so the whole sequence must pass.
if [ -z "$GATE_CMD" ] && [ -n "$LEDGER" ]; then
  GC=$(extract_block gate-commands < "$LEDGER" | awk 'NR>1{printf " && "} {printf "%s",$0} END{if(NR)print ""}')
  [ -n "$GC" ] && GATE_CMD="$GC"
fi

TIP_SHORT=$(git -C "$REPO" rev-parse --short "$TIP_SHA")
run_gate_at "$TIP_SHA"; TIP_RC=$?
TIP_LOG="$GATE_LOG"
case $TIP_RC in
  0)
    record toolchain_gate ok <<EOF
full-tree gate green at tip $TIP_SHORT (clean archive extraction)
$(tail -n 15 "$TIP_LOG")
EOF
    ;;
  1)
    record toolchain_gate fail <<EOF
full-tree gate FAILED at tip $TIP_SHORT (clean archive extraction):
$(tail -n 150 "$TIP_LOG")
EOF
    ;;
  2)
    # No recognized toolchain AND no gate command from any source. Nothing was
    # verified — that is a FAIL by default (a green report with zero build/test
    # coverage is exactly the hole a self-attested gate leaves). Only an explicit
    # --allow-no-gate waiver downgrades it to a loud skip, for genuinely
    # toolchain-less trees (e.g. a static-content epoch).
    if [ $ALLOW_NO_GATE -eq 1 ]; then
      record toolchain_gate skipped <<EOF
NO RECOGNIZED TOOLCHAIN and no gate command — waived via --allow-no-gate.
NOTHING WAS GATED; this is absence of data, not verification. Waiver was explicit.
EOF
    else
      record toolchain_gate fail <<EOF
NO RECOGNIZED TOOLCHAIN at the tip (no Cargo.toml, no uv pyproject.toml), no --gate-cmd,
and no ledger gate-commands block — NOTHING WOULD BE GATED. Failing closed: supply
--gate-cmd, author a gate-commands block in the ledger, or pass --allow-no-gate to
waive deliberately for a genuinely toolchain-less tree.
EOF
    fi
    ;;
esac

if [ $PER_COMMIT -eq 0 ]; then
  record per_commit_gate skipped <<EOF
disabled via --no-per-commit (tip-only run)
EOF
elif [ $TIP_RC -eq 2 ]; then
  record per_commit_gate skipped <<EOF
skipped: no recognized toolchain (see toolchain_gate)
EOF
elif [ $TIP_RC -eq 1 ]; then
  record per_commit_gate skipped <<EOF
skipped: the tip gate already failed — fix that first (per-commit runs would repeat the noise)
EOF
elif [ "$COUNT" -le 1 ]; then
  record per_commit_gate ok <<EOF
single-commit range — the tip gate covers it
EOF
else
  PC_FAIL=""
  PC_N=0
  for C in $(git -C "$REPO" rev-list --reverse "$BASE_SHA..$TIP_SHA" | sed '$d'); do
    PC_N=$((PC_N+1))
    run_gate_at "$C"; RC=$?
    if [ $RC -ne 0 ]; then
      PC_FAIL="$C"
      PC_FAIL_LOG="$GATE_LOG"
      break
    fi
  done
  if [ -n "$PC_FAIL" ]; then
    PC_SHORT=$(git -C "$REPO" rev-parse --short "$PC_FAIL")
    PC_SUBJ=$(git -C "$REPO" log -1 --format=%s "$PC_FAIL")
    record per_commit_gate fail <<EOF
commit $PC_SHORT ("$PC_SUBJ") fails the full-tree gate from a clean extraction — the range is not per-commit buildable. Remaining earlier-range commits not checked.
$(tail -n 150 "$PC_FAIL_LOG")
EOF
  else
    record per_commit_gate ok <<EOF
$PC_N non-tip commit(s) each gate-green from a clean archive extraction
EOF
  fi
fi

# ---------------------------------------------------- forbidden_patterns -----
PATS="$RES/patterns.txt"; : > "$PATS"
if [ -n "$PATTERNS_FILE" ]; then
  grep -v '^[[:space:]]*#' "$PATTERNS_FILE" | grep -v '^[[:space:]]*$' >> "$PATS" || true
fi
if [ -n "$LEDGER" ]; then
  extract_block prose-sweep-patterns < "$LEDGER" >> "$PATS"
fi
if [ ! -s "$PATS" ]; then
  record forbidden_patterns not_assessed <<EOF
no pattern source supplied — pass --forbidden-patterns-file or author a prose-sweep-patterns fenced block in the ledger.
Forward-reference/prose semantics beyond mechanical pattern scanning are the judge panel's job; this script does not fake a universal parser.
EOF
else
  # Scan changed TEXT files (numstat "-" = binary) as they exist at the tip,
  # from a clean extraction (reuse the tip gate's if it exists).
  SCAN_DIR="$RES/ext-$TIP_SHORT"
  if [ ! -d "$SCAN_DIR" ]; then
    mkdir -p "$SCAN_DIR"
    if ! git -C "$REPO" archive -o "$RES/archive.tar" "$TIP_SHA" \
       || ! tar -xf "$RES/archive.tar" -C "$SCAN_DIR"; then
      die "archive extraction for pattern scan failed"
    fi
  fi
  TEXTFILES="$RES/textfiles.txt"
  git -C "$REPO" diff --numstat --no-renames "$BASE_SHA" "$TIP_SHA" \
    | awk -F'\t' '$1 != "-" { print $3 }' > "$TEXTFILES"
  HITS="$RES/pattern-hits.txt"; : > "$HITS"
  NPAT=0
  while IFS= read -r PAT; do
    [ -z "$PAT" ] && continue
    NPAT=$((NPAT+1))
    while IFS= read -r F; do
      [ -z "$F" ] && continue
      [ -f "$SCAN_DIR/$F" ] || continue
      # Never interpolate $PAT into sed — an ERE alternation like `CI (runs|asserts)`
      # contains `|`, which breaks a `|`-delimited sed script, discards the hits,
      # and `|| true`-swallows the error into a false "no hits". Format with printf
      # in a read loop, where $PAT is inert data.
      grep -Ein -e "$PAT" -- "$SCAN_DIR/$F" 2>/dev/null \
        | while IFS= read -r LINE; do
            printf '%s:%s   [pattern: %s]\n' "$F" "$LINE" "$PAT" >> "$HITS"
          done
    done < "$TEXTFILES"
  done < "$PATS"
  NFILES=$(wc -l < "$TEXTFILES" | tr -d ' ')
  if [ -s "$HITS" ]; then
    NHITS=$(wc -l < "$HITS" | tr -d ' ')
    record forbidden_patterns fail <<EOF
$NHITS hit(s) for the supplied forbidden patterns across the range's changed text files (case-insensitive ERE):
$(head -n 200 "$HITS")
EOF
  else
    record forbidden_patterns ok <<EOF
$NPAT pattern(s) scanned over $NFILES changed text file(s) at the tip — no hits
EOF
  fi
fi

# ----------------------------------------------------------- safety_refs -----
DECL="$RES/declared-refs.txt"; : > "$DECL"
if [ -n "$SAFETY_FILE" ]; then
  grep -v '^[[:space:]]*#' "$SAFETY_FILE" | grep -v '^[[:space:]]*$' | awk '{print $1}' >> "$DECL" || true
fi
if [ -n "$LEDGER" ]; then
  extract_block safety-refs < "$LEDGER" | awk '{print $1}' >> "$DECL"
fi
EXISTING="$RES/existing-refs.txt"
git -C "$REPO" for-each-ref --format='%(refname:short)' 'refs/tags/safety/*' 'refs/heads/safety/*' > "$EXISTING"
UNDECL="$RES/undeclared-refs.txt"; : > "$UNDECL"
while IFS= read -r R; do
  [ -z "$R" ] && continue
  grep -Fxq "$R" "$DECL" || printf '%s\n' "$R" >> "$UNDECL"
done < "$EXISTING"
NEXIST=$(wc -l < "$EXISTING" | tr -d ' ')
NDECL=$(wc -l < "$DECL" | tr -d ' ')
if [ -s "$UNDECL" ]; then
  record safety_refs fail <<EOF
undeclared safety/* ref(s) exist — every safety ref must be declared (registry lifecycle: created before a rewrite, deleted on ACCEPT):
$(cat "$UNDECL")
Declared sources checked: --safety-refs-file${SAFETY_FILE:+ ($SAFETY_FILE)}, ledger safety-refs block ($NDECL declared).
EOF
else
  record safety_refs ok <<EOF
$NEXIST existing safety/* ref(s), all declared ($NDECL declaration line(s))
EOF
fi

# ----------------------------------------------------- dependency_staging ----
record dependency_staging not_assessed <<EOF
unused-dependency staging (no workspace dep unused by every crate/module existing as of this epoch) is a semantic judgment — owned by the judge panel, not mechanized here
EOF

emit_and_exit
