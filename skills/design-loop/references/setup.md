# Setup and packet format

## Load the definitions

The plugin contains these visual roles. They evaluate screenshots rather than
code-review findings.

| Agent type | Requested model |
| --- | --- |
| `vidi-skills:design-judge-fable` | `claude-fable-5-1[1m]` |
| `vidi-skills:design-judge-astra` | `gpt-6-astra[1m]` |
| `vidi-skills:design-judge-grok` | `grok-4.7` |

These definitions pin full model IDs. `[1m]` is an extended-context marker; Grok
does not use it. Definition files select models;
`agent()` calls must omit `model`. Aliases such as `opus` or `sonnet` can be remapped
by the host and are not substitutes for these pins.

Global copies under `~/.claude/agents` are unnecessary. An already installed plugin
may be older than the source checkout. For an explicitly approved development
session, load a clean plugin checkout with `claude --plugin-dir /absolute/plugin`.
If the checkout contains unrelated unfinished changes, use a small development
plugin containing only this skill and its three definitions, with plugin name
`vidi-skills`. Do not load two versions of the same plugin in that session. Confirm
the three types appear in the session registry before dispatch. Editing source
does not install, activate or publish it.

This workflow requires Claude Code's `Workflow`, `agentType`, image-capable `Read`
and structured output. Installing the skill in Codex alone does not supply those
APIs. If the host lacks them, report the unsupported execution surface; do not
quietly replace the panel with CLI wrappers or a single judge.

The preparation helper checks source model/tool declarations and refuses an active
or unrecognized `CLAUDE_CODE_SUBAGENT_MODEL_FORCE` value. It does not query the
network, change settings, check subscription entitlements or inspect the live
agent registry. `agents_loaded`, `route_ready` and `blindness_checked` are explicit
coordinator attestations, not mechanically proven facts. Do not set them merely
to satisfy validation.

## First live smoke test

Agree on a fixture-only run before testing a new route. It costs three judgments,
plus at most one replacement for each failed transport/structured result.

1. Capture a benign fixture with plainly visible text, color and geometry. Include
   a second variant with one known visible task impediment. Keep expected answers
   in a private record; do not put them in the public brief. Use ordinary task
   requirements so the fixture tests observation rather than instruction-following.
2. Prepare with `purpose: "smoke"`, `round: 1`, `max_rounds: 1`, `agents_loaded: true`
   and `blindness_checked: true`. `route_ready` may be false for this mode. Dispatch
   the bundled one-round workflow without editing the fixture.
3. Check that every judge actually opened each image, described the visible details,
   returned valid JSON and used real evidence IDs. A fabricated image description
   or an empty result is a failure even if the transport completed. Preserve all
   attempts; do not repeat valid judgments until they agree with expected answers.
4. Record requested model IDs from the definitions. Record resolved/upstream IDs
   only if the harness or trusted routing logs expose them. An advertised model
   catalog or an agent's claim about itself is insufficient. Unknown identities
   remain unknown and belong in the smoke-test limits.
5. Verify hashes, record the smoke outcome and only then set `route_ready: true`
   for ordinary reviews on that route. A route/configuration change invalidates
   that readiness. If a seat cannot inspect images, stop rather than substitute a
   textual description of the screenshot or silently reduce the panel.

A passing fixture establishes basic image/JSON operation, not aesthetic quality or
upstream identity where the transport does not expose it. A real UI pilot and a
with/without-skill comparison remain separate evaluations.

## Preparation input

Keep this JSON and the resulting `prepared.json` in a private run directory. Use
a dated directory if the target project requires one. The helper creates the
public image directory separately under the system temporary directory.

