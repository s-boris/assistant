// heartbeat: in-process heartbeat for the main session. Replaces typing into tmux; the cron heartbeat
// (/etc/cron.d/heartbeat: `heartbeat 10` every 5 min) stays as the automatic fallback.
//
// Submits one "[heartbeat] ..." prompt once the main session has been idle HEARTBEAT_MINUTES (10) min,
// or 2 min when new notes landed in ~/inbox. Idle means: no main turn running, none started or ended
// within the threshold, and no transcript in the project dir (main or subagent) written within it,
// which is the cron heartbeat's own test. $.prompt.submit only starts a turn once the session is idle,
// so it never answers a permission prompt or dialog, and the mod doesn't even call it during a turn.
//
// No double beats with cron:
//  - it never fires in cron's run window, :x4:50 to :x5:45 (x5 = minutes divisible by 5; cron is done
//    within ~2 s of :x5:00);
//  - a beat from either side starts a turn: that writes the transcript (cron then skips for 10 min)
//    and raises turn.start here (the mod then waits a full threshold again);
//  - both use /tmp/heartbeat as "last beat sent" (cron, ~/bin/restart's waker and this mod touch it),
//    and the mod keeps >= 5 min after any beat.
//
// Loads only in the main session: enabled solely through start-claude's --settings, and inert unless
// the session is interactive and runs in ~/workspace.
// Idle backoff (0.2.0, 2026-10-13; the owner got ~15 empty beats a night): a heartbeat turn is empty when its
// beat had no new notes and the turn ended with an answer after at most QUICK_TOOLS tool calls, none of them
// real work (Agent/Task, a Telegram reply), with no other prompt or delivery arriving meanwhile. From the
// BACKOFF_AFTER-th empty beat in a row the plain idle threshold doubles per empty beat (10, 10, 20, 40, ...)
// up to 60 min, 120 min in quiet hours (00:00-08:00 owner time zone), never below HEARTBEAT_MINUTES. Back to
// the base at once on any other prompt (owner, Telegram, task notification), a non-empty heartbeat turn, or
// subagent transcript activity since the last heartbeat turn; new inbox notes still wake after INBOX_MIN.
// No new beat while the last one is unanswered (submitted, but no main turn has ended since: e.g. it sits
// queued behind a dialog), for up to UNANSWERED_MAX_MIN.
// The cron fallback follows the backoff through the state file: ~/.local/bin/heartbeat (ops/heartbeat-cron)
// shadows /usr/local/bin/heartbeat and, while lastTick is < 3 min old, uses idleMin, skips while unanswered and
// until nextDue + 3 min (else it raced this mod's cron-window deferral), unless this mod's beats stop landing.
// Status for humans and scripts: ~/.local/state/heartbeat-mod.json (lastTick about once a minute, beats,
// streak, idleMin = current plain threshold, capMin, unanswered, nextDue).
// Gate (0.3.0, 2026-10-14, owner OK; Hermes "wakeAgent:false"): once a beat is due, the mod first asks the token-free
// ~/bin/hb-gate (source ops/hb-gate) `wake` or `skip`, passing the new notes it counted, its last beat, --first for its
// first beat in this session and the background agents from $.agent.list() (running; finished since the last consult
// or main turn end, since a task notification already starts a turn). The gate wakes for inbox notes, a Due item whose
// time has come, a finished agent, the first beat, >= 3 h since the last beat, or no agent running with startable work;
// it skips while an agent is running (the common wasted beat) or nothing is startable. A skip sends nothing: it counts
// in the state file (skips, lastSkip, lastGate), never in the backoff (streak unchanged: no turn ran, so it says
// nothing about whether beats are empty), and restarts the plain idle clock, so the next check comes a full idle
// threshold later and nextDue moves with it (the cron fallback keeps holding). New notes still wake after INBOX_MIN.
// Fails open: a gate error, timeout or unreadable answer means wake (and lastError). HEARTBEAT_GATE=off disables it.
// Board lint (0.4.0): right before a beat goes out, the mod runs `~/bin/board-lint --beat` (token-free, < 1 s) and puts
// its one "[board-lint] ..." line (stale board items not shown in the last 24 h) on a second line of the beat. No
// line, an error, a timeout or anything else means the beat goes out unchanged. HEARTBEAT_LINT=off disables it.
// Idle compaction (0.5.0, v2 Phase 0 D-b): right before an idle beat goes out (after the gate said wake and the lint ran),
// the mod compacts the conversation when ~/.config/heartbeat/compact.json (read every tick; missing or unreadable = off;
// keys enabled, min_tokens, owner_idle_min, gap_min, with_notes, instructions) says enabled and: the beat has no new notes
// (unless with_notes), the owner has been idle owner_idle_min (15) min, the last compaction is gap_min (45) min old (any
// trigger: the session.compact observer also records /compact and the engine's auto compaction), and $.session.usage()
// reports >= min_tokens (160000) context tokens. $.session.compact() races a 10-min timeout ($.clock.sleep); every
// outcome (ok with tokens before/after, skip, timeout, error; unsupported = a host without the calls: off for the session)
// goes to ~/.local/state/heartbeat-compact.jsonl (last 200 lines). A failed try waits gap_min too. Whatever happens, the
// beat then goes out unchanged (an ok adds one "[compact] ..." line). Owner input = a prompt whose origin is composer,
// bridge, channel, slack-ping or auto-continuation (or that has none) and that isn't a beat; the end of its turn is
// lastOwner, which starts at session start; lastCompact survives restarts through the state file.
// Skip memory (0.5.0): a note older than the gate's last skip is no longer new (the gate saw it and said skip, e.g. a
// digest/mute note under the brain's wake rules), so such a note no longer re-asks the gate every 15 s.
// Now lane (0.5.0): while background agents keep the session from looking idle (their transcripts written within
// INBOX_MIN) but the main loop itself has been idle INBOX_MIN and a note newer than the lane's last look exists, the mod
// asks `hb-gate --json --now-only` once per new note and beats only on a wake whose reason starts with "now:" (a
// now-class note under the wake rules). No lint, compaction or lastSkip there. HEARTBEAT_NOW_LANE=off disables it.
// HEARTBEAT_MOD_TEST_DIR (tests only, see test/): a minute becomes 6 s, inbox/marker/state move into
// that dir, any cwd is accepted, the cron-window guard is off and the prompt asks for a one-word reply.

