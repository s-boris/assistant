// Offline end-to-end test of the mod's hooks module (register.js) with a fake `$`: a virtual clock, an in-memory
// fs and a recording prompt.submit. Runs the real tick loop, state file and turn bookkeeping, no claude process.
// node --test ~/plugins/heartbeat/test/register.test.mjs
import { test } from 'node:test'
import assert from 'node:assert/strict'
import { readFileSync } from 'node:fs'
import { GAP_MIN, INBOX_MIN, COMPACT_DEFAULTS, COMPACT_TIMEOUT, compactConfig, compactBlocked, compactDue, compactTokens,
  compactOutcome, isOwnerInput, ownerInput, turnEnded, newestFile, nowLaneDue, projectDir } from '../hooks/register.js'

const MIN = 6_000                                   // test mode: a minute is 6 s
const T0 = Date.UTC(2026, 9, 13, 10, 0, 0)          // 12:00 in Zurich: daytime cap (60 min)
const D = '/hbtest'
// The host's frame around the mod's beat at turn.start (Claude Code 2.1.296, verbatim from the transcript of 2026-10-13).
const FRAME = t => `The heartbeat plugin sent a message:\n${t}\n\nThis is how Claude Code surfaces a prompt a plugin ` +
  "submits between turns \u2014 it starts this turn in the user's place. Address the message above."

let loads = 0
// opts (gate tests): env adds env vars (HEARTBEAT_GATE), process/agent become $.process/$.agent; 0.5.0: session becomes
// $.session, sleep answers $.clock.sleep (default: only an abort ends it).
async function harness(t0 = T0, opts = {}) {      // a fresh module instance each: st, lastBeat, lastError start clean
  const { register } = await import(`../hooks/register.js?harness=${++loads}`)
  const hooks = {}, files = new Map(), submitted = []
  let now = t0, every = null
  const $ = {
    clock: { now: async () => now, every: (ms, fn) => { every = { ms, fn }; return { cancel() { every = null } } },
      sleep: (ms, o) => (opts.sleep ? opts.sleep(ms, o) : new Promise((resolve, reject) => {
        if (o && o.signal) o.signal.addEventListener('abort', () => reject(new Error('aborted')))
      })) },
    session: opts.session,
    env: { get: async k => ({ HEARTBEAT_MOD_TEST_DIR: D, HEARTBEAT_MINUTES: '10', ...opts.env })[k] },
    process: opts.process,
    agent: opts.agent,
    fs: {
      stat: async p => { if (!files.has(p)) throw new Error('ENOENT: ' + p); return { mtimeMs: files.get(p).mtimeMs } },
      write: async (p, data) => { files.set(p, { data, mtimeMs: now }) },
      read: async p => { if (!files.has(p)) throw new Error('ENOENT: ' + p); return files.get(p).data },
      list: async dir => {
        const out = [...files].filter(([p]) => p.startsWith(dir + '/') && !p.slice(dir.length + 1).includes('/'))
          .map(([p, f]) => ({ name: p.slice(dir.length + 1), kind: 'file', mtimeMs: f.mtimeMs }))
        if (!out.length && dir !== `${D}/inbox`) throw new Error('ENOENT: ' + dir)
        return out
      },
    },
    // As in the host: the mod's own prompt.submit hook sees its beat bare (origin plugin, no turnId); the test's
    // turn() then hands turn.start the framed text (FRAME), as the host does.
    prompt: { submit: async ({ text }) => {
      submitted.push({ at: now, text })
      return hooks['prompt.submit']($, { text, origin: { kind: 'plugin', name: 'heartbeat' }, wait: false }, async x => x)
    } },
    ui: { log() {} },
  }
  register((name, fn) => { hooks[name] = fn })
  const fire = (name, e) => hooks[name]($, e, async x => x)
  const call = (name, e, next) => hooks[name]($, e, next)   // a hook with the host's own next (e.g. its result)
  const flush = () => new Promise(r => setImmediate(r))
  return {
    $, files, submitted, fire, call,
    get now() { return now },
    state: () => JSON.parse(files.get(`${D}/state.json`).data),
    async ticked() { now += every.ms; await every.fn(); await flush(); return JSON.parse(files.get(`${D}/state.json`).data) },
    async start() { await fire('session.start', { isInteractive: true, cwd: '/home/node/jobs/hbtest' }) },
    async advance(ms) {                              // run the 15 s (virtual) ticks up to now + ms
      const end = now + ms
      while (now + every.ms <= end) { now += every.ms; await every.fn(); await flush() }
      now = end
    },
    async turn(text, { tools = [], reason = 'answer', mins = 0.5 } = {}) {   // one main turn
      await fire('turn.start', { text, turnId: 't' })
      for (const tool of tools) await fire('tool.call', { tool })
      now += mins * MIN
      await fire('turn.complete', { reason })
    },
    note(name) { files.set(`${D}/inbox/${name}`, { data: 'x', mtimeMs: now }) },
  }
}

