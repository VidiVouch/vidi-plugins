# Astra standard review

Direct `vidi-skills:astra-reviewer` agent, not a wrapper and not the Codex native
harness. Policy and proxy catalog checks select the lane; CLI presence does not.

## Launch prompt

Read the diff and relevant surrounding code in the supplied review root. Apply
the common scope and output contract in the workflow prompt. Check changed
behavior for incorrect results, broken caller contracts, error handling,
resource ownership, and security regressions. Read applicable project rules and
REVIEW.md; respect documented exclusions. Include input-dependent bugs when a
concrete reachable input demonstrates them. Do not report taste preferences or
pre-existing defects unrelated to the change.

Ground each finding in a file/line range and a failure scenario. Check nearby
guards and callers before asserting the defect. Return the shared structured
object, including an empty findings array when the completed review is clean.
Use degraded=true when the scope cannot be inspected or the review cannot finish.
