// Bindings runner (v2 Phase 1 P3). A binding is a decision the brain made in advance: a command or a button bound
// to a registry tool (registry.ts), run here without a brain turn. Unbound input goes to the brain at once.
//
// Sources, read live, first match wins:
//   1. tg-bind rows in the store's bindings table: command (key = name), button (key = exact data), prefix (the
//      longest key the data starts with); active and unexpired only (the brain adds them with `tg-bind`)
//   2. per-card binds: buttons[].bind {tool, args} on a reply/edit_message card (kind card, key chat:message:data,
//      7 days), saved by the outbox when the card is sent
//   3. the 0.1.22 compat tables: the plugin's instant.json (+ the built-in /status) and callbacks.json, in their own
//      format (instant.ts, callbacks.ts), read through the compat paths in the config. P9 moved every entry to a
//      tg-bind row; the tables stay readable as a fallback until P10, and every use the running adapter makes of one
//      (an input resolved from it, menu entries taken from it) is counted in the store's meta `compat_reads:<bot>`
//      (`tg-bind compat`) and logged. A lookup that finds nothing there is not a use.
// The /commands menu (P9): the tg-bind command rows in row order whose binding runs and whose tool has a menu line
// (tools.toml `menu`), then compat entries no row covers; set_commands adds the brain's own commands after them.
// Effects gate: a binding runs only when its tool is registered and its effect is at most send_owner; a compat entry
// whose exact argv is not registered, or a tool above send_owner (approvals: P7), is not run: the input goes to the
// brain as a normal event (payload binding_gated says why). The switch ~/.config/tg-adapter/bindings-off (or config
// bindings false, or send off) sends every command and tap to the brain.
//
// Runner: argv only (no shell), own process group (killed at shutdown), the environment minus the bot token (and any
// value holding it) with ~/bin on PATH, per-binding timeout, one run per binding at a time (a button tapped while
// its run is going only gets "⏳ Already running"; a command waits for the previous run). Answers as 0.1.22 did:
// commands → the stdout as plain text (or a picture: picture.ts, output "picture" tools only); buttons → the stdout
// (≤ 1000 chars) as a silent reply to the card, the keyboard left as it is (callback data, chat and message id in
// TELEGRAM_CALLBACK_DATA/_CHAT_ID/_MESSAGE_ID). The source row is committed as received by the binding (adapter.ts).
// Outcome: one tg.bound row per run (payload: the 0.1.22 channel meta + text, ok, outcome, ms, tool, binding,
// source_event). Delivery 'now' (it reaches the brain through the bridge, 0.1.22's content and meta: handled,
// instant_button, instant_error, status_error, picture, picture_error) when the binding's note is on, on any
// failure, or a forward (on_error "forward"); otherwise a 'digest' row acked by 'binding' with no answer text.
import { InputFile } from 'grammy'
import { accessSync, constants as fsConstants, existsSync, readFileSync, realpathSync, statSync } from 'fs'
import { basename, join } from 'path'
import { BUSY_TOAST, parseCallbacks, pickCallback, replyText, TOAST_MAX, type InstantButton } from './callbacks.ts'
import { killGroups, runLocal, type LocalResult } from './compat.ts'
import type { Config } from './config.ts'
import { nowS, type BindingRow, type EventRow, type Store } from './db.ts'
import { chunk, errText, MAX_CHUNK_LIMIT, type TgCall } from './format.ts'
import { INSTANT_ARG_MAX, INSTANT_NOTE_MAX, parseCommand, parseInstant, splitArgv, statusEntry, STATUS_MENU_ENTRY, type Instant } from './instant.ts'
import type { Paths } from './paths.ts'
import { CAPTION_MAX, parsePicture, pictureFallback, pictureProblem, pictureRoots } from './picture.ts'
import { defaultRegistryFile, Registry, tapAllowed, type Tool } from './registry.ts'

export { BUSY_TOAST }
export const CARD_BIND_TTL_S = 7 * 86_400
export const BINDINGS_OFF = 'bindings-off'
const SOURCE = 'tg-adapter'

export type BindSource = 'tg-bind' | 'card' | 'compat'

