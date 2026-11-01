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
finding was judged with `round`, `ruling`, `severity` and the boss `reason`
(under `--panel`, the three judges' reasons joined, each prefixed with the
judge's name).

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
  was unavailable or returned an invalid result. Under `--panel` it also covers
  a candidate whose judges' evidence conflicts (one confirmed it and another
  refuted it, which no count resolves), one that no judge demonstrated or
  refuted, or one a judge still missing after its one fresh replacement left
  undecided (unless the request authorized a two-judge ruling, judge-contract.md
  "When a judge slot fails"); the finding then carries `dispositions`, what each
  judge said, with `missing` for a judge that returned nothing usable. The run
  is already incomplete and the finding is neither confirmed nor cleared.
- `degraded`: a fixer was dispatched for it and nothing verified the result:
  the loop hit its round limit before a re-review, the fixer failed and its
  partial edits were not re-reviewed, a lane that reported the finding did not
  complete the re-review, or the fixer rebutted it and the run ended before a
  round ruled on the objection. An unverified fix is not resolved and not
  outstanding; it is unknown, and the report says so.

The latest ruling decides. A claim refuted in round 1 and confirmed in round 2
is open or fixed by round 2's history; a finding confirmed in round 1, fixed,
and re-detected in round 2 starts again from round 2, and its reason says it
was re-detected after a fix.

## Rebuttals

A fixer may rebut a confirmed finding instead of fixing it: the fix packet's
`rebuttals` names the finding's id with a reason and evidence, once per finding
per run. The next round judges that finding again with the objection attached
as `rebuttal`, whether or not a reviewer re-detected it, so the claim gets a
ruling from fresh judges rather than a second fixer. The finding then carries
`rebuttal` in this record and its `outcomeReason` says whether the judges
upheld the objection (`refuted`) or confirmed the finding over it, in which
case the fixer that receives it next sees the prior rebuttal and the ruling and
may not rebut again. A false claim therefore ends after one fix step, with the
judges' answer kept, instead of being sent to a fixer every round. A rebuttal
the run ended on, with no round to rule on it, leaves the finding `degraded`.

## Escalations

Fixers are grouped by the file a finding is anchored in, and each owns a
disjoint set of files. A finding is sometimes reported where it shows (a test,
a document, a caller) while its cause is in a file another fixer owns or no
fixer owns this round. The fixer that sees this does not edit outside its
bucket and does not patch around the defect in its own files: the fix packet's
`escalations` names the finding's id, the one file holding the cause, a reason
and evidence. The controller then dispatches, in the same round, one fresh
fixer owning that file for the escalated findings, after every fixer of the
first stage has returned. Only the cause file is routed; files the evidence
cites stay where they are, and no other bucket or reviewer is rerun. From then
on the finding is grouped under the cause file, so a re-detection in a later
round goes straight to that file's owner. An escalation is made once per
finding per run: the fixer that receives a routed finding sees `escalatedFrom`
(the anchor file, round, reason and evidence) and fixes it or leaves it
unresolved, so a finding cannot be passed around until the round cap. A packet
that escalates a finding to a file inside its own bucket, with no evidence, or a
second time is a failed fixer and stops the loop like any other failed packet.
The finding carries `escalation` in this record and its `outcomeReason` names
the file the repair was routed to.

## Identity

A finding is the same finding across rounds when `dedupKey` matches: file,
line range and the first 80 characters of the merged description, the same key
the loop's `seen` set uses. A fix that moves lines or a re-detection worded
differently appears as a new open finding beside the old one marked fixed. The
judging panel (judging.md) derives each candidate's id from that same key
before the judges run, so under `--panel` a finding's `id` and this record's
identity name the same thing.

## Reading it

`run_status.py status --format note` projects the record into the section
"Findings by final disposition", one row per finding with its outcome, rounds
and reason, open and degraded first. A result with no `disposition` key, from an
older controller, is reported as having no recorded disposition; the helper
never rebuilds one from `confirmed`. The Phase 4 output in SKILL.md lists open
findings by severity as the work to do, then the fixed, refuted, unruled and
degraded groups with their reasons, so a reader sees what was fixed in the run
without mistaking it for what remains.
