'use strict';
const $ = s => document.querySelector(s);
const V = window.__voice = { events: [], metrics: [], tPtt: 0 };
let ws = null, ctx = null, mic = null, node = null, playHead = 0, talking = false, live = false;
const sources = new Set();
let cur = { user: null, assistant: null };

function setState(text, cls) { $('#state').textContent = text; $('#dot').className = 'dot ' + (cls || ''); }
function line(who, text) {
  const el = document.createElement('div');
  el.className = 'msg ' + (who === 'user' ? 'you' : who === 'sys' ? 'sys' : 'bot');
  el.textContent = text; $('#log').appendChild(el); el.scrollIntoView({ block: 'end' }); return el;
}
function transcript(who, text) {
  if (who === 'user') cur.assistant = null; else cur.user = null;
  if (!cur[who]) cur[who] = line(who, text.trimStart()); else cur[who].textContent += text;
  cur[who].scrollIntoView({ block: 'end' });
}
function working(n) {
  $('#working').hidden = !n; $('#wcount').textContent = n > 1 ? '(' + n + ')' : '';
  if (live) setState(n ? 'Live · assistant working' : 'Live', n ? 'busy' : 'live');
}
function play(buf) {
  const i16 = new Int16Array(buf), f32 = new Float32Array(i16.length);
  for (let i = 0; i < i16.length; i++) f32[i] = i16[i] / 32768;
  const ab = ctx.createBuffer(1, f32.length, 24000); ab.copyToChannel(f32, 0);
  const s = ctx.createBufferSource(); s.buffer = ab; s.connect(ctx.destination);
  const t = Math.max(ctx.currentTime + 0.03, playHead); s.start(t); playHead = t + ab.duration;
  sources.add(s); s.onended = () => sources.delete(s);
}
function flush() { for (const s of sources) { try { s.stop(); } catch (e) {} } sources.clear(); playHead = 0; }
function onEvent(ev) {
  V.events.push(ev);
  switch (ev.type) {
    case 'state': live = true; setState('Live', 'live'); $('#talk').disabled = $('#open').checked;
      $('#conn').textContent = 'Hang up'; break;
    case 'transcript': transcript(ev.who, ev.text); break;
    case 'turn': cur = { user: null, assistant: null }; break;
    case 'status': working(ev.working); break;
    case 'flush': flush(); break;
    case 'metric': V.metrics.push(ev); break;
    case 'error': line('sys', 'Error: ' + ev.message); break;
    case 'hangup': line('sys', 'Ended: ' + ev.reason); teardown(); break;
  }
}
async function connect() {
  $('#conn').disabled = true; setState('Connecting…');
  try {
    const r = await fetch('/api/ticket', { method: 'POST', headers: { 'X-Voice': '1' } });
    if (r.status === 401) { location.href = '/login'; return; }
    if (!r.ok) throw new Error('ticket ' + r.status);
    const { ticket } = await r.json();
    ctx = ctx || new AudioContext(); await ctx.resume();
    mic = await navigator.mediaDevices.getUserMedia({ audio: { echoCancellation: true, noiseSuppression: true, autoGainControl: true, channelCount: 1 } });
    await ctx.audioWorklet.addModule('/static/worklet.js');
    node = new AudioWorkletNode(ctx, 'pcm-capture');
    ctx.createMediaStreamSource(mic).connect(node); node.connect(ctx.destination);
    node.port.onmessage = e => { if (ws && ws.readyState === 1 && ($('#open').checked || talking)) ws.send(e.data); };
    ws = new WebSocket((location.protocol === 'https:' ? 'wss://' : 'ws://') + location.host + '/ws?ticket=' + encodeURIComponent(ticket));
    ws.binaryType = 'arraybuffer';
    ws.onopen = () => ws.send(JSON.stringify({ type: 'start', mode: $('#open').checked ? 'open' : 'ptt' }));
    ws.onmessage = e => typeof e.data === 'string' ? onEvent(JSON.parse(e.data)) : play(e.data);
    ws.onclose = () => { if (live) line('sys', 'Disconnected'); teardown(); };
  } catch (err) { line('sys', 'Could not connect: ' + err.message); teardown(); }
  finally { $('#conn').disabled = false; }
}
function teardown() {
  live = false; talking = false; flush(); working(0);
  if (ws && ws.readyState <= 1) { try { ws.send(JSON.stringify({ type: 'hangup' })); } catch (e) {} ws.close(); }
  ws = null;
  if (mic) { mic.getTracks().forEach(t => t.stop()); mic = null; }
  if (node) { node.disconnect(); node = null; }
  $('#talk').disabled = true; $('#talk').classList.remove('down');
  $('#conn').textContent = 'Connect'; setState('Not connected');
}
function ptt(down) {
  if (!live || !ws || talking === down) return;
  talking = down; $('#talk').classList.toggle('down', down);
  if (!down) V.tPtt = performance.now(); else flush();
  ws.send(JSON.stringify({ type: 'ptt', down }));
}
$('#conn').onclick = () => (live || ws ? teardown() : connect());
$('#open').onchange = () => { if (live) { teardown(); connect(); } };
const talk = $('#talk');
talk.addEventListener('pointerdown', e => { e.preventDefault(); talk.setPointerCapture(e.pointerId); ptt(true); });
['pointerup', 'pointercancel', 'lostpointercapture'].forEach(n => talk.addEventListener(n, () => ptt(false)));
$('#typeform').onsubmit = e => {
  e.preventDefault(); const t = $('#typed').value.trim();
  if (!t || !live) return;
  line('user', t); cur.user = null; V.tPtt = performance.now();
  ws.send(JSON.stringify({ type: 'text', text: t })); $('#typed').value = '';
};
