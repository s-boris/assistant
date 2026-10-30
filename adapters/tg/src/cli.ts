#!/usr/bin/env bun
// tg-adapter CLI. Every command takes --instance test|live (default test; paths.ts):
//   tg-adapter run                     interlocks, then poll (foreground; SIGTERM/SIGINT stop it)
//   tg-adapter check                   validate config + token file interlocks (no network)
//   tg-adapter status [--json]         offset, PID, row counts, callback ack latency
//   tg-adapter enqueue OP --chat ID --body JSON [--idem KEY] [--not-before EPOCH_S] [--mode live|shadow] [--urgent]
//                                      queue one outbox row (OP: see outbox.ts OPS; --urgent bypasses
//                                      quiet hours and sleep-in, rules.ts; --files a JSON list of paths,
//                                      --reply-to-event ID marks an answer to that event: never held)
//   tg-adapter bindings [--json]       P3: every binding (tg-bind rows, card binds, the compat tables) with its tool,
//                                      effect, argv and whether it runs (else why: the input goes to the brain)
//   tg-adapter bindings resolve TEXT | --data DATA [--chat ID --message ID] [--json]
//                                      what a command text or a press resolves to now (switch included)
//   tg-adapter bindings menu [--custom FILE] [--diff] [--json]
//                                      P9: the /commands menu set_commands would publish now: the bound commands (tg-bind
//                                      rows with a menu line, then compat entries no row covers) + the brain's own (the
//                                      last set_commands list, or FILE: a JSON list of {command, description});
//                                      --diff: compare with 0.1.22's menu (/status + instant.json + the same custom
//                                      list); exit 1 when they differ
//   tg-adapter bindings compat [--json] P9: the running adapter's compat-table uses (store meta compat_reads:<bot>)
//   tg-adapter companion [--once [--wait S]]   H6: the brain-side companion of a hub-mode adapter (no config, no bot
//                                      token: HUB_URL, HUB_TOKEN_FILE; jobs + presence; companion.ts). Stays down while
//                                      ~/.config/tg-adapter/<instance>.companion-off exists (exit 7).
//   tg-adapter companion status [--json]   its pid and counters
// Exit codes: 0 ok · 1 error · 2 config/usage · 3 409 conflict · 4 401 · 5 wrong bot · 6 already
// running · 7 switch file present.
import { existsSync, readFileSync } from 'fs'
import { join } from 'path'
import { isTestRun } from './paths.ts'
import { Companion, companionHub, companionStatus } from './companion.ts'
import { readHubToken } from './hub.ts'
import { COMPANION_SEEN, FWD_ERR, FWD_ID, FWD_OK } from './hubsync.ts'
import { Adapter, EXIT, PID_MARKER } from './adapter.ts'
import { Binder, type Binding, type CompatReads } from './bind.ts'
import { ConfigError, loadConfig, readToken, type Config } from './config.ts'
import { openDb, Store } from './db.ts'
import { OPS } from './outbox.ts'
import { parseInstance, resolvePaths, switchOn, type Instance, type Paths } from './paths.ts'

const out = (s: string) => process.stdout.write(s + '\n')
const err = (s: string) => process.stderr.write(`tg-adapter: ${s}\n`)

let INSTANCE: Instance = 'test'

function setup(): { paths: Paths; cfg: Config } {
  const paths = resolvePaths(process.env, INSTANCE)
  const cfg = loadConfig(paths.config, paths.home, { instance: INSTANCE, eventsDb: paths.eventsDb, testRun: isTestRun(process.env) })
  return { paths, cfg }
}

const dbPath = (cfg: Config, paths: Paths) => cfg.db ?? (cfg.hub ? join(paths.stateDir, 'journal.db') : paths.defaultDb)

function flag(args: string[], name: string): string | undefined {
  const i = args.indexOf(name)
  return i >= 0 ? args[i + 1] : undefined
}

