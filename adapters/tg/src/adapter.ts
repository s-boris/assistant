// The adapter: one getUpdates long-poll loop (grammY's Api as the transport) whose updates become
// event rows, plus the outbox consumer v0. The poll loop replaces the plugin's grammY Bot runner
// and its poller election (bot.pid takeover, the claude-process --channels veto): the adapter
// keeps its own offset in the DB and never competes for a token.
//
// Interlocks, all before the first getUpdates:
//   - the switch file (~/.config/tg-adapter/off) keeps it down (checked again every round);
//   - config required, mode "test", instant commands / side-effect callbacks off (config.ts);
//   - getMe's username must equal config.bot, else it refuses to start;
//   - own PID file (adapter.pid): a live holder → refuse; a stale one is replaced;
//   - 409 Conflict (another poller or a webhook holds the token) → stop, never fight for it;
//   - 401 → stop. Binds no ports.
// Updates: callback queries of a batch are answered at once (concurrently, before any DB work) and
// their latency is kept in the row (payload.ack_ms). Each update's row and the next offset are
// committed together (db.ts), so a crash or kill mid-stream loses nothing and duplicates nothing.
// Outbox (send: true only): the consumer applies the WP7 rules (rules.ts); the owner's zone (tripclock)
// and the sleep-in state are looked up in the background and waited for (≤ 2 s) before the first pass.
// P2 (live mode + parity): inbound prep (prep.ts: eager photo downloads, transcripts, private scans; rows
// stay 'pending' until done, in per-chat order), drafts (draft.ts), board-driven typing (typing.ts, send on),
// the access file's ackReaction (send on), tg.stop rows, the compat inbound journal (compat.ts) and the
// file guard for outgoing files (files.ts).
// P3 (bindings, bind.ts): a command or press bound to a registry tool is answered here (the source row is committed
// as received by the binding, so the bridge never sends it) and its outcome becomes a tg.bound row; a bound press
// gets its own toast at once (or "⏳ Already running"), and its keyboard is left as it is.
// P7 (approvals, approvals.ts): a send row's `approval` field becomes a nonce + a ✅/❌ row (ap:<nonce>:y|n); his tap
// is decided at the callback answer (his user id, his DM, before the expiry, once) and becomes an approval.decided row
// instead of a tg.button row (bindings never see ap: data); pending cards past their expiry get one inert "⌛ expired"
// button. The switch approvals-off hands ap: taps to the brain as plain presses (poll-mode behaviour).
// H6 (hub mode, config `hub`): the same adapter in the hub's tg container. The SQLite journal is local and forwarded
// to hubd (hubsync.ts Forwarder); the outbox, approvals and bindings are hubd's; bind runs, transcripts and private
// scans are companion jobs (companion.ts, brain container); downloads and uploads travel through the shared files dir
// as hubfile: refs; typing reads the board the companion posts. A tap's ack never waits on the hub or a job, except
// an approval tap, whose toast reports the hub's decision (≤ 3 s, else "error" and the keyboard stays).
import { Api, Context, GrammyError } from 'grammy'
import type { Update, UserFromGetMe } from 'grammy/types'
import { closeSync, existsSync, mkdirSync, openSync, readFileSync, rmSync, writeSync } from 'fs'
import { join } from 'path'
import { loadAccess, type Access } from './access.ts'
import {
  ACTION_MAX, Approvals, APPROVALS_OFF, approvalRow, DEFAULT_TTL_S, EXPIRED_LABEL, hm, MAX_TTL_S, MIN_TTL_S, noncesIn, oneLine,
  parseApprovalData, REF_RE, REPLACED_LABEL, sha256, SUMMARY_MAX, TOAST, type ApprovalRow, type Tap,
} from './approvals.ts'
import { Binder, BUSY_TOAST, type Binding, type Origin } from './bind.ts'
import { journalAppend, journalEntry } from './compat.ts'
import { Drafts } from './draft.ts'
import { downloadFile, fileGuard, type Guard } from './files.ts'
import { InboundPrep, SCAN_TIMEOUT_MS, TRANSCRIBE_TIMEOUT_MS } from './prep.ts'
import { ensureHubFiles, fromRef, HubClient, HubError, hubFiles, readHubToken, toRef, type HubFiles } from './hub.ts'
import { BindingsMirror, COMPANION_SEEN, Forwarder, HubApprovals, hubQueue, PresenceMirror, RemoteJobs } from './hubsync.ts'
import type { OutboxQueue } from './outbox.ts'
import { Typing } from './typing.ts'
import { forgetPress, lookupPress, type OnPress } from './buttons.ts'
import { botIdOf, type Config } from './config.ts'
import { nowS, Store, type OutboxRow } from './db.ts'
import { errText, textBody, type TgCall } from './format.ts'
import { ALLOWED_UPDATES, callbackInfo, callbackToast, idemKey, toInbound, truncate, type CallbackAck, type PollMeta, type Recent } from './inbound.ts'
import { drainOutbox, failInterrupted, type OutboxDeps } from './outbox.ts'
import { switchOn, type Paths } from './paths.ts'
import { QuietClock, quietZone, quietZonePinned, tripclockArgv } from './quiet.ts'
import { SleepinGuard, sleepinArgv, type Rules } from './rules.ts'

export const PID_MARKER = 'tg-adapter'
export const EXIT = { ok: 0, config: 2, conflict: 3, unauthorized: 4, wrongBot: 5, running: 6, off: 7, error: 1 } as const

/** A hub-mode card row that names no nonce: the hub's tg never mints approvals (the brain does). */
export const HUB_NO_MINT = 'hub mode: the brain mints approvals; send approval:{nonce}'

export class StopError extends Error {
  constructor(message: string, readonly code: number) { super(message) }
}

export type AdapterOpts = {
  cfg: Config
  paths: Paths
  token: string
  store: Store
  /** grammY ApiClientOptions extras (tests: a mock fetch). */
  apiOptions?: Record<string, unknown>
  log?: (m: string) => void
  now?: () => number
  /** Environment for the rule lookups (tripclock, sleepin commands); default process.env. */
  env?: Record<string, string | undefined>
  /** Tests: override parts of the outbox rules (zone, sleeping, ...). */
  rules?: Partial<Rules>
  /** H6 hub mode, tests: a fake hubd (no sockets) and the tg caller's token. */
  hubFetch?: (req: Request) => Promise<Response>
  hubToken?: string
}

const RECENT_MAX = 200

/** A press's binding decision (or, for ap: data, his approval tap), made while its callback query is answered. */
type Press = { bound?: Binding; busy?: boolean; gated?: Binding; approval?: Promise<Tap> }