export type Binding = {
  source: BindSource
  kind: 'command' | 'button'
  /** The command name, the compat button entry's name, or the tool's name (tg-bind / card buttons). */
  name: string
  /** The bindings row (tg-bind, card). */
  row?: number
  tool: Tool | null
  argv: string[]
  timeoutMs: number
  note: boolean
  onError: 'reply' | 'forward'
  toast: string
  /** Commands: "/name <text>" passes the text as one extra argument. */
  takesText: boolean
  output: 'text' | 'picture'
  /** The built-in /status (status_error, its own note text). */
  status: boolean
  /** Set: not run from Telegram (why); the input goes to the brain. */
  gated?: string
}

/** The source row a run answers (from its payload). */
export type Origin = {
  id: number; idem: string; trace: string | null
  chat_id: string; message_id: number; user: string; user_id: string; ts: string
  /** commands: the message text; buttons: the callback data and the button's label */
  text: string; data?: string; label?: string
}

export type BinderDeps = {
  store: Store
  tg: TgCall
  cfg: Config
  paths: Paths
  env: Record<string, string | undefined>
  /** True for an environment value that holds the bot token (dropped from the tools' environment). */
  secret: (v: string) => boolean
  clean: (s: string) => string
  log: (m: string) => void
  /** Sends that are not the brain's answer (typing for its work goes on). */
  withSend: <T>(chat_id: string, fn: () => Promise<T>) => Promise<T>
  remember: (chat_id: string, message_id: number, text: string) => void
  /** A forward goes to the brain: show typing as for any owner message. */
  typing?: (chat_id: string) => void
  /** The running adapter counts its compat-table uses (the CLI's views don't). */
  countCompat?: boolean
  /** H6 hub mode: the tool runs in the brain container (a companion job); the tool is named, never an argv. */
  remote?: (job: { tool: string; extra: string[]; arg: string; env: Record<string, string>; timeout_ms: number }) => Promise<LocalResult & { unclaimed?: boolean }>
  /** H6 hub mode: where a picture answer's file may come from (files_dir/out), and its ref → path. */
  pictureRoots?: string[]
  resolveFile?: (s: string) => string
}

/** One compat table's uses since the counter started: count, the last one's time and key. */
export type CompatUse = { n: number; last: number; key: string }
export type CompatReads = { since: number; instant?: CompatUse; callbacks?: CompatUse; menu?: CompatUse }
export const COMPAT_READS_KEY = (bot: string) => `compat_reads:${bot}`

const cut = (s: string, n: number) => s.length <= n ? s : s.slice(0, n - 1) + '…'

function isExecutable(p: string): boolean {
  try { accessSync(p, fsConstants.X_OK); return statSync(p).isFile() } catch { return false }
}

type Cached<T> = { key: string; list: T[] } | null

export class Binder {
  readonly registry: Registry
  private instantCache: Cached<Instant> = null
  private callbackCache: Cached<InstantButton> = null
  private busy = new Set<string>()
  private chains = new Map<string, Promise<void>>()
  private groups = new Set<number>()
  private inflight = new Set<Promise<unknown>>()
  private stopping = false

  constructor(private readonly d: BinderDeps) {
    this.registry = new Registry(d.cfg.toolsRegistry ?? d.env.TG_ADAPTER_TOOLS ?? defaultRegistryFile(), d.paths.home, d.log)
    if (d.countCompat) {
      try { if (!d.store.getMeta(COMPAT_READS_KEY(d.cfg.bot))) d.store.setMeta(COMPAT_READS_KEY(d.cfg.bot), JSON.stringify({ since: nowS() })) }
      catch (err) { d.log(`compat counter not started: ${errText(err)}`) }
    }
  }

  /** The compat counter (null: never started in this store). */
  compatReads(): CompatReads | null {
    try { const v = this.d.store.getMeta(COMPAT_READS_KEY(this.d.cfg.bot)); return v ? JSON.parse(v) as CompatReads : null } catch { return null }
  }

