// Offline tests for the heartbeat mod's decision logic: node --test ~/plugins/heartbeat/test/
// Includes a 7-day simulation of the mod next to the real cron heartbeat's rules (/usr/local/bin/heartbeat).
import { homedir } from 'node:os'
import { test } from 'node:test'
import assert from 'node:assert/strict'
import { decide, inCronWindow, countNew, projectDir, INBOX_MIN, GAP_MIN, ownerHour, setOwnerZone, DEFAULT_TZ, capMin, plainIdleMin,
  isBeatText, beatNotes, turnStarted, toolCalled, inputArrived, turnEnded, subagentsWrote, UNANSWERED_MAX_MIN,
  gateVerdict, agentDelta } from '../hooks/register.js'

const S = 1000, MIN = 60 * S
const cfg = { minute: MIN, guard: true, baseMin: 10 }
const T0 = Date.UTC(2026, 9, 11, 12, 0, 0)          // a cron boundary (:00, minute divisible by 5); 14:00 in Zurich
const at = (m, s = 0) => T0 + m * MIN + s * S
const idle = { busy: false, pending: false, lastActivity: 0, lastBeat: 0, marker: 0, notes: 0, newest: 0 }

test('cron window: :x4:50 to :x5:45', () => {
  for (const [m, s, want] of [[0, 0, true], [0, 44, true], [0, 45, false], [2, 0, false], [4, 49, false],
    [4, 50, true], [5, 0, true], [5, 30, true], [7, 59, false], [9, 59, true]])
    assert.equal(inCronWindow(at(m, s)), want, `${m}:${s}`)
})

test('plain beat only after HEARTBEAT_MINUTES idle', () => {
  const now = at(12)                                   // outside the cron window
  assert.equal(decide({ ...idle, lastActivity: now - 10 * MIN + S }, now, cfg).why, 'not idle')
  assert.deepEqual(decide({ ...idle, lastActivity: now - 10 * MIN }, now, cfg), { fire: true, idleMin: 10, notes: 0 })
  assert.equal(decide({ ...idle, lastActivity: now - 20 * MIN }, now, { ...cfg, baseMin: 30 }).why, 'not idle')
})

test('new inbox notes: 2 min idle is enough', () => {
  const now = at(12)
  assert.equal(decide({ ...idle, notes: 1, lastActivity: now - 2 * MIN + S }, now, cfg).why, 'not idle')
  assert.deepEqual(decide({ ...idle, notes: 3, lastActivity: now - 2 * MIN }, now, cfg), { fire: true, idleMin: INBOX_MIN, notes: 3 })
})

test('never during a turn (incl. a permission prompt) or with a beat pending', () => {
  const now = at(12)
  assert.equal(decide({ ...idle, busy: true }, now, cfg).why, 'turn running')
  assert.equal(decide({ ...idle, pending: true }, now, cfg).why, 'beat pending')
})

test('subagent/transcript activity counts as activity, like cron', () => {
  const now = at(12)
  assert.equal(decide({ ...idle, newest: now - 9 * MIN }, now, cfg).why, 'not idle')
  assert.equal(decide({ ...idle, newest: now - 10 * MIN }, now, cfg).fire, true)
})

test('>= 5 min after any beat (own, cron, restart waker)', () => {
  const now = at(12)
  assert.equal(decide({ ...idle, notes: 1, lastBeat: now - 4 * MIN }, now, cfg).why, 'gap')
  assert.equal(decide({ ...idle, notes: 1, marker: now - 4 * MIN }, now, cfg).why, 'gap')
  assert.equal(decide({ ...idle, notes: 1, marker: now - GAP_MIN * MIN }, now, cfg).fire, true)
})

test('blocked in the cron window, test mode without guard', () => {
  assert.equal(decide(idle, at(10, 5), cfg).why, 'cron window')
  assert.equal(decide(idle, at(10, 5), { ...cfg, guard: false }).fire, true)
})

