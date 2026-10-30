#!/usr/bin/env node
// securefill-cdp: the browser half of `securefill` (~/bin/securefill, logic ~/bin/lib/securefill.py). The ONLY place on
// this box where personal values exist in clear: it opens the vault's sealed box with its one-time X25519 key, types the
// values into the task's tab over CDP (Playwright connectOverCDP on the headed Chrome), masks the filled inputs, writes
// keyed fingerprints for the redaction hook, and prints field keys with ok/fail. Never a value, on stdout, stderr or in
// a file. Input: one JSON object on stdin ({op: keygen|probe|fill|mask|shot, ...}); output: one JSON object.
// Tests: ~/opt/webagent-tests/securefill.
import { createRequire } from 'node:module';
import { spawnSync } from 'node:child_process';
import { fileURLToPath } from 'node:url';
import crypto from 'node:crypto';
import fs from 'node:fs';
import os from 'node:os';
import path from 'node:path';

const require = createRequire(import.meta.url);
const HOME = process.env.HOME || os.homedir();
let SECRETS = [];                                   // every clear value this run holds: scrubbed from any message
const scrub = (s) => {
  let t = String(s ?? '');
  for (const v of SECRETS) if (v && v.length >= 3) t = t.split(v).join('…');
  return t.replace(/\d[\d \-./]{4,}\d/g, '…').slice(0, 300);
};
const done = (o, code = 0) => { process.stdout.write(JSON.stringify(o) + '\n'); process.exit(code); };
const fail = (why, extra = {}) => done({ ok: false, error: scrub(why), ...extra }, 1);
process.on('uncaughtException', (e) => fail(`internal: ${e?.name || 'Error'}: ${e?.message || ''}`));
process.on('unhandledRejection', (e) => fail(`internal: ${e?.name || 'Error'}: ${e?.message || ''}`));

// ---------------------------------------------------------------- hosts
// public suffixes + shared platforms (one site per subdomain there); same list as securefill.py and the vault
const SLD2 = new Set(['co.uk', 'org.uk', 'ac.uk', 'gov.uk', 'me.uk', 'com.au', 'net.au', 'org.au', 'co.nz', 'co.jp',
  'com.br', 'com.tr', 'co.za', 'com.mx', 'com.ar', 'com.sg', 'co.in', 'co.il', 'com.cn', 'com.hk', 'co.kr',
  'myshopify.com', 'github.io', 'herokuapp.com', 'netlify.app', 'vercel.app', 'pages.dev', 'web.app', 'firebaseapp.com',
  'azurewebsites.net', 'cloudfront.net', 'appspot.com', 'wixsite.com', 'blogspot.com', 'wordpress.com', 'squarespace.com',
  'webflow.io', 'workers.dev', 'onrender.com', 'amazonaws.com']);
const site = (h) => {
  const s = String(h || '').toLowerCase();
  if (/^[\d.]+$/.test(s) || s.includes(':')) return s;          // IP addresses stay as they are
  const p = s.split('.'); if (p.length <= 2) return p.join('.');
  const two = p.slice(-2).join('.'); return SLD2.has(two) ? p.slice(-3).join('.') : two;
};
const hostOf = (u) => { try { const x = new URL(u); return x.protocol.startsWith('http') ? x.hostname.toLowerCase() : ''; } catch { return ''; } };
const hostMatch = (h, pats) => (pats || []).some((p) => { p = String(p).toLowerCase(); return p.startsWith('*.') ? (h === p.slice(2) || h.endsWith(p.slice(1))) : h === p; });

// ---------------------------------------------------------------- sealed box (personal.py seal(): X25519 -> HKDF-SHA256 -> HMAC-CTR + HMAC tag)
const b64d = (s) => Buffer.from(String(s).replace(/-/g, '+').replace(/_/g, '/'), 'base64');
function unseal(box, jwk, context) {
  const priv = crypto.createPrivateKey({ key: jwk, format: 'jwk' });
  const pub = b64d(jwk.x);
  const [epk, nonce, ct, tag] = ['epk', 'nonce', 'ct', 'tag'].map((k) => b64d(box[k]));
  const epkKey = crypto.createPublicKey({ key: { kty: 'OKP', crv: 'X25519', x: epk.toString('base64url') }, format: 'jwk' });
  const shared = crypto.diffieHellman({ privateKey: priv, publicKey: epkKey });
  const ctx = Buffer.from('securefill-v1|' + context);
  const k = Buffer.from(crypto.hkdfSync('sha256', shared, Buffer.concat([epk, pub]), ctx, 64));
  const want = crypto.createHmac('sha256', k.subarray(32)).update(Buffer.concat([ctx, epk, nonce, ct])).digest();
  if (want.length !== tag.length || !crypto.timingSafeEqual(want, tag)) throw new Error('sealed box does not open (wrong key or changed on the way)');
  const out = Buffer.alloc(ct.length);
  for (let i = 0, blk = 0; i < ct.length; blk++) {
    const c = Buffer.alloc(4); c.writeUInt32BE(blk);
    const ks = crypto.createHmac('sha256', k.subarray(0, 32)).update(Buffer.concat([nonce, c])).digest();
    for (let j = 0; j < 32 && i < ct.length; j++, i++) out[i] = ct[i] ^ ks[j];
  }
  return JSON.parse(out.toString('utf8'));
}

// ---------------------------------------------------------------- word tokens of released text values
// Names, e-mail, street, town ... become keyed hashes of their normalized word tokens; securefill.py (_tnorm, _tokens,
// _text_hits) tokenizes tool output the same way and redacts matches. Normalized = lowercased, transliterated (TR1),
// accents stripped; a second variant with German umlauts as ae/oe/ue (TR2). A "strong" token is redacted on its own; weak
// ones (short, stop words, house numbers) only next to the same value's other tokens (a "seq").
const TR1 = { 'ß': 'ss', 'đ': 'd', 'ø': 'o', 'æ': 'ae', 'ł': 'l', 'œ': 'oe', 'þ': 'th', 'ı': 'i' };
const TR2 = { 'ä': 'ae', 'ö': 'oe', 'ü': 'ue', 'đ': 'dj' };
const trm = (s, m) => Array.from(s, (c) => m[c] ?? c).join('');
const unmark = (s) => s.normalize('NFKD').replace(/\p{M}/gu, '');
const tnorm = (w) => {
  const l = String(w).toLowerCase();
  const out = [unmark(trm(l, TR1))];
  if (/[äöüđ]/.test(l)) { const b = unmark(trm(trm(l, TR2), TR1)); if (b !== out[0]) out.push(b); }
  return out;
};
const STOP = new Set(('am an auf bei der die das den dem des de di da do du del della dello dei degli delle la le les lo ' +
  'los las el il van von vom zum zur im in on of the and und et e y sur sous san santa sankt saint st ste rue via viale ' +
  'route chemin avenue av weg gasse strasse str platz street road rd lane c o mr mrs ms dr herr frau').split(' '));
const MAIL_STOP = new Set(['info', 'mail', 'email', 'contact', 'kontakt', 'hello', 'office', 'admin', 'post', 'service', 'noreply']);
const strongTok = (t) => t.length >= 3 && !STOP.has(t) && (!/\d/.test(t) || (t.match(/\d/g) || []).length >= 4);
const words = (s) => String(s ?? '').split(/[^\p{L}\p{N}\p{M}]+/u).filter(Boolean);
const COMMON_MAIL = /^(gmail|googlemail|outlook|hotmail|live|msn|yahoo|ymail|icloud|me|mac|gmx|web|bluewin|sunrise|protonmail|proton|pm|hispeed|swissonline|aol|mail|t-online|freenet|yandex|zoho)\./;
const TEXT_VALS = ['name.given_names', 'name.surname', 'card.holder', 'identity.birth_place', 'address.street', 'address.line2',
  'address.postal_code', 'address.city'];