  /** Count one use of a compat table by the running adapter (logged; never throws). */
  private compatUse(table: 'instant' | 'callbacks' | 'menu', key: string): void {
    if (!this.d.countCompat) return
    this.d.log(`compat read: ${table === 'callbacks' ? 'callbacks.json' : 'instant.json'} ${table === 'menu' ? `menu ${key}` : key} (no tg-bind row covers it)`)
    try {
      // read-modify-write without a transaction: only the running adapter writes this key, and synchronously
      const k = COMPAT_READS_KEY(this.d.cfg.bot)
      const cur: CompatReads = (() => { try { return JSON.parse(this.d.store.getMeta(k) ?? '') } catch { return { since: nowS() } } })()
      const prev = cur[table]
      cur[table] = { n: (prev?.n ?? 0) + 1, last: nowS(), key: cut(key, 120) }
      this.d.store.setMeta(k, JSON.stringify(cur))
    } catch (err) {
      this.d.log(`compat read not counted: ${errText(err)}`)
    }
  }

  get home(): string { return this.d.paths.home }

  /** The switch file: present = every command and tap goes to the brain. */
  switchFile(): string {
    return this.d.env.TG_ADAPTER_BINDINGS_OFF || join(this.d.paths.configDir, BINDINGS_OFF)
  }

  /** Bindings run only with send on (answers go out from here), the config's bindings on and no switch file. */
  enabled(): boolean {
    return this.d.cfg.send && this.d.cfg.bindings && !existsSync(this.switchFile())
  }

  // --- the compat tables (0.1.22 format, re-read when the file changes) --------------------------------

  private loadTable<T>(file: string | undefined, cache: Cached<T>, parse: (raw: unknown) => { list: T[]; skipped: string[] },
    set: (c: Cached<T>) => void): T[] {
    if (!file) return []
    let key: string
    try {
      const st = statSync(file)
      key = `${st.mtimeMs}:${st.size}`
    } catch {
      set(null)
      return []
    }
    if (cache?.key === key) return cache.list
    let list: T[] = []
    let skipped: string[]
    try {
      const r = parse(JSON.parse(readFileSync(file, 'utf8')))
      list = r.list
      skipped = r.skipped
    } catch (err) {
      skipped = [`the whole file (${errText(err)})`]
    }
    if (skipped.length) this.d.log(`compat table ${basename(file)}: skipped ${skipped.join(', ')}`)
    set({ key, list })
    return list
  }

  /** The 0.1.22 status command: TG_ADAPTER_STATUS_CMD ("" = none), else ~/bin/tg-status show --plain when executable. */
  statusArgv(): string[] | null {
    const v = this.d.env.TG_ADAPTER_STATUS_CMD
    if (v !== undefined) { const a = splitArgv(v, this.home); return a.length ? a : null }
    const bin = join(this.home, 'bin', 'tg-status')
    return isExecutable(bin) ? [bin, 'show', '--plain'] : null
  }

  /** The instant table: /status (when the instant compat table is on and the status command exists), then instant.json. */
  instantTable(): Instant[] {
    if (!this.d.cfg.compatInstant) return []
    const status = this.statusArgv()
    const list = this.loadTable(this.d.cfg.compatInstant, this.instantCache, raw => parseInstant(raw, this.home), c => { this.instantCache = c })
    return [...(status ? [statusEntry(status)] : []), ...list]
  }

  callbackTable(): InstantButton[] {
    const splitter = (s: string) => splitArgv(s, this.home)
    return this.loadTable(this.d.cfg.compatCallbacks, this.callbackCache, raw => parseCallbacks(raw, { splitArgv: splitter, home: this.home }),
      c => { this.callbackCache = c })
  }

  /** The /commands menu entries that come before the brain's own (0.1.22's menuCommands with an allowlist): the
   *  tg-bind command rows in row order whose binding runs and whose tool has a menu line, then the compat instant
   *  entries no command row covers (counted as a compat use). */
  menu(): { command: string; description: string }[] {
    if (!this.d.cfg.send || !this.d.cfg.bindings) return []
    const out: { command: string; description: string }[] = []
    const rowed = new Set<string>()
    for (const r of this.d.store.commandRows()) {
      rowed.add(r.key)
      const b = this.fromRow(r, 'command', 'tg-bind')
      if (!b.gated && b.tool?.menu) out.push({ command: r.key, description: b.tool.menu })
    }
    const extra = this.instantTable().filter(c => !rowed.has(c.command)).map(c => ({ command: c.command, description: c.description }))
    if (extra.length) this.compatUse('menu', extra.map(c => '/' + c.command).join(' '))
    return [...out, ...extra]
  }

