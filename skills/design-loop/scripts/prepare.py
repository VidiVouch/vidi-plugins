"""Prepare neutral visual-review packets without network or model calls."""

import argparse
import hashlib
import json
import math
import os
import re
import secrets
import sys
import tempfile
from pathlib import Path

SKILL = Path(__file__).resolve().parents[1]
MODELS = {
    "design-judge-fable": "claude-fable-5-1[1m]",
    "design-judge-astra": "gpt-6-astra[1m]",
    "design-judge-grok": "grok-4.7",
}
DIMENSIONS = {
    "typography": "Legibility and text roles",
    "hierarchy": "Order of attention and task-relevant actions",
    "spacing_density": "Grouping, scanability and useful density",
    "color_contrast": "Visible differentiation and brand fit",
    "iconography": "Recognizability and meaning",
    "consistency_polish": "Coherent patterns and visible finish",
    "clarity": "Content, labels, state and next action",
}


def require(condition, message):
    if not condition:
        raise ValueError(message)


def text(value, field):
    require(isinstance(value, str) and bool(value.strip()), f"{field} must be nonempty text")
    return value


def integer(value, field):
    require(type(value) is int and value > 0, f"{field} must be a positive integer")
    return value


def strings(value, field, *, nonempty=False):
    require(isinstance(value, list), f"{field} must be an array")
    require(not nonempty or bool(value), f"{field} must not be empty")
    return [text(item, field) for item in value]


def digest(data):
    return hashlib.sha256(data).hexdigest()


