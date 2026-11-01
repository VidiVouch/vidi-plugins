#!/usr/bin/env python3
"""Fill a run's model identity from the harness transcript and the proxy log, never from a label or a model's own words.

Three sources stay apart. The requested model is what the dispatch asked for: the pinned ID of
the definition it selected, or the alias it passed. The harness-resolved model is what Claude
Code's own transcript records for the unit, the model it sent in each API request and the model
each response named, read from metadata fields only. The upstream model is what the proxy's
request log recorded for the same responses, joined by message id. A unit whose sources disagree
with its request fell back. A model-comparison trial is invalid when any unit fell back and
unverified when any unit lacks harness evidence; it is never valid on labels or self-reports.
"""

import argparse
import json
import re
import sys
from pathlib import Path

from prepare_reviewers import agent_model, catalog_id

PLUGIN_PREFIX = "vidi-skills:"
SYNTHETIC = "<synthetic>"  # Claude Code's placeholder model on turns no model produced
RAN = ("returned", "failed")  # dispatch states that reached agent(); queued and cancelled units never ran
SECTION = re.compile(r"^=== (.+?) ===$", re.MULTILINE)
MESSAGE_ID = re.compile(r'"id"\s*:\s*"(msg_[^"]+)"')
MODEL = re.compile(r'"model"\s*:\s*"([^"]+)"')


def bare(model):
    """The ID the provider sees: Claude Code strips a trailing [1m] before sending (models.md)."""
    return catalog_id(model.strip()) if isinstance(model, str) else None


def is_alias(model):
    """A family alias (sonnet, opus, inherit) carries no version digit. The harness resolves it, so it pins nothing."""
    return not any(ch.isdigit() for ch in bare(model) or "")


def journal_units(workflow_dir):
    """label -> agentId from the runtime's journal. The label only addresses a transcript; it proves nothing."""
    units = {}
    path = Path(workflow_dir) / "journal.jsonl"
    if not path.is_file():
        return units
    for line in path.read_text().splitlines():
        try:
            record = json.loads(line)
        except ValueError:
            continue
        label, agent_id = record.get("label"), record.get("agentId")
        if record.get("type") == "started" and isinstance(label, str) and isinstance(agent_id, str):
            units.setdefault(label, agent_id)
    return units


def harness_evidence(transcript):
    """What Claude Code recorded about one unit's API calls: the model it sent and the model each response named.

    Only metadata fields are read (api-request.params.model, assistant.requestedModel, assistant.message.model
    and .id). Message content is never opened, so nothing a model says about itself can reach the record."""
    sent, answered, ids = set(), set(), []
    for line in Path(transcript).read_text().splitlines():
        try:
            record = json.loads(line)
        except ValueError:
            continue
        if record.get("type") == "api-request":
            model = (record.get("params") or {}).get("model")
            if isinstance(model, str):
                sent.add(model)
            continue
        if record.get("type") != "assistant":
            continue
        message = record.get("message") or {}
        model = message.get("model")
        if not isinstance(model, str) or model == SYNTHETIC:
            continue
        answered.add(model)
        if isinstance(record.get("requestedModel"), str):
            sent.add(record["requestedModel"])
        if isinstance(message.get("id"), str):
            ids.append(message["id"])
    return {
        "transcript": str(transcript),
        "sentModels": sorted(sent),
        "answeredModels": sorted(answered),
        "messageIds": ids,
    }


def runtime_meta(workflow_dir, agent_id):
    path = Path(workflow_dir) / f"agent-{agent_id}.meta.json"
    if not path.is_file():
        return {}
    try:
        data = json.loads(path.read_text())
    except ValueError:
        return {}
    return data if isinstance(data, dict) else {}


def _request_model(body):
    try:
        data = json.loads(body.strip())
    except ValueError:
        match = MODEL.search(body)
        return match.group(1) if match else None
    model = data.get("model") if isinstance(data, dict) else None
    return model if isinstance(model, str) else None


def proxy_records(log_dir):
    """CLIProxy request logs (written only with request-log: true): the model the proxy received for a request
    and the model its upstream answered with. Headers are not read, so a logged credential never reaches the record."""
    records = []
    for path in sorted(Path(log_dir).glob("*.log")):
        parts = SECTION.split(path.read_text(errors="replace"))
        sections = {parts[i].strip(): parts[i + 1] for i in range(1, len(parts) - 1, 2)}
        response = sections.get("API RESPONSE", "")
        rid, res = MESSAGE_ID.search(response), MODEL.search(response)
        records.append(
            {
                "path": str(path),
                "requestModel": _request_model(sections.get("REQUEST BODY", "")),
                "responseId": rid.group(1) if rid else None,
                "responseModel": res.group(1) if res else None,
            }
        )
    return records


