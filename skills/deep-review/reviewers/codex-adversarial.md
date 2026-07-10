# Agent C: Codex adversarial review

Skip entirely if the Phase 2 preamble reported `CODEX_ADVERSARIAL=no` — that covers both "`codex` CLI missing" and "codex CLI present but companion script missing", since Agent C needs both. Do NOT fall back to invoking the companion with an empty `CODEX_COMPANION` — that expands to `node "" adversarial-review ...` and fails noisily.

## Launch prompt

Launch a background general-purpose Agent with prompt:

> Run the codex companion script with the `adversarial-review` subcommand. This path templates the plugin's adversarial prompt from `prompts/adversarial-review.md` (skeptical framing, strict grounding rules, JSON-schema output) and hands it to codex via the app-server with an outputSchema attached, returning a structured payload with `result.findings`. Use the `CODEX_COMPANION` path resolved in the Phase 2 preamble.
>
> **Worktree targeting:** pass `--cwd "<REPO_ROOT>"` (or `-C "<REPO_ROOT>"`) to the companion on every invocation. Its `resolveCommandCwd` falls back to `process.cwd()` when `--cwd` is absent, and the Bash tool's cwd is unreliable across calls — so a bare invocation from a worktree can silently retarget the MAIN repo and review the wrong diff.
>
> - `--base main` mode (default): `node "$CODEX_COMPANION" adversarial-review --cwd "<REPO_ROOT>" --json --base <base> <focus>`
> - `--uncommitted` mode: `node "$CODEX_COMPANION" adversarial-review --cwd "<REPO_ROOT>" --json --scope working-tree <focus>`
> - `--commit <sha>` mode: `node "$CODEX_COMPANION" adversarial-review --cwd "<REPO_ROOT>" --json --commit <sha> <focus>`
> - `--range <a>..<b>` mode: the companion's `--base` flag reviews from that commit to HEAD, not to a specific upper bound. Perform the checkout dance inside the target worktree in a single chained Bash call: `cd "<REPO_ROOT>" && ORIG=$(git symbolic-ref --quiet --short HEAD 2>/dev/null || git rev-parse --verify HEAD) && git checkout <b> && (node "$CODEX_COMPANION" adversarial-review --cwd "<REPO_ROOT>" --json --base <a> <focus>); rc=$?; git checkout "$ORIG"; exit $rc`.
>
> Include `<focus>` text only if `--focus` was provided; omit otherwise. Return the raw JSON stdout verbatim — the companion's envelope with `result.findings` carrying the structured findings.
>
> **Post-run sanity check:** before returning, (a) verify `.context.repoRoot` in the returned JSON matches `<REPO_ROOT>` — if not, the review targeted the wrong repo; re-run with `--cwd` explicitly set and flag the mismatch so the orchestrator discards the bogus findings; and (b) validate the output envelope against the contract in "Contract drift" below — if the shape doesn't match, return the `CONTRACT-DRIFT` marker, never an empty/clean result.
>
> **Envelope shape note:** codex-cli 0.143+ flattened the envelope — findings live at `.result.findings`, not `.result.parsed.findings`. If a future codex-cli reintroduces a `.parsed` wrapper (or any other reshaping), that is drift too — validate the actual shape you received against the contract below rather than assuming either form.
>
> **Version attribution:** also capture `codex --version` and the plugin version from the `CODEX_COMPANION` path (the `…/codex/<version>/scripts/…` segment) into the returned payload, so any drift can be pinned to a specific codex-cli / plugin bump and this spec re-verified against it.

## Parsing & degradation

The companion script wraps the raw codex output in an envelope. The schema-conformant findings live at `.result.findings`. Different schema than native! Each finding:

```
{
  "severity": "critical" | "high" | "medium" | "low",
  "title": str,
  "body": str,
  "file": str,
  "line_start": int,
  "line_end": int,
  "confidence": float,
  "recommendation": str
}
```

Normalise each finding as:

- `file` ← `file` (already relative in practice)
- `line_start` / `line_end` ← (already flat ints)
- `severity` ← `severity` (already categorical — use directly, map `"high"` → `"warning"` and `"medium"`/`"low"` → `"nit"` for the final report if you want to match native severity levels)
- `description` ← `title` + `body`
- `recommendation` ← `recommendation`
- Tag with `source: "codex-adversarial"`.
- Also pull `.result.summary` aside as the adversarial ship/no-ship narrative for Phase 4 commentary.
- If `.parseError` is non-null **and** case-insensitively matches the same quota/rate-limit indicator set used for Agent B (including "rate limit", "quota", "too many requests", and "429"): this is a quota/rate-limit error, not a parse failure. Match with a case-insensitive regex or lowercase before comparing — observed casings vary. Mark Agent C as `degraded` (same as Agent B). Do NOT synthesise a warning finding from it — no review data exists to act on.
- If `.parseError` is non-null but does NOT match the quota pattern: fall back to treating `.rawOutput` as a single opaque finding with severity `"warning"` and note the parse error text verbatim in the Phase 4 output.

## Contract drift — complain loudly, never a silent pass

This reviewer rides an **internal, undocumented** interface: `codex-companion.mjs` is the codex plugin's private app-server script (the plugin README documents only the interactive `/codex:adversarial-review` command, not this scriptable path). Its subcommand, flags, and output envelope are **not a supported public contract** and have broken across versions before (e.g. adversarial-review failing on codex-cli 0.125.0 under the default model). A drift that silently yields zero findings reads as a clean adversarial pass — the exact failure this section exists to prevent.

**Validate the contract on every run, before normalising. Any mismatch below is DRIFT, not a review result: mark Agent C `degraded` with reason `CONTRACT-DRIFT`, state plainly what changed (expected vs. observed), echo the actual top-level JSON keys and the versions captured above, and return that as a loud marker the orchestrator MUST surface in Phase 4 — never fold it into "no findings" or a clean pass.**

Validate in this order (envelope shape first, so a reshaped envelope can't sneak through as `.parseError`-null → zero findings):

1. **Invocation contract.** `CODEX_COMPANION` resolves to an existing file `node` can run, and the `adversarial-review` invocation does not exit with an arg-parse/usage error. A rejected subcommand or a rejected `--json` / `--cwd` / `--scope` / `--base` / `--commit` flag means the companion's CLI changed → `CONTRACT-DRIFT`; quote the stderr.
2. **Envelope contract.** stdout parses as a single JSON object with `.result.findings` as an **array**, `.result.summary` a string, `.context.repoRoot` present, and `.parseError` / `.rawOutput` present as the documented error channel. If stdout doesn't parse, or `.result` is absent, or `.result.findings` is not an array → `CONTRACT-DRIFT`.
3. **Finding-shape contract.** Findings, when present, carry the keys in the schema above (`severity`, `title`, `body`, `file`, `line_start`, `line_end`, `confidence`, `recommendation`). Missing/renamed keys → `CONTRACT-DRIFT` (report which keys you actually saw).

Keep the four terminal states distinct — three look alike and must never be conflated:

| State | Signal | Outcome |
|---|---|---|
| **findings** | valid envelope, `findings` non-empty | normalise as above |
| **clean** | valid envelope, `findings: []`, `.parseError` null | report "no adversarial findings" — a real clean pass |
| **quota/rate-limit** | `.parseError` matches the quota set | `degraded`, no data (NOT drift) |
| **CONTRACT-DRIFT** | invocation/envelope/finding shape breaks the contract above | `degraded` + **loud** marker; escalate, do NOT report clean |

The quota check (bullet above) only applies once the envelope itself is valid — if the envelope is malformed, that's drift, regardless of any `.parseError` text.
