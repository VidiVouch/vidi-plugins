export const meta = {
  name: 'deep-review',
  description: 'Multi-tool review → boss-arbitrated judgment → fix → reconverge loop',
  phases: [
    // Detection is deterministic in the skill (via `!` injection) BEFORE this
    // runs, so there is no agentless "Detect" phase here (it just renders as a
    // stalled empty box). The active reviewer set is a narrator line instead.
    { title: 'Review', detail: 'one agent per available reviewer (parallel; codex/cubic serial in range mode)' },
    { title: 'Judge', detail: 'the boss (arbiter) rules the panel: dedup → real-vs-noise → severity → verdict' },
    { title: 'Fix', detail: 'fixer subagents, deterministically sized, one disjoint file-bucket each' },
    { title: 'Conclude', detail: 'loop iff a fix landed and the round cap is not hit' },
  ],
}

// ---------------------------------------------------------------------------
// Three roles (per the deep-review design):
//   Runtime/orchestrator — Claude, always: this workflow holds the loop.
//   Workers  — the review panel (REGISTRY): each produces findings independently.
//   Boss     — the arbiter with final say: dedup → real-vs-noise → severity →
//              verdict. Configurable via args.boss; default inherits the session
//              model (omit `model` on the agent → runs as whatever Claude is now).
//
// args (populated deterministically by SKILL.md, mostly via `!` injection):
//   mode        : 'base' | 'uncommitted' | 'commit' | 'range'
//   base        : base branch          (base mode)
//   sha         : commit sha           (commit mode)
//   rangeA/rangeB : range endpoints    (range mode)
//   rangeBIsHead : bool — rangeB == live HEAD (skill computes); checkout dance
//                  is a no-op, so range externals run parallel, danceless
//   focus       : optional --focus text ('' if none)
//   repoRoot    : absolute review root — normally a FROZEN git worktree the skill
//                 created at the review's start state (live tooling/specs copied
//                 in). Falls back to the live checkout under --in-place.
//   toolchain   : 'python' | 'rust' | 'none'
//   fix         : bool  — apply verified fixes and loop (default false; the
//                 `teams` fix-strategy passes false and fixes via a main-agent team)
//   maxRounds   : int   — hard cap on review↔fix rounds (default 3)
//   boss        : string[] — arbiter model(s). [] = one boss inheriting the
//                 session model. ['codex'] = gpt-5.x via codex exec. Two entries
//                 = [logicBoss, tasteBoss], domain-authoritative merge.
//   tools       : { codex, codexAdversarial, cubic, codexCompanion }
// ---------------------------------------------------------------------------
const A = typeof args === 'string' ? JSON.parse(args) : args || {}
if (!A.repoRoot || !A.mode) {
  throw new Error(
    `deep-review: args did not arrive intact (repoRoot=${A.repoRoot}, mode=${A.mode}, tools=${JSON.stringify(A.tools)}). ` +
      `Pass args as a JSON object (or a JSON string this guard can parse).`,
  )
}
const MAX_ROUNDS = A.maxRounds || 3
const FIXER_CAP = 5 // agent-teams sizing heuristic: 3-5 workers, ~5 tasks each
const SPEC_DIR = `${A.repoRoot}/.claude/skills/deep-review/reviewers`
// Codex-family boss ids route through `codex exec`; everything else is a native
// in-process Claude agent. Adding gpt-5.6 (sol/terra/luna) needs no code change.
const CODEX_FAMILY = /^(codex|gpt-?5|sol|terra|luna)/i

