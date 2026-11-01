#!/usr/bin/env python3
"""Check role model IDs and saved model settings against the proxy catalog, without guessing.

Catalog evidence says an ID is advertised. It never proves tool support, entitlement,
or which upstream model answers; those stay null until trusted harness/proxy evidence
exists (resolvedModel, upstreamModel). A near match is reported, never substituted.
"""

import argparse
import json
import os
import sys
from pathlib import Path

from prepare_reviewers import agent_model, proxy_models

SKILL = Path(__file__).resolve().parents[1]
ROLES = SKILL / "model-roles.json"
# Claude Code strips a trailing [1m] before sending the ID to the provider (model-config docs),
# so the catalog is checked for the bare ID. Nothing else is stripped: a speed or effort suffix
# is part of the requested ID and must be advertised literally.
EXTENDED = "[1m]"
MODEL_ENV_KEYS = (
    "ANTHROPIC_MODEL",
    "ANTHROPIC_DEFAULT_MODEL",
    "ANTHROPIC_DEFAULT_FABLE_MODEL",
    "ANTHROPIC_DEFAULT_OPUS_MODEL",
    "ANTHROPIC_DEFAULT_SONNET_MODEL",
    "ANTHROPIC_DEFAULT_HAIKU_MODEL",
    "CLAUDE_CODE_SUBAGENT_MODEL",
    "CLAUDE_CODE_AUTO_MODE_MODEL",
)


def split_model(model):
    """Separate the documented client-side suffix from the ID the catalog must advertise."""
    model = model.strip()
    extended = model.endswith(EXTENDED)
    catalog_id = model[: -len(EXTENDED)] if extended else model
    unverified = None
    if EXTENDED in catalog_id:
        head, _, tail = catalog_id.partition(EXTENDED)
        unverified = f"{EXTENDED}{tail}"
        if tail:
            unverified += " after the context suffix is not a documented spelling"
    return {"requestedModel": model, "catalogId": catalog_id, "extendedContext": extended, "unverifiedSuffix": unverified}


def _fold(model):
    return model.lower().replace(".", "-")


def near_matches(catalog_id, models):
    """Advertised IDs that differ only by case or dot-versus-hyphen. Evidence for a human, not a substitute."""
    return sorted(m for m in models if m != catalog_id and _fold(m) == _fold(catalog_id))


def check_model(model, models, reason):
    item = split_model(model)
    if models is None:
        item.update(advertised=None, catalogModel=None, nearMatches=[], reason=reason)
    else:
        advertised = item["catalogId"] in models
        item.update(
            advertised=advertised,
            catalogModel=item["catalogId"] if advertised else None,
            nearMatches=[] if advertised else near_matches(item["catalogId"], models),
        )
        item["reason"] = reason if advertised else f"Requested ID {item['catalogId']} is not advertised by the proxy"
    item.update(resolvedModel=None, upstreamModel=None)
    return item


def check_roles(roles, models, reason):
    result = {}
    for name, role in roles.items():
        item = {"provider": role.get("provider"), **check_model(role["model"], models, reason)}
        if "agent" in role:
            item["agentModel"] = agent_model(role["agent"])
            item["agentAgrees"] = item["agentModel"] == item["requestedModel"]
        result[name] = item
    return result


def settings_models(path):
    """Model IDs a CCS/Claude settings file would launch with. Only model keys are read out."""
    data = json.loads(Path(path).read_text())
    found = {}
    for key in MODEL_ENV_KEYS:
        value = data.get("env", {}).get(key)
        if isinstance(value, str) and value.strip():
            found[f"env:{key}"] = value
    for option in data.get("modelPicker", {}).get("options", []):
        if isinstance(option, dict) and isinstance(option.get("model"), str):
            found[f"picker:{option.get('label', option['model'])}"] = option["model"]
    return found


def check(roles_path=ROLES, settings=(), probe_proxy=False, env=None):
    env = os.environ if env is None else env
    models, reason = (None, "Proxy catalog not checked")
    if probe_proxy:
        models, reason = proxy_models(env)
    roles = json.loads(Path(roles_path).read_text())
    report = {
        "catalog": {"checked": models is not None, "reason": reason, "advertisedCount": len(models or ())},
        "roles": check_roles(roles, models, reason),
        "settings": {},
    }
    for path in settings:
        report["settings"][str(path)] = {
            source: check_model(model, models, reason) for source, model in settings_models(path).items()
        }
    return report


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--roles", default=str(ROLES))
    parser.add_argument("--settings", action="append", default=[], help="settings JSON to check; repeatable")
    parser.add_argument(
        "--probe-proxy",
        action="store_true",
        help="GET /v1/models using ANTHROPIC_BASE_URL and token from the first --settings env, else the environment",
    )
    args = parser.parse_args()
    env = None
    if args.settings and args.probe_proxy:
        env = json.loads(Path(args.settings[0]).read_text()).get("env", {})
    try:
        report = check(args.roles, args.settings, args.probe_proxy, env)
    except (OSError, ValueError, IndexError):
        print("Invalid roles, settings, or packaged agent definitions", file=sys.stderr)
        return 2
    print(json.dumps(report, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