/** How often the expiry sweep looks for pending approval cards past their expiry. */
export const APPROVAL_SWEEP_MS = 5_000
/** Hub mode: how long an approval tap's toast waits for hubd's decision. */
export const HUB_TAP_MS = 3_000

export class Adapter {
  readonly api: Api
  readonly tg: TgCall
  readonly botId: string
  me: UserFromGetMe | null = null
  private abort = new AbortController()
  private stopping = false
  private outboxTimer: ReturnType<typeof setInterval> | null = null
  private outboxBusy: Promise<unknown> | null = null
  private repeats = new Map<string, number>()
  /** When Telegram last answered a Bot API call (ok or an error below 500): outbox.ts retryPlan's outage test. */
  private lastOkS = 0
  private recent = new Map<string, Recent>()
  private ownsPid = false
  readonly log: (m: string) => void
  readonly offsetKey: string
  readonly quiet: QuietClock
  readonly sleepin: SleepinGuard
  readonly typing: Typing
  readonly drafts: Drafts
  readonly prep: InboundPrep
  readonly guard: Guard
  readonly buttonsFile: string
  readonly binder: Binder
  readonly approvals: Approvals
  private approvalSweepAt = 0
  private approvalSweeping: Promise<unknown> | null = null
  private readonly fetchFile: (url: string, timeoutMs: number) => Promise<Response>
  // H6 hub mode (null in local mode)
  readonly hub: HubClient | null = null
  readonly files: HubFiles | null = null
  readonly forwarder: Forwarder | null = null
  readonly bindMirror: BindingsMirror | null = null
  readonly presence: PresenceMirror | null = null
  readonly hubAp: HubApprovals | null = null
  readonly jobs: RemoteJobs | null = null
  readonly queue: OutboxQueue | null = null
  /** Downloads: the instance inbox, or files_dir/in in hub mode. */
  readonly inboxDir: string

  constructor(readonly o: AdapterOpts) {
    this.log = o.log ?? (m => process.stderr.write(`tg-adapter: ${this.clean(m)}\n`))
    this.botId = botIdOf(o.token)
    this.offsetKey = `offset:${this.botId}`
    const client: Record<string, unknown> = { apiRoot: o.cfg.apiRoot, fetch: globalThis.fetch, ...(o.apiOptions ?? {}) }
    if (o.cfg.apiUnixSocket) client.baseFetchConfig = { unix: o.cfg.apiUnixSocket }
    this.api = new Api(o.token, client as any)
    this.tg = (method, params) => (this.api.raw as any)[method](params)
    this.api.config.use(async (prev, method, payload, signal) => {
      const res = await prev(method, payload, signal)
      if (res.ok || res.error_code < 500) this.lastOkS = nowS()
      return res
    })
    const env = o.env ?? process.env
    const home = o.paths.home
    if (o.cfg.hub) {
      const h = o.cfg.hub
      this.hub = new HubClient({ url: h.url, token: o.hubToken ?? readHubToken(h.tokenFile), ...(h.schema ? { schema: h.schema } : {}),
        ...(o.hubFetch ? { fetch: o.hubFetch } : {}), log: this.log })
      this.files = hubFiles(h.filesDir)
      this.forwarder = new Forwarder({ store: o.store, hub: this.hub, log: this.log })
      this.bindMirror = new BindingsMirror({ store: o.store, hub: this.hub, log: this.log })
      this.presence = new PresenceMirror({ hub: this.hub, boardFile: join(o.paths.stateDir, 'presence-board.json'), log: this.log,
        seen: () => o.store.setMeta(COMPANION_SEEN, String(nowS())) })
      this.hubAp = new HubApprovals(this.hub, () => (this.o.now ?? Date.now)() / 1000)
      this.jobs = new RemoteJobs(this.hub, this.log)
      this.queue = hubQueue(this.hub, o.store, this.log)
    }
    this.inboxDir = this.files ? this.files.inDir : o.paths.inboxDir
    this.quiet = new QuietClock(quietZone(env, home), tripclockArgv(env, home), { log: this.log, pinned: quietZonePinned(env) })
    this.sleepin = new SleepinGuard(o.cfg.sleepin ? sleepinArgv(env, home) : null, { log: this.log })
    this.buttonsFile = o.cfg.compatButtonsFile ?? o.paths.buttonsFile
    const injected = (o.apiOptions as { fetch?: typeof fetch } | undefined)?.fetch
    this.fetchFile = (url, timeoutMs) => injected
      ? injected(url, { method: 'GET', signal: AbortSignal.timeout(timeoutMs) } as any)
      : fetch(url, { signal: AbortSignal.timeout(timeoutMs), ...(o.cfg.apiUnixSocket ? { unix: o.cfg.apiUnixSocket } : {}) } as any)
    this.guard = this.files // hub mode: only the shared files dir
      ? { deny: ['/'], allow: [this.files.dir], files: [o.cfg.tokenFile, o.cfg.hub!.tokenFile, o.paths.config, ...(o.cfg.allowFromFile ? [o.cfg.allowFromFile] : [])] }
      : fileGuard({ home, configDir: o.paths.configDir, stateRoot: o.paths.stateRoot, inboxDir: o.paths.inboxDir,
        tokenFile: o.cfg.tokenFile, allowFromFile: o.cfg.allowFromFile, configFile: o.paths.config })
    this.drafts = new Drafts({ tg: this.tg, log: this.log, withSend: (c, fn) => this.typing.withSend(c, false, fn) })
    this.typing = new Typing({ tg: this.tg, boardFile: this.presence ? join(o.paths.stateDir, 'presence-board.json') : o.cfg.statusState, enabled: o.cfg.send && o.cfg.typing, streaming: c => this.drafts.has(c) })
    this.prep = new InboundPrep({
      store: o.store, tg: this.tg, bot: () => this.me?.username ?? o.cfg.bot, access: () => this.access(),
      download: (file_id, dir, prefix, timeoutMs) => this.download(file_id, dir, prefix, timeoutMs),
      env, home, transcribe: o.cfg.transcribe, scanPrivate: o.cfg.scanPrivate, canSend: o.cfg.send,
      withSend: (c, fn) => this.typing.withSend(c, false, fn), clean: s => this.clean(s), log: this.log,
      shown: r => this.shown(r), preparing: (c, d) => this.typing.preparing(c, d),
      ...(this.jobs ? {
        remote: {
          transcribe: (ref: string) => this.jobs!.runLocalLike('transcribe', { file: ref }, TRANSCRIBE_TIMEOUT_MS),
          scan: (ref: string, name: string, title: string) => this.jobs!.runLocalLike('scan', { file: ref, name, title }, SCAN_TIMEOUT_MS),
        },
        ref: (p: string) => toRef(this.files!, p),
        refPath: (r: string) => fromRef(this.files!, r),
      } : {}),
    })
    this.binder = new Binder({
      store: o.store, tg: this.tg, cfg: o.cfg, paths: o.paths, env, secret: v => v.includes(o.token), clean: s => this.clean(s), log: this.log,
      withSend: (c, fn) => this.typing.withSend(c, false, fn),
      remember: (c, m, text) => this.remember(c, m, { by: 'bot', text: truncate(text.replace(/\s+/g, ' ').trim(), 200) }),
      typing: c => this.typing.event(c),
      countCompat: !this.hub,
      ...(this.jobs ? {
        remote: job => this.jobs!.runLocalLike('bind', job, job.timeout_ms),
        pictureRoots: [this.files!.outDir],
        resolveFile: (s: string) => fromRef(this.files!, s),
      } : {}),
    })
    this.approvals = new Approvals(o.paths.approvalsDb, () => (this.o.now ?? Date.now)() / 1000)
  }

