import json
import socket
import threading
import urllib.error
import urllib.request

import pytest
import serve_note as sn

BLOCK = {"type": "prose", "md": "hi"}


# ---------------------------------------------------------------- extract_block
def test_extract_block_plain_json():
    assert sn.extract_block(json.dumps(BLOCK)) == BLOCK


def test_extract_block_claude_envelope():
    raw = json.dumps({"type": "result", "result": json.dumps(BLOCK)})
    assert sn.extract_block(raw) == BLOCK


def test_extract_block_codex_jsonl_stream():
    raw = "\n".join(
        [
            json.dumps({"type": "item.started", "item": {"text": "thinking"}}),
            "not json at all",
            json.dumps({"type": "item.completed", "item": {"text": json.dumps(BLOCK)}}),
        ]
    )
    assert sn.extract_block(raw) == BLOCK


def test_extract_block_prose_wrapped():
    assert sn.extract_block(f"Here you go:\n{json.dumps(BLOCK)}\nthanks") == BLOCK


def test_extract_block_no_json_raises():
    with pytest.raises(ValueError):
        sn.extract_block("no json here")


def test_extract_jsonl_text_keeps_last_completed():
    raw = "\n".join(
        [
            json.dumps({"type": "item.completed", "item": {"text": "first"}}),
            json.dumps({"type": "item.completed", "item": {"message": "last"}}),
        ]
    )
    assert sn.extract_jsonl_text(raw) == "last"


# ---------------------------------------------------------------- validate / patch
def test_validate_block_accepts_known_type():
    assert sn.validate_block(BLOCK) == BLOCK


@pytest.mark.parametrize("bad", [{"type": "nope"}, ["not", "a", "dict"], "str"])
def test_validate_block_rejects(bad):
    with pytest.raises(ValueError):
        sn.validate_block(bad)


def note_with_section():
    return {"meta": {"title": "t"}, "blocks": [{"type": "section", "id": "s1", "title": "S", "body": []}]}


def test_patch_appends_and_stamps_provenance():
    note = note_with_section()
    sn.patch(note, "s1", dict(BLOCK), swap="append", by="tester")
    added = note["blocks"][0]["body"][0]
    assert added["md"] == "hi" and added["_added"] is True and added["_by"] == "tester"


def test_patch_replace_swaps_section_body():
    note = note_with_section()
    note["blocks"][0]["body"] = [{"type": "prose", "md": "old"}]
    sn.patch(note, "s1", dict(BLOCK), swap="replace")
    assert [b["md"] for b in note["blocks"][0]["body"]] == ["hi"]


def test_patch_unknown_id_raises():
    with pytest.raises(KeyError):
        sn.patch(note_with_section(), "missing", dict(BLOCK), swap="append")


# ---------------------------------------------------------------- bind_server
def test_bind_server_steps_past_taken_port():
    srv1, _ = sn.bind_server(0)  # ephemeral pick
    try:
        real = srv1.server_address[1]
        srv2, p2 = sn.bind_server(real, tries=20)
        try:
            assert p2 > real
        finally:
            srv2.server_close()
    finally:
        srv1.server_close()


def test_bind_server_exhausts_and_exits():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        s.listen(1)
        taken = s.getsockname()[1]
        with pytest.raises(SystemExit):
            sn.bind_server(taken, tries=1)


# ---------------------------------------------------------------- end-to-end (mock dispatch)
@pytest.fixture
def live_server(tmp_path):
    note_path = tmp_path / "note.json"
    note = note_with_section()
    note["blocks"][0]["actions"] = [{"label": "Expand", "kind": "expand"}]
    note_path.write_text(json.dumps(note))

    saved = dict(sn.STATE)
    sn.STATE.update(note=note, path=str(note_path), dispatch="mock", persist=True, done={})
    srv, _ = sn.bind_server(0)
    port = srv.server_address[1]
    t = threading.Thread(target=srv.serve_forever, daemon=True)
    t.start()
    yield f"http://127.0.0.1:{port}", note_path
    srv.shutdown()
    srv.server_close()
    sn.STATE.update(saved)


def get(url):
    with urllib.request.urlopen(url, timeout=10) as r:
        return r.status, r.read().decode()


def test_serve_renders_note(live_server):
    base, _ = live_server
    status, body = get(f"{base}/")
    assert status == 200 and "<!doctype html>" in body


def test_mock_action_patches_note_and_fragment(live_server):
    base, note_path = live_server
    req = urllib.request.Request(
        f"{base}/action",
        data=json.dumps({"block_id": "s1", "action": 0}).encode(),
        headers={"Content-Type": "application/json"},
    )
    with urllib.request.urlopen(req, timeout=30) as r:
        assert json.loads(r.read())["status"] == "done"

    on_disk = json.loads(note_path.read_text())
    added = on_disk["blocks"][0]["body"][0]
    assert added["heading"] == "expanded (mock)" and added["_added"] is True

    status, frag = get(f"{base}/section/s1")
    assert status == 200 and "expanded by" in frag

    status, res = get(f"{base}/result?id=s1")
    assert json.loads(res)["status"] == "ready"


def test_malformed_action_body_is_400(live_server):
    base, _ = live_server
    req = urllib.request.Request(f"{base}/action", data=b"{not json", headers={"Content-Type": "application/json"})
    with pytest.raises(urllib.error.HTTPError) as e:
        urllib.request.urlopen(req, timeout=10)
    assert e.value.code == 400