test('countNew and projectDir', () => {
  const e = [{ name: 'a.md', kind: 'file', mtimeMs: 10 }, { name: 'b.md', kind: 'file', mtimeMs: 5 },
    { name: 'd', kind: 'dir', mtimeMs: 0 }, { name: 'l', kind: 'other', mtimeMs: 99 }]
  assert.equal(countNew(e, 5), 1)
  assert.equal(countNew(e, 0), 2)
  assert.equal(projectDir('/home/node/workspace'), `${homedir()}/.claude/projects/-home-node-workspace`)
})

// --- idle backoff (0.2.0) ---------------------------------------------------------------------------------
const zurich = (y, mo, d, h, mi = 0) => {           // a Zurich wall-clock time -> ms (CEST until Oct 25, 2026)
  const off = Date.UTC(y, mo, d) < Date.UTC(2026, 9, 25) ? 2 : 1
  return Date.UTC(y, mo, d, h - off, mi)
}
const BEAT = n => `[heartbeat] Idle 10+ min, ${n} new note(s) in ~/inbox. Run your heartbeat routine.`
const MSG = '<channel source="plugin:telegram-plus:telegram" chat_id="1" message_id="2" user="owner">hi</channel>'
// The host's frame around the mod's beat (Claude Code 2.1.296, verbatim from the transcript of 2026-10-13): turn.start
// gets this, prompt.submit hooks get the beat bare. The frame sits on a line of its own.
const FRAME = t => `The heartbeat plugin sent a message:\n${t}\n\nThis is how Claude Code surfaces a prompt a plugin ` +
  "submits between turns — it starts this turn in the user's place. Address the message above."
const fresh = () => ({ busy: false, pending: false, lastActivity: 0, lastBeat: 0, streak: 0, unanswered: false, hb: null, hbStart: 0 })
function hbTurn(s, now, { notes = 0, tools = ['Bash'], reason = 'answer', input = null } = {}) {   // a mod beat, as the host runs it
  inputArrived(s, BEAT(notes))                // prompt.submit: bare, origin plugin "heartbeat", no turnId
  turnStarted(s, FRAME(BEAT(notes)), now)     // turn.start: framed
  for (const t of tools) toolCalled(s, t)
  if (input) inputArrived(s, input)
  turnEnded(s, reason, now + 30 * S)
}

test('quiet hours: Zurich 00:00-08:00 caps at 120 min, else 60 (summer and winter time)', () => {
  for (const [ms, h, cap] of [[zurich(2026, 9, 12, 23, 59), 23, 60], [zurich(2026, 9, 13, 0, 0), 0, 120],
    [zurich(2026, 9, 13, 7, 59), 7, 120], [zurich(2026, 9, 13, 8, 0), 8, 60], [zurich(2026, 11, 2, 0, 30), 0, 120],
    [zurich(2026, 11, 2, 8, 1), 8, 60]]) {
    assert.equal(ownerHour(ms), h, new Date(ms).toISOString())
    assert.equal(capMin(ms), cap, new Date(ms).toISOString())
  }
})

test('owner zone: the profile zone moves quiet hours; junk or empty falls back to the default', () => {
  const ms = Date.UTC(2026, 9, 13, 5, 0)                 // 07:00 in Zurich, 01:00 in New York
  assert.equal(setOwnerZone('America/New_York'), 'America/New_York')
  assert.equal(ownerHour(ms), 1)
  for (const z of ['Bogus/Zone', '', undefined, 42]) assert.equal(setOwnerZone(z), DEFAULT_TZ)
  assert.equal(setOwnerZone(' Europe/Zurich\n'), DEFAULT_TZ)
  assert.equal(ownerHour(ms), 7)
})

test('backoff ladder: 2 empty beats in a row, then doubling up to the cap, never below the base', () => {
  const day = zurich(2026, 9, 13, 14), night = zurich(2026, 9, 13, 3)
  assert.deepEqual([0, 1, 2, 3, 4, 5, 9, 99].map(k => plainIdleMin(k, day, 10)), [10, 10, 20, 40, 60, 60, 60, 60])
  assert.deepEqual([0, 1, 2, 3, 4, 5, 9, 99].map(k => plainIdleMin(k, night, 10)), [10, 10, 20, 40, 80, 120, 120, 120])
  assert.equal(plainIdleMin(5, day, 90), 90)          // HEARTBEAT_MINUTES above the cap stays as set
  assert.equal(plainIdleMin(3, day, 30), 60)
})

