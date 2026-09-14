#!/usr/bin/env python3
"""render_note.py — deterministic JSON note → self-contained HTML.

The whole point: structure, theming, and interactivity scaffolding cost ZERO model
tokens. You author the note as JSON (see references/content-model.md); this renders it.
Same JSON → static file, live-served page, or (via --format md) an agent-readable digest.

Usage:
    python render_note.py note.json > note.html          # static, self-contained
    python render_note.py note.json --live > note.html    # include the live action client
    python render_note.py note.json --format md           # flatten to markdown
    python render_note.py note.json --block <id>          # render one block (fragment swap)

No third-party deps. Text is escaped; only prose markdown and explicit `fragment` nodes
emit markup, and subagent fragments are sandboxed in an iframe.
"""

import argparse
import html as _html
import json
import os
import re
import sys
from pathlib import Path

# ---------------------------------------------------------------- theme (cyanotype)
# One token cascade: :root light -> @media dark (no-JS) -> [data-theme] wins both ways.
THEME_CSS = r"""
:root{
  color-scheme:light;
  --paper:#eef4f8; --surface:#ffffff; --surface-2:#f3f8fb; --ink:#0d2438; --ink-2:#3a5b76; --ink-3:#6f93ad;
  --line:#d3e1ec; --line-2:#bcd2e2; --pb-100:#e8f0f8; --pb-500:#1f5a94; --pb-700:#123a63; --pb-900:#0b1d33;
  --chrome:#0b1d33; --chrome-ink:#eaf2fa;
  --accent:#1c7aad; --accent-2:#38b0e8; --accent-wash:#e2f1fb;
  --ok:#1e6b45; --ok-bg:#e2f2e9; --warn:#8a5a00; --warn-bg:#fdf3e0; --warn-line:#e6b23e;
  --crit:#b3243c; --crit-bg:#fdeaee; --neutral:#3a5b76; --neutral-bg:#eaf1f6;
  --mono:'Maple Mono','JetBrains Mono',ui-monospace,'SF Mono',Menlo,monospace;
  --sans:'Hanken Grotesk',-apple-system,BlinkMacSystemFont,'Segoe UI',Roboto,sans-serif;
  --maxw:1100px;
}
@media (prefers-color-scheme:dark){:root{
  --paper:#091522; --surface:#102338; --surface-2:#0c1c2e; --ink:#dde8f4; --ink-2:#90a9c4; --ink-3:#647e98;
  --line:#20405e; --line-2:#2a557e; --pb-100:#14293f; --pb-500:#5b96c8; --pb-700:#a8c9e6; --pb-900:#e6eef8;
  --accent:#38b0e8; --accent-2:#5cc8f5; --accent-wash:#12314a;
  --ok:#4fc08d; --ok-bg:#0e2b1f; --warn:#e6b23e; --warn-bg:#2d2410; --warn-line:#a97f24;
  --crit:#f0808f; --crit-bg:#331520; --neutral:#90a9c4; --neutral-bg:#152736;
}}
:root[data-theme="light"]{color-scheme:light;
  --paper:#eef4f8; --surface:#ffffff; --surface-2:#f3f8fb; --ink:#0d2438; --ink-2:#3a5b76; --ink-3:#6f93ad;
  --line:#d3e1ec; --line-2:#bcd2e2; --pb-100:#e8f0f8; --pb-500:#1f5a94; --pb-700:#123a63; --pb-900:#0b1d33;
  --accent:#1c7aad; --accent-2:#38b0e8; --accent-wash:#e2f1fb;
  --ok:#1e6b45; --ok-bg:#e2f2e9; --warn:#8a5a00; --warn-bg:#fdf3e0; --warn-line:#e6b23e;
  --crit:#b3243c; --crit-bg:#fdeaee; --neutral:#3a5b76; --neutral-bg:#eaf1f6;}
:root[data-theme="dark"]{color-scheme:dark;
  --paper:#091522; --surface:#102338; --surface-2:#0c1c2e; --ink:#dde8f4; --ink-2:#90a9c4; --ink-3:#647e98;
  --line:#20405e; --line-2:#2a557e; --pb-100:#14293f; --pb-500:#5b96c8; --pb-700:#a8c9e6; --pb-900:#e6eef8;
  --accent:#38b0e8; --accent-2:#5cc8f5; --accent-wash:#12314a;
  --ok:#4fc08d; --ok-bg:#0e2b1f; --warn:#e6b23e; --warn-bg:#2d2410; --warn-line:#a97f24;
  --crit:#f0808f; --crit-bg:#331520; --neutral:#90a9c4; --neutral-bg:#152736;}

*{box-sizing:border-box}
body{margin:0;background:var(--paper);color:var(--ink);font:16px/1.6 var(--sans);
  -webkit-font-smoothing:antialiased}
.wrap{max-width:var(--maxw);margin:0 auto;padding:24px 32px 96px}
a{color:var(--accent)} h1,h2,h3{text-wrap:balance}
code,.mono{font-family:var(--mono);font-size:.86em;background:var(--pb-100);color:var(--pb-700);
  padding:.06em .38em;border-radius:4px}
.eyebrow{font-family:var(--mono);font-size:11.5px;letter-spacing:.16em;text-transform:uppercase;color:var(--ink-3)}

.hero{padding:56px 0 36px;border-bottom:1px solid var(--line)}
.hero .eyebrow{margin-bottom:18px}
.hero h1{font-size:clamp(30px,5.5vw,52px);font-weight:750;letter-spacing:-.02em;margin:0}
.hero h1 .hl{color:var(--accent)}
.hero .lede{max-width:60ch;margin-top:18px;font-size:18px;color:var(--ink-2)}
.flow{font-family:var(--mono);font-size:14px;margin-top:26px;display:flex;flex-wrap:wrap;align-items:center;gap:9px 12px}
.flow b{background:var(--surface);border:1px solid var(--line);border-radius:8px;padding:6px 12px;font-weight:600}
.flow .arw{color:var(--ink-3)}

.stats{display:grid;grid-template-columns:repeat(auto-fit,minmax(150px,1fr));gap:1px;background:var(--line);
  border:1px solid var(--line);border-radius:12px;overflow:hidden;margin:32px 0}
.stat{background:var(--surface);padding:16px 18px}
.stat .v{font-family:var(--mono);font-size:26px;font-weight:700;font-variant-numeric:tabular-nums}
.stat .k{font-size:12.5px;color:var(--ink-2);margin-top:3px}
.stat .ev{font-size:11.5px;color:var(--ink-3);margin-top:4px}

section.blk{padding:34px 0;border-bottom:1px solid var(--line)}
.sec-head{display:flex;align-items:baseline;gap:14px}
.sec-head h2{font-size:clamp(20px,3vw,27px);font-weight:680;letter-spacing:-.01em;margin:0}
.sec-num{font-family:var(--mono);color:var(--accent);font-size:14px;font-weight:600}
.sec-sub{color:var(--ink-2);max-width:66ch;margin:6px 0 20px}
p.prose{max-width:66ch}

.callout{border-radius:12px;padding:16px 20px;margin:20px 0;border:1px solid var(--line)}
.callout .h{font-family:var(--mono);font-size:11px;letter-spacing:.1em;text-transform:uppercase;font-weight:700;margin-bottom:6px}
.callout.info{background:var(--accent-wash);border-color:color-mix(in srgb,var(--accent) 30%,transparent)}
.callout.info .h{color:var(--accent)}
.callout.ok{background:var(--ok-bg)} .callout.ok .h{color:var(--ok)}
.callout.warn{background:var(--warn-bg);border-color:color-mix(in srgb,var(--warn-line) 40%,transparent)} .callout.warn .h{color:var(--warn)}
.callout.crit{background:var(--crit-bg)} .callout.crit .h{color:var(--crit)}

.tablewrap{overflow-x:auto;margin:16px 0;border:1px solid var(--line);border-radius:10px}
table{border-collapse:collapse;width:100%;font-size:14px;background:var(--surface)}
th{text-align:left;font-family:var(--mono);font-size:11px;letter-spacing:.06em;text-transform:uppercase;color:var(--ink-2);
  background:var(--surface-2);padding:9px 12px;border-bottom:1px solid var(--line-2)}
td{padding:8px 12px;border-bottom:1px solid var(--line);vertical-align:top}
tbody tr:last-child td{border-bottom:none}
/* Narrow viewports: stack rows into labeled cards — embedded viewers clip instead of
   scrolling. .wide (>=5 columns) stacks earlier because it needs more room to fit. */
@media (max-width:760px){
  .tablewrap{overflow-x:visible;border:none;border-radius:0}
  .tablewrap thead{display:none}
  .tablewrap table,.tablewrap tbody,.tablewrap tr,.tablewrap td{display:block;background:none}
  .tablewrap tr{background:var(--surface);border:1px solid var(--line);border-radius:10px;padding:6px 14px;margin-top:10px}
  .tablewrap td{border:none;padding:6px 0;overflow-wrap:anywhere}
  .tablewrap td[data-th]::before{content:attr(data-th);display:block;font-family:var(--mono);font-size:10.5px;
    letter-spacing:.06em;text-transform:uppercase;color:var(--ink-2)}
}
@media (max-width:1100px){
  .tablewrap.wide{overflow-x:visible;border:none;border-radius:0}
  .tablewrap.wide thead{display:none}
  .tablewrap.wide table,.tablewrap.wide tbody,.tablewrap.wide tr,.tablewrap.wide td{display:block;background:none}
  .tablewrap.wide tr{background:var(--surface);border:1px solid var(--line);border-radius:10px;padding:6px 14px;margin-top:10px}
  .tablewrap.wide td{border:none;padding:6px 0;overflow-wrap:anywhere}
  .tablewrap.wide td[data-th]::before{content:attr(data-th);display:block;font-family:var(--mono);font-size:10.5px;
    letter-spacing:.06em;text-transform:uppercase;color:var(--ink-2)}
}

.cards{display:grid;grid-template-columns:repeat(auto-fit,minmax(300px,1fr));gap:16px;margin:16px 0}
.card{background:var(--surface);border:1px solid var(--line);border-radius:12px;padding:18px 18px 20px;display:flex;flex-direction:column;gap:11px}
.card-top{display:flex;justify-content:space-between;align-items:flex-start;gap:10px}
.card h3{margin:0;font-size:16.5px;font-weight:640}
.card .idx{font-family:var(--mono);font-size:12px;color:var(--ink-3)}
.chips{display:flex;flex-wrap:wrap;gap:6px}
.chip{font-family:var(--mono);font-size:11px;padding:2px 8px;border-radius:20px;background:var(--surface-2);border:1px solid var(--line);color:var(--ink-2)}
.kv{display:grid;grid-template-columns:auto 1fr;gap:4px 12px;font-size:13.5px;margin:0}
.kv dt{font-family:var(--mono);font-size:11px;letter-spacing:.05em;text-transform:uppercase;color:var(--ink-3);padding-top:2px}
.kv dd{margin:0}

.wire{border:1px solid var(--line);border-radius:10px;overflow:hidden;background:var(--surface-2);margin:14px 0}
.wire .lab{font-family:var(--mono);font-size:10.5px;letter-spacing:.1em;text-transform:uppercase;color:var(--ink-3);
  padding:7px 12px;border-bottom:1px solid var(--line);display:flex;justify-content:space-between}
.wire .lab .verb{color:var(--accent);font-weight:700}
pre{margin:0;padding:12px;overflow-x:auto;font-family:var(--mono);font-size:12.5px;line-height:1.55}

.ladder{display:flex;flex-direction:column;border:1px solid var(--line);border-radius:12px;overflow:hidden;background:var(--surface);margin:14px 0}
.rung{display:grid;grid-template-columns:44px 1fr;border-bottom:1px solid var(--line)}
.rung:last-child{border-bottom:none}
.rung .no{font-family:var(--mono);font-weight:700;color:var(--accent);display:grid;place-items:center;background:var(--surface-2);border-right:1px solid var(--line)}
.rung .b{padding:12px 15px} .rung .b .t{font-weight:600;font-size:14.5px} .rung .b .d{font-size:13px;color:var(--ink-2);margin-top:2px}

.verdict{background:var(--surface);border:1px solid var(--line);border-left:4px solid var(--accent);border-radius:8px;padding:16px 20px;margin:14px 0}
.verdict h3{margin:0 0 4px;font-size:17px}
.verdict .meta{font-family:var(--mono);font-size:12px;color:var(--ink-3);margin-bottom:10px}
.verbs{display:flex;gap:8px;flex-wrap:wrap;margin-top:12px}
.verbs button{font:inherit;font-size:13px;padding:6px 13px;border-radius:6px;border:1.5px solid var(--line-2);background:var(--surface);color:var(--ink-2);cursor:pointer}
.verbs button:hover{border-color:var(--accent)}
.verbs button.sel{border-color:var(--accent);background:var(--accent);color:#fff;font-weight:600}

.actions{display:flex;gap:8px;flex-wrap:wrap;margin-top:14px}
.actions button{font:inherit;font-size:13px;padding:6px 13px;border-radius:6px;border:1px solid var(--accent);background:none;color:var(--accent);cursor:pointer}
.actions button:hover{background:var(--accent-wash)}
.actions button[data-kind="handoff"]{border-style:dashed}
.actions button:disabled{opacity:.55;cursor:default}
.act-status{font-size:12px;color:var(--ink-3);font-family:var(--mono);align-self:center}

.fragment{border:1px dashed var(--line-2);border-radius:10px;margin:14px 0;overflow:hidden}
.fragment > .flab{font-family:var(--mono);font-size:10px;letter-spacing:.1em;text-transform:uppercase;color:var(--ink-3);padding:5px 10px;border-bottom:1px dashed var(--line-2)}
.fragment iframe{width:100%;border:0;display:block}

.chart-box{border:1px solid var(--line);border-radius:10px;background:var(--surface);padding:16px;margin:14px 0;color:var(--ink-3);font-size:13px}

/* subagent-expanded blocks — visually distinct from authored content */
.gen{border-left:3px solid var(--accent-2);background:var(--accent-wash);border-radius:0 8px 8px 0;padding:2px 16px;margin:14px 0;animation:gen-in .5s ease both}
.gen-lab{font-family:var(--mono);font-size:10px;letter-spacing:.12em;text-transform:uppercase;color:var(--accent);margin:10px 0 0;display:flex;align-items:center;gap:6px}
.gen-lab::after{content:"";flex:1;height:1px;background:color-mix(in srgb,var(--accent) 30%,transparent)}
@keyframes gen-in{from{opacity:0;transform:translateY(6px)}to{opacity:1;transform:none}}
@media (prefers-reduced-motion:reduce){.gen{animation:none}}

.theme-toggle{position:fixed;right:16px;bottom:16px;z-index:99;width:38px;height:38px;border-radius:50%;
  display:grid;place-items:center;font:16px/1 var(--mono);color:var(--ink-2);background:var(--surface);border:1px solid var(--line-2);cursor:pointer}
.theme-toggle:hover{color:var(--accent);border-color:var(--accent)}
@media print{.theme-toggle,.actions,.verbs{display:none}}
@media (prefers-reduced-motion:reduce){*{animation:none!important;transition:none!important}}
"""

