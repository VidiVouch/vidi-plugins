# Handing work back — the three transport tiers

A report is worth more when the reader can act from it. There are exactly three ways to
carry that action somewhere an agent picks it up, and they are not interchangeable — pick
by *where the work should land*.

## Tier 1 — same conversation (the live-server loop)

The tightest loop, and the only one that *continues the current work* instead of starting
over. `scripts/serve_note.py` serves the note and, on a section's `expand` action, spins a
**scoped subagent** (just that block + the note's thesis), gets a validated block back,
patches `note.json` by id, and the page swaps that section in place.

- No new session, no URL, no copy-paste. The reader clicks; the note changes.
- The server orchestrates the subagents itself (`claude -p`, `ccs <profile> -p`, or a local
  worker), so the note **keeps working after this conversation ends** — it's not waiting on
  the main window.
- Cost: one *scoped* subagent per click, not a main-conversation reload. Parallel-safe.
- Constraint: localhost, same machine. The moment it must reach a phone or a colleague,
  you're in Tier 2/3 or exporting a static file — a live loop can't be shared.

Use this whenever the report and the agent are on the same box. It's the default for
anything interactive.

## Tier 2 — fresh default app (a deep link)

A one-click URL that opens a *new* session in one of the two apps that register a URL
scheme. Inert until the human presses Enter.

**Claude Code** (v2.1.91+):
```
claude-cli://open?q=<url-encoded prompt>&cwd=<abs path>
claude-cli://open?q=%2Fmy-skill%20args&repo=<owner/name>
```
- Opens a new terminal, prompt pre-filled, never auto-sent. `q` ≤ 5,000 chars.
- **Thin prompt is fine** — the new session inherits `CLAUDE.md`, skills, and can re-fetch
  live state via `` !`command` ``. For long runbooks, point `q` at a skill (`/my-skill args`)
  rather than inlining the whole thing.
- Always a *new* session — there is no `claude-cli://` parameter that resumes an existing
  conversation. (That's what Tier 1 is for.)
- GitHub-rendered markdown strips the scheme; only works in HTML you control.

**Codex Desktop**:
```
codex://threads/new?prompt=<url-encoded>&path=<abs path>
codex://threads/<thread-uuid>            # reopen an existing Desktop thread
```
- Opens the Desktop app, composer pre-filled. Must include at least one of
  `prompt`/`path`/`originUrl`; `path` wins over `originUrl`.
- **Fat prompt required.** Codex inherits none of Claude's context — no `CLAUDE.md` (it
  reads `AGENTS.md`), no Claude skills, no session memory. The prompt must be
  self-contained: restate the task, the absolute paths, and the relevant findings inline.
- `codex://threads/<uuid>` can reopen an existing thread, but a Claude-generated report has
  no Codex thread id to point at — so it's not part of the report loop.

## Tier 3 — switched backend (a copy-command)

Provider-switched targets can't be reached by any URL — there's no scheme parameter for a
profile. The page copies a shell command to the clipboard; the reader pastes it.

- **CCS profile** (GLM / Kimi / Ollama / …): `ccs glm "<fat prompt>"`. A per-invocation
  `ccs` switch never persists, and the `claude-cli://` handler launches plain `claude`
  (default resting config) — so a deep link can't carry a profile. Copy-command only.
- **Codex CLI** (vs Desktop): `codex exec "<fat prompt>"` for a headless run. (Don't use
  `codex exec resume` from a report — there's no prior Codex session to resume into, and it
  targets non-interactive continuation, not a hand-off.)
- Same fat-prompt rule as Codex Desktop: self-contained, because you're starting a cold,
  non-default backend.

## The rule that ties it together

**URLs reach only the two default apps (default Claude, Codex Desktop). Anything
provider-switched — CCS profiles, Codex CLI — needs a copy-command. And any hand-off to
Codex or a switched backend carries a fat, self-contained prompt, because it inherits none
of this session's context.** Claude hand-offs can be thin; everything else must be fat.

In the content model these are all just `handoff` actions with a `target`
(`claude` / `codex` / `ccs`) — the renderer builds the URL or copy-command client-side, so
a hand-off costs no server round-trip. See `references/content-model.md`.

## Why not other approaches

- **`claude -r <session-id>` copy-command** (resume this exact Claude conversation from a
  terminal) works, but Tier 1 already gives same-conversation continuation with one click
  and no paste — so it's redundant. Dropped.
- **Hosting the note + a public endpoint**: reaches other people/devices, but it's no
  longer *this* conversation and it's public. That's a share, not a loop — use a static
  export (`render_note.py > note.html`) and Tier 2/3 buttons inside it.
