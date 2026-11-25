---
name: codex-computer-use
description: Ask Codex CLI (gpt-6.1-terra, or gpt-6.2-sol for bigger tasks) to run local app verification that needs computer use, browser automation, simulators, screenshots, app launching, or independent runtime inspection. This is how the GPT models are invoked for computer-use work. Use when the user asks Claude to have Codex or GPT test a flow, verify UI behavior, inspect a running app, capture screenshots, or report confirmation and feedback about implemented behavior that benefits from computer use functionality.
---

# Codex Computer Use

Use Codex as a separate local verification agent when the task needs real UI interaction, screenshots, simulator/browser/device state, or an independent runtime check outside Claude's current context.

For native app control, require Codex to first call `computer-use.list_apps({})` and confirm the requested app/window is listed as controllable. If the app is missing, inaccessible, or not controllable, Codex must stop immediately and report a loud error instead of trying alternate paths such as screenshots, shell automation, browser substitutes, AppleScript, or manual reasoning. The error should clearly name the target app and say that Codex computer use cannot control it.

Do not use this for ordinary code reading, typechecking, linting, or tests Claude can run directly. Launching apps, simulators, or browsers to verify the requested work is fine without asking; ask first only if the run could disrupt the user's environment beyond that (closing their apps, changing system settings, acting on real accounts or data).

## Failure is not a clean pass

A "verification passed" from a codex run that never actually executed is the worst false positive this skill can produce — it green-lights shipping unverified work. Before relaying any verification verdict, confirm the run itself completed per the codex-cli skill's degradation ladder: exit code 0, no error/`turn.failed` events, and output that describes the requested verification (what was launched, what was observed), not a crash trace. Known trap: `failed to initialize in-process app-server client: Operation not permitted` — the codex run died before doing anything; some sandbox configurations block the GPT app-server. Any such failure is a **degraded** run: report the error verbatim and say plainly that the verification did NOT happen — never fold it into a pass, and never retry with `--dangerously-bypass-approvals-and-sandbox`.