  /** getFile + download (Bot API file URL; the token never leaves this process). */
  download(file_id: string, dir: string | null = null, prefix = '', timeoutMs = 60_000): Promise<string> {
    const root = this.o.cfg.apiRoot
    return downloadFile(this.tg, this.fetchFile, fp => `${root}/file/bot${this.o.token}/${fp}`, file_id, dir ?? this.inboxDir, prefix, timeoutMs)
      .catch(err => { throw new Error(this.clean(errText(err))) })
  }

  /** A row became visible (status new): the compat inbound journal. */
  shown(r: { id: number; kind: string; payload: Record<string, unknown> }): void {
    this.forwarder?.kick()
    const file = this.o.cfg.compatJournal
    if (!file || r.kind.startsWith('outbox.')) return
    try {
      const uid = r.payload.user_id != null ? String(r.payload.user_id) : ''
      journalAppend(file, journalEntry(r.kind, r.payload, !!uid && this.access().allowFrom.includes(uid)))
    } catch (err) { this.log(`compat journal write failed: ${errText(err)}`) }
  }

  /** The outbox rules as the consumer sees them (config + the background lookups). */
  rules(): Rules {
    return {
      quietHours: this.o.cfg.quietHours,
      zone: () => this.presence?.fresh()?.zone || this.quiet.zone(),
      sleeping: () => { const p = this.presence?.fresh(); return p && typeof p.sleeping === 'boolean' ? p.sleeping : this.sleepin.active() },
      dedupeWindowS: this.o.cfg.dedupeWindowS,
      ...(this.o.rules ?? {}),
    }
  }

  /** Error text that's safe to hand on: one line, short, never the token. */
  clean(s: string): string {
    const one = s.split(this.o.token).join('<token>').replace(/\s+/g, ' ').trim()
    return one.length <= 300 ? one : one.slice(0, 299) + '…'
  }

  get offset(): number {
    return Number(this.o.store.getMeta(this.offsetKey) ?? 0) || 0
  }

  access(): Access {
    return loadAccess(this.o.cfg)
  }

  // --- start interlocks -----------------------------------------------------------

  checkSwitch(): void {
    const f = switchOn(this.o.paths)
    if (f) throw new StopError(`switch file present (${f}): staying down`, EXIT.off)
  }

  /** getMe must name the configured bot (the guard against ever polling the live bot). */
  async checkIdentity(): Promise<UserFromGetMe> {
    let me: UserFromGetMe
    try {
      me = await this.api.getMe()
    } catch (err) {
      if (err instanceof GrammyError && err.error_code === 401) throw new StopError('getMe: 401 Unauthorized (bad token)', EXIT.unauthorized)
      throw err
    }
    if (me.username.toLowerCase() !== this.o.cfg.bot.toLowerCase()) {
      throw new StopError(`getMe says @${me.username}, config says @${this.o.cfg.bot}: refusing to start`, EXIT.wrongBot)
    }
    if (String(me.id) !== this.botId) throw new StopError('getMe id does not match the token: refusing to start', EXIT.wrongBot)
    return me
  }

  claimPid(): void {
    const file = this.o.paths.pidFile
    try {
      const [first, second] = readFileSync(file, 'utf8').split('\n')
      const pid = parseInt(first ?? '', 10)
      if (pid > 1 && pid !== process.pid && (second ?? '').trim() === PID_MARKER && isAlive(pid)) {
        throw new StopError(`already running (pid ${pid})`, EXIT.running)
      }
      rmSync(file, { force: true })
    } catch (err) {
      if (err instanceof StopError) throw err
    }
    let fd: number
    try {
      mkdirSync(this.o.paths.stateDir, { recursive: true, mode: 0o700 })
      fd = openSync(file, 'wx', 0o600)
    } catch {
      throw new StopError('could not claim the PID file (another instance starting?)', EXIT.running)
    }
    writeSync(fd, `${process.pid}\n${PID_MARKER}\n`)
    closeSync(fd)
    this.ownsPid = true
  }

  releasePid(): void {
    if (!this.ownsPid) return
    try {
      if (parseInt(readFileSync(this.o.paths.pidFile, 'utf8'), 10) === process.pid) rmSync(this.o.paths.pidFile)
    } catch {}
    this.ownsPid = false
  }

  // --- run -------------------------------------------------------------------------