import { homedir } from 'node:os'

const HOME = homedir()   // the bot user's home (no fixed layout: QUICKSTART installs into any home)
export const INBOX_MIN = 2, GAP_MIN = 5
export const BACKOFF_AFTER = 2, CAP_DAY_MIN = 60, CAP_NIGHT_MIN = 120, QUIET_END_H = 8
export const QUICK_TOOLS = 2, UNANSWERED_MAX_MIN = 120
export const GATE = `${HOME}/bin/hb-gate`, GATE_TIMEOUT_MS = 15_000
export const LINT = `${HOME}/bin/board-lint`, LINT_TIMEOUT_MS = 4_000
export const PROFILE_GET = `${HOME}/bin/profile-get`, DEFAULT_TZ = 'Europe/Zurich'
export const VERSION = '0.5.0'
export const COMPACT_DEFAULTS = Object.freeze({
  enabled: false, min_tokens: 160_000, owner_idle_min: 15, gap_min: 45, with_notes: false, instructions: '' })
export const COMPACT_FLOORS = Object.freeze({ min_tokens: 20_000, owner_idle_min: 0, gap_min: 10 })   // typo guards
export const COMPACT_TIMEOUT_MIN = 10, COMPACT_LOG_KEEP = 200
export const COMPACT_TIMEOUT = Symbol('compact timeout')
const OWNER_ORIGINS = new Set(['composer', 'bridge', 'channel', 'slack-ping', 'auto-continuation'])
const AGENT_ACTIVE = new Set(['pending', 'running', 'waiting'])
const WORK_TOOL = /^(Agent|Task)$|__(reply|send_poll|send_location)$/   // never "just a quick check"
// A beat: "[heartbeat]" early in the first line (cron and the restart waker type it bare; prompt.submit hooks see the
// mod's own bare too), or in the line after the host's frame, which is what turn.start gets for the mod's beat
// (Claude Code 2.1.296: "The heartbeat plugin sent a message:\n[heartbeat] ...\n\nThis is how Claude Code surfaces
// ..."). 0.2.0 expected frame and beat on one line, so no mod beat ever counted and the backoff never engaged.
const BEAT_TEXT = /^(?:[^\n]{0,40}\bheartbeat plugin\b[^\n]{0,60}\n+)?[^\n]{0,80}\[heartbeat\]/

const MAIN_CWD = `${HOME}/workspace`
const PROJECTS = `${HOME}/.claude/projects`
const PROD = {
  minute: 60_000, guard: true, test: false,
  inbox: `${HOME}/inbox`, marker: '/tmp/heartbeat', state: `${HOME}/.local/state/heartbeat-mod.json`,
  compactCfg: `${HOME}/.config/heartbeat/compact.json`, compactLog: `${HOME}/.local/state/heartbeat-compact.jsonl`,
}

// Cron starts `heartbeat` at second 0 of every minute divisible by 5. UTC minutes are fine: every real
// time zone offset is a multiple of 5 minutes.
export function inCronWindow(ms) {
  const d = new Date(ms), m = d.getUTCMinutes() % 5, s = d.getUTCSeconds()
  return (m === 0 && s < 45) || (m === 4 && s >= 50)
}

// The whole decision, pure (unit-tested in test/logic.test.mjs). s: busy, pending, lastActivity (last main
// turn start/end), lastBeat (this mod's), marker (mtime of /tmp/heartbeat), notes (new inbox notes),
// newest (newest transcript mtime), lastSkip (the gate's last skip: restarts the plain idle clock). Times in ms.
// streak (empty heartbeat turns in a row) and unanswered (last beat not answered yet) drive the backoff.
export function decide(s, now, cfg) {
  if (s.busy) return { fire: false, why: 'turn running' }
  if (s.pending) return { fire: false, why: 'beat pending' }
  if (s.unanswered && now - s.lastBeat < UNANSWERED_MAX_MIN * cfg.minute) return { fire: false, why: 'unanswered' }
  if (cfg.guard && inCronWindow(now)) return { fire: false, why: 'cron window' }
  if (now - Math.max(s.lastBeat, s.marker) < GAP_MIN * cfg.minute) return { fire: false, why: 'gap' }
  const idleMin = s.notes > 0 ? INBOX_MIN : plainIdleMin(s.streak || 0, now, cfg.baseMin)
  if (now - Math.max(s.lastActivity, s.newest) < idleMin * cfg.minute) return { fire: false, why: 'not idle' }
  if (s.notes === 0 && now - (s.lastSkip || 0) < idleMin * cfg.minute) return { fire: false, why: 'gate skipped' }
  return { fire: true, idleMin, notes: s.notes }
}

