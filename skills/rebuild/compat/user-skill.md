---
name: rebuild
description: Compatibility entry for the user-level /rebuild command. It forwards to the vidi-skills:rebuild plugin skill, the single rebuild implementation, after mapping or rejecting the older argument forms. Use for any fresh-authoring rebuild of a target ref's end-state; the procedure lives in the plugin, not here.
argument-hint: "--target <ref> --from <base> --into <branch> [--guide <range>] [--continue] [--epoch <id>] [--gate <cmd>] [--max-rounds <n>]"
allowed-tools: Skill
---

<!-- forwarding entry for vidi-skills:rebuild; staged copy: skills/rebuild/compat/user-skill.md in VidiVouch/vidi-plugins -->

# Rebuild (forwarding entry)

This file keeps `/rebuild` working after the rebuild procedure moved into the
`vidi-skills` plugin. It holds no procedure of its own. The one implementation
is `vidi-skills:rebuild`, which dispatches `rebuild-plan`, `rebuild-epoch` and
`rebuild-examine`. Do not reproduce any older phase-by-phase rebuild text from
memory; it is retired, and writing it here would create a second implementation
that drifts from the plugin.

## What to do

1. Map `$ARGUMENTS` with the table below. Stop with the listed message on any
   rejection; never guess a value.
2. Invoke the `vidi-skills:rebuild` skill with the Skill tool, passing the
   mapped argument string unchanged.
3. If `vidi-skills:rebuild` is not in the available skills list, stop and tell
   the user to install the plugin (`/plugin marketplace add VidiVouch/vidi-plugins`,
   then `/plugin install vidi-skills@vidi`). Do not improvise a rebuild inline.

## Argument mapping

The old user-level skill accepted `--target <ref>`, optional `--guide <range>`,
optional `--from <base>` and optional `--into <branch>`, and left the base and
branch implicit when they were missing. The plugin creates a branch and a
worktree from `--from`, so an implicit base would silently build on whatever
HEAD happens to be. Missing values are therefore rejected, not defaulted.

| Argument | Handling |
|---|---|
| `--target <ref>` | Forwarded unchanged. Required for a fresh start; optional with `--continue`. |
| `--guide <range>` | Forwarded unchanged. Optional, as before. |
| `--from <base>` | Forwarded unchanged. Required for a fresh start (it was optional). If missing, reject: "rebuild needs --from <base>, the clean commit or branch to build on. The old implicit base is retired." |
| `--into <branch>` | Forwarded unchanged. Required for a fresh start and for `--continue` (it was optional). If missing, reject: "rebuild needs --into <branch>, the branch the rebuild is authored on. The old implicit branch is retired." |
| `--continue`, `--epoch <id>`, `--gate <cmd>`, `--max-rounds <n>` | Plugin-only flags, forwarded unchanged. With `--continue`, only `--into` is required here; the plugin re-derives the rest from its ledger. |
| Positional arguments | Rejected: "rebuild takes only --flags; a bare value is ambiguous between --target and --from." |
| Any other `--flag` | Rejected by name: "rebuild does not accept <flag>." Never drop a flag silently. |

Every rejection message ends with the full plugin form from the argument hint
above, so the user can rerun with one edit.

## Why a forwarding entry rather than deleting /rebuild

Users and notes refer to `/rebuild`. Keeping the name avoids breaking those
references while making sure every invocation reaches the same implementation.
When the plugin's `rebuild` arguments change, update the staged copy in the
plugin repository and reactivate it with
`skills/rebuild/scripts/user_skill_forwarding.py activate`; do not edit this
file in place.