  /** Interlocks, then poll until stop() / the switch / a fatal error. Resolves with an exit code. */
  async run(): Promise<number> {
    try {
      this.checkSwitch()
      this.me = await this.checkIdentity()
      this.claimPid()
      if (this.files) ensureHubFiles(this.files)
      try {
        const stale = this.queue ? await this.queue.interrupted(this.outboxKeys()) : failInterrupted(this.o.store, this.outboxKeys())
        if (stale) this.log(`${stale} outbox row(s) were interrupted while sending: marked failed`)
      } catch (err) { this.log(`interrupted outbox rows not checked (${this.clean(errText(err))}); the next start does it`) }
      mkdirSync(this.inboxDir, { recursive: true, mode: 0o700 })
      this.prep.sweep(this.inboxDir)
      const pending = this.prep.recover()
      if (pending) this.log(`${pending} inbound row(s) were still being prepared: released as interrupted`)
      this.typing.start()
      if (this.hub) {
        this.forwarder!.start()
        await this.bindMirror!.sync()
        this.bindMirror!.start()
        this.presence!.start()
      }
      this.log(`polling as @${this.me.username} (mode ${this.o.cfg.mode}, offset ${this.offset}, send ${this.o.cfg.send ? 'on' : 'off'}, bindings ${this.binder.enabled() ? 'on' : 'off'}${this.hub ? `, hub ${new URL(this.hub.url).host}${this.o.cfg.hub!.schema ? ` schema ${this.o.cfg.hub!.schema}` : ''}` : ''})`)
      if (this.o.cfg.send) {
        this.quiet.start()
        this.sleepin.start()
        await Promise.all([this.quiet.ready(), this.sleepin.ready()])
        this.startOutbox()
        void this.sweepApprovals(true) // cards that expired while it was down
      }
      await this.pollLoop()
      return EXIT.ok
    } catch (err) {
      if (err instanceof StopError) {
        this.log(err.message)
        return err.code
      }
      this.log(`fatal: ${errText(err)}`)
      return EXIT.error
    } finally {
      this.stopOutbox()
      this.quiet.stop()
      this.sleepin.stop()
      this.typing.stop()
      this.drafts.stopAll()
      await this.binder.stop().catch(() => {})
      if (this.outboxBusy) await this.outboxBusy.catch(() => {})
      if (this.approvalSweeping) await this.approvalSweeping.catch(() => {})
      this.approvals.close()
      this.prep.stop()
      this.bindMirror?.stop()
      await this.presence?.stop()
      if (this.forwarder) await this.forwarder.stop().catch(() => {})
      this.releasePid()
    }
  }

  stop(why = 'stop requested'): void {
    if (this.stopping) return
    this.stopping = true
    this.log(`stopping (${why})`)
    this.abort.abort()
  }

  private async pollLoop(): Promise<void> {
    for (let attempt = 0; !this.stopping;) {
      if (switchOn(this.o.paths)) { this.stop('switch file present'); break }
      let updates: Update[]
      try {
        updates = await this.api.getUpdates({
          offset: this.offset || undefined,
          timeout: this.o.cfg.pollTimeoutS,
          allowed_updates: ALLOWED_UPDATES as any,
        }, this.abort.signal as any) // grammY types the abort-controller shim
        attempt = 0
      } catch (err) {
        if (this.stopping) break
        if (err instanceof GrammyError && err.error_code === 409) {
          throw new StopError(`409 Conflict on getUpdates (${this.clean(err.description)}): another poller or a webhook holds this token; stopping, not fighting for it`, EXIT.conflict)
        }
        if (err instanceof GrammyError && err.error_code === 401) throw new StopError('getUpdates: 401 Unauthorized', EXIT.unauthorized)
        attempt++
        const delay = Math.min(1000 * attempt, 15_000)
        this.log(`polling error: ${errText(err)}; retrying in ${delay / 1000}s`)
        await this.sleep(delay)
        continue
      }
      this.o.store.setMeta('last_poll_ts', String(nowS()))
      if (updates.length) await this.handleBatch(updates)
    }
  }

  private sleep(ms: number): Promise<void> {
    return new Promise(r => {
      const t = setTimeout(r, ms)
      this.abort.signal.addEventListener('abort', () => { clearTimeout(t); r() }, { once: true })
    })
  }

  /** Answer every callback query in the batch at once, then commit the updates in order. A press bound to a tool
   *  (bind.ts) gets the binding's toast (or BUSY_TOAST while it still runs) and is claimed here. */
  async handleBatch(updates: Update[]): Promise<void> {
    const access = this.access()
    const t0 = performance.now()
    const acks = new Map<number, Promise<CallbackAck>>()
    const presses = new Map<number, Press>()
    for (const u of updates) {
      if (!u.callback_query || u.update_id < this.offset) continue
      const info = callbackInfo(this.ctxFor(u), access)
      let toast = info && !info.data.startsWith('perm:') ? callbackToast(info.allowed, info.label) : undefined
      const msg = u.callback_query.message as any
      const ap = info?.allowed ? parseApprovalData(info.data) : null
      if (ap && this.approvalsOn()) { // P7: decided here, before the answer, so the toast can say what happened
        const tap = Promise.resolve(this.approvalTap(ap, String(u.callback_query.from.id), String(msg?.chat?.id ?? ''), access))
        presses.set(u.update_id, { approval: tap })
        const cbId = u.callback_query.id
        acks.set(u.update_id, tap.then(t => this.answerCallback(cbId, t.toast, t0)))
        continue
      } else if (info?.allowed && !info.data.startsWith('perm:') && msg?.chat?.type === 'private') {
        const b = this.bindingFor(() => this.binder.forButton(String(msg.chat.id), msg.message_id, info.data))
        if (b?.gated) presses.set(u.update_id, { gated: b })
        else if (b && !this.binder.claim(b)) { toast = BUSY_TOAST; presses.set(u.update_id, { busy: true }) }
        else if (b) { toast = this.binder.toast(b, info.label); presses.set(u.update_id, { bound: b }) }
      }
      acks.set(u.update_id, this.answerCallback(u.callback_query.id, toast, t0))
    }
    for (const u of updates) {
      if (u.update_id < this.offset) continue // already committed (a re-delivered update)
      const ack = acks.has(u.update_id) ? await acks.get(u.update_id)! : undefined
      const press = presses.get(u.update_id)
      if (press?.approval) {
        await this.commitApproval(u, await press.approval, access, ack).catch(err => this.log(`approval tap: ${errText(err)}`))
        continue
      }
      if (press?.busy) { // 0.1.22: a tap while its instant button runs only gets the toast
        this.o.store.commitUpdate(null, this.offsetKey, u.update_id + 1)
        this.o.store.bumpMeta('dropped:bound busy')
        continue
      }
      const res = toInbound(u, this.api, {
        me: this.me!, botId: this.botId, mode: this.o.cfg.mode, access,
        recent: (c, m) => this.recentFor(c, m),
        poll: id => this.pollFor(id),
        now: this.o.now,
      }, ack)
      const row = res.row
      const chat = row ? String(row.payload.chat_id ?? '') : ''
      let bound: Binding | null = press?.bound ?? null
      let arg = ''
      const text = (u.message as any)?.text
      if (row?.kind === 'tg.message' && typeof text === 'string' && u.message!.chat.type === 'private' && text.startsWith('/')) {
        const m = this.bindingFor(() => this.binder.forCommand(text, this.me!.username))
        if (m?.b.gated) row.payload.binding_gated = m.b.gated
        else if (m) { bound = m.b; arg = m.arg; row.payload.handled = m.b.name }
      }
      if (row && press?.gated) row.payload.binding_gated = press.gated.gated
      if (bound && row) {
        const id = this.o.store.commitUpdate(row, this.offsetKey, u.update_id + 1, 'new', true)
        if (id == null) {
          this.o.store.bumpMeta('duplicates')
          if (bound.kind === 'button') this.binder.release(bound)
          continue
        }
        this.shown({ id, kind: row.kind, payload: row.payload })
        if (res.remember) this.remember(res.remember.chat_id, res.remember.message_id, res.remember.recent)
        const p = row.payload
        const origin: Origin = {
          id, idem: row.idem_key, trace: row.trace_id, chat_id: chat, message_id: Number(p.message_id), user: String(p.user ?? ''),
          user_id: String(p.user_id ?? ''), ts: String(p.ts ?? ''), text: typeof text === 'string' ? text : String(p.text ?? ''),
          ...(bound.kind === 'button' ? { data: String(p.callback_data ?? ''), label: String(p.button_text ?? '') } : {}),
        }
        if (bound.kind === 'button') this.binder.startButton(bound, origin)
        else this.binder.startCommand(bound, arg, origin)
        continue
      }
      if (press?.bound) this.binder.release(press.bound) // no row after all (cannot happen for an allowed press)
      const job = row && u.message ? this.prep.plan(u.message, row, this.ctxFor(u), this.me!.username) : null
      const pending = !!row && (job != null || this.prep.busy(chat))
      const id = this.o.store.commitUpdate(row, this.offsetKey, u.update_id + 1, pending ? 'pending' : 'new')
      if (!row) this.o.store.bumpMeta(`dropped:${res.drop ?? 'other'}`)
      else if (id == null) this.o.store.bumpMeta('duplicates')
      else if (pending) this.prep.track(id, row.kind, row.payload, job)
      else this.shown({ id, kind: row.kind, payload: row.payload })
      if (res.remember && row?.payload.handled !== 'scan-private') this.remember(res.remember.chat_id, res.remember.message_id, res.remember.recent)
      if (row && id != null && row.payload.handled == null) this.afterRow(u, row.kind, chat)
      if (row?.kind === 'tg.button') await this.markPressed(u).catch(err => this.log(`marking the pressed button failed: ${errText(err)}`))
    }
  }

