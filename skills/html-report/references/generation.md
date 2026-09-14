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

Deliberate folder delivery is an exception: full-resolution screenshots or a vendored
heavy runtime may use relative paths when the deliverable is the whole folder. Zip it,
test the extracted bundle, and include serving instructions when needed. Do not leave
loose dependencies behind while claiming the HTML alone is shareable.

### Heavy runtimes

Inline remains the default, not a size-free choice. For static diagrams, prefer rendering
SVG at build time and embedding the inspected result; see `references/diagrams.md`.
If a browser runtime such as Mermaid is actually needed:

- **One file:** inline a pinned, tested classic-script bundle, retain its license notice,
  and accept the measured file size. Bundle size depends on version/build; do not promise
  a fixed megabyte cost. Avoid builds that load additional chunks, fonts or assets.
- **Folder delivery:** vendor the pinned bundle alongside the report, using relative
  paths. This avoids duplicating it across reports but requires distributing the bundle.
  Test its loading mechanism in the intended viewer; use loopback HTTP if needed.

Neither runtime option draws a diagram in a JS-disabled viewer; keep a readable caption
or source fallback. Do not upload private diagrams to online renderers. Keep rendering
local and vendor any runtime instead of depending on a CDN.

## Serving

- **Manual local viewing:** start with `open report.html` for a self-contained report,
  then verify the actual browser. Ordinary sibling classic scripts and images can load
  under `file://`; module scripts, fetch and local-file origin policies behave differently.
  A missing asset path is not evidence of a CORS failure. Choose HTTP based on the loading
  mechanism and viewer, not the number of files.
- **Automated browser/screenshot loops or reports needing HTTP:** use
  `python -m http.server 8089 --bind 127.0.0.1 --directory <report-dir>`, then
  `http://127.0.0.1:8089/report.html`. Serve only the report directory, not a workspace
  containing unrelated private files. Hand over both the command and URL when required.
  If a browser is unavailable, report verification as blocked, not passed.
- **Live feedback loop:** use a loopback-bound server that regenerates the page per
  `GET /` and persists `POST /api/feedback` to disk (see `references/interactivity.md` §3).
  If the port is occupied, choose another or stop only a process you started and own;
  do not kill an unknown listener. Stop your temporary server after verification.

## Distribution triage — decide this BEFORE polishing the tech

Ask where the file will actually be viewed:

| Destination | What works |
|---|---|
| This machine, this user | Self-contained file, verify with `open`; loopback HTTP when needed |
| Email/zip to a colleague (desktop) | Self-contained file or tested relative-path bundle, zipped with serving instructions when needed |
| Phone via iMessage/AirDrop | **Self-contained HTML is a dead end** — iOS Quick Look previews `.html` without executing any JS. No packaging trick fixes this. Host it and send a link |
| Hosted (Cloudflare Pages etc.) | Works everywhere — but anyone with the link can view. Flag that explicitly before deploying; sensitive content doesn't ship this way |
| GitHub README/issue | `claude-cli://` links get stripped; show raw URLs in code blocks |

Related hard-won rules:
- No CDN anything (fonts, libs) and no ES-module imports for the self-contained default;
  these dependencies can fail on restricted networks and in-app browsers. Prefer inline
  classic scripts, or the explicit vendored-bundle path above, and verify the actual
  viewer. Packaging cannot enable JavaScript in a viewer that disables it.
- Interactive/heavy viewers need a **staged diagnostic overlay** ("Decompressing… →
  Building scene… → Rendering…") so a failure is a message, not a silent blank page —
  a remote user can then tell you exactly which stage hung.

## Long-lived documents

Reports that act as a running source of truth (scope docs, audits updated over weeks):
give every section and requirement row a stable `id`, then patch with small targeted
edits as facts change — never wholesale rewrites. Close each revision with a mechanical
sanity pass (e.g. `grep -oE "FR-[0-9]+\.[0-9]+" report.html | sort -u` to confirm
requirement IDs, placeholder grep, link check).