BOOTSTRAP_JS = """
(function(){var q=new URLSearchParams(location.search).get('theme');var s=null;try{s=localStorage.getItem('theme')}catch(e){}
var os=matchMedia('(prefers-color-scheme:dark)').matches?'dark':'light';
var t=(q==='light'||q==='dark')?q:(s==='light'||s==='dark')?s:os;
document.documentElement.setAttribute('data-theme',t);})();
"""

TOGGLE_JS = """
(function(){var r=document.documentElement,b=document.createElement('button');b.className='theme-toggle';b.textContent='\\u25d0';
function l(){var d=r.getAttribute('data-theme')==='dark';b.setAttribute('aria-label',d?'Switch to light theme':'Switch to dark theme');b.title=b.getAttribute('aria-label')}
b.onclick=function(){var t=r.getAttribute('data-theme')==='dark'?'light':'dark';r.setAttribute('data-theme',t);try{localStorage.setItem('theme',t)}catch(e){}l()};
l();document.body.appendChild(b);
matchMedia('(prefers-color-scheme:dark)').addEventListener('change',function(e){var s=null;try{s=localStorage.getItem('theme')}catch(x){}if(!s){r.setAttribute('data-theme',e.matches?'dark':'light');l()}});})();
"""

# The live-action client: posts an action to the server, then swaps the returned
# section fragment in place. Same DOM-swap idea as HTMX; hand-rolled so it stays
# self-contained (no CDN). Hand-off actions are built client-side, no round-trip.
LIVE_JS = r"""
(function(){
function esc(s){return encodeURIComponent(s||'')}
window.__handoff=function(btn){
  var t=btn.dataset.target,p=btn.dataset.prompt||'',cwd=btn.dataset.cwd||'',prof=btn.dataset.profile||'';
  if(t==='claude'){location.href='claude-cli://open?cwd='+esc(cwd)+'&q='+esc(p);}
  else if(t==='codex'){location.href='codex://threads/new?path='+esc(cwd)+'&prompt='+esc(p);}
  else if(t==='ccs'){var cmd='ccs '+prof+' '+JSON.stringify(p);navigator.clipboard.writeText(cmd).then(function(){setStatus(btn,'copied: '+cmd.slice(0,40)+'…')});}
};
function setStatus(btn,msg){var s=btn.parentNode.querySelector('.act-status');if(s)s.textContent=msg;}
window.__expand=function(btn){
  var bid=btn.dataset.block,ai=btn.dataset.action;
  btn.disabled=true;setStatus(btn,'Claude is working…');
  fetch('/action',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({block_id:bid,action:parseInt(ai,10)})})
   .then(function(r){if(!r.ok)throw new Error('dispatch failed');return r.json()})
   .then(function(res){return fetch('/section/'+encodeURIComponent(bid))})
   .then(function(r){return r.text()})
   .then(function(frag){var el=document.querySelector('[data-block-id="'+bid+'"]');if(el){var tmp=document.createElement('div');tmp.innerHTML=frag;var fresh=tmp.firstElementChild;if(fresh){el.replaceWith(fresh);wire(fresh)}}})
   .catch(function(e){btn.disabled=false;setStatus(btn,'failed — retry')});
};
window.__verdict=function(btn){
  var g=btn.parentNode.querySelectorAll('button');for(var i=0;i<g.length;i++)g[i].classList.remove('sel');btn.classList.add('sel');
  fetch('/verdict',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({item:btn.dataset.item,verb:btn.dataset.verb,bucket:btn.dataset.bucket})}).catch(function(){});
};
function wire(root){
  (root||document).querySelectorAll('button[data-kind="expand"]').forEach(function(b){b.onclick=function(){__expand(b)}});
  (root||document).querySelectorAll('button[data-kind="handoff"]').forEach(function(b){b.onclick=function(){__handoff(b)}});
  (root||document).querySelectorAll('.verbs button').forEach(function(b){b.onclick=function(){__verdict(b)}});
}
wire(document);
})();
"""