  // --- resolution ------------------------------------------------------------------------------------

  private gate(b: Binding): Binding {
    if (!b.argv.length || !b.tool) b.gated = `${b.name}: not in the tools registry`
    else if (!tapAllowed(b.tool.effect)) b.gated = `${b.name}: ${b.tool.name} has effect ${b.tool.effect}, which needs an approval`
    return b
  }

  private fromRow(r: BindingRow, kind: 'command' | 'button', source: BindSource): Binding {
    const tool = this.registry.get(r.tool)
    let extra: string[] = []
    try { const a = JSON.parse(r.args); if (Array.isArray(a)) extra = a.map(String) } catch {}
    return this.gate({
      source, kind, name: kind === 'command' ? r.key : r.tool, row: r.id, tool,
      argv: tool ? [...tool.argv, ...extra] : [],
      timeoutMs: tool?.timeoutMs ?? 20_000, note: !!r.note, onError: r.on_error === 'forward' ? 'forward' : 'reply', toast: tool?.toast ?? '',
      takesText: tool?.textArg ?? false, output: tool?.output ?? 'text', status: kind === 'command' && r.key === 'status',
    })
  }

  private fromInstant(spec: Instant): Binding {
    const tool = this.registry.byArgv(spec.argv)
    return this.gate({
      source: 'compat', kind: 'command', name: spec.command, tool, argv: spec.argv, timeoutMs: spec.timeoutMs, note: spec.note,
      onError: spec.onError, toast: '', takesText: spec.args, output: tool?.output ?? 'text', status: spec.command === 'status',
    })
  }

  private fromCallback(cb: InstantButton): Binding {
    const tool = this.registry.byArgv(cb.argv)
    return this.gate({
      source: 'compat', kind: 'button', name: cb.name, tool, argv: cb.argv, timeoutMs: cb.timeoutMs, note: cb.note,
      onError: 'reply', toast: cb.toast, takesText: false, output: tool?.output ?? 'text', status: false,
    })
  }

  /** The binding of a command name (any source), ignoring the switch. */
  commandBinding(name: string): Binding | null {
    const r = this.d.store.binding('command', name)
    if (r) return this.fromRow(r, 'command', 'tg-bind')
    const spec = this.instantTable().find(c => c.command === name)
    if (!spec) return null
    this.compatUse('instant', `/${name}`)
    return this.fromInstant(spec)
  }

  /** The binding of a press (any source), ignoring the switch. */
  buttonBinding(chat_id: string, message_id: number, data: string): Binding | null {
    if (data.startsWith('perm:') || data.startsWith('ap:')) return null // ap: = P7 approvals, never a binding
    const r = this.d.store.binding('button', data) ?? this.d.store.prefixBinding(data)
    if (r) return this.fromRow(r, 'button', 'tg-bind')
    const card = this.d.store.binding('card', `${chat_id}:${message_id}:${data}`)
    if (card) return this.fromRow(card, 'button', 'card')
    const cb = pickCallback(this.callbackTable(), data)
    if (!cb) return null
    this.compatUse('callbacks', `${cb.name} (${cut(data, 64)})`)
    return this.fromCallback(cb)
  }

  /** An owner's DM text that is a bound command: "/name", "/name@<bot>", or "/name <text>" for a binding that takes
   *  text (≤ 512 chars). null: not a command, another bot, unbound, or text the binding doesn't take. */
  forCommand(text: string, botUsername: string): { b: Binding; arg: string } | null {
    if (!this.enabled()) return null
    const c = parseCommand(text)
    if (!c || (c.at && c.at.toLowerCase() !== botUsername.toLowerCase())) return null
    const b = this.commandBinding(c.name)
    if (!b) return null
    if (c.arg && (!b.takesText || c.arg.length > INSTANT_ARG_MAX)) return null
    return { b, arg: c.arg }
  }