test('end to end: exact thresholds, hold, inbox within ~2 min, owner message resets', async () => {
  const h = await harness()
  await h.start()
  const until = async (max) => {                   // advance to the next beat, return minutes waited
    const t = h.now
    for (let i = 0; i < max * 4; i++) { await h.advance(MIN / 4); if (h.submitted.length) break }
    const b = h.submitted.shift()
    assert.ok(b, `a beat within ${max} min`)
    return { b, waited: (b.at - t) / MIN }
  }
  const ladder = []
  for (let i = 0; i < 6; i++) { const { b, waited } = await until(130); ladder.push(Math.ceil(waited)); await h.turn(FRAME(b.text), { tools: ['Bash'] }) }
  // 10 after start; then 10, 20, 40, 60, 60 after each empty turn (the turn's own 0.5 min aside)
  assert.deepEqual(ladder, [10, 10, 20, 40, 60, 60])

  // A beat nobody answers (queued behind a dialog): no second beat, not even for an inbox note, until the cap.
  const { b: held } = await until(70)
  h.note('a.md')
  await h.advance(60 * MIN)
  assert.equal(h.submitted.length, 0, 'held while unanswered')
  assert.equal(h.state().unanswered, true)
  await h.turn(FRAME(held.text), { reason: 'error' })      // consumed (here by an API error): the hold ends
  assert.equal((await h.ticked()).unanswered, false, 'state file follows within a tick')

  // An inbox note at 60 min backoff still wakes after ~2 min idle (>= GAP_MIN after the last beat).
  await h.advance(GAP_MIN * MIN)
  h.note('b.md')
  const { b: inbox, waited } = await until(5)
  assert.ok(waited <= 2.5, `inbox wake after ${waited} min`)
  assert.match(inbox.text, /Idle 2\+ min, \d new note\(s\)/)
  await h.turn(FRAME(inbox.text), { tools: ['Read', 'Bash'] })   // notes handled: not an empty beat
  assert.equal((await h.ticked()).streak, 0, 'a beat with notes resets the backoff')

  // Back to the base; build the streak again, then an owner message resets it.
  for (let i = 0; i < 4; i++) { const { b } = await until(130); await h.turn(FRAME(b.text)) }
  assert.ok((await h.ticked()).idleMin >= 40)
  await h.fire('prompt.submit', { text: '<channel source="plugin:telegram-plus:telegram">hi</channel>', origin: { kind: 'user' } })
  await h.turn('<channel source="plugin:telegram-plus:telegram">hi</channel>', { tools: ['mcp__plugin_telegram-plus_telegram__reply'] })
  const st = await h.ticked()
  assert.equal(st.streak, 0)
  assert.equal(st.idleMin, 10)
  const { waited: afterOwner } = await until(15)
  assert.equal(Math.ceil(afterOwner), 10, 'base interval right after an owner message')

  // The state file stays fresh for the cron fallback through the backoff (written about once a minute).
  assert.ok(h.now - h.state().lastTick <= MIN, 'lastTick fresh')
})

async function nextBeat(h, max) {                   // advance to the next beat: [beat, minutes waited]
  const t = h.now
  for (let i = 0; i < max * 4 && !h.submitted.length; i++) await h.advance(MIN / 4)
  const b = h.submitted.shift()
  assert.ok(b, `a beat within ${max} min`)
  return [b, Math.ceil((b.at - t) / MIN)]
}

test('regression 2026-10-13 night: framed beats, one quick Bash call each, back off 10, 10, 20, 40, 80, 120', async () => {
  const h = await harness(Date.UTC(2026, 9, 13, 2, 1, 48))   // the restart at 04:01:48 Zurich: night cap 120
  await h.start()
  const ladder = [], status = []
  for (let i = 0; i < 6; i++) {                     // 0.2.0: streak 0 and a beat every 10 min all night
    const [b, waited] = await nextBeat(h, 130)
    ladder.push(waited)
    assert.match(b.text, /^\[heartbeat\] Idle \d+\+ min, 0 new note\(s\)/)
    await h.turn(FRAME(b.text), { tools: ['Bash'] })
    const s = await h.ticked()
    status.push(`${s.streak}/${s.idleMin}`)
  }
  assert.deepEqual(status, ['1/10', '2/20', '3/40', '4/80', '5/120', '6/120'])
  assert.deepEqual(ladder, [10, 10, 20, 40, 80, 120])
  assert.equal(h.state().lastError, '', 'the real frame is recognized, no drift flagged')
})

test('frame drift: still counted through prompt.submit, flagged in lastError', async () => {
  const h = await harness()
  await h.start()
  const [b] = await nextBeat(h, 15)
  await h.turn(`<plugin_message source="heartbeat">\n${b.text}`, { tools: ['Bash'] })
  const s = await h.ticked()
  assert.equal(s.streak, 1)
  assert.match(s.lastError, /host frame changed/)
})