def esc(s):
    return _html.escape("" if s is None else str(s), quote=True)


def md_inline(s):
    """Minimal, safe inline markdown: escape first, then re-introduce **bold**, `code`, [t](url)."""
    s = esc(s)
    s = re.sub(r"\*\*(.+?)\*\*", r"<strong>\1</strong>", s)
    s = re.sub(r"`(.+?)`", r"<code>\1</code>", s)

    # links: [text](http/https/relative-safe) — reject javascript: etc.
    def link(m):
        text, url = m.group(1), m.group(2)
        if re.match(r"^(https?:|/|#|mailto:|claude-cli:|codex:)", url):
            return f'<a href="{url}">{text}</a>'
        return text

    return re.sub(r"\[(.+?)\]\(([^)]+)\)", link, s)


# ---------------------------------------------------------------- block renderers
def r_actions(block, live):
    acts = block.get("actions") or []
    if not acts:
        return ""
    bid = block.get("id", "")
    out = ['<div class="actions">']
    for i, a in enumerate(acts):
        kind = a.get("kind", "expand")
        label = esc(a.get("label", "Action"))
        if kind == "handoff":
            out.append(
                f'<button data-kind="handoff" data-target="{esc(a.get("target", ""))}" '
                f'data-profile="{esc(a.get("profile", ""))}" data-cwd="{esc(a.get("cwd", ""))}" '
                f'data-prompt="{esc(a.get("prompt", ""))}">{label}</button>'
            )
        else:
            out.append(f'<button data-kind="expand" data-block="{esc(bid)}" data-action="{i}">{label}</button>')
    out.append('<span class="act-status"></span></div>')
    return "".join(out)


