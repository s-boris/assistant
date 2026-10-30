// Test-only probe of the test Chrome (fake data): {cdp, url, op: values|framevalues|styles|boxes|state|snapshot|screenshot, path?} -> JSON.
import { createRequire } from 'node:module';
import fs from 'node:fs';
const require = createRequire(import.meta.url);
const { chromium } = require(`${process.env.HOME}/opt/browser-mcp/node_modules/playwright-core`);
const inp = JSON.parse(fs.readFileSync(0, 'utf8'));
const b = await chromium.connectOverCDP(inp.cdp);
const page = b.contexts().flatMap((c) => c.pages()).find((p) => p.url().split('#')[0] === inp.url);
if (!page) { console.log(JSON.stringify({ error: 'no page' })); process.exit(1); }
const out = {};
if (inp.op === 'values' || inp.op === 'styles') {
  for (const fr of page.frames()) {
    out[await fr.evaluate(() => location.host)] = await fr.evaluate(() => Object.fromEntries([...document.querySelectorAll('input,select')].map((e) => [e.name, [e.value, getComputedStyle(e).webkitTextSecurity]])));
  }
} else if (inp.op === 'framevalues') {             // every frame by its path + query: {name: [value, checked]}
  for (const fr of page.frames()) {
    out[await fr.evaluate(() => location.pathname + location.search)] = await fr.evaluate(() => Object.fromEntries([...document.querySelectorAll('input,select')].filter((e) => e.name).map((e) => [e.name + (e.type === 'radio' ? `=${e.value}` : ''), [e.value, !!e.checked]])));
  }
} else if (inp.op === 'boxes') {
  Object.assign(out, await page.evaluate(() => { const b = {}; for (const el of document.querySelectorAll('input[type=checkbox]')) b[el.name] = el.checked; b.ins = document.getElementById('ins')?.getAttribute('aria-checked') === 'true'; return b; }));
} else if (inp.op === 'snapshot') {
  out.text = await page.locator('body').ariaSnapshot();
  for (const fr of page.frames().slice(1)) out.text += '\n' + await fr.locator('body').ariaSnapshot();
} else if (inp.op === 'state') {
  Object.assign(out, await page.evaluate(() => window.state()));
} else if (inp.op === 'screenshot') {
  await page.screenshot({ path: inp.path });
  out.path = inp.path;
}
console.log(JSON.stringify(out));
process.exit(0);