test('a subagent turn.start (CC >= 2.1.299, carries agentId) does not mark the main loop busy', async () => {
  const h = await harness()
  await h.start()
  await h.fire('turn.start', { text: 'subagent prompt', turnId: 's', agentId: 'a123' })
  await h.advance(11 * MIN)
  assert.equal(h.submitted.length, 1, 'the beat still fires while a subagent runs')
})

// --- gate (0.3.0) -----------------------------------------------------------------------------------------------
// A fake hb-gate: answers from `answer` (a function of the argv, or a thrown error), records every call.
function fakeGate(answer) {
  const calls = []
  return {
    calls,
    process: { run: async (argv, init) => {
      calls.push({ argv: [...argv], init })
      let a = typeof answer === 'function' ? answer(argv) : answer
      if (typeof a === 'function') a = a(argv)   // answer: () => current (a value or a function of the argv)
      if (a instanceof Error) throw a
      return { exitCode: 0, stdout: JSON.stringify(a) + '\n', stderr: '' }
    } },
  }
}
const GATE_ENV = { HEARTBEAT_GATE: '/fake/hb-gate' }

test('gate skip: nothing is submitted, the skip is counted, the backoff is untouched, nextDue moves on', async () => {
  let answer = { decision: 'skip', reason: 'agent running: 1 (a1)' }
  const g = fakeGate(() => answer)
  const agents = [{ id: 'a1', status: 'running', description: 'build', type: 'general-purpose' }]
  const h = await harness(T0, { env: GATE_ENV, process: g.process, agent: { list: async () => agents } })
  await h.start()
  await h.advance(31 * MIN)
  assert.equal(h.submitted.length, 0, 'a skipped beat never reaches prompt.submit')
  assert.equal(g.calls.length, 3, 'consulted once per idle threshold (10, 20, 30 min), not every tick')
  const [argv] = g.calls.map(c => c.argv)
  assert.equal(argv[0], '/fake/hb-gate')
  assert.deepEqual(argv.slice(1, 4), ['--json', '--notes', '0'])
  assert.ok(argv.includes('--first'), 'first beat of the session is flagged')
  assert.deepEqual(argv.slice(argv.indexOf('--running'), argv.indexOf('--running') + 6),
    ['--running', '1', '--finished', '0', '--ids', 'a1'])
  assert.ok(g.calls[0].init.timeoutMs > 0, 'bounded')
  const s = await h.ticked()
  assert.equal(s.skips, 3)
  assert.equal(s.beats, 0)
  assert.equal(s.streak, 0, 'skips are not empty beats')
  assert.equal(s.unanswered, false)
  assert.match(s.lastGate, /^skip: agent running/)
  assert.ok(s.nextDue >= s.lastSkip + 10 * MIN, 'nextDue follows the skip: the cron fallback keeps holding')
  assert.equal(s.lastError, '')

  // The agent finishes: the gate now says wake, and the beat goes out.
  agents[0].status = 'completed'
  answer = argv => argv.includes('--finished') && argv[argv.indexOf('--finished') + 1] === '1'
    ? { decision: 'wake', reason: '1 agent(s) finished since the last beat' } : { decision: 'skip', reason: 'x' }
  const [b] = await nextBeat(h, 15)
  assert.match(b.text, /^\[heartbeat\] Idle 10\+ min, 0 new note\(s\)/)
  assert.match(h.state().lastGate, /^wake: 1 agent\(s\) finished/)
  await h.turn(FRAME(b.text), { tools: ['Bash'] })
  const before = g.calls.length
  answer = { decision: 'skip', reason: 'no agent running, nothing startable' }
  await h.advance(11 * MIN)
  assert.equal(h.submitted.length, 0)
  assert.ok(!g.calls.at(-1).argv.includes('--first'), 'only the first beat is flagged')
  assert.ok(g.calls.length > before)
  assert.equal(h.state().streak, 1, 'the empty beat that ran still counts; the skip after it adds nothing')
})

test('gate: inbox notes still wake within ~2 min after a skip (the gate is asked and says wake)', async () => {
  const g = fakeGate(argv => argv[argv.indexOf('--notes') + 1] !== '0'
    ? { decision: 'wake', reason: 'inbox: 1 note(s)' } : { decision: 'skip', reason: 'agent running: 1' })
  const h = await harness(T0, { env: GATE_ENV, process: g.process, agent: { list: async () => [] } })
  await h.start()
  await h.advance(11 * MIN)
  assert.equal(h.submitted.length, 0)
  h.note('a.md')
  const [b, waited] = await nextBeat(h, 5)
  assert.ok(waited <= 3, `inbox wake after ${waited} min`)
  assert.match(b.text, /1 new note\(s\)/)
})