  /** The binding of an owner's press in a DM, or null (unbound, or bindings off). */
  forButton(chat_id: string, message_id: number, data: string): Binding | null {
    if (!this.enabled()) return null
    return this.buttonBinding(chat_id, message_id, data)
  }

  /** The toast for a bound press (callbacks.ts toastFor). */
  toast(b: Binding, label: string): string {
    const t = b.toast || `✓ ${label}`
    return t.length > TOAST_MAX ? `${t.slice(0, TOAST_MAX - 1)}…` : t
  }

  private keyOf(b: Binding): string { return `${b.source}:${b.kind}:${b.name}` }

  /** A button run may start (false: that binding is still running: the tap only gets BUSY_TOAST). */
  claim(b: Binding): boolean {
    const k = this.keyOf(b)
    if (this.busy.has(k)) return false
    this.busy.add(k)
    return true
  }

  release(b: Binding): void { this.busy.delete(this.keyOf(b)) }

  // --- card binds (buttons[].bind on a sent card) ----------------------------------------------------

  /** Save a card's button binds after it went out; returns notes for the brain (refused binds). An edit's new
   *  keyboard replaces the message's earlier binds. */
  saveCardBinds(chat_id: string, message_id: number, buttons: unknown): string[] {
    if (!Array.isArray(buttons)) return []
    const flat = (buttons.length > 0 && !Array.isArray(buttons[0]) ? buttons : buttons.flat()) as unknown[]
    const notes: string[] = []
    const binds: { key: string; tool: string; args: string[]; note: boolean }[] = []
    let any = false
    for (const spec of flat) {
      if (!spec || typeof spec !== 'object') continue
      const s = spec as Record<string, any>
      if (s.bind == null) continue
      any = true
      const label = String(s.text ?? '')
      const data = String(s.data ?? s.callback_data ?? s.text)
      if (data.startsWith('ap:')) { notes.push(`button "${label}": approval buttons (ap:) can't be bound`); continue }
      const name = typeof s.bind?.tool === 'string' ? s.bind.tool : ''
      const tool = this.registry.get(name)
      if (s.url) { notes.push(`button "${label}": a URL button can't be bound`); continue }
      if (!tool) { notes.push(`button "${label}": tool "${name}" is not in the tools registry; a press reaches you instead`); continue }
      if (!tapAllowed(tool.effect)) { notes.push(`button "${label}": ${tool.name} has effect ${tool.effect} (needs an approval); a press reaches you instead`); continue }
      const args = Array.isArray(s.bind.args) ? s.bind.args.map(String) : []
      binds.push({ key: `${chat_id}:${message_id}:${data}`, tool: tool.name, args, note: tool.note })
    }
    try {
      this.d.store.saveCardBinds(`${chat_id}:${message_id}:`, binds, nowS() + CARD_BIND_TTL_S)
    } catch (err) {
      notes.push(`button binds not saved (${errText(err)}); presses reach you instead`)
    }
    if (any && binds.length && !this.enabled()) notes.push('bindings are off right now: presses reach you')
    return notes
  }

  // --- runs ------------------------------------------------------------------------------------------

  private env(extra: Record<string, string> = {}): Record<string, string | undefined> {
    const base: Record<string, string | undefined> = {}
    for (const [k, v] of Object.entries(this.d.env)) {
      if (k === 'TELEGRAM_BOT_TOKEN' || v == null || this.d.secret(v)) continue
      base[k] = v
    }
    const path = (base.PATH || '/usr/local/bin:/usr/bin:/bin').split(':')
    const bins = [join(this.home, 'bin'), join(this.home, '.local', 'bin')].filter(b => !path.includes(b))
    base.PATH = [...bins, ...path].join(':')
    return { ...base, ...extra }
  }

  /** Run a binding's tool: here (local mode) or as a companion job (hub mode). */
  private exec(b: Binding, arg: string, extra: Record<string, string>): Promise<LocalResult & { unclaimed?: boolean }> {
    if (this.d.remote) {
      const base = b.tool?.argv.length ?? 0
      return this.d.remote({ tool: b.tool?.name ?? b.name, extra: b.argv.slice(base), arg, env: extra, timeout_ms: b.timeoutMs })
    }
    return runLocal(arg ? [...b.argv, arg] : b.argv, b.timeoutMs, this.d.clean, { env: this.env(extra), cwd: this.home, groups: this.groups })
  }

