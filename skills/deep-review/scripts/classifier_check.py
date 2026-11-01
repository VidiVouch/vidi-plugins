#!/usr/bin/env python3
"""Plan the permission-classifier routing check and read its evidence, without running it.

The classifier is a separate request Claude Code sends in auto mode. It has no agent definition,
so its model comes only from CLAUDE_CODE_AUTO_MODE_MODEL in the launching profile. `plan` reads
the model keys of a settings file and says whether an approved live check could request the
intended model; `evidence` reads the debug log that check wrote and says what was requested and
whether the proxy served it. A launch that sent no classifier request proves nothing: reads and
working-directory edits skip the review, so a read-only worker run is never a classifier test.
"""

import argparse
import json
import re
import shlex
import subprocess
import sys
from pathlib import Path

from model_evidence import bare, proxy_records
from model_roles import ROLES, check_model
from prepare_reviewers import proxy_models

MODEL_KEY = "CLAUDE_CODE_AUTO_MODE_MODEL"
SERVER_KEY = "CLAUDE_CODE_AUTO_MODE_SERVER"
MIN_VERSION = (2, 1, 302)  # permission-modes docs: the override needs Claude Code v2.1.302 or later
# Debug-log markers the permission-modes docs name for reading the classifier's model choice.
STARTED = "classifier_request_started"
IGNORED = f"ignoring {MODEL_KEY}"
FAILED = "cannot determine the safety"
REQUEST_MODEL = re.compile(r"\bmodel=([^\s,;]+)")
VERSION = re.compile(r"\d+(?:\.\d+)+")


def version_tuple(text):
    match = VERSION.search(text or "")
    return tuple(int(part) for part in match.group(0).split(".")) if match else None


def installed_claude_version():
    try:
        out = subprocess.run(["claude", "--version"], capture_output=True, text=True, timeout=30, check=False)
    except (OSError, subprocess.SubprocessError):
        return None
    return out.stdout.strip() or None


def intended_model(roles_path=ROLES):
    return json.loads(Path(roles_path).read_text())["permission-classifier"]["model"]


def launch_command(settings, log):
    """The documented check: an auto-mode session with a debug file, then one shell command that is not read-only."""
    prompt = "Run `touch classifier-probe.tmp` with the Bash tool, then delete that file and say what happened."
    base = ["claude", "--settings", str(settings), "--permission-mode", "auto", "--debug-file", str(log)]
    return {
        "interactive": shlex.join(base) + "  # then ask: " + prompt,
        "print": shlex.join(base + ["-p", prompt]),
    }


def plan(settings_path, roles_path=ROLES, claude_version=None, probe_proxy=False):
    env = json.loads(Path(settings_path).read_text()).get("env", {})
    intended = intended_model(roles_path)
    configured = env.get(MODEL_KEY)
    server = env.get(SERVER_KEY)
    version = claude_version if claude_version is not None else installed_claude_version()
    reasons = []
    if not isinstance(configured, str) or not configured.strip():
        reasons.append(f"{MODEL_KEY} is unset, so the default classifier model would answer, not {intended}")
    elif bare(configured) != bare(intended):
        reasons.append(f"{MODEL_KEY} is {configured}, not the intended {intended}")
    if str(server).strip() != "0":
        reasons.append(
            f"{SERVER_KEY} is not 0; behind a proxy Claude Code asks the server first and may never send its own request"
        )
    parsed = version_tuple(version)
    if parsed is None:
        reasons.append("Claude Code version unknown; the override needs 2.1.302 or later")
    elif parsed < MIN_VERSION:
        reasons.append(f"Claude Code {version} is older than 2.1.302, which ignores {MODEL_KEY}")
    models, reason = proxy_models(env) if probe_proxy else (None, "Proxy catalog not checked")
    catalog = check_model(intended, models, reason)
    if probe_proxy and not catalog["advertised"]:
        reasons.append(f"{intended} is not advertised by the profile's proxy catalog: {catalog['reason']}")
    return {
        "intendedModel": intended,
        "configuredModel": configured,
        "serverReview": server,
        "claudeVersion": version,
        "catalog": catalog,
        "launch": launch_command(settings_path, Path(settings_path).stem + "-classifier-debug.log"),
        "ready": not reasons,
        "reasons": reasons,
    }


def evidence(debug_log, intended, proxy_logs=None):
    """What the debug log shows the classifier requested, and what the proxy log shows it was served.

    Only the model token after `model=` on the documented marker lines is kept; nothing else in the log
    is copied out, so a logged credential or transcript fragment never reaches the record."""
    lines = Path(debug_log).read_text(errors="replace").splitlines()
    requested = [m.group(1) for line in lines if STARTED in line for m in [REQUEST_MODEL.search(line)] if m]
    ignored = sum(IGNORED in line for line in lines)
    failures = sum(FAILED in line for line in lines)
    proxy = [r for r in (proxy_records(proxy_logs) if proxy_logs else []) if bare(r["requestModel"]) == bare(intended)]
    served = [r for r in proxy if r["responseModel"] and bare(r["responseModel"]) == bare(intended)]
    other = sorted({m for m in requested if bare(m) != bare(intended)})
    reasons = []
    if not requested:
        state = "none"
        reasons.append("no classifier request was sent; read-only calls skip the review, so this launch tested nothing")
    elif other or ignored:
        state = "mismatch"
        if other:
            reasons.append(f"requested {', '.join(other)} instead of {intended}")
        if ignored:
            reasons.append(f"Claude Code logged that it ignored {MODEL_KEY}; the log line names the reason")
    elif failures:
        state = "failed"
        reasons.append(f"{failures} classifier request(s) failed, so actions were denied unreviewed")
    elif served:
        state = "served"
    else:
        state = "requested"
        reasons.append(
            "the proxy log shows no answered request for this model; CLIProxy writes one only with request-log: true"
        )
    return {
        "intendedModel": intended,
        "requests": len(requested),
        "requestedModels": sorted(set(requested)),
        "ignoredOverride": ignored,
        "failures": failures,
        "proxyRequests": len(proxy),
        "proxyServed": len(served),
        "state": state,
        "reasons": reasons,
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    p = sub.add_parser("plan", help="say whether a settings file could request the intended classifier model")
    p.add_argument("--settings", required=True)
    p.add_argument("--roles", default=str(ROLES))
    p.add_argument("--claude-version", help="skip running `claude --version` and use this value")
    p.add_argument("--probe-proxy", action="store_true", help="GET /v1/models with the settings file's proxy env")
    e = sub.add_parser("evidence", help="read the debug log an approved check wrote")
    e.add_argument("--debug-log", required=True)
    e.add_argument("--roles", default=str(ROLES))
    e.add_argument("--proxy-logs", help="CLIProxy request-log directory, when request-log is on")
    args = parser.parse_args()
    try:
        if args.command == "plan":
            report = plan(args.settings, args.roles, args.claude_version, args.probe_proxy)
            ok = report["ready"]
        else:
            report = evidence(args.debug_log, intended_model(args.roles), args.proxy_logs)
            ok = report["state"] == "served"
    except (OSError, ValueError, KeyError):
        print("Invalid settings, roles file, or log", file=sys.stderr)
        return 2
    print(json.dumps(report, indent=2))
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