// The owner's zone (profile locale.tz through PROFILE_GET, read at session start), else DEFAULT_TZ.
let ownerTz = DEFAULT_TZ, hourFmt = null
const validZone = z => {
  if (typeof z !== 'string' || !z.trim()) return false
  try { new Intl.DateTimeFormat('en-GB', { timeZone: z.trim() }); return true } catch { return false }
}
export function setOwnerZone(z) {
  const next = validZone(z) ? z.trim() : DEFAULT_TZ
  if (next !== ownerTz) { ownerTz = next; hourFmt = null }
  return ownerTz
}
async function loadOwnerZone($) {
  try {
    const r = await $.process.run([PROFILE_GET, 'locale.tz'], { timeoutMs: 5_000 })
    setOwnerZone(r && r.exitCode === 0 ? String(r.stdout ?? '') : '')
  } catch { setOwnerZone('') }
}
export function ownerHour(ms) {
  try {
    hourFmt ??= new Intl.DateTimeFormat('en-GB', { timeZone: ownerTz, hour: 'numeric', hourCycle: 'h23' })
    return Number(hourFmt.format(ms)) % 24
  } catch { return new Date(ms).getHours() }   // the container's TZ is the owner's zone too
}

export const capMin = now => (ownerHour(now) < QUIET_END_H ? CAP_NIGHT_MIN : CAP_DAY_MIN)

// The idle threshold without new notes after `streak` empty heartbeat turns in a row.
export function plainIdleMin(streak, now, base) {
  const steps = Math.min(10, Math.max(0, streak - BACKOFF_AFTER + 1))
  return Math.max(base, Math.min(capMin(now), base * 2 ** steps))
}

// Turn bookkeeping, pure on a state object (the mod's own `st`, or a test's).
export const isBeatText = text => BEAT_TEXT.test(String(text ?? ''))
export function beatNotes(text) {   // -1 = not a routine beat (e.g. the restart waker's): never empty
  const m = /\b(\d+) new note\(s\)/.exec(String(text ?? ''))
  return m ? Number(m[1]) : -1
}
// Returns true when the turn counts as a beat only through prompt.submit (a beat entered while idle, but turn.start's
// text no longer looks like one): the host's frame changed and BEAT_TEXT needs a look; the hook logs it.
export function turnStarted(s, text, now) {
  s.busy = true
  s.lastActivity = now
  const known = isBeatText(text), drift = !known && s.beatIn != null
  if (known || drift) { s.hb = { notes: known ? beatNotes(text) : s.beatIn, tools: 0, work: false, input: false }; s.hbStart = now }
  else { s.hb = null; s.streak = 0 }   // the owner, Telegram, a task notification, a continuation: news
  s.beatIn = null
  return drift
}
export function toolCalled(s, tool) {   // main loop only
  if (!s.hb) return
  s.hb.tools++
  if (WORK_TOOL.test(String(tool))) s.hb.work = true
}
export function inputArrived(s, text, midTurn = false) {   // prompt.submit: any prompt or delivery, also one queued mid-turn
  if (isBeatText(text)) { if (!midTurn) s.beatIn = beatNotes(text); return }   // entered while idle: its turn.start is next
  s.beatIn = null
  s.streak = 0
  if (s.hb) s.hb.input = true
}
export const emptyBeat = (hb, reason) =>
  reason === 'answer' && hb.notes === 0 && !hb.work && !hb.input && hb.tools <= QUICK_TOOLS
export function turnEnded(s, reason, now) {
  s.busy = false
  s.lastActivity = now
  s.unanswered = false   // the beat was consumed, also by an API error (else one error would hold inbox wakes for hours)
  if (s.ownerTurn) { s.lastOwner = Math.max(s.lastOwner || 0, now); s.ownerTurn = false }   // 0.5.0: owner idle clock
  if (s.hb) {
    if (emptyBeat(s.hb, reason)) s.streak = (s.streak || 0) + 1
    else if (reason === 'answer' || reason === 'aborted') s.streak = 0   // an API error or refusal says nothing
    s.hb = null
  }
}
export function subagentsWrote(s, sub) {   // background work since the last heartbeat turn started
  if (sub > (s.hbStart || 0)) s.streak = 0
}