const TITLE_TAIL = /(mrs|miss|mstr|mr|ms)$/;
// -> fingerprint items for the text values (tok / seq / email), hashed with H
function textItems(vals, H) {
  const items = [], seen = new Set();
  const tok = (t) => { const h = H(`tok:${t}`); if (!seen.has(h)) { seen.add(h); items.push({ kind: 'tok', h }); } };
  const seq = (ws) => { if (ws.length >= 2) items.push({ kind: 'seq', p: ws.map((w) => { const n = tnorm(w); return { s: strongTok(n[0]) ? 1 : 0, h: n.map((v) => H(`tok:${v}`)) }; }) }); };
  const W = (k) => words(vals[k]);
  for (const k of TEXT_VALS) {
    const ws = W(k);
    for (const w of ws) for (const v of tnorm(w)) if (strongTok(tnorm(w)[0])) tok(v);
    seq(ws);
  }
  const given = W('name.given_names'), sur = W('name.surname');
  if (given.length && sur.length) { seq([...given, ...sur]); seq([...sur, ...given]); }
  const street = W('address.street'), no = W('address.house_number');
  if (street.length && no.length) { seq([...street, ...no]); seq([...no, ...street]); }
  const zip = W('address.postal_code'), city = W('address.city');
  if (zip.length && city.length) { seq([...zip, ...city]); seq([...city, ...zip]); }
  const em = String(vals['contact.email'] || '').trim().toLowerCase();
  if (em.includes('@')) {
    const h = H(`email:${em}`); items.push({ kind: 'email', h });
    const [local, domain] = [em.slice(0, em.lastIndexOf('@')), em.slice(em.lastIndexOf('@') + 1)];
    const lw = words(local);
    for (const w of lw) { const n = tnorm(w); if (strongTok(n[0]) && n[0].length >= 4 && !MAIL_STOP.has(n[0])) n.forEach(tok); }
    seq(lw);
    if (!COMMON_MAIL.test(domain)) seq(words(domain));
  }
  return items;
}
// the matcher over page text (securefill.py _text_hits, for masking text before a screenshot): fp = {tok: Set, email: Set,
// seq: [[{s, h: Set}]], idx: Map(h -> [[seq, pos]])}; -> [[start, end]]
function fpIndex(items) {
  const fp = { tok: new Set(), email: new Set(), seq: [], idx: new Map() };
  for (const it of items || []) {
    if (it.kind === 'tok') fp.tok.add(it.h);
    else if (it.kind === 'email') fp.email.add(it.h);
    else if (it.kind === 'seq' && (it.p || []).length >= 2) {
      const si = fp.seq.length; fp.seq.push(it.p.map((x) => ({ s: !!x.s, h: new Set(x.h) })));
      it.p.forEach((x, pos) => { for (const h of x.h) { if (!fp.idx.has(h)) fp.idx.set(h, []); fp.idx.get(h).push([si, pos]); } });
    }
  }
  return fp;
}
const EMAIL_RX = /[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}/g;
function textSpans(text, fp, H, cache) {
  const spans = [];
  if (fp.email.size) for (const m of text.matchAll(EMAIL_RX)) if (fp.email.has(H(`email:${m[0].toLowerCase()}`))) spans.push([m.index, m.index + m[0].length]);
  if (!fp.tok.size && !fp.seq.length) return spans;
  const toks = [...text.matchAll(/[\p{L}\p{N}][\p{L}\p{N}\p{M}]*/gu)].map((m) => [m.index, m.index + m[0].length, m[0]]);
  const hv = toks.map(([a, b, w]) => {
    let hs = cache.get(w);
    if (!hs) {
      const n = tnorm(w); hs = new Set(n.map((v) => H(`tok:${v}`)));
      const m = n[0].length >= 5 && n[0].match(TITLE_TAIL);           // DOE/JANEMS: a title glued to the name
      if (m) hs.add(H(`tok:${n[0].slice(0, -m[1].length)}`));
      cache.set(w, hs);
    }
    if ([...hs].some((h) => fp.tok.has(h))) spans.push([a, b]);
    return hs;
  });
  const near = (i) => { const gap = text.slice(toks[i][1], toks[i + 1][0]); return gap.length <= 3 && !gap.includes('\n'); };
  hv.forEach((hs, i) => {
    for (const h of hs) for (const [si, pos] of fp.idx.get(h) || []) {
      const s = fp.seq[si]; let j = i, p = pos, strong = s[pos].s;
      while (p + 1 < s.length && j + 1 < toks.length && near(j) && [...hv[j + 1]].some((x) => s[p + 1].h.has(x))) { j++; p++; strong = strong || s[p].s; }
      if (j > i && strong) spans.push([toks[i][0], toks[j][1]]);
    }
  });
  return spans;
}

// ---------------------------------------------------------------- field classification
const AC = { 'cc-number': 'card.number', 'cc-csc': 'card.cvc', 'cc-exp': 'card.exp', 'cc-exp-month': 'card.exp_month', 'cc-exp-year': 'card.exp_year',
  'cc-name': 'card.holder', 'given-name': 'name.given_names', 'family-name': 'name.surname', name: 'name.full', 'honorific-prefix': 'name.title',
  bday: 'identity.birth_date', 'bday-day': 'identity.birth_date@day', 'bday-month': 'identity.birth_date@month', 'bday-year': 'identity.birth_date@year',
  sex: 'identity.sex', email: 'contact.email', tel: 'contact.phone_full', 'tel-country-code': 'contact.phone_cc', 'tel-national': 'contact.phone_national',
  'street-address': 'address.street_full', 'address-line1': 'address.street_full', 'address-line2': 'address.line2', 'postal-code': 'address.postal_code',
  'address-level2': 'address.city', 'address-level1': 'address.region', country: 'address.country', 'country-name': 'address.country' };
const R = (s) => new RegExp(s, 'i');
const RULES = [
  ['card.number', R('card.?(number|no\\b|num)|cardnumber|kartennummer|num[eé]ro de (la )?carte|numero (della )?carta|encryptedcardnumber|\\bpan\\b')],
  ['card.cvc', R('\\bcvc|\\bcvv|\\bcsc\\b|security.?code|sicherheitscode|pr[uü]fnummer|cryptogramme|codice di sicurezza|encryptedsecuritycode|card.?verification')],
  ['passport.expiry_date', R('(passport|pass\\b|document|dokument|passaporto|passeport).{0,30}(expir|valid|ablauf|g[uü]ltig|scad)|(expir|ablauf|scad).{0,30}(passport|document|dokument)')],
  ['passport.issue_date', R('(passport|document|dokument).{0,30}(issue|ausstell|rilascio|d[ée]livr)|date of issue')],
  ['passport.issuing_country', R('(passport|document|dokument).{0,30}(country|issu|land|paese|pays)|issuing.?(country|state|authority)|ausstellungs(land|staat)|paese di rilascio')],
  ['passport.number', R('passport.{0,12}(no|num|nr)|passnummer|pass.?nr|document.?(no|num)|dokumentnummer|numero (del )?passaporto|n[°o] de passeport|travel.?document|\\bpassport\\b')],
  ['card.exp_month', R('exp\\w*.?(month|monat|mois|mese)|encryptedexpirymonth')],
  ['card.exp_year', R('exp\\w*.?(year|jahr|ann[eé]e|anno)|encryptedexpiryyear')],
  ['card.exp', R('expir\\w*.?date|exp.?date|mm ?/ ?(yy|jj|aa)|ablaufdatum|g[uü]ltig bis|valid (thru|until)|date d.expiration|scadenza|encryptedexpirydate|\\bexpiry\\b')],
  ['card.holder', R('card.?holder|holder.?name|name on (the )?card|karteninhaber|titulaire|intestatario|holdername')],
  ['identity.nationality', R('nationalit|staatsangeh|citizenship|nazionalit|nationalit[eé]')],
  ['identity.birth_date', R('bday|birth|\\bdob\\b|geburts|nascita|naissance')],
  ['identity.birth_place', R('place of birth|birth.?place|geburtsort|luogo di nascita|lieu de naissance')],
  ['identity.sex', R('\\bsex\\b|gender|geschlecht|sesso|\\bgenre\\b')],
  ['contact.phone_cc', R('tel-country-code|(country|calling|dial(l?ing)?|phone|area).?(code|prefix)|vorwahl|prefisso|indicatif|\\bprefix\\b')],
  ['contact.email', R('e-?mail|\\bmail\\b')],
  ['contact.phone_national', R('phone|mobile|telefon|handy|natel|cellulare|t[ée]l[ée]phone|\\btel\\b|portable')],
  ['name.title', R('honorific|\\btitle\\b|anrede|salutation|titolo|civilit[eé]')],
  ['name.given_names', R('given.?name|first.?name|firstname|vorname|pr[eé]nom|\\bnome\\b')],
  ['name.surname', R('family.?name|last.?name|lastname|surname|nachname|cognome|nom de famille')],
  ['address.postal_code', R('postal|\\bzip|\\bplz\\b|postleitzahl|\\bcap\\b|code postal|postcode')],
  ['address.city', R('\\bcity\\b|\\btown\\b|\\bort\\b|\\bstadt\\b|citt[aà]|\\bville\\b|locality')],
  ['address.region', R('\\bregion\\b|\\bstate\\b|province|kanton|provincia')],
  ['address.house_number', R('house.?(no|num)|hausnummer|street.?(no|num)|numero civico|\\bnr\\.?$')],
  ['address.line2', R('address.?line.?2|adresszusatz|\\bline2\\b|apartment|\\bsuite\\b|c/o')],
  ['address.country', R('\\bcountry\\b|\\bland\\b|\\bpaese\\b|\\bpays\\b')],
  ['address.street_full', R('street|stra(ss|ß)e|\\bvia\\b|\\brue\\b|adresse|address')],
  ['name.full', R('full.?name|^\\s*name\\s*$|\\bvollst[aä]ndiger name')],
];
const PART = { day: R('\\b(day|tag|jour|giorno|dd)\\b'), month: R('\\b(month|monat|mois|mese|mm)\\b'), year: R('\\b(year|jahr|ann[eé]e|anno|yyyy|jjjj|aaaa)\\b') };
const DATE_KEYS = new Set(['identity.birth_date', 'passport.expiry_date', 'passport.issue_date', 'id_card.expiry_date']);

// keys a picker can take (title, sex, countries, calling code, date and expiry parts): custom dropdowns, listboxes,
// typeahead inputs and button radios are filled only for these, so no other toggle on the page is ever clicked
const CHOICE = new Set(['name.title', 'identity.sex', 'identity.nationality', 'passport.issuing_country', 'id_card.issuing_country',
  'address.country', 'contact.phone_cc', 'card.exp_month', 'card.exp_year']);
const isChoice = (k) => !!k && (k.includes('@') || CHOICE.has(k));
const PICKERS = new Set(['combobox', 'listbox', 'popup', 'typeahead', 'xradio']);

function classify(f, profile) {
  const d = [f.ac, f.name, f.elid, f.ph, f.aria, f.label, f.group, f.hints].join(' | ').toLowerCase();
  for (const r of profile?.rules || []) {
    try { if (new RegExp(r.match, 'i').test(d) && (!r.tag || r.tag === f.tag || r.tag === f.kind)) return r.key; } catch { /* bad rule */ }
  }
  for (const tok of String(f.ac || '').split(/\s+/).reverse()) if (AC[tok]) return AC[tok];
  for (const [key, rx] of RULES) {
    if (!rx.test(d)) continue;
    if (key === 'contact.phone_cc' && !(f.tag === 'select' || f.kind === 'combobox' || /code|prefix|vorwahl|prefisso|indicatif|\+/.test(d))) continue;
    if (DATE_KEYS.has(key) || key === 'card.exp') {
      if (f.type === 'date' || /(dd|tt|gg|jj)\s?[./-]\s?mm|mm\s?[./-]\s?(dd|yy|jj|aa)|yyyy\s?[./-]\s?mm/.test(d)) return key;   // one field, whole date
      const n = f.options ? f.options.length : 0;
      for (const [p, rx2] of Object.entries(PART)) if (rx2.test(d)) return key === 'card.exp' ? `card.exp_${p === 'day' ? 'month' : p}` : `${key}@${p}`;
      if (f.tag === 'select') { if (n >= 28 && n <= 33) return `${key}@day`; if (n >= 12 && n <= 14) return `${key}@month`; if (n > 14) return `${key}@year`; }
    }
    return key;
  }
  return null;
}