def r_block(b, live):
    t = b.get("type")
    bid = b.get("id")
    gen = bool(b.get("_added"))  # blocks a subagent appended via an expand action
    cls = ' class="gen"' if gen else ""
    wrap_open = f'<div data-block-id="{esc(bid)}"{cls}>' if bid else f"<div{cls}>"
    inner = _RENDERERS.get(t, r_unknown)(b, live) + r_actions(b, live)
    if gen:
        by = esc(b.get("_by", "")) or "subagent"
        inner = f'<div class="gen-lab">↳ expanded by {by}</div>' + inner
    return wrap_open + inner + "</div>"


def r_unknown(b, live):
    return f'<div class="callout warn"><div class="h">unknown block</div>type <code>{esc(b.get("type"))}</code></div>'


def r_hero(b, live):
    title = esc(b.get("title", ""))
    hl = b.get("highlight")
    if hl and hl in b.get("title", ""):
        title = esc(b["title"]).replace(esc(hl), f'<span class="hl">{esc(hl)}</span>', 1)
    parts = ['<header class="hero">']
    if b.get("eyebrow"):
        parts.append(f'<div class="eyebrow">{esc(b["eyebrow"])}</div>')
    parts.append(f"<h1>{title}</h1>")
    if b.get("lede"):
        parts.append(f'<p class="lede">{esc(b["lede"])}</p>')
    flow = b.get("flow") or []
    if flow:
        segs = '<span class="arw">→</span>'.join(f"<b>{esc(x)}</b>" for x in flow)
        parts.append(f'<div class="flow">{segs}</div>')
    parts.append("</header>")
    return "".join(parts)


