---
name: codex-computer-use
description: Ask Codex CLI (gpt-5.5 or higher e.g., gpt-5.6) to run local app verification that needs computer use, browser automation, simulators, screenshots, app launching, or independent runtime inspection. This is how gpt-5.5/gpt-5.6 is invoked for computer-use work. Use when the user asks Claude to have Codex or gpt-5.5/gpt-5.6 test a flow, verify UI behavior, inspect a running app, capture screenshots, or report confirmation and feedback about implemented behavior that benefits from computer use functionality.
---

# Codex Computer Use

Use Codex as a separate local verification agent when the task needs real UI interaction, screenshots, simulator/browser/device state, or an independent runtime check outside Claude's current context.

For native app control, require Codex to first call `computer-use.list_apps({})` and confirm the requested app/window is listed as controllable. If the app is missing, inaccessible, or not controllable, Codex must stop immediately and report a loud error instead of trying alternate paths such as screenshots, shell automation, browser substitutes, AppleScript, or manual reasoning. The error should clearly name the target app and say that Codex computer use cannot control it.

Do not use this for ordinary code reading, typechecking, linting, or tests Claude can run directly. Launching apps, simulators, or browsers to verify the requested work is fine without asking; ask first only if the run could disrupt the user's environment beyond that (closing their apps, changing system settings, acting on real accounts or data).