test('beat texts: mod (as the transcript shows it), cron, restart waker; not a message quoting one', () => {
  assert.ok(isBeatText(BEAT(0)))
  assert.ok(isBeatText('The heartbeat plugin sent a message: ' + BEAT(2)))
  assert.ok(isBeatText(FRAME(BEAT(2))), 'the real frame: beat on the next line')
  assert.ok(isBeatText(`The heartbeat plugin sent a message while you were working:\n${BEAT(0)}`), 'mid-turn frame')
  assert.equal(beatNotes(FRAME(BEAT(2))), 2)
  assert.ok(!isBeatText(MSG.replace('hi', 'hi\n' + BEAT(0))), 'a message pasting a beat on its second line')
  assert.ok(!isBeatText('The telegram plugin sent a message:\nhi'))
  assert.ok(isBeatText('[heartbeat] Just restarted (x). Run your heartbeat routine'))
  assert.ok(!isBeatText(MSG.replace('hi', 'why so many [heartbeat] turns?')))
  assert.ok(!isBeatText('<task-notification> <task-id>a1</task-id>'))
  assert.ok(!isBeatText(''))
  assert.equal(beatNotes(BEAT(3)), 3)
  assert.equal(beatNotes('[heartbeat] Just restarted (x).'), -1)
})

test('empty heartbeat turns build the streak; real work, notes, input and other turns reset it', () => {
  const s = fresh(), now = zurich(2026, 9, 13, 14, 2)   // outside the cron window
  hbTurn(s, now); hbTurn(s, now, { tools: ['Bash', 'Read'] }); hbTurn(s, now, { tools: [] })
  assert.equal(s.streak, 3)
  assert.equal(decide({ ...idle, ...s, lastActivity: now - 39 * MIN }, now, cfg).why, 'not idle')
  assert.deepEqual(decide({ ...idle, ...s, lastActivity: now - 40 * MIN }, now, cfg), { fire: true, idleMin: 40, notes: 0 })
  // inbox notes still wake after INBOX_MIN, whatever the streak
  assert.equal(decide({ ...idle, ...s, notes: 1, lastActivity: now - INBOX_MIN * MIN }, now, cfg).fire, true)
  const resets = [
    ['3 tool calls', x => hbTurn(x, now, { tools: ['Bash', 'Bash', 'Bash'] })],
    ['an Agent launch', x => hbTurn(x, now, { tools: ['Agent'] })],
    ['a Telegram reply', x => hbTurn(x, now, { tools: ['mcp__plugin_telegram-plus_telegram__reply'] })],
    ['a beat with notes', x => hbTurn(x, now, { notes: 1 })],
    ['a message during the beat', x => hbTurn(x, now, { input: MSG })],
    ['an aborted beat (Esc/Stop)', x => hbTurn(x, now, { reason: 'aborted' })],
    ['the restart waker', x => { turnStarted(x, '[heartbeat] Just restarted (y).', now); turnEnded(x, 'answer', now) }],
    ['an owner message', x => { inputArrived(x, MSG); turnStarted(x, MSG, now); turnEnded(x, 'answer', now) }],
    ['a task notification', x => { turnStarted(x, '<task-notification>', now); turnEnded(x, 'answer', now) }],
    ['subagent activity', x => subagentsWrote(x, now + 1)],
  ]
  for (const [what, f] of resets) { const x = { ...fresh(), streak: 4, hbStart: now }; f(x); assert.equal(x.streak, 0, what) }
  const x = { ...fresh(), streak: 4, hbStart: now }
  hbTurn(x, now, { reason: 'error' }); assert.equal(x.streak, 4, 'an API error says nothing')
  subagentsWrote(x, now - 1); assert.equal(x.streak, 4, 'subagents quiet since the last heartbeat turn')
  inputArrived(x, BEAT(0)); assert.equal(x.streak, 4, 'a heartbeat prompt is not news')
})

