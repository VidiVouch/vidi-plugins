# Interactivity — closing the loop

A report the user can only read loses most of the value. These are the proven patterns
for letting edits, selections, and decisions flow back into the agent loop. All of them
are vanilla JS, no framework. Working demos of each live in `assets/snippets.html`.

## 1. Copy-as-prompt / copy-as-JSON

The workhorse. Checkbox-select rows → one button serializes the selection into
paste-ready text. The user filters thousands of rows down to the evidence they care
about, copies, and pastes into their next Claude message.

```js
function copySelected(asJson) {
  const rows = [...document.querySelectorAll('input.row-pick:checked')]
    .map(cb => DATA[Number(cb.dataset.idx)]);
  const text = asJson
    ? JSON.stringify(rows, null, 2)
    : rows.map(r => `- [${r.date}] ${r.source}: ${r.text}`).join('\n');
  navigator.clipboard.writeText(text).then(() => flash(`Copied ${rows.length} rows`));
}
```

Give every copy action a visible confirmation (a transient "Copied N rows" flash) —
silent clipboard writes read as broken buttons.

## 2. URL-hash view state + "Copy view link"

Serialize filter/search/scroll state into `location.hash` so any filtered view is one
copyable URL. This is what makes multi-artifact reports cross-referenceable: a claim in
the summary doc links to `explorer.html#q=deploy&d=2026-06-12` and the reader lands on
the exact evidence.

```js
function writeHash() {
  const p = new URLSearchParams();
  if (state.q) p.set('q', state.q);
  if (state.day) p.set('d', state.day);
  history.replaceState(null, '', '#' + p.toString());
}
function readHash() {
  const p = new URLSearchParams(location.hash.slice(1));
  state.q = p.get('q') || ''; state.day = p.get('d') || '';
}
addEventListener('hashchange', () => { readHash(); render(); });
```

Add a 🔗 "Copy view link" button (global and per-row/per-day) that copies
`location.href`. **Then verify**: when a summary doc cites hash links into other
artifacts, programmatically check every `href="target.html#..."` matches ≥1 real row in
the target's embedded data. "0 dead links out of 112 checked" is a reportable number.

## 3. Dual-mode feedback export (fetch → catch → Blob)

For pages that collect free-text feedback or edited state. Try POSTing to a local
server; when there is no server, fall back to a client-side file download. One code
path, degrades gracefully:

```js
function saveFeedback(payload) {
  fetch('/api/feedback', {
    method: 'POST', headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(payload),
  })
  .then(() => setStatus('Saved'))
  .catch(() => {                       // static mode — download instead
    const blob = new Blob([JSON.stringify(payload, null, 2)], { type: 'application/json' });
    const url = URL.createObjectURL(blob);
    const a = document.createElement('a');
    a.href = url; a.download = 'feedback.json'; a.click();
    URL.revokeObjectURL(url);
    setStatus('Downloaded feedback.json');
  });
}
```

Supporting patterns that make feedback collection feel right:
- **Debounced autosave**: save 800 ms after the last keystroke, plus on every
  navigation away from an item, so nothing is lost to a missed click.
- **Visited tracking**: keep a `Set` of viewed items; enable the "Submit" button only
  once every item has been seen — no feedback on skimmed-past items.
- **Keyboard nav**: arrow keys move between items, guarded with
  `if (e.target.tagName === 'TEXTAREA') return;` so typing never navigates.

If serving live, a stdlib Python server is enough: regenerate HTML on every `GET /`
(refresh picks up changes without restart) and persist `POST /api/feedback` to a JSON
file. Kill stale listeners first: `lsof -ti :PORT | xargs kill`.

## 4. Deep links — hand the work back to Claude Code

`claude-cli://` is an OS-registered scheme (Claude Code v2.1.91+). A link opens a NEW
terminal running Claude Code with the prompt **pre-filled but never auto-sent** — the
user reviews and presses Enter. That safety property means action buttons in a report
are inert until a human commits.

Format — single endpoint, three params:

```
claude-cli://open?q=<encodeURIComponent prompt>&cwd=<abs path>
claude-cli://open?q=...&repo=<owner/name>
```

