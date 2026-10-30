// Offline tests for ops/heartbeat-cron (the mod-aware cron fallback): node --test test/heartbeat-cron.test.mjs
// A fake real heartbeat records its argument; a fake `claude agents --json` reports the session's status. The
// transcript dir, inbox and marker are temp files with set mtimes; the clock is HEARTBEAT_NOW. Nothing is typed.
import { test } from 'node:test'
import assert from 'node:assert/strict'
import { spawnSync } from 'node:child_process'
import { mkdtempSync, mkdirSync, writeFileSync, readFileSync, rmSync, existsSync, utimesSync, symlinkSync } from 'node:fs'
import { tmpdir } from 'node:os'
import { join, dirname } from 'node:path'
import { fileURLToPath } from 'node:url'

const WRAPPER = join(dirname(fileURLToPath(import.meta.url)), '..', 'ops', 'heartbeat-cron')
const NOW = 1_791_850_000

const put = (path, mtime, body = '') => { writeFileSync(path, body); utimesSync(path, mtime, mtime) }

// transcript: seconds since the last transcript write (main or subagent); notes: ages (s) of inbox notes;
// marker: age (s) of /tmp/heartbeat (null: missing). real: override HEARTBEAT_REAL ('self': the wrapper itself).
function run(args, { state, status = 'idle', transcript = 60, notes = [], marker = 600, real } = {}) {
  const d = mkdtempSync(join(tmpdir(), 'hbcron-'))
  try {
    const out = join(d, 'called'), proj = join(d, 'proj'), inbox = join(d, 'inbox')
    mkdirSync(join(proj, 'sess', 'subagents'), { recursive: true }); mkdirSync(inbox)
    writeFileSync(join(d, 'real'), `#!/bin/sh\necho "$@" > ${out}\n`, { mode: 0o755 })
    writeFileSync(join(d, 'claude'),
      `#!/bin/sh\necho '[{"cwd":"/elsewhere","status":"idle"},{"cwd":"'"$HOME"'/workspace","status":"${status}"}]'\n`, { mode: 0o755 })
    writeFileSync(join(d, 'tmux'), `#!/bin/sh\necho tmux "$@" >> ${join(d, 'typed')}\n`, { mode: 0o755 })   // never reached
    put(join(proj, 'old.jsonl'), NOW - 86400)
    if (transcript !== null) put(join(proj, 'sess', 'subagents', 'agent-x.jsonl'), NOW - transcript)
    notes.forEach((age, i) => put(join(inbox, `note-${i}.md`), NOW - age, 'x'))
    if (marker !== null) put(join(d, 'marker'), NOW - marker)
    if (state !== undefined) writeFileSync(join(d, 'state.json'), typeof state === 'string' ? state : JSON.stringify(state))
    const r = spawnSync('sh', [WRAPPER, ...args], { encoding: 'utf8', timeout: 20_000, env: {
      PATH: `${d}:/usr/bin:/bin`, HOME: '/home/node',
      HEARTBEAT_REAL: real === 'self' ? WRAPPER : real ?? join(d, 'real'),
      HEARTBEAT_MOD_STATE: join(d, 'state.json'), HEARTBEAT_NOW: String(NOW),
      HEARTBEAT_PROJECT: proj, HEARTBEAT_INBOX: inbox, HEARTBEAT_MARKER: join(d, 'marker') } })
    assert.equal(existsSync(join(d, 'typed')), false)
    if (real !== undefined) return r.status
    assert.equal(r.status, 0, r.stderr)
    return existsSync(out) ? readFileSync(out, 'utf8').trim() : null   // null: the real script wasn't run
  } finally { rmSync(d, { recursive: true, force: true }) }
}

const alive = (extra = {}) => ({ lastTick: (NOW - 30) * 1000, idleMin: 10, unanswered: false, ...extra })
// What mod 0.2.x writes: next beat due in `dueIn` s = its last beat + a ~20 s turn + idleMin (pass a transcript
// age below the beat's age: its turn wrote the transcript after it).
const mod = (dueIn, extra = {}) => alive({ started: (NOW - 7200) * 1000, nextDue: (NOW + dueIn) * 1000,
  lastBeat: (NOW + dueIn - (extra.idleMin ?? 10) * 60 - 20) * 1000, ...extra })

test('no mod state, stale or old-format state, garbage: the old behaviour', () => {
  assert.equal(run(['10']), '10')
  assert.equal(run([]), '30')                                      // the real script's own default
  assert.equal(run(['10'], { state: alive({ lastTick: (NOW - 181) * 1000, idleMin: 60 }) }), '10')   // mod dead
  assert.equal(run(['10'], { state: { lastTick: (NOW - 30) * 1000, beats: 3, baseMin: 10 } }), '10')  // mod 0.1.x
  assert.equal(run(['10'], { state: 'not json' }), '10')
  assert.equal(run(['10'], { state: alive({ lastTick: 'x', idleMin: 'y' }) }), '10')
  assert.equal(run(['10'], { state: alive({ lastTick: 'a b', unanswered: 'x y', nextDue: 'z' }) }), '10')
  assert.equal(run(['10'], { state: mod(60, { lastTick: (NOW - 600) * 1000 }) }), '10')  // dead mod's nextDue ignored
})

