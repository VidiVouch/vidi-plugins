# Judging result contract

A judging result is what one judge returns after examining the candidate
findings of a review round against the frozen snapshot. The review panel
produces candidates; a judge turns each candidate into one of four
dispositions and shows its work in the same record, so a lead, a merge step, a
fixer or a later verifier can follow every disposition to a line, a command or
a document without reading the judge's transcript. The shape is
`skills/deep-review/judging-output.schema.json`; `scripts/judging.py check`
refuses a result that breaks the rules below. The same shape serves a single
boss and each member of a judging panel, so merging several judges is a merge
of identical records, not a translation between formats.

The deep-review workflow's boss stage, the default without `--panel`, runs one
boss and returns `rulings` with `real` and `severity`; a request naming more
than one boss is refused, because the retired two-model domain merge was
neither one arbiter nor a panel. Read the rulings through this contract:
`real=true` is confirmed without the evidence this schema demands, `real=false`
is refuted, and an `UNRULED` finding is unreviewed. With `--panel` the workflow
dispatches the three judges of judge-contract.md on one identical brief, each
returns this shape, and the result keeps the candidates of every round beside
each judge's checked result or error under `judgeResults`. The three records
are merged by their evidence, never by count (judge-contract.md, "Merging and
rebuttals"): confirmations with no refutation confirm, refutations with no
confirmation refute, a confirmation beside a refutation is a conflict that
leaves the candidate unruled and open, and an unresolved or unreviewed entry is
recorded but is not a verdict. Nothing about the candidates, the dedup step or
the fix lane changes between the two modes, except that a candidate the
previous round's fixer rebutted carries that objection as `rebuttal` in both.

## Top level

Every field is required and no other field is allowed.

- `input`: `snapshotId`, the 64-hex snapshot identity from the run manifest,
  and `round`, the review round whose candidates were judged. A judge echoes
  what it was given, and the checker's `--snapshot` refuses a result that
  names a different tree, so a result cannot be reused against code it never
  examined.
- `findings`: one entry per candidate, no more and no fewer. A candidate the
  judge did not get to is an `unreviewed` entry, not a missing one; a defect
  the judge noticed that no candidate describes is not an entry at all, it is
  a candidate for the next review round. With `--candidates`, the checker
  refuses a result that drops or invents an id.
- `limits`: what the judge as a whole could not do, one line each, for example
  "static reading only, no command was run" or "no access to the fixture
  files under tests/data". Empty means the judge declares no result-wide
  limit; per-finding limits still apply.
- `summary`: one or two sentences, at most 500 characters.

## One finding

- `id`: the candidate's identity, assigned by the orchestrator before any
  judge runs and echoed unchanged. The orchestrator derives it from the
  candidate's content (file, anchor and merged description), never from its
  position in a list, so two judges, a resumed run and a later fixer all name
  the same thing by the same id. 1 to 64 characters of letters, digits, `.`,
  `_`, `:` or `-`, unique within the result. The deep-review workflow derives
  it from the same key disposition.md uses for identity across rounds (file,
  line range and the first 80 characters of the merged description) with two
  32-bit FNV-1a passes over that string, written `c-` plus 16 hex digits; the
  Workflow runtime has no sha256, and a collision within a round is refused
  rather than resolved by position.
- `file`, `line_start`, `line_end`: the candidate's anchor, repo-relative, and
  also unchanged. A judge that wants to point at narrower lines cites them as
  code evidence; moving the anchor would make the entry a different finding
  under the old id, and the checker refuses it.
- `severity`: `critical`, `warning` or `nit`. For a confirmed finding this is
  the judge's rating; for the other three it is the claimed severity, kept so
  a report can say that a claimed critical was refuted or is still unresolved.
- `disposition`: exactly one of the four words below.
- `scenario`: the concrete trigger, inputs and state in, wrong output or crash
  out. For a refuted finding it is the scenario the claim implies, stated so
  the evidence can show why it does not occur.
- `evidence`: a list of items, each with a `kind`, a `ref` and a `note` on
  what the reference shows. `code` cites `path:line` or `path:start-end` in
  the judged tree; with `--repo` the checker confirms the file exists there
  and the range lies inside it. `command` cites the exact command the judge
  ran, with its outcome in the note; the checker cannot rerun it, so a
  command item is the judge's claim until a host-side gate measures it.
  `document` cites a file or section such as `REVIEW.md#skip` or a
  dependency's note.
- `impact`: who is affected and how if the defect ships, the thing that
  justifies the severity.
- `reason`: one or two sentences on why this disposition and not another.
- `limits`: what this entry's examination did not cover, one line each.

## The four dispositions

They are distinct answers to two questions: did the judge examine the
candidate, and what did the examination show. The checker enforces what each
one must carry; a result that says confirmed without evidence is refused, not
downgraded, because silent downgrading is how a report ends up claiming more
than its record supports.

- `confirmed`: examined, and the scenario was reproduced or traced against the
  snapshot. Needs scenario, at least one evidence item, impact and reason.
  This is the only disposition a fixer acts on.
- `refuted`: examined, and the claimed scenario cannot occur in the snapshot,
  or is excluded by REVIEW.md. Needs scenario, at least one evidence item
  showing why, and reason. A refuted finding is history with a cause, which
  is what lets a false claim die in one round instead of being re-detected.
- `unresolved`: examined, and the judge could not decide: the scenario needs a
  runtime it did not have, the evidence conflicts, or the claim depends on a
  fact outside the tree. Needs scenario, impact if real, reason, and at least
  one limit saying what was missing and what would decide it. Unresolved is
  not confirmed with lower confidence and not refuted by default; it stays
  open until someone with the missing thing resolves it.
- `unreviewed`: not examined: the judge was cancelled, ran out of budget or
  the candidate lies outside the domain it was asked to rule. Needs at least
  one limit saying why, and must cite no evidence, since evidence means an
  examination happened. A missing judge result is not a set of unreviewed
  entries either; the orchestrator records that no result arrived, and may
  write unreviewed entries in its own merged record with the failure as the
  limit, so that absence is never read as silence on the merits.

## Checking

```bash
python3 "$SCRIPTS/judging.py" check --result "$RUN_DIR/judge-1-round-1.json" \
  --candidates "$RUN_DIR/candidates-round-1.json" \
  --snapshot "$SNAPSHOT_ID" --repo "$REVIEW_ROOT"
```

`--candidates` is the JSON array the judges were given, each object carrying
at least `id`, `file`, `line_start` and `line_end`. `--repo` is the judged
tree, normally the frozen worktree. Both are optional; without them the
checker verifies shape and internal consistency only. A passing check prints
the per-disposition counts, which is what a merge step or a report needs
first.

## Why one shape

A boss ruling that only says real or not real leaves the next reader to guess
whether the judge looked, what it saw, and how far it trusted itself. Putting
scenario, evidence, impact, disposition and limits beside each id makes a
disagreement between judges a comparison of evidence rather than a vote, lets
an implementer rebut one specific entry, and lets a final report separate what
was fixed, refuted, still open and never looked at. The checker exists so that
those fields hold evidence and not placeholders; `scripts/hand_back.py` plays
the same role for a worker's hand-back (hand-back.md).