  /** A binding lookup that never breaks the poll loop (a lookup error: unbound, logged). */
  private bindingFor<T>(fn: () => T | null): T | null {
    try { return fn() } catch (err) { this.log(`binding lookup failed: ${errText(err)}`); return null }
  }

  /** Typing for owner messages and presses, the ack reaction, a stopped draft. */
  private afterRow(u: Update, kind: string, chat: string): void {
    if (kind === 'tg.message' || kind === 'tg.button') this.typing.event(chat)
    if (kind === 'tg.stop') this.drafts.stopped(chat, Number((u as any).stopped_message_generation?.draft_id))
    const ack = kind === 'tg.message' && this.o.cfg.send ? this.access().ackReaction : undefined
    if (ack && u.message) void this.tg('setMessageReaction', { chat_id: chat, message_id: u.message.message_id, reaction: [{ type: 'emoji', emoji: ack }] }).catch(() => {})
  }

  /** grammY's Context, only to read the update (no API calls). */
  private ctxFor(u: Update): Context {
    return new Context(u, this.api, this.me!)
  }

  private async answerCallback(id: string, text: string | undefined, t0: number): Promise<CallbackAck> {
    try {
      await this.api.answerCallbackQuery(id, text ? { text } : undefined)
      return { ack_ms: Math.round(performance.now() - t0), ack_ok: true }
    } catch (err) {
      return { ack_ms: Math.round(performance.now() - t0), ack_ok: false, ack_error: this.clean(errText(err)) }
    }
  }

  /** The plugin's on_press handling after a press: 'mark' (default) appends "✓ label" and removes
   *  the keyboard, 'remove' only removes it, 'keep' leaves the message alone. */
  private async markPressed(u: Update, force?: OnPress): Promise<void> {
    const cq = u.callback_query!
    const msg = cq.message as any
    if (!msg) return
    const chat_id = String(msg.chat.id)
    const key = `${chat_id}:${msg.message_id}`
    const onPress = force ?? lookupPress(this.buttonsFile, key) ?? 'mark'
    if (onPress === 'keep') return
    const rows = (msg.reply_markup?.inline_keyboard ?? []) as { text: string; callback_data?: string }[][]
    const label = rows.flat().find(b => b.callback_data === cq.data)?.text ?? String(cq.data)
    const suffix = `\n\n✓ ${label}`
    const empty = { inline_keyboard: [] }
    const rich = msg.text == null && msg.caption == null ? this.sentRichText(chat_id, msg.message_id) : null
    const marked =
      onPress !== 'mark' ? Promise.reject(new Error('remove only'))
      : msg.text != null ? this.tg('editMessageText', { chat_id, message_id: msg.message_id, text: msg.text + suffix, ...(msg.entities ? { entities: msg.entities } : {}), reply_markup: empty })
      : msg.caption != null ? this.tg('editMessageCaption', { chat_id, message_id: msg.message_id, caption: msg.caption + suffix, ...(msg.caption_entities ? { caption_entities: msg.caption_entities } : {}), reply_markup: empty })
      : rich != null ? this.tg('editMessageText', { chat_id, message_id: msg.message_id, ...textBody(rich + suffix, 'rich'), reply_markup: empty })
      : Promise.reject(new Error('no text'))
    await marked.catch(() =>
      this.tg('editMessageReplyMarkup', { chat_id, message_id: msg.message_id, reply_markup: empty }).catch(() => {}),
    )
    try { forgetPress(this.buttonsFile, key) } catch {}
  }

  /** The markdown of a rich message the outbox sent (the "✓ choice" edit needs it: Telegram's
   *  callback message carries no text for rich messages). */
  private sentRichText(chat_id: string, message_id: number): string | null {
    const r = this.o.store.db.query("SELECT body FROM outbox WHERE chat = ? AND tg_message_id = ? AND op IN ('send', 'edit') AND status = 'sent' ORDER BY id DESC LIMIT 1")
      .get(chat_id, message_id) as { body: string } | null
    if (!r) return null
    try {
      const b = JSON.parse(r.body)
      return b.format === 'rich' && typeof b.text === 'string' && b.details == null ? b.text : null
    } catch { return null }
  }

  // --- approvals (P7) ----------------------------------------------------------------

  /** The switch file approvals-off (TG_ADAPTER_APPROVALS_OFF) is absent: ap: taps are decided here. */
  approvalsOn(): boolean {
    return !existsSync((this.o.env ?? process.env).TG_ADAPTER_APPROVALS_OFF || join(this.o.paths.configDir, APPROVALS_OFF))
  }

