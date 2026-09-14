#!/usr/bin/env python3
"""serve_note.py — serve a JSON note live and dispatch its actions to scoped subagents.

This is the same-conversation loop. It serves the rendered note, and when a reader clicks
an `expand` action, it spins a SCOPED subagent (just that block + the note's thesis),
gets back a validated JSON block (or an HTML fragment), patches note.json by block id,
and the page swaps that one section in place. The main conversation is never in the loop —
the server orchestrates subagents itself, so the note keeps working after you walk away.

    python serve_note.py note.json                       # serve, dispatch via `claude -p`
    python serve_note.py note.json --dispatch mock        # no LLM — echo a stub block (testing)
    python serve_note.py note.json --dispatch ccs:glm     # route expands through ccs profile
    python serve_note.py note.json --port 8100 --persist  # stay up (default: exit after first action)

Endpoints:
    GET  /                 full rendered note (regenerated each load — edits show on refresh)
    GET  /section/<id>     one block, rendered as a swap fragment
    POST /action           {block_id, action} -> dispatch -> patch note.json -> 200
    POST /verdict          {item, verb, bucket} -> append to verdicts.jsonl
    GET  /result?id=<id>   {status} for polling (ready once the patch landed)

Stdlib only. Binds 127.0.0.1 (localhost-only — this is a you-on-this-box loop; to reach a
phone or a colleague, export the static render instead). Untrusted subagent output is
schema-checked before it patches the note; a bad response becomes a visible error block,
never a broken render.
"""

import argparse
import errno
import json
import re
import subprocess
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlparse

sys.path.insert(0, str(Path(__file__).resolve().parent))

import contextlib

import render_note as R

STATE = {"note": None, "path": None, "dispatch": "claude", "persist": False, "done": {}, "lock": threading.Lock()}

VALID_BLOCK_TYPES = set(R._RENDERERS.keys())


# ---------------------------------------------------------------- subagent dispatch
def build_prompt(note, block, action):
    """A scoped, self-contained prompt: the note's thesis + the one target block + the ask.
    Kept small on purpose — this is the whole point of the cost model."""
    thesis = note.get("meta", {}).get("title", "")
    want = action.get("response", "block")
    if want == "fragment":
        shape = (
            'Return ONLY a JSON object: {"type":"fragment","sandbox":true,"height":<px>,'
            '"fallback":"<plain-text summary>","html":"<self-contained HTML fragment; define your own '
            'light/dark theme styles, including body foreground and background; parent CSS variables '
            'and theme toggles are not available inside the iframe; use prefers-color-scheme and '
            'add a local theme toggle only if needed; '
            'include a visible text explanation even if scripts fail; no external resources>"}.'
        )
    else:
        shape = (
            "Return ONLY a JSON object for a single content block from this vocabulary: "
            "prose {type,md} | callout {type,tone,heading,md} | table {type,columns,rows} | "
            "wire {type,label,code} | ladder {type,steps:[{t,d}]}. No prose around it."
        )
    return (
        f'You are expanding one section of a note titled "{thesis}".\n'
        f"The section's current JSON:\n{json.dumps(block, ensure_ascii=False)}\n\n"
        f"Task: {action.get('label', 'Expand this section with more depth and specifics.')}\n\n"
        f"{shape}"
    )