// ---------------------------------------------------------------- values per element
const pad2 = (x) => String(x).padStart(2, '0');
const norm = (s) => String(s ?? '').normalize('NFD').replace(/[̀-ͯ]/g, '').toLowerCase().replace(/\s+/g, ' ').trim();
const digits = (s) => String(s ?? '').replace(/\D/g, '');
const LANGS = ['en', 'de', 'fr', 'it', 'es'];
function countryNames(code) {
  const out = [code];
  for (const l of LANGS) { try { const n = new Intl.DisplayNames([l], { type: 'region' }).of(code); if (n && n !== code) out.push(n); } catch { /* */ } }
  if (code === 'CH') out.push('CHE', 'Schweiz', 'Svizzera', 'Suisse');
  return out;
}
const CC_ISO = { '+41': 'CH', '+49': 'DE', '+43': 'AT', '+33': 'FR', '+39': 'IT', '+423': 'LI', '+381': 'RS', '+387': 'BA', '+385': 'HR', '+382': 'ME', '+389': 'MK', '+386': 'SI', '+34': 'ES', '+351': 'PT', '+31': 'NL', '+32': 'BE', '+44': 'GB', '+1': 'US', '+248': 'SC' };
function monthNames(m) {
  const out = [String(+m), pad2(m)];
  for (const l of LANGS) for (const st of ['long', 'short']) { try { out.push(new Intl.DateTimeFormat(l, { month: st, timeZone: 'UTC' }).format(new Date(Date.UTC(2000, +m - 1, 15)))); } catch { /* */ } }
  return out;
}
const TITLES = { Mr: ['mr', 'mr.', 'herr', 'monsieur', 'm.', 'signor', 'sig.', 'sr.'], Ms: ['ms', 'ms.', 'mrs', 'mrs.', 'frau', 'madame', 'mme', 'signora', 'sig.ra', 'miss'], Mrs: ['mrs', 'mrs.', 'ms', 'frau', 'madame', 'signora'], Mx: ['mx', 'mx.'], Dr: ['dr', 'dr.'] };
const SEXES = { M: ['m', 'male', 'man', 'mannlich', 'mann', 'homme', 'masculin', 'maschio', 'uomo', 'herr', 'mr'], F: ['f', 'female', 'woman', 'weiblich', 'frau', 'femme', 'feminin', 'femmina', 'donna', 'ms', 'mrs'], X: ['x', 'diverse', 'divers', 'other'] };

function dateFmt(f, iso, lang) {
  const [y, m, d] = iso.split('-');
  if (f.type === 'date') return iso;
  const hint = norm(`${f.ph} ${f.label} ${f.aria}`);
  if (/yyyy-mm-dd|jjjj-mm-tt/.test(hint)) return iso;
  if (/mm\/dd\/(yyyy|yy)/.test(hint)) return `${m}/${d}/${y}`;
  if (/(dd|tt|gg|jj)\/mm\/(yyyy|jjjj|aaaa)/.test(hint)) return `${d}/${m}/${y}`;
  if (/(dd|tt|gg|jj)-mm-(yyyy|jjjj|aaaa)/.test(hint)) return `${d}-${m}-${y}`;
  if (f.maxlen === 8) return `${d}${m}${y}`;
  if (/(dd|tt)\.mm\.(yyyy|jjjj)/.test(hint) || /^(de|it|fr)/.test(lang || '')) return `${d}.${m}.${y}`;
  return `${d}/${m}/${y}`;
}

// what goes into element f for key (with @part), from the opened values v; null = not available
function want(key, f, v, ctx) {
  const [base, part] = key.split('@');
  const g = (k) => v[k];
  if (DATE_KEYS.has(base)) {
    const iso = g(base); if (!iso) return null;
    const [y, m, d] = iso.split('-');
    if (part === 'day') return { text: String(+d), alts: [String(+d), d] };
    if (part === 'month') return { text: m, alts: monthNames(m) };
    if (part === 'year') return { text: y, alts: [y, y.slice(2)] };
    return { text: dateFmt(f, iso, ctx.lang), digits: true };
  }
  switch (base) {
    case 'name.full': return g('name.given_names') && g('name.surname') ? { text: `${g('name.given_names')} ${g('name.surname')}` } : null;
    case 'name.title': {                             // stored title, else derived from the stored sex
      const raw = g('name.title') || ({ M: 'Mr', F: 'Ms' })[String(g('identity.sex') || '').toUpperCase()];
      if (!raw) return null;
      const t = Object.keys(TITLES).find((k) => k.toLowerCase() === norm(raw).replace(/\.$/, '')) || raw;
      return { text: t, alts: TITLES[t] || [t] };
    }
    case 'identity.sex': {                           // stored sex, else derived from the stored title
      const s = String(g('identity.sex') || ({ mr: 'M', ms: 'F', mrs: 'F', miss: 'F' })[norm(g('name.title')).replace(/\.$/, '')] || '').toUpperCase();
      return SEXES[s] ? { text: s, alts: SEXES[s] } : null;
    }
    case 'identity.nationality': case 'passport.issuing_country': case 'id_card.issuing_country': case 'address.country': {
      if (!g(base)) return null;
      const names = countryNames(g(base)), l2 = String(ctx.lang || '').slice(0, 2);
      let text = names[1] || g(base);                // typed into a filter box: the page language's name
      if (LANGS.includes(l2)) { try { text = new Intl.DisplayNames([l2], { type: 'region' }).of(g(base)) || text; } catch { /* */ } }
      return { text, alts: names, country: true };
    }
    case 'contact.phone_cc': { const cc = g('contact.phone_cc'); if (!cc) return null; const iso = CC_ISO[cc]; return { text: cc, alts: [cc, cc.slice(1), ...(iso ? [iso, ...countryNames(iso)] : [])], cc }; }
    case 'contact.phone_national': {
      const n = g('contact.phone_national'); if (!n) return null;
      if (!ctx.hasCC) return { text: `${g('contact.phone_cc') || ''}${n}`, digits: true };
      return { text: n, digits: true };
    }
    case 'contact.phone_full': return g('contact.phone_national') ? { text: `${g('contact.phone_cc') || ''}${g('contact.phone_national')}`, digits: true } : null;
    case 'address.street_full': {
      if (!g('address.street')) return null;
      return { text: ctx.hasHouse || !g('address.house_number') ? g('address.street') : `${g('address.street')} ${g('address.house_number')}` };
    }
    case 'card.exp': {
      const m = g('card.exp_month'), y = g('card.exp_year'); if (!m || !y) return null;
      const hint = norm(`${f.ph} ${f.label}`);
      const t = f.maxlen === 4 ? `${m}${y.slice(2)}` : (/yyyy|jjjj|aaaa/.test(hint) || f.maxlen === 7) ? `${m}/${y}` : `${m}/${y.slice(2)}`;
      return { text: t, digits: true };
    }
    case 'card.exp_month': return g('card.exp_month') ? { text: g('card.exp_month'), alts: monthNames(g('card.exp_month')) } : null;
    case 'card.exp_year': return g('card.exp_year') ? { text: f.tag === 'select' || f.maxlen !== 2 ? g('card.exp_year') : g('card.exp_year').slice(2), alts: [g('card.exp_year'), g('card.exp_year').slice(2)] } : null;
    case 'card.number': return g(base) ? { text: g(base), digits: true } : null;
    case 'card.cvc': return g(base) ? { text: g(base), digits: true } : null;
    default: return g(base) != null ? { text: String(g(base)) } : null;
  }
}
// which vault fields a detected key needs
function needs(key, ctx) {
  const base = key.split('@')[0];
  switch (base) {
    case 'name.full': return ['name.given_names', 'name.surname'];
    case 'name.title': return ['name.title'];
    case 'contact.phone_national': return ctx.hasCC ? ['contact.phone_national'] : ['contact.phone_cc', 'contact.phone_national'];
    case 'contact.phone_full': return ['contact.phone_cc', 'contact.phone_national'];
    case 'address.street_full': return ctx.hasHouse ? ['address.street'] : ['address.street', 'address.house_number'];
    case 'card.exp': return ['card.exp_month', 'card.exp_year'];
    default: return [base];
  }
}