  /** The owner: the first allowFrom entry (his DM's chat id is his user id). */
  owner(access: Access = this.access()): string {
    return access.allowFrom[0] ?? ''
  }

  private approvalTap(ap: { nonce: string; choice: 'y' | 'n' | 'x' }, userId: string, chatId: string, access: Access): Tap | Promise<Tap> {
    const fail = (err: unknown): Tap => {
      this.log(`approval ${ap.nonce.slice(0, 8)}…: tap not recorded (${this.clean(errText(err))})`)
      return { nonce: ap.nonce, choice: ap.choice, outcome: 'error', repeat: false, row: null, toast: TOAST.error }
    }
    if (this.hubAp) {
      const t = this.hubAp.tap(ap.nonce, ap.choice, { userId, chatId, owner: this.owner(access) })
      return Promise.race([t, Bun.sleep(HUB_TAP_MS).then(() => { throw new Error(`the hub did not answer in ${HUB_TAP_MS / 1000} s`) })]).catch(fail)
    }
    try {
      return this.approvals.tap(ap.nonce, ap.choice, { userId, chatId, owner: this.owner(access) })
    } catch (err) {
      return fail(err)
    }
  }

  private async markCard(nonce: string, state: 'edited' | 'failed'): Promise<void> {
    if (!this.hubAp) return this.approvals.markCard(nonce, state)
    await this.hubAp.markCard(nonce, state).catch(err => this.log(`approval ${nonce.slice(0, 8)}…: card state not saved (${this.clean(errText(err))})`))
  }

  /** His tap on an ap: button: an approval.decided row (the press row reshaped: kind, event_type, nonce, decision,
   *  outcome, summary, ref, expires, text), then the card: "✓ choice" after a decision, one inert button when the
   *  approval had expired or was replaced. A tap that isn't his (or on the inert button) only moves the offset. */
  private async commitApproval(u: Update, tap: Tap, access: Access, ack?: CallbackAck): Promise<void> {
    const msg = u.callback_query!.message as any
    if (tap.outcome === 'not owner' || tap.choice === 'x') {
      this.o.store.commitUpdate(null, this.offsetKey, u.update_id + 1)
      this.o.store.bumpMeta(tap.choice === 'x' ? 'dropped:approval expired button' : 'dropped:approval not his')
      return
    }
    const res = toInbound(u, this.api, {
      me: this.me!, botId: this.botId, mode: this.o.cfg.mode, access,
      recent: (c, m) => this.recentFor(c, m), poll: id => this.pollFor(id), now: this.o.now,
    }, ack)
    const row = res.row
    if (!row) {
      this.o.store.commitUpdate(null, this.offsetKey, u.update_id + 1)
      this.o.store.bumpMeta(`dropped:${res.drop ?? 'other'}`)
      return
    }
    const r = tap.row
    row.kind = 'approval.decided'
    row.subject = truncate(`approval ${tap.outcome}${r ? `: ${r.summary}` : ''}`, 120)
    row.next = tap.outcome === 'approved' && !tap.repeat ? 'the flow that asked uses it once: approvals consume <nonce> --action "<the same action>"'
      : tap.outcome === 'declined' && !tap.repeat ? 'he declined: stop that action' : 'nothing approved by this tap'
    Object.assign(row.payload, {
      event_type: 'approval', nonce: tap.nonce, decision: tap.choice === 'y' ? 'yes' : 'no', outcome: tap.outcome,
      ...(tap.repeat ? { repeat: '1' } : {}),
      ...(r ? { summary: r.summary, expires: new Date(r.expires * 1000).toISOString(), ...(r.ref ? { ref: r.ref } : {}) } : {}),
      text: approvalText(tap),
    })
    const chat = String(row.payload.chat_id ?? '')
    const id = this.o.store.commitUpdate(row, this.offsetKey, u.update_id + 1)
    if (id == null) { this.o.store.bumpMeta('duplicates'); return }
    this.shown({ id, kind: row.kind, payload: row.payload })
    this.afterRow(u, 'tg.button', chat)
    if (tap.outcome === 'error') return // nothing recorded: the keyboard stays for another tap
    if ((tap.outcome === 'approved' || tap.outcome === 'declined') && !tap.repeat) {
      await this.markPressed(u, 'mark').catch(err => this.log(`marking the approval card failed: ${errText(err)}`))
    } else if (tap.outcome === 'approved' || tap.outcome === 'declined') {
      await this.tg('editMessageReplyMarkup', { chat_id: chat, message_id: msg.message_id, reply_markup: { inline_keyboard: [] } }).catch(() => {})
    } else {
      const ok = await this.inertCard(chat, msg.message_id, tap.nonce, tap.outcome === 'cancelled' ? REPLACED_LABEL : EXPIRED_LABEL)
      if (r && tap.outcome === 'expired' && r.card == null && Number(r.message_id) === msg.message_id) await this.markCard(tap.nonce, ok ? 'edited' : 'failed')
    }
  }

  /** The card's keyboard → one inert button (its press only answers with a toast). */
  private async inertCard(chat_id: string, message_id: number, nonce: string, label: string): Promise<boolean> {
    try {
      await this.tg('editMessageReplyMarkup', { chat_id, message_id, reply_markup: { inline_keyboard: [[{ text: label, callback_data: `ap:${nonce}:x` }]] } })
      return true
    } catch (err) {
      if (/not modified/i.test(errText(err))) return true
      this.log(`approval ${nonce.slice(0, 8)}…: editing the card failed (${this.clean(errText(err))})`)
      return false
    }
  }