test('gate fails open: a crash, a timeout, garbage or a missing $.agent all mean wake', async () => {
  for (const [name, answer, agent] of [
    ['throws', new Error('spawn ENOENT'), { list: async () => [] }],
    ['garbage', 'skip', { list: async () => [] }],
    ['no $.agent', { decision: 'wake', reason: 'safety valve' }, undefined],
    ['agent.list throws', { decision: 'wake', reason: 'x' }, { list: async () => { throw new Error('no') } }],
  ]) {
    const g = fakeGate(answer)
    const h = await harness(T0, { env: GATE_ENV, process: g.process, agent })
    await h.start()
    await h.advance(11 * MIN)
    assert.equal(h.submitted.length, 1, `${name}: the beat goes out`)
    assert.equal(g.calls.length, 1, name)
    if (!agent || name === 'agent.list throws')
      assert.ok(!g.calls[0].argv.includes('--running'), `${name}: no agent info, the gate asks agent-watch`)
    if (name === 'throws') assert.match(h.state().lastError, /gate error/)
  }
  const h = await harness(T0, { process: { run: async () => { throw new Error('must not run') } } })
  await h.start()
  await h.advance(11 * MIN)
  assert.equal(h.submitted.length, 1, 'no HEARTBEAT_GATE in test mode: no gate')
  assert.equal(h.state().gate, false)
})

