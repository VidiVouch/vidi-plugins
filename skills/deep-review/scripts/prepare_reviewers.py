#!/usr/bin/env python3
"""Resolve reviewer policy and cheap capabilities, without running a review."""

import argparse
import hashlib
import http.client
import json
import os
import re
import shutil
import subprocess
import sys
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

SKILL = Path(__file__).resolve().parents[1]
AGENTS = SKILL.parents[1] / "agents"
DIRECT = {
    "astra-standard": "astra-reviewer",
    "astra-adversarial": "astra-adversarial-reviewer",
    "grok": "grok-reviewer",
}
CLI = {
    "codex-native": "codex",
    "codex-adversarial": "codex",
    "cubic": "cubic",
    "coderabbit": "coderabbit",
}
# The three judge definitions a --panel run dispatches instead of the boss (judge-contract.md), one per model family.
JUDGES = ("judge-fable", "judge-astra", "judge-grok")
# Claude Code's extended-context marker. It is stripped before the ID reaches the provider, so the catalog
# advertises the bare ID (models.md, "Suffix rules"); anything else in an ID must be advertised literally.
EXTENDED = "[1m]"


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def proxy_models(env):
    """Catalog evidence only. Never claim tool/schema support or upstream identity."""
    base = env.get("ANTHROPIC_BASE_URL", "").rstrip("/")
    try:
        parsed = urllib.parse.urlsplit(base)
    except ValueError:
        return None, "No valid proxy ANTHROPIC_BASE_URL configured"
    if (
        parsed.scheme not in ("http", "https")
        or not parsed.hostname
        or parsed.username
        or parsed.password
        or parsed.query
        or parsed.fragment
    ):
        return None, "No valid proxy ANTHROPIC_BASE_URL configured"
    # Do not send credentials over remote plaintext connections.
    if parsed.scheme == "http" and parsed.hostname not in ("localhost", "127.0.0.1", "::1"):
        return None, "Remote proxy requires HTTPS"
    url = base + ("/models" if parsed.path.rstrip("/").endswith("/v1") else "/v1/models")
    token = env.get("ANTHROPIC_AUTH_TOKEN") or env.get("ANTHROPIC_API_KEY")
    headers = {"Accept": "application/json"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    request = urllib.request.Request(url, headers=headers)
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), NoRedirect())
    try:
        with opener.open(request, timeout=5) as response:
            payload = response.read(1_048_577)
        if len(payload) > 1_048_576:
            return None, "Proxy catalog exceeds size limit"
        data = json.loads(payload)
        rows = data.get("data") if isinstance(data, dict) else None
        if not isinstance(rows, list) or any(
            not isinstance(row, dict) or not isinstance(row.get("id"), str) for row in rows
        ):
            return None, "Proxy catalog has an unsupported shape"
        return {row["id"] for row in rows}, "Catalog advertised model; execution unverified"
    except urllib.error.HTTPError as exc:
        # Do not log response bodies, URLs, headers, or exception text with secrets.
        return None, f"Proxy catalog HTTP {exc.code}; authentication/availability unverified"
    except (OSError, ValueError, urllib.error.URLError, http.client.HTTPException):
        return None, "Proxy catalog unavailable or invalid; execution unverified"


def agent_model(name):
    text = (AGENTS / f"{name}.md").read_text()
    frontmatter = text.split("---", 2)[1]
    match = re.search(r"^model:\s*([^\n]+)$", frontmatter, re.MULTILINE)
    if not match:
        raise ValueError(f"Missing model in packaged agent {name}")
    return match.group(1).strip().strip("\"'")


# Claude Code reads a boolean env var as on for any of these, in any casing (env-vars reference).
ON = ("1", "true", "yes", "on")


def named_agents(env):
    """Whether a session setting would override every packaged definition's model.

    CLAUDE_CODE_SUBAGENT_MODEL_FORCE makes Claude Code ignore the model field in agent definitions,
    so a definition that pins a full ID would run on the session model under its own name. The
    workflow refuses to start with it on instead of recording the definition's model as if it had
    run. CLAUDE_CODE_SUBAGENT_MODEL alone is a default below the definition and does not block.
    CLAUDE_CODE_FORK_SUBAGENT is not checked: fork mode (on by default since 2.1.302) only lets
    Claude start a fork on its own; a dispatch by agentType is not a fork and starts fresh from its
    definition, and the workflow separately refuses any dispatch that passes a fork option."""
    if env.get("CLAUDE_CODE_SUBAGENT_MODEL_FORCE", "").strip().lower() in ON:
        return {
            "blocked": True,
            "reason": "CLAUDE_CODE_SUBAGENT_MODEL_FORCE is on, so the model in every agent definition is ignored",
        }
    return {"blocked": False, "reason": ""}


def catalog_id(model):
    return model[: -len(EXTENDED)] if model.endswith(EXTENDED) else model


