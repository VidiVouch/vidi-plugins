---
name: html-report
description: Produce user-facing outputs as self-contained, interactive HTML files instead of markdown — status reports, audits, findings write-ups, review/sign-off docs, variant comparison galleries, data explorers, timelines, dashboards. Use this whenever the deliverable is something a human will read or review, even if they never say "HTML" — the standing preference here is HTML for anything cross-referenced or reviewed ("with html we can create links across sources for review"). Also use when asked for "a doc to review", "a summary of findings", "an audit report", or "something I can look through". Agent-to-agent notes, scratch files, and plans stay markdown; anything for human eyes gets HTML.
---

# HTML Report

Markdown is for agents; HTML is for people. A markdown report is one long scroll with no
links between evidence and claims. A self-contained HTML file gives you cross-source
links, filterable tables, light/dark theming, and — the real payoff — **two-way
interaction**: the reader filters down to what matters, clicks "Copy as prompt", and
pastes it straight back into the next Claude turn. Background reading (skim once, then
rely on the references here):

- https://claude.com/blog/using-claude-code-the-unreasonable-effectiveness-of-html — why HTML, and the core patterns
- https://code.claude.com/docs/en/deep-links — `claude-cli://` links that hand work back to Claude Code
- https://code.claude.com/docs/en/skills#inject-dynamic-context — `` !`command` `` injection for the skill side of the loop

## Two ways to build

**Hand-authored template** (fast, one-off): copy an `assets/*.html` template, fill in the
content. Right for a static report you'll write once and hand over.

**JSON content model** (the system): author the note as JSON typed blocks and let
`scripts/render_note.py` produce the HTML deterministically — structure, theming, and
transport cost zero tokens; your budget goes only to content. This is the path when the
note is **interactive or long-lived** — because the same JSON also serves live via
`scripts/serve_note.py`, where each "Explain further" button dispatches a *scoped
subagent* that expands that section in place, and the note keeps working after this
conversation ends. Read `references/content-model.md` first; it defines the block
vocabulary and the three response modes (structured block / HTMX-style fragment /
hand-off). Reach for it whenever the reader will *drive* the note, not just read it.

```
python scripts/render_note.py note.json > note.html          # static, self-contained
python scripts/render_note.py note.json --format md          # flatten for agents
python scripts/serve_note.py  note.json                       # live; expands dispatch to subagents
python scripts/serve_note.py  note.json --dispatch ccs:glm    # route expansions through a ccs profile
```

## Pick the shape

| Deliverable | Start from | Notes |
|---|---|---|
| Status report, audit, findings, sign-off, spec — read for the verdict | `assets/report.html` | Utilitarian: dark chrome header, numbered sections, verdict table, stat cards, severity chips. Cyanotype palette |
| Synthesis, field guide, landscape, "what we found across N things" — read to be informed | `assets/report-editorial.html` | Editorial: sticky bar, hero with eyebrow + clamp headline, stat strip, JSON-rendered cards, wire/code samples, step ladder. Commits to its own instrument palette |
| "Which variant?" — designs, screenshots, options | `assets/gallery.html` | Figure grid + live links, light/dark stacked. The user's verdict usually lands as a one-line pick — make each option easy to name |
| Data explorer — logs, timelines, message dumps | write a generator script | See `references/generation.md`; search + filter chips + copy-selected-as-prompt |
| Decide on a set — ratify/triage/sign-off findings, one verdict per item | `assets/review-walkthrough.html` | Guided walkthrough: verdict + note per item, autosave/resume, compile → download ledger + carry-on prompt → deep-link back into Claude Code. Enforces undecided ≠ accepted |
| Interactive playground / logic simulator | freeform single file | Inputs at top, live-updating result below; mirror the real code's logic exactly |
| Live, driveable note — reader expands/discusses sections and the note updates in place | JSON + `scripts/render_note.py` / `serve_note.py` | The content-model path. Per-section actions dispatch to scoped subagents (block/fragment/hand-off); runs headless after the conversation ends. See `references/content-model.md` |

