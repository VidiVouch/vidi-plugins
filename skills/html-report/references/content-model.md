# Content model — the JSON spec behind interactive notes

A note is **JSON, not HTML**. A deterministic renderer (`scripts/render_note.py`) turns
that JSON into a self-contained, themed, interactive HTML page. You author and mutate the
JSON; you never hand-write the scaffolding. This is what makes the structure, theming,
and transport cost zero tokens — the model's budget goes only to *content*.

The same JSON renders three ways:
- **static** — one self-contained HTML file (shareable, no server, no live actions),
- **live** — served by `scripts/serve_note.py`, actions dispatch to subagents,
- **markdown** — flattened for agent-to-agent notes.

One content model, many renderers. Author once.

## The document

```json
{
  "meta": {
    "title": "Report title",
    "flavor": "utilitarian",        // "utilitarian" | "editorial" — picks the render treatment
    "cwd": "/path/to/workspace",    // working dir for hand-off actions
    "theme": "auto"                 // "auto" | "light" | "dark" — initial theme
  },
  "blocks": [ /* ordered typed blocks */ ]
}
```

Every block has a `type` and, if it can be a target of an action or a live update, a
stable `id`. Give ids to anything a subagent might expand, and to top-level sections —
patches and fragment swaps are addressed by id.

## Core block vocabulary

Keep this small. A finite, well-known set is what keeps the renderer deterministic and
the subagent output validatable. When a shape recurs through the `fragment` escape hatch,
*promote* it to a real block here rather than letting fragments sprawl.

| type | fields | renders as |
|---|---|---|
| `hero` | `eyebrow?`, `title`, `highlight?`, `lede?`, `flow?[]` | editorial hero: eyebrow, clamp headline (with `highlight` span), lede, flow strip |
| `stat-strip` | `stats: [{v, k, evidence?}]` | hairline-gap grid, mono tabular numbers |
| `section` | `id`, `num?`, `title`, `sub?`, `body: [block]`, `actions?[]` | numbered section wrapping child blocks |
| `prose` | `md` | paragraph; safe minimal markdown (`**bold**`, `` `code` ``, `[text](url)`) |
| `callout` | `tone: info\|ok\|warn\|crit`, `heading?`, `md` | tinted callout box |
| `table` | `columns:[]`, `rows:[[...]]`, `caption?` | overflow-safe table |
| `cards` | `items:[{name, idx?, chips?[], kv?{}, body?[]}]` | 2-col card grid |
| `wire` | `label`, `verb?`, `code`, `lang?` | labeled code/sample frame |
| `ladder` | `steps:[{t, d}]` | numbered step ladder |
| `verdict-item` | `id`, `title`, `meta?`, `body?[]`, `verbs:[{k,label,bucket,note?}]` | one review item with verdict buttons (feeds the walkthrough loop) |
| `fragment` | `html`, `fallback`, `sandbox?`, `height?` | **escape hatch** — see below |
| `chart` | `spec` | delegate to the `dataviz` skill; renderer emits a container |

Text fields are escaped by the renderer (textContent semantics). Only `prose.md` gets the
minimal-markdown pass, and only `fragment.html` is injected as markup — nothing else.

## Actions — three response modes

Any `section`, `cards` item, or `verdict-item` may carry an `actions` array. The renderer
emits a button per action; a click routes through `serve_note.py`, which dispatches by
`kind`. There are exactly three response modes:

```jsonc
"actions": [
  // 1. STRUCTURED — a scoped subagent returns a validated JSON block; renderer patches by id.
  { "kind": "expand", "label": "Explain further", "agent": "sonnet",
    "swap": "replace",            // "replace" the target's body, or "append" to it
    "response": "block" },        // expect a JSON block back (default)

  // 2. FRAGMENT (HTMX-style) — subagent returns an HTML fragment, swapped into the target.
  //    The escape hatch for anything the block vocabulary can't represent. Second-class:
  //    flagged, sandboxed, must carry a fallback. See guardrails.
  { "kind": "expand", "label": "Visualize", "agent": "sonnet",
    "response": "fragment" },

  // 3. HAND-OFF — no subagent; the page builds a URL/command client-side (zero server round-trip).
  { "kind": "handoff", "label": "Do in Codex", "target": "codex",
    "prompt": "<fat, self-contained task — Codex has no CLAUDE.md/skills/session>" }
]
```

**Dispatch targets** (see `references/handoff.md` for the full transport table):
- `expand`/`discuss` → in-process scoped subagent (`agent` names the model/profile; the
  server shells out via `claude -p`, `ccs <profile> -p`, or a local worker). The subagent
  receives only the target block + the note's title/thesis, and returns one block or
  fragment. Small context, parallel-safe, and it runs **without the main conversation** —
  the note keeps working after you walk away.
- `handoff` with `target: "claude"` → `claude-cli://open?q=&cwd=` (fresh default Claude,
  thin prompt — inherits CLAUDE.md/skills).
- `handoff` with `target: "codex"` → `codex://threads/new?prompt=&path=` (fresh Codex
  Desktop, **fat** self-contained prompt).
- `handoff` with `target: "ccs"`, `profile: "glm"` → copies `ccs glm "<fat prompt>"` (no
  URL can carry a switched provider — copy-command only).

## The fragment escape hatch — guardrails

`fragment` exists so a subagent can express what the spec can't (a bespoke viz, a custom
widget). It surrenders validation, portability, automatic theming, and the no-raw-HTML
safety rule — so it is deliberately **second-class**, and the guardrails are mandatory:

- **Flagged, not blended.** `type: "fragment"` marks the node as unvalidated and
  unportable so it's never confused with trusted blocks.
- **Sandboxed when subagent-authored.** `sandbox: true` (the default for any fragment a
  subagent produced) renders it inside `<iframe srcdoc>` — its script can't touch the
  parent note. Author-written fragments may set `sandbox: false`. This is the one place
  the renderer injects markup; the sandbox is why that's safe.
- **Theme tokens injected.** The renderer passes the CSS custom properties into the
  sandbox; a fragment must style via `var(--accent)` etc., never hardcoded hex, or it
  breaks light/dark.
- **Fallback required.** `fallback` is plain text/markdown the static and markdown
  renderers emit in place of the fragment (like alt text) — otherwise exporting a live
  note leaves a hole.
- **Promote on recurrence.** A fragment shape that shows up repeatedly is a signal to add
  a real block type. The escape hatch is where the vocabulary grows from evidence.

## Cost model (why this shape)

Deterministic (zero tokens): the renderer, theming, transport, fragment swap, and
re-render. Token cost is isolated to (a) authoring the initial note JSON and (b) each
`expand` — a *scoped* subagent turn, not a main-conversation reload. See the top of
`references/interactivity.md` and the SKILL.md cost note.
