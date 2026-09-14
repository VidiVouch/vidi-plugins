# Screenshot verification — the light/dark tripwires

Screenshotting a themed page (for a gallery doc, a design review, a regression check)
has several failure modes that **all produce the same symptom** — both PNGs come out
light, often byte-identical — and none of them raise an error. Treat this as a
checklist, not background reading.

## The four traps

1. **A no-flash bootstrap script re-derives the theme at render time.** If the page
   (e.g. a real app template) carries its own theme bootstrap (`?theme` → localStorage →
   cookie → `prefers-color-scheme`), headless Chrome has none of those set, falls
   through to `prefers-color-scheme` (light), and **silently overwrites** whatever
   `data-theme` you pinned. Fix: **strip the bootstrap `<script>` entirely** (regex-delete
   it from the rendered HTML) before pinning the theme yourself.

2. **Literal-string pinning silently no-ops.** `html.replace('<html lang="en">', ...)`
   does nothing if the real attribute differs at render time (test settings rendering
   `lang="en-us"`, an extra class, anything). Same symptom as trap 1, so it gets
   misdiagnosed as trap 1 recurring. Pin with a regex and **assert the count**:
   ```python
   html, n = re.subn(r'<html\b([^>]*)>', rf'<html\1 data-theme="{theme}">', html, count=1)
   assert n == 1, "data-theme pin did not land"
   ```

3. **Entrance animations screenshot mid-transition.** `animation: rise .4s both` holds
   `opacity: 0` until the animation completes; headless capture can fire first, giving a
   blank hero that looks like a render bug. `prefers-reduced-motion` does NOT save you —
   headless Chrome doesn't report it. Inject a settling override into the CSS before
   shooting:
   ```css
   * { animation: none !important; transition: none !important; }
   .rise { opacity: 1 !important; transform: none !important; }
   ```
   Compounding trap: a blank-hero dark shot can make an all-light run look like
   "light ≠ dark, good" when actually both are light and one is mid-animation.

4. **`file://` navigation can fail under automation**: URLs get mangled
   (`https://file///...`) or load sandboxed without executing. Serve only the report/shot
   folder with `python -m http.server 8089 --bind 127.0.0.1 --directory <report-dir>` and
   navigate to `http://127.0.0.1:8089/report.html`. Opening a localhost URL does not by
   itself restrict the server's bind address. Stop your server after verification;
   see `references/generation.md` for the serving and occupied-port rules.

## The two-fact verification

Because the traps mask each other, verify BOTH facts independently, every run:

```bash
md5 -q light.png dark.png    # must differ
```

AND read the dark PNG (use the plain Read tool — it renders images; don't route /tmp
shots through cwd-scoped image tools) and confirm the page chrome/paper actually reads
dark. Either check alone can pass while the other silently fails.

## Also, before presenting any build for review

- Smoke-click every nav target — dead links get caught in the first minute of review.
- Sweep for leftover branding from any inherited codebase (old product names, wrong
  logo) — these recur; grep the whole tree, don't spot-fix.
- Debug-only chrome (localhost URL badges etc.) must be gated behind a debug flag,
  never always-on.

## Harness hygiene

Screenshot harness files (e.g. `test_shots.py` rendering real templates through the
test client with inlined CSS) are build tools, not test suites — delete them before
committing.
