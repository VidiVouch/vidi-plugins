# Generating, serving, and shipping HTML reports

For anything beyond a hand-written page: re-runnable generators, validation, and the
distribution traps that have actually burned us.

## Generator scripts: placeholder + validate

When a report embeds real data (hundreds of rows, multiple sources), write a
re-runnable script (`generate_report.py`) rather than hand-writing the HTML. The proven
shape:

1. Keep the full HTML as a template string with literal placeholders:
   `__DATA__`, `__META__`, `__GEN__` (generation timestamp).
2. Substitute with plain `str.replace` — no templating engine needed:
   ```python
   html = TEMPLATE.replace("__DATA__", json.dumps(rows)) \
                  .replace("__META__", json.dumps(meta)) \
                  .replace("__GEN__", gen_stamp)
   ```
3. **Validate after every regeneration** — this is what makes the pattern trustworthy:
   ```python
   out = pathlib.Path("report.html").read_text()
   for ph in ("__DATA__", "__META__", "__GEN__"):
       assert ph not in out, f"leftover {ph}"
   json.loads(re.search(r"const DATA = (\[.*?\]);\n", out, re.S).group(1))
   ```
   Plus extract the `<script>` body to a temp file and run `node --check` on it —
   catches JS syntax errors before the user opens a broken page.

**Raw strings, always.** Make the template a raw string (`r"""…"""`). A non-raw Python
template once turned the `\n` inside embedded JS (`.replace(/\n/g,' ')`) into literal
newlines and corrupted the script. Any generator language that processes backslashes has
this trap.

## Embedding files and data

Everything inlines into the one file — no loose assets to lose:

- Structured data → `const DATA = <json.dumps(...)>` in a `<script>` tag; client JS
  renders from it. No fetch/AJAX ever.
- Text files → inlined as strings.
- Images / binaries → base64 `data:` URIs (`data:image/png;base64,...`); anything
  unviewable gets a data-URI download link.
- Untrusted strings (user text, LLM output, filenames) enter the DOM via
  `textContent`/`createTextNode` — never `innerHTML` concatenation. If you must build
  HTML strings, escape through a textContent round-trip first.
- Scan raw export data for PII before embedding: card-number-like digit runs, tokens,
  secrets. Redact, then assert the redaction held in the validation pass.

Exception to "everything inline": a folder of full-resolution screenshots referenced by
relative path is fine when the deliverable is the folder (zip it and paths still work).

## Serving

- Handing to the user on this machine: `open report.html`. Done.
- **Any automated browser/screenshot loop: use a local HTTP server, not `file://`.**
  `file://` navigation via browser automation breaks unpredictably (URLs mangled to
  `https://file///...`, sandboxed non-executing loads). `python -m http.server 8089 -d <dir>`
  then `http://localhost:8089/report.html` sidesteps all of it.
- Live feedback loop: stdlib `http.server` that regenerates the page per `GET /` and
  persists `POST /api/feedback` to disk (see `references/interactivity.md` §3). Kill
  stale port-holders first: `lsof -ti :8089 | xargs kill`.

## Distribution triage — decide this BEFORE polishing the tech

Ask where the file will actually be viewed:

| Destination | What works |
|---|---|
| This machine, this user | Self-contained file, `open` it |
| Email/zip to a colleague (desktop) | Self-contained file or relative-path folder, zipped |
| Phone via iMessage/AirDrop | **Self-contained HTML is a dead end** — iOS Quick Look previews `.html` without executing any JS. No packaging trick fixes this. Host it and send a link |
| Hosted (Cloudflare Pages etc.) | Works everywhere — but anyone with the link can view. Flag that explicitly before deploying; sensitive content doesn't ship this way |
| GitHub README/issue | `claude-cli://` links get stripped; show raw URLs in code blocks |

Related hard-won rules:
- No CDN anything (fonts, libs) and no ES-module imports — they silently fail on
  restricted networks and in-app browsers. Inline libraries as classic `<script>` IIFE
  globals. This is the difference between "works on my desktop" and "works on the
  phone in someone else's hand".
- Interactive/heavy viewers need a **staged diagnostic overlay** ("Decompressing… →
  Building scene… → Rendering…") so a failure is a message, not a silent blank page —
  a remote user can then tell you exactly which stage hung.

## Long-lived documents

Reports that act as a running source of truth (scope docs, audits updated over weeks):
give every section and requirement row a stable `id`, then patch with small targeted
edits as facts change — never wholesale rewrites. Close each revision with a mechanical
sanity pass (e.g. `grep -oE "FR-[0-9]+\.[0-9]+" report.html | sort -u` to confirm
requirement IDs, placeholder grep, link check).