def r_stat_strip(b, live):
    cells = []
    for s in b.get("stats", []):
        ev = f'<div class="ev">{esc(s["evidence"])}</div>' if s.get("evidence") else ""
        cells.append(
            f'<div class="stat"><div class="v">{esc(s.get("v", ""))}</div><div class="k">{esc(s.get("k", ""))}</div>{ev}</div>'
        )
    return f'<div class="stats">{"".join(cells)}</div>'


def r_section(b, live):
    num = f'<span class="sec-num">{esc(b["num"])}</span>' if b.get("num") else ""
    sub = f'<p class="sec-sub">{md_inline(b["sub"])}</p>' if b.get("sub") else ""
    body = "".join(r_block(c, live) for c in b.get("body", []))
    return (
        f'<section class="blk"><div class="sec-head">{num}<h2>{esc(b.get("title", ""))}</h2></div>{sub}{body}</section>'
    )


def r_prose(b, live):
    return f'<p class="prose">{md_inline(b.get("md", ""))}</p>'


def r_callout(b, live):
    tone = b.get("tone", "info")
    if tone not in ("info", "ok", "warn", "crit"):
        tone = "info"
    h = f'<div class="h">{esc(b["heading"])}</div>' if b.get("heading") else ""
    return f'<div class="callout {tone}">{h}<div>{md_inline(b.get("md", ""))}</div></div>'


