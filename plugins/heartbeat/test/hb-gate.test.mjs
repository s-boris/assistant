// Fixture tests for the heartbeat gate (ops/hb-gate, installed as ~/bin/hb-gate): every wake/skip rule, its order,
// the agent-watch fallback, fail-open and the log. Runs the real script against temp files; no claude, no network.
// node --test ~/plugins/heartbeat/test/hb-gate.test.mjs   (HB_GATE_BIN=~/bin/hb-gate tests the installed copy)
import { test } from 'node:test'
import assert from 'node:assert/strict'
import { spawnSync } from 'node:child_process'
import { mkdtempSync, mkdirSync, writeFileSync, readFileSync, rmSync, utimesSync, existsSync } from 'node:fs'
import { tmpdir } from 'node:os'
import { join, dirname } from 'node:path'
import { fileURLToPath } from 'node:url'

const GATE = process.env.HB_GATE_BIN || join(dirname(fileURLToPath(import.meta.url)), '..', 'ops', 'hb-gate')
const NOW = '2026-10-14 21:00'                       // Zurich (CEST): 19:00 UTC
const NOW_S = Date.UTC(2026, 9, 14, 19, 0) / 1000
const MIN = 60

const TODO = ({ due = [], ideas = ['**Idea A** — buildable now', '**Idea B (owner: later)** — parked'],
  night = ['**Tests for scripts** — one per night'] } = {}) => [
  '# TODO', 'Goal: prose with - dashes and 09:00 that is not an item.', '', '## Due', ...due.map(d => `- ${d}`), '',
  '## In progress', '- **Something** — building', '  - sub-bullet 08:00', '', '## Waiting on you', '- **Disk day** — 10:00',
  '', '## Ideas', ...ideas.map(i => `- ${i}`), '', '## Night backlog', '(prose 07:00)', ...night.map(n => `- ${n}`), ''].join('\n')
const GOALS = (steps = ['- [ ] Ask what exactly bugs him', '- [ ] Storage plan for the new disk', '- [x] Done step']) =>
  ['# Goals', '## 1. Admin', ...steps, ''].join('\n')

// args: gate argv; marker: age (s) of /tmp/heartbeat or null (missing); notes: inbox file names;
// aw: agent-watch status stdout (fallback mode; null = the fake fails); todoMtime: epoch s for TODO.md;
// rulesOff: the wake-rules switch file (default present = the gate without wake rules); wake: the fake
// `wake check --json` answer (an object, or a string printed as is), wakeExit its exit code.
function gate(args = [], { todo = TODO(), goals = GOALS(), marker = 20 * MIN, notes = [], aw = AW([]), awExit = 0,
  off = false, todoMtime = NOW_S - 3600, json = true, env = {}, rulesOff = true, wake = null, wakeExit = 0 } = {}) {
  const d = mkdtempSync(join(tmpdir(), 'hbgate-'))
  try {
    const inbox = join(d, 'inbox')
    mkdirSync(inbox)
    for (const n of notes) n.endsWith('/') ? mkdirSync(join(inbox, n)) : writeFileSync(join(inbox, n), 'x')
    writeFileSync(join(d, 'TODO.md'), todo)
    utimesSync(join(d, 'TODO.md'), todoMtime, todoMtime)
    if (goals !== null) writeFileSync(join(d, 'GOALS.md'), goals)
    if (marker !== null) { writeFileSync(join(d, 'marker'), ''); utimesSync(join(d, 'marker'), NOW_S - marker, NOW_S - marker) }
    writeFileSync(join(d, 'aw'), `#!/bin/sh\ncat <<'EOF'\n${aw}\nEOF\nexit ${awExit}\n`, { mode: 0o755 })
    if (off) writeFileSync(join(d, 'off'), '')
    if (rulesOff) writeFileSync(join(d, 'rules-off'), '')
    const answer = typeof wake === 'string' ? wake : JSON.stringify(wake ?? {})
    writeFileSync(join(d, 'wake'), `#!/bin/sh\necho "$*" > "${d}/wake.args"\ncat <<'EOF'\n${answer}\nEOF\nexit ${wakeExit}\n`,
      { mode: 0o755 })
    const r = spawnSync(GATE, [...(json ? ['--json'] : []), ...args], {
      encoding: 'utf8', timeout: 20_000,
      env: { PATH: process.env.PATH, HOME: d, HB_GATE_NOW: NOW, HB_GATE_INBOX: inbox, HB_GATE_TODO: join(d, 'TODO.md'),
        HB_GATE_GOALS: join(d, 'GOALS.md'), HB_GATE_MARKER: join(d, 'marker'), HB_GATE_LOG: join(d, 'log'),
        HB_GATE_OFF: join(d, 'off'), HB_GATE_AGENT_WATCH: join(d, 'aw'), HB_GATE_WAKE: join(d, 'wake'),
        HB_GATE_RULES_OFF: join(d, 'rules-off'), ...env },
    })
    assert.equal(r.status, 0, `exit 0 always (stderr: ${r.stderr})`)
    const log = existsSync(join(d, 'log')) ? readFileSync(join(d, 'log'), 'utf8') : ''
    const out = json ? JSON.parse(r.stdout.trim().split('\n').pop()) : r.stdout
    const wakeArgs = existsSync(join(d, 'wake.args')) ? readFileSync(join(d, 'wake.args'), 'utf8').trim() : null
    return { ...(json ? out : { text: out }), log, wakeArgs }
  } finally { rmSync(d, { recursive: true, force: true }) }
}
// agent-watch status output: rows [id, state, silentMin].
function AW(rows) {
  if (!rows.length) return 'agent-watch 2026-10-14 21:00 Zurich: subagents written in the last 6 h\n  (none)'
  const lines = ['agent-watch 2026-10-14 21:00 Zurich: subagents written in the last 6 h (stalled = no growth >15 min)',
    '  ID                 STATE    RUN  SILENT  LAST         OWNER TAG                        DESCRIPTION']
  for (const [id, state, silent] of rows)
    lines.push(`  ${id.padEnd(17)}  ${state.padEnd(7)}  9m   ${(silent + 'm').padEnd(6)}  tool_use     Owner task (⏳20:45)               Fix two things`)
  return lines.join('\n')
}
const AGENTS = (running, finished = 0) => ['--running', String(running), '--finished', String(finished)]
const lastBeat = ago => ['--last-beat', String((NOW_S - ago) * 1000)]   // ms, as the mod passes it