  /** A picture answer; in hub mode its "photo" is a hub ref, resolved to the shared files dir. */
  private picture(out: string): ReturnType<typeof parsePicture> {
    const pic = parsePicture(out)
    if (pic && this.d.resolveFile) pic.photo = this.d.resolveFile(pic.photo)
    return pic
  }

  private picRoots(): string[] {
    return this.d.pictureRoots ?? pictureRoots(this.home)
  }

  private track(p: Promise<unknown>): void {
    this.inflight.add(p)
    void p.finally(() => this.inflight.delete(p))
  }

  /** Run a bound command (after the previous run of the same binding). Never throws. */
  startCommand(b: Binding, arg: string, o: Origin): void {
    const k = this.keyOf(b)
    const prev = this.chains.get(k) ?? Promise.resolve()
    const p = prev.then(() => this.runCommand(b, arg, o)).catch(err => this.d.log(`binding ${b.name} failed: ${errText(err)}`))
    this.chains.set(k, p)
    void p.finally(() => { if (this.chains.get(k) === p) this.chains.delete(k) })
    this.track(p)
  }

  /** Run a bound press (claimed by the caller); releases the claim when done. Never throws. */
  startButton(b: Binding, o: Origin): void {
    const p = this.runButton(b, o).catch(err => this.d.log(`binding ${b.name} failed: ${errText(err)}`)).finally(() => this.release(b))
    this.track(p)
  }

  /** Stop: kill the running tools' process groups; wait ≤ waitMs for their outcome rows. */
  async stop(waitMs = 3_000): Promise<void> {
    killGroups(this.groups)
    if (this.inflight.size) await Promise.race([Promise.allSettled([...this.inflight]), Bun.sleep(waitMs)])
    this.stopping = true
  }

  private async runCommand(b: Binding, arg: string, o: Origin): Promise<void> {
    const t0 = performance.now()
    const { clean } = this.d
    const chat_id = o.chat_id
    const cmd = `/${b.name}`
    const meta: Record<string, string> = { chat_id, message_id: String(o.message_id), user: o.user, user_id: o.user_id, ts: o.ts }
    const errKey = b.status ? 'status_error' : 'instant_error'
    const done = (outcome: string, ok: boolean, content: string, extra: Record<string, string>, deliver: boolean) =>
      this.outcome(b, o, t0, outcome, ok, content, { ...meta, ...extra }, deliver)
    const forward = (why: string) => {
      this.d.typing?.(chat_id)
      done('forwarded', false, o.text, { [errKey]: clean(why) }, true)
    }
    const r = await this.exec(b, arg, {})
    if (r.unclaimed) return forward(r.error ?? 'the brain-side companion is offline')
    const out = r.stdout.trim()
    const failed = !r.ok || !out
    const why = r.ok ? `the ${b.status ? 'status ' : ''}command printed nothing` : r.error ?? 'failed'
    if (failed && b.onError === 'forward') return forward(why)
    let body = !failed || out ? out : `⚠️ ${cmd} didn't work (${clean(why)}). Ask me in a normal message if you need it.`
    const pic = out && b.output === 'picture' ? this.picture(out) : null
    let picSent = false
    let picError = pic ? pictureProblem(pic.photo, this.picRoots()) : null
    try {
      await this.d.withSend(chat_id, async () => {
        if (pic && !picError) {
          const cap = pic.caption.length <= CAPTION_MAX ? pic.caption : ''
          try {
            const sent = await this.d.tg('sendPhoto', { chat_id, photo: new InputFile(realpathSync(pic.photo)), ...(cap ? { caption: cap } : {}) })
            this.d.remember(chat_id, sent.message_id, cap || `[picture] ${cmd}`)
            picSent = true
            body = cap ? '' : pic.caption // a caption over 1024 chars follows as text
          } catch (err) {
            picError = clean(`sendPhoto failed: ${errText(err)}`)
          }
        }
        if (pic && picError) body = pictureFallback(pic, cmd, picError)
        for (const part of body ? chunk(body, MAX_CHUNK_LIMIT, 'newline') : []) {
          const sent = await this.d.tg('sendMessage', { chat_id, text: part, link_preview_options: { is_disabled: true } })
          this.d.remember(chat_id, sent.message_id, part)
        }
      })
    } catch (err) {
      return forward(`${failed ? `${why}; ` : ''}sending the ${b.status ? 'status' : 'answer'} failed: ${errText(err)}`)
    }
    const handled = { handled: b.name }
    if (failed) {
      const sentWhat = picSent ? "the command's own fallback picture" : out ? "the command's own fallback text" : 'a short "didn\'t work" line'
      return done('failed', false, `${cmd} (the plugin's instant answer failed: ${clean(why)}; it sent the owner ${sentWhat}; no reply needed)`,
        { ...handled, instant_error: clean(why) }, true)
    }
    const asked = b.takesText ? cut(o.text.trim().replace(/\s+/g, ' '), 200) : cmd
    // A picture that fell back to text is reported even with note false (like instant_error).
    if (pic && !picSent) {
      return done('ok', true, `${asked} (the plugin's picture answer failed: ${picError}; it sent the owner the text instead; no reply needed). It sent:\n${cut(body, INSTANT_NOTE_MAX)}`,
        { ...handled, picture_error: picError ?? 'failed' }, true)
    }
    if (!b.note) return done('ok', true, `${asked} (answered instantly by the plugin; no reply needed)`, handled, false)
    if (b.status) return done('ok', true, '/status (answered instantly by the plugin with the status board; no reply needed)', handled, true)
    if (pic) {
      return done('ok', true, `${asked} (answered instantly by the plugin with a picture; no reply needed). It sent a photo${pic.caption ? ' with the caption:\n' + cut(pic.caption, INSTANT_NOTE_MAX) : ' (no caption)'}`,
        { ...handled, picture: basename(pic.photo) }, true)
    }
    return done('ok', true, `${asked} (answered instantly by the plugin; no reply needed). It sent:\n${cut(out, INSTANT_NOTE_MAX)}`, handled, true)
  }