**Utilitarian vs editorial is a real fork, not a mood.** Per the artifact-design skill: calibrate the treatment to how the deliverable will be consumed. A thing someone scans for a pass/fail ruling wants `report.html` — hierarchy and a verdict, no hero. A thing someone reads to understand a landscape wants `report-editorial.html` — it earns attention with a hero, an eyebrow-labeled rhythm, and density. When unsure, ask which mode the reader is in; don't dress an audit as a magazine or vice versa.

Templates are starting points, not straitjackets — restructure freely, but keep the
non-negotiables below. Read `assets/snippets.html` for the interactive building blocks
(copy-as-prompt, view links, feedback export, deep-link buttons) — it is itself a
working demo page you can open. The two report templates share the same DNA (self-
contained, token cascade, no-flash bootstrap, embedded-JSON rendering); they differ only
in altitude — reach for the editorial one when the content deserves to be read, not
just checked.

## Non-negotiables

1. **Self-contained single file.** Inline all CSS and JS. No CDN fonts, no ES-module
   imports, no fetch/AJAX for data — embed the dataset as JSON in a `<script>` tag.
   CDN and module imports silently fail on restricted networks and in-app browsers;
   classic `<script>` IIFEs work everywhere. System font stack by default; if the
   treatment calls for a display face, inline it as a `@font-face` data URI.
2. **Both themes, toggleable.** Use the token cascade + `data-theme` bootstrap from
   `references/theming.md`. One `:root` block, one `:root[data-theme="dark"]` override —
   never separate light/dark stylesheets, never media-query-only dark mode.
3. **Numbers carry evidence.** Every stat, count, or verdict links to or names its
   source ("14/17 repos reporting", a sha, a file path in `<code>`). A bare number in a
   report is a claim without a citation.
4. **One severity taxonomy.** Whatever labels you use in chat (Critical/High/Med/Low,
   Must/Should/Could, keep/redesign/drop) — the HTML uses the same words and one color
   token each. Never invent a second vocabulary for the doc.
5. **Close the loop.** A read-only report loses most of the value. Include at least one
   export path: copy-as-prompt/JSON for selected rows, a feedback box with Blob export,
   or a `claude-cli://` action button. Patterns in `references/interactivity.md`.
6. **Sensitive content stays local.** Private business data, PII, unreleased work →
   local file, never a hosted artifact. Redact obvious PII patterns (card numbers,
   tokens) programmatically before embedding raw export data. Untrusted strings go in
   via `textContent`, never `innerHTML` concatenation.

## Build

1. **Sketch a design plan first** (per the artifact-design skill — invoke it for
   anything beyond a utilitarian internal doc): 4–6 named hex colors, typefaces by
   role, one-sentence layout concept. A report is a *utilitarian* treatment: real
   hierarchy and a considered palette, no hero sections. Avoid the AI-design tells:
   warm-cream + terracotta, purple gradients, Inter-everywhere, emoji section markers.
2. **Honor existing tokens.** If the project has a design-token file (e.g.
   `design/assets/cyanotype.css`), use its custom properties instead of the template's
   palette. `report.html`, `gallery.html`, and `snippets.html` ship the cyanotype system
   (Prussian-blue ink, azure accent, Hanken Grotesk + Maple Mono) as the default for
   utilitarian docs; `report-editorial.html` runs its own committed teal instrument
   palette — an editorial piece is allowed its own visual world. Either way the token
   cascade (`:root` → `@media dark` → `:root[data-theme]`) is fixed; swap the values, keep
   the structure.
3. **Real content only.** Never lorem ipsum, never placeholder rows. If the data isn't
   gathered yet, gather it first.
4. **Charts belong to the dataviz skill.** If the report includes any chart, stat tile,
   or dashboard element, invoke `dataviz` and follow it — don't re-derive chart rules.
5. **Generated at scale?** (hundreds of rows, multiple artifacts) — write a re-runnable
   generator script with the placeholder+validate pattern in `references/generation.md`.
6. **Long-lived doc?** Give sections and rows stable `id` anchors so later updates are
   small targeted edits, not rewrites — these docs get patched incrementally for weeks.