def r_table(b, live):
    cols = b.get("columns", [])
    head = "".join(f"<th>{esc(c)}</th>" for c in cols)

    def cell(i, c):
        th = f' data-th="{esc(cols[i])}"' if i < len(cols) else ""
        return f"<td{th}>{md_inline(c)}</td>"

    rows = "".join("<tr>" + "".join(cell(i, c) for i, c in enumerate(row)) + "</tr>" for row in b.get("rows", []))
    wide = " wide" if len(cols) >= 5 else ""
    cap = f"<caption>{esc(b['caption'])}</caption>" if b.get("caption") else ""
    return f'<div class="tablewrap{wide}"><table>{cap}<thead><tr>{head}</tr></thead><tbody>{rows}</tbody></table></div>'


def r_cards(b, live):
    out = ['<div class="cards">']
    for it in b.get("items", []):
        idx = f'<span class="idx">{esc(it["idx"])}</span>' if it.get("idx") else ""
        chips = ""
        if it.get("chips"):
            chips = (
                '<div class="chips">' + "".join(f'<span class="chip">{esc(c)}</span>' for c in it["chips"]) + "</div>"
            )
        kv = ""
        if it.get("kv"):
            kv = (
                '<dl class="kv">'
                + "".join(f"<dt>{esc(k)}</dt><dd>{md_inline(v)}</dd>" for k, v in it["kv"].items())
                + "</dl>"
            )
        body = "".join(r_block(c, live) for c in it.get("body", []))
        out.append(
            f'<div class="card"><div class="card-top"><h3>{esc(it.get("name", ""))}</h3>{idx}</div>{chips}{kv}{body}</div>'
        )
    out.append("</div>")
    return "".join(out)


def r_wire(b, live):
    verb = f'<span class="verb">{esc(b["verb"])}</span>' if b.get("verb") else "<span></span>"
    return f'<div class="wire"><div class="lab">{verb}<span>{esc(b.get("label", ""))}</span></div><pre><code>{esc(b.get("code", ""))}</code></pre></div>'


def r_ladder(b, live):
    rungs = "".join(
        f'<div class="rung"><div class="no">{i + 1}</div><div class="b"><div class="t">{esc(s.get("t", ""))}</div><div class="d">{md_inline(s.get("d", ""))}</div></div></div>'
        for i, s in enumerate(b.get("steps", []))
    )
    return f'<div class="ladder">{rungs}</div>'


