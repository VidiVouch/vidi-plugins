# Vidi Plugins

Lean plugin repo for Vidi's AI-driven development workflows across Claude Code and Codex.

## Contents

- `check-coverage`
- `codex-cli`
- `codex-computer-use`
- `codex-review`
- `deep-eval`
- `deep-review`
- `epoch-review`
- `html-report`
- `local-review`
- `rebuild`
- `rebuild-plan`
- `rebuild-epoch`
- `rebuild-examine`
- `vidi-review-attestation`

## Layout

- `skills/` contains the skill directories loaded by Claude Code and Codex.
- `.claude-plugin/plugin.json` makes the repo installable as a Claude Code plugin.
- `.codex-plugin/plugin.json` makes the repo installable as a Codex plugin.
- `.agents/plugins/marketplace.json` describes the Codex marketplace entry for `VidiVouch/vidi-plugins`.
- `.claude-plugin/marketplace.json` describes the Claude Code marketplace entry for `VidiVouch/vidi-plugins`.

## Install

### Claude Code

```text
/plugin marketplace add VidiVouch/vidi-plugins
/plugin install vidi-skills@vidi
```

Send those as separate prompts. The Claude Code desktop app can add the same
marketplace from the plugins UI by choosing add from repository and entering
`https://github.com/VidiVouch/vidi-plugins`.

### Codex

```bash
codex plugin marketplace add VidiVouch/vidi-plugins
codex plugin add vidi-skills@vidi
```

The marketplace marks `vidi-skills` as installed by default. If your Codex UI
shows it as available after adding the marketplace, add it from `/plugins` or
run the second command.

## Development

Edit skills in `skills/<name>/`, then validate:

```bash
uv run python /path/to/plugin-creator/scripts/validate_plugin.py .
```

Some Claude-facing skills intentionally keep Claude Code command metadata such as
`argument-hint`; use the plugin validator for release checks.
