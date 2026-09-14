# Diagrams in HTML reports

Use a diagram when the reader needs to trace a path, see ownership or understand
relationships that prose obscures. Use a table for exact values and prose for a simple
list. Pick ER notation for entities/cardinality and a flowchart for process/control flow;
color is not the deciding factor. Keep labels and a text explanation alongside the graph.

## Choose the rendering path

**Static report: prefer pre-rendered inline SVG.** Author a small diagram directly as
SVG, or render Mermaid/another diagram source locally at build time. Embed the inspected
SVG in the hand-authored report. This keeps the delivered file offline-capable and avoids
shipping a parser/runtime just to draw a fixed picture. Retain editable source with the
generator or in an escaped code/details section; do not make later changes depend on
reverse-engineering paths. Check both themes after embedding, not only the source preview.

**Runtime rendering: use it only when needed.** If the reader edits diagram source or the
graph changes in the browser, a runtime can be justified. Pin a tested library version
and build, retain its license, and record them with the source. Inline a classic-script
bundle by default; avoid builds with network-loaded chunks or fonts. A vendored sibling
is an explicit folder-delivery choice, not a self-contained file. See
`generation.md` for size tradeoffs, relative paths and loopback serving.

The JSON renderer currently has no `diagram` block; `chart` is also only a placeholder.
Prefer hand-authored HTML for static diagrams, not an invented JSON type. For sandboxed
fragments in JSON notes, see the theme and fallback limits in `content-model.md`.
A reusable block would need separate validation, HTML and Markdown behavior, theme
handling and live-swap initialization, including a graph introduced into an initially
diagram-free page.

## Worked static SVG: source to report

This small authoring-flow example uses the host's ink token, native SVG shapes and
text. It needs no library, script or network resource. Place it in a report using
`theming.md`; transparent node interiors preserve the host's light or dark surface.
Give every copied instance unique title/description IDs and update its labels and caption.

```html
<style>
  .diagram { margin: 1.5rem 0; color: var(--ink, #0d2438); }
  .diagram svg { display: block; width: 100%; max-width: 20rem; height: auto; }
  .diagram rect { fill: none; stroke: currentColor; }
  .diagram path { fill: none; stroke: currentColor; stroke-width: 2; }
  .diagram text { fill: currentColor; font: 16px system-ui, sans-serif; }
  .diagram figcaption { max-width: 65ch; }
</style>
<figure class="diagram">
  <svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 320 280"
       role="img" aria-labelledby="build-title build-desc">
    <title id="build-title">Build a static diagram</title>
    <desc id="build-desc">Editable source is rendered or authored as SVG,
      then embedded in the HTML report. Arrows show that order.</desc>
    <rect x="20" y="12" width="280" height="60" rx="6" />
    <text x="160" y="47" text-anchor="middle">Editable source</text>
    <path d="M160 72 V108 M154 100 L160 108 L166 100" />
    <rect x="20" y="110" width="280" height="60" rx="6" />
    <text x="160" y="145" text-anchor="middle">Inspect SVG</text>
    <path d="M160 170 V206 M154 198 L160 206 L166 198" />
    <rect x="20" y="208" width="280" height="60" rx="6" />
    <text x="160" y="243" text-anchor="middle">Embed in report</text>
  </svg>
  <figcaption>Keep the editable source, inspect the SVG, then embed it in the report.
    The SVG inherits the report's colors; no client-side rendering is needed.</figcaption>
</figure>
```

For a larger graph, split it into readable subgraphs or provide an intentional zoom/pan
view with a text alternative. Merely shrinking a wide SVG to phone width makes labels
unreadable. Avoid `foreignObject`/HTML labels when plain SVG text is sufficient. Verify
exported SVG really inherits tokens: Mermaid-generated internal styles and hardcoded
fills may override host CSS. Use a tested CSS adaptation or pre-render both themes;
keep only the active variant exposed to assistive technology.

## Mermaid compatibility and lifecycle

Current [Mermaid ER documentation](https://mermaid.js.org/syntax/entityRelationshipDiagram.html)
supports `style` and `classDef`, including hex fills. Do not teach that ER diagrams must
become flowcharts to support color. For a parse error, record the exact runtime version,
diagram source and error, then test a minimal example against that pinned version's
syntax. Latest online docs may describe features absent from an older vendored build.

When using a browser runtime:

- Keep original source separate from rendered DOM. Rendering can replace the source
  container; re-render from the source, not from an already-generated SVG.
- Initialize only after the runtime is available. Catch load/parse/render failures and
  display a local error message plus the textual explanation or escaped source. Keep
  that fallback readable if scripts never run; do not hide all content at startup.
- Integrate with the report's actual `data-theme` toggle, not just OS preference. Apply
  tested theme variables and re-render if required. Serialize/coalesce updates or use a
  generation guard so a slow old render cannot overwrite a newer theme/source result.
- Replace old output rather than appending duplicates. For dynamically inserted sections,
  invoke initialization explicitly: inserting HTML containing a script is not a reliable
  way to execute that script. Do not claim the current JSON live server does this.

## Trust boundaries

SVG is markup, not inert image data. Script tags, event attributes, links, external
resources and HTML inside SVG can change its behavior. Directly embed only trusted,
inspected output produced with a pinned renderer and restrictive security settings.
For untrusted input, use a maintained SVG-aware sanitizer with a restrictive allowlist
or fall back to a raster image plus text; HTML escaping or a regex is not SVG sanitization.
Do not loosen Mermaid security settings or set a live fragment's `sandbox: false` just
to make a diagram work. A sandbox is not a substitute for input validation or network
isolation. Keep private diagrams local; do not upload them to an online renderer.

Escape source when placing it in HTML. Use `textContent` for dynamic text. For embedded
JSON, encode literal less-than characters as JSON Unicode escapes after serialization
so a closing script tag in the source cannot terminate the data element. Do not interpolate raw source into a
script, HTML attribute, CSS rule or `innerHTML`.

## Verify the delivered diagram

- Compare expected diagrams with actual output. Each needs meaningful nodes/edges and
  visible, nonzero geometry; an `<svg>` alone proves nothing because error output may
  also be SVG. Check parse errors, error nodes and the console for runtime failures.
- Inspect labels, arrows and relationship/cardinality meaning, not just the container.
  Confirm no clipped content, duplicate IDs/output or unreadable labels at narrow widths.
- Check light and dark, including manual toggles. Use `screenshot-verify.md` when taking
  screenshots; verify the diagram itself changed appropriately, not only the page.
- Keep a visible caption/explanation that conveys the relationships without color or JS.
  Use an accessible SVG title/description; preserve keyboard access for interactive graphs.
- For runtime diagrams, deliberately test invalid source or an unavailable runtime and
  confirm a visible fallback. For build-time rendering, fail generation visibly rather
  than shipping an empty/error SVG as a successful report.
- Test the actual offline file or extracted bundle in its intended viewer. For automated
  browser checks use the loopback server in `generation.md`. Static syntax/XML checks
  do not prove visual success; explicitly report blocked browser checks.
