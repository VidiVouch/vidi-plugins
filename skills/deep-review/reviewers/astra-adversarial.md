# Astra adversarial review

Direct `vidi-skills:astra-adversarial-reviewer` agent in a fresh context, not the
Codex companion. It shares the standard review's snapshot and result contract,
but uses a different review stance. Two Astra roles are not two different models.

## Launch prompt

Read the diff and relevant surrounding code in the supplied review root. Apply
the common scope and output contract in the workflow prompt. Try to disprove the
change's assumptions: boundary inputs, partial failure, retries, concurrent
operations, ordering, tenant/authorization boundaries, cleanup and recovery.
Prioritize expensive failure paths. Read applicable project rules and REVIEW.md.

For each suspected defect, identify a concrete triggering state and trace it
through the code. Actively look for guards or invariants that refute it. Keep
only supported findings; do not invent issues, repeat generic checklists, or
require a fix just to be adversarial. Do not read other reviewers' conclusions.
Return the shared structured object; a completed clean review is valid. Missing
evidence or an interrupted review is degraded, not clean.