  /** Pending approval cards past their expiry get the inert "⌛ expired" button (send on; every APPROVAL_SWEEP_MS and
   *  at start, so cards that expired while the adapter was down are edited too). Single-flight; never throws. */
  sweepApprovals(force = false): Promise<unknown> {
    if (this.approvalSweeping) return this.approvalSweeping
    const now = Date.now()
    if (!force && now - this.approvalSweepAt < APPROVAL_SWEEP_MS) return Promise.resolve()
    this.approvalSweepAt = now
    if (!this.o.cfg.send || !this.approvalsOn() || (!this.hubAp && !this.approvals.exists())) return Promise.resolve()
    this.approvalSweeping = (async () => {
      if (this.hubAp) { // hubd expires them in one statement and names the cards to edit
        for (const r of await this.hubAp.expireDue()) {
          if (this.stopping) break
          const ok = await this.inertCard(r.chat, r.message_id, r.nonce, EXPIRED_LABEL)
          await this.markCard(r.nonce, ok ? 'edited' : 'failed')
          this.o.store.bumpMeta(ok ? 'approvals:expired cards' : 'approvals:expiry edit failed')
        }
        return
      }
      for (const r of this.approvals.due()) {
        if (this.stopping) break
        if (!this.approvals.expire(r.nonce)) continue // decided meanwhile
        const ok = await this.inertCard(r.chat!, r.message_id!, r.nonce, EXPIRED_LABEL)
        this.approvals.markCard(r.nonce, ok ? 'edited' : 'failed')
        this.o.store.bumpMeta(ok ? 'approvals:expired cards' : 'approvals:expiry edit failed')
      }
      this.approvals.sweepStale()
    })().then(() => this.repeated('approval sweep', null), err => this.repeated('approval sweep', errText(err)))
      .finally(() => { this.approvalSweeping = null })
    return this.approvalSweeping
  }

  /** A send row with an `approval` field: {action, summary, ttl_s?, ref?, yes?, no?} makes a nonce here, {nonce} uses
   *  one made with `approvals new` (an action given with it must match). Only to his DM, with inline buttons. The
   *  stored body keeps the nonce instead of the action (a retry reuses it); the ✅/❌ row is appended to the buttons. */
  approvalSend(row: OutboxRow, args: Record<string, unknown>): Record<string, unknown> | Promise<Record<string, unknown>> {
    if (this.hubAp) return this.approvalSendHub(row, args)
    const a = args.approval
    if (!a || typeof a !== 'object' || Array.isArray(a)) throw new Error('approval must be an object: {action, summary, ttl_s}')
    const ap = a as Record<string, unknown>
    // The action never stays in the outbox: the stored body keeps its sha256 until the nonce replaces it (a refused
    // approval fails the row with the hash only).
    if (typeof ap.action === 'string') {
      const { action, ...rest } = ap
      this.o.store.setOutbox(row.id, { body: JSON.stringify({ ...args, approval: { ...rest, action_sha: sha256(action) } }) })
    }
    const owner = this.owner()
    if (!owner || row.chat !== owner) throw new Error('approval cards go to the owner\'s private chat only')
    if (args.keyboard != null || args.remove_keyboard || args.force_reply) throw new Error('an approval card needs inline buttons: no keyboard, remove_keyboard or force_reply')
    if (args.album) throw new Error('an album cannot carry the approval buttons: send the approval card on its own')
    let r: ApprovalRow
    if (typeof ap.nonce === 'string' && ap.nonce) {
      const got = this.approvals.get(ap.nonce)
      if (!got || got.status !== 'pending' || got.expires <= this.approvals.now()) throw new Error(`approval ${ap.nonce}: not a pending approval`)
      if (typeof ap.action === 'string' && sha256(ap.action) !== got.action_sha) throw new Error('approval.action does not match its nonce')
      r = got
    } else {
      r = this.approvals.create({ action: ap.action, summary: ap.summary, ttlS: ap.ttl_s, ref: ap.ref, chat: row.chat })
    }
    const kept: Record<string, unknown> = { nonce: r.nonce, summary: r.summary, expires: r.expires, ...(r.ref ? { ref: r.ref } : {}) }
    for (const k of ['yes', 'no']) if (typeof ap[k] === 'string') kept[k] = ap[k]
    const stored = { ...args, approval: kept }
    this.o.store.setOutbox(row.id, { body: JSON.stringify(stored) })
    const given = args.buttons == null ? [] : Array.isArray(args.buttons) && args.buttons.length && !Array.isArray(args.buttons[0]) ? [args.buttons] : args.buttons as unknown[]
    if (!Array.isArray(given)) throw new Error('buttons must be an array of rows')
    return { ...stored, buttons: [...given, approvalRow(r.nonce, ap.yes, ap.no)] }
  }

  /** approvalSend in hub mode: the card must name a brain-minted nonce (checked: pending, not expired, same action
   *  when one is given). The hub's tg never mints one: a row without a nonce fails with HUB_NO_MINT. */
  private async approvalSendHub(row: OutboxRow, args: Record<string, unknown>): Promise<Record<string, unknown>> {
    const a = args.approval
    if (!a || typeof a !== 'object' || Array.isArray(a)) throw new Error('approval must be an object: {action, summary, ttl_s}')
    const ap = a as Record<string, unknown>
    const owner = this.owner()
    if (!owner || row.chat !== owner) throw new Error('approval cards go to the owner\'s private chat only')
    if (args.keyboard != null || args.remove_keyboard || args.force_reply) throw new Error('an approval card needs inline buttons: no keyboard, remove_keyboard or force_reply')
    if (args.album) throw new Error('an album cannot carry the approval buttons: send the approval card on its own')
    const hub = this.hubAp!
    const sha = typeof ap.action === 'string' ? sha256(ap.action) : typeof ap.action_sha === 'string' ? ap.action_sha : null
    const nonce = typeof ap.nonce === 'string' && ap.nonce ? ap.nonce : ''
    // The hub's tg never creates or mints approvals (Cedar: Binding may decide, never request): the brain mints the
    // nonce (`approvals new` → POST /approvals, brain token) and the card names it.
    if (!nonce) throw new Error(HUB_NO_MINT)
    const r = await hub.get(nonce)
    if (!r || r.status !== 'pending' || r.expires <= hub.now()) throw new Error(`approval ${nonce}: not a pending approval`)
    if (sha && sha !== r.action_sha) throw new Error('approval.action does not match its nonce')
    const kept: Record<string, unknown> = { nonce: r.nonce, summary: r.summary, expires: r.expires, ...(r.ref ? { ref: r.ref } : {}) }
    for (const k of ['yes', 'no']) if (typeof ap[k] === 'string') kept[k] = ap[k]
    const stored = { ...args, approval: kept }
    const given = args.buttons == null ? [] : Array.isArray(args.buttons) && args.buttons.length && !Array.isArray(args.buttons[0]) ? [args.buttons] : args.buttons as unknown[]
    if (!Array.isArray(given)) throw new Error('buttons must be an array of rows')
    return { ...stored, buttons: [...given, approvalRow(r.nonce, ap.yes, ap.no)] }
  }