def run_subagent(prompt, agent):
    """Route to a real headless subagent. `agent` is a model id, or 'ccs:<profile>', or 'codex'.
    Returns the raw stdout string, or raises."""
    disp = STATE["dispatch"]
    if disp == "mock":
        return json.dumps(
            {
                "type": "callout",
                "tone": "info",
                "heading": "expanded (mock)",
                "md": "This is a stubbed expansion — run without `--dispatch mock` for a real subagent.",
            }
        )
    # --permission-mode dontAsk is the load-bearing flag: a bare `claude -p` still prompts
    # for tool approval, which in a no-TTY context is a SILENT INDEFINITE HANG. dontAsk
    # denies anything outside the read-only set instead of blocking. (NOT
    # --dangerously-skip-permissions — that still parks on a one-time bypass dialog.)
    if disp.startswith("ccs:"):
        profile = disp.split(":", 1)[1]
        cmd = ["ccs", profile, "-p", prompt, "--permission-mode", "dontAsk", "--output-format", "json"]
    elif disp == "codex":
        # codex has its own approval/sandbox gate; -s read-only keeps a read-only expand safe.
        cmd = ["codex", "exec", prompt, "--json", "--skip-git-repo-check", "-s", "read-only"]
    else:  # claude headless; model from the action's `agent` if given
        cmd = ["claude", "-p", prompt, "--permission-mode", "dontAsk", "--output-format", "json"]
        if agent and agent not in ("claude", "default"):
            cmd += ["--model", agent]
    out = subprocess.run(cmd, capture_output=True, text=True, timeout=240, check=False)
    if out.returncode != 0:
        raise RuntimeError(out.stderr.strip()[:300] or "subagent failed")
    return out.stdout


def extract_jsonl_text(raw):
    """Pull the final agent-message text out of a `codex exec --json` JSONL event
    stream (one JSON object per line): parse line-by-line, keep only valid events,
    and return the last `item.completed` payload's text (that's the response)."""
    text = None
    for raw_line in raw.splitlines():
        line = raw_line.strip()
        if not line:
            continue
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            continue
        if not isinstance(event, dict):
            continue
        item = event.get("item")
        if event.get("type") == "item.completed" and isinstance(item, dict):
            payload = item.get("text") or item.get("message")
            if payload:
                text = payload
    return text


def extract_block(raw):
    """Pull a JSON block out of subagent stdout (which may be JSON-wrapped, a
    codex JSONL event stream, or fenced)."""

    # claude -p --output-format json wraps: {"type":"result","result":"<text>",...}
    def _try_jsonl():
        # not a single JSON object — maybe a JSONL event stream (codex --json)
        lines = raw.strip().splitlines()
        if len(lines) > 1:
            text = extract_jsonl_text(raw)
            if text:
                return text
        return raw

    try:
        env = json.loads(raw)
        if isinstance(env, dict) and "result" in env:
            raw = env["result"]
    except (json.JSONDecodeError, TypeError):
        raw = _try_jsonl()
    m = re.search(r"\{.*\}", raw, re.DOTALL)
    if not m:
        raise ValueError("no JSON object in subagent output")
    return json.loads(m.group(0))


def validate_block(b):
    if not isinstance(b, dict) or b.get("type") not in VALID_BLOCK_TYPES:
        raise ValueError(f"invalid block type: {b.get('type') if isinstance(b, dict) else type(b)}")
    return b


def patch(note, block_id, new_block, swap, by="subagent"):
    """Insert new_block into the target's body (append) or replace it, addressed by id.
    Stamp provenance so the renderer marks it as agent-generated, not authored content."""
    target = R.find_block(note.get("blocks", []), block_id)
    if target is None:
        raise KeyError(block_id)
    if isinstance(new_block, dict):
        new_block["_added"] = True
        new_block["_by"] = by
    if swap == "replace" and target.get("type") == "section":
        target["body"] = [new_block]
    else:  # append into the target's body (works for section / verdict-item)
        target.setdefault("body", []).append(new_block)


def handle_action(block_id, action_idx):
    with STATE["lock"]:
        note = STATE["note"]
        block = R.find_block(note.get("blocks", []), block_id)
        if block is None:
            raise KeyError(block_id)
        actions = block.get("actions") or []
        action = actions[action_idx]
    prompt = build_prompt(note, block, action)
    try:
        raw = run_subagent(prompt, action.get("agent"))
        new_block = validate_block(extract_block(raw))
    except Exception as e:  # any failure becomes a visible, retryable error block
        new_block = {
            "type": "callout",
            "tone": "warn",
            "heading": "expansion failed",
            "md": f"{str(e)[:200]} — click again to retry.",
        }
    with STATE["lock"]:
        patch(STATE["note"], block_id, new_block, action.get("swap", "append"), by=action.get("agent") or "subagent")
        with Path(STATE["path"]).open("w") as fh:
            json.dump(STATE["note"], fh, indent=2, ensure_ascii=False)
        STATE["done"][block_id] = True