// Data-driven registry — adding CodeRabbit et al. is one row here plus one
// reviewers/<key>.md spec. `need` names the args.tools flag that gates it;
// `serialInRange` marks reviewers whose checkout dance can't race in range mode.
// model/effort follow the project rubric: opus for deep bug reasoning, sonnet
// for pattern-matching, thin sonnet/low WRAPPERS for the external CLIs (the real
// work is gpt-5.x via codex, or cubic's own service). `label` names the real worker.
const REGISTRY = [
  { key: 'claude-bugs',        always: true,             serialInRange: false, external: false, model: 'opus',   effort: 'high',   label: 'claude-bugs' },
  { key: 'claude-conventions', always: true,             serialInRange: false, external: false, model: 'sonnet', effort: 'medium', label: 'claude-conv' },
  { key: 'coderabbit',         need: 'coderabbit',       serialInRange: true,  external: true,  model: 'sonnet', effort: 'low',    label: 'coderabbit' },
  { key: 'codex-native',       need: 'codex',            serialInRange: true,  external: true,  model: 'sonnet', effort: 'low',    label: 'gpt-5.5:codex-native' },
  { key: 'codex-adversarial',  need: 'codexAdversarial', serialInRange: true,  external: true,  model: 'sonnet', effort: 'low',    label: 'gpt-5.5:codex-adv' },
  { key: 'cubic',              need: 'cubic',            serialInRange: true,  external: true,  model: 'sonnet', effort: 'low',    label: 'cubic' },
]

// ---- schemas -------------------------------------------------------------
const FINDING = {
  type: 'object',
  required: ['file', 'line_start', 'line_end', 'severity', 'description'],
  properties: {
    file: { type: 'string' },
    line_start: { type: 'integer' },
    line_end: { type: 'integer' },
    severity: { type: 'string', enum: ['critical', 'warning', 'nit'] },
    description: { type: 'string' },
    recommendation: { type: 'string' },
  },
}
const REVIEW_OUT = {
  type: 'object',
  required: ['findings', 'degraded'],
  properties: {
    findings: { type: 'array', items: FINDING },
    degraded: { type: 'boolean' },
    reason: { type: 'string' },
  },
}
const BOSS_OUT = {
  type: 'object',
  required: ['rulings'],
  properties: {
    rulings: {
      type: 'array',
      items: {
        type: 'object',
        required: ['id', 'real', 'severity'],
        properties: {
          id: { type: 'integer' },
          real: { type: 'boolean' },
          severity: { type: 'string', enum: ['critical', 'warning', 'nit'] },
          reason: { type: 'string' },
          domain: { type: 'string', enum: ['logic', 'taste'] },
        },
      },
    },
    summary: { type: 'string' },
  },
}