test('(a) inbox notes wake first; hidden files and folders are not notes; --notes counts too', () => {
  const r = gate([...AGENTS(2), ...lastBeat(5 * MIN)], { notes: ['vault-events-1.md'] })
  assert.equal(r.decision, 'wake')
  assert.equal(r.reason, 'inbox: 1 note(s)')
  assert.equal(gate([...AGENTS(1)], { notes: ['.hidden', 'sub/'] }).decision, 'skip')
  assert.equal(gate(['--notes', '2', ...AGENTS(1)]).reason, 'inbox: 2 note(s)')
})

test('(b) a Due item whose time has come wakes, even with an agent running; future ones do not', () => {
  const due = (items, opts = {}) => gate([...AGENTS(1), ...lastBeat(5 * MIN)], { todo: TODO({ due: items }), ...opts })
  assert.equal(due(['**09:00 report: LLM bench** — ready']).reason, 'due: 09:00 report: LLM bench')
  assert.equal(due(['**Pitch** — send tonight 22:30']).decision, 'skip', 'later today: not yet')
  assert.equal(due(['**Today:** 09:30 verify · 23:00 pitch']).decision, 'wake', 'the earliest time counts')
  assert.equal(due(['**Call the garage** — no time given']).decision, 'wake', 'undated = due now')
  assert.equal(due(['**Disk day** — Fri 16.10 evening']).decision, 'skip', 'a later date')
  assert.equal(due(['**Disk day** — 13.10. at 18:00']).decision, 'wake', 'a past date')
  assert.equal(due(['**Kündigung** — 2026-10-14 20:59']).decision, 'wake', 'ISO date and time, just passed')
  assert.equal(due(['**Kündigung** — 2026-10-14 21:01']).decision, 'skip')
  assert.equal(due(['**Brief** — tomorrow 09:00']).decision, 'skip', 'tomorrow, anchored on TODO.md written today')
  assert.equal(due(['**Brief** — tomorrow 09:00'], { todoMtime: NOW_S - 2 * 86400 }).decision, 'wake',
    'written two days ago: that tomorrow has passed')
  assert.equal(due(['**Disk day** — v4.1 on Unraid 7.3.3 at 93.5 % — 23:00']).decision, 'skip', 'versions are not dates')
  const two = due(['**A** — 08:00', '**B** — 09:00'])
  assert.equal(two.reason, 'due: A (+1)')
  assert.equal(due([]).decision, 'skip', 'times outside ## Due (Waiting on you 10:00, prose, sub-bullets) never count')
})