def r_verdict_item(b, live):
    meta = f'<div class="meta">{esc(b["meta"])}</div>' if b.get("meta") else ""
    body = "".join(r_block(c, live) for c in b.get("body", []))
    verbs = ""
    if b.get("verbs"):
        vb = "".join(
            f'<button data-item="{esc(b.get("id", ""))}" data-verb="{esc(v.get("k", ""))}" data-bucket="{esc(v.get("bucket", ""))}">{esc(v.get("label", ""))}</button>'
            for v in b["verbs"]
        )
        verbs = f'<div class="verbs">{vb}</div>'
    return f'<div class="verdict"><h3>{esc(b.get("title", ""))}</h3>{meta}{body}{verbs}</div>'


def r_fragment(b, live):
    # Escape hatch. Static and live HTML sandbox fragments by default, without parent
    # theme tokens or toggle synchronization. Only Markdown export uses `fallback`.
    frag = b.get("html", "")
    height = int(b.get("height", 220))
    if b.get("sandbox", True):
        doc = (
            "<style>:root{color-scheme:light dark}body{margin:0;font:14px/1.5 system-ui;"
            "color:var(--ink,#0d2438);background:transparent}</style>" + frag
        )
        srcdoc = esc(doc)
        body = f'<iframe sandbox="allow-scripts" srcdoc="{srcdoc}" style="height:{height}px"></iframe>'
    else:
        body = frag  # author-trusted only
    return f'<div class="fragment"><div class="flab">fragment · unportable</div>{body}</div>'


def r_chart(b, live):
    return '<div class="chart-box">chart placeholder — render via the <code>dataviz</code> skill and embed as a <code>fragment</code>, or add a first-class <code>chart</code> renderer.</div>'


_RENDERERS = {
    "hero": r_hero,
    "stat-strip": r_stat_strip,
    "section": r_section,
    "prose": r_prose,
    "callout": r_callout,
    "table": r_table,
    "cards": r_cards,
    "wire": r_wire,
    "ladder": r_ladder,
    "verdict-item": r_verdict_item,
    "fragment": r_fragment,
    "chart": r_chart,
}


# ---------------------------------------------------------------- markdown flatten
def md_block(b, depth=0):
    t = b.get("type")
    if t == "hero":
        return f"# {b.get('title', '')}\n\n{b.get('lede', '')}\n"
    if t == "section":
        h = "#" * min(6, depth + 2)
        inner = "\n".join(md_block(c, depth + 1) for c in b.get("body", []))
        return f"{h} {b.get('title', '')}\n\n{b.get('sub', '') or ''}\n\n{inner}"
    if t == "prose":
        return b.get("md", "")
    if t == "callout":
        return f"> **{b.get('heading', '')}** — {b.get('md', '')}"
    if t == "stat-strip":
        return "\n".join(f"- **{s.get('v', '')}** {s.get('k', '')}" for s in b.get("stats", []))
    if t == "table":
        cols = b.get("columns", [])
        out = ["| " + " | ".join(cols) + " |", "| " + " | ".join("---" for _ in cols) + " |"]
        out += ["| " + " | ".join(str(c) for c in row) + " |" for row in b.get("rows", [])]
        return "\n".join(out)
    if t == "wire":
        return f"`{b.get('label', '')}`\n\n```\n{b.get('code', '')}\n```"
    if t == "ladder":
        return "\n".join(f"{i + 1}. **{s.get('t', '')}** — {s.get('d', '')}" for i, s in enumerate(b.get("steps", [])))
    if t == "verdict-item":
        return f"### {b.get('title', '')}\n{b.get('meta', '') or ''}"
    if t == "fragment":
        return b.get("fallback", "*(interactive fragment — view the live note)*")
    if t == "cards":
        return "\n\n".join(f"**{it.get('name', '')}**" for it in b.get("items", []))
    return ""


# ---------------------------------------------------------------- top level
def find_block(blocks, bid):
    for b in blocks:
        if b.get("id") == bid:
            return b
        for key in ("body",):
            if isinstance(b.get(key), list):
                found = find_block(b[key], bid)
                if found:
                    return found
        if b.get("type") == "cards":
            for it in b.get("items", []):
                found = find_block(it.get("body", []), bid)
                if found:
                    return found
    return None