async function main(argv: string[]): Promise<number> {
  argv = [...argv]
  const ii = argv.indexOf('--instance')
  try { INSTANCE = parseInstance(ii >= 0 ? argv[ii + 1] : undefined) } catch (e) { err((e as Error).message); return EXIT.config }
  if (ii >= 0) argv.splice(ii, 2)
  const [cmd, ...args] = argv
  try {
    switch (cmd) {
      case 'run': {
        const { paths, cfg } = setup()
        const sw = switchOn(paths)
        if (sw) { err(`switch file present (${sw}): staying down`); return EXIT.off }
        const token = readToken(cfg, process.env, paths.home)
        const store = new Store(openDb(dbPath(cfg, paths)))
        const adapter = new Adapter({ cfg, paths, token, store })
        for (const s of ['SIGTERM', 'SIGINT', 'SIGHUP'] as const) process.on(s, () => adapter.stop(s))
        const code = await adapter.run()
        store.db.close()
        return code
      }
      case 'check': {
        const { paths, cfg } = setup()
        readToken(cfg, process.env, paths.home)
        if (cfg.hub) readHubToken(cfg.hub.tokenFile)
        out(`config ok: @${cfg.bot}, instance ${paths.instance}, mode ${cfg.mode}, db ${dbPath(cfg, paths)}, send ${cfg.send ? 'on' : 'off'}${cfg.hub ? `, hub ${new URL(cfg.hub.url).host}${cfg.hub.schema ? ` schema ${cfg.hub.schema}` : ''}` : ''}${switchOn(paths) ? ' (switch file present: run stays down)' : ''}`)
        return EXIT.ok
      }
      case 'status': {
        const { paths, cfg } = setup()
        const store = new Store(openDb(dbPath(cfg, paths)))
        const s = { ...status(store, paths, cfg), rules: { quiet_hours: cfg.quietHours, sleepin: cfg.sleepin, dedupe_window_s: cfg.dedupeWindowS } }
        store.db.close()
        if (args.includes('--json')) out(JSON.stringify(s))
        else {
          out(`tg-adapter @${cfg.bot} (instance ${paths.instance}, mode ${cfg.mode}): ${s.running ? `running, pid ${s.pid}` : 'not running'}${s.switch_off ? ', switch off' : ''}`)
          out(`offset ${s.offset} · events ${JSON.stringify(s.events)} · outbox ${JSON.stringify(s.outbox)}`)
          if (s.callback_ack_ms.n) out(`callback ack ms: p50 ${s.callback_ack_ms.p50} · p95 ${s.callback_ack_ms.p95} · max ${s.callback_ack_ms.max} (n ${s.callback_ack_ms.n})`)
          if (s.hub) out(`hub ${s.hub.host}${s.hub.schema ? ` (schema ${s.hub.schema})` : ''}: forwarded to row ${s.hub.forwarded_to} · backlog ${s.hub.backlog} · last ok ${s.hub.last_ok_age_s ?? '-'} s ago · companion seen ${s.hub.companion_seen_age_s ?? '-'} s ago${s.hub.last_error ? ` · last error: ${s.hub.last_error}` : ''}`)
        }
        return EXIT.ok
      }
      case 'companion': {
        const paths = resolvePaths(process.env, INSTANCE)
        if (args[0] === 'status') {
          const s = companionStatus(paths)
          if (args.includes('--json')) out(JSON.stringify(s))
          else out(`companion (${paths.instance}): ${s.running ? `running, pid ${s.pid}` : 'not running'} · jobs ${s.jobs ?? 0} (failed ${s.failed ?? 0}) · presence posts ${s.presence_posts ?? 0}${s.last_error ? ` · last error: ${s.last_error}` : ''}`)
          return EXIT.ok
        }
        const off = join(paths.configDir, `${paths.instance}.companion-off`)
        if (existsSync(off)) { err(`switch file present (${off}): companion stays down`); return EXIT.off }
        const c = new Companion({ hub: companionHub(process.env, paths.home), paths, env: process.env, log: m => process.stderr.write(`tg-adapter companion: ${m}\n`) })
        if (args.includes('--once')) {
          c.claimPid()
          try {
            await c.presenceTick(true)
            await c.claimOnce(Number(flag(args, '--wait') ?? 1))
            await c.settle()
          } finally { c.saveStats(); c.releasePid() }
          return EXIT.ok
        }
        for (const s of ['SIGTERM', 'SIGINT', 'SIGHUP'] as const) process.on(s, () => c.stop())
        return await c.run()
      }
      case 'enqueue': {
        const op = args[0]
        if (!op || !(OPS as readonly string[]).includes(op)) { err(`enqueue needs an op: ${OPS.join('|')}`); return EXIT.config }
        const chat = flag(args, '--chat')
        const bodyText = flag(args, '--body')
        if (!chat || !(/^-?\d+$/.test(chat) || (chat === '-' && ['commands', 'download'].includes(op))) || bodyText == null) { err('enqueue needs --chat ID (- for commands/download) and --body JSON'); return EXIT.config }
        let body: Record<string, unknown>
        try {
          body = JSON.parse(bodyText)
          if (!body || typeof body !== 'object' || Array.isArray(body)) throw new Error('not an object')
        } catch (e) { err(`--body must be a JSON object: ${(e as Error).message}`); return EXIT.config }
        const mode = flag(args, '--mode') ?? 'live'
        if (mode !== 'live' && mode !== 'shadow') { err('--mode must be live or shadow'); return EXIT.config }
        const nb = flag(args, '--not-before')
        const { paths, cfg } = setup()
        const store = new Store(openDb(dbPath(cfg, paths)))
        const filesText = flag(args, '--files')
        let files: string[] | null = null
        if (filesText != null) {
          try { files = JSON.parse(filesText); if (!Array.isArray(files) || !files.every(f => typeof f === 'string')) throw new Error('not a list of strings') }
          catch (e) { err(`--files must be a JSON list of paths: ${(e as Error).message}`); store.db.close(); return EXIT.config }
        }
        const rte = flag(args, '--reply-to-event')
        const id = store.enqueue({ bot: cfg.bot, chat, op, body, idem_key: flag(args, '--idem') ?? null, not_before: nb ? Number(nb) : null, mode,
          urgent: args.includes('--urgent'), files, reply_to_event: rte ? Number(rte) : null })
        store.db.close()
        out(id == null ? 'duplicate (idem key already queued)' : `queued #${id}`)
        return EXIT.ok
      }
      case 'bindings': {
        const { paths, cfg } = setup()
        const store = new Store(openDb(dbPath(cfg, paths)))
        try {
          const binder = new Binder({ store, tg: (async () => { throw new Error('no Bot API here') }) as any, cfg, paths, env: process.env,
            secret: () => false, clean: s => s, log: m => err(m), withSend: (_c, fn) => fn(), remember: () => {} })
          const state = { enabled: binder.enabled(), send: cfg.send, bindings: cfg.bindings, switch_file: binder.switchFile(),
            switch_off: existsSync(binder.switchFile()), registry: binder.registry.file, tools: binder.registry.tools().size,
            compat_instant: cfg.compatInstant ? true : false, compat_callbacks: cfg.compatCallbacks ? true : false }
          const show = (b: Binding | null) => b ? { source: b.source, kind: b.kind, name: b.name, tool: b.tool?.name ?? null,
            effect: b.tool?.effect ?? null, argv: b.argv, runs: !b.gated, why: b.gated ?? '', note: b.note, on_error: b.onError } : null
          if (args[0] === 'menu') return menuCmd(binder, store, cfg, args)
          if (args[0] === 'compat') {
            const c = binder.compatReads()
            if (args.includes('--json')) out(JSON.stringify({ bot: cfg.bot, compat_reads: c }))
            else out(compatText(c))
            return EXIT.ok
          }
          if (args[0] === 'resolve') {
            const data = flag(args, '--data')
            const text = data == null ? args.slice(1).filter(a => a !== '--json').join(' ') : ''
            const r = data != null
              ? show(binder.forButton(flag(args, '--chat') ?? '0', Number(flag(args, '--message') ?? 0), data))
              : (() => { const m = binder.forCommand(text, cfg.bot); return m ? { ...show(m.b), arg: m.arg } : null })()
            if (args.includes('--json')) out(JSON.stringify({ ...state, resolved: r }))
            else out(r ? `${r.runs ? 'runs' : 'to the brain'}: ${r.source} ${r.kind} ${r.name} → ${r.tool ?? '-'} (${r.effect ?? '-'}) ${JSON.stringify(r.argv)}${r.why ? ` · ${r.why}` : ''}`
              : `unbound${state.enabled ? '' : ' (bindings off)'}: goes to the brain`)
            return EXIT.ok
          }
          const table = binder.table()
          if (args.includes('--json')) out(JSON.stringify({ ...state, bindings: table }))
          else {
            out(`bindings ${state.enabled ? 'on' : `off (${!cfg.send ? 'send off' : !cfg.bindings ? 'config' : 'switch file'})`} · registry ${state.tools} tools · compat instant ${state.compat_instant ? 'on' : 'off'}, callbacks ${state.compat_callbacks ? 'on' : 'off'}`)
            for (const t of table) out(`${t.runs ? 'runs ' : 'BRAIN'} ${t.source.padEnd(7)} ${t.kind.padEnd(15)} ${t.key.padEnd(18)} ${String(t.tool ?? '-').padEnd(26)} ${String(t.effect ?? '-').padEnd(11)} ${t.why}`)
          }
          return EXIT.ok
        } finally {
          store.db.close()
        }
      }
      default:
        err('usage: tg-adapter [--instance test|live] run | check | status [--json] | enqueue OP --chat ID --body JSON [--idem KEY] [--not-before S] [--mode live|shadow] [--urgent] [--files JSON] [--reply-to-event ID] | bindings [--json] | bindings resolve TEXT|--data DATA [--json] | bindings menu [--custom FILE] [--diff] [--json] | bindings compat [--json]')
        return EXIT.config
    }
  } catch (e) {
    if (e instanceof ConfigError) { err(e.message); return EXIT.config }
    err(`error: ${(e as Error).message}`)
    return EXIT.error
  }
}