test('(d) a finished agent, (e) the first beat, (f) the 3 h safety valve wake despite a running agent', () => {
  assert.equal(gate([...AGENTS(1, 1), ...lastBeat(5 * MIN)]).reason, '1 agent(s) finished since the last beat')
  assert.equal(gate([...AGENTS(1), ...lastBeat(5 * MIN), '--first']).reason, 'first beat after a restart')
  assert.match(gate([...AGENTS(1), ...lastBeat(3 * 3600)], { marker: 4 * 3600 }).reason, /^safety valve: last beat 3\.0 h ago/)
  assert.equal(gate([...AGENTS(1), ...lastBeat(4 * 3600)], { marker: 10 * MIN }).decision, 'skip',
    'a newer /tmp/heartbeat (cron, restart waker) counts as the last beat')
  assert.equal(gate([...AGENTS(1)], { marker: null }).reason, 'safety valve: no beat on record')
})

test('skip while a background agent runs (the common wasted beat); (c) wake when none runs and work is startable', () => {
  const r = gate([...AGENTS(2), '--ids', 'a1426fd9c66fb137d,a2db626ce8887870e', ...lastBeat(30 * MIN)])
  assert.equal(r.decision, 'skip')
  assert.equal(r.reason, 'agent running: 2 (a1426fd9, a2db626c)')
  assert.match(r.log, /^2026-10-14 21:00:\d\d skip agent running: 2/m, 'logged')
  const c = gate([...AGENTS(0), ...lastBeat(30 * MIN)])
  assert.equal(c.decision, 'wake')
  assert.equal(c.reason, 'no agent running; startable: goals 1, ideas 1, night 1', 'blocked goal and idea not counted')
  const none = gate([...AGENTS(0), ...lastBeat(30 * MIN)], {
    todo: TODO({ ideas: ['**X (owner: later)** — y', '**Y** — not picked yet'], night: [] }),
    goals: GOALS(['- [ ] Ask him about money', '- [ ] Signals: offer help once he mentions it', '- [x] Done']) })
  assert.equal(none.decision, 'skip')
  assert.equal(none.reason, 'no agent running, nothing startable')
  assert.equal(gate([...AGENTS(0), ...lastBeat(30 * MIN)], { goals: null, todo: TODO({ ideas: [] }) }).reason,
    'no agent running; startable: goals 0, ideas 0, night 1', 'GOALS.md missing is fine')
})

test('fallback without --running: agent-watch status (running, stalled <= 45 min, finishes after the last beat)', () => {
  const fb = (rows, ago = 30 * MIN, opts = {}) => gate(lastBeat(ago), { aw: AW(rows), marker: ago, ...opts })
  assert.equal(fb([['a1426fd9c66fb137d', 'running', 1]]).reason, 'agent running: 1 (a1426fd9)')
  assert.equal(fb([['a1', 'stalled', 40]]).decision, 'skip', 'stalled but recent: maybe a long command')
  assert.match(fb([['a1', 'stalled', 50]]).reason, /^no agent running; startable/, 'stalled > 45 min: dead or stuck')
  assert.equal(fb([['a1', 'done', 10], ['a2', 'running', 2]]).reason, '1 agent(s) finished since the last beat')
  assert.equal(fb([['a1', 'done', 40], ['a2', 'running', 2]]).decision, 'skip', 'finished before the last beat')
  assert.match(fb([]).reason, /^no agent running; startable/, '(none)')
  assert.match(fb([], 30 * MIN, { awExit: 1 }).reason, /^gate error: RuntimeError: agent-watch status exit 1/, 'fails open')
  assert.match(gate(lastBeat(30 * MIN), { aw: 'garbage' }).reason, /^gate error/)
})

test('fail open and off switch: bad args, unreadable TODO.md or the off file all wake; logged unless --dry-run', () => {
  const bad = gate(['--bogus'])
  assert.equal(bad.decision, 'wake')
  assert.match(bad.reason, /^gate error: ValueError: unknown argument --bogus/)
  assert.match(bad.log, / wake gate error/)
  assert.match(gate([...AGENTS(1)], { todo: '\udcff' }).decision, /wake|skip/, 'odd TODO text: still an answer')
  assert.equal(gate([...AGENTS(1), '--notes', 'x']).decision, 'wake')
  assert.equal(gate([...AGENTS(1), ...lastBeat(5 * MIN)], { off: true }).reason, 'gate off (~/.config/hb-gate/off)')
  assert.equal(gate([...AGENTS(1), ...lastBeat(5 * MIN), '--dry-run']).log, '', '--dry-run writes no log')
  const plain = gate([...AGENTS(1), ...lastBeat(5 * MIN)], { json: false })
  assert.equal(plain.text, 'skip: agent running: 1\n')
})