// --- 0.5.0: owner idle, idle compaction, now lane (pure) ---------------------------------------------------------------
// Owner input: a prompt from him (terminal, Remote Control bridge, a channel such as Telegram, a Slack ping, the
// continuation of his UI action) or one without an origin (an older host). A beat never counts, whoever typed it.
export function isOwnerInput(text, origin) {
  if (isBeatText(text)) return false
  const kind = origin && typeof origin === 'object' && origin.kind != null ? String(origin.kind) : ''
  return !kind || OWNER_ORIGINS.has(kind)
}
export function ownerInput(s, text, origin, now) {   // prompt.submit; the turn's end (turnEnded) moves lastOwner again
  if (!isOwnerInput(text, origin)) return false
  s.ownerTurn = true
  s.lastOwner = Math.max(s.lastOwner || 0, now)
  return true
}

// compact.json's text -> the knob with defaults filled in; unreadable JSON or a non-object = the defaults (off).
// Numbers must be JSON numbers; floors (COMPACT_FLOORS) keep a typo from compacting every beat.
export function compactConfig(text) {
  let j = null
  try { j = JSON.parse(String(text ?? '')) } catch {}
  if (!j || typeof j !== 'object' || Array.isArray(j)) return { ...COMPACT_DEFAULTS }
  const num = k => (typeof j[k] === 'number' && Number.isFinite(j[k]) ? Math.max(COMPACT_FLOORS[k], j[k]) : COMPACT_DEFAULTS[k])
  return {
    enabled: j.enabled === true, min_tokens: num('min_tokens'), owner_idle_min: num('owner_idle_min'),
    gap_min: num('gap_min'), with_notes: j.with_notes === true,
    instructions: typeof j.instructions === 'string' ? j.instructions.trim().slice(0, 2000) : '',
  }
}
// Why an idle beat does NOT compact first, before any token count is read ('' = read it). s: lastOwner, lastCompact,
// lastCompactTry (a failed, skipped or timed-out try), compactUnsupported.
export function compactBlocked(c, s, now, notes, minute) {
  if (!c || !c.enabled) return 'off'
  if (s.compactUnsupported) return 'unsupported'
  if (notes > 0 && !c.with_notes) return 'notes'
  if (now - (s.lastOwner || 0) < c.owner_idle_min * minute) return 'owner active'
  if (now - Math.max(s.lastCompact || 0, s.lastCompactTry || 0) < c.gap_min * minute) return 'gap'
  return ''
}
// $.session.usage()'s answer -> context tokens, NaN when absent (right after a compaction, until the next response).
export function compactTokens(u) {
  const n = u && u.context && u.context.tokens != null ? Number(u.context.tokens) : NaN
  return Number.isFinite(n) ? n : NaN
}
const k = n => `${Math.round(n / 1000)}k`
export function compactDue(c, s, now, tokens, notes, minute) {
  const blocked = compactBlocked(c, s, now, notes, minute)
  if (blocked) return { due: false, why: blocked }
  if (!Number.isFinite(tokens)) return { due: false, why: 'no token count' }
  if (tokens < c.min_tokens) return { due: false, why: `small: ${k(tokens)} < ${k(c.min_tokens)}` }
  return { due: true, why: `${k(tokens)} >= ${k(c.min_tokens)}` }
}
// $.session.compact()'s answer, the timeout, or what it (or $.session.usage()) threw -> the log row's outcome.
// A TypeError is a host without the calls (feature detection): unsupported.
export function compactOutcome(r, threw = false, err = undefined) {
  const msg = x => String(x).slice(0, 160)
  if (threw) return err instanceof TypeError ? { outcome: 'unsupported', error: msg(err) } : { outcome: 'error', error: msg(err) }
  if (r === COMPACT_TIMEOUT) return { outcome: 'timeout' }
  if (!r || typeof r !== 'object') return { outcome: 'error', error: 'no answer' }
  if (r.skip) return { outcome: 'skip', error: msg(r.skip) }
  const n = v => (typeof v === 'number' && Number.isFinite(v) ? Math.round(v) : null)
  return { outcome: 'ok', before: n(r.tokensBefore), after: n(r.tokensAfter) }
}
// Newest mtime among the inbox entries that count as new (0: none).
export const newestFile = (entries, since) =>
  entries.reduce((m, f) => (f.kind === 'file' && f.mtimeMs > since && f.mtimeMs > m ? f.mtimeMs : m), 0)
// The now lane: decide() said 'not idle' (with new notes its threshold is INBOX_MIN), the main loop's own last turn or
// transcript write is INBOX_MIN old (so subagents are what keeps it from idling), and a note is newer than the last look.
export const nowLaneDue = (d, notes, newestNote, seen, mainIdleMs, minute) =>
  Boolean(d) && d.why === 'not idle' && notes > 0 && newestNote > seen && mainIdleMs >= INBOX_MIN * minute

// The gate's answer ($.process.run's result): skip only on exit 0 with a JSON line saying so; anything else wakes.
// board-lint's answer: its one "[board-lint] ..." line on exit 0, else '' (a lint problem never changes a beat).
export function lintLine(r) {
  try {
    if (!r || r.exitCode !== 0) return ''
    const line = String(r.stdout ?? '').trim().split('\n')[0] || ''
    return line.startsWith('[board-lint]') && !/\bnew note\(s\)/.test(line) ? line.slice(0, 500) : ''
  } catch { return '' }
}
export function gateVerdict(r) {
  try {
    if (!r || r.exitCode !== 0) return { skip: false, reason: `gate error: exit ${r ? r.exitCode : 'none'}` }
    const j = JSON.parse(String(r.stdout ?? '').trim().split('\n').pop())
    const reason = String(j.reason ?? '').slice(0, 160)
    return j.decision === 'skip' ? { skip: true, reason } : { skip: false, reason: reason || 'wake' }
  } catch (err) { return { skip: false, reason: 'gate error: ' + String(err).slice(0, 120) } }
}