# ---------------------------------------------------------------- theme overrides (env)
# Optionally override any token from the environment, so a report picks up your palette
# without editing this file. Set these in Claude Code's settings.json `env` block (they
# become session env vars the script reads) or your shell/.envrc. All optional — unset
# tokens keep the cyanotype defaults.
#   HTMLREPORT_ACCENT=#7a5cff            -> --accent, both themes
#   HTMLREPORT_LIGHT_PAPER=#fff          -> --paper, light only
#   HTMLREPORT_DARK_ACCENT_2=#9d84ff     -> --accent-2, dark only   (ACCENT_2 -> --accent-2)
#   HTMLREPORT_THEME_JSON='{"light":{"--accent":"#.."},"dark":{"--accent":"#.."}}'  (or @path)
_COLOR_OK = re.compile(r"^[#0-9a-zA-Z(),.%\s*/-]+$")


def _safe_color(v):
    v = (v or "").strip()
    bad = any(c in v for c in (";", "{", "}", "<", ">"))
    return v if v and not bad and _COLOR_OK.match(v) else None


def read_theme_env(env=None):
    """Return (light_overrides, dark_overrides) as {css-var: value} from the environment."""
    env = os.environ if env is None else env
    light, dark = {}, {}
    raw = env.get("HTMLREPORT_THEME_JSON")
    if raw:
        try:
            if raw.startswith("@"):
                with Path(raw[1:]).open() as fh:
                    data = json.loads(fh.read())
            else:
                data = json.loads(raw)
            for scope, dst in (("light", light), ("dark", dark)):
                for k, v in (data.get(scope) or {}).items():
                    c = _safe_color(str(v))
                    if c:
                        dst[k if str(k).startswith("--") else "--" + str(k)] = c
        except (OSError, ValueError):
            pass
    for key, val in env.items():
        if not key.startswith("HTMLREPORT_") or key == "HTMLREPORT_THEME_JSON":
            continue
        c = _safe_color(val)
        if not c:
            continue
        name, scope = key[len("HTMLREPORT_") :], "both"
        if name.startswith("LIGHT_"):
            scope, name = "light", name[6:]
        elif name.startswith("DARK_"):
            scope, name = "dark", name[5:]
        token = "--" + name.lower().replace("_", "-")
        if scope in ("light", "both"):
            light[token] = c
        if scope in ("dark", "both"):
            dark[token] = c
    return light, dark


def override_css(light, dark):
    if not light and not dark:
        return ""

    def blk(sel, d):
        return sel + "{" + "".join(f"{k}:{v};" for k, v in d.items()) + "}" if d else ""

    out = []
    if light:
        out += [blk(":root", light), blk(':root[data-theme="light"]', light)]
    if dark:
        out += ["@media (prefers-color-scheme:dark){" + blk(":root", dark) + "}", blk(':root[data-theme="dark"]', dark)]
    return "\n/* env token overrides */\n" + "\n".join(x for x in out if x)


def render_page(note, live=False):
    meta = note.get("meta", {})
    title = esc(meta.get("title", "Note"))
    body = "".join(r_block(b, live) for b in note.get("blocks", []))
    scripts = f"<script>{BOOTSTRAP_JS}</script>"
    tail = f"<script>{TOGGLE_JS}</script>"
    if live:
        tail += f"<script>{LIVE_JS}</script>"
    return f"""<!doctype html><html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1"><title>{title}</title>
{scripts}
<style>
{THEME_CSS}{override_css(*read_theme_env())}
</style></head><body><div class="wrap">{body}</div>{tail}</body></html>"""


def render_markdown(note):
    meta = note.get("meta", {})
    out = [f"# {meta.get('title', 'Note')}\n"]
    out += [md_block(b) for b in note.get("blocks", [])]
    return "\n\n".join(x for x in out if x.strip())


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("note")
    ap.add_argument("--live", action="store_true", help="include the live-action client JS")
    ap.add_argument("--format", choices=["html", "md"], default="html")
    ap.add_argument("--block", help="render only this block id (a fragment for live swap)")
    args = ap.parse_args()
    with Path(args.note).open() as fh:
        note = json.load(fh)
    if args.format == "md":
        sys.stdout.write(render_markdown(note))
        return
    if args.block:
        b = find_block(note.get("blocks", []), args.block)
        if not b:
            sys.stderr.write(f"block {args.block} not found\n")
            sys.exit(1)
        sys.stdout.write(r_block(b, live=True))
        return
    sys.stdout.write(render_page(note, live=args.live))


if __name__ == "__main__":
    main()
