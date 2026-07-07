import json

import pytest
import render_note as rn
from conftest import REPO

EXAMPLE = REPO / "skills" / "html-report" / "assets" / "example-note.json"


@pytest.fixture(scope="module")
def note():
    return json.loads(EXAMPLE.read_text())


def test_render_example_note_page(note):
    html = rn.render_page(note)
    assert html.startswith("<!doctype html>")
    assert "<title>Data-pipeline migration — findings</title>" in html
    assert "data-theme" in html  # theme bootstrap present
    assert "https://cdn." not in html and "fonts.googleapis" not in html


def test_render_example_note_markdown(note):
    md = rn.render_markdown(note)
    assert md.startswith("# Data-pipeline migration — findings")


def test_esc_escapes_html():
    assert rn.esc('<script>"x"</script>') == "&lt;script&gt;&quot;x&quot;&lt;/script&gt;"
    assert rn.esc(None) == ""


def test_md_inline_bold_code_and_safe_links():
    assert rn.md_inline("**b** and `c`") == "<strong>b</strong> and <code>c</code>"
    assert rn.md_inline("[t](https://x.y)") == '<a href="https://x.y">t</a>'


def test_md_inline_rejects_javascript_urls():
    out = rn.md_inline("[click](javascript:alert(1))")
    assert "<a" not in out and "javascript:" not in out


def test_md_inline_escapes_before_markup():
    assert "<script>" not in rn.md_inline("**<script>**")


def test_find_block_nested(note):
    b = rn.find_block(note["blocks"], "critical")
    assert b is not None and b["type"] == "section"


def test_unknown_block_type_renders_warning():
    out = rn.r_block({"type": "nope"}, live=False)
    assert "unknown block" in out


def test_fragment_is_sandboxed():
    out = rn.r_fragment({"html": '<b onclick="hax()">x</b>'}, live=False)
    assert 'sandbox="allow-scripts"' in out
    assert "srcdoc=" in out
    assert '<b onclick="hax()">' not in out  # escaped into the srcdoc attribute


def test_read_theme_env_scopes_and_tokens():
    light, dark = rn.read_theme_env(
        {
            "HTMLREPORT_ACCENT": "#7a5cff",
            "HTMLREPORT_LIGHT_PAPER": "#fff",
            "HTMLREPORT_DARK_ACCENT_2": "#9d84ff",
            "UNRELATED": "#123456",
        }
    )
    assert light == {"--accent": "#7a5cff", "--paper": "#fff"}
    assert dark == {"--accent": "#7a5cff", "--accent-2": "#9d84ff"}


def test_read_theme_env_rejects_css_injection():
    light, dark = rn.read_theme_env({"HTMLREPORT_ACCENT": "red;}body{background:url(evil)"})
    assert light == {} and dark == {}


def test_read_theme_env_json_blob():
    env = {"HTMLREPORT_THEME_JSON": json.dumps({"light": {"--accent": "#111"}, "dark": {"accent": "#222"}})}
    light, dark = rn.read_theme_env(env)
    assert light == {"--accent": "#111"}
    assert dark == {"--accent": "#222"}  # bare name gets the -- prefix


def test_read_theme_env_bad_json_is_ignored():
    light, dark = rn.read_theme_env({"HTMLREPORT_THEME_JSON": "{not json"})
    assert light == {} and dark == {}


def test_override_css_emits_both_scopes():
    css = rn.override_css({"--accent": "#111"}, {"--accent": "#222"})
    assert ':root[data-theme="light"]{--accent:#111;}' in css
    assert ':root[data-theme="dark"]{--accent:#222;}' in css
    assert "@media (prefers-color-scheme:dark)" in css
    assert rn.override_css({}, {}) == ""