type MenuEntry = { command: string; description: string }

/** `bindings menu`: the menu set_commands would publish (outbox.ts 'commands'), and with --diff 0.1.22's. */
function menuCmd(binder: Binder, store: Store, cfg: Config, args: string[]): number {
  const file = flag(args, '--custom')
  let custom: MenuEntry[] = []
  let customFrom = 'none (no set_commands through the adapter yet)'
  try {
    if (file) { custom = JSON.parse(readFileSync(file, 'utf8')); customFrom = file }
    else { const m = store.getMeta(`commands:${cfg.bot}`); if (m) { custom = JSON.parse(m); customFrom = 'the last set_commands' } }
  } catch (e) { err(`custom list unreadable: ${(e as Error).message}`); return EXIT.config }
  if (!Array.isArray(custom)) { err('the custom list must be a JSON list of {command, description}'); return EXIT.config }
  const merge = (first: MenuEntry[]) => { const taken = new Set(first.map(c => c.command)); return [...first, ...custom.filter(c => !taken.has(c.command))] }
  const bound = binder.menu()
  const shown = merge(bound)
  const old = cfg.send && cfg.bindings ? merge(binder.instantTable().map(c => ({ command: c.command, description: c.description }))) : null
  const line = (c: MenuEntry) => `/${c.command} — ${c.description}`
  const diff: string[] = []
  if (old) {
    const a = old.map(line), b = shown.map(line)
    for (let i = 0; i < Math.max(a.length, b.length); i++) if (a[i] !== b[i]) diff.push(`#${i + 1}: 0.1.22 ${a[i] ?? '(none)'} | now ${b[i] ?? '(none)'}`)
  }
  if (args.includes('--json')) out(JSON.stringify({ bound, custom, custom_from: customFrom, shown, ...(args.includes('--diff') ? { menu_0122: old, identical: !!old && diff.length === 0, diff } : {}) }))
  else {
    out(`menu: ${shown.length} entries (${bound.length} bound, then the brain's own from ${customFrom})`)
    for (const c of shown) out(`  ${line(c)}`)
    if (args.includes('--diff')) out(!old ? 'diff: bindings off in the config (no bound entries)' : diff.length ? `differs from 0.1.22:\n  ${diff.join('\n  ')}` : 'identical to the 0.1.22 menu (/status + instant.json + the same custom list)')
  }
  return args.includes('--diff') && (!old || diff.length) ? EXIT.error : EXIT.ok
}