def canonical(value):
    return json.dumps(value, ensure_ascii=True, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()


def named_agents(env=None):
    env = os.environ if env is None else env
    force = env.get("CLAUDE_CODE_SUBAGENT_MODEL_FORCE", "").strip().lower()
    require(force in {"", "0", "false", "no", "off"}, "CLAUDE_CODE_SUBAGENT_MODEL_FORCE would override judge models")
    for name, model in MODELS.items():
        source = (SKILL.parent.parent / "agents" / f"{name}.md").read_text()
        require(re.search(rf"^model: {re.escape(model)}$", source, re.M), f"Unexpected model in {name}")
        require(re.search(r"^tools: Read$", source, re.M), f"Unexpected tools in {name}")
    return {"forced_model": False, "source_definitions_checked": True}


def prepare(spec, *, env=None, temp_parent=None):
    """Allowlist public fields and copy immutable images to a fresh neutral folder.

    Free text and pixels still need a coordinator's contamination check. This is
    neither a content sanitizer nor an enforced filesystem isolation mechanism.
    """
    require(isinstance(spec, dict), "input must be an object")
    preflight = named_agents(env)
    brief = spec.get("brief")
    require(isinstance(brief, dict), "brief must be an object")
    public_brief = {"audience": text(brief.get("audience"), "audience")}
    for key in ("tasks", "requirements", "brand"):
        public_brief[key] = strings(brief.get(key), key, nonempty=key == "tasks")
    source_candidates = spec.get("candidates")
    require(isinstance(source_candidates, list) and bool(source_candidates), "candidates must be a nonempty array")
    require(all(isinstance(c, dict) for c in source_candidates), "each candidate must be an object")
    keys = [text(c.get("key"), "candidate key") for c in source_candidates]
    require(len(set(keys)) == len(keys), "candidate keys must be unique")
    order = spec.get("order")
    if order is None:
        order = keys.copy()
        secrets.SystemRandom().shuffle(order)
    require(isinstance(order, list) and all(isinstance(k, str) for k in order), "order must be an array of keys")
    require(len(order) == len(keys) and set(order) == set(keys), "order must be a permutation of candidate keys")
    focus = spec.get("focus")
    require(focus in keys, "focus must name a candidate key")
    round_no = integer(spec.get("round", 1), "round")
    max_rounds = integer(spec.get("max_rounds", 3), "max_rounds")
    require(round_no <= max_rounds, "round allowance exhausted")
    target = spec.get("target_overall")
    require(
        target is None or (type(target) in (int, float) and math.isfinite(target) and 0 <= target <= 10),
        "target_overall must be null or a finite number from 0 to 10",
    )
    runtime = spec.get("runtime", {"status": "not_run", "revision": "", "evidence": []})
    require(isinstance(runtime, dict), "runtime must be an object")
    require(runtime.get("status") in {"passed", "failed", "not_run", "blocked"}, "invalid runtime status")
    runtime_evidence = strings(runtime.get("evidence"), "runtime evidence")
    by_key = {c["key"]: c for c in source_candidates}
    focus_revision = text(by_key[focus].get("revision"), "focus revision")
    if runtime["status"] == "passed":
        require(
            runtime.get("revision") == focus_revision and runtime_evidence,
            "runtime pass needs this revision and evidence",
        )
    require(spec.get("blindness_checked") is True, "inspect brief, pixels, metadata and inherited instructions first")
    purpose = spec.get("purpose", "review")
    require(purpose in {"review", "smoke"}, "purpose must be review or smoke")
    require(spec.get("agents_loaded") is True, "confirm all three named agents are loaded first")
    require(purpose == "smoke" or spec.get("route_ready") is True, "complete the image/JSON routing smoke test first")
    require(purpose != "smoke" or (round_no == max_rounds == 1), "smoke tests use exactly one round")

    # Validate and read everything before creating the public directory.
    candidates, private, images = [], [], []
    signatures = None
    for i, key in enumerate(order, 1):
        candidate = by_key[key]
        candidate_id = f"C{i}"
        revision = text(candidate.get("revision"), "revision")
        captures = candidate.get("captures")
        require(isinstance(captures, list) and bool(captures), "captures must be a nonempty array")
        evidence, capture_signatures, provenance = [], [], []
        for j, capture in enumerate(captures, 1):
            require(isinstance(capture, dict), "capture must be an object")
            path = Path(text(capture.get("path"), "capture path"))
            require(path.is_absolute() and path.is_file(), "capture path must name an existing absolute file")
            data = path.read_bytes()
            suffix = (
                ".png"
                if data.startswith(b"\x89PNG\r\n\x1a\n")
                else ".jpg"
                if data.startswith(b"\xff\xd8\xff")
                else None
            )
            if suffix is None:
                raise ValueError("capture must be a PNG or JPEG; image decoding is verified by the judge")
            viewport = capture.get("viewport")
            require(isinstance(viewport, dict), "viewport must be an object")
            public_viewport = {k: integer(viewport.get(k), k) for k in ("width", "height")}
            scale = viewport.get("device_scale", 1)
            require(type(scale) in (float, int) and math.isfinite(scale) and scale > 0, "device_scale must be positive")
            public_viewport["device_scale"] = scale
            context = {k: text(capture.get(k), k) for k in ("theme", "state", "fixture")}
            context["viewport"] = public_viewport
            capture_signatures.append(canonical(context).decode())
            evidence_id = f"{candidate_id}E{j}"
            name = evidence_id + suffix
            evidence.append({"id": evidence_id, "file": name, "sha256": digest(data), **context})
            images.append((name, data))
            provenance.append({"evidence_id": evidence_id, "source_path": str(path.resolve()), "sha256": digest(data)})
        require(len(set(capture_signatures)) == len(capture_signatures), "duplicate capture context in a candidate")
        if signatures is None:
            signatures = set(capture_signatures)
        require(set(capture_signatures) == signatures, "candidates need matching viewport/theme/state/fixture contexts")
        candidates.append({"id": candidate_id, "evidence": evidence})
        private.append({"id": candidate_id, "key": key, "revision": revision, "captures": provenance})

    public_root = Path(tempfile.mkdtemp(prefix="design-evidence-", dir=temp_parent)).resolve()
    for name, data in images:
        with (public_root / name).open("xb") as stream:
            stream.write(data)
    for candidate in candidates:
        for image in candidate["evidence"]:
            image["path"] = str(public_root / image.pop("file"))
    packet = {
        "brief": public_brief,
        "rubric": {
            "dimensions": DIMENSIONS,
            "anchors": {
                "0": "Task is unusable",
                "5": "Usable with material weaknesses",
                "8": "Strong task fit with specific refinements",
                "10": "No material visual weakness evident for the stated task",
            },
            "overall": "Holistic task fit; not an arithmetic average",
        },
        "candidates": candidates,
    }
    packet["packet_id"] = digest(canonical(packet))
    return {
        "public_root": str(public_root),
        "provenance": private,
        "input_sha256": digest(canonical(spec)),
        "workflow": {
            "packet": packet,
            "contract": (SKILL / "references/critic-contract.md").read_text(),
            "judge_schema": json.loads((SKILL / "judge-output.schema.json").read_text()),
            "preflight": preflight,
            "control": {
                "purpose": purpose,
                "focus_id": f"C{order.index(focus) + 1}",
                "round": round_no,
                "max_rounds": max_rounds,
                "target_overall": target,
                "runtime": {
                    "status": runtime["status"],
                    "revision": runtime.get("revision", ""),
                    "evidence": runtime_evidence,
                },
            },
        },
    }


def verify(prepared):
    """Verify frozen packet bytes, not the current application or model routing."""
    named_agents()
    packet = prepared["workflow"]["packet"]
    content = {key: value for key, value in packet.items() if key != "packet_id"}
    require(digest(canonical(content)) == packet["packet_id"], "public packet changed")
    root = Path(prepared["public_root"]).resolve()
    for candidate in packet["candidates"]:
        for image in candidate["evidence"]:
            path = Path(image["path"])
            require(path.resolve().parent == root and not path.is_symlink(), "evidence escaped its frozen directory")
            require(digest(path.read_bytes()) == image["sha256"], f"evidence changed: {image['id']}")
    return {"packet_id": packet["packet_id"], "image_hashes_verified": True}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=("prepare", "verify"))
    parser.add_argument("input", type=Path)
    parser.add_argument("--out", type=Path)
    args = parser.parse_args()
    try:
        if args.command == "prepare":
            out = args.out
            if out is None or out.exists():
                raise ValueError("--out must name a new private file")
            result = prepare(json.loads(args.input.read_text()))
            with out.open("x") as stream:
                json.dump(result, stream, indent=2, allow_nan=False)
                stream.write("\n")
            print(json.dumps({"prepared": str(out.resolve()), "public_root": result["public_root"]}))
        else:
            print(json.dumps(verify(json.loads(args.input.read_text()))))
    except (ValueError, OSError, KeyError, TypeError) as exc:
        print(f"design-loop: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