test('regression 2026-10-13: framed mod beats answered by one quick Bash call build the streak (0.2.0: stuck at 0)', () => {
  const s = fresh(), t0 = zurich(2026, 9, 13, 4, 12)
  for (let i = 0; i < 3; i++) {                         // 04:12, 04:22, 04:32, as in the transcript
    const t = t0 + i * 10 * MIN
    inputArrived(s, BEAT(0))                            // prompt.submit sees the beat bare
    assert.equal(turnStarted(s, FRAME(BEAT(0)), t), false, 'recognized by its text, no drift')
    toolCalled(s, 'Bash')
    turnEnded(s, 'answer', t + 3 * S)
    assert.equal(s.streak, i + 1, `streak after beat ${i + 1}`)
  }
  assert.equal(plainIdleMin(s.streak, t0 + 21 * MIN, 10), 40, 'backed off')
})

test('frame drift: a beat that entered while idle still counts if turn.start no longer shows it, and is flagged', () => {
  const s = fresh(), now = T0
  inputArrived(s, BEAT(0))
  assert.equal(turnStarted(s, '<plugin_message source="heartbeat">\n<content>\n' + BEAT(0), now), true, 'flagged')
  toolCalled(s, 'Bash'); turnEnded(s, 'answer', now + 3 * S)
  assert.equal(s.streak, 1, 'counted through prompt.submit')
  inputArrived(s, BEAT(0)); inputArrived(s, MSG)       // the owner's message right behind it
  assert.equal(turnStarted(s, MSG, now + MIN), false); turnEnded(s, 'answer', now + MIN)
  assert.equal(s.streak, 0, 'news')
  inputArrived(s, BEAT(0), true)                       // typed into a running turn: no turn.start of its own follows
  assert.equal(turnStarted(s, '', now + 2 * MIN), false, 'a continuation is not a beat')
  assert.equal(s.hb, null)
})

test('no new beat while the last one is unanswered (up to UNANSWERED_MAX_MIN)', () => {
  const now = at(12), s = { ...idle, unanswered: true, lastBeat: now - 30 * MIN, lastActivity: now - 30 * MIN }
  assert.equal(decide({ ...s, notes: 2 }, now, cfg).why, 'unanswered')
  assert.equal(decide({ ...s, lastBeat: now - UNANSWERED_MAX_MIN * MIN }, now, cfg).fire, true)
  const a = { ...fresh(), unanswered: true }; hbTurn(a, now); assert.equal(a.unanswered, false, 'answered')
  const b = { ...fresh(), unanswered: true }; hbTurn(b, now, { reason: 'error' })
  assert.equal(b.unanswered, false, 'an API error consumed the beat too: inbox notes must not wait for the cap')
  const c = { ...fresh(), unanswered: true }; turnStarted(c, MSG, now); turnEnded(c, 'answer', now)
  assert.equal(c.unanswered, false, 'any ended main turn clears it')
})

// --- simulation: mod (ticks every 15 s) + cron (every 5 min: /usr/local/bin/heartbeat's rules behind ---------
// ops/heartbeat-cron, i.e. while the mod is alive its backoff threshold and its unanswered hold apply).
// Random mode: owner messages 08:00-24:00 Zurich, inbox notes, subagent runs ending in task notifications;
// heartbeat turns are quick checks, handle notes, or start work. Scripted mode: only the given events.
function rng(seed) {   // mulberry32
  return () => { seed = (seed + 0x6D2B79F5) | 0; let t = Math.imul(seed ^ (seed >>> 15), 1 | seed)
    t = (t + Math.imul(t ^ (t >>> 7), 61 | t)) ^ t; return ((t ^ (t >>> 14)) >>> 0) / 4294967296 }
}