/** The compat counter as text. */
export function compatText(c: CompatReads | null, now = Date.now() / 1000): string {
  if (!c) return 'compat reads: not counted yet (the running adapter starts the counter)'
  const at = (t: number) => new Date(t * 1000).toISOString().slice(0, 16).replace('T', ' ') + 'Z'
  const uses = (['instant', 'callbacks', 'menu'] as const).filter(k => c[k])
  const last = Math.max(c.since, ...uses.map(k => c[k]!.last))
  const lines = [`compat reads since ${at(c.since)}: ${uses.length ? uses.map(k => `${k} ${c[k]!.n}`).join(', ') : 'none'}`]
  for (const k of uses) lines.push(`  ${k}: last ${at(c[k]!.last)} (${c[k]!.key})`)
  lines.push(`unread for ${((now - last) / 3600).toFixed(1)} h${uses.length ? '' : ' (since the counter started)'}`)
  return lines.join('\n')
}

export function status(store: Store, paths: Paths, cfg?: Config) {
  let pid: number | null = null
  let running = false
  try {
    const [first, second] = readFileSync(paths.pidFile, 'utf8').split('\n')
    pid = parseInt(first ?? '', 10) || null
    if (pid && (second ?? '').trim() === PID_MARKER) { try { process.kill(pid, 0); running = true } catch {} }
  } catch {}
  const rows = (sql: string) => Object.fromEntries((store.db.query(sql).all() as { k: string; n: number }[]).map(r => [r.k, r.n]))
  const acks = (store.db.query("SELECT CAST(json_extract(payload, '$.ack_ms') AS INTEGER) AS ms FROM events WHERE kind = 'tg.button' AND json_extract(payload, '$.ack_ms') IS NOT NULL ORDER BY id DESC LIMIT 500").all() as { ms: number }[])
    .map(r => r.ms).sort((a, b) => a - b)
  const pct = (p: number) => acks.length ? acks[Math.min(acks.length - 1, Math.ceil(p * acks.length) - 1)]! : null
  const offsets = store.db.query("SELECT k, v FROM meta WHERE k LIKE 'offset:%'").all() as { k: string; v: string }[]
  return {
    pid, running,
    instance: paths.instance,
    switch_off: switchOn(paths) != null,
    pending: (store.db.query("SELECT COUNT(*) AS n FROM events WHERE status = 'pending' AND source = 'tg-adapter'").get() as { n: number }).n,
    offset: offsets.length === 1 ? Number(offsets[0]!.v) : Object.fromEntries(offsets.map(o => [o.k, Number(o.v)])),
    events: rows("SELECT kind AS k, COUNT(*) AS n FROM events GROUP BY kind"),
    outbox: rows("SELECT status AS k, COUNT(*) AS n FROM outbox GROUP BY status"),
    outbox_failed_events: (store.db.query("SELECT COUNT(*) AS n FROM events WHERE kind = 'outbox.failed'").get() as { n: number }).n,
    dropped: rows("SELECT substr(k, 9) AS k, CAST(v AS INTEGER) AS n FROM meta WHERE k LIKE 'dropped:%'"),
    callback_ack_ms: { n: acks.length, p50: pct(0.5), p95: pct(0.95), max: acks.length ? acks[acks.length - 1]! : null },
    ...(cfg?.hub ? { hub: hubStatus(store, cfg) } : {}),
  }
}

/** Hub mode: the forwarder's state from the journal's meta (written by the running adapter). No secrets, no text. */
function hubStatus(store: Store, cfg: Config) {
  const fwd = Number(store.getMeta(FWD_ID) ?? 0) || 0
  const ok = Number(store.getMeta(FWD_OK) ?? 0) || 0
  const seen = Number(store.getMeta(COMPANION_SEEN) ?? 0) || 0
  let lastErr = ''
  try { const e = JSON.parse(store.getMeta(FWD_ERR) ?? 'null'); if (e && (!ok || e.ts > ok)) lastErr = String(e.error ?? '') } catch {}
  return {
    host: new URL(cfg.hub!.url).host,
    ...(cfg.hub!.schema ? { schema: cfg.hub!.schema } : {}),
    forwarded_to: fwd,
    backlog: (store.db.query("SELECT COUNT(*) AS n FROM events WHERE id > ? AND status != 'pending'").get(fwd) as { n: number }).n,
    last_ok_age_s: ok ? Math.round(Date.now() / 1000 - ok) : null,
    last_error: lastErr,
    companion_seen_age_s: seen ? Math.round(Date.now() / 1000 - seen) : null,
  }
}

if (import.meta.main) process.exit(await main(process.argv.slice(2)))

export { main }