test('mod alive: follows its backoff threshold, never below cron\'s own', () => {
  assert.equal(run(['10'], { state: alive({ idleMin: 40 }) }), '40')
  assert.equal(run(['10'], { state: alive({ idleMin: 120 }) }), '120')
  assert.equal(run(['10'], { state: alive({ idleMin: 5 }) }), '10')
  assert.equal(run(['10'], { state: alive({ idleMin: 60.0 }) }), '60')
})

test('mod alive with its beat unanswered: no beat, but a stuck dialog still reaches the real script\'s alert', () => {
  assert.equal(run(['10'], { state: alive({ unanswered: true, idleMin: 20 }) }), null)
  assert.equal(run(['10'], { state: alive({ unanswered: true, idleMin: 20 }), status: 'busy' }), null)
  assert.equal(run(['10'], { state: alive({ unanswered: true, idleMin: 20 }), status: 'waiting' }), '20')
  assert.equal(run(['10'], { state: alive({ unanswered: true, lastTick: (NOW - 600) * 1000 }) }), '10')   // mod dead
  assert.equal(run(['10'], { state: mod(0, { unanswered: true, nextDue: null, lastBeat: (NOW - 3600) * 1000 }) }), null)
})

test('mod alive, its next beat not overdue: cron leaves it to the mod (the 07:50 race), alert still possible', () => {
  assert.equal(run(['10'], { state: mod(2), transcript: 600 }), null)       // 2026-10-13 07:50:00: mod due 07:50:02
  assert.equal(run(['10'], { state: mod(-60), transcript: 660 }), null)     // just past due: cron window delays the mod
  assert.equal(run(['10'], { state: mod(-179), transcript: 780 }), null)
  assert.equal(run(['10'], { state: mod(1800, { idleMin: 40 }), transcript: 600 }), null)
  assert.equal(run(['10'], { state: mod(2), status: 'waiting' }), '10')      // stuck on a dialog: the real script alerts
  assert.equal(run(['10'], { state: mod(2, { idleMin: 40 }), status: 'waiting' }), '40')
})

test('mod alive but overdue or failing: the real script takes over', () => {
  assert.equal(run(['10'], { state: mod(-180), transcript: 790 }), '10')    // 3 min past nextDue
  assert.equal(run(['10'], { state: mod(-900, { idleMin: 20 }), transcript: 1500 }), '20')
  // its last beat (3 min ago) started no turn: no transcript written since
  assert.equal(run(['10'], { state: mod(120, { lastBeat: (NOW - 180) * 1000 }), transcript: 900 }), '10')
  assert.equal(run(['10'], { state: mod(120, { lastBeat: (NOW - 60) * 1000 }), transcript: 900 }), null)   // < 2 min: wait
  assert.equal(run(['10'], { state: mod(120, { lastBeat: (NOW - 180) * 1000 }), transcript: 100 }), null)  // it landed
  assert.equal(run(['10'], { state: mod(120, { lastBeat: (NOW - 180) * 1000 }), transcript: null }), '10') // only old files
})

test('inbox: a note the mod should have woken for 5+ min ago goes to the real script; fresh ones stay with the mod', () => {
  const old = { lastBeat: (NOW - 1200) * 1000 }   // its last beat, 20 min ago, landed (transcript 60 s old)
  assert.equal(run(['10'], { state: mod(600, old), notes: [120] }), null)                // fresh: the mod wakes at 2 min idle
  assert.equal(run(['10'], { state: mod(600, old), notes: [400] }), '10')                // waited 6+ min: mod missed it
  assert.equal(run(['10'], { state: mod(600, old), notes: [400], marker: 300 }), null)   // a beat (any side) came after it
  assert.equal(run(['10'], { state: mod(600, { ...old, lastBeat: (NOW - 200) * 1000 }), notes: [400], transcript: 100 }), null)
  assert.equal(run(['10'], { state: mod(600, { ...old, started: (NOW - 100) * 1000 }), notes: [400] }), null)   // before mod start
  assert.equal(run(['10'], { state: mod(600, old), notes: [400], marker: null }), '10')  // no marker: like the real script
  assert.equal(run(['10'], { state: mod(600, { ...old, lastSkip: (NOW - 100) * 1000 }), notes: [400] }), null)   // 0.5.0: gate skipped it
  assert.equal(run(['10'], { state: mod(600, { ...old, lastSkip: (NOW - 500) * 1000 }), notes: [400] }), '10')   // skip before the note
})

test('never runs itself, and fails loudly without a real heartbeat', () => {
  assert.equal(run(['10'], { real: 'self' }), 1)
  assert.equal(run(['10'], { real: '/nonexistent/heartbeat' }), 1)
})