# ---------------------------------------------------------------- http
class H(BaseHTTPRequestHandler):
    def log_message(self, format: str, *args: Any) -> None:  # noqa: A002  (matches base API)
        pass

    def _send(self, code, body, ctype="text/html; charset=utf-8"):
        data = body.encode("utf-8") if isinstance(body, str) else body
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self):
        u = urlparse(self.path)
        if u.path == "/":
            with STATE["lock"]:
                self._send(200, R.render_page(STATE["note"], live=True))
        elif u.path.startswith("/section/"):
            bid = u.path[len("/section/") :]
            with STATE["lock"]:
                b = R.find_block(STATE["note"].get("blocks", []), bid)
            self._send(200, R.r_block(b, live=True) if b else "<div></div>")
        elif u.path == "/result":
            bid = parse_qs(u.query).get("id", [""])[0]
            self._send(
                200, json.dumps({"status": "ready" if STATE["done"].get(bid) else "pending"}), "application/json"
            )
        else:
            self._send(404, "not found")

    def do_POST(self):
        n = int(self.headers.get("Content-Length", 0))
        try:
            payload = json.loads(self.rfile.read(n) or "{}")
        except json.JSONDecodeError:
            self._send(400, json.dumps({"error": "malformed JSON body"}), "application/json")
            return
        if not isinstance(payload, dict):
            self._send(400, json.dumps({"error": "JSON body must be an object"}), "application/json")
            return
        if self.path == "/action":
            bid, ai = payload.get("block_id"), int(payload.get("action", 0))
            STATE["done"][bid] = False
            try:
                handle_action(bid, ai)
                self._send(200, json.dumps({"status": "done", "block_id": bid}), "application/json")
            except Exception as e:
                self._send(500, json.dumps({"error": str(e)[:200]}), "application/json")
            if not STATE["persist"]:
                threading.Thread(target=self.server.shutdown, daemon=True).start()
        elif self.path == "/verdict":
            with (Path(STATE["path"]).parent / "verdicts.jsonl").open("a") as f:
                f.write(json.dumps(payload) + "\n")
            self._send(200, json.dumps({"status": "recorded"}), "application/json")
        else:
            self._send(404, "not found")


def bind_server(port, tries=20):
    """Bind on `port`, auto-incrementing past ports already in use (up to `tries`).
    Never touches other processes — just steps to the next free port."""
    for p in range(port, port + tries):
        try:
            return ThreadingHTTPServer(("127.0.0.1", p), H), p
        except OSError as e:
            if e.errno != errno.EADDRINUSE:
                raise
    raise SystemExit(f"no free port in {port}..{port + tries - 1}")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("note")
    ap.add_argument("--port", type=int, default=8100)
    ap.add_argument("--dispatch", default="claude", help="claude | mock | codex | ccs:<profile>")
    ap.add_argument(
        "--persist",
        action="store_true",
        help="stay up after actions (default: exit after the first, so the "
        "harness can re-invoke you to review the result)",
    )
    args = ap.parse_args()
    with Path(args.note).open() as fh:
        note = json.load(fh)
    STATE.update(note=note, path=str(Path(args.note).resolve()), dispatch=args.dispatch, persist=args.persist)
    srv, port = bind_server(args.port)
    url = f"http://127.0.0.1:{port}/"
    sys.stderr.write(
        f"serving {args.note} at {url}  (dispatch={args.dispatch}, {'persist' if args.persist else 'exit-on-action'})\n"
    )
    with contextlib.suppress(KeyboardInterrupt):
        srv.serve_forever()


if __name__ == "__main__":
    main()
