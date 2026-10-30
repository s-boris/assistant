// Where tg-adapter keeps its config, state and switch files, per instance (Phase 1 §3):
//   config  ~/.config/tg-adapter/<instance>.json   (test: config.json, the Phase 0 name, while test.json is absent)
//   switch  ~/.config/tg-adapter/<instance>.off    (test: the Phase 0 switch ~/.config/tg-adapter/off counts too)
//   state   ~/.local/state/tg-adapter/<instance>/  (adapter.pid, buttons.json, inbox/ 0700, adapter.log, ensure.log)
//   db      test: ~/.local/state/tg-adapter/test.db (the Phase 0 DB) · live: the main events DB
//           ~/.local/state/assistant/events.db
//   approvals (P7)  live: ~/.local/state/assistant/approvals.db (beside the events DB) · test: <state>/approvals.db
// All of it is $HOME-relative and overridable by env (tests): TG_ADAPTER_CONFIG, TG_ADAPTER_OFF_FILE,
// TG_ADAPTER_STATE_DIR (the root holding the instance dirs), ASSISTANT_EVENTS_DB (the main events DB), APPROVALS_DB.
// Under ASSISTANT_TEST=1 or TG_ADAPTER_TEST=1 a path inside the real home directory (the passwd entry, not
// $HOME) is refused, like core.paths does for the Python side.
import { existsSync, readFileSync } from 'fs'
import { homedir } from 'os'
import { join, resolve, sep } from 'path'
import { approvalsDbFor } from './approvals.ts'

export const INSTANCES = ['test', 'live'] as const
export type Instance = (typeof INSTANCES)[number]

export type Paths = {
  home: string
  instance: Instance
  /** ~/.config/tg-adapter/<instance>.json (TG_ADAPTER_CONFIG) */
  config: string
  /** ~/.config/tg-adapter/<instance>.off (TG_ADAPTER_OFF_FILE); test also honours the Phase 0 `off`. */
  offFiles: string[]
  /** ~/.config/tg-adapter: the adapter's config dir (refused as a file source). */
  configDir: string
  /** ~/.local/state/tg-adapter (TG_ADAPTER_STATE_DIR): the instances' parent and the Phase 0 test.db. */
  stateRoot: string
  /** <stateRoot>/<instance>: adapter.pid, buttons.json, inbox/. */
  stateDir: string
  pidFile: string
  buttonsFile: string
  /** Downloads (photos, download op, transcribed audio): 0700. */
  inboxDir: string
  /** The DB this instance writes unless the config names one. */
  defaultDb: string
  /** The main events DB (live mode must write there). */
  eventsDb: string
  /** P7: the approvals store (live: beside the main events DB; test: in the instance's state dir). */
  approvalsDb: string
}

type Env = Record<string, string | undefined>

export const expandHome = (p: string, home: string) => p === '~' ? home : p.replace(/^~(?=\/)/, home)

export function isTestRun(env: Env = process.env): boolean {
  return env.ASSISTANT_TEST === '1' || env.TG_ADAPTER_TEST === '1'
}

/** The real home directory from the password database (never $HOME, which tests point elsewhere; Bun's
 *  os.userInfo() follows $HOME), which tests must never touch: /etc/passwd, else the nss_wrapper passwd file
 *  (NSS_WRAPPER_PASSWD: the hub CI runner's uid has no /etc/passwd entry), else ASSISTANT_REAL_HOME, as
 *  core.paths.real_home() does on the Python side. */
export function realHome(): string {
  const uid = String(process.getuid?.() ?? '')
  for (const file of ['/etc/passwd', process.env.NSS_WRAPPER_PASSWD]) {
    if (!file) continue
    try {
      for (const line of readFileSync(file, 'utf8').split('\n')) {
        const f = line.split(':')
        if (f[2] === uid && f[5]) return f[5]
      }
    } catch {}
  }
  return process.env.ASSISTANT_REAL_HOME || ''
}

function inside(p: string, dir: string): boolean {
  if (!dir) return false
  const a = resolve(p), d = resolve(dir)
  return a === d || a.startsWith(d.endsWith(sep) ? d : d + sep)
}

/** Local CI run dirs under the real home (ops/assistant.py CI_ROOT, core.paths.ci_scratch()): throwaway, so a
 *  test run may use paths strictly inside them. */
const CI_SCRATCH = ['.cache', 'aci']

function strictlyInside(p: string, dir: string): boolean {
  return !!dir && inside(p, dir) && resolve(p) !== resolve(dir)
}

export function parseInstance(v: string | undefined): Instance {
  const i = (v ?? 'test').trim()
  if (!(INSTANCES as readonly string[]).includes(i)) throw new Error(`--instance must be one of ${INSTANCES.join('|')}`)
  return i as Instance
}

export function resolvePaths(env: Env = process.env, instance: Instance = 'test'): Paths {
  const home = env.HOME || homedir()
  const r = (p: string) => resolve(expandHome(p, home))
  const configDir = join(home, '.config', 'tg-adapter')
  let config: string
  if (env.TG_ADAPTER_CONFIG) config = r(env.TG_ADAPTER_CONFIG)
  else {
    config = join(configDir, `${instance}.json`)
    const legacy = join(configDir, 'config.json')
    if (instance === 'test' && !existsSync(config) && existsSync(legacy)) config = legacy
  }
  const offFiles = env.TG_ADAPTER_OFF_FILE
    ? [r(env.TG_ADAPTER_OFF_FILE)]
    : [join(configDir, `${instance}.off`), ...(instance === 'test' ? [join(configDir, 'off')] : [])]
  const stateRoot = r(env.TG_ADAPTER_STATE_DIR || join(home, '.local', 'state', 'tg-adapter'))
  const stateDir = join(stateRoot, instance)
  const eventsDb = r(env.ASSISTANT_EVENTS_DB || join(home, '.local', 'state', 'assistant', 'events.db'))
  const paths: Paths = {
    home, instance, config, offFiles, configDir: resolve(configDir), stateRoot, stateDir,
    pidFile: join(stateDir, 'adapter.pid'),
    buttonsFile: join(stateDir, 'buttons.json'),
    inboxDir: join(stateDir, 'inbox'),
    defaultDb: instance === 'test' ? join(stateRoot, 'test.db') : eventsDb,
    eventsDb,
    approvalsDb: approvalsDbFor(env, instance, eventsDb, stateDir, r),
  }
  if (isTestRun(env)) {
    const real = realHome()
    const scratch = real ? join(real, ...CI_SCRATCH) : ''
    const approvalsDb = paths.approvalsDb
    for (const [name, p] of Object.entries({ config, stateRoot, eventsDb, approvalsDb, ...Object.fromEntries(offFiles.map((f, i) => [`off${i}`, f])) })) {
      if (inside(p, real) && !strictlyInside(p, scratch)) throw new Error(`test run refuses a path under the real home: ${name}`)
    }
  }
  return paths
}

/** The first switch file that exists (null: none). */
export const switchOn = (p: Paths): string | null => p.offFiles.find(f => existsSync(f)) ?? null

export { inside as isInside }