test('0.3.1: $.agent.list() is called as spelled (the loader rejects a noun read as a value); a non-list answer falls back', async () => {
  // The host's load rule (CC 2.1.296+, same as `claude plugin validate`): every $ noun is spelled $.noun.event(...)
  // at the call site. 0.3.0's `typeof $.agent?.list === 'function'` broke it, and the whole module never loaded.
  const code = readFileSync(new URL('../hooks/register.js', import.meta.url), 'utf8').replace(/\/\/.*$/gm, '')
  const bad = [...code.matchAll(/(?<![\w$])\$\.\w+(?!\w)(?!\.\w+\()/g)].map(m => m[0])
  assert.deepEqual(bad, [], 'every $.noun is followed by .event( at its call site')
  assert.doesNotMatch(code, /(?<![\w$])\$\??\.\w+\?\./, 'no optional chaining on $ nouns')
  // The fixed path: the gate gets the running agents from $.agent.list() itself, called as a method.
  let calls = 0
  const agent = { list: async function () { calls++; assert.equal(this, agent); return [{ id: 'a7', status: 'running' }] } }
  const g = fakeGate({ decision: 'skip', reason: 'agent running: 1 (a7)' })
  const h = await harness(T0, { env: GATE_ENV, process: g.process, agent })
  await h.start()
  await h.advance(11 * MIN)
  assert.ok(calls >= 1, 'agent.list was called')
  const argv = g.calls.at(-1).argv
  assert.deepEqual(argv.slice(argv.indexOf('--running'), argv.indexOf('--running') + 2), ['--running', '1'])
  assert.equal(argv[argv.indexOf('--ids') + 1], 'a7')
  assert.equal(h.submitted.length, 0, 'the gate said skip')
  // A host whose agent.list answers no list: no agent info, the gate asks agent-watch, the beat still goes out.
  const g2 = fakeGate({ decision: 'wake', reason: 'x' })
  const h2 = await harness(T0, { env: GATE_ENV, process: g2.process, agent: { list: async () => undefined } })
  await h2.start()
  await h2.advance(11 * MIN)
  assert.ok(!g2.calls[0].argv.includes('--running'), 'non-list answer: the gate asks agent-watch')
  assert.equal(h2.submitted.length, 1)
})

test('gate: a finish the main session already saw (its task notification turn) is not news', async () => {
  const agents = [{ id: 'a1', status: 'running' }, { id: 'a2', status: 'running' }]
  const g = fakeGate({ decision: 'skip', reason: 'agent running' })
  const h = await harness(T0, { env: GATE_ENV, process: g.process, agent: { list: async () => agents } })
  await h.start()
  await h.advance(11 * MIN)                          // first consult: snapshot a1, a2 running
  agents[0].status = 'completed'
  await h.turn('<task-notification>a1 completed</task-notification>')   // the notification turn refreshes the snapshot
  await h.advance(11 * MIN)
  const argv = g.calls.at(-1).argv
  assert.deepEqual(argv.slice(argv.indexOf('--running'), argv.indexOf('--running') + 4), ['--running', '1', '--finished', '0'])
  agents[1].status = 'failed'                        // a finish with no main turn since: news
  await h.advance(11 * MIN)
  const last = g.calls.at(-1).argv
  assert.equal(last[last.indexOf('--finished') + 1], '1')
})

// --- board lint (0.4.0) -----------------------------------------------------------------------------------------
test('board-lint line: on a second line of the beat; a silent, failing or odd lint leaves the beat unchanged', async () => {
  const cases = [
    ['[board-lint] 1 stale board item(s): TODO Waiting "X" — ledger 19.10 declined: X — fix them', true],
    ['', false],
    [new Error('boom'), false],
    ['some other output', false],
    ['[board-lint] 3 new note(s) in ~/inbox', false],   // must never fake the note count the mod parses
  ]
  for (const [answer, shown] of cases) {
    const calls = []
    const process = { run: async (argv, init) => {
      calls.push({ argv: [...argv], init })
      if (answer instanceof Error) throw answer
      return { exitCode: 0, stdout: answer + '\n', stderr: '' }
    } }
    const h = await harness(T0, { env: { HEARTBEAT_LINT: '/fake/board-lint' }, process })
    await h.start()
    await h.advance(11 * MIN)
    assert.equal(h.submitted.length, 1, 'the beat still goes out')
    const [first, ...rest] = h.submitted[0].text.split('\n')
    assert.match(first, /^\[heartbeat\] Idle \d+\+ min, 0 new note\(s\) in ~\/inbox\./)
    assert.deepEqual(rest, shown ? [answer] : [])
    assert.deepEqual(calls.map(c => c.argv), [['/fake/board-lint', '--beat']], 'one lint run per beat')
    assert.ok(calls[0].init.timeoutMs > 0 && calls[0].init.timeoutMs <= 5000, 'bounded')
    assert.equal(h.state().beats, 1)
  }
})

test('board-lint: off in tests unless HEARTBEAT_LINT is set (no process runs)', async () => {
  const h = await harness(T0, { process: { run: async () => { throw new Error('must not run') } } })
  await h.start()
  await h.advance(11 * MIN)
  assert.equal(h.submitted.length, 1)
  assert.ok(!h.submitted[0].text.includes('\n'))
})

// --- 0.5.0: idle compaction, owner idle, skip memory, now lane -------------------------------------------------------
const COMPACT_ON = { enabled: true, owner_idle_min: 0 }   // owner idle aside: the first idle beat may compact
const PROJ = projectDir('/home/node/jobs/hbtest')   // projectDir of the harness's cwd (under this HOME)
const compactLog = h => (h.files.get(`${D}/compact.jsonl`)?.data ?? '').trim().split('\n').filter(Boolean).map(l => JSON.parse(l))

// A fake $.session: usage answers `tokens`; compact answers `answer(args)` (a value, a promise or a throw) and records
// when it ran (submitsBefore: beats submitted so far).
function fakeSession(answer = () => ({ messages: [], tokensBefore: 210_000, tokensAfter: 30_000 }), tokens = 200_000) {
  const s = { usage: 0, compacts: [], h: null, tokens }
  s.session = {
    usage: async () => { s.usage++; return { startedAt: 0, context: { tokens: s.tokens, window: 1_000_000 }, rateLimits: [] } },
    compact: async args => { s.compacts.push({ args, at: s.h.now, submitsBefore: s.h.submitted.length }); return answer(args) },
  }
  return s
}
async function compactHarness(conf, answer, opts = {}) {
  const fs = fakeSession(answer, opts.tokens)
  const h = await harness(T0, { session: opts.noSession ? undefined : fs.session, sleep: opts.sleep, env: opts.env, process: opts.process, agent: opts.agent })
  fs.h = h
  if (conf !== null) h.files.set(`${D}/compact.json`, { data: typeof conf === 'string' ? conf : JSON.stringify(conf), mtimeMs: T0 })
  await h.start()
  return { h, fs }
}

test('0.5.0 pure: compact.json, owner input, the compaction decision, outcomes, now lane', () => {
  assert.deepEqual(compactConfig(undefined), { ...COMPACT_DEFAULTS })
  assert.deepEqual(compactConfig('{oops'), { ...COMPACT_DEFAULTS })
  assert.deepEqual(compactConfig('[1]'), { ...COMPACT_DEFAULTS })
  assert.equal(COMPACT_DEFAULTS.enabled, false, 'off unless the brain switches it on')
  assert.equal(compactConfig('{"enabled": "true"}').enabled, false, 'only a JSON true enables it')
  assert.deepEqual(compactConfig('{"enabled": true, "min_tokens": 1000, "gap_min": 0, "owner_idle_min": "5", ' +
    '"with_notes": true, "instructions": " keep the plan "}'),
  { enabled: true, min_tokens: 20000, owner_idle_min: 15, gap_min: 10, with_notes: true, instructions: 'keep the plan' })

  for (const kind of ['composer', 'bridge', 'channel', 'slack-ping', 'auto-continuation']) assert.ok(isOwnerInput('hi', { kind }), kind)
  assert.ok(isOwnerInput('hi', undefined), 'no origin (an older host) counts as him')
  for (const kind of ['plugin', 'task-notification', 'peer', 'scheduled-trigger', 'unclassified', 'sdk'])
    assert.ok(!isOwnerInput('hi', { kind }), kind)
  assert.ok(!isOwnerInput('[heartbeat] Idle 10+ min, 0 new note(s) in ~/inbox.', { kind: 'composer' }), 'a typed beat is not him')
  const s = { lastOwner: 0 }
  assert.equal(ownerInput(s, 'hi', { kind: 'channel', server: 'plugin:telegram-plus:telegram' }, 1000), true)
  assert.equal(s.lastOwner, 1000)
  turnEnded(s, 'answer', 5000)
  assert.equal(s.lastOwner, 5000, "the end of his turn is the owner clock")
  turnEnded(s, 'answer', 9000)
  assert.equal(s.lastOwner, 5000, 'other turns leave it')

  const on = compactConfig('{"enabled": true}'), idle = { lastOwner: 0, lastCompact: 0 }
  assert.equal(compactBlocked(compactConfig(''), idle, 100, 0, 1), 'off')
  assert.equal(compactBlocked(on, { ...idle, compactUnsupported: true }, 100, 0, 1), 'unsupported')
  assert.equal(compactBlocked(on, idle, 100, 1, 1), 'notes')
  assert.equal(compactBlocked({ ...on, with_notes: true }, idle, 100, 1, 1), '')
  assert.equal(compactBlocked(on, { lastOwner: 90 }, 100, 0, 1), 'owner active')
  assert.equal(compactBlocked(on, { lastOwner: 0, lastCompact: 60 }, 100, 0, 1), 'gap')
  assert.equal(compactBlocked(on, { lastOwner: 0, lastCompactTry: 60 }, 100, 0, 1), 'gap', 'a failed try waits gap_min too')
  assert.equal(compactDue(on, idle, 100, NaN, 0, 1).why, 'no token count')
  assert.deepEqual(compactDue(on, idle, 100, 159_999, 0, 1), { due: false, why: 'small: 160k < 160k' })
  assert.equal(compactDue(on, idle, 100, 160_000, 0, 1).due, true)
  assert.ok(Number.isNaN(compactTokens({ context: { window: 1e6 } })), 'no tokens right after a compaction')
  assert.equal(compactTokens({ context: { tokens: 200_000, window: 1e6 } }), 200_000)

  assert.equal(compactOutcome(undefined, true, new TypeError('x is not a function')).outcome, 'unsupported')
  assert.equal(compactOutcome(undefined, true, new Error('a turn is running')).outcome, 'error')
  assert.equal(compactOutcome(COMPACT_TIMEOUT).outcome, 'timeout')
  assert.deepEqual(compactOutcome({ skip: 'vetoed' }), { outcome: 'skip', error: 'vetoed' })
  assert.deepEqual(compactOutcome({ messages: [], tokensBefore: 300_000, tokensAfter: 30_000 }), { outcome: 'ok', before: 300_000, after: 30_000 })

  const notIdle = { fire: false, why: 'not idle' }
  assert.equal(nowLaneDue(notIdle, 1, 50, 40, INBOX_MIN, 1), true)
  assert.equal(nowLaneDue(notIdle, 1, 50, 50, INBOX_MIN, 1), false, 'already asked about this note')
  assert.equal(nowLaneDue(notIdle, 1, 50, 40, INBOX_MIN - 1, 1), false, 'the main loop itself is busy')
  assert.equal(nowLaneDue({ fire: false, why: 'gap' }, 1, 50, 40, 99, 1), false)
  assert.equal(nowLaneDue(notIdle, 0, 0, 0, 99, 1), false)
  assert.equal(newestFile([{ kind: 'file', mtimeMs: 5 }, { kind: 'dir', mtimeMs: 9 }, { kind: 'file', mtimeMs: 7 }], 6), 7)
})

test('0.5.0 idle compaction: off without compact.json; owner idle 15 min; before the beat; logged; once per gap_min', async () => {
  const off = await compactHarness(null)
  await nextBeat(off.h, 15)
  assert.equal(off.fs.usage + off.fs.compacts.length, 0, 'no compact.json: no $.session call at all')
  assert.equal(off.h.state().compact.enabled, false)

  const { h, fs } = await compactHarness({ enabled: true })   // owner_idle_min 15, gap_min 45, min_tokens 160k
  let [b] = await nextBeat(h, 15)
  assert.equal(fs.compacts.length, 0, 'the session started 10 min ago: that counts as the owner')
  assert.equal(h.state().compact.last, 'owner active')
  await h.turn(FRAME(b.text), { tools: ['Agent'] })   // real work: no backoff, beats stay 10 min apart
  ;[b] = await nextBeat(h, 15)
  assert.equal(fs.compacts.length, 1, 'idle 20 min, 200k >= 160k')
  assert.equal(fs.compacts[0].submitsBefore, 0, 'compacted before the beat was submitted')
  assert.equal(fs.compacts[0].args, undefined, 'plain $.session.compact() without instructions')
  assert.match(b.text, /^\[heartbeat\] Idle 10\+ min, 0 new note\(s\) in ~\/inbox\. Run your heartbeat routine\./)
  assert.match(b.text, /\n\[compact\] idle compaction before this beat: 210k -> 30k context tokens$/)
  let log = compactLog(h)
  assert.equal(log.length, 1)
  assert.deepEqual({ ...log[0], ts: 0, ms: 0 }, { ts: 0, ms: 0, trigger: 'mod', outcome: 'ok', before: 210_000, after: 30_000, tokens: 200_000 })
  const st = h.state()
  assert.equal(st.version, '0.5.0')
  assert.ok(st.lastCompact > 0 && st.lastCompact === log[0].ts)
  assert.equal(st.compacting, null)
  await h.turn(FRAME(b.text), { tools: ['Agent'] })

  // His message: the owner clock restarts at the end of his turn (no compaction for 15 min after it).
  ;[b] = await nextBeat(h, 15)
  assert.equal(fs.compacts.length, 1, 'within gap_min (45)')
  assert.equal(h.state().compact.last, 'gap')
  await h.turn(FRAME(b.text), { tools: ['Agent'] })
  for (let i = 0; i < 3; i++) { [b] = await nextBeat(h, 15); await h.turn(FRAME(b.text), { tools: ['Agent'] }) }
  const msg = '<channel source="plugin:telegram-plus:telegram">hi</channel>'
  await h.fire('prompt.submit', { text: msg, origin: { kind: 'channel', server: 'plugin:telegram-plus:telegram' }, wait: false })
  await h.turn(msg, { tools: ['mcp__plugin_telegram-plus_telegram__reply'] })
  const ownerEnd = h.now
  assert.equal((await h.ticked()).lastOwner, ownerEnd)
  ;[b] = await nextBeat(h, 15)
  assert.equal(fs.compacts.length, 1, 'gap passed, but he wrote 10 min ago')
  assert.equal(h.state().compact.last, 'owner active')
  await h.turn(FRAME(b.text), { tools: ['Agent'] })
  ;[b] = await nextBeat(h, 15)
  assert.equal(fs.compacts.length, 2, 'owner idle 20 min and the last compaction 45+ min ago')

  // A beat with notes never compacts (with_notes false); a small context doesn't either.
  await h.turn(FRAME(b.text), { tools: ['Agent'] })
  h.files.set(`${D}/compact.json`, { data: JSON.stringify({ enabled: true, gap_min: 10, owner_idle_min: 0 }), mtimeMs: h.now })
  await h.advance(5 * MIN)
  h.note('n.md')
  ;[b] = await nextBeat(h, 5)
  assert.match(b.text, /1 new note\(s\)/)
  assert.equal(fs.compacts.length, 2)
  assert.equal(h.state().compact.last, 'notes')
  await h.turn(FRAME(b.text), { tools: ['Agent'] })
  fs.tokens = 120_000
  ;[b] = await nextBeat(h, 15)
  assert.equal(fs.compacts.length, 2)
  assert.match(h.state().compact.last, /^small: 120k < 160k$/)
  assert.ok(!b.text.includes('[compact]'))
})

test('0.5.0 idle compaction never holds the beat: error, skip, timeout, no $.session, instructions', async () => {
  const cases = [
    ['error', () => { throw new Error('a turn is running') }, {}],
    ['skip', () => ({ skip: 'vetoed by another plugin' }), {}],
    ['timeout', () => new Promise(() => {}), { sleep: async () => {} }],   // compact never answers; the timer fires
    ['unsupported', null, { noSession: true }],
  ]
  for (const [outcome, answer, opts] of cases) {
    const { h, fs } = await compactHarness({ ...COMPACT_ON, instructions: 'keep the plan' }, answer, opts)
    let [b] = await nextBeat(h, 15)
    assert.match(b.text, /^\[heartbeat\] Idle 10\+ min, 0 new note\(s\)/, outcome)
    assert.ok(!b.text.includes('[compact]'), outcome)
    const log = compactLog(h)
    assert.equal(log.length, 1, outcome)
    assert.equal(log[0].outcome, outcome)
    if (outcome !== 'unsupported') assert.deepEqual(fs.compacts[0].args, { instructions: 'keep the plan' })
    const st = h.state()
    assert.equal(st.lastCompact, 0, outcome)
    assert.equal(st.compact.unsupported, outcome === 'unsupported')
    if (outcome !== 'unsupported') assert.ok(st.compact.lastTry > 0, 'a failed try waits gap_min')
    assert.equal(st.lastError, '', outcome)
    await h.turn(FRAME(b.text), { tools: ['Agent'] })
    ;[b] = await nextBeat(h, 15)
    assert.equal(compactLog(h).length, 1, `${outcome}: no second try within gap_min (or ever, when unsupported)`)
  }
})

test('0.5.0 compaction observer and restarts: /compact and auto count, precompute/subagent/skip do not; lastCompact persists', async () => {
  const h = await harness(T0)
  h.files.set(`${D}/state.json`, { data: JSON.stringify({ lastCompact: T0 - 10 * MIN, lastOwner: T0 - 99 * MIN }), mtimeMs: T0 })
  await h.start()
  let st = h.state()
  assert.equal(st.lastCompact, T0 - 10 * MIN, 'kept from the previous run')
  assert.equal(st.lastOwner, T0, 'the owner clock starts at session start')
  await h.advance(MIN)
  const done = { messages: [], tokensBefore: 380_000, tokensAfter: 40_000 }
  for (const e of [{ trigger: 'precompute' }, { trigger: 'auto', agentId: 'a1' }]) {
    const r = await h.call('session.compact', { ...e, messages: [] }, async () => done)
    assert.equal(r, done, 'the result passes on unchanged')
  }
  await h.call('session.compact', { trigger: 'manual', messages: [] }, async () => ({ skip: 'blocked' }))
  assert.equal((await h.ticked()).lastCompact, T0 - 10 * MIN, 'nothing installed: not counted')
  await h.call('session.compact', { trigger: 'manual', messages: [] }, async () => done)
  await h.advance(MIN)
  st = h.state()
  assert.ok(st.lastCompact > T0, 'his /compact counts')
  const log = compactLog(h)
  assert.equal(log.length, 1)
  assert.deepEqual({ trigger: log[0].trigger, outcome: log[0].outcome, before: log[0].before, after: log[0].after },
    { trigger: 'manual', outcome: 'ok', before: 380_000, after: 40_000 })
})

test('0.5.0 skip memory: a note the gate skipped (digest/mute) re-asks it after a full idle threshold, not every 15 s', async () => {
  const g = fakeGate({ decision: 'skip', reason: 'nothing startable (+1 digest/mute)' })
  const h = await harness(T0, { env: GATE_ENV, process: g.process, agent: { list: async () => [] } })
  await h.start()
  await h.advance(MIN)
  h.note('digest.md')
  await h.advance(12 * MIN)
  assert.equal(h.submitted.length, 0)
  assert.equal(g.calls.length, 2, `asked at ~2 min, then a full threshold later (0.4.0: every tick), got ${g.calls.length}`)
  const notesArg = c => c.argv[c.argv.indexOf('--notes') + 1]
  assert.equal(notesArg(g.calls[0]), '1', 'the first ask saw the note')
  assert.equal(notesArg(g.calls[1]), '0', 'afterwards the skipped note is not new')
  h.note('fresh.md')
  await h.advance(MIN)
  assert.equal(g.calls.length, 3, 'a newer note asks again at once')
  assert.equal(notesArg(g.calls[2]), '1')
})

test('0.5.0 now lane: agents keep the session busy; one --now-only ask per newer note; beats only for now:', async () => {
  let answer = { decision: 'skip', reason: 'now lane: no now-class note' }
  const g = fakeGate(() => answer)
  const agents = [{ id: 'a1', status: 'running', description: 'build', type: 'general-purpose' }]
  const { h, fs } = await compactHarness(COMPACT_ON, undefined,
    { env: { ...GATE_ENV, HEARTBEAT_LINT: '/fake/lint' }, process: g.process, agent: { list: async () => agents } })
  h.files.set(`${PROJ}/s1.jsonl`, { data: '', mtimeMs: T0 })   // the main transcript, quiet since the start
  const busy = async mins => {                                  // a subagent writes its transcript every tick
    for (let i = 0; i < mins * 4; i++) { h.files.set(`${PROJ}/s1/subagents/agent-a1.jsonl`, { data: '', mtimeMs: h.now }); await h.advance(MIN / 4) }
  }
  await busy(1)
  h.note('vault-events-ha-1.md')
  await busy(6)
  const lane = () => g.calls.filter(c => c.argv.includes('--now-only'))
  assert.equal(h.submitted.length, 0)
  assert.equal(lane().length, 1, 'asked once for the note, not every tick')
  assert.deepEqual(lane()[0].argv, ['/fake/hb-gate', '--json', '--now-only', '--notes', '1', '--last-beat', '0'])
  assert.equal(g.calls.length, 1, 'the regular path never ran: the session never looked idle')
  assert.match(h.state().nowLane.last, /^skip: now lane/)

  answer = { decision: 'wake', reason: 'now: 1 note(s) (vault-events-ha) [now lane]' }
  h.note('vault-events-ha-2.md')
  await busy(1)
  assert.equal(lane().length, 2)
  assert.equal(h.submitted.length, 1)
  assert.equal(h.submitted[0].text, '[heartbeat] Idle 2+ min, 2 new note(s) in ~/inbox (now-class, background agents running). ' +
    'Run your heartbeat routine. TEST ONLY: reply with the single word OK and use no tools.')
  assert.equal(g.calls.filter(c => c.argv[0] === '/fake/lint').length, 0, 'no lint in the lane')
  assert.equal(fs.usage + fs.compacts.length, 0, 'no compaction in the lane')
  const st = h.state()
  assert.equal(st.lastSkip, 0)
  assert.deepEqual([st.nowLane.asks, st.nowLane.beats], [2, 1])
  assert.equal(st.beats, 1)

  const off = await harness(T0, { env: { ...GATE_ENV, HEARTBEAT_NOW_LANE: 'off' }, process: fakeGate(answer).process, agent: { list: async () => agents } })
  await off.start()
  assert.equal(off.state().nowLane.on, false, 'HEARTBEAT_NOW_LANE=off')
})