  private async runButton(b: Binding, o: Origin): Promise<void> {
    const t0 = performance.now()
    const { clean } = this.d
    const chat_id = o.chat_id
    const label = o.label ?? o.data ?? ''
    const data = o.data ?? ''
    const r = await this.exec(b, '', { TELEGRAM_CALLBACK_DATA: data, TELEGRAM_CALLBACK_CHAT_ID: chat_id, TELEGRAM_CALLBACK_MESSAGE_ID: String(o.message_id) })
    if (r.unclaimed) { // hub mode, no companion: the press goes to the brain as it is (as with bindings off)
      this.d.typing?.(chat_id)
      return this.outcome(b, o, t0, 'forwarded', false, `(pressed button: ${label})`, {
        chat_id, message_id: String(o.message_id), user: o.user, user_id: o.user_id, ts: o.ts,
        event_type: 'button', callback_data: data, button_text: label, instant_error: clean(r.error ?? 'the brain-side companion is offline'),
      }, true)
    }
    const pic = r.ok && b.output === 'picture' ? this.picture(r.stdout.trim()) : null
    const out = pic ? '' : replyText(r.stdout)
    const why = r.ok ? '' : clean(r.error ?? 'failed')
    let body = out || (r.ok ? '' : `⚠️ ${label} didn't work (${why}). Ask me in a normal message if you need it.`)
    let sendErr = ''
    const reply = { disable_notification: true, reply_parameters: { message_id: o.message_id, allow_sending_without_reply: true } }
    try {
      await this.d.withSend(chat_id, async () => {
        if (pic) {
          const problem = pictureProblem(pic.photo, this.picRoots())
          const cap = pic.caption.length <= CAPTION_MAX ? pic.caption : ''
          if (!problem) {
            const sent = await this.d.tg('sendPhoto', { chat_id, photo: new InputFile(realpathSync(pic.photo)), ...(cap ? { caption: cap } : {}), ...reply })
            this.d.remember(chat_id, sent.message_id, cap || `[picture] ${label}`)
            body = cap ? '' : replyText(pic.caption)
          } else body = replyText(pictureFallback(pic, label, problem))
        }
        if (body) {
          const sent = await this.d.tg('sendMessage', { chat_id, text: body, link_preview_options: { is_disabled: true }, ...reply })
          this.d.remember(chat_id, sent.message_id, body)
        }
      })
    } catch (err) {
      sendErr = clean(`sending its output failed: ${errText(err)}`)
    }
    const told = sendErr ? `the owner got nothing (${sendErr})` : body ? `the owner got: ${cut(body, 300)}` : 'the owner got nothing'
    const meta: Record<string, string> = {
      chat_id, message_id: String(o.message_id), user: o.user, user_id: o.user_id, ts: o.ts,
      event_type: 'button', callback_data: data, button_text: label,
    }
    const result = out ? { instant_result: cut(out, 500) } : {}
    if (!r.ok) {
      return this.outcome(b, o, t0, 'failed', false,
        `(pressed button: ${label}) The plugin's instant button "${b.name}" failed (${why}); ${told}. Check it; no reply needed unless you can fix it.`,
        { ...meta, instant_button: b.name, instant_error: why }, true)
    }
    if (sendErr) {
      return this.outcome(b, o, t0, 'send_failed', false,
        `(pressed button: ${label}) The plugin ran instant button "${b.name}", but ${told}: tell the owner yourself.`,
        { ...meta, handled: b.name, instant_button: b.name, instant_error: sendErr, ...result }, true)
    }
    if (!b.note) {
      return this.outcome(b, o, t0, 'ok', true, `(pressed button: ${label}) The plugin ran instant button "${b.name}". Done; no reply needed.`,
        { ...meta, handled: b.name, instant_button: b.name }, false)
    }
    return this.outcome(b, o, t0, 'ok', true, `(pressed button: ${label}) The plugin ran instant button "${b.name}"; ${told}. Done; no reply needed.`,
      { ...meta, handled: b.name, instant_button: b.name, ...result }, true)
  }

