# Final disposition of findings

The deep-review workflow returns two lists about findings and one record about
where they stand. `currentFindings` is what the last review round confirmed or
could not rule. `confirmed` is the union of everything any round confirmed, in
the order it first appeared. Neither says whether a finding from round 1 was
fixed in round 1 and cleared by round 2, whether the boss ruled a claim noise,
or whether a fix was applied and then never looked at again. `disposition`
answers that for every finding the run ever judged, and a report reads it
instead of `confirmed`, because presenting the union as the current state makes
every earlier finding look either still outstanding or quietly resolved.

## The record

`result.disposition` holds five lists. A finding appears in exactly one, sorted
severity-first, carrying the finding fields, `outcome` (the list it is in),
`outcomeReason` (one line saying why) and `rounds`, one entry per round the
finding was judged with `round`, `ruling`, `severity` and the boss `reason`.

- `open`: the boss confirmed it in the last round it appeared and no fixer was
  sent for it. That happens in a report-only run, for a nit (nits are never
  sent to fixers), or when the loop stopped before its fix step. It is a known
  defect that still needs work.
- `fixed`: the boss confirmed it, a fixer was dispatched for it, and the next
  review round did not detect it again while every lane that had reported it
  completed that round. A lane that did not complete cannot clear what it
  found, so that case is degraded, not fixed.
- `refuted`: the boss's latest ruling was not real. The claim is history with
  a cause and the reason is kept, which is what lets a false claim die in one
  round instead of being re-detected as new.
- `unruled`: no boss ruled it in the last round it appeared, because the boss
  was unavailable or returned an invalid result. The run is already incomplete
  and the finding is neither confirmed nor cleared.
- `degraded`: a fixer was dispatched for it and nothing verified the result:
  the loop hit its round limit before a re-review, the fixer failed and its
  partial edits were not re-reviewed, or a lane that reported the finding did
  not complete the re-review. An unverified fix is not resolved and not
  outstanding; it is unknown, and the report says so.

The latest ruling decides. A claim refuted in round 1 and confirmed in round 2
is open or fixed by round 2's history; a finding confirmed in round 1, fixed,
and re-detected in round 2 starts again from round 2, and its reason says it
was re-detected after a fix.

## Identity

A finding is the same finding across rounds when `dedupKey` matches: file,
line range and the first 80 characters of the merged description, the same key
the loop's `seen` set uses. A fix that moves lines or a re-detection worded
differently appears as a new open finding beside the old one marked fixed. The
judging panel (judging.md) assigns content-derived ids before judges run; when
it replaces the boss stage the same ids should key this record.

## Reading it

`run_status.py status --format note` projects the record into the section
"Findings by final disposition", one row per finding with its outcome, rounds
and reason, open and degraded first. A result with no `disposition` key, from an
older controller, is reported as having no recorded disposition; the helper
never rebuilds one from `confirmed`. The Phase 4 output in SKILL.md lists open
findings by severity as the work to do, then the fixed, refuted, unruled and
degraded groups with their reasons, so a reader sees what was fixed in the run
without mistaking it for what remains.