def unit_identity(dispatch, workflow_dir, units, proxy):
    """One dispatched unit: what was asked, what the harness says answered, what the proxy says answered."""
    label, agent_type = dispatch.get("label"), dispatch.get("agentType")
    name = agent_type.removeprefix(PLUGIN_PREFIX) if isinstance(agent_type, str) else None
    unit = {
        "label": label,
        "phase": dispatch.get("phase"),
        "agentType": agent_type,
        "requestedModel": None,
        "pinned": False,
        "resolvedModel": None,
        "upstreamModel": None,
        "state": "unverified",
        "reasons": [],
        "harness": None,
        "proxy": [],
    }
    reasons = unit["reasons"]
    if name:
        try:
            unit["requestedModel"] = agent_model(name)
        except (OSError, ValueError):
            reasons.append(f"no packaged definition for {agent_type}")
    else:
        unit["requestedModel"] = dispatch.get("model") or "inherit"
    requested = unit["requestedModel"]
    unit["pinned"] = bool(requested) and not is_alias(requested)
    expected = bare(requested) if unit["pinned"] else None

    agent_id = units.get(label)
    transcript = Path(workflow_dir) / f"agent-{agent_id}.jsonl" if agent_id else None
    if transcript is None or not transcript.is_file():
        reasons.append("no harness transcript for this unit")
        return unit
    harness = harness_evidence(transcript)
    unit["harness"] = harness
    ran = runtime_meta(workflow_dir, agent_id).get("agentType")
    if name and isinstance(ran, str) and ran.removeprefix(PLUGIN_PREFIX) != name:
        reasons.append(f"runtime ran {ran} instead of {agent_type}")

    sent = {bare(model) for model in harness["sentModels"]}
    answered = harness["answeredModels"]
    if not answered:
        reasons.append("transcript holds no model-answered turn")
        return unit
    if expected and sent != {expected}:
        reasons.append(f"harness sent {', '.join(sorted(sent)) or 'nothing'} instead of {expected}")
    # A pinned unit must be answered by its ID; an alias unit by whatever the harness resolved the alias to.
    targets = {expected} if expected else sent
    if len(answered) == 1:
        unit["resolvedModel"] = answered[0]
    else:
        reasons.append(f"more than one model answered: {', '.join(answered)}")
    for model in answered:
        if targets and bare(model) not in targets:
            reasons.append(f"answered by {model}, not {', '.join(sorted(targets))}")

    matched = [r for r in proxy if r["responseId"] and r["responseId"] in harness["messageIds"]]
    unit["proxy"] = matched
    upstream = sorted({r["responseModel"] for r in matched if r["responseModel"]})
    if len(upstream) == 1:
        unit["upstreamModel"] = upstream[0]
    elif upstream:
        reasons.append(f"more than one upstream model answered: {', '.join(upstream)}")
    for record in matched:
        for side, model in (("received", record["requestModel"]), ("answered upstream with", record["responseModel"])):
            if model and targets and bare(model) not in targets:
                reasons.append(f"proxy {side} {model}, not {', '.join(sorted(targets))}")
    unit["state"] = "fallback" if reasons else "verified"
    return unit


def evaluate(result, workflow_dir, proxy_logs=None):
    """The identity record for every unit that ran, and whether the run can count as a model-comparison trial."""
    dispatches = (result.get("admission") or {}).get("dispatches") or []
    units = journal_units(workflow_dir)
    proxy = proxy_records(proxy_logs) if proxy_logs else []
    records = [unit_identity(d, workflow_dir, units, proxy) for d in dispatches if d.get("state") in RAN]
    fallen = [u["label"] for u in records if u["state"] == "fallback"]
    unverified = [u["label"] for u in records if u["state"] == "unverified"]
    reasons = [f"{u['label']}: {reason}" for u in records for reason in u["reasons"]]
    if fallen:
        state = "invalid"
    elif unverified or not records:
        state = "unverified"
        if not records:
            reasons.append("no unit ran")
    else:
        state = "valid"
    return {
        "run": result.get("run"),
        "sources": {
            "workflowDir": str(workflow_dir),
            "proxyLogs": str(proxy_logs) if proxy_logs else None,
            "proxyLogReason": None
            if proxy_logs
            else "no proxy request log given; CLIProxy writes one only with request-log: true, so upstreamModel stays null",
        },
        "units": records,
        "trial": {
            "state": state,
            "harnessVerified": bool(records) and not fallen and not unverified,
            "upstreamVerified": bool(records) and all(u["upstreamModel"] for u in records) and not fallen,
            "fallback": fallen,
            "unverified": unverified,
            "reasons": reasons,
        },
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--result", required=True, help="the saved workflow result (outputs.resultPath)")
    parser.add_argument(
        "--workflow-dir",
        required=True,
        help="the Workflow run's transcript directory (journal.jsonl and agent-*.jsonl)",
    )
    parser.add_argument("--proxy-logs", help="CLIProxy request log directory, when request-log is on")
    parser.add_argument("--out", help="write the record here as well as printing it")
    args = parser.parse_args()
    try:
        result = json.loads(Path(args.result).read_text())
        report = evaluate(result, args.workflow_dir, args.proxy_logs)
    except (OSError, ValueError, AttributeError):
        print("Invalid result, workflow directory or proxy log; no identity recorded", file=sys.stderr)
        return 2
    text = json.dumps(report, indent=2)
    if args.out:
        Path(args.out).write_text(text + "\n")
    print(text)
    return 0 if report["trial"]["state"] == "valid" else 1


if __name__ == "__main__":
    sys.exit(main())