// Background agents from $.agent.list() against the last snapshot (id -> active): running ids, and the ones that
// finished since (were active, or new and already done; an active one that left the list ended too). snap null =
// no snapshot yet: nothing counts as finished.
export function agentDelta(snap, list) {
  const next = {}, running = [], finished = []
  for (const a of list || []) {
    if (!a || !a.id) continue
    const active = AGENT_ACTIVE.has(a.status)
    next[a.id] = active
    if (active) running.push(a.id)
    else if (snap && a.status !== 'idle' && (snap[a.id] === true || !(a.id in snap))) finished.push(a.id)
  }
  if (snap) for (const [id, active] of Object.entries(snap)) if (active && !(id in next)) finished.push(id)
  return { running, finished, snap: next }
}

// Inbox entries that count as new: regular files modified after `since` (like cron's find -newer).
export const countNew = (entries, since) => entries.filter(f => f.kind === 'file' && f.mtimeMs > since).length

export const projectDir = cwd => `${PROJECTS}/${cwd.replace(/[^A-Za-z0-9]/g, '-')}`

const FRESH = () => ({ lastOwner: 0, ownerTurn: false, lastCompact: 0, lastCompactTry: 0, compactUnsupported: false,
  compacting: 0, compactWhy: '', compactCfg: { ...COMPACT_DEFAULTS }, compacts: 0,
  nowLaneSeen: 0, laneAsks: 0, laneBeats: 0, lastLane: '' })
const st = { busy: false, pending: false, lastActivity: 0, lastBeat: 0, started: 0,
  streak: 0, unanswered: false, hb: null, hbStart: 0, beatIn: null,
  lastSkip: 0, sessionBeats: 0, agentSnap: null, lastGate: '', ...FRESH() }
let cfg = null, timer = null, ticking = false, ticks = 0, beats = 0, skips = 0, lastError = '', lastShown = ''

const logErr = ($, msg) => { lastError = msg; try { $.ui.log(msg) } catch {} }

async function mtime($, path) {
  try { return (await $.fs.stat(path)).mtimeMs || 0 } catch { return 0 }
}

// Newest mtimes of the current main transcript and of its subagents' transcripts.
async function newestTranscripts($) {
  const t = { main: 0, sub: 0 }
  try {
    const top = (await $.fs.list(cfg.project)).filter(f => f.kind === 'file' && f.name.endsWith('.jsonl'))
    if (!top.length) return t
    const cur = top.reduce((a, b) => (b.mtimeMs > a.mtimeMs ? b : a))
    t.main = cur.mtimeMs
    try {
      for (const f of await $.fs.list(`${cfg.project}/${cur.name.slice(0, -6)}/subagents`))
        if (f.kind === 'file' && f.name.endsWith('.jsonl') && f.mtimeMs > t.sub) t.sub = f.mtimeMs
    } catch {}   // no subagents yet
  } catch (err) {   // ENOENT: no transcript written yet
    if (!String(err).includes('ENOENT')) logErr($, 'transcript check failed: ' + err)
  }
  return t
}

// null when the host has no agent.list, it throws, or it answers no list: the gate then asks agent-watch.
// Spelled as a plain call on purpose: the host's loader (CC 2.1.296+, also `claude plugin validate`) rejects the
// WHOLE module when a $ noun is read as a value (`typeof $.agent?.list` did that, so 0.3.0 never loaded).
// A host without the noun throws a TypeError here, which the catch turns into the fallback.
async function listAgents($) {
  try {
    const list = await $.agent.list()
    return Array.isArray(list) ? list : null
  } catch { return null }
}
async function refreshAgents($) {   // after a main turn: finishes it was told about (task notifications) are not news
  const list = await listAgents($)
  if (list) st.agentSnap = agentDelta(st.agentSnap, list).snap
}

async function consultGate($, d, marker) {
  const args = ['--json', '--notes', String(d.notes), '--last-beat', String(Math.max(st.lastBeat, marker))]
  if (!st.sessionBeats) args.push('--first')
  const list = await listAgents($)
  if (list) {
    const a = agentDelta(st.agentSnap, list)
    st.agentSnap = a.snap
    args.push('--running', String(a.running.length), '--finished', String(a.finished.length))
    if (a.running.length) args.push('--ids', a.running.join(','))
  }
  try { return gateVerdict(await $.process.run([cfg.gate, ...args], { timeoutMs: GATE_TIMEOUT_MS })) }
  catch (err) { return { skip: false, reason: 'gate error: ' + String(err).slice(0, 120) } }
}

async function consultLint($) {
  try { return lintLine(await $.process.run([cfg.lint, '--beat'], { timeoutMs: LINT_TIMEOUT_MS })) }
  catch { return '' }
}