function simulate({ seed = 1, days = 7, mod = true, start = T0, script = null }) {
  const r = rng(seed), end = Math.round(days * 86400), phase = Math.floor(r() * 15)
  const ms = t => start + t * S
  const night = t => ownerHour(ms(t)) < 8
  const st = { busy: false, pending: false, lastActivity: start, lastBeat: 0, started: start,
    streak: 0, unanswered: false, hb: null, hbStart: 0 }
  let turnEnd = -1, turnKind = '', races = 0, waiting = false, mainMtime = start, subMtime = 0, subUntil = -1, marker = 0
  const queue = [], notes = [], beats = [], problems = []
  const msgAt = new Set(script?.msgs ?? []), noteAt = new Set(script?.notes ?? [])
  let nextMsg = script ? Infinity : r() * 3600, nextNote = script ? Infinity : r() * 3600
  let nextSub = script ? Infinity : r() * 7200, cronEnter = null, taskAt = -1
  const newest = () => Math.max(mainMtime, subMtime)
  const spawn = t => { subUntil = Math.max(subUntil, t + 1200 + r() * 2400); taskAt = Math.ceil(subUntil) }
  const startTurn = (t, kind, n = 0, src = '') => {
    const long = kind === 'msg' && r() < 0.03          // a turn stuck on a permission prompt for a while
    const text = kind === 'hb' ? BEAT(n) : kind === 'msg' ? MSG : '<task-notification> <task-id>a1</task-id>'
    inputArrived(st, text)                             // prompt.submit: bare; turn.start: the mod's beat framed by the host
    turnStarted(st, src === 'mod' ? FRAME(text) : text, ms(t)); mainMtime = ms(t); waiting = long; turnKind = kind
    turnEnd = t + (long ? 900 + r() * 1500 : kind === 'msg' ? 5 + r() * 115 : 5 + r() * 55)
    if (kind === 'hb') {                               // the heartbeat routine handles and deletes the notes
      const had = notes.length > 0, tools = had ? ['Bash', 'Bash', 'Bash'] : ['Bash']
      notes.length = 0
      if (!script && r() < (had ? 0.3 : night(t) ? 0.1 : 0.3)) { tools.push('Agent'); spawn(t) }
      for (const x of tools) toolCalled(st, x)
    } else if (kind === 'msg' && !script && r() < 0.2) spawn(t)
  }
  const submit = (t, kind, n, src) => { if (st.busy) queue.push([kind, n, src]); else startTurn(t, kind, n, src) }
  const idleFor = t => ms(t) - Math.max(st.lastActivity, newest())
  const beat = (t, src, n) => {
    if (st.busy) problems.push(`${src} beat at ${t}s while a turn was running`)
    if (idleFor(t) < INBOX_MIN * MIN) problems.push(`${src} beat at ${t}s only ${idleFor(t) / S}s after activity`)
    const lim = mod ? plainIdleMin(st.streak, ms(t), cfg.baseMin) : cfg.baseMin
    if (n === 0 && idleFor(t) < lim * MIN) problems.push(`plain ${src} beat at ${t}s before ${lim} min idle`)
    beats.push({ t, src, n, streak: st.streak }); submit(t, 'hb', n, src)
  }
  for (let t = 1; t <= end; t++) {
    if (st.busy && t >= turnEnd) {
      waiting = false; turnEnded(st, 'answer', ms(t)); mainMtime = ms(t)
      if (queue.length) startTurn(t, ...queue.shift())
    }
    if (t >= nextMsg || msgAt.has(t)) {                // the owner sleeps 00:00-08:00
      if (!night(t) || msgAt.has(t)) submit(t, 'msg')
      if (!script) nextMsg = t - Math.log(1 - r()) * 25 * 60
    }
    if (t >= nextNote || noteAt.has(t)) { notes.push(ms(t)); if (!script) nextNote = t - Math.log(1 - r()) * 45 * 60 }
    if (t >= nextSub) { spawn(t); nextSub = subUntil + r() * 4 * 3600 }
    if (t < subUntil && t % 10 === 0) subMtime = ms(t)  // a background subagent writing its transcript
    if (t === taskAt) { taskAt = -1; submit(t, 'task') } // its task notification
    if (cronEnter === t) {                              // cron, 1 s after typing: Enter unless waiting on a dialog
      cronEnter = null
      if (!waiting) {
        const n = notes.filter(m => m > marker).length
        marker = ms(t)
        // A message turn that started in the 1 s between typing and Enter: cron's own old race (the reason
        // for the mod), not a double with the mod. A mod beat there would be one: beat() flags it.
        if (st.busy && turnKind === 'msg') { races++; beats.push({ t, src: 'cron', race: true }); submit(t, 'hb', n, 'cron') }
        else beat(t, 'cron', n)
      }
    }
    if (t % 300 === 0 && !(mod && st.unanswered)) {     // cron: heartbeat-cron -> heartbeat max(10, mod's idleMin)
      const n = notes.filter(m => m > marker).length
      const lim = n > 0 ? 3 : mod ? Math.max(10, plainIdleMin(st.streak, ms(t), cfg.baseMin)) : 10
      if (ms(t) - newest() >= lim * MIN && !st.busy) cronEnter = t + 1
    }
    if (mod && t % 15 === phase) {
      const n = countNew(notes.map(m => ({ kind: 'file', mtimeMs: m })), Math.max(st.started, st.lastBeat, marker))
      subagentsWrote(st, subMtime)
      const d = decide({ ...st, marker, notes: n, newest: newest() }, ms(t), cfg)
      if (d.fire) { st.lastBeat = marker = ms(t); st.unanswered = true; beat(t, 'mod', n) }
    }
  }
  return { beats, problems, races }
}

