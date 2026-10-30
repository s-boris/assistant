// The MockBot as a standalone process for the Python e2e (tests/tg/test_e2e_bun.py):
//   bun test/mockserve.ts SOCK CALLS_JSON [TEXT]
// serves the mock Bot API on the unix socket SOCK (no TCP port), queues one private text message
// from the owner (TEXT, default "ping"), and rewrites CALLS_JSON (method + params of every call
// except getUpdates) every 100 ms until SIGTERM.
import { writeFileSync } from 'fs'
import { MockBot, OWNER } from './mockbot.ts'

const [sock, out, text] = process.argv.slice(2)
if (!sock || !out) {
  process.stderr.write('usage: bun test/mockserve.ts SOCK CALLS_JSON [TEXT]\n')
  process.exit(2)
}
const mock = new MockBot()
mock.text(OWNER, text ?? 'ping')
mock.serveUnix(sock)
const dump = () => writeFileSync(out, JSON.stringify(mock.calls.map(c => ({ method: c.method, params: c.params }))))
dump()
const timer = setInterval(dump, 100)
for (const s of ['SIGTERM', 'SIGINT'] as const) {
  process.on(s, () => { clearInterval(timer); dump(); mock.stop(); process.exit(0) })
}