## Verify before declaring done

- No leftover template placeholders (`grep` for `__` markers, `{{`, "TODO").
- Embedded JSON parses; extracted `<script>` passes `node --check`.
- Every internal `href` resolves — cross-artifact hash links checked against the target's
  embedded data ("0 dead links" is a reportable number), and every nav target smoke-clicked.
- Light and dark both render: if screenshotting, follow `references/screenshot-verify.md`
  — the two PNGs must differ by md5 AND the dark one must actually look dark. Several
  silent failure modes produce byte-identical shots.
- Open it. `open report.html` works for handing to the user; for any automated
  browser/screenshot loop use a throwaway `python -m http.server` — `file://`
  navigation breaks unpredictably in automation.

## Delivery

- Local viewing: `open <file>.html` — done. Zip the folder if assets are split; keep
  paths relative so a zipped folder just works.
- **iOS / messaging apps: a self-contained HTML attachment is a dead end** — Quick Look
  previews `.html` without executing any JS. If the report must reach a phone, host it
  (Cloudflare Pages or similar) and send a link — after flagging that hosted = anyone
  with the link, which is why sensitive docs don't ship this way.
- Any interactive viewer needs a **visible failure state**: a staged status/diagnostic
  overlay ("Decompressing… → Building… → Rendering…") so a blank page is never silent
  and a remote user can report exactly where it stuck.

## Hand work back — three transport tiers

Ordered by how tightly they close the loop. Full mechanics in `references/handoff.md`.

1. **Same conversation → the live-server loop.** `serve_note.py` dispatches a section's
   action to a scoped subagent that edits the note in place. No new session, no hand-off —
   *this* is the tightest loop, and the only one that continues the current work. Prefer it
   whenever the report and the agent live on the same machine.
2. **Fresh default app → a deep link.** `claude-cli://open?q=&cwd=` opens a new Claude Code
   session pre-filled (never auto-sent); `codex://threads/new?prompt=&path=` opens Codex
   Desktop. Thin prompt for Claude (inherits CLAUDE.md/skills, or point `q` at `/skill`);
   **fat, self-contained prompt for Codex** (no CLAUDE.md, reads AGENTS.md, cold).
3. **Switched backend → a copy-command.** Provider-switched targets (a `ccs <profile>`
   GLM/Kimi run, or `codex exec`) can't be reached by any URL — the page copies a
   `ccs glm "<fat prompt>"` command to the clipboard instead. Rule: **URLs reach only the
   two default apps; anything switched needs a copy-command with a self-contained prompt.**

## References

- `references/content-model.md` — **the JSON block spec**: vocabulary, the note document, actions, the three response modes, fragment guardrails. Read first when building anything interactive or long-lived.
- `scripts/render_note.py` (JSON → self-contained HTML, `--live` / `--format md` / `--block <id>`) and `scripts/serve_note.py` (live server + subagent dispatch). `assets/example-note.json` is a worked note that exercises the whole vocabulary.
- `references/handoff.md` — the three transport tiers (live loop / deep link / copy-command), Claude + Codex + CCS targets, thin-vs-fat prompt rule.
- `assets/report.html` (utilitarian) and `assets/report-editorial.html` (editorial) — the two hand-authored report templates; `assets/gallery.html` (variant comparison); `assets/review-walkthrough.html` (verdict collection → carry-on); `assets/snippets.html` (interactive building blocks). Open them; copy the one that fits the altitude.
- `references/theming.md` — token cascade, no-flash bootstrap, toggle, print, reduced motion, contrast math. Read before writing any `<style>`.
- `references/interactivity.md` — copy-as-prompt, URL-hash view links, dual-mode feedback export, deep links, dynamic-context skills. Read when adding any interactive element.
- `references/generation.md` — generator scripts, placeholder+validate, embedding files, serving, distribution triage. Read before writing a generator or shipping beyond the local machine.
- `references/screenshot-verify.md` — the light/dark screenshot tripwires. Read before screenshotting any themed page.