  /** The tg.bound row of a run. */
  private outcome(b: Binding, o: Origin, t0: number, outcome: string, ok: boolean, content: string, meta: Record<string, string>, deliver: boolean): void {
    if (this.stopping) return
    const payload: Record<string, unknown> = {
      ...meta, text: content, ok, outcome, ms: Math.round(performance.now() - t0),
      tool: b.tool?.name ?? null, binding: b.source, binding_name: b.name, ...(b.row != null ? { binding_row: b.row } : {}), source_event: o.id,
    }
    const row: EventRow = {
      ts: nowS(), source: SOURCE, kind: 'tg.bound',
      subject: `bound ${b.kind} ${b.name}: ${outcome}`, next: deliver ? (ok ? 'no reply needed' : 'check it; tell him if needed') : '',
      payload, labels: ['tg', this.d.cfg.mode, 'bound'], delivery: deliver ? 'now' : 'digest', trace_id: o.trace, idem_key: `tg.bound:${o.idem}`,
    }
    try { this.d.store.insertBound(row, deliver) } catch (err) { this.d.log(`tg.bound row for #${o.id} not written: ${errText(err)}`) }
  }

  /** Every binding as the CLI shows it (tg-bind rows, then the compat tables), ignoring the switch. */
  table(): { source: BindSource; kind: string; key: string; name: string; tool: string | null; effect: string | null; argv: string[]; runs: boolean; why: string }[] {
    const out: ReturnType<Binder['table']> = []
    const add = (key: string, kind: string, b: Binding) => out.push({
      source: b.source, kind, key, name: b.name, tool: b.tool?.name ?? null, effect: b.tool?.effect ?? null, argv: b.argv,
      runs: !b.gated, why: b.gated ?? '',
    })
    for (const r of this.d.store.bindings(['command', 'button', 'prefix', 'card'])) {
      add(r.key, r.kind, this.fromRow(r, r.kind === 'command' ? 'command' : 'button', r.kind === 'card' ? 'card' : 'tg-bind'))
    }
    for (const spec of this.instantTable()) add(`/${spec.command}`, 'compat command', this.fromInstant(spec))
    for (const cb of this.callbackTable()) add(cb.data.join(' '), 'compat button', this.fromCallback(cb))
    return out
  }
}

