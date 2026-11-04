---
name: design-loop
description: Improve or compare UI designs through a bounded build, capture and independent visual-review cycle. Use when the user asks for a design council, iterative UI polish, blind design judging, or comparison of interface variants. Keeps acceptance targets and earlier scores out of fresh Fable, Astra and Grok judge contexts. Complements frontend-design; does not replace runtime accessibility testing or code review.
argument-hint: "<UI or screenshot set> [--review-only] [--max-rounds N]"
---

# Design loop

Use a fresh three-model panel to assess frozen screenshots against the product's
actual tasks. Keep the acceptance decision with the coordinator. A rating is
feedback, not proof that the interface works.

The bundled Workflow runs **one review round**. The coordinator owns the bounded
build, verify, review and adjudicate cycle below. Keeping edits outside the panel
barrier lets each project use its own build and browser tools without giving those
tools or the coordinator's private state to a visual judge.

## Scope and readiness

1. Establish the audience, primary tasks, required content, brand constraints and
   permitted edit scope from the request and project. Preserve useful character
   and density. Use `frontend-design` for substantive design work when available.
2. Default to at most three reviewed snapshots, including the initial review.
   `--max-rounds N` changes that bound; `--review-only` permits one round and no UI
   edits. Record any target score privately, with the acceptance policy and budget.
   Do not invent a universal target or promise to keep trying until it is reached.
3. This skill uses three named judge agents per round, with at most one fresh
   replacement per failed seat. Tell the user the scope before dispatch. An explicit
   invocation authorizes the described Workflow, not deployment, publication,
   account changes or unrestricted edits. Confirm permission before sending
   sensitive screenshots to a provider not already authorized for that material.
4. Read [setup and packet format](references/setup.md). Verify that the loaded
   plugin contains `design-judge-fable`, `design-judge-astra` and
   `design-judge-grok`. Dispatch by agent type without a per-call model override.
   Source files and a model catalog are not evidence of working image support.
   On a new route, complete the separately scoped image/JSON smoke test first.
   Do not install a plugin or change model/permission settings to unblock a run.
5. Use the existing `codex-computer-use` skill for browser evidence. If capture is
   unavailable, stop and report what is missing. Static source inspection cannot
   substitute for viewing an interface.

## Keep two records

The coordinator's private record holds source paths, candidate authorship and
revision identities, the candidate to improve, round count, targets, budgets,
earlier judgments, runtime results and final decisions. Keep it outside the
public evidence directory. Never send the original request or this skill to a
judge: either can reveal the acceptance target.

The public packet contains only the task brief, frozen rubric and screenshots
under neutral IDs. `scripts/prepare.py` constructs it by an allowlist, copies
images byte-for-byte to a new neutral directory and hashes them. The helper does
not decide whether free text or pixels leak a target. Inspect the brief, UI text,
filenames, image metadata, injected project instructions and task labels before
dispatch. If they leak previous results or acceptance policy, prepare a clean
packet; if that cannot be done, report the review as non-blind.

The agents have only `Read`, but that does not enforce a filesystem boundary.
This is procedural blinding, not a security sandbox. A hard confidentiality
requirement needs an isolated image/text-only execution surface instead.

## Run a round

1. Use one writer for the candidate. Before any edits, preserve the current valid
   state using the project's non-destructive snapshot mechanism. Do not commit,
   stash, reset or overwrite unrelated work without authorization. Give the builder
   task requirements and concrete findings, not a score to achieve. Start fresh
   builder context for a new design task.
2. Freeze the candidate and reference. Capture matching task states, viewport,
   theme and fixture identifiers; keep capture quality comparable. Retain an
   unchanged reference across rounds and record its image hashes. Freeze code and
   data as well: a HEAD SHA alone does not identify a dirty working tree. Record
   a tree/diff identity and data fixture revision in the private capture manifest.
   Stop all candidate writers before capturing and keep them stopped through judging.
3. Verify runtime behavior separately: primary task, keyboard navigation, focus,
   responsive states and applicable accessibility checks. Record failures and
   untested behavior. A screenshot cannot prove semantics, interaction or measured
   contrast. Runtime evidence must name the same candidate revision as the captures.
4. Prepare an input file using [setup.md](references/setup.md), then run:

   ```sh
   uv run python /absolute/plugin/skills/design-loop/scripts/prepare.py prepare \
     /absolute/private/input.json --out /absolute/private/prepared.json
   ```

   The helper refuses existing output files. Read `prepared.json`; its `workflow`
   member contains the exact Workflow arguments. It also holds the private
   provenance map. Do not pass the entire prepared file to a judge.
5. Read `design-loop.workflow.js` and the `workflow-authoring` reference. Call
   `Workflow` with the bundled script's text as `script` and the prepared `workflow`
   object as `args`. The script dispatches fresh named definitions, with a shared
   prompt and schema, and waits for all three seats. Never resume an old critic
   conversation. Workflow resume may recover the same frozen round; it must not
   supply a judgment for a new packet.
6. Save the returned JSON privately, then verify the evidence again:

   ```sh
   uv run python /absolute/plugin/skills/design-loop/scripts/prepare.py verify \
     /absolute/private/prepared.json
   ```

   A hash mismatch, changed revision, contaminated judgment, unreadable image or
   missing required seat leaves the round incomplete. Do not drop failed results,
   substitute a different model or treat two judges as the full panel. A reduced
   panel needs a separately agreed protocol; this workflow does not implement it.

## Adjudicate and stop

Read the reasons and cited evidence, not just the scores. Confirm material findings
against the frozen UI. A demonstrated blocker cannot be averaged away. Preserve
useful strengths, allow ties, and separate taste differences from task failures.
Conflicting evidence stays unresolved until checked. Record each finding's
accepted, rejected-with-evidence or unresolved disposition privately.

The workflow reports coverage, target diagnostics, disagreement and the remaining
round allowance. It never returns an automatic approval. Declare the candidate
accepted only when the required panel is complete and uncontaminated, every
material finding is disposed of, the agreed acceptance policy is met, the evidence
is unchanged, and required runtime checks pass on that revision. Report uncertain
or unverified properties rather than silently waiving them.

Continue only when a specific, in-scope repair is supported by evidence and the
round/budget allowance permits another complete capture and review. Keep the
strongest valid candidate, not automatically the latest. Stop on a plateau,
repeated unresolved disagreement, missing capability, exhausted budget or the
round limit. If a fixed reference's rating drifts, report the instability rather
than interpreting every score increase as improvement. Do not edit after the last
review and describe the resulting unreviewed state as accepted. A failed transport
retry is not a design iteration, and a low score is not a reason to retry a judge.

## Deliver

Use `html-report` for a local comparison with the candidate and reference,
findings and dispositions, runtime evidence, model-routing evidence, limits and
stop reason. Keep target-bearing report annotations out of future judge packets.
Include human feedback controls. Use `humanizer` when available for the report's
prose, preserving evidence, code and quoted judgments. Keep machine records as
JSON and agent instructions as Markdown.

Record requested, harness-resolved and upstream model identities separately.
Leave unavailable identities unknown; a model's self-description proves nothing.
Do not claim a three-provider test succeeded merely because static tests passed.

`evals/evals.json` contains proposed pilot cases. They are not recorded results.
Run the fixture smoke test before an authorized pilot, then use `deep-eval` or
`skill-creator` for an explicitly scoped with/without-skill comparison.