test('simulation: no double beats, none during a turn, none before the backoff threshold, mod is primary', () => {
  let mod = 0, cron = 0, backedOff = 0
  for (const seed of [1, 2, 3, 4, 5, 6, 7, 8]) {
    const { beats, problems } = simulate({ seed })
    assert.deepEqual(problems, [], `seed ${seed}`)
    for (let i = 1; i < beats.length; i++)
      assert.ok(beats[i].t - beats[i - 1].t >= 120, `seed ${seed}: beats ${beats[i - 1].t}s and ${beats[i].t}s`)
    mod += beats.filter(b => b.src === 'mod').length; cron += beats.filter(b => b.src === 'cron').length
    backedOff += beats.filter(b => b.streak >= 2).length
  }
  assert.ok(mod + cron > 8 * 7 * 24, `enough beats (${mod + cron})`)
  assert.ok(backedOff > 0, 'the backoff kicks in')
  assert.ok(mod / (mod + cron) > 0.85, `mod is primary (${mod} mod, ${cron} cron)`)
  const fallback = simulate({ seed: 9, mod: false })   // mod dead: cron alone keeps the heartbeat going
  assert.deepEqual(fallback.problems, [])
  assert.ok(fallback.beats.length > 7 * 24, `fallback beats: ${fallback.beats.length}`)
})

const NIGHT = zurich(2026, 9, 12, 23, 0), H = 3600   // Mon 23:00 Zurich, a cron boundary
const between = (beats, a, b) => beats.filter(x => x.t >= a && x.t < b)

test('quiet night 23:00-08:00: ~8 beats instead of 35-50, cron stays in step, beats again hourly by day', () => {
  const { beats, problems } = simulate({ start: NIGHT, days: 21 / 24, script: { msgs: [1] } })
  assert.deepEqual(problems, [])
  const night = between(beats, 0, 9 * H)
  assert.ok(night.length <= 8 && night.length >= 5, `night beats: ${night.map(b => (b.t / 60).toFixed(0)).join(' ')}`)
  for (let i = 1; i < night.length; i++) assert.ok(night[i].t - night[i - 1].t <= 121 * 60, 'never more than ~2 h apart')
  assert.ok(night.filter(b => b.src === 'cron').length <= 1, 'cron does not undo the backoff')
  const day = between(beats, 10 * H, 21 * H)                // 09:00-20:00, still nothing happening
  assert.ok(day.length >= 10 && day.length <= 12, `day beats: ${day.length}`)
  const old = simulate({ start: NIGHT, days: 9 / 24, mod: false, script: { msgs: [1] } })   // cron alone, as before
  assert.ok(old.beats.length >= 30, `without the backoff: ${old.beats.length}`)
})

