#!/usr/bin/env python3
"""Plan the permission-classifier routing check and read its evidence, without running it.

The classifier is a separate request Claude Code sends in auto mode. It has no agent definition,
so its model comes only from CLAUDE_CODE_AUTO_MODE_MODEL in the launching profile. `plan` reads
the model keys of a settings file and says whether an approved live check could request the
intended model; `evidence` reads the debug log that check wrote and says what was requested and
whether the proxy served it. A launch that sent no classifier request proves nothing: reads and
working-directory edits skip the review, so a read-only worker run is never a classifier test.

`eligibility` answers a different question: whether auto mode is available at all to a session on
the profile's main model. The permission-modes page lists the Claude families that support it and
says any other model starts the session in Manual. The classifier variable is not in that list, so
pinning a Sonnet classifier never makes a GPT or Grok session model eligible.
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
# Session-model rules from the permission-modes page (read 2026-11-01). On the Anthropic API auto mode needs
# Opus 4.6 or later, Sonnet 4.6 or later, Haiku 5.5, or a Fable model; Bedrock, Agent Platform, Foundry and the
# apps gateway raise the floors to Opus 4.7 and Sonnet 5. "On any other model, the session starts in Manual."
# An LLM gateway has no row of its own, so a proxy session is graded by the API list and the gap is reported.
CLAUDE_ID = re.compile(r"^claude-(fable|opus|sonnet|haiku)-(\d+)(?:-(\d+))?(?:-|$)")
API_FLOORS = {"fable": (0, 0), "opus": (4, 6), "sonnet": (4, 6), "haiku": (5, 5)}
STRICT_FLOORS = {"fable": (0, 0), "opus": (4, 7), "sonnet": (5, 0), "haiku": (5, 5)}
ALIASES = ("fable", "opus", "sonnet", "haiku")
SESSION_SOURCES = (("env", "ANTHROPIC_MODEL"), ("setting", "model"), ("env", "ANTHROPIC_DEFAULT_MODEL"))
MANUAL = "not a Claude family the permission-modes page lists for auto mode; the session starts in Manual instead"


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


def grade_session_model(model, env):
    """Whether a session on this model can be in auto mode, by the documented family list alone.

    An alias follows the profile's ANTHROPIC_DEFAULT_<FAMILY>_MODEL when set; otherwise Claude Code resolves it,
    and only `fable` has a documented native target (Fable 5.1). Nothing is inferred about undocumented IDs."""
    item = {"requestedModel": model, "resolvedModel": model, "supported": None, "reason": None}
    base = bare(model)
    if base in ALIASES:
        pinned = env.get(f"ANTHROPIC_DEFAULT_{base.upper()}_MODEL")
        if isinstance(pinned, str) and pinned.strip():
            item["resolvedModel"], base = pinned.strip(), bare(pinned)
        elif base == "fable":
            item.update(
                supported=True, reason="fable resolves to Fable 5.1 unless ANTHROPIC_DEFAULT_FABLE_MODEL is set"
            )
            return item
        else:
            item["reason"] = (
                f"alias {base} is resolved by Claude Code, not this file; supported only if it lands on a listed version"
            )
            return item
    match = CLAUDE_ID.match(base)
    if not match:
        item.update(supported=False, reason=f"{base} is {MANUAL}")
        return item
    family, major, minor = match.group(1), int(match.group(2)), int(match.group(3) or 0)
    version = (major, minor)
    ok = version == API_FLOORS[family] if family == "haiku" else version >= API_FLOORS[family]
    if not ok:
        item.update(
            supported=False,
            reason=f"{family} {major}.{minor} is older than the auto mode floor; the session starts in Manual",
        )
    elif family != "haiku" and version < STRICT_FLOORS[family]:
        item.update(
            supported=True,
            reason=f"{family} {major}.{minor} is listed for the Anthropic API only; Bedrock, Agent Platform, Foundry and the apps gateway need {'.'.join(map(str, STRICT_FLOORS[family]))} or later",
        )
    else:
        item.update(supported=True, reason=f"{family} {major}.{minor} is listed as supporting auto mode")
    return item


def eligibility(settings_path, roles_path=ROLES):
    """Expected starting mode for a session on a settings profile, and which roles' models never decide it."""
    data = json.loads(Path(settings_path).read_text())
    env = data.get("env", {})
    source = model = None
    for kind, key in SESSION_SOURCES:
        value = env.get(key) if kind == "env" else data.get(key)
        if isinstance(value, str) and value.strip():
            source, model = f"{kind}:{key}", value.strip()
            break
    session = (
        grade_session_model(model, env) if model else {"requestedModel": None, "resolvedModel": None, "supported": None}
    )
    session["source"] = source
    if not model:
        session["reason"] = (
            "this file sets no session model; the launch inherits one from --model, the user settings or the saved /model choice"
        )
    picker = [
        grade_session_model(o["model"], env)
        for o in data.get("modelPicker", {}).get("options", [])
        if isinstance(o, dict) and isinstance(o.get("model"), str)
    ]
    roles = json.loads(Path(roles_path).read_text())
    workers = {
        name: {"model": r["model"], "provider": r.get("provider")}
        for name, r in roles.items()
        if r.get("provider") != "claude"
    }
    reasons = [] if session["supported"] else [session["reason"]]
    configured = env.get(MODEL_KEY)
    if configured and not session["supported"]:
        reasons.append(
            f"{MODEL_KEY}={configured} picks the classifier model only; it is not an availability requirement and does not make the session model eligible"
        )
    return {
        "sessionModel": session,
        "expectedStart": {True: "auto", False: "manual"}.get(session["supported"], "unknown"),
        "classifierOverride": {"configured": configured, "affectsEligibility": False},
        "picker": picker,
        "nonClaudeWorkers": workers,
        "workerNote": "a worker runs under the launching session's mode; the documentation states eligibility only for the session model, so a non-Claude worker model neither grants nor removes auto mode and its own behaviour as a session model is graded above",
        "ready": session["supported"] is True,
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
    g = sub.add_parser("eligibility", help="say whether a session on a settings file's main model can be in auto mode")
    g.add_argument("--settings", required=True)
    g.add_argument("--roles", default=str(ROLES))
    args = parser.parse_args()
    try:
        if args.command == "plan":
            report = plan(args.settings, args.roles, args.claude_version, args.probe_proxy)
            ok = report["ready"]
        elif args.command == "eligibility":
            report = eligibility(args.settings, args.roles)
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