// The brain's wake rules (`wake check --json`: notes by delivery class + the hold); the switch file = the gate as before.
const VIEW = ({ now = 0, beat = 0, digest = 0, mute = 0, hold = null } = {}) => ({
  ok: true, ts: 0, db: 'ok', registry_error: null, rules: 1, hold,
  counts: { now, beat, digest, mute },
  kinds: { now: now ? { 'note.voice': now } : {}, beat: beat ? { 'cron.fail': beat } : {}, digest: {}, mute: {} },
  classes: {},
})

test('wake rules off (switch present): wake is never asked, the now lane skips unlogged', () => {
  const r = gate(['--notes', '1', ...AGENTS(1)], { wake: VIEW({ mute: 1 }) })
  assert.equal(r.reason, 'inbox: 1 note(s)')
  assert.equal(r.wakeArgs, null, 'wake not run')
  const n = gate(['--now-only', ...AGENTS(1)], { wake: VIEW({ now: 1 }) })
  assert.deepEqual([n.decision, n.reason, n.log], ['skip', 'now lane: wake rules off', ''])
})

test('wake rules on: now first, digest/mute ignored, beat as before, a hold skips only the idle work beat', () => {
  const on = (args, view, opts = {}) => gate(args, { rulesOff: false, wake: view, ...opts })
  const now = on([...AGENTS(1)], VIEW({ now: 1, mute: 2 }))
  assert.deepEqual([now.decision, now.reason], ['wake', 'now: 1 note(s) (note.voice)'])
  assert.match(now.wakeArgs, /^check --json --inbox \/.+\/inbox$/)
  assert.equal(on(['--notes', '3', ...AGENTS(1)], VIEW({ digest: 2, mute: 1 })).reason, 'agent running: 1')
  assert.equal(on([...AGENTS(1)], VIEW({ beat: 1, digest: 1 })).reason, 'inbox: 1 note(s) (+1 digest/mute)')
  const hold = { id: 3, until: NOW_S + 3600, why: 'night', created: 0 }
  const idle = TODO({ ideas: ['**Idea A** — buildable now'], night: [] })
  assert.equal(on([...AGENTS(0), ...lastBeat(5 * MIN)], VIEW({ hold }), { todo: idle }).reason, 'held until 14.10 22:00: night')
  assert.match(on([...AGENTS(0), ...lastBeat(5 * MIN)], VIEW({ hold: { ...hold, until: NOW_S - 1 } }), { todo: idle }).reason,
    /^no agent running; startable/, 'an expired hold holds nothing')
  assert.equal(on([...AGENTS(0, 1), ...lastBeat(5 * MIN)], VIEW({ hold })).decision, 'wake', 'a finished agent still wakes')
  assert.match(on([...AGENTS(0), ...lastBeat(4 * 3600)], VIEW({ hold }), { marker: 4 * 3600 }).reason, /^safety valve/,
    'the 3 h valve stays')
  const lane = on(['--now-only', ...AGENTS(1)], VIEW({ now: 1 }))
  assert.deepEqual([lane.decision, lane.reason], ['wake', 'now: 1 note(s) (note.voice) [now lane]'])
  assert.match(lane.log, / wake now: 1 note\(s\)/)
  const miss = on(['--now-only', ...AGENTS(1)], VIEW({ beat: 2 }))
  assert.deepEqual([miss.decision, miss.reason, miss.log], ['skip', 'now lane: no now-class note', ''])
})

test('wake rules unreadable: the gate as before, marked; the now lane skips', () => {
  const on = (args, opts) => gate(args, { rulesOff: false, ...opts })
  assert.equal(on(['--notes', '1', ...AGENTS(1)], { wake: VIEW(), wakeExit: 3 }).reason,
    'inbox: 1 note(s) [wake rules unreadable: wake check exit 3]')
  assert.equal(on([...AGENTS(1)], { wake: 'garbage' }).reason,
    'agent running: 1 [wake rules unreadable: wake check: bad output]')
  assert.equal(on(['--now-only', ...AGENTS(1)], { wake: VIEW({ now: 1 }), wakeExit: 1 }).decision, 'skip')
})
