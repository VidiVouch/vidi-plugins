# Theming

The one pattern that makes light/dark work everywhere — OS preference, manual toggle,
screenshot pinning, print — without flashes or split stylesheets.

## The token cascade

Define every color as a custom property. Style components **only through tokens**.
Redefine tokens in exactly four places, in this order:

```css
:root {                                   /* 1. light defaults (cyanotype) */
  --paper: #eef4f8;  --ink: #0d2438;  --accent: #1c7aad;
}
@media (prefers-color-scheme: dark) {     /* 2. OS preference (no-JS fallback) */
  :root { --paper: #091522; --ink: #dde8f4; --accent: #38b0e8; }
}
:root[data-theme="dark"] {                /* 3. explicit dark — beats the media query */
  --paper: #091522; --ink: #dde8f4; --accent: #38b0e8;
}
:root[data-theme="light"] {               /* 4. explicit light — beats a dark-OS media query */
  --paper: #eef4f8; --ink: #0d2438; --accent: #1c7aad;
}
```

These are the cyanotype defaults the utilitarian templates ship (full palette in
`assets/report.html`). The editorial template (`assets/report-editorial.html`) runs a
teal instrument variant through the identical cascade — same four blocks, different hex.
An editorial page is allowed to commit to its own visual world; a utilitarian doc should
match the project's existing tokens.

Blocks 3 and 4 restate 2 and 1 — that redundancy is the point: a stamped `data-theme`
attribute must win over the OS preference *in both directions*. Never split light/dark
into separate files, and never rely on the media query alone for anything that needs a
manual toggle or a pinnable screenshot state.

One deliberate exception: header/banner "chrome" may keep constant colors across both
themes (a dark banner that stays dark) — that's a choice, declare those tokens once.

## No-flash bootstrap

A synchronous `<script>` in `<head>` (never `defer`/`async` — that's what causes the
flash), resolution order **`?theme=` param → localStorage → OS preference**:

```html
<script>
(function () {
  var q = new URLSearchParams(location.search).get('theme');
  var s = null; try { s = localStorage.getItem('theme'); } catch (e) {}
  var os = matchMedia('(prefers-color-scheme: dark)').matches ? 'dark' : 'light';
  var t = (q === 'light' || q === 'dark') ? q
        : (s === 'light' || s === 'dark') ? s : os;
  document.documentElement.setAttribute('data-theme', t);
})();
</script>
```

The `?theme=` param comes first so a URL can pin the theme — that's what makes
screenshot harnesses and shareable "look at the dark version" links work.

## Toggle button

Floating, fixed bottom-right, with a real `aria-label` that updates on toggle. Follow
the OS live **only while the user hasn't explicitly chosen**:

```html
<script>
(function () {
  var root = document.documentElement;
  var btn = document.createElement('button');
  btn.className = 'theme-toggle'; btn.textContent = '◐';
  function label() {
    var d = root.getAttribute('data-theme') === 'dark';
    btn.setAttribute('aria-label', d ? 'Switch to light theme' : 'Switch to dark theme');
    btn.title = btn.getAttribute('aria-label');
  }
  btn.addEventListener('click', function () {
    var t = root.getAttribute('data-theme') === 'dark' ? 'light' : 'dark';
    root.setAttribute('data-theme', t);
    try { localStorage.setItem('theme', t); } catch (e) {}
    label();
  });
  label(); document.body.appendChild(btn);
  matchMedia('(prefers-color-scheme: dark)').addEventListener('change', function (e) {
    var s = null; try { s = localStorage.getItem('theme'); } catch (err) {}
    if (!s) { root.setAttribute('data-theme', e.matches ? 'dark' : 'light'); label(); }
  });
})();
</script>
```

## Print and reduced motion — standard boilerplate

```css
@media print {          /* print is always on paper: force light tokens */
  :root, :root[data-theme="dark"] {
    --paper: #ffffff; --ink: #0d2438; /* …restate all light tokens… */
  }
  .theme-toggle { display: none; }
}
@media (prefers-reduced-motion: reduce) {
  * { animation: none !important; transition: none !important; }
}
```

## Overriding tokens from the environment

`render_note.py` reads optional token overrides from the environment, so a report can
pick up your palette without editing the renderer. Set these in Claude Code's
`settings.json` `env` block (they become session env vars the script reads), your
`.envrc`, or inline. All optional — any unset token keeps the cyanotype default.

```bash
HTMLREPORT_ACCENT=#7a5cff            # --accent, both themes
HTMLREPORT_LIGHT_PAPER=#fffdf7       # --paper, light only
HTMLREPORT_DARK_ACCENT_2=#9d84ff     # --accent-2, dark only  (ACCENT_2 → --accent-2)
HTMLREPORT_THEME_JSON='{"light":{"--accent":"#0a7"},"dark":{"--accent":"#3d9"}}'   # or @/path/to.json
```

Naming: `HTMLREPORT_[LIGHT_|DARK_]<TOKEN>`, where `<TOKEN>` lowercases and turns `_` into
`-` (`INK_2` → `--ink-2`). No `LIGHT_`/`DARK_` prefix means both themes. The overrides are
emitted after the base cascade under `:root`, the dark media query, and both
`:root[data-theme]` scopes, so they win in every path (OS preference and manual toggle).
Values are sanitized — anything containing `; { } < >` is rejected, so an env var can't
inject arbitrary CSS. This is a light per-session override; for a full project palette,
still prefer the project's own token file (e.g. `design/assets/cyanotype.css`).

## Palette rules

- Honor the project's existing token file over any defaults here.
- Neutrals are chosen, not defaulted: bias greys slightly toward the accent hue.
- Semantic status colors (ok/warn/critical) are a separate reserved set — never reuse
  one as an extra series/accent color.
- Avoid the AI-design tells: warm cream + terracotta, indigo/violet "LLM purple",
  purple→blue gradient heroes. If a warm caution hue is needed, amber/ochre — not orange.

## Contrast: use the real math

When ink color is picked against a data-driven background, use WCAG relative luminance —
the true black/white crossover is **L = 0.1791**, not a naive brightness cutoff. A crude
`brightness > 140` check has picked white-on-teal at 2.1:1 (fails) when black gave 8.2:1.

```js
function relLum(hex) {
  const c = [1, 3, 5].map(i => parseInt(hex.slice(i, i + 2), 16) / 255)
    .map(v => v <= 0.03928 ? v / 12.92 : Math.pow((v + 0.055) / 1.055, 2.4));
  return 0.2126 * c[0] + 0.7152 * c[1] + 0.0722 * c[2];
}
const ink = relLum(bg) > 0.1791 ? '#000' : '#fff';
```

When this class of bug is found once, audit every ink-on-state-color site, not just the
reported one.