async function writeState($, now) {
  try {
    const idleMin = plainIdleMin(st.streak, now, cfg.baseMin)
    const unanswered = st.unanswered && now - st.lastBeat < UNANSWERED_MAX_MIN * cfg.minute
    await $.fs.write(cfg.state, JSON.stringify({
      started: st.started, lastTick: now, lastBeat: st.lastBeat, beats, baseMin: cfg.baseMin, lastError,
      streak: st.streak, idleMin, capMin: capMin(now), unanswered,
      // Earliest next plain beat (ms; a later main/subagent transcript write or a new note moves it), null while held.
      // A gate skip restarts the idle clock, so the cron fallback keeps holding while the gate skips.
      // While a compaction runs (at most COMPACT_TIMEOUT_MIN), the beat after it is due at the timeout at the latest.
      nextDue: unanswered ? null
        : st.compacting ? st.compacting + COMPACT_TIMEOUT_MIN * cfg.minute
        : Math.max(Math.max(st.lastActivity, st.lastSkip) + idleMin * cfg.minute, st.lastBeat + GAP_MIN * cfg.minute),
      gate: Boolean(cfg.gate), skips, lastSkip: st.lastSkip, lastGate: st.lastGate,
      version: VERSION, lastOwner: st.lastOwner, lastCompact: st.lastCompact, compacting: st.compacting || null,
      compact: { enabled: st.compactCfg.enabled, min_tokens: st.compactCfg.min_tokens, owner_idle_min: st.compactCfg.owner_idle_min,
        gap_min: st.compactCfg.gap_min, with_notes: st.compactCfg.with_notes, last: st.compactWhy, runs: st.compacts,
        lastTry: st.lastCompactTry, unsupported: st.compactUnsupported },
      nowLane: { on: Boolean(cfg.gate && cfg.nowLane), asks: st.laneAsks, beats: st.laneBeats, last: st.lastLane },
    }) + '\n')
  } catch {}
}

// One JSONL row per compaction (the mod's own tries and, via the observer, every other one), the last COMPACT_LOG_KEEP.
async function logCompact($, row) {
  let lines = []
  try { lines = String(await $.fs.read(cfg.compactLog)).split('\n').filter(Boolean) } catch {}   // none yet
  lines.push(JSON.stringify(row))
  try { await $.fs.write(cfg.compactLog, lines.slice(-COMPACT_LOG_KEEP).join('\n') + '\n') } catch {}
  try {
    $.ui.log(`heartbeat: compaction (${row.trigger}) ${row.outcome}` + (row.before ? ` ${k(row.before)}` : '') +
      (row.after ? ` -> ${k(row.after)}` : '') + (row.error ? `: ${row.error}` : ''))
  } catch {}
}

// D-b: compact before an idle beat (see the header). Never throws. ran: $.session.compact() was called (at = when).
async function maybeCompact($, d, now) {
  const none = { ran: false, at: now, line: '' }
  const c = st.compactCfg
  const blocked = compactBlocked(c, st, now, d.notes, cfg.minute)
  if (blocked) { st.compactWhy = blocked; return none }
  let tokens = NaN
  try { tokens = compactTokens(await $.session.usage()) }
  catch (err) {
    const o = compactOutcome(undefined, true, err)
    if (o.outcome === 'unsupported') st.compactUnsupported = true
    else st.lastCompactTry = now
    st.compactWhy = o.outcome
    await logCompact($, { ts: now, trigger: 'mod', step: 'usage', ...o })
    return none
  }
  const due = compactDue(c, st, now, tokens, d.notes, cfg.minute)
  st.compactWhy = due.why
  if (!due.due) return none
  const at = await $.clock.now()
  st.compacting = at
  st.compacts++
  await writeState($, at)
  let r, threw = false, err
  const ac = new AbortController()
  try {
    const timeout = $.clock.sleep(COMPACT_TIMEOUT_MIN * cfg.minute, { signal: ac.signal }).then(() => COMPACT_TIMEOUT, () => COMPACT_TIMEOUT)
    const p = Promise.resolve(c.instructions ? $.session.compact({ instructions: c.instructions }) : $.session.compact())
    p.catch(() => {})   // a rejection after the timeout won is not unhandled
    r = await Promise.race([p, timeout])
  } catch (e) { threw = true; err = e }
  finally { ac.abort(); st.compacting = 0 }
  const end = await $.clock.now()
  const o = compactOutcome(r, threw, err)
  if (o.outcome === 'ok') st.lastCompact = end
  else if (o.outcome === 'unsupported') st.compactUnsupported = true
  else st.lastCompactTry = end
  st.compactWhy = o.outcome
  const row = { ts: end, trigger: 'mod', ...o, tokens: Math.round(tokens), ms: end - at }
  if (o.outcome === 'ok' && row.before == null) row.before = Math.round(tokens)
  await logCompact($, row)
  const line = o.outcome === 'ok' ? `[compact] idle compaction before this beat: ${k(row.before)}` +
    (row.after != null ? ` -> ${k(row.after)}` : '') + ' context tokens' : ''
  return { ran: true, at, line }
}