// ---------------------------------------------------------------- in-page code
// shown(el): a person can see and use el. Rendered with a usable size; not transparent (opacity 0 on it or any ancestor);
// not visibility-hidden or pointer-events:none; not cut away by an ancestor's overflow, clip or clip-path (what a scroll
// container hides can be scrolled in); not off the page (left of / above it, past a right edge that does not scroll, or
// outside the viewport in a fixed layer); not in a closed <dialog> or outside an open modal one; no aria-hidden or inert
// on it or an ancestor. Shadow roots and slots are followed. This is the field filter: a value typed into anything else
// goes to the site unseen (an invisible helper input next to a payment frame, a login popup that is only faded out).
// scan() also runs it on each frame's <iframe> element, up the chain. own = el's own opacity does not count (a native
// <select> laid transparently over its own styled look; every ancestor's still does).
function shown(el, own = false) {
  if (!el || el.nodeType !== 1 || !el.isConnected) return false;
  const doc = el.ownerDocument, win = doc && doc.defaultView;
  if (!win) return false;
  const MIN = 4;                                               // px: anything smaller is no field a person can use
  if (typeof el.checkVisibility === 'function' &&
      !el.checkVisibility({ opacityProperty: !own, visibilityProperty: true, checkOpacity: !own, checkVisibilityCSS: true })) return false;
  const cs0 = win.getComputedStyle(el);
  if (cs0.visibility !== 'visible' || cs0.pointerEvents === 'none') return false;
  const r = el.getBoundingClientRect();
  if (!(r.width >= MIN && r.height >= MIN)) return false;
  const up = (n) => n.assignedSlot || n.parentElement || (n.parentNode && n.parentNode.host) || null;
  const len = (v, of) => (/%$/.test(v) ? (parseFloat(v) / 100) * of : parseFloat(v) || 0);
  const root = doc.documentElement, body = doc.body;
  let box = { l: r.left, t: r.top, r: r.right, b: r.bottom };   // the part of el a person can get to see
  const cut = (l, t, rr, b) => { box = { l: Math.max(box.l, l), t: Math.max(box.t, t), r: Math.min(box.r, rr), b: Math.min(box.b, b) }; };
  let op = 1, esc = '';          // esc: el sits in an absolute / fixed box: ancestors below its containing block do not clip it
  for (let n = el; n; n = up(n)) {
    const cs = n === el ? cs0 : win.getComputedStyle(n);
    if (cs.display === 'none') return false;
    if (!(own && n === el)) op *= parseFloat(cs.opacity);
    if (!(op >= 0.05)) return false;
    if (n.getAttribute('aria-hidden') === 'true' || n.hasAttribute('inert')) return false;
    if (n.tagName === 'DIALOG' && !n.open) return false;
    if (n !== el && cs.contentVisibility === 'hidden') return false;
    const nr = n === el ? r : n.getBoundingClientRect();
    if (/^(absolute|fixed)$/.test(cs.position) && /^rect\(/.test(cs.clip || '')) {          // clip: rect(top, right, bottom, left)
      const v = cs.clip.slice(5, -1).split(/[\s,]+/).filter(Boolean);
      const at = (i, d) => (v[i] === 'auto' ? d : parseFloat(v[i]) || 0);
      if (v.length === 4) cut(nr.left + at(3, 0), nr.top + at(0, 0), nr.left + at(1, nr.width), nr.top + at(2, nr.height));
    }
    const cp = cs.clipPath || 'none';
    if (/^inset\(/.test(cp)) {
      const v = cp.slice(6, -1).split(/\s+round\s+/)[0].trim().split(/\s+/);
      const [t, rr, b, l] = [v[0], v[1] ?? v[0], v[2] ?? v[0], v[3] ?? v[1] ?? v[0]];
      cut(nr.left + len(l, nr.width), nr.top + len(t, nr.height), nr.right - len(rr, nr.width), nr.bottom - len(b, nr.height));
    } else if (/^(circle|ellipse)\(\s*0(px|%)?[\s)]/.test(cp)) return false;
    if (n !== el) {
      const cb = cs.transform !== 'none' || cs.filter !== 'none' || cs.perspective !== 'none' || /\b(paint|layout|strict|content)\b/.test(cs.contain || '');
      let clips = true;
      if (esc === 'abs') { if (cs.position !== 'static' || cb) esc = ''; else clips = false; }
      else if (esc === 'fixed') { if (cb) esc = ''; else clips = false; }
      if (clips && n !== root && n !== body) {                 // the page's own scrolling: checked below
        const sx = /^(auto|scroll)$/.test(cs.overflowX), sy = /^(auto|scroll)$/.test(cs.overflowY);
        const hx = /^(hidden|clip)$/.test(cs.overflowX), hy = /^(hidden|clip)$/.test(cs.overflowY);
        if (sx || sy) {                                        // a scroll container: el can be scrolled into its view
          const l = Math.max(box.l, nr.left), t = Math.max(box.t, nr.top), rr = Math.min(box.r, nr.right), b = Math.min(box.b, nr.bottom);
          box = rr - l >= MIN && b - t >= MIN ? { l, t, r: rr, b } : { l: nr.left, t: nr.top, r: nr.right, b: nr.bottom };
        }
        if (hx || hy) cut(hx ? nr.left : -Infinity, hy ? nr.top : -Infinity, hx ? nr.right : Infinity, hy ? nr.bottom : Infinity);
      }
    }
    if (!(box.r - box.l >= MIN && box.b - box.t >= MIN)) return false;
    if (cs.position === 'fixed') esc = 'fixed';
    else if (cs.position === 'absolute' && esc !== 'fixed') esc = 'abs';
  }
  const ltr = win.getComputedStyle(root).direction !== 'rtl';
  if (esc === 'fixed') {                                       // a fixed layer: only the viewport shows it
    if (box.r <= 0 || box.b <= 0 || box.l >= win.innerWidth || box.t >= win.innerHeight) return false;
  } else {
    if ((ltr && box.r + win.scrollX <= 0) || box.b + win.scrollY <= 0) return false;            // left of / above the page
    const noX = [root, body].some((n) => n && /^(hidden|clip)$/.test(win.getComputedStyle(n).overflowX));
    if (ltr && noX && box.l >= win.innerWidth) return false;                                    // past a right edge that does not scroll
  }
  let modal = null;
  try { modal = doc.querySelector('dialog:modal'); } catch { /* an engine without :modal */ }
  if (modal) { let n = el; while (n && n !== modal) n = up(n); if (!n) return false; }
  return true;
}
// Playwright sends String(fn) into the page: these carry shown() along as their first argument
const withShown = (fn) => Object.assign((...a) => fn(shown, ...a), { toString: () => `(...a) => (${fn})((${shown}), ...a)` });
// locator / element handle .evaluate(SHOWN_EL): the same test pageFields() applies to a control
const SHOWN_EL = withShown((sh, el) => sh(el) || (el.tagName === 'SELECT' && sh(el, true)));
const MASK_CSS = 'input:not([type=checkbox]):not([type=radio]):not([type=submit]):not([type=button]):not([type=hidden]):not([type=image]):not([type=reset]):not([type=file]):not([type=range]):not([type=color]),textarea{-webkit-text-security:disc!important}';
function pageFields(shown) {
  const skip = new Set(['hidden', 'submit', 'button', 'image', 'reset', 'file', 'range', 'color', 'checkbox']);
  const txt = (n) => (n ? (n.innerText || n.textContent || '') : '').replace(/\s+/g, ' ').trim();
  const labelOf = (el) => {
    const by = (el.getAttribute('aria-labelledby') || '').split(/\s+/).map((i) => document.getElementById(i)).filter(Boolean).map(txt).join(' ');
    if (by) return by.slice(0, 100);
    if (el.labels && el.labels.length) return [...el.labels].map(txt).join(' ').slice(0, 100);
    const lab = el.closest('label'); if (lab) return txt(lab).slice(0, 100);
    let p = el.parentElement;
    for (let i = 0; i < 3 && p; i++, p = p.parentElement) { const t = txt(p); if (t && t.length <= 100) return t; }
    return '';
  };
  // the group label of a radio (fieldset legend, radiogroup label): "Title" for Mr / Ms buttons
  const groupOf = (el) => {
    const g = el.closest('[role=radiogroup],fieldset,[role=group]'); if (!g) return '';
    const by = (g.getAttribute('aria-labelledby') || '').split(/\s+/).map((i) => document.getElementById(i)).filter(Boolean).map(txt).join(' ');
    const lg = g.tagName === 'FIELDSET' ? g.querySelector('legend') : null;
    return (by || g.getAttribute('aria-label') || txt(lg) || '').slice(0, 80);
  };
  // the document and its open shadow roots (web components)
  const roots = [document];
  for (let i = 0; i < roots.length && i < 200; i++) for (const el of roots[i].querySelectorAll('*')) if (el.shadowRoot) roots.push(el.shadowRoot);
  const SEL = 'input,select,textarea,[role=combobox],[role=listbox],[role=radio],button[aria-haspopup],[role=button][aria-haspopup],' +
    '[aria-haspopup=listbox],button[aria-expanded],[role=button][aria-expanded]';
  const out = [], seen = new Set();
  for (const el of roots.flatMap((r) => [...r.querySelectorAll(SEL)])) {
    if (seen.has(el)) continue; seen.add(el);
    const tag = el.tagName.toLowerCase(), type = (el.getAttribute('type') || (tag === 'input' ? 'text' : '')).toLowerCase();
    const role = (el.getAttribute('role') || '').toLowerCase();
    if (tag === 'input' && skip.has(type)) continue;
    if (role === 'listbox' && el.closest('[role=combobox]')) continue;      // a combobox's own popup
    const r = el.getBoundingClientRect();
    let id = el.getAttribute('data-sf-id');
    if (!id) { id = 'sf' + Math.random().toString(36).slice(2, 9); el.setAttribute('data-sf-id', id); }
    const text = tag === 'input' || tag === 'select' || tag === 'textarea' ? '' : txt(el).slice(0, 80);
    let kind;
    if (tag === 'select') kind = 'select';
    else if (tag === 'input' && type === 'radio') kind = 'radio';
    else if (role === 'radio') kind = 'xradio';                                // a button or div acting as a radio
    else if (tag === 'input' || tag === 'textarea') {
      const aa = (el.getAttribute('aria-autocomplete') || '').toLowerCase();
      kind = role === 'combobox' || aa === 'list' || aa === 'both' || el.hasAttribute('list') ? 'typeahead' : 'input';
    } else if (role === 'combobox') kind = 'combobox';
    else if (role === 'listbox') kind = 'listbox';                             // options always on the page
    else kind = 'popup';                                                       // a button that opens a list
    const radio = kind === 'radio' || kind === 'xradio';
    // shown() only; a transparent native <select> over its styled look counts, and a native radio also when a label of
    // it is shown (a styled radio hides the input itself)
    const vis = shown(el) || (tag === 'select' && shown(el, true)) ||
      (kind === 'radio' && r.width > 0 && r.height > 0 && [...(el.labels || []), el.closest('label')].some((l) => l && shown(l)));
    out.push({ id, tag, type, kind, name: el.getAttribute('name') || '', elid: el.id || '', ac: (el.getAttribute('autocomplete') || '').toLowerCase(),
      ph: el.getAttribute('placeholder') || '', aria: el.getAttribute('aria-label') || '', label: kind === 'xradio' ? (el.getAttribute('aria-label') || text) : labelOf(el),
      group: radio ? groupOf(el) : '',
      hints: [...el.attributes].filter((a) => a.name.startsWith('data-') && !a.name.startsWith('data-sf-') && a.value.length < 40).map((a) => `${a.name}=${a.value}`).join(' ').slice(0, 120),
      maxlen: el.maxLength > 0 ? el.maxLength : null, vis,
      off: !!(el.disabled || el.readOnly || el.getAttribute('aria-disabled') === 'true'),
      chrome: !!el.closest('header,nav,footer,[role=banner],[role=navigation],[role=contentinfo]'),   // site menus, locale pickers
      inform: !!el.closest('form,fieldset,[role=form],[role=dialog],[role=group],[role=radiogroup]'), hp: (el.getAttribute('aria-haspopup') || '').toLowerCase(),
      has: kind === 'xradio' ? el.getAttribute('aria-checked') === 'true' : !!el.value,
      options: tag === 'select' ? [...el.options].slice(0, 400).map((o) => [o.value, o.text.trim()]) : null,
      rval: type === 'radio' ? el.value : (kind === 'xradio' ? text : null), lang: (document.documentElement.lang || '').toLowerCase() });
  }
  return out;
}

const PAGE_FIELDS = withShown(pageFields);

// the options a picker shows (in the page): the list it controls (aria-controls / aria-owns), else any visible role=option,
// else the items of a visible listbox / menu, else what became visible since markPre (a list of plain buttons). Each
// option gets a data-sf-opt id. -> [{id, t}] (option texts: countries, titles, codes; never a value of his)
function listOptions(id) {
  const vis = (n) => { const r = n.getBoundingClientRect(), cs = getComputedStyle(n); return r.width > 0 && r.height > 0 && cs.visibility !== 'hidden' && cs.display !== 'none'; };
  const txt = (n) => (n.innerText || n.textContent || '').replace(/\s+/g, ' ').trim();
  const roots = [document];
  for (let i = 0; i < roots.length && i < 200; i++) for (const el of roots[i].querySelectorAll('*')) if (el.shadowRoot) roots.push(el.shadowRoot);
  const all = (sel) => roots.flatMap((r) => [...r.querySelectorAll(sel)]);
  const trig = all(`[data-sf-id="${id}"]`)[0] || null;
  const OPT = '[role=option],[role=menuitem],[role=menuitemradio],[role=menuitemcheckbox]';
  const ITEM = 'li,button,a,[role=button]';
  let opts = [];
  if (trig && trig.getAttribute('role') === 'listbox') opts = [...trig.querySelectorAll(OPT)];
  const ctl = trig && (trig.getAttribute('aria-controls') || trig.getAttribute('aria-owns'));
  const box = ctl ? (trig.getRootNode().getElementById?.(ctl.split(/\s+/)[0]) || document.getElementById(ctl.split(/\s+/)[0])) : null;
  if (!opts.length && box && vis(box)) { opts = [...box.querySelectorAll(OPT)]; if (!opts.length) opts = [...box.querySelectorAll(ITEM)]; }
  if (!opts.length) opts = all(OPT).filter(vis);
  if (!opts.length) for (const lb of all('[role=listbox],[role=menu]').filter(vis)) opts.push(...lb.querySelectorAll(ITEM));
  if (!opts.length) opts = all(`${ITEM},[tabindex]`).filter((n) => !n.hasAttribute('data-sf-pre') && n !== trig && !n.contains(trig) && vis(n) && txt(n).length <= 60);
  opts = opts.filter((n) => !n.matches('[aria-disabled=true],[disabled]') && !(trig && n.contains(trig)));
  return opts.slice(0, 600).map((n) => {
    let oid = n.getAttribute('data-sf-opt');
    if (!oid) { oid = 'op' + Math.random().toString(36).slice(2, 9); n.setAttribute('data-sf-opt', oid); }
    return { id: oid, t: (txt(n) || n.getAttribute('aria-label') || '').slice(0, 80) };
  });
}
// before opening a list: tag what is already visible, so listOptions' last resort sees only what the click opened
function markPre(on) {
  const roots = [document];
  for (let i = 0; i < roots.length && i < 200; i++) for (const el of roots[i].querySelectorAll('*')) if (el.shadowRoot) roots.push(el.shadowRoot);
  const vis = (n) => { const r = n.getBoundingClientRect(); return r.width > 0 && r.height > 0 && getComputedStyle(n).visibility !== 'hidden'; };
  for (const r of roots) for (const n of r.querySelectorAll('li,button,a,input,[role=button],[tabindex],[data-sf-pre]')) {
    if (on && vis(n)) n.setAttribute('data-sf-pre', ''); else n.removeAttribute('data-sf-pre');
  }
}

// checkboxes for --tick: every input[type=checkbox] / [role=checkbox] with its visible label texts (never values)
function pageBoxes(shown) {
  const txt = (n) => (n ? (n.innerText || n.textContent || '') : '').replace(/\s+/g, ' ').trim();
  const vis = (n) => !!n && shown(n);
  const out = [];
  for (const el of document.querySelectorAll('input[type=checkbox],[role=checkbox]')) {
    const texts = [];
    const by = (el.getAttribute('aria-labelledby') || '').split(/\s+/).map((i) => document.getElementById(i)).filter(Boolean);
    if (by.length) texts.push(by.map(txt).join(' '));
    const labs = el.labels ? [...el.labels] : [];
    const cl = el.closest('label'); if (cl && !labs.includes(cl)) labs.push(cl);
    for (const l of labs) texts.push(txt(l));
    if (el.getAttribute('aria-label')) texts.push(el.getAttribute('aria-label'));
    if (el.tagName !== 'INPUT') texts.push(txt(el));
    const p = el.parentElement;
    const alone = p && p.querySelectorAll('input[type=checkbox],[role=checkbox]').length === 1;
    if (alone) texts.push(txt(p));
    let id = el.getAttribute('data-sf-box');
    if (!id) { id = 'bx' + Math.random().toString(36).slice(2, 9); el.setAttribute('data-sf-box', id); }
    out.push({ id, texts: texts.filter(Boolean).map((t) => t.slice(0, 500)),
      checked: el.tagName === 'INPUT' ? el.checked : el.getAttribute('aria-checked') === 'true',
      visible: vis(el) || labs.some(vis) || (alone && vis(p)), selfVisible: vis(el),
      disabled: !!el.disabled || el.getAttribute('aria-disabled') === 'true' });
  }
  return out;
}
const tickNorm = (s) => norm(s).replace(/^[*\s]+|[*\s]+$/g, '');
// each listed label -> exactly one visible checkbox in the allowed frames; anything else is a problem (fail closed)
async function resolveTicks(page, inp, labels) {
  const frames = [];
  for (const fr of page.frames()) {
    let h = hostOf(fr.url());
    if (!h) h = hostOf(await fr.evaluate(() => location.href).catch(() => ''));
    if (!frameOk(h, inp, fr === page.mainFrame())) continue;
    const boxes = await fr.evaluate(withShown(pageBoxes)).catch(() => []);
    if (boxes.length) frames.push({ frame: fr, host: h, boxes });
  }
  const res = [];
  for (const label of labels) {
    const w = tickNorm(label);
    const hits = [];
    for (const f of frames) for (const b of f.boxes) if (b.visible && w && b.texts.some((t) => tickNorm(t) === w)) hits.push({ frame: f.frame, host: f.host, box: b });
    res.push({ label, hits });
  }
  return res;
}
const tickProblems = (res) => res.filter((r) => r.hits.length !== 1 || r.hits[0].box.disabled).map((r) => ({ label: r.label,
  problem: !r.hits.length ? 'not found' : r.hits.length > 1 ? `ambiguous (${r.hits.length} boxes)` : 'disabled' }));
const boxState = (fr, id) => fr.locator(`[data-sf-box="${id}"]`).evaluate((el) => (el.tagName === 'INPUT' ? el.checked : el.getAttribute('aria-checked') === 'true'), null, { timeout: 3000 }).catch(() => null);
async function tickOne(h) {
  const fr = h.frame, id = h.box.id;
  if (await boxState(fr, id)) return 'already';
  if (h.box.selfVisible) await fr.locator(`[data-sf-box="${id}"]`).check({ timeout: 4000 }).catch(() => {});
  if (!(await boxState(fr, id))) await fr.locator(`[data-sf-box="${id}"]`).evaluate((el) => el.click(), null, { timeout: 3000 }).catch(() => {});
  await fr.page().waitForTimeout(150);
  return (await boxState(fr, id)) ? 'ticked' : 'failed';
}
// resolve all first (nothing ticked unless every label is unique), then tick; -> {results, problems}
async function doTicks(page, inp, labels) {
  const res = await resolveTicks(page, inp, labels);
  const problems = tickProblems(res);
  if (problems.length) return { problems, results: [], hits: [] };
  const results = [];
  for (const r of res) {
    const st = await tickOne(r.hits[0]);
    results.push({ label: r.label, result: st });
    if (st === 'failed') return { problems: [{ label: r.label, problem: 'did not stay ticked' }], results, hits: [] };
  }
  return { problems: [], results, hits: res.map((r) => r.hits[0]) };
}

// ---------------------------------------------------------------- browser
async function connect(cdp) {
  const PW = process.env.SECUREFILL_PLAYWRIGHT || `${HOME}/opt/browser-mcp/node_modules/playwright-core`;
  const { chromium } = require(PW);
  const browser = await chromium.connectOverCDP(cdp || 'http://127.0.0.1:9222', { timeout: 15000 });
  return browser.contexts().flatMap((c) => c.pages());
}
function pickPage(pages, inp) {
  const strip = (u) => String(u || '').split('#')[0];
  if (inp.tab_url) {
    const m = pages.filter((p) => strip(p.url()) === strip(inp.tab_url));
    if (m.length === 1) return m[0];
    if (m.length > 1) fail('several tabs show that exact url; close the extra one');
    fail('no tab shows that url (pass --tab-url = your tab\'s location.href)');
  }
  const ts = site(inp.task_host);
  const m = pages.filter((p) => { const h = hostOf(p.url()); return h && (site(h) === ts || hostMatch(h, inp.psp)); });
  if (m.length === 1) return m[0];
  fail(m.length ? `${m.length} tabs are on ${ts}: pass --tab-url (your tab's location.href)` : `no tab on ${ts}`);
}
function frameOk(h, inp, main) {
  if (!h) return false;
  if (inp.allow_hosts) return inp.allow_hosts.includes(h);
  return site(h) === site(inp.task_host) || hostMatch(h, inp.psp) || (main && h === inp.task_host);
}
// a frame counts only when its <iframe> element, and each one above it, is shown() in its parent (fail closed)
async function frameShown(fr) {
  for (let f = fr; f.parentFrame(); f = f.parentFrame()) {
    const fe = await f.frameElement().catch(() => null);
    if (!fe) return false;
    const ok = await fe.evaluate(SHOWN_EL).catch(() => false);
    fe.dispose().catch(() => {});
    if (ok !== true) return false;
  }
  return true;
}
async function scan(page, inp) {
  const mainHost = hostOf(page.url());
  const frames = [];
  for (const fr of page.frames()) {
    let h = hostOf(fr.url());
    if (!h) h = hostOf(await fr.evaluate(() => location.href).catch(() => ''));   // OOPIFs loaded before we attached
    const ok = frameOk(h, inp, fr === page.mainFrame());
    let fields = [];
    try { fields = await fr.evaluate(PAGE_FIELDS); } catch { continue; }
    if (!fields.length) continue;
    const shownFrame = fr === page.mainFrame() || await frameShown(fr);
    if (!shownFrame) for (const f of fields) f.vis = false;      // a hidden frame: none of its fields
    const profile = (inp.profiles || {})[mainHost] || (inp.profiles || {})[h];
    for (const f of fields) {
      f.key = f.vis && !f.off ? classify(f, profile) : null;
      if (!f.key || !PICKERS.has(f.kind) || isChoice(f.key)) continue;
      if (f.kind === 'typeahead') f.kind = 'input';    // a city / street autocomplete: typed like any text field
      else f.key = null;                               // other buttons and lists are never clicked
    }
    for (const f of fields) {                          // no site menus or locale pickers; a bare expander only inside a form
      if (f.key && PICKERS.has(f.kind) && f.kind !== 'typeahead' && (f.chrome || (f.kind === 'popup' && !f.hp && !f.inform))) f.key = null;
    }
    const ctx = { hasCC: fields.some((f) => f.key === 'contact.phone_cc'), hasHouse: fields.some((f) => f.key === 'address.house_number'), lang: fields[0]?.lang };
    frames.push({ frame: fr, host: h, ok, main: fr === page.mainFrame(), shown: shownFrame, fields, ctx });
  }
  return { mainHost, frames };
}
const view = (s, inp) => ({ page_host: s.mainHost, site_ok: frameOk(s.mainHost, inp, true),
  frames: s.frames.map((f) => ({ host: f.host, main: f.main, allowed: f.ok, ...(f.shown ? {} : { hidden: true }),
    fields: f.fields.filter((x) => x.key).map((x) => ({ key: x.key, kind: x.kind, label: scrub(x.label || x.aria || x.ph || x.name).slice(0, 50), filled: x.has })),
    unknown: f.fields.filter((x) => !x.key && x.vis && !x.off).length,
    needs: [...new Set(f.fields.filter((x) => x.key).flatMap((x) => needs(x.key, f.ctx)))] })),
  card_frames_blocked: s.frames.filter((f) => !f.ok && f.fields.some((x) => x.key && x.key.startsWith('card.'))).map((f) => f.host) });

const esc = (s) => s.replace(/[.*+?^${}()|[\]\\]/g, '\\$&');
// the option (normalized texts) that carries w: exact, in the order of w's alternatives (Ms before Mrs), then the calling
// code, then a country name as a whole word ("🇨🇭 Switzerland (+41)"); -1 = none
function bestOption(texts, w) {
  const cands = (w.alts || [w.text]).map(norm).filter(Boolean);
  for (const c of cands) { const i = texts.indexOf(c); if (i >= 0) return i; }
  if (w.cc) { const rx = new RegExp(`\\+\\s?${w.cc.slice(1)}(?!\\d)`); const i = texts.findIndex((t) => rx.test(t)); if (i >= 0) return i; }
  if (w.country || w.cc) {
    for (const c of cands) {
      if (c.length <= 3) continue;
      const rx = new RegExp(`(^|[^\\p{L}])${esc(c)}([^\\p{L}]|$)`, 'u'); const i = texts.findIndex((t) => rx.test(t)); if (i >= 0) return i;
    }
  }
  return -1;
}
// still shown right before a value goes in (the page can change between the scan and the fill)
const stillShown = (fr, f) => fr.locator(`[data-sf-id="${f.id}"]`).evaluate(SHOWN_EL, null, { timeout: 3000 }).then((v) => v === true, () => false);
async function pickOption(fr, f, w) {
  if (!(await stillShown(fr, f))) return 'hidden';
  const opts = f.options || [];
  let idx = -1;
  for (const c of (w.alts || [w.text]).map(norm)) { idx = opts.findIndex(([v, t]) => norm(v) === c || norm(t) === c); if (idx >= 0) break; }
  if (idx < 0 && w.cc) { const rx = new RegExp(`\\+?${w.cc.slice(1)}(?!\\d)`); const hits = opts.map(([v, t], i) => (rx.test(t) || rx.test(v) ? i : -1)).filter((i) => i >= 0); idx = hits.length ? hits[0] : -1; }
  if (idx < 0 && w.country) idx = bestOption(opts.map(([, t]) => norm(t)), w);
  if (idx < 0) return 'no_option';
  await fr.locator(`[data-sf-id="${f.id}"]`).selectOption({ index: idx }, { timeout: 5000 });
  return 'ok';
}
// a list that wants typing (a filter box in the popup, or a type-to-jump listbox): fill the box / type the name
async function typeFilter(fr, f, w) {
  const how = await fr.evaluate(withShown((shown, tid) => {
    const vis = (n) => shown(n);
    let a = document.activeElement; while (a && a.shadowRoot && a.shadowRoot.activeElement) a = a.shadowRoot.activeElement;
    const textish = (n) => n && n.matches('input:not([type=checkbox]):not([type=radio]):not([type=button]):not([type=submit]),[role=searchbox],[role=textbox]');
    let box = textish(a) && a.getAttribute('data-sf-id') !== tid && !a.hasAttribute('data-sf-pre') ? a : null;
    if (!box) box = [...document.querySelectorAll('input,[role=searchbox]')].find((n) => textish(n) && vis(n) && !n.hasAttribute('data-sf-pre')) || null;
    if (box) { box.setAttribute('data-sf-filter', '1'); return 'box'; }
    if (a && (a.getAttribute('data-sf-id') === tid || a.closest('[role=listbox],[role=menu]'))) return 'keys';
    return '';
  }), f.id).catch(() => '');
  if (how === 'box') {
    const b = fr.locator('[data-sf-filter="1"]').first();
    await b.fill(w.text, { timeout: 4000 }).catch(() => b.pressSequentially(w.text, { delay: 40, timeout: 8000 }).catch(() => {}));
    await b.evaluate((n) => n.removeAttribute('data-sf-filter')).catch(() => {});
    return true;
  }
  if (how === 'keys') { await fr.page().keyboard.type(w.text.slice(0, 10), { delay: 60 }).catch(() => {}); return true; }
  return false;
}
// a custom dropdown (button / combobox that opens a list, or an always-open listbox): open it, find the option, click it,
// read the trigger back. Options can be role=option items or plain buttons in the list it opened.
async function pickList(fr, f, w) {
  const page = fr.page(), el = fr.locator(`[data-sf-id="${f.id}"]`);
  const list = () => fr.evaluate(listOptions, f.id).catch(() => []);
  const pick = (os) => bestOption(os.map((o) => norm(o.t)), w);
  const open = f.kind !== 'listbox';
  if (open) {
    const cur = norm(await el.innerText({ timeout: 2000 }).catch(() => ''));
    if (cur && pick([{ t: cur }]) === 0) return 'same';
    await fr.evaluate(markPre, true).catch(() => {});
    await el.click({ timeout: 5000 });
    await page.waitForTimeout(250);
  }
  try {
    let os = await list();
    if (!os.length) { await page.waitForTimeout(600); os = await list(); }
    let i = pick(os);
    if (i < 0 && open && await typeFilter(fr, f, w)) { await page.waitForTimeout(600); os = await list(); i = pick(os); }
    if (i < 0) { if (open) await page.keyboard.press('Escape').catch(() => {}); return os.length ? 'no_option' : 'no_list'; }
    const o = fr.locator(`[data-sf-opt="${os[i].id}"]`).first();
    await o.scrollIntoViewIfNeeded({ timeout: 3000 }).catch(() => {});
    await o.click({ timeout: 5000 });
    await page.waitForTimeout(250);
    if (!open) return (await o.getAttribute('aria-selected', { timeout: 2000 }).catch(() => null)) === 'false' ? 'unverified' : 'ok';
    const after = norm(await el.innerText({ timeout: 2000 }).catch(() => '')) || norm(await el.inputValue({ timeout: 1000 }).catch(() => ''));
    return after && (after.includes(norm(os[i].t)) || pick([{ t: after }]) === 0) ? 'ok' : 'unverified';
  } finally { await fr.evaluate(markPre, false).catch(() => {}); }
}
// a typeahead input (role=combobox / aria-autocomplete): type the name, click the matching suggestion
async function pickTypeahead(fr, f, w) {
  if (!(await stillShown(fr, f))) return 'hidden';
  const page = fr.page(), el = fr.locator(`[data-sf-id="${f.id}"]`);
  const now = norm(await el.inputValue({ timeout: 3000 }).catch(() => ''));
  if (now && bestOption([now], w) === 0) return 'same';
  await fr.evaluate(markPre, true).catch(() => {});
  try {
    await el.fill('', { timeout: 5000 }).catch(() => {});
    await el.pressSequentially(w.text, { delay: 40, timeout: 15000 });
    await page.waitForTimeout(600);
    const os = await fr.evaluate(listOptions, f.id).catch(() => []);
    const i = bestOption(os.map((o) => norm(o.t)), w);
    if (i < 0) return os.length ? 'no_option' : 'typed';
    await fr.locator(`[data-sf-opt="${os[i].id}"]`).first().click({ timeout: 5000 });
    await page.waitForTimeout(250);
    const v = norm(await el.inputValue({ timeout: 3000 }).catch(() => ''));
    return v && bestOption([v], w) === 0 ? 'ok' : 'unverified';
  } finally { await fr.evaluate(markPre, false).catch(() => {}); }
}
async function fillOne(fr, f, w) {
  if (f.kind === 'select') return pickOption(fr, f, w);
  if (f.kind === 'combobox' || f.kind === 'popup' || f.kind === 'listbox') return pickList(fr, f, w);
  if (f.kind === 'typeahead') return pickTypeahead(fr, f, w);
  if (f.kind === 'radio' || f.kind === 'xradio') return 'skipped';
  if (!(await stillShown(fr, f))) return 'hidden';
  const el = fr.locator(`[data-sf-id="${f.id}"]`);
  const same = (got) => (w.digits ? digits(got) === digits(w.text) : norm(got) === norm(w.text));
  const now = await el.inputValue({ timeout: 3000 }).catch(() => '');
  if (now && same(now)) return 'same';
  await el.fill(w.text, { timeout: 5000 });
  if (same(await el.inputValue({ timeout: 3000 }).catch(() => ''))) return 'ok';
  await el.fill('', { timeout: 5000 }).catch(() => {});
  await el.pressSequentially(w.text, { delay: 30, timeout: 15000 });
  return same(await el.inputValue({ timeout: 3000 }).catch(() => '')) ? 'ok' : 'mismatch';
}
// a radio group (inputs, or buttons / divs with role=radio): the one whose value or label carries w
async function radios(fr, fields, key, w) {
  const group = fields.filter((f) => (f.kind === 'radio' || f.kind === 'xradio') && f.key === key);
  if (!group.length) return null;
  const cands = (w.alts || [w.text]).map(norm);
  let hit = null;
  for (const c of cands) { hit = group.find((f) => norm(f.rval) === c || norm(f.label) === c); if (hit) break; }
  if (!hit) hit = group.find((f) => cands.some((c) => c.length > 1 && norm(f.label).split(' ').includes(c)));
  if (!hit) return 'no_option';
  const loc = fr.locator(`[data-sf-id="${hit.id}"]`);
  if (hit.kind === 'radio') { await loc.check({ timeout: 5000 }); return 'ok'; }
  if ((await loc.getAttribute('aria-checked', { timeout: 2000 }).catch(() => null)) === 'true') return 'same';
  await loc.click({ timeout: 5000 });
  await fr.page().waitForTimeout(150);
  return (await loc.getAttribute('aria-checked', { timeout: 2000 }).catch(() => null)) === 'true' ? 'ok' : 'unverified';
}
async function mask(page) {
  let n = 0;
  for (const fr of page.frames()) {
    try {
      n += await fr.evaluate((css) => {
        let st = document.getElementById('securefill-mask');
        if (!st) { st = document.createElement('style'); st.id = 'securefill-mask'; st.textContent = css; (document.head || document.documentElement).appendChild(st); }
        for (const el of document.querySelectorAll('[data-sf-filled]')) el.style.setProperty('-webkit-text-security', 'disc', 'important');
        return 1;
      }, MASK_CSS);
    } catch { /* a frame that went away */ }
  }
  return n;
}
// page text that shows a released value (a name in a heading, the e-mail on a review page) gets a black CSS highlight
// (CSS Custom Highlight API: no DOM change), so a screenshot shows no value; in every frame and open shadow root
function collectText() {
  const nodes = [], roots = [document];
  for (let i = 0; i < roots.length && i < 200; i++) {
    const tw = document.createTreeWalker(roots[i], NodeFilter.SHOW_ELEMENT | NodeFilter.SHOW_TEXT);
    for (let n = tw.nextNode(); n && nodes.length < 20000; n = tw.nextNode()) {
      if (n.nodeType === 1) { if (n.shadowRoot) roots.push(n.shadowRoot); continue; }
      const p = n.parentElement;
      if (!p || /^(SCRIPT|STYLE|NOSCRIPT|TEMPLATE)$/.test(p.tagName) || n.data.trim().length < 2) continue;
      nodes.push(n);
    }
  }
  window.__sfText = nodes;
  return nodes.map((n) => n.data.slice(0, 4000));
}
function highlight(marks) {
  const nodes = window.__sfText || [];
  if (!window.CSS || !CSS.highlights || typeof Highlight === 'undefined') return -1;
  const hl = CSS.highlights.get('securefill') || new Highlight();
  const css = '::highlight(securefill){background-color:#000;color:#000;text-decoration:none}';
  let n = 0;
  for (const [i, a, b] of marks) {
    const nd = nodes[i]; if (!nd || !nd.isConnected) continue;
    try {
      const r = new Range(); r.setStart(nd, a); r.setEnd(nd, Math.min(b, nd.data.length)); hl.add(r); n++;
      const root = nd.getRootNode();                     // the rule must live in the node's own tree (shadow roots)
      const host = root === document ? (document.head || document.documentElement) : root;
      if (!host.querySelector?.('style[data-sf-hl]')) { const st = document.createElement('style'); st.setAttribute('data-sf-hl', ''); st.textContent = css; host.appendChild(st); }
    } catch { /* a node that changed */ }
  }
  CSS.highlights.set('securefill', hl);
  return n;
}
// his personal data from the private profile (+ patterns: cards, IBANs, what the profile lacks), with or without a
// fill: ownermask.py spans over the frame's text nodes -> [[node, start, end]]. Fails closed: an error throws, so the
// op fails and no screenshot is taken.
const OWNERMASK = path.join(path.dirname(fileURLToPath(import.meta.url)), 'ownermask.py');
function ownerMarks(texts, keepTown) {
  if (!texts.length) return [];
  const r = spawnSync('python3', ['-I', OWNERMASK, 'spans'], { input: JSON.stringify({ texts, keep_town: !!keepTown }), encoding: 'utf8', timeout: 30000, maxBuffer: 64 << 20 });
  let o = null;
  try { o = JSON.parse(String(r.stdout || '').trim().split('\n').pop()); } catch { o = null; }
  if (r.status !== 0 || !o || !o.ok || !Array.isArray(o.marks)) throw new Error('ownermask: the page text could not be checked for personal data (no screenshot)');
  return o.marks;
}
// keepTown (the screenshots for his approval / result cards): the black highlights start over and the town next to
// his postcode stays readable, so the card shows him his own delivery town (street, name, postcode stay masked)
async function maskText(page, items, keyHex, keepTown = false) {
  const live = !!(items && items.length && keyHex);
  const H = live ? (s) => crypto.createHmac('sha256', Buffer.from(keyHex, 'hex')).update(s).digest('hex').slice(0, 32) : null;
  const fp = live ? fpIndex(items) : null, cache = new Map();
  let n = 0;
  for (const fr of page.frames()) {
    let texts, vals = [];
    try { texts = await fr.evaluate(collectText); } catch { continue; }
    try { vals = await fr.evaluate(collectInputs); } catch { vals = []; }
    const om = ownerMarks(texts.concat(vals), keepTown), nt = texts.length;     // one helper call: text nodes, then input values
    if (keepTown) try { await fr.evaluate(() => { if (window.CSS && CSS.highlights) CSS.highlights.delete('securefill'); }); } catch { /* a frame that went away */ }
    const marks = om.filter((m) => m[0] < nt), hide = new Set(om.filter((m) => m[0] >= nt).map((m) => m[0] - nt));
    if (live) {
      texts.forEach((t, i) => { for (const [a, b] of textSpans(t, fp, H, cache)) marks.push([i, a, b]); });
      vals.forEach((t, i) => { if (textSpans(t, fp, H, cache).length) hide.add(i); });
    }
    if (hide.size) try { n += await fr.evaluate(hideInputs, [...hide]); } catch { /* a frame that went away */ }
    if (!marks.length) continue;
    try { const k = await fr.evaluate(highlight, marks); n += k > 0 ? k : 0; } catch { /* a frame that went away */ }
  }
  return n;
}
// input / textarea values a site prefilled (a saved address form): the whole field shown as discs when it holds his data
function collectInputs() {
  const els = [], roots = [document];
  const SKIP = /^(hidden|password|checkbox|radio|submit|button|image|file|range|color|reset)$/i;
  for (let i = 0; i < roots.length && i < 200; i++) {
    const tw = document.createTreeWalker(roots[i], NodeFilter.SHOW_ELEMENT);
    for (let e = tw.nextNode(); e && els.length < 2000; e = tw.nextNode()) {
      if (e.shadowRoot) roots.push(e.shadowRoot);
      if (!((e.tagName === 'INPUT' && !SKIP.test(e.type || '')) || e.tagName === 'TEXTAREA')) continue;
      if (String(e.value || '').trim().length >= 2) els.push(e);
    }
  }
  window.__sfInputs = els;
  return els.map((e) => String(e.value).slice(0, 4000));
}
function hideInputs(idx) {
  const els = window.__sfInputs || [];
  let n = 0;
  for (const i of idx) {
    const e = els[i]; if (!e || !e.isConnected) continue;
    e.style.setProperty('-webkit-text-security', 'disc', 'important');
    if (e.tagName === 'TEXTAREA') e.style.setProperty('color', 'transparent', 'important');
    n++;
  }
  return n;
}
const fpKeyHex = (inp) => { try { return fs.readFileSync(inp.fp_key_path, 'utf8').trim(); } catch { return ''; } };

function fingerprints(inp, vals) {
  const keyHex = fs.readFileSync(inp.fp_key_path, 'utf8').trim();
  const H = (s) => crypto.createHmac('sha256', Buffer.from(keyHex, 'hex')).update(s).digest('hex').slice(0, 32);
  const items = textItems(vals, H);
  const add = (kind, s) => { if (s) items.push({ kind, n: s.length, h: H(`${kind}:${s}`) }); };
  for (const [k, v] of Object.entries(vals)) {
    if (k === 'card.number' || k === 'contact.phone_national') add('digits', digits(v));
    if (k === 'card.cvc') add('cvc', digits(v));
    if (k === 'passport.number' || k === 'id_card.number') add('alnum', String(v).replace(/[^A-Za-z0-9]/g, '').toUpperCase());
    if (DATE_KEYS.has(k)) add('date', v);
  }
  if (vals['contact.phone_national'] && vals['contact.phone_cc']) add('digits', digits(vals['contact.phone_cc']) + digits(vals['contact.phone_national']));
  const rec = { v: 2, task: inp.task, site: inp.site, created: Date.now() / 1000, expires: Date.now() / 1000 + (inp.fp_ttl || 86400), items };
  const tmp = `${inp.fp_out}.${process.pid}.tmp`;
  fs.writeFileSync(tmp, JSON.stringify(rec), { mode: 0o600 });
  fs.renameSync(tmp, inp.fp_out);
  return items;
}

// ---------------------------------------------------------------- ops
async function main() {
  const inp = JSON.parse(fs.readFileSync(0, 'utf8'));
  if (inp.op === 'keygen') {
    const { privateKey } = crypto.generateKeyPairSync('x25519');
    const jwk = privateKey.export({ format: 'jwk' });
    const kid = crypto.randomBytes(8).toString('hex');
    fs.mkdirSync(inp.keydir, { recursive: true, mode: 0o700 });
    fs.writeFileSync(path.join(inp.keydir, `${kid}.key`), JSON.stringify(jwk), { mode: 0o600, flag: 'wx' });
    return done({ ok: true, key_id: kid, pub: Buffer.from(jwk.x, 'base64url').toString('base64') });
  }
  if (inp.op === 'fpitems') {                       // tests: the text fingerprint items of given (fake) values, hashes only
    const keyHex = fs.readFileSync(inp.fp_key_path, 'utf8').trim();
    const H = (s) => crypto.createHmac('sha256', Buffer.from(keyHex, 'hex')).update(s).digest('hex').slice(0, 32);
    return done({ ok: true, items: textItems(inp.vals || {}, H) });
  }
  const page = pickPage(await connect(inp.cdp), inp);
  if (inp.op === 'mask') {
    const frames = await mask(page);
    return done({ ok: true, frames, text_masked: await maskText(page, inp.fp_items, fpKeyHex(inp)), page_host: hostOf(page.url()) });
  }
  if (inp.op === 'shot') {                            // the card / result screenshot: his town stays readable
    await mask(page);
    await maskText(page, inp.fp_items, fpKeyHex(inp), true);
    await page.screenshot({ path: inp.path, timeout: 15000 });
    fs.chmodSync(inp.path, 0o600);
    return done({ ok: true, path: inp.path, page_host: hostOf(page.url()) });
  }
  const s = await scan(page, inp);
  if (inp.op === 'probe') {
    const o = { ok: true, ...view(s, inp) };
    if ((inp.ticks || []).length) o.tick_check = (await resolveTicks(page, inp, inp.ticks)).map((r) => ({ label: r.label, matches: r.hits.length, checked: r.hits.length === 1 ? r.hits[0].box.checked : null, disabled: r.hits.length === 1 ? r.hits[0].box.disabled : null }));
    return done(o);
  }
  if (inp.op === 'tick') {
    if (!frameOk(s.mainHost, inp, true)) fail(`the tab is on ${s.mainHost}, not on the task's site: nothing ticked`, { refused: 'site' });
    const t = await doTicks(page, inp, inp.ticks || []);
    if (t.problems.length) return done({ ok: false, refused: 'tick', error: 'listed box not ticked: ' + t.problems.map((p) => `“${p.label}” ${p.problem}`).join('; '), tick_problems: t.problems, ticks: t.results }, 3);
    return done({ ok: true, ticks: t.results, ticked: t.results.filter((r) => r.result === 'ticked').length });
  }
  if (inp.op !== 'fill') fail(`unknown op ${inp.op}`);
  // ---- fill: the approved site, then open the box (key used once), then type
  if (s.mainHost !== inp.site) fail(`the tab is on ${s.mainHost}, the approval is for ${inp.site}: nothing filled`, { refused: 'site' });
  let tk = { problems: [], results: [], hits: [] };
  if ((inp.ticks || []).length) {                    // the listed boxes first: a missing/ambiguous one stops everything
    tk = await doTicks(page, inp, inp.ticks);
    if (tk.problems.length) {
      try { fs.unlinkSync(inp.key_path); } catch { /* */ }
      return done({ ok: false, refused: 'tick', error: 'listed box not ticked: ' + tk.problems.map((p) => `“${p.label}” ${p.problem}`).join('; ') + ': nothing filled, nothing clicked',
        tick_problems: tk.problems, ticks: tk.results, filled: {}, clicked: false }, 3);
    }
  }
  const jwk = JSON.parse(fs.readFileSync(inp.key_path, 'utf8'));
  fs.unlinkSync(inp.key_path);
  const vals = unseal(inp.sealed, jwk, inp.rid);
  SECRETS = Object.values(vals).map(String).filter((x) => x.length >= 3);
  const only = new Set(inp.only || []);
  const res = {}; const filledVals = {};
  for (const fr of s.frames) {
    if (!fr.ok) continue;
    const done1 = new Set();
    for (const f of fr.fields) {
      if (!f.key) continue;
      const base = f.key.split('@')[0];
      if (only.size && !only.has(base.split('.')[0]) && !only.has(base)) continue;
      if (base.startsWith('card.') && !inp.card) continue;
      const w = want(f.key, f, vals, fr.ctx);
      const k = `${f.key}${fr.main ? '' : ` [${fr.host}]`}`;
      if (!w) { if (!res[k]) res[k] = 'no_value'; continue; }   // not in the vault (title: neither title nor sex)
      let r;
      try {
        if (f.kind === 'radio' || f.kind === 'xradio') { if (done1.has(f.key)) continue; done1.add(f.key); r = await radios(fr.frame, fr.fields, f.key, w); } else r = await fillOne(fr.frame, f, w);
      } catch (e) { r = /timeout/i.test(String(e?.message)) ? 'timeout' : 'error'; }
      res[k] = res[k] && res[k] !== 'ok' && res[k] !== 'same' && res[k] !== 'no_value' ? res[k] : r;
      if (r === 'ok' || r === 'same') {
        for (const n of needs(f.key, fr.ctx)) if (vals[n] != null) filledVals[n] = vals[n];
        if (f.kind === 'input') await fr.frame.locator(`[data-sf-id="${f.id}"]`).evaluate((el) => el.setAttribute('data-sf-filled', '1')).catch(() => {});
      }
    }
  }
  await mask(page);
  const fpItems = Object.keys(vals).length ? fingerprints(inp, vals) : [];   // every released value, filled or not
  const nfp = fpItems.length;
  const hlItems = [...(inp.fp_items || []), ...fpItems];
  await maskText(page, hlItems, fpKeyHex(inp)).catch(() => 0);
  const out = { ok: Object.values(res).some((r) => r === 'ok' || r === 'same'), filled: res, fingerprints: nfp,
    blocked_frames: view(s, inp).card_frames_blocked };
  if (tk.results.length) { out.ticks = tk.results; out.ticked = tk.results.filter((r) => r.result === 'ticked').length; }
  if (inp.click && out.ok) {
    const bad = Object.entries(res).filter(([k, r]) => k.startsWith('card.') && !['ok', 'same', 'no_value'].includes(r));
    const haveNumber = Object.keys(res).some((k) => k.startsWith('card.number'));
    if (inp.card && (bad.length || !haveNumber)) { out.clicked = false; out.why = 'card fields incomplete: not clicked'; return done(out, 3); }
    let btn = inp.click.target ? page.locator(inp.click.target) : page.getByRole('button', { name: inp.click.label, exact: true });
    if ((await btn.count()) !== 1 && !inp.click.target) btn = page.locator('button,input[type=submit],[role=button],a').filter({ hasText: new RegExp(`^\\s*${inp.click.label.replace(/[.*+?^${}()|[\]\\]/g, '\\$&')}\\s*$`, 'i') });
    const c = await btn.count();
    if (c !== 1) { out.clicked = false; out.why = c ? `${c} buttons match the label: not clicked` : 'button not found: not clicked'; return done(out, 3); }
    for (const h of tk.hits) {                        // the listed boxes must still be ticked right before the click
      if (await boxState(h.frame, h.box.id)) continue;
      if ((await tickOne(h)) === 'failed') { out.clicked = false; out.why = 'a listed box is no longer ticked: not clicked'; return done(out, 3); }
    }
    await btn.click({ timeout: 10000 });
    out.clicked = true;
    await page.waitForLoadState('load', { timeout: 20000 }).catch(() => {});
    await page.waitForTimeout(inp.settle_ms ?? 2500);
    await mask(page).catch(() => {});
    const masked = await maskText(page, hlItems, fpKeyHex(inp), true).then(() => true, () => false);
    out.after = { host: hostOf(page.url()), path: (() => { try { return new URL(page.url()).pathname.slice(0, 80); } catch { return ''; } })() };
    if (inp.shot_path && !masked) out.shot_withheld = 'his personal data could not be masked on the result page';
    else if (inp.shot_path) { await page.screenshot({ path: inp.shot_path, timeout: 15000 }); fs.chmodSync(inp.shot_path, 0o600); out.shot = inp.shot_path; }
  }
  return done(out, out.ok ? 0 : 3);
}
main().catch((e) => fail(`${e?.name || 'Error'}: ${e?.message || ''}`));