  /** A sent or edited keyboard that carries ap:<nonce> buttons: record the card (chat + message id) for the expiry
   *  edit, and tell the brain the nonce (a note in the tool's answer). */
  approvalCards(chat_id: string, message_id: number, buttons: unknown): string[] | Promise<string[]> {
    if (this.hubAp) return this.approvalCardsHub(chat_id, message_id, buttons)
    const notes: string[] = []
    for (const nonce of noncesIn(buttons)) {
      let ok = false
      try { ok = chat_id === this.owner() && this.approvals.attach(nonce, chat_id, message_id) } catch (err) { this.log(`approval card: ${errText(err)}`) }
      const r = ok ? this.approvals.get(nonce) : null
      notes.push(r ? `approval ${nonce} pending until ${hm(r.expires)} (his tap arrives as an approval event)` : `approval ${nonce}: not pending: a tap on this card approves nothing`)
    }
    return notes
  }

  private async approvalCardsHub(chat_id: string, message_id: number, buttons: unknown): Promise<string[]> {
    const notes: string[] = []
    for (const nonce of noncesIn(buttons)) {
      let ok = false
      try { ok = chat_id === this.owner() && await this.hubAp!.attach(nonce, chat_id, message_id) } catch (err) { this.log(`approval card: ${this.clean(errText(err))}`) }
      const r = ok ? await this.hubAp!.get(nonce).catch(() => null) : null
      notes.push(r ? `approval ${nonce} pending until ${hm(r.expires)} (his tap arrives as an approval event)` : `approval ${nonce}: not pending: a tap on this card approves nothing`)
    }
    return notes
  }

  // --- recent messages / polls -------------------------------------------------------

  remember(chat_id: string, message_id: number, r: Recent): void {
    const key = `${chat_id}:${message_id}`
    this.recent.delete(key)
    this.recent.set(key, r)
    while (this.recent.size > RECENT_MAX) this.recent.delete(this.recent.keys().next().value!)
  }

  private recentFor(chat_id: string, message_id: number): Recent | null {
    const mem = this.recent.get(`${chat_id}:${message_id}`)
    if (mem) return mem
    const db = this.o.store.messageText(chat_id, message_id)
    if (!db) return null
    return db.by === 'bot'
      ? { by: 'bot', text: truncate(db.text.replace(/\s+/g, ' ').trim(), 200) }
      : { by: 'user', user_id: db.user_id ?? '', user: db.user ?? '', text: truncate(db.text.replace(/\s+/g, ' ').trim(), 200) }
  }

  private pollFor(id: string): PollMeta | null {
    const v = this.o.store.getMeta(`poll:${id}`)
    if (!v) return null
    try { return JSON.parse(v) as PollMeta } catch { return null }
  }

  // --- outbox ----------------------------------------------------------------------

  /** Outbox keys this adapter consumes: the bot username (CLI, tg wrapper) and the instance name (the bridge). */
  outboxKeys(): string[] {
    return [...new Set([this.o.cfg.bot, this.o.paths.instance])]
  }

  outboxDeps(): OutboxDeps {
    return {
      tg: this.tg,
      store: this.o.store,
      bot: this.o.cfg.bot,
      bots: this.outboxKeys(),
      access: () => this.access(),
      buttonsFile: this.buttonsFile,
      remember: (c, m, text) => this.remember(c, m, { by: 'bot', text: truncate(text.replace(/\s+/g, ' ').trim(), 200) }),
      clean: s => this.clean(s),
      log: this.log,
      rules: this.rules(),
      guard: this.guard,
      drafts: this.drafts,
      typing: this.typing,
      download: file_id => Promise.resolve(this.prep.takeKept(file_id)).then(p => p ?? this.download(file_id)).then(p => this.files ? toRef(this.files, p) : p),
      withSend: (c, answer, fn) => this.typing.withSend(c, answer, fn),
      binds: (c, m, buttons) => this.binder.saveCardBinds(c, m, buttons),
      menu: () => this.binder.menu(),
      approve: (row, args) => this.approvalSend(row, args),
      cards: (c, m, buttons) => this.approvalCards(c, m, buttons),
      ...(this.queue ? { queue: this.queue } : {}),
      ...(this.files ? { resolveFile: (f: string) => fromRef(this.files!, f) } : {}),
      lastOk: () => this.lastOkS,
    }
  }

  /** A failure that repeats every pass (a hub that is down): the first one, every 40th, and one line when it clears. */
  private repeated(what: string, err: string | null): void {
    const n = this.repeats.get(what) ?? 0
    if (err === null) {
      if (n > 1) this.log(`${what}: back after ${n} failed passes`)
      if (n) this.repeats.delete(what)
      return
    }
    this.repeats.set(what, n + 1)
    if (n === 0 || (n + 1) % 40 === 0) this.log(`${what} failed${n ? ` (${n + 1} in a row)` : ''}: ${err}`)
  }

  /** One outbox pass (single-flight). */
  drainOutbox(): Promise<unknown> {
    if (this.outboxBusy) return this.outboxBusy
    this.outboxBusy = drainOutbox(this.outboxDeps())
      .then(() => this.repeated('outbox pass', null), err => this.repeated('outbox pass', errText(err)))
      .finally(() => { this.outboxBusy = null })
    return this.outboxBusy
  }

  private startOutbox(): void {
    if (this.outboxTimer) return
    this.outboxTimer = setInterval(() => { if (!this.stopping) { void this.drainOutbox(); void this.sweepApprovals() } }, this.o.cfg.outboxIntervalMs)
  }

  private stopOutbox(): void {
    if (this.outboxTimer) clearInterval(this.outboxTimer)
    this.outboxTimer = null
  }
}

/** The approval event's content (the brain reads it): what he did on which card, never the action itself. */
export function approvalText(t: Tap): string {
  const r = t.row
  const what = r ? `"${r.summary}"${r.ref ? ` · ref ${r.ref}` : ''}` : 'an approval card'
  const tail = ` · nonce ${t.nonce}`
  switch (t.outcome) {
    case 'approved': return `(approval ✅ ${t.repeat ? 'already approved' : 'approved'}: ${what}${tail})`
    case 'declined': return `(approval ❌ ${t.repeat ? 'already declined' : 'declined'}: ${what}${tail})`
    case 'expired': return `(approval tap after its expiry ⌛: ${what}: nothing approved${tail})`
    case 'cancelled': return `(approval tap on a replaced card: ${what}: nothing approved${tail})`
    case 'error': return `(approval tap NOT recorded (store error): ${what}: nothing approved; he may tap again${tail})`
    default: return `(approval tap on an unknown card: nothing approved${tail})`
  }
}

function isAlive(pid: number): boolean {
  try { process.kill(pid, 0) } catch { return false }
  try {
    const args = readFileSync(`/proc/${pid}/cmdline`, 'utf8')
    return /tg-adapter|cli\.ts/.test(args)
  } catch {
    return true
  }
}