test('a note at 03:00 still wakes within ~3 min; an owner message resets to the base interval', () => {
  const note = 4 * H, msg = 9.5 * H                         // 03:00 and 08:30
  const { beats, problems } = simulate({ start: NIGHT, days: 11 / 24, script: { msgs: [1, msg], notes: [note] } })
  assert.deepEqual(problems, [])
  const woke = beats.find(b => b.t >= note)
  assert.ok(woke.t - note <= 180 && woke.n === 1, `woke ${woke.t - note}s after the note`)
  const next = beats.find(b => b.t > woke.t)
  assert.ok(next.t - woke.t <= 12 * 60, `back to the base interval after a note (${(next.t - woke.t) / 60} min)`)
  const after = beats.find(b => b.t > msg)
  assert.ok(after.t - msg <= 13 * 60 && after.streak === 0, `beat ${(after.t - msg) / 60} min after the message`)
})

// --- gate (0.3.0): verdict parsing, agent deltas, skip restarts the plain idle clock --------------------------
test('gate verdict: skip only on exit 0 with a JSON skip line; anything else wakes (fail open)', () => {
  const ok = (o, extra = {}) => ({ exitCode: 0, stdout: JSON.stringify(o) + '\n', stderr: '', ...extra })
  assert.deepEqual(gateVerdict(ok({ decision: 'skip', reason: 'agent running: 1' })), { skip: true, reason: 'agent running: 1' })
  assert.equal(gateVerdict(ok({ decision: 'wake', reason: 'inbox: 1 note(s)' })).skip, false)
  assert.equal(gateVerdict({ exitCode: 0, stdout: 'noise\n' + JSON.stringify({ decision: 'skip', reason: 'x' }) }).skip, true,
    'the last line counts')
  for (const r of [ok({ decision: 'skip' }, { exitCode: 1 }), { exitCode: 0, stdout: 'skip: agent running' },
    { exitCode: 0, stdout: '' }, ok({ decision: 'SKIP' }), ok(['skip']), null, undefined, { exitCode: null, stdout: '' }]) {
    const v = gateVerdict(r)
    assert.equal(v.skip, false, JSON.stringify(r))
  }
  assert.match(gateVerdict({ exitCode: 2, stdout: '' }).reason, /^gate error: exit 2/)
  assert.match(gateVerdict({ exitCode: 0, stdout: '{oops' }).reason, /^gate error/)
})

test('agent delta: running ids, finishes since the snapshot (incl. new-and-done and dropped ones), none without one', () => {
  const list = [{ id: 'a', status: 'running' }, { id: 'b', status: 'completed' }, { id: 'c', status: 'pending' }]
  const first = agentDelta(null, list)
  assert.deepEqual(first.running, ['a', 'c'])
  assert.deepEqual(first.finished, [], 'no snapshot yet: nothing counts as finished')
  const later = agentDelta(first.snap, [{ id: 'a', status: 'completed' }, { id: 'b', status: 'completed' },
    { id: 'd', status: 'failed' }, { id: 'e', status: 'waiting' }, { id: 't', status: 'idle' }])
  assert.deepEqual(later.running, ['e'])
  assert.deepEqual(later.finished.sort(), ['a', 'c', 'd'], 'a ended, c left the list while active, d is new and done; b was done before')
  assert.deepEqual(agentDelta(later.snap, [{ id: 'e', status: 'running' }]).finished, [], 'done ones leaving the list are no news')
  assert.deepEqual(agentDelta({}, [null, {}, { id: 'x', status: 'killed' }]).finished, ['x'])
})

test('a gate skip restarts the plain idle clock (next check a full threshold later); notes still wake after 2 min', () => {
  const now = at(42), s = { ...idle, lastActivity: now - 30 * MIN, lastSkip: now - 10 * MIN + S }
  assert.equal(decide(s, now, cfg).why, 'gate skipped')
  assert.equal(decide({ ...s, lastSkip: now - 10 * MIN }, now, cfg).fire, true)
  assert.equal(decide({ ...s, notes: 1 }, now, cfg).fire, true, 'inbox wake ignores the skip')
  assert.equal(decide({ ...s, lastActivity: now - 60 * MIN, streak: 4, lastSkip: now - 20 * MIN }, now, cfg).why, 'gate skipped',
    'at the backed-off threshold (40 min)')
  assert.equal(decide({ ...idle, lastActivity: now - 30 * MIN }, now, cfg).fire, true, 'no lastSkip: as before')
})