def judge_panel(models, proxy_reason):
    """Catalog evidence for the three judge definitions; the workflow refuses a panel that lacks any of them.

    Same evidence as a direct reviewer lane: the definition's pinned ID must be advertised by the proxy. The
    catalog does not prove tools, structured output, entitlement or which upstream answers, so resolvedModel
    and upstreamModel stay null here and are never filled from a judge's own account of itself."""
    panel = {}
    for name in JUDGES:
        model = agent_model(name)
        bare = catalog_id(model)
        available = models is not None and bare in models
        panel[name] = {
            "agentType": f"vidi-skills:{name}",
            "requestedModel": model,
            "available": available,
            "catalogModel": bare if available else None,
            "reason": (
                proxy_reason
                if models is None or available
                else f"Required model ID {bare} is not advertised by the proxy"
            ),
            "resolvedModel": None,
            "upstreamModel": None,
        }
    return panel


def companion_path():
    root = Path.home() / ".claude/plugins/cache/openai-codex/codex"
    paths = list(root.glob("*/scripts/codex-companion.mjs"))
    paths.sort(key=lambda p: [int(n) for n in re.findall(r"\d+", p.parents[1].name)])
    if not paths or not shutil.which("node"):
        return ""
    try:
        result = subprocess.run(["node", "--check", str(paths[-1])], capture_output=True, timeout=5, check=False)
    except (OSError, subprocess.TimeoutExpired):
        return ""
    return str(paths[-1]) if result.returncode == 0 else ""


def prepare(repo, config=None, probe_proxy=False, env=None, panel=False):
    """Policy and cheap capability evidence for the reviewer lanes, plus the judge panel when `panel` is requested.

    `judges` is None without the request: the workflow then runs its boss stage. With it, the three judge
    definitions get the same catalog check as the direct lanes, and the workflow refuses to start a panel
    missing any of them, so a panel never quietly runs with fewer model families than it claims."""
    env = os.environ if env is None else env
    policies = json.loads((SKILL / "reviewer-defaults.json").read_text())
    config_path = Path(config).expanduser() if config else Path(repo) / ".claude/deep-review.json"
    if config or config_path.exists():
        overrides = json.loads(config_path.read_text())
        if not isinstance(overrides, dict) or set(overrides) - set(policies):
            raise ValueError("Reviewer config must map known reviewer keys to policy objects")
        for key, value in overrides.items():
            if (
                not isinstance(value, dict)
                or set(value) - {"policy", "reason"}
                or value.get("policy") not in ("disabled", "optional", "required")
                or not isinstance(value.get("reason", ""), str)
            ):
                raise ValueError(f"Invalid policy for {key}")
            policies[key] = value

    needs_catalog = panel or any(policies[key]["policy"] != "disabled" for key in DIRECT)
    named = named_agents(env)
    models, proxy_reason = None, "Proxy catalog not checked"
    if needs_catalog and named["blocked"]:
        proxy_reason = "Forced subagent model conflicts with independent named reviewers"
    elif needs_catalog and probe_proxy:
        models, proxy_reason = proxy_models(env)

    reviewers, tools = {}, {}
    for key, policy in policies.items():
        item = {**policy, "available": False}
        if policy["policy"] == "disabled":
            item["reason"] = policy.get("reason") or "Disabled by reviewer policy"
        elif key in DIRECT:
            name = DIRECT[key]
            model = agent_model(name)
            item.update(agentType=f"vidi-skills:{name}", requestedModel=model)
            item["available"] = models is not None and model in models
            item["reason"] = (
                proxy_reason
                if models is None or item["available"]
                else f"Required model ID {model} is not advertised by the proxy"
            )
            item["catalogModel"] = model if item["available"] else None
            item["resolvedModel"] = None
            item["upstreamModel"] = None
        elif key in CLI:
            tool = CLI[key]
            item["available"] = bool(shutil.which(tool))
            item["reason"] = (
                "CLI installed; authentication, subscription and quota unverified"
                if item["available"]
                else f"{tool} CLI missing"
            )
            tools[tool] = item["available"]
            if key == "codex-adversarial" and item["available"]:
                tools["codexCompanion"] = companion_path()
                item["available"] = bool(tools["codexCompanion"])
                tools["codexAdversarial"] = item["available"]
                if not item["available"]:
                    item["reason"] = "Legacy companion absent or unloadable"
        else:
            item.update(available=True, reason="Built-in reviewer; model alias routing unverified")
        reviewers[key] = item

    judges = judge_panel(models, proxy_reason) if panel else None
    identity = {"reviewers": reviewers, "judges": judges}  # requesting the panel changes the attempt identity too
    fingerprint = hashlib.sha256(json.dumps(identity, sort_keys=True).encode()).hexdigest()
    return {
        "reviewers": reviewers,
        "tools": tools,
        "namedAgents": named,
        "judges": judges,
        "reviewerFingerprint": fingerprint,
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo", required=True)
    parser.add_argument("--config")
    parser.add_argument(
        "--probe-proxy", action="store_true", help="GET the configured model catalog; never runs inference"
    )
    parser.add_argument(
        "--panel",
        action="store_true",
        help="also report catalog evidence for the three judge definitions a --panel run dispatches instead of the boss",
    )
    args = parser.parse_args()
    try:
        result = prepare(args.repo, args.config, args.probe_proxy, panel=args.panel)
    except (OSError, ValueError, IndexError):
        print("Invalid reviewer configuration or missing packaged assets; abort review", file=sys.stderr)
        return 2
    print(json.dumps(result, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
