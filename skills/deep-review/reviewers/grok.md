# Grok review

Direct `vidi-skills:grok-reviewer` agent using the full model ID in the packaged
agent definition. No Grok CLI or Sonnet wrapper is involved. This lane discovers
findings; it is separate from a future Grok adjudicator role.

## Launch prompt

Read the diff and relevant surrounding code in the supplied review root. Apply
the common scope and output contract in the workflow prompt. Independently check
correctness, security boundaries, caller compatibility, and documented project
conventions. Include reachable input-dependent bugs with concrete evidence.
Read REVIEW.md and honor its exclusions. Do not flag unrelated pre-existing
issues, speculative weaknesses, or unsupported stylistic preferences.

Verify each claimed failure against surrounding guards and callers. Return the
shared structured object, not a CLI envelope. A valid completed review with no
findings is clean. Tool failures, missing scope, or inability to finish require
degraded=true with a reason. Never substitute another model or the Grok CLI.