- `q` — pre-filled prompt text. URL-encoded, `%0A` for newlines, **max 5,000 chars**.
- `cwd` — absolute working directory. Wins over `repo` if both given.
- `repo` — GitHub slug; resolves only to a local clone Claude Code has already been run
  in; otherwise falls back to `$HOME`.

```html
<a href="claude-cli://open?repo=acme/payments&q=Investigate%20the%20failed%20deploy">
  Investigate in Claude Code →
</a>
```

Or build dynamically from page state:

```js
function claudeLink(prompt, cwd) {
  return 'claude-cli://open?cwd=' + encodeURIComponent(cwd) +
         '&q=' + encodeURIComponent(prompt);
}
```

Caveats:
- **GitHub-rendered markdown/HTML strips non-http(s) schemes** — deep links only work in
  HTML you control (local files, hosted pages). For GitHub surfaces, show the raw URL in
  a code block for copy-paste.
- The 5,000-char cap on `q` means long runbooks don't fit. The fix is the next section.

## 5. The full loop: deep link → skill → dynamic context

Instead of stuffing a runbook into `q`, store it as a skill and make the link just name
it: `q=%2Fmy-skill%20some-arg` (i.e. `/my-skill some-arg`). The skill then pulls **live**
state at invocation time via dynamic-context injection, so it acts on current reality,
not a snapshot baked into the report:

```yaml
---
name: triage-finding
description: Triage a finding exported from the audit report
arguments: [finding_id]
---

## Live state
- Current diff: !`git diff HEAD --stat`
- Finding record: !`grep -A5 "$finding_id" audit/findings.jsonl`

## Task
Triage finding $finding_id against the current tree...
```

Mechanics of `` !`command` `` (it's preprocessing by Claude Code, not something the model
runs): executes when the skill loads, stdout replaces the placeholder before the model
sees anything; runs once, output is not re-scanned; `!` must be at line start or after
whitespace; multi-line commands use a fenced block opened with ```` ```! ````. Argument
substitution: `$ARGUMENTS`, `$0`/`$1`…, named args via `arguments:` frontmatter, plus
`${CLAUDE_SKILL_DIR}` for bundled scripts and `${CLAUDE_SESSION_ID}` for session-scoped
filenames.

So the complete circuit: **report → filter/select → action button → deep link opens
Claude Code pre-filled with `/skill args` → user hits Enter → skill re-fetches live
state via `` !`command` `` → work continues with fresh data.**

## 6. Verdict collection → carry-on prompt (the fullest loop)

When the deliverable is a set of items the reader must *decide on* — ratify/revise/drop,
triage, sign-off — the payoff isn't feedback text, it's a compiled set of decisions
handed back to the agent. `assets/review-walkthrough.html` is the working template; this
is its spine, proven on the vidivouch F0 ratification pass (55 items).

- **Autosave + resume.** Every verdict writes to `localStorage` immediately; on reload,
  jump to the first *undecided* item rather than the top. A review that can't be resumed
  won't be finished.
- **Undecided ≠ accepted — the honesty rule.** This is the one that matters. An item the
  user never reached is *not* an affirmed item. Compile pending items into their own
  bucket and, crucially, say so in the prompt back to the agent: "I have NOT accepted
  these; ask me before relying on them." Silently treating skipped items as approved is
  the failure mode that makes a review tool untrustworthy.
- **Compile → three outputs.** At the end, one screen: (a) `downloadJSON()` writes the
  full decisions ledger as a Blob; (b) `carryPrompt()` assembles a prose prompt from the
  buckets — affirmed count, revised-with-notes, dropped-with-notes, not-reviewed; (c)
  `carryOn()` does both, then fires the `claude-cli://` deep link pre-filled with that
  prompt (sliced to ~4800 chars to stay under the 5,000 cap), never auto-sent.

```js
function carryOn() {
  downloadJSON();                                   // ledger to disk first
  location.href = 'claude-cli://open?cwd=' + encodeURIComponent(DATA.cwd) +
    '&q=' + encodeURIComponent(carryPrompt().slice(0, 4800));
}
```

The reader walks the set, the agent gets back a structured, note-annotated decision set
it can act on directly — and the pending items are flagged, not laundered into approval.
Optional extensions the F0 tool carried: a rapid bulk-toggle table for a long tail of
low-stakes items, per-item "discussion thread" questions collected alongside verdicts,
and an "affirm all remaining" bulk action.