```json
{
  "purpose": "review",
  "agents_loaded": true,
  "route_ready": true,
  "blindness_checked": true,
  "brief": {
    "audience": "Staff processing support tickets",
    "tasks": ["Find an urgent ticket and assign it"],
    "requirements": ["Keep urgency, ownership and status visible"],
    "brand": ["Retain the product's blue accent"]
  },
  "candidates": [
    {
      "key": "reference",
      "revision": "frozen-tree-and-fixture-identity-A",
      "captures": [{
        "path": "/absolute/captures/reference.png",
        "viewport": {"width": 1280, "height": 900, "device_scale": 1},
        "theme": "light",
        "state": "ticket list",
        "fixture": "support-fixture-v1"
      }]
    },
    {
      "key": "candidate",
      "revision": "frozen-tree-and-fixture-identity-B",
      "captures": [{
        "path": "/absolute/captures/candidate.png",
        "viewport": {"width": 1280, "height": 900, "device_scale": 1},
        "theme": "light",
        "state": "ticket list",
        "fixture": "support-fixture-v1"
      }]
    }
  ],
  "focus": "candidate",
  "round": 1,
  "max_rounds": 3,
  "target_overall": null,
  "runtime": {"status": "not_run", "revision": "", "evidence": []}
}
```

`revision` must identify the captured code and data, including uncommitted changes.
These example strings are placeholders, not valid evidence of a freeze. The helper
records the supplied identity but cannot verify the state of an arbitrary app.

`order` optionally supplies a permutation of private candidate keys. Otherwise the
helper randomly chooses an order once outside Workflow and records it in the
provenance map. All judges in that round see the same order. Keep references fixed
across rounds, but vary labels/order without exposing which candidate is new.

Every candidate needs the same set of viewport/theme/state/fixture contexts.
Capture the missing contexts rather than filling gaps with invented observations.
For comparisons across different products, choose a common task fixture and state
vocabulary; retain content differences that matter to the task. A single candidate
is allowed, with comparison preference `unknown`.

The helper copies PNG/JPEG bytes without altering them. It checks signatures, not
full image decoding or pixel dimensions. The capture step supplies viewport
metadata, and actual image inspection remains necessary. Review screenshot text
and metadata for leakage before attesting blindness. Keep public paths neutral;
never put targets, author names or round numbers into filenames or labels.

Extra fields on the input object are private and are never copied wholesale into
the public packet. Public brief/capture fields are selected explicitly. This does
not sanitize prose placed inside those allowed fields. The output contains:

- `public_root`: a new directory containing only neutrally named image files.
- `provenance`: private source paths, revisions, candidate keys and image hashes.
- `input_sha256`: identity of the preparation input.
- `workflow`: the public packet, shared contract/schema and private control fields.

Pass only the `workflow` object as Workflow arguments. The script builds each
critic prompt from `contract` and `packet`, never `control`, provenance or the
original request. Its public-key checks reject accidentally merged private fields.
The contract and schema must come from this skill, not user-supplied review data.

## Results and continued rounds

Save the Workflow result privately with the verified packet identity and the
coordinator's finding dispositions. A completed panel returns
`status: "needs_adjudication"`, not a pass. `accepted` is always false because the
panel alone cannot authorize acceptance. `target_met` is a diagnostic for the
focus candidate's holistic score across every required judge; it does not average
scores or erase findings. A null target stays null.

`can_revise` means only that the panel completed and a review-round slot remains.
It is not edit authorization or evidence that there is time/token budget left.
Before continuing, identify a worthwhile repair, preserve the best valid state,
advance `round`, retain the original `max_rounds`, and prepare new captures.
This coordinator-managed cycle stops at the recorded limit; the workflow refuses
an input round beyond it. It does not provide a persistent cross-run budget ledger
or prevent a caller from lying about its round count.

`verify` rehashes frozen images and the packet. It does not re-run browser checks,
re-prove model routing, or check the current source tree. Verify those separately
before acceptance. If capture or code changes, create a new packet and fresh
judgments; an old result's matching filenames are not sufficient.

The public directory is retained for review and replay. Record its path, keep it
until the user no longer needs the evidence, and follow the project's cleanup
policy. The helper does not delete evidence or private records.
