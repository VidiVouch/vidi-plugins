# Generating, serving, and shipping HTML reports

For anything beyond a hand-written page: re-runnable generators, validation, and the
distribution traps that have actually burned us.

## Generator scripts: placeholder + validate

When a report embeds real data (hundreds of rows, multiple sources), write a
re-runnable script (`generate_report.py`) rather than hand-writing the HTML. The proven
shape:

1. Keep the full HTML as a template string with literal placeholders:
   `__DATA__`, `__META__`, `__GEN__` (generation timestamp), each used once in this recipe.
   These slots hold complete JSON values inside a `<script>`, such as `const DATA = __DATA__;`, not quoted
   strings, HTML attributes or visible HTML text.
2. Serialize for the HTML context, then substitute slots in one pass. JSON serialization
   alone does not stop a value containing `</script>` from closing the element. A single
   substitution pass also preserves marker-like text inside input data.
   ```python
   import json
   import re

   def json_for_html(value):
       return json.dumps(value).replace("<", "\\u003c")

   values = {"__DATA__": rows, "__META__": meta, "__GEN__": gen_stamp}
   assert all(TEMPLATE.count(slot) == 1 for slot in values), "missing or duplicate slot"
   html = re.sub(r"__(?:DATA|META|GEN)__",
                 lambda match: json_for_html(values[match.group()]), TEMPLATE)
   ```
3. **Validate after every regeneration**: parse embedded data and check executable JS.
   For the single-line `const DATA = ...;` form above (terminated by a newline):
   ```python
   import pathlib

   out = pathlib.Path("report.html").read_text()
   assert json.loads(re.search(r"const DATA = (.*?);\n", out).group(1)) == rows
   ```
   Check metadata and timestamp bindings the same way. Do not reject marker-like text
   in the rendered output if it came from the input; validate slots in the template.
   Extract executable `<script>` bodies and run `node --check` on them; parse JSON data
   script elements with `json.loads` instead.

**Raw strings, always.** Make the template a raw string (`r"""…"""`). A non-raw Python
template once turned the `\n` inside embedded JS (`.replace(/\n/g,' ')`) into literal
newlines and corrupted the script. Any generator language that processes backslashes has
this trap.

## Embedding files and data

Everything inlines into the one file — no loose assets to lose:

- Structured data: embed `json_for_html(...)` output from the recipe above as a complete
  JSON value in a `<script>` tag; client JS renders from it. This includes non-executing
  `type="application/json"` data elements, which still need the same HTML-boundary escaping.
  No fetch/AJAX for report data.
- Text files: inline as strings using the same serializer when they enter a script.
  Outside scripts, use the appropriate HTML text/attribute escaping instead; JSON
  serialization is not a general-purpose HTML sanitizer.
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

**Mermaid uses jsDelivr by default**, via the pinned classic-script URL in `diagrams.md`.
Do not inline or vendor it merely to keep the report in one file. The HTML still embeds
its data and diagram source, but rendering needs network access; disclose that dependency
and keep a readable fallback for CDN failures. Rendering happens in the browser, not an
online rendering service. Loading third-party JavaScript still grants it access to the
page; use inspected inline SVG instead when offline or no-third-party-code delivery is required.

For offline diagrams, render SVG locally at build time and embed the inspected result.
If an offline report needs a browser runtime, or another heavy runtime cannot use a CDN:

- **One file:** inline a pinned, tested classic-script bundle, retain its license notice,
  and accept the measured file size. Bundle size depends on version/build; do not promise
  a fixed megabyte cost. Avoid builds that load additional chunks, fonts or assets.
- **Folder delivery:** vendor the pinned bundle alongside the report, using relative
  paths. This avoids duplicating it across reports but requires distributing the bundle.
  Test its loading mechanism in the intended viewer; use loopback HTTP if needed.

No runtime option draws a diagram in a JS-disabled viewer; keep a readable caption
or source fallback. Do not upload private diagrams to online renderers.

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
- No CDN fonts or other libraries and no ES-module imports for the self-contained default;
  the pinned Mermaid CDN runtime is the explicit exception above. Verify the actual viewer
  with the CDN available and blocked; do not promise offline diagrams without an inline SVG
  fallback or an offline bundle. Packaging cannot enable JavaScript in a viewer that disables it.
- Interactive/heavy viewers need a **staged diagnostic overlay** ("Decompressing… →
  Building scene… → Rendering…") so a failure is a message, not a silent blank page —
  a remote user can then tell you exactly which stage hung.

## Long-lived documents

Reports that act as a running source of truth (scope docs, audits updated over weeks):
give every section and requirement row a stable `id`, then patch with small targeted
edits as facts change — never wholesale rewrites. Close each revision with a mechanical
sanity pass (e.g. `grep -oE "FR-[0-9]+\.[0-9]+" report.html | sort -u` to confirm
requirement IDs, placeholder grep, link check).