// Marks the beat sent and submits it; the submit resolves once the prompt entered the session and its turn started (or
// it was queued behind one).
async function sendBeat($, now, text) {
  st.pending = true
  st.unanswered = true   // until a main turn ends (or the submit is dropped)
  st.lastBeat = now
  st.sessionBeats++
  beats++
  try { await $.fs.write(cfg.marker, '') } catch {}   // cron's "last heartbeat sent" marker
  await writeState($, now)
  Promise.resolve().then(() => $.prompt.submit({ text }))
    .then(r => { if (r && r.drop) { st.unanswered = false; st.beatIn = null; logErr($, 'beat dropped: ' + r.drop) } })
    .catch(err => { st.unanswered = false; st.beatIn = null; logErr($, 'submit failed: ' + err) })
    .finally(() => { st.pending = false })
}

// The now lane (see the header): one `hb-gate --now-only` per newer note; a beat only for a "now:" wake.
async function nowLane($, notes, newestNote, marker) {
  const prev = st.nowLaneSeen
  st.nowLaneSeen = newestNote
  st.laneAsks++
  let g
  try {
    g = gateVerdict(await $.process.run([cfg.gate, '--json', '--now-only', '--notes', String(notes),
      '--last-beat', String(Math.max(st.lastBeat, marker))], { timeoutMs: GATE_TIMEOUT_MS }))
  } catch (err) { g = { skip: true, reason: 'gate error: ' + String(err).slice(0, 120) } }
  st.lastLane = `${g.skip ? 'skip' : 'wake'}: ${g.reason}`.slice(0, 160)
  if (g.skip || !g.reason.startsWith('now:')) return   // a gate error never wakes here: the regular path still runs
  const now = await $.clock.now()
  if (st.busy || st.pending || st.unanswered || (cfg.guard && inCronWindow(now))) { st.nowLaneSeen = prev; return }   // ask again
  st.laneBeats++
  await sendBeat($, now, `[heartbeat] Idle ${INBOX_MIN}+ min, ${notes} new note(s) in ~/inbox (now-class, background agents ` +
    'running). Run your heartbeat routine.' + (cfg.test ? ' TEST ONLY: reply with the single word OK and use no tools.' : ''))
}

async function tick($) {
  if (ticking) {   // a compaction holds its tick for up to COMPACT_TIMEOUT_MIN: keep lastTick fresh for the cron fallback
    if (st.compacting && ++ticks % 4 === 1) { try { await writeState($, await $.clock.now()) } catch {} }
    return
  }
  ticking = true
  try {
    const now = await $.clock.now()
    try { st.compactCfg = compactConfig(await $.fs.read(cfg.compactCfg)) } catch { st.compactCfg = { ...COMPACT_DEFAULTS } }
    // About once a minute, and soon after the backoff changed: shows the mod is alive, feeds the cron fallback.
    const shown = `${st.streak}/${st.unanswered}`
    if (++ticks % 4 === 1 || shown !== lastShown) { lastShown = shown; await writeState($, now) }
    // Cheapest checks first: with the most permissive values for what isn't read yet, can it fire at all?
    if (!decide({ ...st, marker: 0, notes: 1, newest: 0 }, now, cfg).fire) return
    const marker = await mtime($, cfg.marker)
    let notes = 0, newestNote = 0
    try {   // 0.5.0: notes the gate already saw and skipped (st.lastSkip) are not new
      const entries = await $.fs.list(cfg.inbox), since = Math.max(st.started, st.lastBeat, marker, st.lastSkip)
      notes = countNew(entries, since)
      newestNote = newestFile(entries, since)
    } catch (err) { logErr($, 'inbox check failed: ' + err) }
    const t = await newestTranscripts($)
    subagentsWrote(st, t.sub)
    const seen = { marker, notes, newest: Math.max(t.main, t.sub) }
    const d = decide({ ...st, ...seen }, now, cfg)
    if (!d.fire) {
      if (cfg.gate && cfg.nowLane &&
          nowLaneDue(d, notes, newestNote, st.nowLaneSeen, now - Math.max(st.lastActivity, t.main), cfg.minute))
        await nowLane($, notes, newestNote, marker)
      return
    }
    if (cfg.gate) {
      const g = await consultGate($, d, marker)
      st.lastGate = `${g.skip ? 'skip' : 'wake'}: ${g.reason}`
      if (g.reason.startsWith('gate error')) logErr($, g.reason)
      if (!decide({ ...st, ...seen }, await $.clock.now(), cfg).fire) return   // a turn may have started meanwhile
      if (g.skip) {   // nothing is sent; the next check comes a full idle threshold later (see decide)
        st.lastSkip = now
        skips++
        await writeState($, now)
        return
      }
    }
    const lint = cfg.lint ? await consultLint($) : ''
    if (cfg.lint && !decide({ ...st, ...seen }, await $.clock.now(), cfg).fire) return   // a turn may have started meanwhile
    const comp = await maybeCompact($, d, now)
    // After a compaction (minutes, and it writes the transcript): only a turn or his message since then stops the beat.
    if (comp.ran && (st.busy || st.pending || st.lastOwner > comp.at)) return
    const text = `[heartbeat] Idle ${d.idleMin}+ min, ${d.notes} new note(s) in ~/inbox. Run your heartbeat routine.` +
      (cfg.test ? ' TEST ONLY: reply with the single word OK and use no tools.' : '') + (lint ? '\n' + lint : '') +
      (comp.line ? '\n' + comp.line : '')
    await sendBeat($, comp.ran ? await $.clock.now() : now, text)
  } catch (err) {
    logErr($, 'tick failed: ' + err)
  } finally {
    ticking = false
  }
}