// ---- helpers -------------------------------------------------------------
const SEV_RANK = { critical: 3, warning: 2, nit: 1 }
const maxSev = (a, b) => (SEV_RANK[b] > SEV_RANK[a || 'nit'] ? b : a || 'nit')
const normPath = (p) => (p || '').replace(/^\.\//, '')
const shortFile = (p) => normPath(p).split('/').slice(-2).join('/')
const dedupKey = (f) => `${normPath(f.file)}:${f.line_start}-${f.line_end}:${(f.description || '').slice(0, 80)}`

function modeLine() {
  switch (A.mode) {
    case 'uncommitted':
      return '--uncommitted (staged + unstaged, INCLUDING untracked files — `git diff HEAD` alone will not see new files, so also list them via `git ls-files --others --exclude-standard` and read their full contents)'
    case 'commit':
      return `--commit ${A.sha}`
    case 'range':
      return `--range ${A.rangeA}..${A.rangeB}${A.rangeBIsHead ? ' (the range upper bound IS the current HEAD, so the spec’s checkout dance is a no-op — SKIP it and run the tool directly against the current tree)' : ''}`
    default:
      return `--base ${A.base}`
  }
}
const focusClause = () => (A.focus ? `\n\nReviewer focus (from --focus): ${A.focus}` : '')

// Group findings by file so each fixer owns a disjoint file set.
function groupByFile(findings) {
  const m = new Map()
  for (const f of findings) {
    const k = normPath(f.file)
    if (!m.has(k)) m.set(k, [])
    m.get(k).push(f)
  }
  return [...m.entries()]
}

// Round-robin items into n buckets (drop empties).
function bucketize(items, n) {
  const b = Array.from({ length: n }, () => [])
  items.forEach((it, i) => b[i % n].push(it))
  return b.filter((x) => x.length)
}

// Cluster overlapping line ranges within a file → one representative finding
// carrying every source that agreed (cross-tool agreement = high confidence)
// and EVERY distinct description — overlap is a hint two findings are the same
// issue, not proof. A broad whole-function finding must not swallow a narrower
// distinct one before the boss rules on it; the boss sees all merged texts and
// makes the same-issue call itself.
function dedupeByOverlap(findings) {
  const byFile = {}
  for (const f of findings) (byFile[normPath(f.file)] ||= []).push(f)
  const out = []
  for (const [file, fs] of Object.entries(byFile)) {
    fs.sort((a, b) => (a.line_start || 0) - (b.line_start || 0))
    let c = null
    const flush = () => {
      if (!c) return
      out.push({
        file,
        line_start: c.line_start,
        line_end: c.line_end,
        severity: c.severity,
        sources: [...c.sources],
        description: c.descriptions.join('\n[merged overlapping finding] '),
        recommendation: c.recommendations.join('\n[merged] '),
      })
    }
    for (const f of fs) {
      if (c && (f.line_start || 0) <= (c.line_end || 0)) {
        c.line_end = Math.max(c.line_end || 0, f.line_end || 0)
        c.severity = maxSev(c.severity, f.severity)
        c.sources.add(f.source)
        if (f.description && !c.descriptions.includes(f.description)) c.descriptions.push(f.description)
        if (f.recommendation && !c.recommendations.includes(f.recommendation)) c.recommendations.push(f.recommendation)
      } else {
        flush()
        c = {
          line_start: f.line_start || 0,
          line_end: f.line_end || 0,
          severity: f.severity,
          sources: new Set([f.source]),
          descriptions: f.description ? [f.description] : [],
          recommendations: f.recommendation ? [f.recommendation] : [],
        }
      }
    }
    flush()
  }
  return out
}

// ---- prompt builders -----------------------------------------------------
function reviewPrompt(r) {
  const runLine = r.external
    ? `This is an EXTERNAL-TOOL reviewer. Run the tool exactly as the spec's "Launch prompt" directs (REPO_ROOT=${A.repoRoot}${r.key === 'codex-adversarial' ? `, CODEX_COMPANION=${A.tools?.codexCompanion || ''}` : ''}), then parse its output into the findings schema per the spec's "Parsing & degradation" section. If the tool crashed, rate-limited, or was misconfigured, set degraded=true with the reason and findings=[] — a degraded run is NOT a clean pass.`
    : `Perform the review directly as the spec describes and return the findings.`
  return [
    `Read the reviewer spec at ${SPEC_DIR}/${r.key}.md and follow its "Launch prompt" section exactly for this diff target: ${modeLine()}.`,
    runLine + focusClause(),
    `Return raw findings (the boss arbiter judges them later — do NOT self-censor): {"findings": [...], "degraded": <bool>, "reason": "<short>"}. Each finding: {file (repo-relative), line_start, line_end, severity one of "critical"|"warning"|"nit", description, recommendation}.`,
  ].join('\n\n')
}

function bossPrompt(findings) {
  return [
    `You are the ARBITER ("boss") of a multi-tool code review — the final say. The panel produced these DEDUPED findings against the code in ${A.repoRoot}. Rule each one.`,
    `For every finding: verify it against the ACTUAL current code (read the cited file and lines), decide real (true) vs noise (false), and assign a final severity. Default real=false when you cannot confirm it — false positives are worse than misses, especially a single-source "critical". Honor REVIEW.md's "Skip" section: a match → real=false. Also classify each finding's domain: "logic" (correctness / repro / resource / type) or "taste" (design / API shape / naming / copy / UX).`,
    JSON.stringify(
      findings.map((f) => ({
        id: f.id,
        file: f.file,
        line_start: f.line_start,
        line_end: f.line_end,
        severity: f.severity,
        sources: f.sources,
        description: f.description,
        recommendation: f.recommendation,
      })),
      null,
      2,
    ),
    `Return {"rulings": [{"id": <int>, "real": <bool>, "severity": "critical"|"warning"|"nit", "reason": "<why>", "domain": "logic"|"taste"}, ...], "summary": "<one-line ship/no-ship verdict>"}. One ruling per input id.`,
  ].join('\n\n')
}

function typecheckPrompt() {
  const cmd =
    A.toolchain === 'rust'
      ? '`cargo fmt --all -- --check`, then (only if that passes) `cargo clippy --all-targets --all-features -- -D warnings`'
      : '`uv run ruff check`, then (only if that passes) `uv run basedpyright`'
  return [
    `A prior fix round edited the working tree. Phase 0 preflight guaranteed a CLEAN lint/type baseline before this review began, so ANY failure you find now was introduced by a fix and is in scope.`,
    `From ${A.repoRoot}, run ${cmd}.`,
    `Turn each distinct error into a finding {file, line_start, line_end, severity:"critical", description (the compiler/type message verbatim), recommendation}. Return {"findings":[...], "degraded":false, "reason":""} — findings:[] if everything passes.`,
  ].join('\n\n')
}

// One fixer owns a disjoint bucket of files (no cross-fixer coordination). The
// detailed surgical/self-gate rules live in the `fixer` subagent definition.
function fixBucketPrompt(bucket) {
  const files = bucket.map(([file, group]) => ({
    file,
    findings: group.map((f) => ({
      line_start: f.line_start,
      line_end: f.line_end,
      severity: f.severity,
      description: f.description,
      recommendation: f.recommendation,
    })),
  }))
  return [
    `Repo root: ${A.repoRoot}. Toolchain: ${A.toolchain}. You own exactly these files — they are disjoint from every other fixer this round, so no coordination is needed:`,
    JSON.stringify(files, null, 2),
    `Apply the fixes per your instructions (minimal, surgical, self-gate), then return a terse per-file summary.`,
  ].join('\n\n')
}

// ---- boss dispatch --------------------------------------------------------
// One boss over the findings → rulings. Codex-family → codex exec (read-only);
// Claude-family → native agent (omit model → inherit the session model).
async function runBoss(findings, model, roundNo) {
  const slot = model || 'inherit'
  if (CODEX_FAMILY.test(model || '')) {
    const wrapper = [
      `You are a thin wrapper — do not judge yourself. Read ${A.repoRoot}/.claude/skills/codex-cli/SKILL.md for Codex CLI mechanics, then run gpt-5.x as the review ARBITER, READ-ONLY, targeting ${A.repoRoot}.`,
      `Invoke Codex read-only: \`codex exec -s read-only\`${model === 'codex' ? ' (CLI default model)' : ` --model ${model}`}, targeting ${A.repoRoot}, passing the judging prompt below; parse the JSONL final message.`,
      `JUDGING PROMPT >>>\n${bossPrompt(findings)}\n<<<`,
      `Return ONLY the arbiter's JSON verbatim: {"rulings":[...], "summary":"..."}.`,
    ].join('\n\n')
    return agent(wrapper, { label: `gpt-5.5:boss:${slot}#${roundNo}`, phase: 'Judge', schema: BOSS_OUT, model: 'sonnet', effort: 'low' }).catch(() => null)
  }
  const opts = { label: `boss:${slot}#${roundNo}`, phase: 'Judge', schema: BOSS_OUT }
  if (model) opts.model = model // else inherit the session model
  return agent(bossPrompt(findings), opts).catch(() => null)
}

// Run the boss(es) and merge rulings onto the deduped findings.
// FAIL CLOSED: a crashed boss (or a missing per-finding ruling) must never read
// as "no real findings" — an empty ruling map would default the whole panel to
// noise and report a clean convergence. Unruled findings stay real=true, marked
// UNRULED in bossReason, and the failure is surfaced via status[]/degraded.
const UNRULED = { real: true, reason: 'UNRULED: boss unavailable or returned no ruling — surfaced fail-closed, NOT boss-confirmed' }
async function judge(findings, roundNo) {
  if (!findings.length) return []
  const bosses = A.boss && A.boss.length ? A.boss : [''] // '' = inherit session model
  const results = await parallel(bosses.map((m) => () => runBoss(findings, m, roundNo)))
  results.forEach((r, i) => {
    if (!r || !Array.isArray(r.rulings)) {
      bossFailed = true
      status[`boss:${bosses[i] || 'inherit'}`] = 'degraded: boss agent failed — this round\'s findings are unruled, not boss-confirmed'
    }
  })
  const byBoss = results.map((r) => {
    const map = {}
    for (const x of (r && r.rulings) || []) map[x.id] = x
    return map
  })
  return findings.map((f) => {
    if (bosses.length >= 2) {
      // Domain-authoritative: slot 0 rules logic findings, slot 1 rules taste.
      const rulings = byBoss.map((m) => m[f.id]).filter(Boolean)
      const domain = rulings.find((x) => x.domain)?.domain || 'logic'
      const auth = byBoss[domain === 'taste' ? 1 : 0]?.[f.id] || rulings[0] || UNRULED
      return { ...f, real: !!auth.real, severity: auth.severity || f.severity, bossReason: auth.reason, domain }
    }
    const r = byBoss[0]?.[f.id] || UNRULED
    return { ...f, real: !!r.real, severity: r.severity || f.severity, bossReason: r.reason, domain: r.domain }
  })
}

// ---- run ------------------------------------------------------------------
// Reviewer set decided deterministically from the injected tool map — no agent,
// no phase box; logged as a narrator line so it's visible.
const REVIEWERS = REGISTRY.filter((r) => r.always || A.tools?.[r.need])
const bossDesc = A.boss && A.boss.length ? A.boss.join(' + ') : 'inherit'
log(
  `Reviewers (from injected tool map): ${REVIEWERS.map((r) => r.key).join(', ')} · boss: ${bossDesc}` +
    (A.fix ? ` · fix (≤${MAX_ROUNDS} rounds)` : ' · report-only'),
)

const status = {} // key -> 'ok' | 'degraded: <reason>' (reviewers + boss + typecheck)
const seen = new Set()
const confirmed = []
let round = 0
let fixedLastRound = false
let bossFailed = false

// A worker produces RAW findings (the boss judges them; workers never self-filter).
async function runOne(r, roundNo) {
  const out = await agent(reviewPrompt(r), {
    label: `${r.label}#${roundNo}`,
    phase: 'Review',
    schema: REVIEW_OUT,
    model: r.model,
    effort: r.effort,
  }).catch(() => null)
  status[r.key] = out ? (out.degraded ? `degraded: ${out.reason || 'unknown'}` : 'ok') : 'degraded: agent error'
  const findings = (out && !out.degraded && out.findings) || []
  return findings.map((f) => ({ ...f, source: r.key }))
}

async function runReviewers(roundNo) {
  // Range mode: the external reviewers each do a `git checkout <b>` dance that
  // races OTHER checkouts — so they are serial AMONG THEMSELVES, but they never
  // conflict with the Claude reviewers (no checkout). Chain the externals in one
  // thunk and run it CONCURRENTLY with the parallel Claude reviewers; barriering
  // externals behind the Claude pair (the old shape) wasted their whole runtime
  // and made them invisible until the Claude reviewers finished.
  // …and when the range's upper bound IS the current HEAD (skill passes
  // rangeBIsHead), the checkout is a pure no-op — no dance, no race, so the
  // externals run fully parallel like every other mode.
  if (A.mode === 'range' && !A.rangeBIsHead) {
    const par = REVIEWERS.filter((r) => !r.serialInRange)
    const ser = REVIEWERS.filter((r) => r.serialInRange)
    const results = await parallel([
      ...par.map((r) => () => runOne(r, roundNo)),
      async () => {
        const out = []
        for (const r of ser) out.push(await runOne(r, roundNo))
        return out.flat()
      },
    ])
    return results.filter(Boolean).flat()
  }
  return (await parallel(REVIEWERS.map((r) => () => runOne(r, roundNo)))).filter(Boolean).flat()
}

while (round < MAX_ROUNDS) {
  round++

  // Workers → dedup → boss rules.
  const raw = await runReviewers(round)
  const deduped = dedupeByOverlap(raw)
  deduped.forEach((f, i) => (f.id = i))
  const ruled = (await judge(deduped, round)).filter((f) => f.real)

  // Type gate: only after a fix landed. Objective (compiler errors) — not
  // boss-judged, always real. Phase 0 guaranteed a clean baseline, so anything
  // here is fix-induced.
  let typeFindings = []
  if (A.fix && fixedLastRound && A.toolchain !== 'none') {
    const tc = await agent(typecheckPrompt(), { label: `typecheck#${round}`, phase: 'Judge', schema: REVIEW_OUT, model: 'sonnet', effort: 'low' }).catch(() => null)
    // FAIL CLOSED: a crashed typecheck is absence of data, not "no type errors".
    if (!tc) status[`typecheck#${round}`] = 'degraded: typecheck agent failed — fix-induced type errors unknown, not verified clean'
    typeFindings = ((tc && tc.findings) || []).map((f) => ({ ...f, source: 'typecheck', sources: ['typecheck'], real: true }))
  }

  // seen/fresh dedup only decides what's NEW for the final report — it must NOT
  // decide whether to keep fixing, or a fixer that fails on a finding makes that
  // same finding invisible next round (re-detected → filtered by `seen` → looks
  // "converged" with the bug still there). Round-to-round fix/converge decisions
  // are driven by roundActionable (this round's ruled findings) instead.
  const fresh = [...ruled, ...typeFindings].filter((f) => !seen.has(dedupKey(f)))
  fresh.forEach((f) => seen.add(dedupKey(f)))
  confirmed.push(...fresh)

  const roundActionable = [...ruled, ...typeFindings].filter((f) => f.severity !== 'nit')
  const freshActionable = fresh.filter((f) => f.severity !== 'nit').length
  const refixed = roundActionable.length - freshActionable // actionable but already `seen` — a prior fix didn't take
  log(
    `Round ${round}: ${fresh.length} new confirmed (${roundActionable.length} actionable this round` +
      (refixed > 0 ? `, ${refixed} refixed/re-detected` : '') +
      `, ${confirmed.length} total)`,
  )

  // Boss failure → stop fail-closed: this round's findings were surfaced
  // UNRULED, and there are no verified rulings to fix against or converge on.
  if (bossFailed) {
    log(`Round ${round}: boss failed — stopping fail-closed with ${roundActionable.length} unruled actionable finding(s) surfaced`)
    break
  }

  // Report-only, or nothing actionable this round (including re-detections) → converged.
  if (!A.fix || roundActionable.length === 0) break

  // Fix: deterministically-sized fixer pool, each owning a disjoint file bucket.
  const groups = groupByFile(roundActionable)
  const N = Math.max(1, Math.min(FIXER_CAP, Math.ceil(groups.length / 5)))
  const buckets = bucketize(groups, N)
  fixedLastRound = false
  await parallel(
    buckets.map((bucket, i) => () =>
      agent(fixBucketPrompt(bucket), { label: `fix:bucket${i + 1}/${buckets.length}#${round}`, phase: 'Fix', agentType: 'fixer' }),
    ),
  )
  fixedLastRound = true
  // loop back → next round reviews the fixed tree and type-gates the fixes
}

phase('Conclude')
// status covers reviewers AND the non-reviewer gates (boss, typecheck) — any
// entry that isn't 'ok' is absence of data, never a clean pass.
const degraded = Object.entries(status)
  .filter(([, v]) => v !== 'ok')
  .map(([key, reason]) => ({ key, reason }))
return {
  mode: A.mode,
  rounds: round,
  fixed: !!A.fix,
  boss: A.boss && A.boss.length ? A.boss : ['inherit'],
  attempted: REVIEWERS.map((r) => r.key),
  degraded,
  confirmed: confirmed.sort((a, b) => SEV_RANK[b.severity] - SEV_RANK[a.severity]),
}