export function register(on) {
  on('session.start', async ($, e, next) => {
    try {
      if (timer) { timer.cancel(); timer = null }
      const testDir = await $.env.get('HEARTBEAT_MOD_TEST_DIR')
      const base = testDir
        ? { minute: 6_000, guard: false, test: true, inbox: `${testDir}/inbox`, marker: `${testDir}/marker`, state: `${testDir}/state.json` }
        : PROD
      if (e.isInteractive && (testDir || e.cwd === MAIN_CWD)) {
        if (!testDir) await loadOwnerZone($)   // tests keep DEFAULT_TZ (and an exact count of $.process runs)
        const gate = (await $.env.get('HEARTBEAT_GATE')) || (testDir ? 'off' : GATE)   // tests opt in
        const lint = (await $.env.get('HEARTBEAT_LINT')) || (testDir ? 'off' : LINT)   // tests opt in
        const lane = (await $.env.get('HEARTBEAT_NOW_LANE')) || 'on'
        cfg = { ...base, project: projectDir(e.cwd), gate: gate === 'off' ? null : gate, lint: lint === 'off' ? null : lint,
          baseMin: Math.max(3, Number(await $.env.get('HEARTBEAT_MINUTES')) || 10), nowLane: lane !== 'off',
          ...(testDir ? { compactCfg: `${testDir}/compact.json`, compactLog: `${testDir}/compact.jsonl` } : {}) }
        const now = await $.clock.now()
        let lastCompact = 0   // the previous run's, so a restart doesn't compact again within gap_min
        try {
          const prev = JSON.parse(String(await $.fs.read(cfg.state)))
          if (prev && typeof prev.lastCompact === 'number' && prev.lastCompact <= now) lastCompact = prev.lastCompact
        } catch {}   // no state file yet
        Object.assign(st, { busy: false, pending: false, lastActivity: now, started: now,
          streak: 0, unanswered: false, hb: null, hbStart: 0, beatIn: null,
          lastSkip: 0, sessionBeats: 0, agentSnap: null, lastGate: '', ...FRESH(), lastOwner: now, lastCompact })
        await writeState($, now)
        timer = $.clock.every(cfg.minute / 4, () => tick($))
      }
    } catch (err) { logErr($, 'start failed: ' + err) }
    return next(e)
  })
  // 0.5.0: records every main-conversation compaction it didn't start itself (/compact, the engine's auto, another
  // plugin's): its own call skips this hook. A skip or a precompute installs nothing. Observe only: the result passes on.
  on('session.compact', async ($, e, next) => {
    const r = await next(e)
    try {
      if (cfg && !e.agentId && e.trigger !== 'precompute' && r && !r.skip) {
        const now = await $.clock.now(), o = compactOutcome(r)
        st.lastCompact = Math.max(st.lastCompact, now)
        logCompact($, { ts: now, trigger: String(e.trigger ?? ''), ...o }).catch(() => {})
      }
    } catch {}
    return r
  })
  on('session.end', async ($, e, next) => {
    if (timer) { timer.cancel(); timer = null }
    return next(e)
  })
  // The bookkeeping hooks only observe: every one passes its event on unchanged, whatever happens.
  // turn.start's text is the user message as the model reads it: the mod's own beat arrives framed (see BEAT_TEXT).
  on('turn.start', async ($, e, next) => {
    if (e.agentId) return next(e)   // CC >= 2.1.299 raises turn.start for subagent turns too; only the main loop counts
    try {
      if (turnStarted(st, e.text, await $.clock.now()))
        logErr($, 'beat counted via prompt.submit; turn.start text not recognized (host frame changed?): ' +
          JSON.stringify(String(e.text ?? '').slice(0, 60)))
    } catch { st.busy = true }
    return next(e)
  })
  on('turn.complete', async ($, e, next) => {
    if (!e.agentId) {   // a subagent's turns carry agentId; their transcripts count via newestTranscripts
      try { turnEnded(st, e.reason, await $.clock.now()) } catch { st.busy = false }
      if (cfg?.gate) refreshAgents($).catch(() => {})   // not awaited: the turn's end never waits on it
    }
    return next(e)
  })
  on('tool.call', async ($, e, next) => {
    try { if (!e.agentId) toolCalled(st, e.tool) } catch {}
    return next(e)
  })
  // prompt.submit sees every prompt bare (the host frames a plugin's prompt after these hooks), the mod's own beat
  // included (origin plugin "heartbeat", no turnId: it enters once idle and its turn.start follows).
  on('prompt.submit', async ($, e, next) => {
    try { inputArrived(st, e.text, Boolean(e.turnId)) } catch {}
    try { if (cfg) ownerInput(st, e.text, e.origin, await $.clock.now()) } catch {}
    return next(e)
  })
}
